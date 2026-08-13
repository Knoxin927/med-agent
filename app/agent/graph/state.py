"""定义 M3.3 可 JSON 编码的 AgentState 与纯状态转换。"""

# 导入 asdict，把 RankedChunk 快照转换为 JSON 基础类型。
from dataclasses import asdict, dataclass, replace
# 导入 Enum，用稳定字符串表示运行状态与外调类型。
from enum import Enum
# 导入 Any，承载受控 JSON 消息和工具参数。
from typing import Any, Literal

# 导入既有领域对象；状态层不依赖 LangGraph、HTTP、Chroma 或数据库客户端。
from app.agent.types import AgentErrorCode, ToolCall, ToolObservation

# 审批等待状态只允许三种值，未知值必须 fail-closed。
ApprovalWaitStatus = Literal["none", "pending", "reconciliation_required", "terminal"]


# 运行状态只允许这些明确值，终态不会再回到 running。
class AgentRunStatus(str, Enum):
    """描述一次 Agent run 的生命周期状态。"""

    # 正在等待模型、工具或内部 append 节点推进。
    running = "running"
    # 模型已经给出最终回答。
    completed = "completed"
    # 发生稳定领域错误后终止。
    failed = "failed"
    # 服务端取消后终止；M3.3 先定义状态，M3.6 才接 API。
    cancelled = "cancelled"


# 外调种类用于审计预记，不让节点通过自由字符串伪造成本。
class ActiveSegmentKind(str, Enum):
    """定义 M3.3 会消耗逻辑 step 的外调类别。"""

    # 调用 AgentModelClient.decide。
    model_decide = "model_decide"
    # 调用 ToolRuntime.execute_with_policy。
    tool_execute = "tool_execute"


# 保存一次已经预扣资源、但尚未完成外调的段。
@dataclass(frozen=True)
class ActiveSegment:
    """记录本次外调已经占用的 step 与最长等待预算。"""

    # 保存段类型，帮助后续 graph 节点确定如何完成或失败。
    kind: ActiveSegmentKind
    # 保存本段从 run 总预算中预留的最大毫秒数。
    reserved_ms: int


# 保存已生成但尚未写入 canonical conversation 的工具结果。
@dataclass(frozen=True)
class PendingToolOutcome:
    """保证工具结果先落入中间态，再由 append 节点生成唯一 observation。"""

    # 保留已验证的 observation，避免 append 时重新执行工具。
    observation: ToolObservation
    # 保存本次工具已经消耗的尝试次数，供未来审计/恢复使用。
    attempt_count: int
    # 标记 observation 是否来自人工审批终局，供最小控制投影精确区分恢复语义。
    origin: Literal["tool", "approval"] = "tool"

    @classmethod
    def from_observation(cls, observation: ToolObservation, *, attempt_count: int, origin: Literal["tool", "approval"] = "tool") -> "PendingToolOutcome":
        """把一次稳定工具结果封为可由 append 节点消费的中间态。"""

        # attempt_count 必须是至少一次的真实工具尝试，不能用零或 bool 冒充。
        if type(attempt_count) is not int or attempt_count < 1:
            raise ValueError("attempt_count 必须是正整数")
        if origin not in {"tool", "approval"}:
            raise ValueError("pending outcome 来源不合法")
        # 返回不可变对象，防止结果在 append 前被调用方替换。
        return cls(observation=observation, attempt_count=attempt_count, origin=origin)


# 用带稳定错误码的异常拒绝非法状态转换；graph 节点可统一映射为 fail 边。
class StateTransitionError(ValueError):
    """表示在外调之前已经发现的、可安全暴露的状态转换失败。"""

    # 保存稳定错误码，禁止调用方依赖异常文本判断分支。
    def __init__(self, code: AgentErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


# AgentState 是项目拥有的业务状态，不包含框架私有对象或外部客户端。
@dataclass(frozen=True)
class AgentState:
    """保存一次 in-process Agent run 的完整、可编码业务状态。"""

    # schema_version 让后续 M3.4 持久化能够拒绝未知状态形状。
    schema_version: int
    # run_id 由服务生成，模型无法覆盖。
    run_id: str
    # 原问题只用于初始用户消息和后续审计，不进入 trace。
    question: str
    # canonical 对话顺序只保存项目拥有的 JSON 消息。
    conversation: tuple[dict[str, Any], ...]
    # 已接受但尚未得到 append observation 的唯一工具调用。
    pending_call: ToolCall | None
    # 已得到工具结果但尚未写入 conversation 的唯一中间态。
    pending_tool_outcome: PendingToolOutcome | None
    # 当前生命周期状态。
    status: AgentRunStatus
    # 已预记的 model/tool 逻辑步骤数。
    step_count: int
    # 本次 run 不可突破的逻辑步骤上限。
    max_steps: int
    # 尚可预留的总等待预算，单位毫秒。
    active_budget_remaining_ms: int
    # 非空代表外调已经被允许开始，必须先完成或失败才能继续。
    active_segment: ActiveSegment | None
    # 终态答案只在 completed 时存在。
    final_answer: str | None
    # failed/cancelled 的稳定终态错误码；成功时为 None。
    terminal_error_code: AgentErrorCode | None
    # 记录可读的项目节点名，供 graph 路径测试而非框架私有快照使用。
    transitions: tuple[str, ...]
    # M3.5 审批等待标记；该标记只控制 graph fail-closed，不保存原始参数。
    approval_status: ApprovalWaitStatus = "none"

    def with_pending_tool_outcome(self, outcome: PendingToolOutcome) -> "AgentState":
        """保存工具结果中间态，拒绝错配 call 或重复结果。"""

        # 结果只能属于已经接受的 pending call，避免把迟到结果写入另一调用。
        if self.pending_call is None:
            raise StateTransitionError(AgentErrorCode.internal_error, "没有 pending_call，不能保存工具结果")
        if self.pending_tool_outcome is not None:
            raise StateTransitionError(AgentErrorCode.internal_error, "已有 pending_tool_outcome")
        observation = outcome.observation
        if observation.call_id != self.pending_call.call_id or observation.tool_name != self.pending_call.tool_name:
            raise StateTransitionError(AgentErrorCode.internal_error, "工具结果与 pending_call 不匹配")
        # 结果只能在 running 状态保存，终态拒绝迟到结果覆盖。
        _require_running(self)
        return replace(self, pending_tool_outcome=outcome, transitions=self.transitions + ("pending_tool_outcome",))

    def to_dict(self) -> dict[str, Any]:
        """返回仅由 JSON 基础类型组成的状态快照，供未来 store/checkpointer 使用。"""

        # 显式编码，避免 dataclass/Enum/领域对象被 JSON 库隐式处理为私有形状。
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "question": self.question,
            "conversation": list(self.conversation),
            "pending_call": None if self.pending_call is None else _encode_tool_call(self.pending_call),
            "pending_tool_outcome": None if self.pending_tool_outcome is None else _encode_pending_outcome(self.pending_tool_outcome),
            "status": self.status.value,
            "step_count": self.step_count,
            "max_steps": self.max_steps,
            "active_budget_remaining_ms": self.active_budget_remaining_ms,
            "active_segment": None if self.active_segment is None else {"kind": self.active_segment.kind.value, "reserved_ms": self.active_segment.reserved_ms},
            "final_answer": self.final_answer,
            "terminal_error_code": None if self.terminal_error_code is None else self.terminal_error_code.value,
            "transitions": list(self.transitions),
            "approval_status": self.approval_status,
        }


# 创建新 run 的唯一入口，固定默认 cap 并写入 canonical user 消息。
def create_agent_state(run_id: str, question: str, *, max_steps: int = 8, active_budget_ms: int = 120_000) -> AgentState:
    """创建新 AgentState；所有输入在进入 graph 前严格校验。"""

    # run_id 与 question 为空会破坏状态定位或模型决策，必须在外调前拒绝。
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id 必须是非空字符串")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question 必须是非空字符串")
    # bool 是 int 子类，必须显式排除，且客户端只能在 1..8 内设定上限。
    if type(max_steps) is not int or max_steps < 1 or max_steps > 8:
        raise ValueError("max_steps 必须是 1..8 的整数")
    if type(active_budget_ms) is not int or active_budget_ms <= 0:
        raise ValueError("active_budget_ms 必须是正整数")
    # 初始 conversation 只有项目拥有的 user 消息，不保存供应商原始对象。
    return AgentState(
        schema_version=1,
        run_id=run_id,
        question=question,
        conversation=({"kind": "user", "content": question},),
        pending_call=None,
        pending_tool_outcome=None,
        status=AgentRunStatus.running,
        step_count=0,
        max_steps=max_steps,
        active_budget_remaining_ms=active_budget_ms,
        active_segment=None,
        final_answer=None,
        terminal_error_code=None,
        transitions=("start",),
    )


# 在任何 model/tool 外调前预记固定 cost=1 与最长等待预算。
def reserve_external_step(state: AgentState, kind: ActiveSegmentKind, reservation_ms: int) -> AgentState:
    """预扣一次外调资源；失败时不返回可执行状态。"""

    # 终态、已有外调或非法预留均不能继续，避免并发或迟到结果覆盖。
    _require_running(state)
    if state.active_segment is not None:
        raise StateTransitionError(AgentErrorCode.internal_error, "已有 active_segment")
    if type(reservation_ms) is not int or reservation_ms <= 0:
        raise ValueError("reservation_ms 必须是正整数")
    if state.step_count + 1 > state.max_steps:
        raise StateTransitionError(AgentErrorCode.step_limit_exceeded, "超过 max_steps")
    if reservation_ms > state.active_budget_remaining_ms:
        raise StateTransitionError(AgentErrorCode.active_budget_exceeded, "active budget 不足")
    # 先扣 step 和预算再允许调用方发起外调，这是 M3.3 的核心 fail-closed 保证。
    return replace(
        state,
        step_count=state.step_count + 1,
        active_budget_remaining_ms=state.active_budget_remaining_ms - reservation_ms,
        active_segment=ActiveSegment(kind=kind, reserved_ms=reservation_ms),
        transitions=state.transitions + (f"reserve:{kind.value}",),
    )


# 在外调返回后关闭 active segment，并只退还未实际消耗的时间。
def complete_active_segment(state: AgentState, *, elapsed_ms: int) -> AgentState:
    """按可证明的已用时间结算预留预算，不能让预算超过初始可用范围。"""

    # 没有预留段却试图结算代表节点顺序错误，必须 fail-closed。
    if state.active_segment is None:
        raise StateTransitionError(AgentErrorCode.internal_error, "没有 active_segment 可以完成")
    if type(elapsed_ms) is not int or elapsed_ms < 0:
        raise ValueError("elapsed_ms 必须是非负整数")
    # 仅退还 reservation 减已用时间的正值；超时不会凭空恢复预算。
    refund_ms = max(0, state.active_segment.reserved_ms - elapsed_ms)
    return replace(
        state,
        active_budget_remaining_ms=state.active_budget_remaining_ms + refund_ms,
        active_segment=None,
        transitions=state.transitions + ("complete_active_segment",),
    )


# 保存一次已通过模型协议的工具提议，并写入 canonical assistant tool call 消息。
def set_pending_call(state: AgentState, call: ToolCall) -> AgentState:
    """登记唯一 accepted tool call；schema/白名单仍由 ToolRuntime 负责。"""

    # 不允许在已有 call/outcome 或终态上覆盖，保证每个 call 只有一个 append owner。
    _require_running(state)
    if state.pending_call is not None or state.pending_tool_outcome is not None:
        raise StateTransitionError(AgentErrorCode.internal_error, "已有 pending tool 状态")
    # 把已解析的项目 ToolCall 编码为 JSON，不保存供应商原始 function payload。
    message = {"kind": "assistant_tool_call", **_encode_tool_call(call)}
    return replace(
        state,
        pending_call=call,
        conversation=state.conversation + (message,),
        transitions=state.transitions + ("set_pending_call",),
    )


# 唯一 append owner：消费 pending outcome，将其转换为 canonical tool observation。
def append_pending_tool_outcome(state: AgentState) -> AgentState:
    """幂等地 append 已保存的工具结果，绝不重新执行工具。"""

    # 已经 append 完的重放是 no-op，支持未来 checkpoint 恢复而不制造第二条 observation。
    if state.pending_call is None and state.pending_tool_outcome is None:
        return state
    if state.pending_call is None or state.pending_tool_outcome is None:
        raise StateTransitionError(AgentErrorCode.internal_error, "pending call 与 outcome 必须同时存在")
    outcome = state.pending_tool_outcome
    # 状态保存时已校验身份，这里再次核验以防未来 codec/restore 绕过入口。
    if outcome.observation.call_id != state.pending_call.call_id:
        raise StateTransitionError(AgentErrorCode.internal_error, "append 的 call_id 不匹配")
    message = _encode_observation_message(outcome)
    return replace(
        state,
        pending_call=None,
        pending_tool_outcome=None,
        conversation=state.conversation + (message,),
        transitions=state.transitions + ("append_tool_result" if outcome.observation.success else "append_tool_error",),
    )


# 把 run 转为稳定失败终态，后续 reserve 会被 _require_running 拒绝。
def fail_run(state: AgentState, code: AgentErrorCode) -> AgentState:
    """以稳定错误码结束 run，禁止原始异常进入业务状态。"""

    # 已终态不能被二次覆盖，防止迟到结果改变已经公开的失败结论。
    _require_running(state)
    return replace(
        state,
        status=AgentRunStatus.failed,
        active_segment=None,
        terminal_error_code=code,
        transitions=state.transitions + ("fail",),
    )


# 把 run 转为取消终态；迟到结果不能覆盖该状态。
def cancel_run(state: AgentState) -> AgentState:
    """以 cancelled 结束 run，供 M3.6 cancel API 幂等写入。"""

    # 已终态不能被二次覆盖，重复 cancel 由上层读取当前记录处理。
    _require_running(state)
    return replace(
        state,
        status=AgentRunStatus.cancelled,
        active_segment=None,
        # 取消后不再保留“仍在等审批”的中间标记，避免终态被误读。
        pending_call=None,
        pending_tool_outcome=None,
        approval_status="terminal",
        terminal_error_code=AgentErrorCode.cancelled,
        transitions=state.transitions + ("cancel",),
    )


# 把 run 转为成功终态；最终回答只能由 graph 的 emit_final 节点写入。
def complete_run(state: AgentState, answer: str) -> AgentState:
    """保存模型给出的最终回答并结束本次运行。"""

    # 只有 running 状态且非空回答能进入成功终态。
    _require_running(state)
    if not isinstance(answer, str) or not answer.strip():
        raise StateTransitionError(AgentErrorCode.model_protocol_error, "最终回答必须是非空字符串")
    return replace(state, status=AgentRunStatus.completed, final_answer=answer, transitions=state.transitions + ("emit_final",))


# 统一检查可继续执行的前提，终态或取消状态不允许再预记外调。
def _require_running(state: AgentState) -> None:
    """确保状态仍可推进；否则返回稳定内部转换错误。"""

    if state.status is not AgentRunStatus.running:
        raise StateTransitionError(AgentErrorCode.internal_error, "run 已终态，不能继续推进")


# 把项目 ToolCall 转为 JSON 基础类型，避免 dataclass 直接泄漏进状态快照。
def _encode_tool_call(call: ToolCall) -> dict[str, Any]:
    """编码已解析工具调用的稳定字段。"""

    return {"call_id": call.call_id, "tool_name": call.tool_name, "arguments": dict(call.arguments)}


# 编码 pending outcome，完整 chunk 快照只在非敏感本地知识库范围内保留。
def _encode_pending_outcome(outcome: PendingToolOutcome) -> dict[str, Any]:
    """把中间工具结果编码为 JSON，供未来 store/checkpointer 复用。"""

    return {"attempt_count": outcome.attempt_count, "origin": outcome.origin, "observation": _encode_observation(outcome.observation)}


# 统一编码 observation，成功和失败都只保存项目稳定字段。
def _encode_observation(observation: ToolObservation) -> dict[str, Any]:
    """编码 ToolObservation，避免原始异常或 runtime 私有对象进入状态。"""

    # MCP-only authority payload 不得进入 graph checkpoint 或 conversation 快照。
    if observation.authority_payload is not None:
        raise ValueError("authority_payload 不得进入 Agent graph 编码")
    return {
        "call_id": observation.call_id,
        "tool_name": observation.tool_name,
        "success": observation.success,
        "error_code": None if observation.error_code is None else observation.error_code.value,
        "error_message": observation.error_message,
        "chunks": [asdict(chunk) for chunk in observation.chunks],
    }


# 将 pending outcome 转为 canonical conversation 内的 tool observation 消息。
def _encode_observation_message(outcome: PendingToolOutcome) -> dict[str, Any]:
    """构造唯一 observation 消息，保留尝试次数供后续审计。"""

    return {"kind": "tool_observation", "attempt_count": outcome.attempt_count, **_encode_observation(outcome.observation)}
