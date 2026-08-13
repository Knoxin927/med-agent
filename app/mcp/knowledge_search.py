"""M4.2 knowledge_search 公开绑定：把 dense 检索快照投影成受限 MCP 结果。"""

# 导入 Callable，声明由装配层注入的 dense 检索函数类型。
from collections.abc import Callable
# 导入 Any，构造 JSON schema 与公开结果字典。
from typing import Any

# 导入内部 search_knowledge 工具规格与默认 top_k。
from app.agent.search_knowledge_tool import (
    DEFAULT_SEARCH_TOP_K,
    SEARCH_KNOWLEDGE_TOOL_NAME,
    build_search_knowledge_tool_spec,
)
# 导入 ToolExecutionResult，作为可信投影器唯一可读的 runtime 结果。
from app.agent.tool_runtime import ToolExecutionResult, ToolSpec
# 导入公开结果 schema 版本常量。
from app.mcp.codec import PUBLIC_RESULT_SCHEMA_VERSION
# 导入公开参数错误与 binding 类型。
from app.mcp.registry import PublicArgumentError, PublicToolBinding, ValidatedPublicValues
# 导入 RankedChunk，只在可信投影器里读取快照字段。
from app.retrieval_strategies.types import RankedChunk


# MCP 对外工具名；与内部 search_knowledge 显式区分。
KNOWLEDGE_SEARCH_PUBLIC_NAME = "knowledge_search"
# 公开 query 规范化后的最短长度。
KNOWLEDGE_SEARCH_QUERY_MIN_LENGTH = 1
# 公开 query 规范化后的最长长度；公开 JSON schema 不写 maxLength，由 canonicalizer 独家执行。
KNOWLEDGE_SEARCH_QUERY_MAX_LENGTH = 512
# 公开 top_k 上限，比内部 1..10 更窄。
KNOWLEDGE_SEARCH_PUBLIC_MAX_TOP_K = 5
# 公开结果 text 的最大 Unicode 字符数。
KNOWLEDGE_SEARCH_TEXT_MAX_CHARS = 1200


def canonicalize_knowledge_search_arguments(
    values: ValidatedPublicValues,
) -> ValidatedPublicValues:
    """对 knowledge_search 公开参数做 strip 与 query 长度规范化。

    通用 schema 只声明 query 是 string，不声明 minLength/maxLength；
    因此本函数是 strip 后 1..512 的唯一权威。top_k 若存在，已由通用校验保证是 1..5 整数。
    """

    # 复制一份，避免意外改写调用方持有的 dict。
    canonical = dict(values)
    query = canonical.get("query")
    # 防御：若 schema 校验被绕过，这里仍拒绝非字符串。
    if not isinstance(query, str):
        raise PublicArgumentError("query 必须是字符串")
    # strip 掉首尾空白；空白串随后会因长度 0 被拒绝。
    stripped = query.strip()
    if len(stripped) < KNOWLEDGE_SEARCH_QUERY_MIN_LENGTH:
        raise PublicArgumentError("query 不能为空")
    if len(stripped) > KNOWLEDGE_SEARCH_QUERY_MAX_LENGTH:
        raise PublicArgumentError("query 过长")
    canonical["query"] = stripped
    return canonical


def _to_internal_arguments(values: ValidatedPublicValues) -> dict[str, Any]:
    """把 canonical 公开值映射成内部 search_knowledge 参数。

    这是唯一允许读取 query 的 binding 函数；必须总是显式写出 top_k，
    省略时用 DEFAULT_SEARCH_TOP_K=3，而不是依赖 schema default。
    """

    return {
        "query": values["query"],
        "top_k": values.get("top_k", DEFAULT_SEARCH_TOP_K),
    }


def _project_public_values(_values: ValidatedPublicValues) -> ValidatedPublicValues:
    """knowledge_search 的 codec 公开值固定为空对象，防止 query 泄漏进 codec。"""

    return {}


def _truncate_text(text: str) -> str:
    """按 Unicode 字符截断 text，最多保留 1200 个字符。"""

    if len(text) <= KNOWLEDGE_SEARCH_TEXT_MAX_CHARS:
        return text
    return text[:KNOWLEDGE_SEARCH_TEXT_MAX_CHARS]


def project_knowledge_search_results(chunks: list[RankedChunk]) -> dict[str, Any]:
    """把 RankedChunk 快照投影为公开 results；只保留四字段并截断 text。

    调用方必须先完成完整快照校验；本函数不再静默截断条数、不跳过不合规条目，
    也不重排、不改 rank。超额/不合规只能在 validated_retrieve 失败后映射错误。
    """

    results: list[dict[str, Any]] = []
    # 投影层只做字段白名单与 text 截断；条数边界由 validate_ranked_chunks 独占。
    for chunk in chunks:
        results.append(
            {
                "source_name": chunk.source_name,
                "chunk_index": chunk.chunk_index,
                "rank": chunk.rank,
                "text": _truncate_text(chunk.text),
            }
        )
    return {
        "schema_version": PUBLIC_RESULT_SCHEMA_VERSION,
        "data": {"results": results},
    }


def project_knowledge_search_success(result: ToolExecutionResult) -> dict[str, Any]:
    """可信成功投影：只从 observation.chunks 读取四字段白名单。"""

    if not result.observation.success:
        raise RuntimeError("失败结果不得进入 trusted_success_projector")
    return project_knowledge_search_results(list(result.observation.chunks))


def encode_knowledge_search_success(
    validated_public_values: dict[str, Any],
    public_tool_result: dict[str, Any],
    execution_summary: dict[str, Any],
) -> dict[str, Any]:
    """把 knowledge_search 的受限公开结果编码成固定 MCP 成功 payload。

    三个入参中：公开值必须是 {}；成功投影必须是 schema_version/data/results；
    执行摘要只含 error_code/attempt_count。codec 不得读取 query。
    """

    # 显式忽略公开值与摘要内容，防止未来误把 query 放进 codec。
    _ = validated_public_values
    _ = execution_summary
    # 成功 content 只给固定短文本，不回显检索正文全文，避免协议帧过大。
    result_count = len(public_tool_result.get("data", {}).get("results", []))
    return {
        "isError": False,
        "structuredContent": public_tool_result,
        "content": [{"type": "text", "text": f"ok:{result_count}"}],
    }


def build_knowledge_search_input_schema() -> dict[str, Any]:
    """构造 tools/list 使用的公开 JSON Schema。

    query 只声明 type=string，不写 minLength/maxLength；
    top_k 声明 1..5 整数，schema default 不参与通用校验。
    """

    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要在本地知识库中检索的问题文本。",
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": KNOWLEDGE_SEARCH_PUBLIC_MAX_TOP_K,
                "default": DEFAULT_SEARCH_TOP_K,
                "description": "返回的文本块数量，范围 1..5。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }


def build_knowledge_search_tool_spec(
    retrieve: Callable[..., list[RankedChunk]],
) -> ToolSpec:
    """复用 M3 search_knowledge ToolSpec；retrieve 必须是已包装的 validated_retrieve。"""

    return build_search_knowledge_tool_spec(retrieve)


def build_knowledge_search_public_binding() -> PublicToolBinding:
    """构造 knowledge_search 的显式公开绑定。"""

    return PublicToolBinding(
        public_name=KNOWLEDGE_SEARCH_PUBLIC_NAME,
        internal_tool_name=SEARCH_KNOWLEDGE_TOOL_NAME,
        description="检索本地医疗知识库，返回脱敏后的相关文本块。",
        input_schema=build_knowledge_search_input_schema(),
        to_internal_arguments=_to_internal_arguments,
        public_value_projection=_project_public_values,
        trusted_success_projector=project_knowledge_search_success,
        result_codec=encode_knowledge_search_success,
        canonicalize_public_arguments=canonicalize_knowledge_search_arguments,
    )
