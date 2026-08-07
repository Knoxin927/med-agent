"""受控 ToolRuntime：守住 schema，并集中执行 timeout、retry、取消和审计策略。"""

# 导入 dataclass，定义不可变的工具规格与执行边界配置。
from dataclasses import dataclass
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Lock
# 导入 Callable，声明校验器与执行器的窄函数类型。
from collections.abc import Callable
import time
# 导入 Any，承载 OpenAI 工具 schema 与参数。
from typing import Any

# 导入 RankedChunk，作为成功 observation 的快照元素类型。
from app.retrieval_strategies.types import RankedChunk
# 导入错误码、ToolCall 与 observation 值对象。
from app.agent.types import (
    AgentErrorCode,
    ApprovalPolicy,
    ToolAuditTrace,
    ToolCall,
    ToolEffect,
    ToolErrorCode,
    ToolExecutionContext,
    ToolObservation,
)


class TransientToolError(Exception):
    """工具 adapter 用此异常声明本次失败可重试。"""


class TimeoutToolError(Exception):
    """确定性 adapter 用此异常声明已完成但可重试的 timeout 失败。"""


# 保存单个已注册工具的全部静态信息；不可变以避免运行时改写调用边界。
@dataclass(frozen=True)
class ToolSpec:
    """把工具名、对外 schema、校验器与执行器绑定成一个可注册单元。"""

    # 工具名，是 ToolRuntime 注册表的主键。
    tool_name: str
    # 保存随上游请求发送的 OpenAI function schema，供模型了解可用工具。
    openai_tool: dict[str, Any]
    # 校验器返回 None 表示参数合法，返回字符串表示具体非法原因。
    validator: Callable[[ToolCall], str | None]
    # 执行器返回成功或失败的 observation；不负责 schema 校验。
    executor: Callable[[ToolCall], ToolObservation]
    # 工具版本只用于脱敏审计与将来的持久化 observation，不承载业务输入。
    tool_version: str = "v1"
    # 单次等待的最大秒数；到期仅停止等待，不强杀 worker 线程。
    timeout_seconds: float = 5.0
    # 业务尝试次数上限；设计限制最多两次。
    max_attempts: int = 1
    # M3.2 只允许注册只读工具，副作用工具留给后续审批 feature。
    effect: ToolEffect = ToolEffect.read_only
    # 只读工具不需要审批；M3.2 不实现 required 的执行路径。
    approval_policy: ApprovalPolicy = ApprovalPolicy.none


@dataclass(frozen=True)
class ToolExecutionResult:
    """向 graph/loop 提供稳定 observation、执行次数和脱敏 trace。"""

    observation: ToolObservation
    error_code: ToolErrorCode | None
    attempt_count: int
    trace: ToolAuditTrace


# 统一构造失败 observation，避免在各处重复填写默认字段。
def make_failure_observation(
    call: ToolCall,
    code: AgentErrorCode,
    message: str,
) -> ToolObservation:
    """返回一个携带稳定错误码、不贡献 sources 的失败 observation。"""

    # 失败 observation 不携带 chunk 快照，保证 sources 只来自成功检索。
    # authority_payload 默认 None，失败路径不得携带权威检索结果。
    return ToolObservation(
        call_id=call.call_id,
        tool_name=call.tool_name,
        success=False,
        error_code=code,
        error_message=message,
        chunks=[],
        authority_payload=None,
    )


# 统一构造成功 observation，封装本轮可回填并可贡献 sources 的检索快照。
def make_success_observation(
    call: ToolCall,
    chunks: list[RankedChunk],
) -> ToolObservation:
    """返回一个携带本轮 RankedChunk 快照的成功 observation。"""

    # 成功路径没有错误信息，快照由传入的 chunks 决定。
    # knowledge/search_probe 成功时 authority_payload 必须为 None。
    return ToolObservation(
        call_id=call.call_id,
        tool_name=call.tool_name,
        success=True,
        error_code=None,
        error_message=None,
        chunks=chunks,
        authority_payload=None,
    )


def make_authority_success_observation(
    call: ToolCall,
    authority_payload: Any,
) -> ToolObservation:
    """返回权威检索成功 observation：chunks 固定为空，payload 必须存在。

    这是创建 authority 成功结果的唯一工厂；空 hits 列表也合法。
    Agent graph 不得消费该 payload。
    """

    # 成功权威结果不走 RankedChunk 路径，避免与 knowledge sources 混用。
    return ToolObservation(
        call_id=call.call_id,
        tool_name=call.tool_name,
        success=True,
        error_code=None,
        error_message=None,
        chunks=[],
        authority_payload=authority_payload,
    )


# 负责注册表、schema 与执行的唯一边界，避免循环或调用方直接驱动工具特例。
class ToolRuntime:
    """把模型提议经过注册表、schema 与 tool adapter 转换为唯一 observation。"""

    # 接收已注册的工具规格，构建按工具名查找的注册表。
    def __init__(self, specs: list[ToolSpec], *, allow_approved_effects: bool = False) -> None:
        # 收集每个规格以构造 O(1) 查找表。
        registry: dict[str, ToolSpec] = {}
        # 逐个登记，发现重复工具名时立即拒绝，保持严格边界。
        for spec in specs:
            # 重复注册属于配置错误，必须在执行任何模型提议前暴露。
            if spec.tool_name in registry:
                raise ValueError(f"重复注册的工具: {spec.tool_name}")
            if spec.timeout_seconds <= 0:
                raise ValueError("工具 timeout_seconds 必须大于 0")
            if spec.max_attempts not in {1, 2}:
                raise ValueError("工具 max_attempts 必须为 1 或 2")
            if not allow_approved_effects and (spec.effect is not ToolEffect.read_only or spec.approval_policy is not ApprovalPolicy.none):
                raise ValueError("默认运行时只允许无需审批的 read-only 工具")
            if allow_approved_effects and ((spec.effect is ToolEffect.side_effect) != (spec.approval_policy is ApprovalPolicy.required)):
                raise ValueError("副作用工具必须同时声明 required 审批")
            registry[spec.tool_name] = spec
        # 保存只读注册表，execute 时按名查找。
        self._registry = registry
        # 超时后的 worker 会自然完成；active 表只用于阻止同一 run/call 形成并行执行。
        self._executor = ThreadPoolExecutor(max_workers=max(1, len(registry)))
        self._active: dict[tuple[str, str], Future[ToolObservation]] = {}
        self._active_lock = Lock()
        self._closed = False

    def get_spec(self, tool_name: str) -> ToolSpec | None:
        """向 graph 暴露只读工具元数据，禁止调用方修改注册表。"""
        return self._registry.get(tool_name)

    def validate_call(self, call: ToolCall) -> str | None:
        """只执行注册表与参数校验，绝不调用 executor。"""
        spec = self._registry.get(call.tool_name)
        if spec is None:
            return "未注册的工具"
        try:
            return spec.validator(call)
        except Exception:  # noqa: BLE001 - validator 异常也必须在副作用前失败闭合。
            return "工具参数校验失败"

    # 列出注册表中的工具定义，随上游请求发送给模型，作为现有能力的单一来源。
    def list_definitions(self) -> list[dict[str, Any]]:
        """返回可直接放入 OpenAI 请求 tools 字段的工具定义列表。"""

        # 取出所有已注册的 OpenAI 工具 schema。
        return [spec.openai_tool for spec in self._registry.values()]

    # 把一次已提议 ToolCall 转换为 observation；永不向调用方抛出工具内部异常。
    def execute(self, call: ToolCall) -> ToolObservation:
        """先判注册表与 schema，再在必要时执行工具；未知/非法在工具执行前失败。"""

        # M3.1 兼容入口沿用原返回类型；可靠执行细节由新结果入口承载。
        return self.execute_with_policy(call, ToolExecutionContext("m3-1", Event())).observation

    def close(self, *, wait: bool = True) -> None:
        """关闭执行线程池；wait=True 时等待已启动的同步工具自然结束。"""

        with self._active_lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def execute_with_policy(
        self,
        call: ToolCall,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        """在统一策略下执行一次工具调用，并始终返回稳定结果而不是原始异常。"""

        started_at = time.monotonic()
        # 按名查找规格；未知工具不调用 validator 或 executor。
        spec = self._registry.get(call.tool_name)
        # 未知工具直接返回失败 observation，绝不宽松匹配相似名称。
        if spec is None:
            return self._result(call, context, None, 0, ToolErrorCode.unknown_tool, f"未注册的工具: {call.tool_name}", started_at)
        if spec.effect is ToolEffect.side_effect:
            # 副作用工具必须先由 graph 审批节点处理，runtime 本身不执行。
            return self._result(call, context, spec, 0, ToolErrorCode.permission_denied, "工具需要人工审批", started_at)
        # schema 校验在 executor 之前，保证非法参数不触发任何工具副作用。
        try:
            error_message = spec.validator(call)
        except Exception:  # noqa: BLE001 - validator 也是 runtime seam，异常必须稳定收敛。
            return self._result(call, context, spec, 0, ToolErrorCode.business_failure, "工具参数校验失败", started_at)
        # 校验失败返回稳定 invalid_arguments observation，不进入执行器。
        if error_message is not None:
            return self._result(call, context, spec, 0, ToolErrorCode.invalid_arguments, error_message, started_at)
        if context.cancellation_signal.is_set():
            return self._result(call, context, spec, 0, ToolErrorCode.cancelled, "工具执行已取消", started_at)

        key = (context.run_id, call.call_id)
        for attempt in range(1, spec.max_attempts + 1):
            if context.deadline_monotonic is not None and time.monotonic() >= context.deadline_monotonic:
                return self._result(call, context, spec, attempt - 1, ToolErrorCode.timeout, "工具执行未完成", started_at)
            with self._active_lock:
                if self._closed:
                    return self._result(call, context, spec, attempt - 1, ToolErrorCode.business_failure, "工具运行时已关闭", started_at)
                active = self._active.get(key)
                if active is not None and not active.done():
                    return self._result(call, context, spec, attempt - 1, ToolErrorCode.conflict, "同一工具调用仍在执行", started_at)
                future = self._executor.submit(spec.executor, call)
                self._active[key] = future
            outcome = self._wait_for_attempt(future, context, spec.timeout_seconds)
            if outcome == ToolErrorCode.timeout or outcome == ToolErrorCode.cancelled:
                future.add_done_callback(lambda done, active_key=key: self._clear_active(active_key, done))
                return self._result(call, context, spec, attempt, outcome, "工具执行未完成", started_at)
            self._clear_active(key, future)
            try:
                observation = future.result()
                self._validate_observation(call, observation)
                if observation.success:
                    return self._result(call, context, spec, attempt, None, None, started_at, observation)
                return self._result(call, context, spec, attempt, ToolErrorCode.business_failure, "工具执行失败", started_at, observation)
            except (TransientToolError, TimeoutToolError):
                if attempt < spec.max_attempts:
                    continue
                return self._result(call, context, spec, attempt, ToolErrorCode.transient_failure, "工具临时失败", started_at)
            except Exception:  # noqa: BLE001 - executor 是受控边界，原始异常不得泄漏。
                return self._result(call, context, spec, attempt, ToolErrorCode.business_failure, "工具执行失败", started_at)
        raise AssertionError("不可达：attempt 范围至少执行一次")

    def _clear_active(self, key: tuple[str, str], future: Future[ToolObservation]) -> None:
        """只清理仍指向同一个 future 的单飞记录。"""

        with self._active_lock:
            if self._active.get(key) is future:
                self._active.pop(key, None)

    def _wait_for_attempt(self, future: Future[ToolObservation], context: ToolExecutionContext, timeout_seconds: float) -> ToolErrorCode | None:
        """轮询 future，支持协作取消且不强杀正在运行的线程。"""

        deadline = time.monotonic() + timeout_seconds
        if context.deadline_monotonic is not None:
            deadline = min(deadline, context.deadline_monotonic)
        while not future.done():
            if context.cancellation_signal.is_set():
                return ToolErrorCode.cancelled
            if time.monotonic() >= deadline:
                return ToolErrorCode.timeout
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
        return None

    def _validate_observation(self, call: ToolCall, observation: ToolObservation) -> None:
        """保持 accepted call 与 observation 的身份和字段一致性。"""

        if not isinstance(observation, ToolObservation):
            raise TypeError("工具执行器返回值不是 ToolObservation")
        if observation.call_id != call.call_id or observation.tool_name != call.tool_name:
            raise ValueError("工具 observation 身份与调用不匹配")
        if observation.success and (observation.error_code is not None or observation.error_message is not None):
            raise ValueError("成功 observation 不得携带错误")
        if not observation.success and (
            observation.error_code is None
            or observation.error_message is None
            or observation.chunks
            or observation.authority_payload is not None
        ):
            # 失败时 chunks 与 authority_payload 都必须清空，防止半成功结果外泄。
            raise ValueError("失败 observation 字段不一致")
        if observation.success:
            if call.tool_name == "search_authority":
                # authority 成功必须携带窄 payload，且不能混入知识库 chunks。
                if observation.authority_payload is None or observation.chunks:
                    raise ValueError("authority 成功 observation 字段不一致")
            elif observation.authority_payload is not None:
                # authority payload 只能归属于 search_authority，其他工具不得携带。
                raise ValueError("非 authority 工具不得携带 authority_payload")

    def _result(self, call: ToolCall, context: ToolExecutionContext, spec: ToolSpec | None, attempt_count: int, error_code: ToolErrorCode | None, message: str | None, started_at: float, observation: ToolObservation | None = None) -> ToolExecutionResult:
        """由白名单字段生成结果与 trace，绝不记录参数、结果正文或异常文本。"""

        finished_at = time.monotonic()
        if observation is None:
            agent_code = AgentErrorCode.unknown_tool if error_code is ToolErrorCode.unknown_tool else AgentErrorCode.invalid_arguments if error_code is ToolErrorCode.invalid_arguments else AgentErrorCode.tool_execution_error
            observation = make_failure_observation(call, agent_code, message or "工具执行失败")
        trace = ToolAuditTrace(context.run_id, call.call_id, call.tool_name, "unknown" if spec is None else spec.tool_version, "succeeded" if observation.success else "failed", error_code, attempt_count, started_at, finished_at, (finished_at - started_at) * 1000)
        return ToolExecutionResult(observation, error_code, attempt_count, trace)
