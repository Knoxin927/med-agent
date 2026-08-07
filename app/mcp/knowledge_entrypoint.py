"""M4.4 knowledge-only MCP stdio 入口：只装配 knowledge_search，不声明 authority。"""

# 导入 asyncio / sys。
import asyncio
import sys
from collections.abc import Callable
from typing import Any

# 导入 knowledge-only factory 与固定启动错误。
from app.mcp.retrieval_assembly import (
    MCP_RETRIEVAL_STARTUP_STDERR,
    McpRetrievalStartupError,
    build_mcp_knowledge_search_server,
)
from app.mcp.server import configure_stderr_logging, run_connected_stdio


def main(build_server: Callable[[], tuple[Any, Any]] | None = None) -> int:
    """启动 knowledge-only MCP server；启动失败 stdout 为空、stderr 固定标签、退出码 1。"""

    configure_stderr_logging()
    factory = build_mcp_knowledge_search_server if build_server is None else build_server
    try:
        server, service = factory()
    except McpRetrievalStartupError:
        print(MCP_RETRIEVAL_STARTUP_STDERR, file=sys.stderr)
        return 1

    asyncio.run(run_connected_stdio(server, service))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
