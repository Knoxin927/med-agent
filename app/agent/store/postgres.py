"""M3.4 PostgreSQL AgentRunStore：参数化 SQL、JSON 白名单和 version CAS。"""

# 导入 Jsonb，确保 psycopg 以 JSONB 参数而非字符串拼接写入状态。
from psycopg import Connection
from psycopg.types.json import Jsonb
from app.agent.graph.state import AgentRunStatus
from app.agent.store.port import AgentRunRecord, NewAgentRun, RunNotFound, StoreConflict, StoredRunState, TerminalRun, decode_persisted_state, encode_persisted_state, validate_store_input


class PostgresAgentRunStore:
    """使用已提交事务作为跨请求 run/version 的唯一事实。"""

    def __init__(self, connection: Connection) -> None:
        # 连接由应用装配层创建，store 不读取 .env 或持有凭据。
        self._connection = connection

    def create(self, record: NewAgentRun) -> AgentRunRecord:
        # 唯一约束冲突由 SQL 捕获并映射为项目稳定错误。
        validate_store_input(record.run_id, record.state, record.status)
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agent_runs (run_id, version, status, state_json) VALUES (%s, 0, %s, %s)",
                    (record.run_id, record.status.value, Jsonb(encode_persisted_state(record.state))),
                )
            self._connection.commit()
        except Exception as error:
            self._connection.rollback()
            if getattr(error, "sqlstate", None) == "23505":
                raise StoreConflict("run_id 已存在") from error
            raise
        persisted = decode_persisted_state(encode_persisted_state(record.state))
        return AgentRunRecord(record.run_id, 0, persisted, record.status)

    def load(self, run_id: str) -> AgentRunRecord:
        # load 不修改版本，读取到的数据再经 codec 严格验证。
        try:
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT version, status, state_json FROM agent_runs WHERE run_id = %s", (run_id,))
                row = cursor.fetchone()
            if row is None:
                raise RunNotFound("run 不存在")
            state = decode_persisted_state(row[2])
            status = AgentRunStatus(row[1])
            validate_store_input(run_id, state, status)
            return AgentRunRecord(run_id, row[0], state, status)
        finally:
            # psycopg 默认读操作也会开启事务；读取后必须结束它，避免连接长期 idle in transaction。
            if not self._connection.autocommit:
                self._connection.rollback()

    def compare_and_swap(self, run_id: str, expected_version: int, state: StoredRunState, status: AgentRunStatus) -> AgentRunRecord:
        # SQL 条件同时保护版本和终态；受影响行必须恰好为一条。
        validate_store_input(run_id, state, status)
        with self._connection.cursor() as cursor:
            cursor.execute(
                "UPDATE agent_runs SET version = version + 1, status = %s, state_json = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE run_id = %s AND version = %s AND status = 'running' RETURNING version",
                (status.value, Jsonb(encode_persisted_state(state)), run_id, expected_version),
            )
            row = cursor.fetchone()
        if row is None:
            self._connection.rollback()
            current = self.load(run_id)
            if current.status is not AgentRunStatus.running:
                raise TerminalRun("终态 run 不能普通覆盖")
            raise StoreConflict("run version 冲突")
        self._connection.commit()
        return AgentRunRecord(run_id, row[0], state, status)
