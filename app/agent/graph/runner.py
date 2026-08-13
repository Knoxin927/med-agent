"""用 LangGraph 编排 M3.3 的单 Agent 状态机。"""

# 导入 replace，用项目自有状态保存 pending 的最终回答。
from dataclasses import replace
import hashlib
import json
import time
# 导入 TypedDict，给 LangGraph 传递最小包装状态而不复制领域字段。
from collections.abc import Iterator
from typing import TypedDict

# LangGraph 只提供节点和边的调度，不拥有业务状态或工具策略。
from langgraph.graph import END, START, StateGraph

# 导入既有模型端口、工具运行时和项目状态转换。
from app.agent.model_client import AgentModelClient, AgentModelError
from app.agent.tool_runtime import ToolRuntime, make_failure_observation
from app.agent.types import AgentErrorCode, FinalAnswerDecision, ToolCallDecision, ToolEffect, ApprovalPolicy
from app.agent.approval.port import ApprovalConflict, ApprovalRequest
from app.agent.tools.create_follow_up_request import CreateFollowUpRequestService
from app.agent.graph.state import (
    ActiveSegmentKind, AgentState, PendingToolOutcome, StateTransitionError,
    append_pending_tool_outcome, cancel_run, complete_active_segment, complete_run, fail_run,
    reserve_external_step, set_pending_call,
)
from app.agent.graph.approval import mark_approval_required


# LangGraph 的 state 只包住一个项目拥有的 AgentState，避免双真相。
class _GraphInput(TypedDict):
    state: AgentState


class AgentGraphRunner:
    """唯一生产编排入口；模型与工具仍经既有 ports 调用。"""

    # 接收现有端口，禁止 graph 节点直接接触供应商或检索实现。
    def __init__(self, model: AgentModelClient, runtime: ToolRuntime, *, reservation_ms: int = 5_000, approval_service: CreateFollowUpRequestService | None = None, approval_ttl_seconds: int = 300) -> None:
        # 预留值是本 feature 的 in-process 等待预算上限。
        if type(approval_ttl_seconds) is not int or approval_ttl_seconds <= 0:
            raise ValueError("approval_ttl_seconds 必须是正整数")
        self._model = model
        self._runtime = runtime
        self._reservation_ms = reservation_ms
        # 副作用工具只有显式注入审批 service 才能进入 pause/resume 路径。
        self._approval_service = approval_service
        self._approval_ttl_seconds = approval_ttl_seconds
        self._graph = self._build_graph()

    # 从已创建或待恢复的项目状态开始运行图。
    def run(self, state: AgentState) -> AgentState:
        """运行固定节点和条件边，返回唯一的业务状态。"""

        # invoke 的输出仍是同一个项目状态，不暴露 LangGraph 私有对象。
        return self._graph.invoke({"state": self.resume_approval(state)})["state"]

    def resume_approval(self, state: AgentState) -> AgentState:
        """从 durable pending outcome 恢复，不接受 resume 请求携带替换参数。"""
        if self._approval_service is None or state.pending_call is None or state.pending_tool_outcome is not None:
            return state
        try:
            outcome = self._approval_service.load_pending_outcome(state.run_id, state.pending_call.call_id)
        except Exception:
            # durable 查证不可用时继续停留，不把基础设施异常变成模型可继续状态。
            return state
        if outcome is None:
            return state
        from app.agent.graph.approval import apply_approval_outcome
        return apply_approval_outcome(state, outcome)

    # 暴露 LangGraph 的节点级状态更新，让 durable store 能在每个节点后提交 checkpoint。
    def stream_states(self, state: AgentState) -> Iterator[AgentState]:
        """按图节点顺序产出项目 AgentState，不泄漏 LangGraph 私有 checkpoint。"""

        # updates 只返回本节点对 _GraphInput 的更新，状态真相仍是项目 AgentState。
        resumed = self.resume_approval(state)
        if resumed != state:
            # durable approval outcome 必须先成为 checkpoint，不能藏在节点内部崩溃窗口。
            yield resumed
        for update in self._graph.stream({"state": resumed}, stream_mode="updates"):
            for node_update in update.values():
                yield node_update["state"]

    # 构建 design 约定的节点和边；边函数只根据业务状态选路。
    def _build_graph(self):
        graph = StateGraph(_GraphInput)
        graph.add_node("start", self._start)
        graph.add_node("decide", self._decide)
        graph.add_node("validate_tool_call", self._validate_tool_call)
        graph.add_node("request_approval", self._request_approval)
        graph.add_node("execute_tool", self._execute_tool)
        graph.add_node("append_tool_result", self._append)
        graph.add_node("append_tool_error", self._append)
        graph.add_node("emit_final", self._emit_final)
        graph.add_node("fail", self._fail)
        graph.add_edge(START, "start")
        graph.add_conditional_edges(
            "start",
            self._after_start,
            {"decide": "decide", "result": "append_tool_result", "error": "append_tool_error", "approval": "approval_wait", "fail": "fail"},
        )
        graph.add_node("approval_wait", self._approval_wait)
        graph.add_edge("approval_wait", END)
        graph.add_conditional_edges("decide", self._after_decide, {"final": "emit_final", "tool": "validate_tool_call", "fail": "fail"})
        graph.add_conditional_edges("validate_tool_call", self._after_validation, {"execute": "execute_tool", "approval": "request_approval", "error": "append_tool_error"})
        graph.add_edge("request_approval", "approval_wait")
        graph.add_conditional_edges("execute_tool", self._after_tool, {"result": "append_tool_result", "error": "append_tool_error", "fail": "fail"})
        graph.add_conditional_edges("append_tool_result", self._after_append, {"decide": "decide", "fail": "fail"})
        graph.add_conditional_edges("append_tool_error", self._after_append, {"decide": "decide", "fail": "fail"})
        graph.add_edge("emit_final", END)
        graph.add_edge("fail", END)
        return graph.compile()

    # 新 run 正常进入 decide；恢复窗口先消费 pending outcome，绝不再次外调。
    def _start(self, data: _GraphInput) -> _GraphInput:
        state = data["state"]
        if state.approval_status not in {"none", "pending", "reconciliation_required", "terminal"}:
            return {"state": fail_run(state, AgentErrorCode.internal_error)}
        # approval/reconciliation 暂停合法地保留 pending_call 但没有工具结果。
        if state.approval_status != "none":
            return {"state": state}
        if (state.pending_call is None) != (state.pending_tool_outcome is None):
            state = fail_run(state, AgentErrorCode.internal_error)
        return {"state": state}

    def _after_start(self, data: _GraphInput) -> str:
        state = data["state"]
        if state.status.value != "running":
            return "fail"
        if state.pending_tool_outcome is not None:
            return "result" if state.pending_tool_outcome.observation.success else "error"
        if state.approval_status != "none":
            return "approval"
        return "decide"

    # 模型调用先预记资源，失败时只写稳定错误码。
    def _decide(self, data: _GraphInput) -> _GraphInput:
        state = data["state"]
        try:
            reserved = reserve_external_step(state, ActiveSegmentKind.model_decide, self._reservation_ms)
            decision = self._model.decide(_model_messages(reserved))
            state = complete_active_segment(reserved, elapsed_ms=0)
            if isinstance(decision, FinalAnswerDecision):
                state = replace(state, final_answer=decision.answer, transitions=state.transitions + ("decide_final",))
            elif isinstance(decision, ToolCallDecision):
                state = set_pending_call(state, decision.tool_call)
            else:
                state = fail_run(state, AgentErrorCode.model_protocol_error)
        except StateTransitionError as error:
            # 预算或步数等本地状态错误保留其稳定分类。
            state = fail_run(state, error.code) if state.status.value == "running" else state
        except AgentModelError:
            # 只有模型端口协议错误才映射为 model_protocol_error。
            state = fail_run(reserved, AgentErrorCode.model_protocol_error) if reserved.status.value == "running" else state
        return {"state": state}

    # ToolRuntime 本身拥有 schema 与白名单；副作用也必须先完成纯参数校验。
    def _validate_tool_call(self, data: _GraphInput) -> _GraphInput:
        state = data["state"]
        if state.pending_call is None:
            return {"state": fail_run(state, AgentErrorCode.invalid_arguments)}
        error = self._runtime.validate_call(state.pending_call)
        if error is None:
            return data
        observation = make_failure_observation(state.pending_call, AgentErrorCode.invalid_arguments, error)
        return {"state": state.with_pending_tool_outcome(PendingToolOutcome.from_observation(observation, attempt_count=1))}

    def _request_approval(self, data: _GraphInput) -> _GraphInput:
        """先写 durable pending approval，再把 graph 停在等待节点。"""
        state = data["state"]
        if self._approval_service is None or state.pending_call is None:
            return {"state": fail_run(state, AgentErrorCode.approval_required)}
        if state.pending_call.tool_name != "create_follow_up_request":
            return {"state": fail_run(state, AgentErrorCode.approval_conflict)}
        spec = self._runtime.get_spec(state.pending_call.tool_name)
        if spec is None or spec.effect is not ToolEffect.side_effect or spec.approval_policy is not ApprovalPolicy.required:
            return {"state": fail_run(state, AgentErrorCode.approval_conflict)}
        arguments_hash = hashlib.sha256(json.dumps(state.pending_call.arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        request = ApprovalRequest(state.run_id, state.pending_call.call_id, "创建本地随访请求", spec.effect.value, arguments_hash, time.time() + self._approval_ttl_seconds)
        try:
            self._approval_service.request(request)
        except ApprovalConflict:
            # graph checkpoint 在 durable row 写入后崩溃时，重复恢复只接受同一冻结 hash。
            existing = self._approval_service.load_approval(state.run_id, state.pending_call.call_id)
            if existing.arguments_hash != request.arguments_hash or existing.effect != request.effect:
                return {"state": fail_run(state, AgentErrorCode.approval_conflict)}
        return {"state": mark_approval_required(state)}

    def _approval_wait(self, data: _GraphInput) -> _GraphInput:
        """等待 owner resume；该节点不调用模型、不调用工具。"""
        return data

    # 只经 ToolRuntime 执行并先保存 pending outcome，append 节点绝不重执行。
    def _execute_tool(self, data: _GraphInput) -> _GraphInput:
        state = data["state"]
        try:
            reserved = reserve_external_step(state, ActiveSegmentKind.tool_execute, self._reservation_ms)
            observation = self._runtime.execute(reserved.pending_call)  # type: ignore[arg-type]
            state = complete_active_segment(reserved, elapsed_ms=0).with_pending_tool_outcome(PendingToolOutcome.from_observation(observation, attempt_count=1))
        except StateTransitionError as error:
            state = fail_run(state, error.code)
        return {"state": state}

    # 唯一 observation append owner，支持 future checkpoint 重放。
    def _append(self, data: _GraphInput) -> _GraphInput:
        # 外层 checkpoint 成功前不得清除 durable outcome，否则崩溃会丢失可恢复事实。
        return {"state": append_pending_tool_outcome(data["state"])}

    # emit_final 是唯一成功终态写入点。
    def _emit_final(self, data: _GraphInput) -> _GraphInput:
        state = data["state"]
        return {"state": complete_run(state, state.final_answer or "")}

    def _after_decide(self, data: _GraphInput) -> str:
        state = data["state"]
        return "fail" if state.status.value != "running" else ("final" if state.final_answer is not None else "tool")
    def _after_validation(self, data: _GraphInput) -> str:
        state = data["state"]
        if state.pending_call is None:
            return "error"
        if state.pending_tool_outcome is not None:
            return "error"
        spec = self._runtime.get_spec(state.pending_call.tool_name)
        if spec is not None and spec.effect is ToolEffect.side_effect:
            return "approval"
        return "execute"
    def _after_tool(self, data: _GraphInput) -> str:
        state = data["state"]
        # 预记失败没有结果可 append，必须直接终止而不是伪造错误 observation。
        if state.status.value != "running":
            return "fail"
        return "error" if state.pending_tool_outcome is None or not state.pending_tool_outcome.observation.success else "result"
    def _after_append(self, data: _GraphInput) -> str:
        state = data["state"]
        if state.approval_status == "terminal":
            return "fail"
        return "decide" if state.status.value == "running" else "fail"

    def _fail(self, data: _GraphInput) -> _GraphInput:
        state = data["state"]
        if state.status.value == "running" and state.terminal_error_code is not None:
            # 审批 cancel 必须进入 cancelled 终态，不能被通用 fail 边误写成 failed。
            if state.terminal_error_code is AgentErrorCode.cancelled:
                state = cancel_run(state)
            else:
                state = fail_run(state, state.terminal_error_code)
        return {"state": state}


# 由项目 conversation 还原端口需要的 OpenAI-compatible 消息。
def _model_messages(state: AgentState) -> list[dict]:
    # 延迟导入避免 framework-neutral state 层反向依赖消息 codec。
    from app.agent.messages import build_initial_agent_messages, format_assistant_tool_call_message, format_tool_observation_message
    # 导入快照与领域对象，把项目 JSON 重新变为模型端口需要的受控消息。
    from app.agent.types import AgentErrorCode, ToolCall, ToolObservation
    from app.retrieval_strategies.types import RankedChunk
    messages = build_initial_agent_messages(state.question)
    for item in state.conversation[1:]:
        if item["kind"] == "assistant_tool_call":
            messages.append(format_assistant_tool_call_message(ToolCall(item["call_id"], item["tool_name"], item["arguments"])))
        # observation 已是项目拥有的 JSON 快照；还原它不执行任何工具。
        elif item["kind"] == "tool_observation":
            # 历史/恢复路径若出现 authority 字段，直接 fail-closed，避免回流模型。
            if item.get("authority_payload") is not None:
                raise ValueError("authority_payload 不得进入 Agent graph 解码")
            chunks = [RankedChunk(**chunk) for chunk in item["chunks"]]
            error_code = item["error_code"]
            observation = ToolObservation(
                item["call_id"], item["tool_name"], item["success"],
                None if error_code is None else AgentErrorCode(error_code), item["error_message"], chunks,
            )
            messages.append(format_tool_observation_message(observation))
    return messages
