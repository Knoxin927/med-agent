"""M3.5 审批 repository 的确定性内存实现，供状态矩阵测试使用。"""

# 导入锁，确保决策和 claim 的检查/写入是一个原子临界区。
from threading import Lock
from app.agent.approval.port import (
    ApprovalConflict, ApprovalDecision, ApprovalRecord, ApprovalRequest,
    ApprovalStatus, ClaimConflict, ExecutionClaim, LocalFollowUpRequest,
    PendingOutcome,
)


class InMemoryApprovalRepository:
    """不执行副作用，只验证 TTL、CAS、幂等决策和租约竞争。"""

    def __init__(self) -> None:
        # key 使用 run_id+call_id，与后续 PostgreSQL 唯一约束保持一致。
        self._records: dict[tuple[str, str], ApprovalRecord] = {}
        self._claims: dict[tuple[str, str], ExecutionClaim] = {}
        # pending outcome 是 append 前可恢复的 durable 中间事实。
        self._pending_outcomes: dict[tuple[str, str], PendingOutcome] = {}
        self._reconciliation_attempts: dict[tuple[str, str], int] = {}
        # follow-up 只保存唯一幂等事实，不保存业务参数。
        self._follow_ups: dict[tuple[str, str], LocalFollowUpRequest] = {}
        self._lock = Lock()

    def create_pending(self, request: ApprovalRequest) -> ApprovalRecord:
        # 摘要为空、TTL 非未来或 hash 为空都不能进入审批队列。
        if not request.summary.strip() or not request.arguments_hash.strip() or request.expires_at <= 0:
            raise ValueError("审批请求字段不合法")
        key = (request.run_id, request.call_id)
        with self._lock:
            if key in self._records:
                raise ApprovalConflict("审批请求已存在")
            record = ApprovalRecord(request.run_id, request.call_id, 0, "pending", request.summary, request.effect, request.arguments_hash, request.expires_at)
            self._records[key] = record
            return record

    def decide(self, run_id: str, call_id: str, expected_version: int, decision: ApprovalDecision, owner: str, now: float) -> ApprovalRecord:
        key = (run_id, call_id)
        with self._lock:
            current = self._records.get(key)
            if current is None:
                raise ApprovalConflict("审批请求不存在")
            if current.status == "pending" and now >= current.expires_at:
                expired = ApprovalRecord(**{**current.__dict__, "version": current.version + 1, "status": "expired", "decided_at": now})
                self._records[key] = expired
                current = expired
            # 过期是持久事实；重复恢复只返回同一记录，不制造新的冲突版本。
            if current.status == "expired":
                return current
            if current.status != "pending":
                same = (decision == "approve" and current.status == "approved") or (decision == "reject" and current.status == "rejected") or (decision == "cancel" and current.status == "cancelled")
                if same:
                    return current
                raise ApprovalConflict("审批已进入其他状态")
            if current.version != expected_version:
                raise ApprovalConflict("审批版本冲突")
            status: ApprovalStatus = {"approve": "approved", "reject": "rejected", "cancel": "cancelled"}[decision]
            updated = ApprovalRecord(**{**current.__dict__, "version": current.version + 1, "status": status, "decided_by": owner, "decided_at": now})
            self._records[key] = updated
            return updated

    def load(self, run_id: str, call_id: str) -> ApprovalRecord:
        """读取冻结审批记录，恢复时不接受客户端替换摘要或 hash。"""
        with self._lock:
            record = self._records.get((run_id, call_id))
            if record is None:
                raise ApprovalConflict("审批请求不存在")
            return record

    def claim_execution(self, run_id: str, call_id: str, owner: str, lease_seconds: int, now: float) -> ExecutionClaim:
        if lease_seconds <= 0 or not owner.strip():
            raise ValueError("claim 租约参数不合法")
        key = (run_id, call_id)
        with self._lock:
            record = self._records.get(key)
            if record is None or record.status != "approved":
                raise ClaimConflict("只有 approved 请求可以 claim")
            current = self._claims.get(key)
            if current is not None and current.lease_expires_at > now:
                raise ClaimConflict("已有未过期 execution claim")
            claim = ExecutionClaim(run_id, call_id, owner, now + lease_seconds, 0 if current is None else current.claim_version + 1)
            self._claims[key] = claim
            return claim

    def release_or_finish(self, claim: ExecutionClaim, outcome: PendingOutcome, now: float) -> None:
        """只允许当前 claim owner 完成，迟到 owner 的写入必须拒绝。"""
        if (outcome.run_id, outcome.call_id) != (claim.run_id, claim.call_id) or outcome.idempotency_key != f"{claim.run_id}:{claim.call_id}":
            raise ClaimConflict("claim 与 outcome 身份不一致")
        if outcome.attempt_count < 1:
            raise ValueError("outcome attempt_count 必须为正整数")
        key = (claim.run_id, claim.call_id)
        with self._lock:
            current = self._claims.get(key)
            if current != claim or now >= claim.lease_expires_at:
                raise ClaimConflict("旧 execution claim 不能写入终局")
            self._claims.pop(key)
            self._pending_outcomes[key] = outcome

    def load_pending_outcome(self, run_id: str, call_id: str) -> PendingOutcome | None:
        """读取 durable pending outcome，支持 append 前崩溃恢复。"""
        with self._lock:
            return self._pending_outcomes.get((run_id, call_id))

    def store_pending_outcome(self, outcome: PendingOutcome) -> None:
        """持久化无 execution claim 的拒绝/取消/过期终局。"""
        if outcome.attempt_count < 1:
            raise ValueError("outcome attempt_count 必须为正整数")
        with self._lock:
            self._pending_outcomes[(outcome.run_id, outcome.call_id)] = outcome

    def clear_pending_outcome(self, run_id: str, call_id: str) -> None:
        """append 成功后清理中间事实，canonical observation 仍由 graph 持有。"""
        with self._lock:
            self._pending_outcomes.pop((run_id, call_id), None)

    def next_reconciliation_attempt(self, run_id: str, call_id: str) -> int:
        """原子递增未知结果查证次数，重启不能通过调用参数重置。"""
        key = (run_id, call_id)
        with self._lock:
            count = self._reconciliation_attempts.get(key, 0) + 1
            self._reconciliation_attempts[key] = count
            return count

    def create_once(self, run_id: str, call_id: str, now: float, arguments_hash: str, effect: str) -> LocalFollowUpRequest:
        """以 run/call 唯一键收敛重复批准，不执行外部通知。"""
        key = (run_id, call_id)
        with self._lock:
            existing = self._follow_ups.get(key)
            if existing is not None:
                if existing.arguments_hash != arguments_hash or existing.effect != effect:
                    raise ApprovalConflict("幂等键已绑定不同的冻结副作用")
                return existing
            created = LocalFollowUpRequest(run_id, call_id, now, arguments_hash, effect)
            self._follow_ups[key] = created
            return created

    def lookup_by_idempotency_key(self, run_id: str, call_id: str) -> LocalFollowUpRequest | None:
        """按唯一键查证未知结果，不创建新事实。"""
        with self._lock:
            return self._follow_ups.get((run_id, call_id))
