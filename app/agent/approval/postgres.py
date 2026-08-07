"""M3.5 PostgreSQL 审批与本地 follow-up 的参数化持久化实现。"""

# 导入 UTC 时间转换，确保 port 的 fake clock 可映射为 PostgreSQL timestamptz。
from datetime import UTC, datetime

# 导入 psycopg 连接类型，adapter 由装配层传入已建立的连接。
from psycopg import Connection

# 导入审批领域对象，SQL 只实现 port 而不泄露到 graph。
from app.agent.approval.port import (
    ApprovalConflict, ApprovalDecision, ApprovalRecord, ApprovalRequest,
    ApprovalStatus, ClaimConflict, ExecutionClaim, LocalFollowUpRequest,
    PendingOutcome,
)


def _timestamp(value: float) -> datetime:
    """将测试可控的 Unix 秒数转为时区明确的数据库时间。"""
    return datetime.fromtimestamp(value, UTC)


def _seconds(value: datetime) -> float:
    """把 PostgreSQL 时间戳转回 framework-neutral 的 Unix 秒数。"""
    return value.timestamp()


def _approval_record(row: tuple) -> ApprovalRecord:
    """把最小 SQL 行解码为领域记录，避免把数据库对象泄露出去。"""
    return ApprovalRecord(
        run_id=row[0], call_id=row[1], version=row[2], status=row[3],
        summary=row[4], effect=row[5], arguments_hash=row[6],
        expires_at=_seconds(row[7]), decided_by=row[8],
        decided_at=None if row[9] is None else _seconds(row[9]),
    )


class PostgresApprovalRepository:
    """以事务和行锁实现 TTL、决策 CAS、claim 与本地幂等写入。"""

    def __init__(self, connection: Connection) -> None:
        # 连接由应用装配层管理；这里不读取 .env 或输出连接信息。
        self._connection = connection

    def create_pending(self, request: ApprovalRequest) -> ApprovalRecord:
        """创建一条冻结审批记录；重复键必须 fail-closed。"""
        if not request.summary.strip() or not request.arguments_hash.strip() or request.expires_at <= 0:
            raise ValueError("审批请求字段不合法")
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agent_approvals "
                    "(run_id, call_id, version, status, summary, effect, arguments_hash, expires_at) "
                    "VALUES (%s, %s, 0, 'pending', %s, %s, %s, %s)",
                    (request.run_id, request.call_id, request.summary, request.effect, request.arguments_hash, _timestamp(request.expires_at)),
                )
            self._connection.commit()
        except Exception as error:
            self._connection.rollback()
            if getattr(error, "sqlstate", None) == "23505":
                raise ApprovalConflict("审批请求已存在") from error
            raise
        return ApprovalRecord(request.run_id, request.call_id, 0, "pending", request.summary, request.effect, request.arguments_hash, request.expires_at)

    def load(self, run_id: str, call_id: str) -> ApprovalRecord:
        """读取冻结审批事实，恢复路径不接收客户端提供的替换字段。"""
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "SELECT run_id, call_id, version, status, summary, effect, arguments_hash, expires_at, decided_by, decided_at "
                    "FROM agent_approvals WHERE run_id = %s AND call_id = %s",
                    (run_id, call_id),
                )
                row = cursor.fetchone()
            if row is None:
                raise ApprovalConflict("审批请求不存在")
            return _approval_record(row)
        finally:
            if not self._connection.autocommit:
                self._connection.rollback()

    def decide(self, run_id: str, call_id: str, expected_version: int, decision: ApprovalDecision, owner: str, now: float) -> ApprovalRecord:
        """在同一事务内锁定审批记录并执行 TTL 与版本检查。"""
        current_time = _timestamp(now)
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "SELECT run_id, call_id, version, status, summary, effect, arguments_hash, expires_at, decided_by, decided_at "
                    "FROM agent_approvals WHERE run_id = %s AND call_id = %s FOR UPDATE",
                    (run_id, call_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ApprovalConflict("审批请求不存在")
                current = _approval_record(row)
                if current.status == "pending" and now >= current.expires_at:
                    cursor.execute(
                        "UPDATE agent_approvals SET version = version + 1, status = 'expired', decided_at = %s "
                        "WHERE run_id = %s AND call_id = %s RETURNING "
                        "run_id, call_id, version, status, summary, effect, arguments_hash, expires_at, decided_by, decided_at",
                        (current_time, run_id, call_id),
                    )
                    current = _approval_record(cursor.fetchone())
                if current.status == "expired":
                    self._connection.commit()
                    return current
                if current.status != "pending":
                    same = (decision == "approve" and current.status == "approved") or (decision == "reject" and current.status == "rejected") or (decision == "cancel" and current.status == "cancelled")
                    if same:
                        self._connection.commit()
                        return current
                    raise ApprovalConflict("审批已进入其他状态")
                if current.version != expected_version:
                    raise ApprovalConflict("审批版本冲突")
                status: ApprovalStatus = {"approve": "approved", "reject": "rejected", "cancel": "cancelled"}[decision]
                cursor.execute(
                    "UPDATE agent_approvals SET version = version + 1, status = %s, decided_by = %s, decided_at = %s "
                    "WHERE run_id = %s AND call_id = %s AND version = %s AND status = 'pending' AND expires_at > %s "
                    "RETURNING run_id, call_id, version, status, summary, effect, arguments_hash, expires_at, decided_by, decided_at",
                    (status, owner, current_time, run_id, call_id, expected_version, current_time),
                )
                updated = cursor.fetchone()
                if updated is None:
                    raise ApprovalConflict("审批版本或 TTL 冲突")
            self._connection.commit()
            return _approval_record(updated)
        except Exception:
            self._connection.rollback()
            raise

    def claim_execution(self, run_id: str, call_id: str, owner: str, lease_seconds: int, now: float) -> ExecutionClaim:
        """只为已批准记录授予可过期的单 worker 租约。"""
        if lease_seconds <= 0 or not owner.strip():
            raise ValueError("claim 租约参数不合法")
        current_time = _timestamp(now)
        lease_expires_at = _timestamp(now + lease_seconds)
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status FROM agent_approvals WHERE run_id = %s AND call_id = %s FOR UPDATE",
                    (run_id, call_id),
                )
                approval = cursor.fetchone()
                if approval is None or approval[0] != "approved":
                    raise ClaimConflict("只有 approved 请求可以 claim")
                cursor.execute(
                    "SELECT owner, lease_expires_at, claim_version FROM agent_execution_claims "
                    "WHERE run_id = %s AND call_id = %s FOR UPDATE",
                    (run_id, call_id),
                )
                current = cursor.fetchone()
                if current is not None and current[1] > current_time:
                    raise ClaimConflict("已有未过期 execution claim")
                version = 0 if current is None else current[2] + 1
                if current is None:
                    cursor.execute(
                        "INSERT INTO agent_execution_claims (run_id, call_id, owner, lease_expires_at, claim_version) VALUES (%s, %s, %s, %s, %s)",
                        (run_id, call_id, owner, lease_expires_at, version),
                    )
                else:
                    cursor.execute(
                        "UPDATE agent_execution_claims SET owner = %s, lease_expires_at = %s, claim_version = %s "
                        "WHERE run_id = %s AND call_id = %s AND claim_version = %s",
                        (owner, lease_expires_at, version, run_id, call_id, current[2]),
                    )
                    if cursor.rowcount != 1:
                        raise ClaimConflict("execution claim 已被并发更新")
            self._connection.commit()
            return ExecutionClaim(run_id, call_id, owner, now + lease_seconds, version)
        except Exception:
            self._connection.rollback()
            raise

    def release_or_finish(self, claim: ExecutionClaim, outcome: PendingOutcome, now: float) -> None:
        """仅当前 owner/version 可以释放租约，旧 claim 的迟到写入被拒绝。"""
        if (outcome.run_id, outcome.call_id) != (claim.run_id, claim.call_id) or outcome.idempotency_key != f"{claim.run_id}:{claim.call_id}":
            raise ClaimConflict("claim 与 outcome 身份不一致")
        if outcome.attempt_count < 1:
            raise ValueError("outcome attempt_count 必须为正整数")
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agent_pending_outcomes (run_id, call_id, status, idempotency_key, tool_name, attempt_count, consumed) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (run_id, call_id) DO UPDATE SET status = EXCLUDED.status, idempotency_key = EXCLUDED.idempotency_key, tool_name = EXCLUDED.tool_name, attempt_count = EXCLUDED.attempt_count, consumed = EXCLUDED.consumed",
                    (outcome.run_id, outcome.call_id, outcome.status, outcome.idempotency_key, outcome.tool_name, outcome.attempt_count, outcome.consumed),
                )
                cursor.execute(
                    "DELETE FROM agent_execution_claims WHERE run_id = %s AND call_id = %s AND owner = %s AND claim_version = %s "
                    "AND lease_expires_at > %s",
                    (claim.run_id, claim.call_id, claim.owner, claim.claim_version, _timestamp(now)),
                )
                if cursor.rowcount != 1:
                    raise ClaimConflict("旧 execution claim 不能写入终局")
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def load_pending_outcome(self, run_id: str, call_id: str) -> PendingOutcome | None:
        """读取 append 前终局，支持本地写入成功后的 crash/replay。"""
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "SELECT run_id, call_id, status, idempotency_key, tool_name, attempt_count, consumed FROM agent_pending_outcomes WHERE run_id = %s AND call_id = %s",
                    (run_id, call_id),
                )
                row = cursor.fetchone()
            return None if row is None else PendingOutcome(row[0], row[1], row[2], row[3], row[4], row[5], row[6])
        finally:
            if not self._connection.autocommit:
                self._connection.rollback()

    def store_pending_outcome(self, outcome: PendingOutcome) -> None:
        """独立持久化拒绝/取消/过期终局，支持 append 前崩溃恢复。"""
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agent_pending_outcomes (run_id, call_id, status, idempotency_key, tool_name, attempt_count, consumed) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (run_id, call_id) DO UPDATE SET status = EXCLUDED.status, idempotency_key = EXCLUDED.idempotency_key, tool_name = EXCLUDED.tool_name, attempt_count = EXCLUDED.attempt_count, consumed = EXCLUDED.consumed",
                    (outcome.run_id, outcome.call_id, outcome.status, outcome.idempotency_key, outcome.tool_name, outcome.attempt_count, outcome.consumed),
                )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def clear_pending_outcome(self, run_id: str, call_id: str) -> None:
        """canonical append 成功后删除中间事实，避免被当成未消费结果。"""
        try:
            with self._connection.cursor() as cursor:
                cursor.execute("DELETE FROM agent_pending_outcomes WHERE run_id = %s AND call_id = %s", (run_id, call_id))
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def next_reconciliation_attempt(self, run_id: str, call_id: str) -> int:
        """事务内递增查证次数，调用方无法通过重启重置预算。"""
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agent_reconciliation_attempts (run_id, call_id, attempt_count) VALUES (%s, %s, 1) "
                    "ON CONFLICT (run_id, call_id) DO UPDATE SET attempt_count = agent_reconciliation_attempts.attempt_count + 1 "
                    "RETURNING attempt_count",
                    (run_id, call_id),
                )
                row = cursor.fetchone()
            self._connection.commit()
            return row[0]
        except Exception:
            self._connection.rollback()
            raise

    def create_once(self, run_id: str, call_id: str, now: float, arguments_hash: str, effect: str) -> LocalFollowUpRequest:
        """插入本地唯一事实；重复调用读取既有记录而非再次创建。"""
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO local_follow_up_requests (run_id, call_id, created_at, arguments_hash, effect) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (run_id, call_id) DO NOTHING RETURNING run_id, call_id, created_at, arguments_hash, effect",
                    (run_id, call_id, _timestamp(now), arguments_hash, effect),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        "SELECT run_id, call_id, created_at, arguments_hash, effect FROM local_follow_up_requests WHERE run_id = %s AND call_id = %s",
                        (run_id, call_id),
                    )
                    row = cursor.fetchone()
            self._connection.commit()
            if row is None:
                raise RuntimeError("本地 follow-up 幂等记录读取失败")
            existing = LocalFollowUpRequest(row[0], row[1], _seconds(row[2]), row[3], row[4])
            if existing.arguments_hash != arguments_hash or existing.effect != effect:
                raise ApprovalConflict("幂等键已绑定不同的冻结副作用")
            return existing
        except Exception:
            self._connection.rollback()
            raise

    def lookup_by_idempotency_key(self, run_id: str, call_id: str) -> LocalFollowUpRequest | None:
        """只读查证本地唯一事实；未命中不产生任何副作用。"""
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "SELECT run_id, call_id, created_at, arguments_hash, effect FROM local_follow_up_requests WHERE run_id = %s AND call_id = %s",
                    (run_id, call_id),
                )
                row = cursor.fetchone()
            return None if row is None else LocalFollowUpRequest(row[0], row[1], _seconds(row[2]), row[3], row[4])
        finally:
            if not self._connection.autocommit:
                self._connection.rollback()
