"""M5.3 可观测性值对象与冻结常量。"""

# 导入 dataclass，保证事件在写入后不可被就地改写。
from dataclasses import dataclass


class RequestKind:
    """跨协议请求类型；只允许这三类，避免 label 漂移。"""

    # 固定 dense /chat/stream。
    chat_stream = "chat_stream"
    # Agent API / SSE。
    agent_sse = "agent_sse"
    # MCP knowledge-only stdio。
    mcp_stdio = "mcp_stdio"


class EventStatus:
    """事件最终状态。"""

    # 成功完成。
    ok = "ok"
    # 业务/协议失败。
    error = "error"
    # 超时。
    timeout = "timeout"


# 设计冻结的日志 allowlist。
LOG_EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "run_id",
    "call_id",
    "request_kind",
    "tool_name",
    "status",
    "error_code",
    "duration_ms",
    "step_count",
    "model_id",
    "tool_version",
    "corpus_version",
    "sampled_at",
}

# 指标 label 白名单；禁止 query/用户输入。
METRIC_LABEL_KEYS = {
    "request_kind",
    "tool_name",
    "status",
    "error_code",
    "model_id",
    "tool_version",
}

# 每个 label 的 cardinality 上限。
METRIC_LABEL_CARDINALITY_LIMIT = 32

# 当前 local-only 证据允许的受控值；生产装配须经 owner 另行批准后扩展。
OBSERVABILITY_FIELD_VALUE_ALLOWLIST = {
    "tool_name": {"none", "search_knowledge", "knowledge_search"},
    "model_id": {
        "not_available",
        "synthetic-agent-model",
        "synthetic-dense-model",
        "synthetic-dense-model-v2",
    },
    "tool_version": {"not_available", "agent-tools-v1", "mcp-knowledge-v1"},
    "corpus_version": {"synthetic-corpus-v1"},
    "error_code": {"none", "tool_timeout", "mcp_timeout"},
}

CORRELATION_ID_PREFIXES = {"event_id": "evt-", "run_id": "run-", "call_id": "call-"}

# 请求类型冻结集合。
REQUEST_KINDS = {
    RequestKind.chat_stream,
    RequestKind.agent_sse,
    RequestKind.mcp_stdio,
}

# 状态冻结集合。
EVENT_STATUSES = {
    EventStatus.ok,
    EventStatus.error,
    EventStatus.timeout,
}


@dataclass(frozen=True)
class ObservabilityManifest:
    """可观测性运行前必须冻结的配置。"""

    # 机器可读 schema 版本。
    schema_version: int
    # synthetic=工程验证；production=真实装配候选。
    run_mode: str
    # 人读 manifest 版本。
    manifest_version: str
    # 本批证据批次。
    batch_id: str
    # 事件 schema 版本。
    event_schema_version: str
    # 采样率；synthetic 可用 1.0，生产需 owner 批准。
    sample_rate: float
    # 本地保留天数；当前只记录策略，不自动删除生产日志。
    retention_days: int
    # 指标 label cardinality 上限。
    label_cardinality_limit: int
    # owner 是否确认生产装配。
    owner_confirmed: bool
    # owner 授权引用。
    owner_confirmation_ref: str
    # 本地事件文件相对路径。
    events_path: str
    # 事件文件内容 hash。
    events_sha256: str


@dataclass(frozen=True)
class ObservabilityEvent:
    """单条脱敏结构化事件。"""

    schema_version: int
    event_id: str
    run_id: str
    call_id: str
    request_kind: str
    tool_name: str
    status: str
    error_code: str | None
    duration_ms: float
    step_count: int
    model_id: str
    tool_version: str
    corpus_version: str
    sampled_at: str
