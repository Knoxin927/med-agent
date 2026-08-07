"""定义 M3.4 版本化 AgentRunStore 端口与受限状态编码。"""

# 导入 dataclass 与 Protocol，声明不可变记录和窄存储接口。
from dataclasses import dataclass
from typing import Protocol
from app.agent.graph.state import AgentRunStatus, AgentState
from app.agent.types import AgentErrorCode


# store 层只接受这些项目生命周期状态，避免自由字符串覆盖终态保护。
RunStatus = AgentRunStatus


# 统一的持久化错误类型；调用方不依赖数据库异常文本分支。
class StoreConflict(RuntimeError):
    """run_id 重复或 CAS 版本过期。"""


class RunNotFound(RuntimeError):
    """请求的 run_id 不存在。"""


class TerminalRun(StoreConflict):
    """终态记录不能被普通 CAS 覆盖。"""


# 新建记录只携带项目状态和明确的初始生命周期状态。
@dataclass(frozen=True)
class NewAgentRun:
    """创建 Agent run 的最小输入。"""

    run_id: str
    state: AgentState
    status: RunStatus = AgentRunStatus.running


# PostgreSQL 只保存这份控制投影，绝不保存问题、对话、工具参数或检索正文。
@dataclass(frozen=True)
class PersistedRunControl:
    """不含恢复上下文的 durable Agent run 控制面。"""

    schema_version: int
    run_id: str
    status: RunStatus
    step_count: int
    max_steps: int
    active_budget_remaining_ms: int
    terminal_error_code: AgentErrorCode | None
    requires_restart: bool
    approval_projection: dict | None = None

    @classmethod
    def from_state(cls, state: AgentState) -> "PersistedRunControl":
        """提取允许落库的控制字段；running state 不携带可恢复上下文。"""
        projection = None
        if state.pending_call is not None and state.pending_tool_outcome is not None and state.pending_tool_outcome.origin == "approval":
            observation = state.pending_tool_outcome.observation
            outcome_status = "succeeded" if observation.success else _approval_outcome_status(observation.error_code)
            projection = {
                "call_id": observation.call_id,
                "tool_name": observation.tool_name,
                "outcome_status": outcome_status,
                "idempotency_key": f"{state.run_id}:{observation.call_id}",
                "success": observation.success,
                "error_code": None if observation.error_code is None else observation.error_code.value,
                "attempt_count": state.pending_tool_outcome.attempt_count,
                "consumed": False,
            }
        return cls(
            schema_version=state.schema_version,
            run_id=state.run_id,
            status=state.status,
            step_count=state.step_count,
            max_steps=state.max_steps,
            active_budget_remaining_ms=state.active_budget_remaining_ms,
            terminal_error_code=state.terminal_error_code,
            requires_restart=state.status is AgentRunStatus.running,
            approval_projection=projection,
        )

    def fail_for_restart(self) -> "PersistedRunControl":
        """将无恢复上下文的运行中记录安全转为稳定失败终态。"""

        return PersistedRunControl(
            schema_version=self.schema_version,
            run_id=self.run_id,
            status=AgentRunStatus.failed,
            step_count=self.step_count,
            max_steps=self.max_steps,
            active_budget_remaining_ms=self.active_budget_remaining_ms,
            terminal_error_code=AgentErrorCode.resume_requires_restart,
            requires_restart=False,
            approval_projection=self.approval_projection,
        )

    def consume_approval_projection(self) -> "PersistedRunControl":
        """以不可变投影标记一次消费，禁止重启重复处理同一终局。"""
        if self.approval_projection is None or self.approval_projection.get("consumed") is True:
            return self
        projection = dict(self.approval_projection)
        projection["consumed"] = True
        return PersistedRunControl(
            schema_version=self.schema_version, run_id=self.run_id,
            status=AgentRunStatus.failed, step_count=self.step_count,
            max_steps=self.max_steps, active_budget_remaining_ms=self.active_budget_remaining_ms,
            terminal_error_code=AgentErrorCode.resume_requires_restart,
            requires_restart=False, approval_projection=projection,
        )


StoredRunState = AgentState | PersistedRunControl


# 记录的 version 是 CAS 唯一事实，创建版本固定为 0。
@dataclass(frozen=True)
class AgentRunRecord:
    """保存一次可恢复 run 的版本化快照。"""

    run_id: str
    version: int
    state: StoredRunState
    status: RunStatus


# 项目拥有的 store port，不暴露 ORM、SQL 或 LangGraph 私有对象。
class AgentRunStore(Protocol):
    """定义 create/load/CAS 三个必须具备的持久化动作。"""

    def create(self, record: NewAgentRun) -> AgentRunRecord:
        """以唯一 run_id 创建 version=0 记录。"""

    def load(self, run_id: str) -> AgentRunRecord:
        """读取当前版本，不修改状态。"""

    def compare_and_swap(
        self, run_id: str, expected_version: int, state: StoredRunState, status: RunStatus
    ) -> AgentRunRecord:
        """仅在 expected_version 匹配且记录非终态时递增版本。"""


# 统一校验状态列和 JSON state 的身份/生命周期，避免同一记录出现双真相。
def validate_store_input(run_id: str, state: StoredRunState, status: RunStatus) -> None:
    """拒绝 run_id 或 status 与项目 AgentState 不一致的持久化输入。"""

    # 数据库行的 run_id、status 必须完全来自相同的领域状态快照。
    if state.run_id != run_id:
        raise ValueError("run_id 与 AgentState 不一致")
    if state.status is not status:
        raise ValueError("status 与 AgentState 不一致")
    if isinstance(state, PersistedRunControl) and state.requires_restart != (status is AgentRunStatus.running):
        raise ValueError("控制快照 requires_restart 与 status 不一致")


# 把可恢复状态编码为白名单 JSON；未来数据库 adapter 只能使用此入口。
def encode_persisted_state(state: StoredRunState) -> dict:
    """编码严格控制面；不允许完整会话、原始参数或医疗相关正文进入数据库。"""

    control = state if isinstance(state, PersistedRunControl) else PersistedRunControl.from_state(state)
    return {
        "schema_version": control.schema_version,
        "run_id": control.run_id,
        "status": control.status.value,
        "step_count": control.step_count,
        "max_steps": control.max_steps,
        "active_budget_remaining_ms": control.active_budget_remaining_ms,
        "terminal_error_code": None if control.terminal_error_code is None else control.terminal_error_code.value,
        "requires_restart": control.requires_restart,
        "approval_projection": control.approval_projection,
    }


# 从白名单 JSON 重建领域状态；缺字段或未知枚举一律拒绝而不是猜测恢复。
def decode_persisted_state(payload: dict) -> PersistedRunControl:
    """把数据库控制快照还原为不可携带敏感上下文的投影。"""

    # 顶层字段必须精确匹配 encoder 输出，防止任意 JSON 混入状态。
    required = {
        "schema_version", "run_id", "status", "step_count", "max_steps",
        "active_budget_remaining_ms", "terminal_error_code", "requires_restart", "approval_projection",
    }
    if set(payload) != required:
        raise ValueError("持久化 state 字段不符合白名单")
    error_code = payload["terminal_error_code"]
    control = PersistedRunControl(
        schema_version=payload["schema_version"],
        run_id=payload["run_id"],
        status=AgentRunStatus(payload["status"]),
        step_count=payload["step_count"],
        max_steps=payload["max_steps"],
        active_budget_remaining_ms=payload["active_budget_remaining_ms"],
        terminal_error_code=None if error_code is None else AgentErrorCode(error_code),
        requires_restart=payload["requires_restart"],
        approval_projection=payload["approval_projection"],
    )
    projection = control.approval_projection
    if projection is not None:
        allowed = {"call_id", "tool_name", "outcome_status", "idempotency_key", "success", "error_code", "attempt_count", "consumed"}
        statuses = {"succeeded", "rejected", "expired", "cancelled", "failed"}
        if set(projection) != allowed or not isinstance(projection["call_id"], str) or not projection["call_id"] or not isinstance(projection["tool_name"], str) or not projection["tool_name"] or projection["outcome_status"] not in statuses or projection["idempotency_key"] != f"{control.run_id}:{projection['call_id']}" or type(projection["success"]) is not bool or (projection["error_code"] is not None and not isinstance(projection["error_code"], str)) or type(projection["attempt_count"]) is not int or projection["attempt_count"] < 1 or type(projection["consumed"]) is not bool:
            raise ValueError("approval_projection 不符合最小白名单")
        if projection["consumed"] and (control.status is AgentRunStatus.running or control.requires_restart):
            raise ValueError("已消费 approval_projection 不能对应可恢复运行")
        expected = {
            "succeeded": (True, None),
            "rejected": (False, AgentErrorCode.approval_conflict.value),
            "expired": (False, AgentErrorCode.approval_expired.value),
            "cancelled": (False, AgentErrorCode.cancelled.value),
            "failed": (False, AgentErrorCode.internal_error.value),
        }[projection["outcome_status"]]
        if (projection["success"], projection["error_code"]) != expected:
            raise ValueError("approval_projection 终局语义不一致")
    validate_store_input(control.run_id, control, control.status)
    return control


def _approval_outcome_status(error_code: AgentErrorCode | None) -> str:
    """将已映射的稳定错误码还原为可审计的审批终局类别。"""

    return {
        AgentErrorCode.approval_conflict: "rejected",
        AgentErrorCode.approval_expired: "expired",
        AgentErrorCode.cancelled: "cancelled",
    }.get(error_code, "failed")
