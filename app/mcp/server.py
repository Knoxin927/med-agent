"""M4.1 MCP Server：stdio 协议适配，所有业务调用必须经 ToolRuntime。"""

# 导入 asyncio，把同步 runtime 调用放到线程，避免阻塞事件循环。
import asyncio
# 导入 logging，固定日志只写 stderr。
import logging
# 导入 sys，在 stdio 入口使用标准输入输出。
import sys
# 导入 time，计算 10 秒 deadline。
import time
# 导入 uuid，生成 server-owned 的 run_id/call_id。
import uuid
# 导入 Event，构造协作取消信号。
from threading import Event, Lock
# 导入 Any，承载 JSON 参数与结果。
from typing import Any

# 导入 MCP low-level Server 与 stdio transport。
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
import mcp.types as types

# 导入 ToolRuntime 与调用值对象。
from app.agent.tool_runtime import ToolRuntime
from app.agent.types import ToolCall, ToolExecutionContext
# 导入 codec 摘要构造器。
from app.mcp.codec import build_execution_summary
# 导入固定错误投影。
from app.mcp.errors import build_tool_error_result, map_runtime_error_code
# 导入公开 registry 与参数校验。
from app.mcp.registry import (
    PublicArgumentError,
    PublicToolBinding,
    PublicToolRegistry,
    validate_public_arguments,
)


# 模块日志器：handler 由入口配置到 stderr，绝不写 stdout。
logger = logging.getLogger("app.mcp.server")
# design 固定：每次 tools/call 的默认 deadline 为 10 秒。
DEFAULT_TOOL_DEADLINE_SECONDS = 10.0
# server 对外声明的稳定名字与版本，便于 client initialize 断言。
MCP_SERVER_NAME = "med-agent-mcp"
MCP_SERVER_VERSION = "0.4.1"


class McpToolCallService:
    """把一次 MCP tools/call 映射为唯一的 execute_with_policy 调用。"""

    def __init__(
        self,
        runtime: ToolRuntime,
        registry: PublicToolRegistry,
        *,
        deadline_seconds: float = DEFAULT_TOOL_DEADLINE_SECONDS,
    ) -> None:
        # runtime 是唯一执行入口；本类不直接调用工具 callable。
        self._runtime = runtime
        # registry 决定哪些公开工具可被发现和调用。
        self._registry = registry
        # 保存默认 deadline，便于测试注入更短超时。
        self._deadline_seconds = deadline_seconds
        # 活动取消信号表：EOF 时统一置位。
        self._active_signals: dict[str, Event] = {}
        # accepting 状态与活动信号必须由同一把锁保护，避免 EOF 与 late registration 竞态。
        self._lifecycle_lock = Lock()
        # 关闭后拒绝新请求。
        self._accepting = True

    def stop_accepting(self) -> None:
        """stdin EOF 或关闭时调用：停止接收新请求。"""

        with self._lifecycle_lock:
            self._accepting = False

    def cancel_all_active(self) -> None:
        """置位全部活动取消信号，不承诺强杀同步 worker。"""

        with self._lifecycle_lock:
            signals = list(self._active_signals.values())
        for signal in signals:
            signal.set()

    def close_runtime(self, *, wait: bool = False) -> None:
        """释放 runtime 线程池；默认 wait=False，符合 design 的 close(wait=False)。"""

        self._runtime.close(wait=wait)

    def call_tool(self, public_name: str, raw_arguments: dict[str, Any] | None) -> dict[str, Any]:
        """同步执行一次公开工具调用，始终返回固定成功/失败 payload。"""

        # 关闭后不再接受新请求，直接返回 cancelled。
        with self._lifecycle_lock:
            if not self._accepting:
                return build_tool_error_result("cancelled")

        # 公开 registry 未命中：不进入 runtime；registry provider 异常收敛为内部错误。
        try:
            binding = self._registry.get_binding(public_name)
        except Exception:  # noqa: BLE001 - registry 是 adapter seam，原始异常不得外泄。
            return self._internal_error("public registry lookup failed")
        if binding is None:
            try:
                return self._registry.build_unknown_tool_result()
            except Exception:  # noqa: BLE001 - provider 异常必须固定投影。
                return self._internal_error("unknown tool projection failed")

        # 1) 先做公开参数严格校验，得到 schema 层的 canonical 值。
        try:
            validated = validate_public_arguments(binding, raw_arguments)
        except PublicArgumentError:
            return build_tool_error_result("invalid_arguments")
        except Exception:  # noqa: BLE001 - 参数入口自身异常也必须稳定收敛。
            return self._internal_error("public argument validation failed")

        # 1.5) 可选 binding 级规范化（例如 knowledge_search 的 strip/长度）。
        # 字段为 None 时跳过，保证 M4.1 mcp_probe 行为不变。
        if binding.canonicalize_public_arguments is not None:
            try:
                validated = binding.canonicalize_public_arguments(validated)
            except PublicArgumentError:
                return build_tool_error_result("invalid_arguments")
            except Exception:  # noqa: BLE001 - canonicalizer 非协议异常收敛为内部错误。
                return self._internal_error("public argument canonicalize failed")

        # 2) 构造 codec 可消费的受限公开值；失败则内部错误。
        try:
            public_values = binding.public_value_projection(validated)
        except Exception:  # noqa: BLE001
            return self._internal_error("public value projection failed")

        # 3) identity/自定义映射只消费 canonical 值；映射异常不算 runtime 调用。
        try:
            internal_arguments = binding.to_internal_arguments(validated)
        except Exception:  # noqa: BLE001
            return build_tool_error_result("invalid_arguments")

        # 4) server 自己生成 run_id/call_id，客户端 ID 不参与授权。
        run_id = str(uuid.uuid4())
        call_id = str(uuid.uuid4())
        cancellation_signal = Event()
        with self._lifecycle_lock:
            # projection/mapping 期间可能已经发生 EOF，late call 必须在 runtime 前闭合。
            if not self._accepting:
                return build_tool_error_result("cancelled")
            self._active_signals[call_id] = cancellation_signal
        deadline = time.monotonic() + self._deadline_seconds
        context = ToolExecutionContext(
            run_id=run_id,
            cancellation_signal=cancellation_signal,
            verified_scopes=frozenset(),
            deadline_monotonic=deadline,
        )
        call = ToolCall(
            call_id=call_id,
            tool_name=binding.internal_tool_name,
            arguments=internal_arguments,
        )

        try:
            # 唯一允许的执行入口：execute_with_policy，禁止兼容 execute()。
            result = self._runtime.execute_with_policy(call, context)
        except Exception:  # noqa: BLE001 - runtime 不应抛出，但 adapter 仍要兜底。
            return self._internal_error("runtime execute failed")
        finally:
            with self._lifecycle_lock:
                self._active_signals.pop(call_id, None)

        # runtime 已返回错误码时，按固定表投影，绝不覆盖为 internal_error。
        if result.error_code is not None or not result.observation.success:
            code = map_runtime_error_code(result.error_code)
            return build_tool_error_result(code)

        # 成功路径：可信投影器读取 ToolExecutionResult，codec 永远看不到它。
        try:
            public_result = binding.trusted_success_projector(result)
            summary = build_execution_summary(
                error_code=None,
                attempt_count=result.attempt_count,
            )
            return binding.result_codec(public_values, public_result, summary)
        except Exception:  # noqa: BLE001
            return self._internal_error("success projection or codec failed")

    def _internal_error(self, reason: str) -> dict[str, Any]:
        """adapter 自身失败：写固定脱敏 stderr 日志，并返回 internal_error。"""

        # 日志只写固定 reason 标签，不拼接用户输入或异常正文。
        logger.error("mcp_internal_error reason=%s", reason)
        return build_tool_error_result("internal_error")


def build_mcp_server(
    runtime: ToolRuntime,
    bindings: list[PublicToolBinding],
    *,
    deadline_seconds: float = DEFAULT_TOOL_DEADLINE_SECONDS,
) -> tuple[Server, McpToolCallService]:
    """装配 low-level MCP Server 与调用服务；不自动连接 stdio。"""

    registry = PublicToolRegistry(runtime, bindings)
    service = McpToolCallService(runtime, registry, deadline_seconds=deadline_seconds)
    server: Server = Server(MCP_SERVER_NAME, version=MCP_SERVER_VERSION)

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        """只返回显式 binding 的稳定 schema。"""

        tools: list[types.Tool] = []
        for binding in registry.list_bindings():
            tools.append(
                types.Tool(
                    name=binding.public_name,
                    description=binding.description,
                    inputSchema=binding.input_schema,
                )
            )
        return tools

    @server.call_tool(validate_input=False)
    async def handle_call_tool(
        name: str,
        arguments: dict[str, Any] | None,
    ) -> types.CallToolResult:
        """tools/call 入口：关闭 SDK 输入校验，改由我们的严格公开校验负责。"""

        # 关闭接收后直接返回 cancelled，避免 EOF 后仍接受新业务。
        try:
            payload = await asyncio.to_thread(service.call_tool, name, arguments)
        except Exception:  # noqa: BLE001
            payload = service._internal_error("call handler crashed")
        # 把我们的固定 dict 转成 SDK CallToolResult。
        content_items = []
        for item in payload.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                content_items.append(types.TextContent(type="text", text=str(item.get("text", ""))))
        return types.CallToolResult(
            content=content_items,
            structuredContent=payload.get("structuredContent"),
            isError=bool(payload.get("isError", False)),
        )

    return server, service


async def run_connected_stdio(server: Server, service: McpToolCallService) -> None:
    """用已装配 server/service 跑共享 stdio 生命周期；EOF 后按固定顺序关闭。"""

    init_options = InitializationOptions(
        server_name=MCP_SERVER_NAME,
        server_version=MCP_SERVER_VERSION,
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_options)
    finally:
        # EOF 或异常退出：停止接新请求、置取消、按 design close(wait=False)。
        service.stop_accepting()
        service.cancel_all_active()
        service.close_runtime(wait=False)


async def run_stdio_server(
    runtime: ToolRuntime,
    bindings: list[PublicToolBinding],
    *,
    deadline_seconds: float = DEFAULT_TOOL_DEADLINE_SECONDS,
) -> None:
    """以本机 stdio 运行 MCP server；日志只应配置到 stderr。"""

    server, service = build_mcp_server(
        runtime,
        bindings,
        deadline_seconds=deadline_seconds,
    )
    # 委托共享生命周期，避免两套启动逻辑分叉。
    await run_connected_stdio(server, service)


def configure_stderr_logging() -> None:
    """把 app.mcp 日志固定到 stderr，保证 stdout 只含协议帧。"""

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("app.mcp")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # 禁止向上冒泡到可能写 stdout 的父 handler。
    root.propagate = False
