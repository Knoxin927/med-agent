"""把 search_knowledge 工具适配为 dense 检索快照观察值。"""

# 导入 Callable，声明由调用方注入的可替换检索函数类型。
from collections.abc import Callable
# 导入 Any，构造 OpenAI 工具 schema。
from typing import Any

# 导入检索快照类型。
from app.retrieval_strategies.types import RankedChunk
# 导入 ToolCall、ToolObservation 值对象。
from app.agent.types import ToolCall, ToolObservation
# 导入 ToolSpec 与成功 observation 构造助手。
from app.agent.tool_runtime import ToolSpec, make_success_observation

# 固定本阶段唯一注册的工具名；模型不能发明其他名称。
SEARCH_KNOWLEDGE_TOOL_NAME = "search_knowledge"
# 与 app/main.py 的 ChatRequest 默认值一致的工具内默认召回数。
DEFAULT_SEARCH_TOP_K = 3
# 限制最大召回数，避免模型请求过多 chunk 而放大上下文成本。
MAX_SEARCH_TOP_K = 10


# 校验 search_knowledge 参数；返回 None 表示合法，字符串表示具体原因。
def validate_search_knowledge_arguments(call: ToolCall) -> str | None:
    """严格校验 query 与 top_k，禁止额外字段或宽松类型转换。"""

    # 取出已经解析为字典的参数。
    arguments: dict[str, Any] = call.arguments
    # 只允许 query 与 top_k 两个字段，额外字段视为协议错误。
    unknown_keys = set(arguments) - {"query", "top_k"}
    if unknown_keys:
        # 用稳定顺序返回非法字段，便于测试断言。
        return f"不允许的字段: {sorted(unknown_keys)}"
    # query 必须是非空字符串，避免空问题消耗检索。
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return "query 必须是非空字符串"
    # 读取可选 top_k；缺失时使用与聊天一致的默认值。
    if "top_k" in arguments:
        top_k = arguments["top_k"]
        # bool 在 Python 中是 int 子类，必须单独排除以免冒充 0 或 1。
        if type(top_k) is not int:
            return "top_k 必须是整数"
        # 越界 top_k 在执行 dense 前失败，不宽松裁剪到合法范围。
        if top_k < 1 or top_k > MAX_SEARCH_TOP_K:
            return "top_k 必须在 1..10 范围内"
    # 参数合法，返回 None。
    return None


# 执行 search_knowledge：调用注入的检索函数并把结果封为快照 observation。
def execute_search_knowledge(
    call: ToolCall,
    retrieve: Callable[..., list[RankedChunk]],
) -> ToolObservation:
    """用已校验参数调用 dense，返回携带本轮 RankedChunk 快照的成功 observation。"""

    # 取出已校验的 query，确保非空。
    query = call.arguments["query"]
    # top_k 缺失时使用默认值，保持与聊天一致的召回口径。
    top_k = call.arguments.get("top_k", DEFAULT_SEARCH_TOP_K)
    # 调用注入的检索函数；异常由 ToolRuntime 统一收敛为 tool_execution_error。
    chunks = retrieve(query, top_k=top_k)
    # 把本轮快照封为唯一成功 observation，供回填与最终 sources 同时使用。
    return make_success_observation(call, chunks)


# 构造随上游请求发送的 OpenAI function schema，描述工具用法与参数约束。
def build_search_knowledge_openai_tool() -> dict[str, Any]:
    """返回 search_knowledge 的 OpenAI 工具定义。"""

    # 返回 OpenAI-compatible 的 function 工具定义。
    return {
        "type": "function",
        "function": {
            # 工具名与注册表主键一致。
            "name": SEARCH_KNOWLEDGE_TOOL_NAME,
            # 简短描述，帮助模型理解工具用途。
            "description": "检索本地医疗知识库，返回与问题最相关的非敏感文本块。",
            "parameters": {
                "type": "object",
                # 只允许 query 和 top_k，additionalProperties=False 阻止模型塞入未知字段。
                "properties": {
                    "query": {"type": "string", "description": "要在知识库中检索的问题文本。"},
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_TOP_K,
                        "default": DEFAULT_SEARCH_TOP_K,
                        "description": "返回的文本块数量，范围 1..10。",
                    },
                },
                # query 是必需字段；top_k 可缺省使用默认值。
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


# 把检索函数装配为可注册的 ToolSpec，供 ToolRuntime 管理。
def build_search_knowledge_tool_spec(
    retrieve: Callable[..., list[RankedChunk]],
) -> ToolSpec:
    """返回绑定注入检索函数的 search_knowledge 工具规格。"""

    # 用闭包捕获检索函数，避免执行器直接依赖具体检索策略。
    def executor(call: ToolCall) -> ToolObservation:
        # 委托模块级执行函数，保持可独立测试。
        return execute_search_knowledge(call, retrieve)

    # 组装工具名、OpenAI schema、校验器与执行器为不可变规格。
    return ToolSpec(
        tool_name=SEARCH_KNOWLEDGE_TOOL_NAME,
        openai_tool=build_search_knowledge_openai_tool(),
        validator=validate_search_knowledge_arguments,
        executor=executor,
    )
