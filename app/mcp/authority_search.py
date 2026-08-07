"""M4.3 authority_search 公开绑定：source_id + query 映射到 search_authority。"""

# 导入 time，计算剩余 deadline 预算。
import time
# 导入 Any，构造 schema 与内部参数。
from typing import Any

# 导入 ToolSpec / ToolExecutionResult 与 observation 工厂。
from app.agent.tool_runtime import (
    ToolExecutionResult,
    ToolSpec,
    make_authority_success_observation,
)
# 导入 ToolCall / ToolObservation。
from app.agent.types import ToolCall, ToolObservation
# 导入公开结果 schema 版本。
from app.mcp.codec import PUBLIC_RESULT_SCHEMA_VERSION
# 导入抓取错误与策略。
from app.mcp.authority_fetch import AuthorityFetchError, AuthorityFetchPolicy
# 导入 registry。
from app.mcp.authority_registry import AuthoritySourceRegistry
# 导入值对象与边界常量。
from app.mcp.authority_types import (
    ALLOWED_AUTHORITY_SOURCE_IDS,
    AUTHORITY_MAX_RESULTS,
    AUTHORITY_QUERY_MAX_LENGTH,
    AUTHORITY_QUERY_MIN_LENGTH,
    AUTHORITY_SNIPPET_MAX_CHARS,
    AUTHORITY_TITLE_MAX_CHARS,
    AuthoritySearchHit,
    AuthoritySearchPayload,
)
# 导入公开参数错误与 binding 类型。
from app.mcp.registry import PublicArgumentError, PublicToolBinding, ValidatedPublicValues


# MCP 对外工具名。
AUTHORITY_SEARCH_PUBLIC_NAME = "authority_search"
# 内部 ToolRuntime 工具名。
SEARCH_AUTHORITY_TOOL_NAME = "search_authority"
# 内部工具默认 timeout：略高于 fetch 5 秒，仍受 server 10 秒 deadline 约束。
SEARCH_AUTHORITY_TIMEOUT_SECONDS = 6.0


def canonicalize_authority_search_arguments(
    values: ValidatedPublicValues,
) -> ValidatedPublicValues:
    """规范化公开参数：source_id 必须在白名单，query strip 后 1..256。"""

    canonical = dict(values)
    source_id = canonical.get("source_id")
    if not isinstance(source_id, str):
        raise PublicArgumentError("source_id 必须是字符串")
    normalized_source = source_id.strip().lower()
    if normalized_source not in ALLOWED_AUTHORITY_SOURCE_IDS:
        raise PublicArgumentError("source_id 不在允许列表")
    query = canonical.get("query")
    if not isinstance(query, str):
        raise PublicArgumentError("query 必须是字符串")
    stripped = query.strip()
    if len(stripped) < AUTHORITY_QUERY_MIN_LENGTH:
        raise PublicArgumentError("query 不能为空")
    if len(stripped) > AUTHORITY_QUERY_MAX_LENGTH:
        raise PublicArgumentError("query 过长")
    canonical["source_id"] = normalized_source
    canonical["query"] = stripped
    return canonical


def _to_internal_arguments(values: ValidatedPublicValues) -> dict[str, Any]:
    """公开值到内部参数的唯一映射位置。"""

    return {
        "source_id": values["source_id"],
        "query": values["query"],
    }


def _project_public_values(_values: ValidatedPublicValues) -> ValidatedPublicValues:
    """codec 公开值固定为空对象，防止 query 泄漏。"""

    return {}


def validate_search_authority_arguments(call: ToolCall) -> str | None:
    """内部 schema 校验：只允许 source_id/query，且 source_id 在白名单。"""

    arguments = call.arguments
    unknown = set(arguments) - {"source_id", "query"}
    if unknown:
        return f"不允许的字段: {sorted(unknown)}"
    source_id = arguments.get("source_id")
    if not isinstance(source_id, str) or source_id not in ALLOWED_AUTHORITY_SOURCE_IDS:
        return "source_id 非法"
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return "query 必须是非空字符串"
    if len(query) > AUTHORITY_QUERY_MAX_LENGTH:
        return "query 过长"
    return None


def execute_search_authority(
    call: ToolCall,
    *,
    registry: AuthoritySourceRegistry,
    fetch_policy: AuthorityFetchPolicy,
    deadline_monotonic: float | None = None,
) -> ToolObservation:
    """执行权威检索：registry -> fetch policy -> extractor -> payload。"""

    source_id = call.arguments["source_id"]
    query = call.arguments["query"]
    source = registry.require(source_id)
    search_request = registry.build_search_request(source, query)
    # 剩余预算：优先受 call 级 deadline 限制，否则用 fetch 默认上限。
    if deadline_monotonic is None:
        budget = 5.0
    else:
        budget = max(0.01, deadline_monotonic - time.monotonic())
    try:
        final_url, html_text = fetch_policy.fetch_html(source, search_request, budget_seconds=budget)
        raw_hits = registry.extract_hits(source, final_url, html_text)
        hits = fetch_policy.revalidate_hits(source, final_url, raw_hits)
    except AuthorityFetchError as error:
        # 把稳定 code 重新抛出，供 executor 包装层映射；这里转为 RuntimeError 会丢 code。
        # 直接 raise，由外层 adapter 捕获。
        raise error
    payload = AuthoritySearchPayload(hits=tuple(hits))
    return make_authority_success_observation(call, payload)


def build_search_authority_tool_spec(
    registry: AuthoritySourceRegistry,
    fetch_policy: AuthorityFetchPolicy,
) -> ToolSpec:
    """装配内部 search_authority ToolSpec。"""

    def executor(call: ToolCall) -> ToolObservation:
        try:
            return execute_search_authority(
                call,
                registry=registry,
                fetch_policy=fetch_policy,
            )
        except AuthorityFetchError as error:
            # transient 可声明为可重试；timeout 不走 TimeoutToolError（那会变成 transient_failure）。
            # wall-clock timeout 由 ToolRuntime deadline/tool timeout 覆盖；这里的 timeout code
            # 表示 fetch policy 主动判定超时，映射为 business_failure 以外的稳定失败：
            # 用 TransientToolError 仅用于 transient；timeout 与 business 都抛 RuntimeError，
            # 由 runtime 收敛为 business_failure。真实 deadline timeout 仍由慢 transport 测试覆盖。
            if error.code == "transient_failure":
                from app.agent.tool_runtime import TransientToolError

                raise TransientToolError("authority fetch transient") from error
            raise RuntimeError("authority fetch failed") from error
        except Exception as error:  # noqa: BLE001 - 提取/registry 异常也必须稳定收敛。
            raise RuntimeError("authority search failed") from error

    return ToolSpec(
        tool_name=SEARCH_AUTHORITY_TOOL_NAME,
        openai_tool={
            "type": "function",
            "function": {
                "name": SEARCH_AUTHORITY_TOOL_NAME,
                "description": "在允许的权威来源站内搜索公开页面。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "query": {"type": "string"},
                    },
                    "required": ["source_id", "query"],
                    "additionalProperties": False,
                },
            },
        },
        validator=validate_search_authority_arguments,
        executor=executor,
        timeout_seconds=SEARCH_AUTHORITY_TIMEOUT_SECONDS,
        max_attempts=1,
    )


def _truncate(text: str, limit: int) -> str:
    """按 Unicode 字符截断。"""

    if len(text) <= limit:
        return text
    return text[:limit]


def project_authority_search_results(hits: list[AuthoritySearchHit]) -> dict[str, Any]:
    """把命中投影为公开 results；最多 5 条，字段白名单。"""

    results: list[dict[str, Any]] = []
    for hit in hits[:AUTHORITY_MAX_RESULTS]:
        results.append(
            {
                "source_id": hit.source_id,
                "source_name": hit.source_name,
                "title": _truncate(hit.title, AUTHORITY_TITLE_MAX_CHARS),
                "url": hit.url,
                "snippet": _truncate(hit.snippet, AUTHORITY_SNIPPET_MAX_CHARS),
            }
        )
    return {
        "schema_version": PUBLIC_RESULT_SCHEMA_VERSION,
        "data": {"results": results},
    }


def project_authority_search_success(result: ToolExecutionResult) -> dict[str, Any]:
    """可信成功投影：只读 authority_payload.hits。"""

    if not result.observation.success:
        raise RuntimeError("失败结果不得进入 trusted_success_projector")
    payload = result.observation.authority_payload
    if payload is None:
        raise RuntimeError("authority 成功结果缺少 payload")
    hits = list(payload.hits)
    return project_authority_search_results(hits)


def encode_authority_search_success(
    validated_public_values: dict[str, Any],
    public_tool_result: dict[str, Any],
    execution_summary: dict[str, Any],
) -> dict[str, Any]:
    """编码固定 MCP 成功 payload；不读取 query。"""

    _ = validated_public_values
    _ = execution_summary
    result_count = len(public_tool_result.get("data", {}).get("results", []))
    return {
        "isError": False,
        "structuredContent": public_tool_result,
        "content": [{"type": "text", "text": f"ok:{result_count}"}],
    }


def build_authority_search_input_schema() -> dict[str, Any]:
    """构造 tools/list 公开 schema：只有 source_id 与 query。"""

    return {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "description": "权威来源 ID：who、nhc 或 chinacdc。",
            },
            "query": {
                "type": "string",
                "description": "要在该来源站内搜索的关键词。",
            },
        },
        "required": ["source_id", "query"],
        "additionalProperties": False,
    }


def build_authority_search_public_binding() -> PublicToolBinding:
    """构造 authority_search 的显式公开绑定。"""

    return PublicToolBinding(
        public_name=AUTHORITY_SEARCH_PUBLIC_NAME,
        internal_tool_name=SEARCH_AUTHORITY_TOOL_NAME,
        description="在 WHO、国家卫健委或中国疾控中心的公开站内搜索入口检索。",
        input_schema=build_authority_search_input_schema(),
        to_internal_arguments=_to_internal_arguments,
        public_value_projection=_project_public_values,
        trusted_success_projector=project_authority_search_success,
        result_codec=encode_authority_search_success,
        canonicalize_public_arguments=canonicalize_authority_search_arguments,
    )
