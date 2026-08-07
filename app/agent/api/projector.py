"""M3.6 终态与进度投影：只输出脱敏公开事件。"""

# 导入 PublicEvent，投影结果永远是脱敏事件。
from app.agent.api.events import PublicEvent
# 导入 store 记录与控制投影类型。
from app.agent.store.port import AgentRunRecord, PersistedRunControl
# 导入完整 AgentState，进程内 runner 成功路径需要从 conversation 聚合 sources。
from app.agent.graph.state import AgentRunStatus, AgentState
# 导入稳定错误码，失败事件只暴露 code/message。
from app.agent.types import AgentErrorCode


# 把稳定错误码映射为公开、脱敏的中文说明。
_ERROR_MESSAGES = {
    AgentErrorCode.unknown_tool: "未知工具",
    AgentErrorCode.invalid_arguments: "工具参数不合法",
    AgentErrorCode.model_protocol_error: "模型协议错误",
    AgentErrorCode.tool_execution_error: "工具执行失败",
    AgentErrorCode.step_limit_exceeded: "步数预算已耗尽",
    AgentErrorCode.active_budget_exceeded: "等待预算已耗尽",
    AgentErrorCode.cancelled: "运行已取消",
    AgentErrorCode.internal_error: "内部错误",
    AgentErrorCode.resume_requires_restart: "跨进程恢复需要重新发起",
    AgentErrorCode.approval_required: "需要人工审批",
    AgentErrorCode.approval_conflict: "审批决策冲突",
    AgentErrorCode.approval_expired: "审批已过期",
    AgentErrorCode.reconciliation_required: "副作用结果待查证",
}


def project_run_started(record: AgentRunRecord) -> PublicEvent:
    """创建 run 后的首个公开事件。"""

    return PublicEvent("run_started", {"run_id": record.run_id, "version": record.version})


def project_terminal_record(record: AgentRunRecord) -> list[PublicEvent]:
    """从 durable 终态投影 answer/sources/done 或单一 error。"""

    # 运行中记录只允许继续等待，不能伪造 answer/done。
    if record.status is AgentRunStatus.running:
        return []
    # 失败与取消都只发单一脱敏 error，不伪装成功。
    if record.status in {AgentRunStatus.failed, AgentRunStatus.cancelled}:
        code = _terminal_error_code(record)
        return [PublicEvent("error", {"code": code.value, "message": _ERROR_MESSAGES.get(code, "运行失败")})]
    # 成功终态必须有完整 answer；进程内 AgentState 才允许聚合 sources。
    if not isinstance(record.state, AgentState) or not record.state.final_answer:
        return [PublicEvent("error", {"code": AgentErrorCode.internal_error.value, "message": "内部错误"})]
    events = [
        PublicEvent("answer", {"text": record.state.final_answer}),
        PublicEvent("sources", aggregate_sources_v2(record.state)),
        PublicEvent("done", {"version": record.version}),
    ]
    return events


def project_tool_status(record: AgentRunRecord) -> PublicEvent | None:
    """从当前 durable 状态投影 tool_status；没有可公开进度时返回 None。"""

    state = record.state
    if not isinstance(state, AgentState) or state.pending_call is None:
        return None
    # 审批链路只公开 awaiting_approval / reconciliation，绝不能在批准前宣称 running。
    if state.approval_status == "pending":
        status = "awaiting_approval"
    elif state.approval_status == "reconciliation_required":
        status = "reconciliation_required"
    elif state.pending_tool_outcome is not None:
        # 已有终局结果时才公开 succeeded/failed；append 前后都允许投影该事实。
        status = "succeeded" if state.pending_tool_outcome.observation.success else "failed"
    elif state.active_segment is not None and state.approval_status == "none":
        # 仅在项目已预留外调 segment 且不在审批等待时，才公开 running。
        status = "running"
    else:
        # validate/set_pending 等中间快照对客户端不可见，避免误导“工具已执行”。
        return None
    return PublicEvent(
        "tool_status",
        {
            "call_id": state.pending_call.call_id,
            "name": state.pending_call.tool_name,
            "state": status,
        },
    )


def aggregate_sources_v2(state: AgentState) -> dict:
    """按 canonical observation 完成顺序聚合 sources v2。"""

    # 用 (source_name, chunk_index) 保留首次出现，避免多轮检索重复引用。
    seen: set[tuple[str, int]] = set()
    items: list[dict] = []
    for message in state.conversation:
        if message.get("kind") != "tool_observation":
            continue
        if message.get("tool_name") != "search_knowledge" or message.get("success") is not True:
            continue
        for chunk in message.get("chunks") or []:
            key = (chunk["source_name"], chunk["chunk_index"])
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "source_name": chunk["source_name"],
                    "chunk_index": chunk["chunk_index"],
                    "rank": chunk["rank"],
                    "method": chunk["method"],
                }
            )
    return {"schema_version": 2, "items": items}


def _terminal_error_code(record: AgentRunRecord) -> AgentErrorCode:
    """从完整状态或控制投影提取稳定错误码。"""

    if isinstance(record.state, AgentState) and record.state.terminal_error_code is not None:
        return record.state.terminal_error_code
    if isinstance(record.state, PersistedRunControl) and record.state.terminal_error_code is not None:
        return record.state.terminal_error_code
    if record.status is AgentRunStatus.cancelled:
        return AgentErrorCode.cancelled
    return AgentErrorCode.internal_error
