"""M4.2 无 LLM key 的 dense MCP 生产装配：只构造检索，不碰聊天密钥。"""

# 导入 os，只读取 RAG_CHROMA_PATH。
import os
# 导入 dataclass，定义窄配置对象。
from dataclasses import dataclass
# 导入 Path，表示本机 Chroma 目录。
from pathlib import Path
# 导入 Callable，声明可 fake 的 settings/strategy provider。
from collections.abc import Callable
# 导入 Any，承载 factory 返回的 server 对象。
from typing import Any

# 导入 ToolRuntime，作为唯一工具执行边界。
from app.agent.tool_runtime import ToolRuntime
# 导入 knowledge_search 绑定与内部 ToolSpec 构造。
from app.mcp.knowledge_search import (
    build_knowledge_search_public_binding,
    build_knowledge_search_tool_spec,
)
# 导入 M4.1 server 装配。
from app.mcp.server import build_mcp_server
# 导入真实 BGE-M3 编码器；生产路径才构造，测试用 fake provider 绕过。
from app.rag.embedding import BgeM3Embedder
# 导入 dense 策略与结果校验。
from app.retrieval_strategies.dense import DenseRetrievalStrategy
from app.retrieval_strategies.types import RankedChunk, validate_ranked_chunks


# 默认 Chroma 路径与聊天侧保持一致。
DEFAULT_RAG_CHROMA_PATH = "data/chroma"
# MCP 生产检索方法固定为 dense，不读取 RETRIEVAL_METHOD 实验开关。
MCP_RETRIEVAL_METHOD = "dense"
# 启动失败时对外唯一允许的固定文案；不得拼接路径/环境/异常正文。
MCP_RETRIEVAL_STARTUP_MESSAGE = "mcp retrieval startup unavailable"
# stdio entrypoint 写到 stderr 的固定标签。
MCP_RETRIEVAL_STARTUP_STDERR = "ERROR app.mcp.startup mcp_retrieval_startup_failed"


class McpRetrievalStartupError(RuntimeError):
    """MCP 检索装配启动失败的固定异常；消息不得包含路径或密钥。"""


@dataclass(frozen=True)
class RetrievalSettings:
    """MCP 生产检索所需的窄配置：只含 Chroma 路径与固定 dense 方法。"""

    # 本机 Chroma 持久化目录。
    rag_chroma_path: Path
    # 固定为 dense；字段存在是为了测试断言，不允许 hybrid/rerank。
    retrieval_method: str = MCP_RETRIEVAL_METHOD


def load_retrieval_settings(
    *,
    environ: dict[str, str] | None = None,
) -> RetrievalSettings:
    """只从 RAG_CHROMA_PATH 读取检索配置，绝不读取 LLM key 或聊天 load_settings。"""

    # 允许测试注入 environ；生产默认读进程环境。
    source = os.environ if environ is None else environ
    raw_path = source.get("RAG_CHROMA_PATH", DEFAULT_RAG_CHROMA_PATH)
    # 空字符串视为非法，由 factory 统一改写为启动失败。
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("RAG_CHROMA_PATH 无效")
    return RetrievalSettings(
        rag_chroma_path=Path(raw_path),
        retrieval_method=MCP_RETRIEVAL_METHOD,
    )


def build_mcp_retrieval_strategy(settings: RetrievalSettings) -> DenseRetrievalStrategy:
    """根据窄配置构造 DenseRetrievalStrategy；不创建 LLM 客户端。"""

    if settings.retrieval_method != MCP_RETRIEVAL_METHOD:
        raise ValueError("MCP 检索方法仅允许 dense")
    # 真实生产会加载 BGE-M3；测试应 fake strategy_provider，避免拉模型。
    encoder = BgeM3Embedder()
    return DenseRetrievalStrategy(encoder, settings.rag_chroma_path)


def build_validated_retrieve(
    strategy: DenseRetrievalStrategy,
) -> Callable[..., list[RankedChunk]]:
    """把 strategy.retrieve 包装为 validated_retrieve：先检索，再按本次 top_k 校验快照。"""

    def validated_retrieve(question: str, *, top_k: int) -> list[RankedChunk]:
        # 直接调用策略；异常交给 ToolRuntime 映射为 business_failure 等稳定码。
        results = strategy.retrieve(question, top_k=top_k)
        # 超额、路径型来源、空文本、错误 rank、重复 identity 一律 fail-closed。
        validate_ranked_chunks(
            results,
            method_name=MCP_RETRIEVAL_METHOD,
            top_k=top_k,
        )
        return results

    return validated_retrieve


def build_mcp_knowledge_search_server(
    settings_loader: Callable[[], RetrievalSettings] | None = None,
    strategy_provider: Callable[[RetrievalSettings], DenseRetrievalStrategy] | None = None,
    *,
    deadline_seconds: float | None = None,
) -> tuple[Any, Any]:
    """可 fake 的生产 factory：成功返回已注册 knowledge_search 的 server/service。

    settings 或 strategy provider 的任意异常都改写为固定 McpRetrievalStartupError，
    不泄漏路径、环境值或原始异常文本。
    """

    load = load_retrieval_settings if settings_loader is None else settings_loader
    provide = (
        build_mcp_retrieval_strategy if strategy_provider is None else strategy_provider
    )
    try:
        settings = load()
        strategy = provide(settings)
        # 必须传包装后的 retrieve callable，不能把 strategy 对象直接塞进 ToolSpec。
        retrieve = build_validated_retrieve(strategy)
        tool_spec = build_knowledge_search_tool_spec(retrieve)
        binding = build_knowledge_search_public_binding()
        runtime = ToolRuntime([tool_spec])
        if deadline_seconds is None:
            return build_mcp_server(runtime, [binding])
        return build_mcp_server(runtime, [binding], deadline_seconds=deadline_seconds)
    except McpRetrievalStartupError:
        # 已经是固定启动异常时直接上抛，避免二次包装。
        raise
    except Exception as error:  # noqa: BLE001 - 启动期所有细节都必须收敛。
        raise McpRetrievalStartupError(MCP_RETRIEVAL_STARTUP_MESSAGE) from error
