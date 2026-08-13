"""M4.3 combined MCP stdio 入口：同时暴露 knowledge_search 与 authority_search。"""

# 导入 asyncio / sys。
import asyncio
import os
import sys
from collections.abc import Callable
from typing import Any

# 导入 combined factory 与两类固定启动错误。
from app.mcp.authority_assembly import (
    MCP_AUTHORITY_STARTUP_STDERR,
    McpAuthorityStartupError,
    build_mcp_search_server,
)
from app.mcp.retrieval_assembly import (
    MCP_RETRIEVAL_STARTUP_STDERR,
    McpRetrievalStartupError,
)
from app.mcp.server import configure_stderr_logging, run_connected_stdio


def main(build_server: Callable[[], tuple[Any, Any]] | None = None) -> int:
    """启动 combined MCP server；启动失败 stdout 为空、stderr 固定标签、退出码 1。"""

    configure_stderr_logging()
    factory = build_mcp_search_server if build_server is None else build_server
    try:
        server, service = factory()
    except McpRetrievalStartupError:
        print(MCP_RETRIEVAL_STARTUP_STDERR, file=sys.stderr)
        return 1
    except McpAuthorityStartupError:
        print(MCP_AUTHORITY_STARTUP_STDERR, file=sys.stderr)
        return 1

    asyncio.run(run_connected_stdio(server, service))
    # 真实 BGE/torch 会留下非 daemon 后台线程，阻止解释器在 stdio EOF 后正常退出；
    # 协议生命周期已结束后强制退出，确保 MCP smoke wrapper 能读到 exit_code=0。
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
