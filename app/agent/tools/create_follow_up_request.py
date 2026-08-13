"""M3.5 本地 create_follow_up_request 的审批与幂等执行入口。"""

# 导入审批 port，工具只依赖项目拥有的稳定契约。
from app.agent.approval.port import (
    ApprovalDecision, ApprovalRecord, ApprovalRepository, ApprovalRequest,
    LocalFollowUpRepository, PendingOutcome,
)
from app.agent.approval.reconciliation import resolve_reconciliation
from app.agent.approval.port import ReconciliationContext


class CreateFollowUpRequestService:
    """串联审批、claim、本地唯一写入和 pending outcome。"""

    def __init__(self, approvals: ApprovalRepository, follow_ups: LocalFollowUpRepository) -> None:
        # 两个 port 可分别替换为内存或 PostgreSQL 实现，graph 不直接碰 SQL。
        self._approvals = approvals
        self._follow_ups = follow_ups

    def request(self, request: ApprovalRequest) -> ApprovalRecord:
        """先冻结摘要/effect/hash/TTL，再返回 pending，不执行任何副作用。"""
        return self._approvals.create_pending(request)

    def load_approval(self, run_id: str, call_id: str) -> ApprovalRecord:
        """为 graph 恢复重复 pending 请求提供只读 durable 访问。"""
        return self._approvals.load(run_id, call_id)

    def acknowledge_pending_outcome(self, run_id: str, call_id: str) -> None:
        """graph append 成功后确认消费 durable 中间事实。"""
        self._approvals.clear_pending_outcome(run_id, call_id)

    def load_pending_outcome(self, run_id: str, call_id: str) -> PendingOutcome | None:
        """为 graph resume 暴露只读 durable outcome 查询。"""
        return self._approvals.load_pending_outcome(run_id, call_id)

    def decide(self, run_id: str, call_id: str, expected_version: int, decision: ApprovalDecision, owner: str, now: float, worker: str = "local-worker", lease_seconds: int = 30) -> PendingOutcome:
        """只根据 durable 审批记录执行，调用方不能替换已冻结字段。"""
        record = self._approvals.decide(run_id, call_id, expected_version, decision, owner, now)
        if record.status in {"rejected", "expired"}:
            outcome = PendingOutcome(run_id, call_id, record.status, f"{run_id}:{call_id}")
            self._approvals.store_pending_outcome(outcome)
            return outcome
        if record.status == "cancelled":
            outcome = PendingOutcome(run_id, call_id, "cancelled", f"{run_id}:{call_id}")
            self._approvals.store_pending_outcome(outcome)
            return outcome
        claim = self._approvals.claim_execution(run_id, call_id, worker, lease_seconds, now)
        try:
            self._follow_ups.create_once(run_id, call_id, now, record.arguments_hash, record.effect)
            outcome = PendingOutcome(run_id, call_id, "succeeded", f"{run_id}:{call_id}")
            self._approvals.release_or_finish(claim, outcome, now)
            return outcome
        except Exception:
            # 未知结果只能交给 reconciliation，不能静默 retry 或继续模型。
            return PendingOutcome(run_id, call_id, "reconciliation_required", f"{run_id}:{call_id}")

    def reconcile(self, run_id: str, call_id: str, now: float, attempt_count: int, max_attempts: int, worker: str = "local-worker", lease_seconds: int = 30) -> PendingOutcome:
        """对未知本地写入先查证；只有缺失且预算允许才重新 claim。"""
        key = f"{run_id}:{call_id}"
        try:
            record = self._approvals.load(run_id, call_id)
            durable_outcome = self._approvals.load_pending_outcome(run_id, call_id)
            existing = self._follow_ups.lookup_by_idempotency_key(run_id, call_id)
        except Exception:
            # 数据库不可达时保持 non-terminal，调用方不得继续模型。
            return PendingOutcome(run_id, call_id, "reconciliation_required", key)
        if durable_outcome is not None:
            # create_once 已完成而 append 尚未完成时，优先恢复原终局。
            return durable_outcome
        if record.status != "approved":
            outcome = PendingOutcome(run_id, call_id, "failed", key)
            self._approvals.store_pending_outcome(outcome)
            return outcome
        status = "missing"
        if existing is not None:
            status = "matching" if (existing.arguments_hash == record.arguments_hash and existing.effect == record.effect) else "mismatch"
        # attempt_count 仅保留旧调用签名；可否重试只能由 durable 计数决定。
        durable_attempt_count = self._approvals.next_reconciliation_attempt(run_id, call_id)
        decision = resolve_reconciliation(ReconciliationContext(run_id, call_id, key, status), durable_attempt_count, max_attempts).decision
        if decision == "succeeded":
            outcome = PendingOutcome(run_id, call_id, "succeeded", key)
            # 查证 matching 是终局，必须先持久化再交给 graph append。
            self._approvals.store_pending_outcome(outcome)
            return outcome
        if decision == "failed":
            outcome = PendingOutcome(run_id, call_id, "failed", key)
            self._approvals.store_pending_outcome(outcome)
            return outcome
        # missing 只在明确有剩余预算时再次 claim；参数不会来自 resume 请求。
        try:
            claim = self._approvals.claim_execution(run_id, call_id, worker, lease_seconds, now)
            self._follow_ups.create_once(run_id, call_id, now, record.arguments_hash, record.effect)
            outcome = PendingOutcome(run_id, call_id, "succeeded", key)
            self._approvals.release_or_finish(claim, outcome, now)
            return outcome
        except Exception:
            return PendingOutcome(run_id, call_id, "reconciliation_required", key)
