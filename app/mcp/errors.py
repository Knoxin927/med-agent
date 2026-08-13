"""M4.1 MCP 错误投影：把稳定错误码映射成固定、脱敏的工具失败结果。"""

# 导入 Any，构造 JSON 可序列化的错误 payload。
from typing import Any

# 导入 ToolErrorCode，只映射 runtime 已经稳定分类过的错误。
from app.agent.types import ToolErrorCode


# 固定公开错误表：客户端只能看到这些 code/message，不能看到原始异常或输入。
_PUBLIC_ERROR_MESSAGES: dict[str, str] = {
    "unknown_tool": "不支持的 MCP 工具",
    "invalid_arguments": "MCP 工具参数无效",
    "permission_denied": "MCP 工具不可调用",
    "timeout": "MCP 工具执行超时",
    "transient_failure": "MCP 工具暂时不可用",
    "business_failure": "MCP 工具执行失败",
    "conflict": "MCP 工具调用冲突",
    "cancelled": "MCP 工具调用已取消",
    "internal_error": "MCP 工具调用内部失败",
}


def public_error_message(code: str) -> str:
    """返回固定公开中文消息；未知 code 也收敛到 internal_error 文案。"""

    return _PUBLIC_ERROR_MESSAGES.get(code, _PUBLIC_ERROR_MESSAGES["internal_error"])


def map_runtime_error_code(error_code: ToolErrorCode | None) -> str:
    """把 runtime 错误码映射为公开 code；成功时不应调用本函数。"""

    if error_code is None:
        return "internal_error"
    code = error_code.value
    # runtime 八种稳定码都直接公开；未来未知码 fail-closed 为 business_failure。
    if code in _PUBLIC_ERROR_MESSAGES and code != "internal_error":
        return code
    return "business_failure"


def build_tool_error_result(code: str) -> dict[str, Any]:
    """构造 MCP tools/call 的固定 isError 结果，绝不回显输入或异常正文。"""

    # 若传入未知 code，仍收敛到 internal_error 的固定文案与 code。
    if code not in _PUBLIC_ERROR_MESSAGES:
        code = "internal_error"
    message = public_error_message(code)
    return {
        "isError": True,
        "structuredContent": {"code": code, "message": message},
        "content": [{"type": "text", "text": message}],
    }
