"""M5.4 内存缓存 store：默认旁路、TTL/容量/轮换失效，仅服务离线实验。"""

# 导入 OrderedDict，用插入顺序近似 FIFO 容量淘汰。
from collections import OrderedDict
# 导入 datetime/timezone，做 TTL 过期判断。
from datetime import datetime, timezone
# 导入 dataclasses.asdict，便于把 value 转成 JSON 友好结构。
from dataclasses import asdict

# 导入 key 解析，保证 namespace 与 key 段一致。
from app.evaluation.cache.keys import parse_cache_key
# 导入扫描，防止 value 夹带敏感内容。
from app.evaluation.cache.scan import scan_cache_payload
# 导入结果标签与 value 对象。
from app.evaluation.cache.types import (
    CACHE_VALUE_FIELDS,
    CACHE_METHODS,
    CacheOutcome,
    CacheValue,
)


def _parse_iso8601(text: str) -> datetime:
    """把 ISO-8601 文本解析为 aware datetime。"""

    if not isinstance(text, str) or not text:
        raise ValueError("时间字段必须是非空 ISO-8601 字符串")
    # 允许 Z 结尾，统一转成 +00:00。
    normalized = text.replace("Z", "+00:00")
    try:
        value = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"时间字段不是合法 ISO-8601: {text}") from exc
    if value.tzinfo is None:
        raise ValueError(f"时间字段必须带时区: {text}")
    return value.astimezone(timezone.utc)


def validate_cache_value(value: CacheValue | dict) -> CacheValue:
    """校验 value 白名单并返回不可变对象。"""

    if isinstance(value, CacheValue):
        payload = asdict(value)
    elif isinstance(value, dict):
        payload = value
    else:
        raise TypeError("cache value 必须是 CacheValue 或 dict")

    scan_cache_payload(payload, "cache_value")
    unknown = sorted(set(payload).difference(CACHE_VALUE_FIELDS))
    if unknown:
        raise ValueError(f"cache value 包含白名单外字段: {unknown}")
    missing = sorted(CACHE_VALUE_FIELDS.difference(payload))
    if missing:
        raise ValueError(f"cache value 缺少必填字段: {missing}")

    schema_version = payload["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version <= 0:
        raise ValueError("cache value.schema_version 必须是正整数")
    chunk_index = payload["chunk_index"]
    if isinstance(chunk_index, bool) or not isinstance(chunk_index, int) or chunk_index < 0:
        raise ValueError("cache value.chunk_index 必须是非负整数")
    rank = payload["rank"]
    if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
        raise ValueError("cache value.rank 必须是正整数")
    method = payload["method"]
    if method not in CACHE_METHODS:
        raise ValueError(f"cache value.method 不在冻结集合中: {method}")
    for field in ("source_id", "corpus_version", "created_at", "expires_at"):
        if not isinstance(payload[field], str) or not payload[field].strip():
            raise ValueError(f"cache value.{field} 必须是非空字符串")
    created_at = _parse_iso8601(payload["created_at"])
    expires_at = _parse_iso8601(payload["expires_at"])
    if expires_at <= created_at:
        raise ValueError("cache value.expires_at 必须晚于 created_at")

    return CacheValue(
        schema_version=schema_version,
        source_id=str(payload["source_id"]).strip(),
        chunk_index=chunk_index,
        rank=rank,
        method=method,
        corpus_version=str(payload["corpus_version"]).strip(),
        created_at=payload["created_at"],
        expires_at=payload["expires_at"],
    )


class InMemoryCacheStore:
    """按 key_version 分 namespace 的内存 store。

    这是离线实验组件，不是生产 Redis/本地热路径实现。
    """

    def __init__(
        self,
        *,
        key_version: int,
        max_entries: int,
        default_bypass: bool = True,
    ) -> None:
        if isinstance(key_version, bool) or not isinstance(key_version, int) or key_version <= 0:
            raise ValueError("key_version 必须是正整数")
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
            raise ValueError("max_entries 必须是正整数")
        if not isinstance(default_bypass, bool):
            raise ValueError("default_bypass 必须是布尔值")
        # 当前活跃 key_version；轮换后旧 namespace 只读不可命中。
        self._active_key_version = key_version
        # 每个 namespace 的有序字典：key -> (value, expires_at)。
        self._namespaces: dict[int, OrderedDict[str, tuple[CacheValue, datetime]]] = {
            key_version: OrderedDict()
        }
        self._max_entries = max_entries
        # 旁路开关优先级高于命中。
        self.bypass_enabled = default_bypass

    @property
    def active_key_version(self) -> int:
        """返回当前活跃 key_version。"""

        return self._active_key_version

    def namespace_size(self, key_version: int | None = None) -> int:
        """返回指定 namespace 当前条目数。"""

        version = self._active_key_version if key_version is None else key_version
        return len(self._namespaces.get(version, OrderedDict()))

    def rotate_key_version(self, new_key_version: int) -> None:
        """轮换 key_version：旧 namespace 整体失效，不再可命中。"""

        if isinstance(new_key_version, bool) or not isinstance(new_key_version, int):
            raise ValueError("new_key_version 必须是正整数")
        if new_key_version <= self._active_key_version:
            raise ValueError("new_key_version 必须严格大于当前 key_version")
        self._active_key_version = new_key_version
        self._namespaces.setdefault(new_key_version, OrderedDict())

    def set(
        self,
        cache_key: str,
        value: CacheValue | dict,
        *,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """写入缓存；旁路开启时拒绝写入。"""

        if self.bypass_enabled:
            return {
                "outcome": CacheOutcome.bypass,
                "evicted_key": None,
                "namespace_size_before": self.namespace_size(),
                "namespace_size_after": self.namespace_size(),
                "value": None,
            }
        parsed = parse_cache_key(cache_key)
        key_version = int(parsed["key_version"])
        if key_version != self._active_key_version:
            # 写入必须落在活跃 namespace，避免旧版本继续扩张。
            raise ValueError("只能写入当前活跃 key_version 的 namespace")
        validated = validate_cache_value(value)
        if validated.method != parsed["method"]:
            raise ValueError("cache value.method 必须等于 key.method")
        if validated.corpus_version != parsed["corpus_version"]:
            raise ValueError("cache value.corpus_version 必须等于 key.corpus_version")
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            raise ValueError("now 必须是带时区的 datetime")
        expires_at = _parse_iso8601(validated.expires_at)
        if expires_at <= clock.astimezone(timezone.utc):
            raise ValueError("不能写入已经过期的 cache value")

        namespace = self._namespaces.setdefault(key_version, OrderedDict())
        size_before = len(namespace)
        evicted_key = None
        # 容量淘汰：写满后先删最早插入的条目。
        if cache_key not in namespace and len(namespace) >= self._max_entries:
            evicted_key, _ = namespace.popitem(last=False)
        namespace[cache_key] = (validated, expires_at)
        # 再次 set 同一 key 时刷新插入顺序，近似 LRU 行为但不依赖外部库。
        namespace.move_to_end(cache_key)
        return {
            # 成功写入用 stored；只有发生容量淘汰时才标 evict。
            "outcome": CacheOutcome.evict if evicted_key is not None else CacheOutcome.stored,
            "evicted_key": evicted_key,
            "namespace_size_before": size_before,
            "namespace_size_after": len(namespace),
            "value": validated,
        }

    def get(
        self,
        cache_key: str,
        *,
        now: datetime | None = None,
        expected_corpus_version: str | None = None,
        expected_model_version: str | None = None,
        expected_tool_version: str | None = None,
    ) -> dict[str, object]:
        """读取缓存；旁路/TTL/版本/轮换都会 fail-closed 为对应 miss 结果。"""

        size_before = self.namespace_size()
        if self.bypass_enabled:
            return {
                "outcome": CacheOutcome.bypass,
                "value": None,
                "namespace_size_before": size_before,
                "namespace_size_after": size_before,
            }

        parsed = parse_cache_key(cache_key)
        key_version = int(parsed["key_version"])
        # key_version 轮换后旧 namespace 不可命中。
        if key_version != self._active_key_version:
            return {
                "outcome": CacheOutcome.rotation_miss,
                "value": None,
                "namespace_size_before": size_before,
                "namespace_size_after": size_before,
            }
        # corpus/model/tool 任一变化都视为版本 miss。
        if expected_corpus_version is not None and expected_corpus_version != parsed["corpus_version"]:
            return {
                "outcome": CacheOutcome.version_miss,
                "value": None,
                "namespace_size_before": size_before,
                "namespace_size_after": size_before,
            }
        if expected_model_version is not None and expected_model_version != parsed["model_version"]:
            return {
                "outcome": CacheOutcome.version_miss,
                "value": None,
                "namespace_size_before": size_before,
                "namespace_size_after": size_before,
            }
        if expected_tool_version is not None and expected_tool_version != parsed["tool_version"]:
            return {
                "outcome": CacheOutcome.version_miss,
                "value": None,
                "namespace_size_before": size_before,
                "namespace_size_after": size_before,
            }

        namespace = self._namespaces.get(key_version, OrderedDict())
        item = namespace.get(cache_key)
        if item is None:
            return {
                "outcome": CacheOutcome.miss,
                "value": None,
                "namespace_size_before": size_before,
                "namespace_size_after": size_before,
            }

        value, expires_at = item
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            raise ValueError("now 必须是带时区的 datetime")
        if expires_at <= clock.astimezone(timezone.utc):
            # TTL 到期：删除后返回 expire。
            del namespace[cache_key]
            return {
                "outcome": CacheOutcome.expire,
                "value": None,
                "namespace_size_before": size_before,
                "namespace_size_after": len(namespace),
            }
        # 命中时刷新顺序，便于后续容量淘汰。
        namespace.move_to_end(cache_key)
        return {
            "outcome": CacheOutcome.hit,
            "value": value,
            "namespace_size_before": size_before,
            "namespace_size_after": len(namespace),
        }
