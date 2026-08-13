"""M4.1 MCP 结果 codec：只消费受限公开值，绝不接触 ToolExecutionResult。"""

# 导入 Any，构造 JSON 可序列化的成功 payload。
from typing import Any


# 固定成功结果的公开数据形状；M4.1 的 data 永远是空对象。
PUBLIC_RESULT_SCHEMA_VERSION = 1


def build_public_tool_result() -> dict[str, Any]:
    """构造 M4.1 固定成功公开结果：schema_version=1 且 data 为空。"""

    # M4.2 才允许扩展 data 字段；这里写死空对象防止偷渡检索 chunk。
    return {"schema_version": PUBLIC_RESULT_SCHEMA_VERSION, "data": {}}


def build_execution_summary(
    *,
    error_code: str | None,
    attempt_count: int,
) -> dict[str, Any]:
    """构造 codec 可消费的执行摘要；只含错误码与尝试次数。"""

    # 成功时 error_code 必须是 None；失败路径不应进入 codec。
    return {"error_code": error_code, "attempt_count": attempt_count}


def encode_mcp_probe_success(
    validated_public_values: dict[str, Any],
    public_tool_result: dict[str, Any],
    execution_summary: dict[str, Any],
) -> dict[str, Any]:
    """把 mcp_probe 的受限公开值编码成固定 MCP 成功结果。"""

    # codec 只允许读 value_length，不能读原始 value。
    value_length = validated_public_values["value_length"]
    # 成功时 design 要求 ok=true 与 value_length；attempt_count 不进入公开 structuredContent。
    _ = public_tool_result
    _ = execution_summary
    return {
        "isError": False,
        "structuredContent": {"ok": True, "value_length": value_length},
        "content": [{"type": "text", "text": "ok"}],
    }
