"""M5.4 缓存策略值对象：只保存脱敏、可重算字段。"""

# 导入 dataclass，保证配置与事件在聚合过程中不可被就地改写。
from dataclasses import dataclass


class CacheMethod:
    """缓存方法标签；当前只覆盖只读检索投影实验。"""

    # 只读 dense 检索投影；命中后仍不改变回答语义。
    dense_retrieve = "dense-retrieve"
    # 只读 hybrid 检索投影；与 dense 使用同一 value 契约。
    hybrid_retrieve = "hybrid-retrieve"


class CacheOutcome:
    """单次缓存实验事件的结果标签。"""

    # 命中有效条目。
    hit = "hit"
    # 未命中且未触发其他失效原因。
    miss = "miss"
    # 旁路开关打开，主动跳过读写。
    bypass = "bypass"
    # 容量淘汰导致旧条目被移除。
    evict = "evict"
    # TTL 到期导致失效。
    expire = "expire"
    # key_version 轮换后旧 namespace 整体不可用。
    rotation_miss = "rotation_miss"
    # corpus/model/tool 版本变化导致无法命中旧值。
    version_miss = "version_miss"
    # 成功写入且未触发淘汰；只用于 set 操作，不计入 hit 分母。
    stored = "stored"


class CacheOperation:
    """raw 事件记录的操作类型。"""

    # 读取缓存。
    get = "get"
    # 写入缓存。
    set = "set"
    # 显式旁路（不读写）。
    bypass = "bypass"
    # 显式 key_version 轮换。
    rotate = "rotate"
    # 容量淘汰事件。
    evict = "evict"


# 设计冻结的方法集合。
CACHE_METHODS = {
    CacheMethod.dense_retrieve,
    CacheMethod.hybrid_retrieve,
}

# 设计冻结的结果集合。
CACHE_OUTCOMES = {
    CacheOutcome.hit,
    CacheOutcome.miss,
    CacheOutcome.bypass,
    CacheOutcome.evict,
    CacheOutcome.expire,
    CacheOutcome.rotation_miss,
    CacheOutcome.version_miss,
    CacheOutcome.stored,
}

# 设计冻结的操作集合。
CACHE_OPERATIONS = {
    CacheOperation.get,
    CacheOperation.set,
    CacheOperation.bypass,
    CacheOperation.rotate,
    CacheOperation.evict,
}

# 缓存 value 白名单；禁止 query/正文/密钥。
CACHE_VALUE_FIELDS = {
    "schema_version",
    "source_id",
    "chunk_index",
    "rank",
    "method",
    "corpus_version",
    "created_at",
    "expires_at",
}


@dataclass(frozen=True)
class CacheValue:
    """只读缓存值：仅允许脱敏投影字段。"""

    # value schema 版本。
    schema_version: int
    # 来源文档稳定 ID，不是正文。
    source_id: str
    # chunk 序号。
    chunk_index: int
    # 排序名次。
    rank: int
    # 产生该投影的方法。
    method: str
    # 语料版本；变化后旧值不可复用。
    corpus_version: str
    # 写入时间（ISO-8601 文本，便于 JSON 稳定）。
    created_at: str
    # 过期时间（ISO-8601 文本）。
    expires_at: str


@dataclass(frozen=True)
class CacheManifest:
    """正式缓存实验前必须冻结的 manifest。"""

    # 机器可读 schema 版本。
    schema_version: int
    # 显式运行模式：synthetic=工程验证，production=真实证据候选。
    run_mode: str
    # 人读 manifest 版本。
    manifest_version: str
    # 本批实验批次号。
    batch_id: str
    # 缓存 raw schema 版本。
    cache_schema_version: str
    # 当前 key_version；轮换后旧 namespace 整体失效。
    key_version: int
    # 语料版本。
    corpus_version: str
    # 模型版本。
    model_version: str
    # 工具版本。
    tool_version: str
    # 默认 TTL 秒数。
    default_ttl_seconds: int
    # 单 namespace 最大容量。
    max_entries: int
    # 默认是否旁路；设计要求默认 True。
    default_bypass: bool
    # secret 来源引用；只写来源标签，不写 secret 内容。
    secret_source_ref: str
    # owner 是否确认真实运行。
    owner_confirmed: bool
    # owner 授权引用；未确认时为空字符串。
    owner_confirmation_ref: str
    # raw 文件相对路径。
    raw_path: str
    # raw 文件内容 hash。
    raw_sha256: str


@dataclass(frozen=True)
class CacheRawEvent:
    """单次缓存实验原始事件；禁止包含 query/正文/健康信息/密钥。"""

    # 事件稳定 ID。
    event_id: str
    # 批次号，与 manifest.batch_id 一致。
    batch_id: str
    # 本 run 的唯一 ID。
    run_id: str
    # get/set/bypass/rotate/evict。
    operation: str
    # dense-retrieve / hybrid-retrieve。
    method: str
    # hit/miss/bypass/evict/expire/rotation_miss/version_miss。
    outcome: str
    # 事件发生时的 key_version。
    key_version: int
    # 语料版本。
    corpus_version: str
    # 模型版本。
    model_version: str
    # 工具版本。
    tool_version: str
    # 完整 cache key；只含 HMAC digest，不含 query 原文。
    cache_key: str
    # 操作耗时毫秒。
    latency_ms: float
    # 当时旁路开关状态。
    bypass_enabled: bool
    # 当时 TTL。
    ttl_seconds: int
    # 当时容量上限。
    capacity: int
    # 操作前 namespace 条目数。
    namespace_size_before: int
    # 操作后 namespace 条目数。
    namespace_size_after: int
    # 命中时的脱敏 value；未命中为 None。
    value: CacheValue | None
    # 采样时间。
    sampled_at: str


def cache_event_key(event_id: str, batch_id: str, run_id: str) -> str:
    """构造 raw 事件去重键。"""

    return f"{batch_id}|{run_id}|{event_id}"
