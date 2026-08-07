"""M5.6 工程化收尾值对象：只保存五条线 public 字段与脱敏聚合结果。"""

# 导入 dataclass，保证配置与聚合在过程中不可被就地改写。
from dataclasses import dataclass


class ClosureLine:
    """被汇总的五条工程化线名；固定集合，禁止拼写漂移。"""

    # 回答级质量评测。
    quality = "quality"
    # 负载/压测。
    load = "load"
    # 脱敏可观测性。
    observability = "observability"
    # 版本化缓存策略。
    cache = "cache"
    # 成本统计。
    cost = "cost"


# 五条线冻结集合。
CLOSURE_LINES = {
    ClosureLine.quality,
    ClosureLine.load,
    ClosureLine.observability,
    ClosureLine.cache,
    ClosureLine.cost,
}

# 每条线允许的 evidence_kind，保证读到的 decision 来自正确线。
EVIDENCE_KIND_BY_LINE = {
    ClosureLine.quality: "m5-quality-evaluation",
    ClosureLine.load: "m5-load-performance",
    ClosureLine.observability: "m5-observability",
    ClosureLine.cache: "m5-cache-strategy",
    ClosureLine.cost: "m5-cost-accounting",
}

# 从各线 decision 中提取的公共字段白名单；禁止 query/正文/密钥。
LINE_DECISION_FIELDS = {
    "schema_version",
    "run_mode",
    "decision",
    "evidence_kind",
    "synthetic_only",
    "scan_failed",
    "owner_confirmed",
    "owner_confirmation_ref",
    "run_id",
    "batch_id",
    "manifest_version",
    "reasons",
}

# 各线专属性 production claim：若存在必须为 false，否则 closure 必须 hold。
PRODUCTION_CLAIM_FIELDS = {
    "capacity_claim",        # load
    "production_logging_claim",  # observability
    "hot_path_claim",        # cache
    "default_bypass",        # cache（语义为 bool，true 才合规）
    "production_cost_claim", # cost
    "production_quality_claim",  # quality（若存在）
}

# 每条线仅允许读取自身的 production claim，禁止跨线或任意字段透传。
PRODUCTION_CLAIM_FIELDS_BY_LINE = {
    ClosureLine.quality: {"production_quality_claim"},
    ClosureLine.load: {"capacity_claim"},
    ClosureLine.observability: {"production_logging_claim"},
    ClosureLine.cache: {"hot_path_claim", "default_bypass"},
    ClosureLine.cost: {"production_cost_claim"},
}


@dataclass(frozen=True)
class ClosureLineRef:
    """单条线的冻结引用：路径 + 原始字节 SHA-256。"""

    # 线名，必须属于 CLOSURE_LINES。
    line: str
    # 相对项目根的 decision.json 路径。
    decision_path: str
    # decision.json 原始字节 SHA-256。
    decision_sha256: str


@dataclass(frozen=True)
class ClosureManifest:
    """正式 closure 前必须冻结的 manifest。"""

    # 机器可读 schema 版本。
    schema_version: int
    # 显式运行模式：synthetic=工程验证，production=真实证据候选。
    run_mode: str
    # 人读 manifest 版本。
    manifest_version: str
    # 本批 closure 批次号。
    batch_id: str
    # closure schema 版本。
    closure_schema_version: str
    # owner 是否确认真实运行。
    owner_confirmed: bool
    # owner 授权引用；未确认时为空字符串。
    owner_confirmation_ref: str
    # 五条线引用列表。
    lines: tuple[ClosureLineRef, ...]


@dataclass(frozen=True)
class ClosureLineSummary:
    """从单条线 decision 提取的脱敏汇总。"""

    # 线名。
    line: str
    # evidence_kind；必须与 LINE 对应。
    evidence_kind: str
    # 该线 decision。
    decision: str
    # 是否 synthetic_only。
    synthetic_only: bool
    # 是否扫描失败。
    scan_failed: bool
    # owner 是否确认（应保持 false，各线 gate pending）。
    owner_confirmed: bool
    # 该线下所有 production claim 是否合规（均为 false / 缺省）。
    claims_ok: bool
    # 该线 run_id（只用于追溯，不含敏感内容）。
    run_id: str
    # 该线 manifest_version。
    manifest_version: str
    # 不合规 claim 字段名列表。
    bad_claims: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClosureAggregate:
    """五条线聚合结果。"""

    # 每条线汇总。
    lines: tuple[ClosureLineSummary, ...]
    # 是否五线均 synthetic_only。
    all_synthetic_only: bool
    # 是否所有 scan_failed 均为 false。
    all_scan_ok: bool
    # 是否所有 production claim 合规。
    all_claims_ok: bool
    # 是否所有 owner gate 仍 pending（未 confirmed）。
    all_owners_pending: bool
    # 是否整体证据完整。
    has_complete_evidence: bool
