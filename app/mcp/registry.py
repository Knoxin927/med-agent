"""M4.1 公开工具绑定与 registry：只暴露显式白名单工具。"""

# 导入 Callable，声明参数映射、投影与 codec 的窄函数类型。
from collections.abc import Callable
# 导入 dataclass，定义不可变 binding 配置。
from dataclasses import dataclass
# 导入 Any，承载 JSON schema 与参数字典。
from typing import Any

# 导入 ToolRuntime 与其规格，启动时校验内部工具是否只读且已登记。
from app.agent.tool_runtime import ToolExecutionResult, ToolRuntime, ToolSpec
# 导入 effect/approval 枚举，拒绝副作用工具进入 MCP 公开面。
from app.agent.types import ApprovalPolicy, ToolEffect
# 导入固定错误结果构造器。
from app.mcp.errors import build_tool_error_result


# 公开参数校验通过后返回的 canonical 值。
ValidatedPublicValues = dict[str, Any]
# 成功路径的受限公开结果。
PublicToolResult = dict[str, Any]
# codec 可消费的执行摘要。
McpExecutionSummary = dict[str, Any]


@dataclass(frozen=True)
class PublicToolBinding:
    """把 MCP 公开名映射到内部 ToolRuntime 工具，并固定投影边界。"""

    # 外部 client 看到的工具名，例如 mcp_probe。
    public_name: str
    # ToolRuntime 注册表中的内部工具名。
    internal_tool_name: str
    # 给 tools/list 使用的 JSON Schema；只描述公开参数。
    input_schema: dict[str, Any]
    # 把 canonical 公开值映射成内部 ToolCall.arguments。
    to_internal_arguments: Callable[[ValidatedPublicValues], dict[str, Any]]
    # 从 canonical 公开值构造 codec 可消费的受限字段。
    public_value_projection: Callable[[ValidatedPublicValues], ValidatedPublicValues]
    # 唯一允许读取 ToolExecutionResult 的可信投影器。
    trusted_success_projector: Callable[[ToolExecutionResult], PublicToolResult]
    # 只接收受限公开值/结果/摘要的 codec。
    result_codec: Callable[
        [ValidatedPublicValues, PublicToolResult, McpExecutionSummary],
        dict[str, Any],
    ]
    # 可选的 binding 级规范化：在通用 schema 校验后、投影与内部映射前执行。
    # 为 None 时保持 M4.1 行为，把 validate_public_arguments 的原值原样下传。
    canonicalize_public_arguments: (
        Callable[[ValidatedPublicValues], ValidatedPublicValues] | None
    ) = None
    # tools/list 给人看的简短说明。
    description: str = ""


class PublicArgumentError(ValueError):
    """公开参数校验或映射失败时使用的稳定异常。"""


def validate_public_arguments(
    binding: PublicToolBinding,
    raw_arguments: Any,
) -> ValidatedPublicValues:
    """严格校验 MCP 公开参数，只返回 canonical 值或抛出 PublicArgumentError。"""

    # 非 object 一律拒绝，避免数组/字符串被误当参数表。
    if not isinstance(raw_arguments, dict):
        raise PublicArgumentError("arguments 必须是 object")
    schema = binding.input_schema
    # 只支持本 feature 需要的 object schema；未知 schema 形态直接失败。
    if schema.get("type") != "object":
        raise PublicArgumentError("input_schema 必须是 object")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise PublicArgumentError("input_schema.properties 无效")
    required = schema.get("required", [])
    if not isinstance(required, list):
        raise PublicArgumentError("input_schema.required 无效")
    # 拒绝未知字段，防止客户端塞 run_id/timeout 等内部控制面。
    unknown = set(raw_arguments) - set(properties)
    if unknown:
        raise PublicArgumentError(f"不允许的字段: {sorted(unknown)}")
    # 缺必填字段时在 mapping 前失败。
    missing = [name for name in required if name not in raw_arguments]
    if missing:
        raise PublicArgumentError(f"缺少必填字段: {missing}")
    # additionalProperties=false 时再保险一次。
    if schema.get("additionalProperties", True) is False and unknown:
        raise PublicArgumentError("不允许额外字段")

    validated: ValidatedPublicValues = {}
    for name, value in raw_arguments.items():
        prop = properties[name]
        expected_type = prop.get("type")
        if expected_type == "string":
            # bool/int 不能冒充 string。
            if not isinstance(value, str):
                raise PublicArgumentError(f"{name} 必须是字符串")
            min_length = prop.get("minLength")
            max_length = prop.get("maxLength")
            if isinstance(min_length, int) and len(value) < min_length:
                raise PublicArgumentError(f"{name} 长度过短")
            if isinstance(max_length, int) and len(value) > max_length:
                raise PublicArgumentError(f"{name} 长度过长")
            validated[name] = value
            continue
        if expected_type == "integer":
            # Python 的 bool 是 int 子类，必须单独拒绝。
            if type(value) is not int:
                raise PublicArgumentError(f"{name} 必须是整数")
            minimum = prop.get("minimum")
            maximum = prop.get("maximum")
            if isinstance(minimum, int) and value < minimum:
                raise PublicArgumentError(f"{name} 小于最小值")
            if isinstance(maximum, int) and value > maximum:
                raise PublicArgumentError(f"{name} 大于最大值")
            validated[name] = value
            continue
        raise PublicArgumentError(f"不支持的公开参数类型: {name}")
    return validated


class PublicToolRegistry:
    """由显式 PublicToolBinding 派生可发现工具，并在启动时做安全门禁。"""

    def __init__(self, runtime: ToolRuntime, bindings: list[PublicToolBinding]) -> None:
        # 保存 runtime，供 get_spec 与后续 execute_with_policy 使用。
        self._runtime = runtime
        # 按公开名索引 binding，保证 O(1) 查找。
        registry: dict[str, PublicToolBinding] = {}
        # 同时记录 internal 名，确保一对一，禁止多个 public 指向同一 internal。
        internal_names: set[str] = set()
        for binding in bindings:
            if binding.public_name in registry:
                raise ValueError(f"重复的公开工具名: {binding.public_name}")
            if binding.internal_tool_name in internal_names:
                raise ValueError(f"重复的内部工具映射: {binding.internal_tool_name}")
            spec = runtime.get_spec(binding.internal_tool_name)
            if spec is None:
                raise ValueError(f"内部工具未登记: {binding.internal_tool_name}")
            # 只允许只读且无需审批的工具进入 MCP 公开面。
            if spec.effect is not ToolEffect.read_only:
                raise ValueError(f"MCP 不能公开副作用工具: {binding.internal_tool_name}")
            if spec.approval_policy is not ApprovalPolicy.none:
                raise ValueError(f"MCP 不能公开需审批工具: {binding.internal_tool_name}")
            registry[binding.public_name] = binding
            internal_names.add(binding.internal_tool_name)
        self._bindings = registry

    def list_bindings(self) -> list[PublicToolBinding]:
        """返回稳定顺序的公开 binding 列表，供 tools/list 使用。"""

        # 按 public_name 排序，避免 dict 插入顺序影响测试断言。
        return [self._bindings[name] for name in sorted(self._bindings)]

    def get_binding(self, public_name: str) -> PublicToolBinding | None:
        """按公开名查找 binding；未命中返回 None，不猜测近似名。"""

        return self._bindings.get(public_name)

    def build_unknown_tool_result(self) -> dict[str, Any]:
        """公开 registry 未命中时直接返回固定 unknown_tool，不进入 runtime。"""

        return build_tool_error_result("unknown_tool")

    def require_internal_spec(self, binding: PublicToolBinding) -> ToolSpec:
        """再次取出内部 ToolSpec；缺失属于配置漂移，应视为内部错误。"""

        spec = self._runtime.get_spec(binding.internal_tool_name)
        if spec is None:
            raise RuntimeError(f"内部工具在运行时消失: {binding.internal_tool_name}")
        return spec
