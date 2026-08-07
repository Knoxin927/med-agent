"""把 LangGraph runner 的一次运行协调到 AgentRunStore 的单一 CAS 版本事实。"""

# 导入 Callable，事件发布由调用方注入且只能在提交后运行。
from collections.abc import Callable
from app.agent.graph.runner import AgentGraphRunner
from app.agent.graph.state import AgentRunStatus, AgentState
from app.agent.store.port import AgentRunRecord, AgentRunStore, NewAgentRun, PersistedRunControl


class AgentRunCheckpointCoordinator:
    """以 AgentRunStore 作为 graph checkpoint 的唯一持久化真相。"""

    def __init__(self, store: AgentRunStore, runner: AgentGraphRunner) -> None:
        # runner 只负责编排，store 负责跨请求版本与事务事实。
        self._store = store
        self._runner = runner

    def create(self, state: AgentState) -> AgentRunRecord:
        """创建 version=0 的 run，供后续 resume 使用。"""

        # status 必须与 state 同源，不能由外层自由覆盖。
        return self._store.create(NewAgentRun(state.run_id, state, state.status))

    def resume(self, run_id: str, publish_after_commit: Callable[[AgentRunRecord], None] | None = None) -> AgentRunRecord:
        """读取当前版本、逐节点提交 graph 状态，并在每次提交后才发布事件。"""

        # 先观察版本；每个 LangGraph 节点更新都必须先成为可恢复的 CAS 事实。
        current = self._store.load(run_id)
        if isinstance(current.state, PersistedRunControl):
            if current.state.status is not AgentRunStatus.running:
                return current
            failed = current.state.consume_approval_projection() if current.state.approval_projection else current.state.fail_for_restart()
            return self._store.compare_and_swap(run_id, current.version, failed, failed.status)
        committed = current
        for next_state in self._runner.stream_states(current.state):
            # 已终态后不再接受后续节点快照，避免 fail 边重复覆盖。
            if committed.status is not AgentRunStatus.running:
                break
            committed = self._store.compare_and_swap(run_id, committed.version, next_state, next_state.status)
            # 回调严格位于每次 CAS 之后，保证观察者不会看到未提交版本。
            if publish_after_commit is not None:
                publish_after_commit(committed)
        return committed
