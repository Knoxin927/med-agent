"""定义 M3.1 Agent 框架无关的领域值对象与稳定错误码。"""

# 导入 Enum，用稳定的字符串枚举表达可序列化的错误分类。
from enum import Enum
# 导入 dataclass，定义不可变、类型明确的值对象。
from dataclasses import dataclass
# 导入 Any，承载模型提议的 JSON 参数。
from typing import Any, Protocol

# 导入 M2 统一的有序检索结果，作为成功 observation 的本轮快照类型。
from app.retrieval_strategies.types import RankedChunk


# 用字符串枚举保证错误码可安全 JSON 序列化，不被当成实现私有常量。
class AgentErrorCode(str, Enum):
    """保存 Agent 领域所有稳定、脱敏的错误分类。"""

    # 模型违反单 call 协议：多 call、正文与 call 混合、损坏 JSON、空 call id 或第二轮仍提工具。
    model_protocol_error = "model_protocol_error"
    # ToolRuntime 在执行前发现注册表里没有模型提议的工具名。
    unknown_tool = "unknown_tool"
    # ToolRuntime 在调用工具前发现参数违反 schema（空 query、越界 top_k、额外字段）。
    invalid_arguments = "invalid_arguments"
    # 工具执行中发生异常（如 dense 检索失败），M3.1 仅转换为稳定失败 observation，不自动重试。
    tool_execution_error = "tool_execution_error"
    # M3.3 在外调前发现下一次逻辑步骤会超过 max_steps。
    step_limit_exceeded = "step_limit_exceeded"
    # M3.3 在外调前无法从 active budget 中预留正数等待时间。
    active_budget_exceeded = "active_budget_exceeded"
    # M3.3 收到协作取消后使用的稳定终态错误码。
    cancelled = "cancelled"
    # M3.3 仅在无法归入已知稳定分类时使用的脱敏内部错误码。
    internal_error = "internal_error"
    # M3.4 严格不落敏感恢复上下文时，中断 run 只能重新发起。
    resume_requires_restart = "resume_requires_restart"
    # M3.5 审批尚未完成；该状态不允许模型继续调用副作用工具。
    approval_required = "approval_required"
    # M3.5 owner 决策与 durable 版本或 TTL 不一致时使用。
    approval_conflict = "approval_conflict"
    # M3.5 审批 TTL 到期后拒绝执行，不能回退为普通工具错误。
    approval_expired = "approval_expired"
    # M3.5 副作用结果未知且数据库暂不可查证时保持非终态等待。
    reconciliation_required = "reconciliation_required"


# M3.2 给工具执行结果定义更细粒度的稳定分类；与面向模型的 AgentErrorCode 不同，
# ToolErrorCode 只在 ToolExecutionResult 与脱敏 audit trace 内部使用，供执行策略层
# 判断是否重试，不会回填到给模型看的 observation，也不暴露给终端。
class ToolErrorCode(str, Enum):
    """保存工具执行期间的稳定、脱敏错误分类，供 retry 策略和审计使用。"""

    # 等待 dense 线程超过 ToolExecutionPolicy.timeout_seconds；仅停止等待，不强杀工作线程。
    timeout = "timeout"
    # 工具执行抛出非超时可恢复异常；在 max_attempts 允许时可重试。
    transient_failure = "transient_failure"
    # 注册表内没有模型提议的工具名；在执行前失败，不可重试。
    unknown_tool = "unknown_tool"
    # 工具参数违反 schema；在执行前失败，不可重试。
    invalid_arguments = "invalid_arguments"
    # 调用方缺少必要权限；由服务端 scope 拒绝，不可重试（M3.2 未使用，保留契约）。
    permission_denied = "permission_denied"
    # 工具业务规则拒绝（例如检索返回不可用配置）；不可重试。
    business_failure = "business_failure"
    # 写入冲突（例如幂等键已存在）；不可重试（M3.2 未使用，保留契约）。
    conflict = "conflict"
    # 服务端协作取消信号被置位；停止等待并丢弃迟到结果，不可重试。
    cancelled = "cancelled"


# 描述工具对外产生的效果类型；M3.2 只注册 read-only 工具，保留副作用类型供后续 feature。
class ToolEffect(str, Enum):
    """工具的副作用分类，决定是否需要审批与重试策略。"""

    # 只读检索，不产生任何持久写入或外部副作用（search_knowledge 属于此类）。
    read_only = "read_only"
    # 产生需要人工审批的本地副作用；M3.2 不注册此类工具，仅保留契约供 M3.5。
    side_effect = "side_effect"


# 声明工具执行前是否需要人工审批；M3.2 的 search_knowledge 用 none。
class ApprovalPolicy(str, Enum):
    """工具的审批策略。"""

    # 无需审批，read-only 工具默认值。
    none = "none"
    # 执行前必须取得人工批准；M3.2 不实现，保留契约供 M3.5。
    required = "required"


class CancellationSignal(Protocol):
    """描述运行时只需要读取的协作式取消信号。"""

    def is_set(self) -> bool:
        """取消已请求时返回 True。"""


@dataclass(frozen=True)
class ToolExecutionContext:
    """保存一次工具调用的服务端执行上下文。"""

    run_id: str
    cancellation_signal: CancellationSignal
    verified_scopes: frozenset[str] = frozenset()
    deadline_monotonic: float | None = None


@dataclass(frozen=True)
class ToolAuditTrace:
    """保存可复核但不含业务输入或原始异常的工具执行审计记录。"""

    run_id: str
    call_id: str
    tool_name: str
    tool_version: str
    status: str
    error_code: ToolErrorCode | None
    attempt_count: int
    started_at_monotonic: float
    finished_at_monotonic: float
    latency_ms: float


# frozen=True 防止模型提议在传递过程中被意外篡改。
@dataclass(frozen=True)
class ToolCall:
    """保存模型提议的一次工具调用；它只是提议，不是执行授权。"""

    # 保存模型为本次调用生成的稳定身份，用于把 observation 配对回同一次 call。
    call_id: str
    # 保存模型提议的工具名；是否注册由 ToolRuntime 判定，不在本对象校验。
    tool_name: str
    # 保存已经解析过的 JSON 参数字典；供应商原始字符串不进入本对象。
    arguments: dict[str, Any]


# frozen=True 保证最终回答在回填和聚合阶段不可变。
@dataclass(frozen=True)
class FinalAnswerDecision:
    """保存模型直接给出的完整最终回答。"""

    # 保存模型本轮生成的完整回答文本。
    answer: str


# frozen=True 防止已提议的 tool call 在校验前被修改。
@dataclass(frozen=True)
class ToolCallDecision:
    """保存模型本轮选择进入工具调用而不是直接回答的决策。"""

    # 保存模型提议的唯一 tool call；是否被接受由 ToolRuntime 决定。
    tool_call: ToolCall


# 用类型别名表达“每轮恰好一个决策”：要么直接最终回答，要么一个 tool call。
AgentDecision = FinalAnswerDecision | ToolCallDecision


# frozen=True 让 observation 作为可安全回填和用于聚合 sources 的快照。
@dataclass(frozen=True)
class ToolObservation:
    """保存一个已接受调用的唯一、最终、可回填给模型的成功或失败结果。"""

    # 保存与被接受 ToolCall 的 call_id 相同的身份，保证回填时不配对错误。
    call_id: str
    # 保存产出该 observation 的工具名，便于溯源。
    tool_name: str
    # 成功为 True；失败 observation 仍要回填给模型，但不会贡献 sources。
    success: bool
    # 失败时携带稳定错误码；成功时为 None。
    error_code: AgentErrorCode | None
    # 失败时携带脱敏说明；成功时为 None。
    error_message: str | None
    # 成功检索时保存本轮 RankedChunk 快照，供模型上下文和最终 sources 同时使用。
    chunks: list[RankedChunk]
    # M4.3 权威检索专用窄 payload；默认 None，保持旧六参数位置构造兼容。
    # Agent graph/message 遇到非 None 必须 fail-closed，不得持久化或回填模型。
    authority_payload: Any | None = None

    # 暴露只读谓词，避免调用方用 success 字段猜测是否携带快照。
    @property
    def has_chunks(self) -> bool:
        # 仅成功且快照非空时才代表有可引用来源。
        return self.success and bool(self.chunks)


# frozen=True 保证循环返回的完整结果在交给未来 SSE 编码前不可变。
@dataclass(frozen=True)
class AgentLoopResult:
    """保存有界循环返回的完整结果对象；M3.1 尚不发布 Agent SSE。"""

    # 保存模型第二次决策给出的完整最终回答。
    answer: str
    # 保存只从成功 search_knowledge observation 派生的来源快照；无成功检索时为空列表。
    sources: list[RankedChunk]
