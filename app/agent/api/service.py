"""M3.6 Agent API 服务：只做校验、装配、编码，不复制 graph 分支。"""

# 导入 queue/threading，把 CAS 后事件增量交给 SSE 消费。
from queue import Queue
from threading import Thread
import time
# 导入 uuid，生成服务端拥有的 run_id。
from uuid import uuid4
# 导入 PublicEvent，服务只返回脱敏事件序列。
from app.agent.api.events import PublicEvent
# 导入投影器，把 durable 记录转成公开事件。
from app.agent.api.projector import project_run_started, project_terminal_record, project_tool_status
# 导入审批服务与决策字面量，resume 只能接受匹配 pending decision。
from app.agent.approval.port import ApprovalConflict, ApprovalDecision, ClaimConflict
from app.agent.tools.create_follow_up_request import CreateFollowUpRequestService
# 导入 graph 状态转换，取消与创建初始状态都走项目状态层。
from app.agent.graph.state import AgentRunStatus, AgentState, create_agent_state, cancel_run
# 导入 store 与协调器，checkpoint 成功后才能发布事件。
from app.agent.store.checkpoint import AgentRunCheckpointCoordinator
from app.agent.store.port import AgentRunRecord, AgentRunStore, RunNotFound, StoreConflict, TerminalRun


class AgentApiError(RuntimeError):
    """API 层稳定错误，供路由映射为 HTTP/SSE 契约。"""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class AgentApiService:
    """把 start/resume/cancel 接到 store/runner/approval 端口。"""

    def __init__(
        self,
        store: AgentRunStore,
        coordinator: AgentRunCheckpointCoordinator,
        *,
        approval_service: CreateFollowUpRequestService | None = None,
    ) -> None:
        # store 拥有 CAS 事实；coordinator 负责每次提交后再发布事件。
        self._store = store
        self._coordinator = coordinator
        self._approval_service = approval_service
        # 可选清理钩子：生产装配可关闭 runtime 与模型 HTTP 客户端。
        self._cleanup = None

    def attach_cleanup(self, cleanup) -> None:
        """挂接资源清理回调，供应用 lifespan 关闭时调用。"""

        self._cleanup = cleanup

    def close(self) -> None:
        """释放本服务持有的可选资源；无钩子时是空操作。"""

        cleanup = self._cleanup
        self._cleanup = None
        if cleanup is not None:
            cleanup()

    def start(self, question: str) -> tuple[AgentRunRecord, list[PublicEvent]]:
        """创建 run 并推进到暂停或终态；兼容批量收集全部公开事件。"""

        record = None
        events: list[PublicEvent] = []
        for item in self.iter_start(question):
            if isinstance(item, PublicEvent):
                events.append(item)
            else:
                record = item
        assert record is not None
        return record, events

    def iter_start(self, question: str):
        """创建 run 后按 CAS 提交顺序增量产出公开事件，最后 yield 终态记录。"""

        cleaned = question.strip()
        if not cleaned:
            raise AgentApiError("invalid_request", "question 不能为空白", status_code=422)
        run_id = f"agent-{uuid4().hex}"
        state = create_agent_state(run_id, cleaned)
        created = self._coordinator.create(state)
        yield project_run_started(created)
        yield from self._iter_resume_committed(run_id)

    def resume(
        self,
        run_id: str,
        decision: ApprovalDecision,
        *,
        owner: str = "local-owner",
        expected_version: int | None = None,
        now: float | None = None,
    ) -> tuple[AgentRunRecord, list[PublicEvent]]:
        """只接受匹配 pending approval 的决策；兼容批量收集全部公开事件。"""

        record = None
        events: list[PublicEvent] = []
        for item in self.iter_resume(run_id, decision, owner=owner, expected_version=expected_version, now=now):
            if isinstance(item, PublicEvent):
                events.append(item)
            else:
                record = item
        assert record is not None
        return record, events

    def iter_resume(
        self,
        run_id: str,
        decision: ApprovalDecision,
        *,
        owner: str = "local-owner",
        expected_version: int | None = None,
        now: float | None = None,
    ):
        """审批决策落库后，按 CAS 提交顺序增量产出公开事件，最后 yield 终态记录。"""

        if decision not in {"approve", "reject", "cancel"}:
            raise AgentApiError("invalid_request", "decision 不合法", status_code=422)
        if self._approval_service is None:
            raise AgentApiError("approval_unavailable", "审批服务不可用", status_code=503)
        try:
            current = self._store.load(run_id)
        except RunNotFound as error:
            raise AgentApiError("run_not_found", "run 不存在", status_code=404) from error
        if current.status is not AgentRunStatus.running or not isinstance(current.state, AgentState):
            raise AgentApiError("terminal_run", "终态 run 不能 resume", status_code=409)
        if current.state.pending_call is None or current.state.approval_status != "pending":
            raise AgentApiError("approval_required", "当前没有待审批决策", status_code=409)
        if expected_version is not None and expected_version != current.version:
            raise AgentApiError("version_conflict", "run version 冲突", status_code=409)
        call_id = current.state.pending_call.call_id
        approval = self._approval_service.load_approval(run_id, call_id)
        decision_time = time.time() if now is None else now
        try:
            self._approval_service.decide(run_id, call_id, approval.version, decision, owner, decision_time)
        except ApprovalConflict as error:
            raise AgentApiError("approval_conflict", "审批决策冲突", status_code=409) from error
        except ClaimConflict as error:
            raise AgentApiError("claim_conflict", "执行租约冲突", status_code=409) from error
        except Exception as error:  # noqa: BLE001 - 未知异常统一脱敏，避免泄漏堆栈。
            raise AgentApiError("internal_error", "内部错误", status_code=500) from error
        yield from self._iter_resume_committed(run_id)

    def cancel(self, run_id: str) -> tuple[AgentRunRecord, list[PublicEvent]]:
        """幂等取消；终态 run 返回当前投影，不覆盖。"""

        try:
            current = self._store.load(run_id)
        except RunNotFound as error:
            raise AgentApiError("run_not_found", "run 不存在", status_code=404) from error
        if current.status is not AgentRunStatus.running:
            return current, project_terminal_record(current)
        if not isinstance(current.state, AgentState):
            # 跨进程控制投影没有可恢复上下文，只能 fail-closed。
            raise AgentApiError("resume_requires_restart", "跨进程恢复需要重新发起", status_code=409)
        cancelled_state = cancel_run(current.state)
        try:
            updated = self._store.compare_and_swap(run_id, current.version, cancelled_state, AgentRunStatus.cancelled)
        except TerminalRun:
            latest = self._store.load(run_id)
            return latest, project_terminal_record(latest)
        except StoreConflict as error:
            raise AgentApiError("version_conflict", "run version 冲突", status_code=409) from error
        return updated, project_terminal_record(updated)

    def _iter_resume_committed(self, run_id: str):
        """在后台推进 coordinator，主线程按 CAS 顺序消费公开事件。"""

        queue: Queue[object] = Queue()
        sentinel = object()
        last_tool_status: PublicEvent | None = None
        final_holder: dict[str, AgentRunRecord | BaseException | None] = {"record": None, "error": None}

        def publish_after_commit(record: AgentRunRecord) -> None:
            # 只在 CAS 成功后入队；连续重复 tool_status 直接丢弃，保持公开顺序稳定。
            status_event = project_tool_status(record)
            nonlocal last_tool_status
            if status_event is not None and status_event != last_tool_status:
                last_tool_status = status_event
                queue.put(status_event)
            if record.status is not AgentRunStatus.running:
                for event in project_terminal_record(record):
                    queue.put(event)

        def worker() -> None:
            try:
                final_holder["record"] = self._coordinator.resume(run_id, publish_after_commit)
            except BaseException as error:  # noqa: BLE001 - 必须把后台异常带回主线程。
                final_holder["error"] = error
            finally:
                queue.put(sentinel)

        thread = Thread(target=worker, name=f"agent-api-resume-{run_id}", daemon=True)
        thread.start()
        while True:
            item = queue.get()
            if item is sentinel:
                break
            assert isinstance(item, PublicEvent)
            yield item
        thread.join()
        error = final_holder["error"]
        if error is not None:
            raise error
        record = final_holder["record"]
        assert isinstance(record, AgentRunRecord)
        # 若 worker 只推进到 running 暂停点，确保不会漏发最后一次 tool_status。
        # publish 回调已覆盖暂停快照；这里只返回 durable 记录给路由结束流。
        yield record
