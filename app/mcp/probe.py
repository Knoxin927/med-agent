"""M4.1 仅测试使用的 mcp_probe 工具：验证协议壳，不进入生产装配。"""

# 导入 Any，构造 OpenAI 工具 schema 与参数。
from typing import Any

# 导入 ToolCall / ToolObservation 值对象。
from app.agent.types import ToolCall, ToolObservation
# 导入 ToolSpec、执行结果与成功 observation 构造助手。
from app.agent.tool_runtime import ToolExecutionResult, ToolSpec, make_success_observation
# 导入公开绑定与 codec。
from app.mcp.codec import build_public_tool_result, encode_mcp_probe_success
from app.mcp.registry import PublicToolBinding, ValidatedPublicValues


# 固定 test-only 工具名；生产装配不得注册它。
MCP_PROBE_TOOL_NAME = "mcp_probe"
# 公开名与内部名在 M4.1 保持同形 identity 映射。
MCP_PROBE_PUBLIC_NAME = "mcp_probe"
# value 最短 1 个字符，拒绝空串。
MCP_PROBE_MIN_LENGTH = 1
# value 最长 128 个字符，防止超大输入进入 runtime。
MCP_PROBE_MAX_LENGTH = 128


def validate_mcp_probe_arguments(call: ToolCall) -> str | None:
    """严格校验内部 value 参数；返回 None 表示合法。"""

    arguments = call.arguments
    # 只允许 value 一个字段。
    unknown = set(arguments) - {"value"}
    if unknown:
        return f"不允许的字段: {sorted(unknown)}"
    value = arguments.get("value")
    if not isinstance(value, str):
        return "value 必须是字符串"
    if len(value) < MCP_PROBE_MIN_LENGTH or len(value) > MCP_PROBE_MAX_LENGTH:
        return "value 长度必须在 1..128"
    return None


def execute_mcp_probe(call: ToolCall) -> ToolObservation:
    """执行 probe：只证明 runtime 调用链通，不返回敏感正文。"""

    # 成功 observation 不携带 chunks，避免 MCP 层误把检索快照当公开结果。
    return make_success_observation(call, [])


def build_mcp_probe_openai_tool() -> dict[str, Any]:
    """返回 mcp_probe 的 OpenAI function schema，供 ToolRuntime 注册。"""

    return {
        "type": "function",
        "function": {
            "name": MCP_PROBE_TOOL_NAME,
            "description": "仅测试用的只读探测工具，验证 MCP 到 ToolRuntime 的调用链。",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "minLength": MCP_PROBE_MIN_LENGTH,
                        "maxLength": MCP_PROBE_MAX_LENGTH,
                        "description": "非敏感探测文本，仅用于计算长度。",
                    }
                },
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }


def build_mcp_probe_tool_spec() -> ToolSpec:
    """装配可注册到 ToolRuntime 的 test-only 工具规格。"""

    return ToolSpec(
        tool_name=MCP_PROBE_TOOL_NAME,
        openai_tool=build_mcp_probe_openai_tool(),
        validator=validate_mcp_probe_arguments,
        executor=execute_mcp_probe,
    )


def _identity_to_internal_arguments(values: ValidatedPublicValues) -> dict[str, Any]:
    """M4.1 只允许同形 identity 映射：公开 value 原样进入内部 arguments。"""

    return {"value": values["value"]}


def _project_public_values(values: ValidatedPublicValues) -> ValidatedPublicValues:
    """构造 codec 可消费的受限公开值：只保留 value_length。"""

    # 故意丢掉原始 value，防止 sk- 样式字符串进入成功 payload 或日志。
    return {"value_length": len(values["value"])}


def _project_success_result(result: ToolExecutionResult) -> dict[str, Any]:
    """可信成功投影：M4.1 固定返回空 data，不读取 observation 正文。"""

    # 只确认 runtime 成功；失败路径不会进入本函数。
    if not result.observation.success:
        raise RuntimeError("失败结果不得进入 trusted_success_projector")
    return build_public_tool_result()


def build_mcp_probe_public_binding() -> PublicToolBinding:
    """构造 mcp_probe 的显式公开绑定。"""

    return PublicToolBinding(
        public_name=MCP_PROBE_PUBLIC_NAME,
        internal_tool_name=MCP_PROBE_TOOL_NAME,
        description="仅测试用的只读探测工具。",
        input_schema={
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "minLength": MCP_PROBE_MIN_LENGTH,
                    "maxLength": MCP_PROBE_MAX_LENGTH,
                    "description": "非敏感探测文本，仅用于计算长度。",
                }
            },
            "required": ["value"],
            "additionalProperties": False,
        },
        to_internal_arguments=_identity_to_internal_arguments,
        public_value_projection=_project_public_values,
        trusted_success_projector=_project_success_result,
        result_codec=encode_mcp_probe_success,
    )
