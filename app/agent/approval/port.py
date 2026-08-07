"""定义 M3.5 审批、TTL 和 execution claim 的稳定领域契约。"""

# 导入 dataclass、Literal 和 Protocol，声明不可变记录与窄 repository port。
from dataclasses import dataclass
from typing import Literal, Protocol


# 审批状态是持久化事实，不允许自由字符串扩展。
ApprovalStatus = Literal["pending", "approved", "rejected", "cancelled", "expired"]
# owner 可作出的三种终态决策。
ApprovalDecision = Literal["approve", "reject", "cancel"]


class ApprovalConflict(RuntimeError):
    """版本冲突、重复审批冲突或已过期审批。"""


class ClaimConflict(RuntimeError):
    """执行租约仍由其他 worker 持有。"""


class ReconciliationRequired(RuntimeError):
    """副作用结果未知时，禁止直接继续执行模型。"""


# 审批请求只保存脱敏摘要和 hash；原始参数不进入 repository 记录。
@dataclass(frozen=True)
class ApprovalRequest:
    """创建一条待 owner 决策的固定审批请求。"""

    run_id: str
    call_id: str
    summary: str
    effect: str
    arguments_hash: str
    expires_at: float


# durable 审批记录的最小字段；version 用于 owner 决策 CAS。
@dataclass(frozen=True)
class ApprovalRecord:
    """保存审批状态，不包含完整会话或原始 arguments。"""

    run_id: str
    call_id: str
    version: int
    status: ApprovalStatus
    summary: str
    effect: str
    arguments_hash: str
    expires_at: float
    decided_by: str | None = None
    decided_at: float | None = None


# execution claim 是短租约；owner 过期前不能被其他 worker 抢占。
@dataclass(frozen=True)
class ExecutionClaim:
    """保存获批调用的 worker 租约。"""

    run_id: str
    call_id: str
    owner: str
    lease_expires_at: float
    claim_version: int


@dataclass(frozen=True)
class PendingOutcome:
    """只保存可安全追加的终局状态，不携带原始工具参数。"""

    run_id: str
    call_id: str
    status: Literal["succeeded", "rejected", "expired", "cancelled", "failed", "reconciliation_required"]
    idempotency_key: str
    tool_name: str = "create_follow_up_request"
    attempt_count: int = 1
    consumed: bool = False


@dataclass(frozen=True)
class LocalFollowUpRequest:
    """本地随访请求的最小幂等事实。"""

    run_id: str
    call_id: str
    created_at: float
    arguments_hash: str
    effect: str


@dataclass(frozen=True)
class ReconciliationContext:
    """描述未知结果查证所需的稳定分支。"""

    run_id: str
    call_id: str
    idempotency_key: str
    status: Literal["matching", "missing", "mismatch", "database_unavailable"]


# repository 是项目事实源，数据库 adapter 必须实现这些同名操作。
class ApprovalRepository(Protocol):
    """定义审批决策、claim 和唯一执行事实的最小 port。"""

    def create_pending(self, request: ApprovalRequest) -> ApprovalRecord: ...

    def load(self, run_id: str, call_id: str) -> ApprovalRecord: ...
    def decide(self, run_id: str, call_id: str, expected_version: int, decision: ApprovalDecision, owner: str, now: float) -> ApprovalRecord: ...
    def claim_execution(self, run_id: str, call_id: str, owner: str, lease_seconds: int, now: float) -> ExecutionClaim: ...

    def release_or_finish(self, claim: ExecutionClaim, outcome: PendingOutcome, now: float) -> None: ...

    def load_pending_outcome(self, run_id: str, call_id: str) -> PendingOutcome | None: ...

    def clear_pending_outcome(self, run_id: str, call_id: str) -> None: ...

    def store_pending_outcome(self, outcome: PendingOutcome) -> None: ...

    def next_reconciliation_attempt(self, run_id: str, call_id: str) -> int: ...


class LocalFollowUpRepository(Protocol):
    """定义本地 follow-up 唯一写入与查证 port。"""

    def create_once(self, run_id: str, call_id: str, now: float, arguments_hash: str, effect: str) -> LocalFollowUpRequest: ...

    def lookup_by_idempotency_key(self, run_id: str, call_id: str) -> LocalFollowUpRequest | None: ...
