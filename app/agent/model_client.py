"""把 OpenAI-compatible Tool Calling 响应映射为项目 AgentDecision，与文字 token 流隔离。"""

# 导入 json，把上游 tool_calls 中的 arguments JSON 字符串解析为字典。
import json
# 导入 Protocol，声明循环依赖的窄模型端口。
from typing import Any, Protocol

# 导入 httpx，使用同步客户端发送单次结构化决策请求。
import httpx

# 导入项目决策值对象与稳定错误码。
from app.agent.types import AgentDecision, AgentErrorCode, FinalAnswerDecision, ToolCall, ToolCallDecision


# 表示模型违反 Tool Calling 协议；循环捕获后 fail-closed 为脱敏的 model_protocol_error。
class AgentModelError(RuntimeError):
    """模型本轮决策不符合单 call 协议时携带稳定错误码抛出。"""

    # 保存稳定错误码，供调用方在不读取异常文本的情况下分类终态。
    def __init__(self, code: AgentErrorCode, message: str) -> None:
        # 保存错误码，方便测试与未来 SSE 编码引用枚举而非字符串匹配。
        self.code = code
        # 把不含密钥的稳定说明交给基类，避免泄露模型私有字段。
        super().__init__(message)


# 声明循环唯一依赖的窄模型端口；生产适配器与确定性 fake 都实现同一接口。
class AgentModelClient(Protocol):
    """把当前消息上下文转换为至多一个决策。"""

    # 接收本机循环构造的消息，返回 FinalAnswerDecision 或 ToolCallDecision。
    def decide(self, messages: list[dict[str, Any]]) -> AgentDecision:
        """依据消息上下文给出本轮唯一决策。"""


# 把单个供应商 tool_call 项校验并转换为项目的 ToolCallDecision；协议不符则失败闭合。
def _parse_single_tool_call(tool_call: dict[str, Any]) -> ToolCallDecision:
    """校验 id、名称与 arguments，返回一个 ToolCallDecision 或抛出 AgentModelError。"""

    # tool_calls 列表中的每一项必须是对象，避免 malformed provider payload 泄漏 AttributeError。
    if not isinstance(tool_call, dict):
        raise AgentModelError(AgentErrorCode.model_protocol_error, "工具调用结构不正确")
    # call_id 必须是非空字符串，缺失或空都无法把 observation 配对回本次调用。
    raw_id = tool_call.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise AgentModelError(AgentErrorCode.model_protocol_error, "工具调用缺少有效 call_id")
    # 单个 tool_call 的 function 元素必须存在且是对象。
    function = tool_call.get("function")
    if not isinstance(function, dict):
        raise AgentModelError(AgentErrorCode.model_protocol_error, "工具调用缺少 function 字段")
    # 工具名必须是非空字符串，模型不能发明为空或缺失的工具名。
    raw_name = function.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise AgentModelError(AgentErrorCode.model_protocol_error, "工具调用缺少有效工具名")
    # arguments 在 OpenAI-compatible 协议中是 JSON 字符串，先按字符串读取。
    raw_arguments = function.get("arguments")
    if not isinstance(raw_arguments, str):
        raise AgentModelError(AgentErrorCode.model_protocol_error, "工具调用 arguments 必须是 JSON 字符串")
    # 把 JSON 字符串解析为对象；损坏 JSON 属于协议错误，不尝试宽容修复。
    try:
        parsed_arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as error:
        raise AgentModelError(AgentErrorCode.model_protocol_error, "工具调用 arguments 不是合法 JSON") from error
    # 解析结果必须是对象，列表或标量无法作为工具参数。
    if not isinstance(parsed_arguments, dict):
        raise AgentModelError(AgentErrorCode.model_protocol_error, "工具调用 arguments 必须是 JSON 对象")
    # 返回已经校验、可直接进入 ToolRuntime 的 tool call 决策。
    return ToolCallDecision(ToolCall(raw_id, raw_name, parsed_arguments))


# 把上游 chat/completions 的非流式响应解析为项目决策；纯函数可离线穷举测试。
def parse_openai_tool_calling_response(payload: dict[str, Any]) -> AgentDecision:
    """只接受每轮恰好一个决策：一个非空最终回答或一个可配对的 tool call。"""

    # 顶层响应必须是对象；合法 JSON 的数组/标量也属于供应商协议错误。
    if not isinstance(payload, dict):
        raise AgentModelError(AgentErrorCode.model_protocol_error, "上游响应顶层结构不正确")
    # choices 必须是包含至少一个对象的列表。
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AgentModelError(AgentErrorCode.model_protocol_error, "上游响应缺少 choices")
    # M3.1 只读取第一条 choice，与既有文字流适配器保持一致。
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise AgentModelError(AgentErrorCode.model_protocol_error, "上游响应 choice 结构不正确")
    # 非流式响应把本轮内容放在 message 中，而不是 delta。
    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise AgentModelError(AgentErrorCode.model_protocol_error, "上游响应缺少 message")
    # 读取可能的正文；None 或空字符串都视作没有正文。
    raw_content = message.get("content")
    has_content = isinstance(raw_content, str) and raw_content.strip()
    # 读取可能的 tool_calls；只有非空列表才算提出工具调用。
    raw_tool_calls = message.get("tool_calls")
    has_tool_calls = isinstance(raw_tool_calls, list) and len(raw_tool_calls) > 0
    # 正文与工具调用混合违反“每轮恰好一个决策”，不允许拆分消费。
    if has_content and has_tool_calls:
        raise AgentModelError(AgentErrorCode.model_protocol_error, "不能同时给出正文与工具调用")
    # 只有正文时视作直接最终回答。
    if has_content:
        return FinalAnswerDecision(raw_content)
    # 只有 tool_calls 时必须恰好一个；多个在 M3.1 不被支持。
    if has_tool_calls:
        if len(raw_tool_calls) != 1:
            raise AgentModelError(AgentErrorCode.model_protocol_error, "每轮只允许一个工具调用")
        # 校验并转换唯一一个 tool_call。
        return _parse_single_tool_call(raw_tool_calls[0])
    # 既没有正文也没有工具调用，模型没有给出可执行的一步。
    raise AgentModelError(AgentErrorCode.model_protocol_error, "模型没有给出正文或工具调用")


# 生产结构化适配器：用同步 httpx 发送单次非流式 chat/completions + tools 请求。
class OpenAiToolCallingClient:
    """把 OpenAI-compatible Tool Calling 响应适配为项目决策，不混入 token 流逻辑。"""

    # 保存模型配置与上游工具定义，构造一次可在循环中复用的客户端。
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: float,
        tools_schema: list[dict[str, Any]],
    ) -> None:
        # 保存模型名，写入每次上游请求体。
        self._model = model
        # 保存运行时允许的工具定义，随每次请求发送给上游。
        self._tools_schema = tools_schema
        # 创建只在内存保存 Authorization 头的同步客户端；trust_env=False 避免本机被环境代理劫持。
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            trust_env=False,
        )

    # 实现 AgentModelClient 端口：返回本轮唯一决策，协议错误直接以 AgentModelError 抛出。
    def decide(self, messages: list[dict[str, Any]]) -> AgentDecision:
        """发送带 tools 的非流式请求，并把响应解析为项目决策。"""

        # 回填 observation 后禁止模型再次调用工具；首轮同时关闭并行调用。
        has_observation = any(message.get("role") == "tool" for message in messages)
        tool_choice = "none" if has_observation else "auto"
        # 向固定 OpenAI-compatible 路径发送请求；stream 关闭以一次性获得结构化 message。
        try:
            response = self._client.post(
                "/chat/completions",
                json={
                    "model": self._model,
                    "messages": messages,
                    "tools": self._tools_schema,
                    # 首轮允许模型选择工具，回填轮强制只给最终回答。
                    "tool_choice": tool_choice,
                    # 明确关闭供应商的并行工具调用，守住 M3.1 单 call 契约。
                    "parallel_tool_calls": False,
                    "stream": False,
                },
            )
        except httpx.HTTPError as error:
            raise AgentModelError(AgentErrorCode.model_protocol_error, "上游模型服务不可用") from error
        # 非 2xx 视作上游不可用，不伪装成正常决策。
        if response.status_code < 200 or response.status_code >= 300:
            raise AgentModelError(AgentErrorCode.model_protocol_error, "上游模型服务不可用")
        # 只接受 JSON 响应，避免把 HTML 或错误文本当作工具调用结果。
        if "application/json" not in response.headers.get("content-type", ""):
            raise AgentModelError(AgentErrorCode.model_protocol_error, "上游响应不是 JSON")
        # 解析响应体；非法 JSON 在这里也转为稳定的协议错误。
        try:
            payload = response.json()
        except Exception as error:  # noqa: BLE001 - 上游 JSON 变体很多，统一收敛为协议错误。
            raise AgentModelError(AgentErrorCode.model_protocol_error, "上游响应 JSON 无法解析") from error
        # 把解析后的响应交给纯函数转换为项目决策。
        return parse_openai_tool_calling_response(payload)

    # 释放同步连接池，供应用关闭或测试清理调用。
    def aclose(self) -> None:
        # 同步客户端用 close 而非 aclose；保留同名方法以匹配项目既有清理惯例。
        self._client.close()
