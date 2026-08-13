"""M3.5 将 durable 审批终局桥接为唯一 pending tool outcome。"""

# 导入 approval 终局与既有 graph state，保持 append 节点仍是唯一 observation owner。
from app.agent.approval.port import PendingOutcome
from dataclasses import replace
from app.agent.graph.state import AgentState, PendingToolOutcome, StateTransitionError
from app.agent.types import AgentErrorCode, ToolObservation


def apply_approval_outcome(state: AgentState, outcome: PendingOutcome) -> AgentState:
    """把审批终局放入 pending outcome；调用方随后只能进入 append 节点。"""
    if state.pending_call is None:
        raise StateTransitionError(AgentErrorCode.internal_error, "审批恢复时没有 pending_call")
    if state.pending_call.call_id != outcome.call_id or state.run_id != outcome.run_id:
        raise StateTransitionError(AgentErrorCode.internal_error, "审批终局与 pending_call 不匹配")
    if outcome.status == "reconciliation_required":
        # 非终态未知结果不能伪造 observation，也不能让模型继续。
        return replace(state, approval_status="reconciliation_required")
    if outcome.status == "succeeded":
        observation = ToolObservation(outcome.call_id, state.pending_call.tool_name, True, None, None, [])
    else:
        code = AgentErrorCode.approval_expired if outcome.status == "expired" else AgentErrorCode.approval_conflict if outcome.status == "rejected" else AgentErrorCode.cancelled if outcome.status == "cancelled" else AgentErrorCode.internal_error
        message = "人工审批已过期" if outcome.status == "expired" else "人工审批已拒绝" if outcome.status == "rejected" else "人工审批未允许执行"
        observation = ToolObservation(outcome.call_id, state.pending_call.tool_name, False, code, message, [])
    wait_status = "none" if outcome.status == "succeeded" else "terminal"
    return replace(state, approval_status=wait_status, terminal_error_code=None if outcome.status == "succeeded" else observation.error_code).with_pending_tool_outcome(PendingToolOutcome.from_observation(observation, attempt_count=1, origin="approval"))


def mark_approval_required(state: AgentState) -> AgentState:
    """将待审批调用冻结在 graph 内，后续 run 只能等待 owner resume。"""
    if state.pending_call is None:
        raise StateTransitionError(AgentErrorCode.internal_error, "审批暂停时没有 pending_call")
    return replace(state, approval_status="pending", transitions=state.transitions + ("approval_required",))
