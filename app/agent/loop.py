"""M3.1 有界两决策工具调用循环：模型决策 -> 校验执行 -> 回填 -> 最终回答。"""

# 导入 Any，标注模型消息字典类型。
from typing import Any

# 导入消息 codec，按序构造本轮 model.decide 的输入。
from app.agent.messages import (
    build_initial_agent_messages,
    format_assistant_tool_call_message,
    format_tool_observation_message,
)
# 导入模型端口与协议异常，捕获并复用稳定错误码。
from app.agent.model_client import AgentModelClient, AgentModelError
# 导入 ToolRuntime，作为唯一执行边界。
from app.agent.tool_runtime import ToolRuntime
# 导入错误码、循环结果与决策值对象。
from app.agent.types import (
    AgentErrorCode,
    AgentLoopResult,
    FinalAnswerDecision,
    ToolCallDecision,
)


# 用一个绑定模型与 runtime 的对象表达 M3.1 唯一编排，避免散落自由函数。
class BoundedToolCallingLoop:
    """运行至多两次决策的受控循环；不引入 LangGraph、审批或副作用。"""

    # 保存模型端口与执行边界，循环本身不持有任何可变运行态。
    def __init__(self, model: AgentModelClient, runtime: ToolRuntime) -> None:
        # 保存实现 AgentModelClient 的模型实例（真实适配器或确定性 fake）。
        self._model = model
        # 保存持有 search_knowledge 注册表的运行时。
        self._runtime = runtime

    # 运行一次问答：直接回答或一次工具调用后再回答，否则 fail-closed。
    def run(self, question: str) -> AgentLoopResult:
        """根据问题返回完整结果对象；协议违反时以 AgentModelError 失败闭合。"""

        # 空白问题没有可决策的语义，在进入模型前即失败。
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question 必须是非空字符串")
        # 构造 [system, user] 初始消息作为第一次决策输入。
        messages: list[dict[str, Any]] = build_initial_agent_messages(question)
        # 第一次决策：要么直接最终回答，要么提议一次工具调用。
        first_decision = self._model.decide(messages)
        # 直接回答形状：不调用 ToolRuntime，不产生任何 sources。
        if isinstance(first_decision, FinalAnswerDecision):
            return AgentLoopResult(first_decision.answer, [])
        if not isinstance(first_decision, ToolCallDecision):
            raise AgentModelError(AgentErrorCode.model_protocol_error, "模型返回了未知决策")
        # 从此处起第一次决策只能是 ToolCallDecision；对应唯一一次工具执行。
        tool_call = first_decision.tool_call
        # 由 ToolRuntime 把提议转为 observation；成功或失败都作为下一步可回填结果。
        observation = self._runtime.execute(tool_call)
        # 按序追加模型本轮提议与对应 observation，构造第二次决策的输入。
        messages.append(format_assistant_tool_call_message(tool_call))
        messages.append(format_tool_observation_message(observation))
        # 第二次决策必须给出最终回答；再次提议工具调用违反单 call 协议。
        second_decision = self._model.decide(messages)
        # 第二轮仍提议工具调用时不执行额外工具，直接 fail-closed。
        if isinstance(second_decision, ToolCallDecision):
            raise AgentModelError(AgentErrorCode.model_protocol_error, "第二轮仍提议工具调用")
        if not isinstance(second_decision, FinalAnswerDecision):
            raise AgentModelError(AgentErrorCode.model_protocol_error, "模型返回了未知决策")
        # 命中 FinalAnswerDecision 形状；sources 只来自成功 search_knowledge observation。
        sources = list(observation.chunks) if observation.has_chunks else []
        # 返回完整结果对象；M3.1 尚不发布 Agent SSE，交由 M3.6 适配。
        return AgentLoopResult(second_decision.answer, sources)
