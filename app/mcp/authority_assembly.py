"""M4.3 权威检索与 knowledge_search 的 combined MCP 生产装配。"""

# 导入 Callable，允许测试注入 fake provider。
from collections.abc import Callable
# 导入 Any，标注 server 对象。
from typing import Any

# 导入 ToolRuntime。
from app.agent.tool_runtime import ToolRuntime
# 导入权威 fetch 策略与生产 transport/resolver。
from app.mcp.authority_fetch import (
    AuthorityFetchPolicy,
    PinnedAuthorityTransport,
    SystemAuthorityResolver,
)
# 导入 registry。
from app.mcp.authority_registry import (
    AuthoritySourceRegistry,
    build_production_authority_registry,
)
# 导入 authority 工具与 binding。
from app.mcp.authority_search import (
    build_authority_search_public_binding,
    build_search_authority_tool_spec,
)
# 导入 knowledge_search 绑定与内部规格。
from app.mcp.knowledge_search import (
    build_knowledge_search_public_binding,
    build_knowledge_search_tool_spec,
)
# 导入 dense 装配能力与固定启动错误。
from app.mcp.retrieval_assembly import (
    MCP_RETRIEVAL_STARTUP_MESSAGE,
    McpRetrievalStartupError,
    RetrievalSettings,
    build_mcp_retrieval_strategy,
    build_validated_retrieve,
    load_retrieval_settings,
)
# 导入 server 装配。
from app.mcp.server import build_mcp_server
from app.retrieval_strategies.dense import DenseRetrievalStrategy


# 权威启动失败的固定文案；不得拼接路径/URL/异常。
MCP_AUTHORITY_STARTUP_MESSAGE = "mcp authority startup unavailable"
# stdio entrypoint 写到 stderr 的固定标签。
MCP_AUTHORITY_STARTUP_STDERR = "ERROR app.mcp.startup mcp_authority_startup_failed"


class McpAuthorityStartupError(RuntimeError):
    """权威 registry/transport 启动失败的固定异常。"""


def _close_quietly(resource: Any) -> None:
    """尽量关闭已分配资源；关闭失败被吞掉，避免掩盖主错误。"""

    close = getattr(resource, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - 启动失败路径只保证尽力关闭。
            return


def require_verified_authority_registry(registry: AuthoritySourceRegistry) -> None:
    """生产装配前要求至少一条已登记源，且每条均为 verified。

    live 允许只启用单源（当前 WHO）；未登记的 nhc/chinacdc 不阻塞。
    空 registry 或任何未 verified entry 仍 fail-closed。
    """

    source_ids = registry.list_source_ids()
    if not source_ids:
        raise McpAuthorityStartupError(MCP_AUTHORITY_STARTUP_MESSAGE)
    for source_id in source_ids:
        source = registry.get(source_id)
        if source is None or not source.verified:
            raise McpAuthorityStartupError(MCP_AUTHORITY_STARTUP_MESSAGE)



def build_mcp_search_server(
    *,
    retrieval_settings_loader: Callable[[], RetrievalSettings] | None = None,
    retrieval_strategy_provider: Callable[[RetrievalSettings], DenseRetrievalStrategy] | None = None,
    authority_registry_provider: Callable[[], AuthoritySourceRegistry] | None = None,
    authority_transport_provider: Callable[[], Any] | None = None,
    authority_resolver_provider: Callable[[], Any] | None = None,
    deadline_seconds: float | None = None,
) -> tuple[Any, Any]:
    """一次构造 knowledge_search + authority_search 的 server/service。

    retrieval 失败抛既有 McpRetrievalStartupError；
    authority 失败抛 McpAuthorityStartupError，并关闭已创建的 authority 资源。
    """

    load_retrieval = load_retrieval_settings if retrieval_settings_loader is None else retrieval_settings_loader
    provide_strategy = (
        build_mcp_retrieval_strategy
        if retrieval_strategy_provider is None
        else retrieval_strategy_provider
    )
    provide_registry = (
        build_production_authority_registry
        if authority_registry_provider is None
        else authority_registry_provider
    )
    provide_transport = (
        (lambda: PinnedAuthorityTransport())
        if authority_transport_provider is None
        else authority_transport_provider
    )
    provide_resolver = (
        (lambda: SystemAuthorityResolver())
        if authority_resolver_provider is None
        else authority_resolver_provider
    )

    authority_resource: Any | None = None
    try:
        # 1) 先装配 dense 检索；失败保持 M4.2 固定错误。
        try:
            settings = load_retrieval()
            strategy = provide_strategy(settings)
            retrieve = build_validated_retrieve(strategy)
            knowledge_spec = build_knowledge_search_tool_spec(retrieve)
            knowledge_binding = build_knowledge_search_public_binding()
        except McpRetrievalStartupError:
            raise
        except Exception as error:  # noqa: BLE001
            raise McpRetrievalStartupError(MCP_RETRIEVAL_STARTUP_MESSAGE) from error

        # 2) 再装配权威 registry/transport；失败时关闭已分配资源。
        try:
            registry = provide_registry()
            require_verified_authority_registry(registry)
            transport = provide_transport()
            authority_resource = transport
            resolver = provide_resolver()
            fetch_policy = AuthorityFetchPolicy(resolver, transport)
            authority_spec = build_search_authority_tool_spec(registry, fetch_policy)
            authority_binding = build_authority_search_public_binding()
        except McpAuthorityStartupError:
            raise
        except Exception as error:  # noqa: BLE001
            _close_quietly(authority_resource)
            raise McpAuthorityStartupError(MCP_AUTHORITY_STARTUP_MESSAGE) from error

        # 3) 一个 runtime 注册两个内部工具；binding 按 public_name 稳定排序。
        runtime = ToolRuntime([knowledge_spec, authority_spec])
        bindings = [knowledge_binding, authority_binding]
        if deadline_seconds is None:
            return build_mcp_server(runtime, bindings)
        return build_mcp_server(runtime, bindings, deadline_seconds=deadline_seconds)
    except (McpRetrievalStartupError, McpAuthorityStartupError):
        _close_quietly(authority_resource)
        raise
    except Exception as error:  # noqa: BLE001 - 后段装配失败也必须释放 authority 资源。
        _close_quietly(authority_resource)
        raise McpAuthorityStartupError(MCP_AUTHORITY_STARTUP_MESSAGE) from error
