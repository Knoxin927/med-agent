"""M5.5 成本统计值对象：只保存脱敏、可重算字段，缺失不写 0。"""

# 导入 dataclass，保证配置与明细在聚合过程中不可被就地改写。
from dataclasses import dataclass, field


class CostRequestKind:
    """成本事件请求类型；对应四种成本边界，禁止混用单位。"""

    # LLM token 成本：按 provider/model/price 绑定单价和 usage 计算。
    llm = "llm"
    # 检索成本：记录 embedding/rerank 调用次数与本地资源计数。
    retrieval = "retrieval"
    # 缓存成本：记录 hit/miss、读写次数和估算节省。
    cache = "cache"
    # 压测成本：记录请求数、错误数和资源计数，不冒充 provider 金额。
    load = "load"


class CostUsageStatus:
    """usage 是否可用。"""

    # usage 已知且经归一。
    known = "known"
    # usage 缺失或不可靠；金额必须传播为 not_available。
    not_available = "not_available"


class CostPricingStatus:
    """单价/价格来源是否可用。"""

    # 价格来源、日期、单位已绑定。
    known = "known"
    # 价格缺失或未授权；金额必须传播为 not_available。
    not_available = "not_available"


# 四类成本边界冻结集合。
COST_REQUEST_KINDS = {
    CostRequestKind.llm,
    CostRequestKind.retrieval,
    CostRequestKind.cache,
    CostRequestKind.load,
}

# usage/价格状态冻结集合。
COST_STATUS_VALUES = {
    CostUsageStatus.known,
    CostUsageStatus.not_available,
    CostPricingStatus.known,
    CostPricingStatus.not_available,
}

# usage 白名单键；按 request_kind 分组，禁止 query/正文/密钥进入 usage。
USAGE_KEYS = {
    # LLM 标准 token 字段。
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    # 检索计数。
    "embed_calls",
    "rerank_calls",
    # 缓存计数。
    "cache_hits",
    "cache_misses",
    "cache_reads",
    "cache_writes",
    "load_requests",
    "load_errors",
}

# 每个 request_kind 允许的 usage 键，避免用错单位（例如 LLM 写入 cache_hits）。
USAGE_KEYS_BY_KIND = {
    CostRequestKind.llm: {"prompt_tokens", "completion_tokens", "total_tokens"},
    CostRequestKind.retrieval: {"embed_calls", "rerank_calls"},
    CostRequestKind.cache: {"cache_hits", "cache_misses", "cache_reads", "cache_writes"},
    CostRequestKind.load: {"load_requests", "load_errors"},
}

# cost detail 顶层允许字段（不含 usage，usage 在下方分组白名单）。
COST_DETAIL_FIELDS = {
    "cost_schema_version",
    "detail_id",
    "batch_id",
    "run_id",
    "request_kind",
    "provider",
    "model",
    "usage_status",
    "pricing_status",
    "price_source_ref",
    "price_as_of",
    "currency",
    "unit_cost",
    "total_cost",
    "usage",
    "sampled_at",
}


@dataclass(frozen=True)
class CostManifest:
    """正式成本统计前必须冻结的 manifest。"""

    # 机器可读 schema 版本。
    schema_version: int
    # 显式运行模式：synthetic=工程验证，production=真实证据候选。
    run_mode: str
    # 人读 manifest 版本。
    manifest_version: str
    # 本批统计批次号。
    batch_id: str
    # cost detail schema 版本。
    cost_schema_version: str
    # 本批覆盖的 request_kind 列表。
    request_kinds: tuple[str, ...]
    # 本批使用的币种；不同币种不得合并金额。
    currency: str
    # 价格来源引用；真实来源需单独 owner approval。
    price_source_ref: str
    # 价格生效日期；用于绑定单价版本。
    price_as_of: str
    # owner 是否确认真实运行。
    owner_confirmed: bool
    # owner 授权引用；未确认时为空字符串。
    owner_confirmation_ref: str
    # detail 文件相对路径。
    detail_path: str
    # detail 文件内容 hash。
    detail_sha256: str


@dataclass(frozen=True)
class CostDetail:
    """单条成本明细；缺失 usage 或价格时金额为 None，表示 not_available。"""

    # cost schema 版本。
    cost_schema_version: int
    # 明细稳定 ID。
    detail_id: str
    # 批次号，与 manifest.batch_id 一致。
    batch_id: str
    # 本 run 的唯一 ID。
    run_id: str
    # llm/retrieval/cache/load。
    request_kind: str
    # provider 标签；本地检索/缓存/load 可写 local 或 not_available。
    provider: str
    # model 标签；LLM 必填，非 LLM 可写 not_available。
    model: str
    # usage 是否已知。
    usage_status: str
    # 价格/单价是否已知。
    pricing_status: str
    # 价格来源引用。
    price_source_ref: str
    # 价格生效日期。
    price_as_of: str
    # 币种。
    currency: str
    # 单价；pricing_status=not_available 时为 None。
    unit_cost: float | None
    # 总金额；usage 或 pricing 不可用时为 None。
    total_cost: float | None
    # usage 计数字典；键受 USAGE_KEYS_BY_KIND 约束。
    usage: dict[str, int] = field(default_factory=dict)
    # 采样时间。
    sampled_at: str = "not_available"


def cost_detail_key(detail_id: str, batch_id: str, run_id: str) -> str:
    """构造明细去重键。"""

    return f"{batch_id}|{run_id}|{detail_id}"
