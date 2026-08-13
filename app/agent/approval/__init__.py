"""M3.5 人工审批与执行 claim 的框架无关领域层。"""

# 公开审批领域对象和确定性内存 repository；PostgreSQL adapter 后续复用同一 port。
from app.agent.approval.memory import InMemoryApprovalRepository
from app.agent.approval.postgres import PostgresApprovalRepository
from app.agent.approval.reconciliation import ReconciliationResult, resolve_reconciliation
from app.agent.approval.port import (
    ApprovalConflict, ApprovalDecision, ApprovalRecord, ApprovalRepository,
    ApprovalRequest, ApprovalStatus, ClaimConflict, ExecutionClaim,
    LocalFollowUpRequest, LocalFollowUpRepository, PendingOutcome,
    ReconciliationContext, ReconciliationRequired,
)

__all__ = [
    "ApprovalConflict", "ApprovalDecision", "ApprovalRecord", "ApprovalRepository",
    "ApprovalRequest", "ApprovalStatus", "ClaimConflict", "ExecutionClaim",
    "LocalFollowUpRequest", "LocalFollowUpRepository", "PendingOutcome",
    "ReconciliationContext", "ReconciliationRequired",
    "InMemoryApprovalRepository",
    "PostgresApprovalRepository",
    "ReconciliationResult", "resolve_reconciliation",
]
