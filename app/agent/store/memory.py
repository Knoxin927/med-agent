"""M3.4 的确定性内存 store，作为 PostgreSQL 前的 CAS 测试替身。"""

# 导入锁，模拟单进程 store 对 create/load/CAS 的原子临界区。
from threading import Lock
from app.agent.graph.state import AgentRunStatus
from app.agent.store.port import AgentRunRecord, NewAgentRun, RunNotFound, StoreConflict, StoredRunState, TerminalRun, validate_store_input


class InMemoryAgentRunStore:
    """用内存字典验证版本和终态不变量，不声称跨进程持久化。"""

    def __init__(self) -> None:
        # 字典只用于 deterministic fake，生产 adapter 不复用此实现。
        self._records: dict[str, AgentRunRecord] = {}
        self._lock = Lock()

    def create(self, record: NewAgentRun) -> AgentRunRecord:
        # 创建和重复检查必须在同一锁内，模拟数据库唯一约束。
        with self._lock:
            if record.run_id in self._records:
                raise StoreConflict("run_id 已存在")
            validate_store_input(record.run_id, record.state, record.status)
            created = AgentRunRecord(record.run_id, 0, record.state, record.status)
            self._records[record.run_id] = created
            return created

    def load(self, run_id: str) -> AgentRunRecord:
        # 不存在统一映射为 RunNotFound，避免泄漏底层 KeyError。
        with self._lock:
            try:
                return self._records[run_id]
            except KeyError as error:
                raise RunNotFound("run 不存在") from error

    def compare_and_swap(self, run_id: str, expected_version: int, state: StoredRunState, status: AgentRunStatus) -> AgentRunRecord:
        # CAS 的读取、版本判断和写入必须在同一锁内完成。
        with self._lock:
            current = self._records.get(run_id)
            if current is None:
                raise RunNotFound("run 不存在")
            if current.status is not AgentRunStatus.running:
                raise TerminalRun("终态 run 不能普通覆盖")
            if current.version != expected_version:
                raise StoreConflict("run version 冲突")
            validate_store_input(run_id, state, status)
            updated = AgentRunRecord(run_id, current.version + 1, state, status)
            self._records[run_id] = updated
            return updated
