"""M5.4 cache manifest 与 raw 的解析、冻结与严格校验。"""

# 导入 hashlib/json/math/re，计算 hash、解析 JSON、校验数值与 SHA-256。
import hashlib
import json
import math
import re
from pathlib import Path

# 导入 key 解析，确保 raw 里的 cache_key 符合冻结格式。
from app.evaluation.cache.keys import parse_cache_key
# 导入敏感扫描。
from app.evaluation.cache.scan import scan_cache_payload
# 导入 store 的 value 校验，复用同一白名单。
from app.evaluation.cache.store import validate_cache_value
# 导入值对象与冻结集合。
from app.evaluation.cache.types import (
    CACHE_METHODS,
    CACHE_OPERATIONS,
    CACHE_OUTCOMES,
    CACHE_VALUE_FIELDS,
    CacheManifest,
    CacheRawEvent,
    CacheValue,
    cache_event_key,
)


# 当前只支持 schema_version=1。
_SUPPORTED_SCHEMA_VERSION = 1
# 必填 hash 统一为 64 位小写十六进制。
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
# 脚手架占位符前缀，正式 owner 确认后禁止出现。
_PLACEHOLDER_MARKERS = ("REPLACE_", "TODO_", "CHANGEME", "<YOUR_", "YOUR_")
# manifest 顶层允许字段。
_MANIFEST_FIELDS = {
    "schema_version",
    "run_mode",
    "manifest_version",
    "batch_id",
    "cache_schema_version",
    "key_version",
    "corpus_version",
    "model_version",
    "tool_version",
    "default_ttl_seconds",
    "max_entries",
    "default_bypass",
    "secret_source_ref",
    "owner_confirmed",
    "owner_confirmation_ref",
    "raw_path",
    "raw_sha256",
}
# raw 顶层允许字段。
_RAW_FIELDS = {
    "schema_version",
    "batch_id",
    "run_id",
    "events",
}
# raw 单事件允许字段。
_EVENT_FIELDS = {
    "event_id",
    "batch_id",
    "run_id",
    "operation",
    "method",
    "outcome",
    "key_version",
    "corpus_version",
    "model_version",
    "tool_version",
    "cache_key",
    "latency_ms",
    "bypass_enabled",
    "ttl_seconds",
    "capacity",
    "namespace_size_before",
    "namespace_size_after",
    "value",
    "sampled_at",
}


def compute_sha256(raw_bytes: bytes) -> str:
    """对冻结输入原始字节计算 SHA-256。"""

    return hashlib.sha256(raw_bytes).hexdigest()


def is_synthetic_cache_manifest(manifest: CacheManifest) -> bool:
    """识别仅用于工程验证的 synthetic manifest。"""

    # 只相信显式 run_mode，不用 tool/model 字符串猜测。
    return manifest.run_mode == "synthetic"


def _require_non_empty_str(value: object, field: str) -> str:
    """要求非空字符串。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _require_positive_int(value: object, field: str) -> int:
    """要求严格正整数，bool 不能被当作数字。"""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} 必须是正整数")
    return value


def _require_non_negative_int(value: object, field: str) -> int:
    """要求非负整数。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} 必须是非负整数")
    return value


def _require_non_negative_number(value: object, field: str) -> float:
    """要求非负有限数值。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数值")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} 必须是非负有限数值")
    return number


def _require_sha256(value: object, field: str) -> str:
    """要求 64 位 SHA-256 小写十六进制。"""

    text = _require_non_empty_str(value, field).lower()
    if not _SHA256_PATTERN.fullmatch(text):
        raise ValueError(f"{field} 必须是 SHA-256 十六进制字符串")
    return text


def _looks_like_placeholder(value: str) -> bool:
    """判断字符串是否仍是模板占位符。"""

    upper = value.upper()
    return any(marker in upper for marker in _PLACEHOLDER_MARKERS)


def _assert_known_fields(
    payload: object,
    allowed: set[str],
    path: str,
    *,
    nested_allowed: dict[str, set[str]] | None = None,
) -> None:
    """按结构层递归拒绝未知字段。"""

    if isinstance(payload, dict):
        unknown = sorted(set(payload).difference(allowed))
        if unknown:
            raise ValueError(f"{path} 包含白名单外字段: {unknown}")
        for key, value in payload.items():
            child_allowed = (nested_allowed or {}).get(key, allowed)
            _assert_known_fields(value, child_allowed, f"{path}.{key}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            _assert_known_fields(value, allowed, f"{path}[{index}]")


def parse_cache_manifest(payload: object) -> CacheManifest:
    """把 manifest JSON 解析为不可变配置。"""

    if not isinstance(payload, dict):
        raise ValueError("manifest 必须是 JSON 对象")
    scan_cache_payload(payload, "manifest")
    _assert_known_fields(payload, _MANIFEST_FIELDS, "manifest")
    schema_version = _require_positive_int(payload.get("schema_version"), "manifest.schema_version")
    if schema_version != _SUPPORTED_SCHEMA_VERSION:
        raise ValueError("manifest 目前只支持 schema_version=1")
    run_mode = _require_non_empty_str(payload.get("run_mode"), "manifest.run_mode")
    if run_mode not in {"synthetic", "production"}:
        raise ValueError("manifest.run_mode 必须为 synthetic 或 production")
    owner_confirmed = payload.get("owner_confirmed", False)
    if not isinstance(owner_confirmed, bool):
        raise ValueError("owner_confirmed 必须是布尔值")
    owner_confirmation_ref = str(payload.get("owner_confirmation_ref") or "").strip()
    default_bypass = payload.get("default_bypass", True)
    if not isinstance(default_bypass, bool):
        raise ValueError("default_bypass 必须是布尔值")
    # 设计要求默认旁路；synthetic 工程证据必须保持 True。
    if run_mode == "synthetic" and default_bypass is not True:
        raise ValueError("synthetic manifest 必须 default_bypass=true")
    secret_source_ref = _require_non_empty_str(
        payload.get("secret_source_ref"),
        "secret_source_ref",
    )
    # secret 只允许来源标签，禁止看起来像密钥本体。
    if _looks_like_placeholder(secret_source_ref) is False and any(
        marker in secret_source_ref.lower()
        for marker in ("sk-", "secret=", "password=")
    ):
        raise ValueError("secret_source_ref 只能写来源标签，不能写密钥内容")
    manifest = CacheManifest(
        schema_version=schema_version,
        run_mode=run_mode,
        manifest_version=_require_non_empty_str(payload.get("manifest_version"), "manifest_version"),
        batch_id=_require_non_empty_str(payload.get("batch_id"), "batch_id"),
        cache_schema_version=_require_non_empty_str(
            payload.get("cache_schema_version"),
            "cache_schema_version",
        ),
        key_version=_require_positive_int(payload.get("key_version"), "key_version"),
        corpus_version=_require_non_empty_str(payload.get("corpus_version"), "corpus_version"),
        model_version=_require_non_empty_str(payload.get("model_version"), "model_version"),
        tool_version=_require_non_empty_str(payload.get("tool_version"), "tool_version"),
        default_ttl_seconds=_require_positive_int(
            payload.get("default_ttl_seconds"),
            "default_ttl_seconds",
        ),
        max_entries=_require_positive_int(payload.get("max_entries"), "max_entries"),
        default_bypass=default_bypass,
        secret_source_ref=secret_source_ref,
        owner_confirmed=owner_confirmed,
        owner_confirmation_ref=owner_confirmation_ref,
        raw_path=_require_non_empty_str(payload.get("raw_path"), "raw_path"),
        raw_sha256=_require_sha256(payload.get("raw_sha256"), "raw_sha256"),
    )
    if owner_confirmed and not owner_confirmation_ref:
        raise ValueError("owner_confirmed=true 时必须提供 owner_confirmation_ref")
    if owner_confirmed:
        if _looks_like_placeholder(owner_confirmation_ref):
            raise ValueError("owner_confirmed=true 时 owner_confirmation_ref 不能是占位符")
        if is_synthetic_cache_manifest(manifest):
            raise ValueError("synthetic manifest 不能标记 owner_confirmed=true")
    return manifest


def load_cache_manifest(path: Path, *, project_root: Path | None = None) -> CacheManifest:
    """从磁盘读取并解析 cache manifest。"""

    raw_bytes = path.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    manifest = parse_cache_manifest(payload)
    if project_root is not None:
        # raw_path 必须相对项目根可解析，防止路径穿越。
        raw_path = (project_root / manifest.raw_path).resolve()
        if project_root.resolve() not in raw_path.parents and raw_path != project_root.resolve():
            raise ValueError(f"raw_path 必须位于项目根下: {manifest.raw_path}")
    return manifest


def parse_cache_raw(payload: object) -> tuple[CacheRawEvent, ...]:
    """解析 raw events；事件键必须唯一。"""

    if not isinstance(payload, dict):
        raise ValueError("raw 必须是 JSON 对象")
    scan_cache_payload(payload, "raw")
    # 只校验 raw 顶层字段；events/value 在下方分层校验，避免父子白名单互相污染。
    unknown_top = sorted(set(payload).difference(_RAW_FIELDS))
    if unknown_top:
        raise ValueError(f"raw 包含白名单外字段: {unknown_top}")
    schema_version = _require_positive_int(payload.get("schema_version"), "raw.schema_version")
    if schema_version != _SUPPORTED_SCHEMA_VERSION:
        raise ValueError("raw 目前只支持 schema_version=1")
    batch_id = _require_non_empty_str(payload.get("batch_id"), "raw.batch_id")
    run_id = _require_non_empty_str(payload.get("run_id"), "raw.run_id")
    events_raw = payload.get("events")
    if not isinstance(events_raw, list) or not events_raw:
        raise ValueError("raw.events 必须是非空数组")
    events: list[CacheRawEvent] = []
    seen: set[str] = set()
    for index, row in enumerate(events_raw):
        if not isinstance(row, dict):
            raise ValueError(f"raw.events[{index}] 必须是对象")
        # 事件层白名单；value 单独再校验。
        event_without_value = {k: v for k, v in row.items() if k != "value"}
        _assert_known_fields(event_without_value, _EVENT_FIELDS, f"raw.events[{index}]")
        if "value" in row and row["value"] is not None:
            if not isinstance(row["value"], dict):
                raise ValueError(f"raw.events[{index}].value 必须是对象或 null")
            _assert_known_fields(row["value"], CACHE_VALUE_FIELDS, f"raw.events[{index}].value")
        event_id = _require_non_empty_str(row.get("event_id"), f"raw.events[{index}].event_id")
        sample_batch = _require_non_empty_str(row.get("batch_id"), f"raw.events[{index}].batch_id")
        sample_run = _require_non_empty_str(row.get("run_id"), f"raw.events[{index}].run_id")
        if sample_batch != batch_id:
            raise ValueError(f"raw.events[{index}].batch_id 必须等于 raw.batch_id")
        if sample_run != run_id:
            raise ValueError(f"raw.events[{index}].run_id 必须等于 raw.run_id")
        operation = _require_non_empty_str(row.get("operation"), f"raw.events[{index}].operation")
        if operation not in CACHE_OPERATIONS:
            raise ValueError(f"raw.events[{index}].operation 不合法")
        method = _require_non_empty_str(row.get("method"), f"raw.events[{index}].method")
        if method not in CACHE_METHODS:
            raise ValueError(f"raw.events[{index}].method 不合法")
        outcome = _require_non_empty_str(row.get("outcome"), f"raw.events[{index}].outcome")
        if outcome not in CACHE_OUTCOMES:
            raise ValueError(f"raw.events[{index}].outcome 不合法")
        key_version = _require_positive_int(row.get("key_version"), f"raw.events[{index}].key_version")
        corpus_version = _require_non_empty_str(
            row.get("corpus_version"),
            f"raw.events[{index}].corpus_version",
        )
        model_version = _require_non_empty_str(
            row.get("model_version"),
            f"raw.events[{index}].model_version",
        )
        tool_version = _require_non_empty_str(
            row.get("tool_version"),
            f"raw.events[{index}].tool_version",
        )
        cache_key = _require_non_empty_str(row.get("cache_key"), f"raw.events[{index}].cache_key")
        parsed_key = parse_cache_key(cache_key)
        # key 段必须与事件声明一致，防止伪造命中。
        if int(parsed_key["key_version"]) != key_version:
            raise ValueError(f"raw.events[{index}].cache_key key_version 与事件不一致")
        if parsed_key["corpus_version"] != corpus_version:
            raise ValueError(f"raw.events[{index}].cache_key corpus_version 与事件不一致")
        if parsed_key["model_version"] != model_version:
            raise ValueError(f"raw.events[{index}].cache_key model_version 与事件不一致")
        if parsed_key["tool_version"] != tool_version:
            raise ValueError(f"raw.events[{index}].cache_key tool_version 与事件不一致")
        if parsed_key["method"] != method:
            raise ValueError(f"raw.events[{index}].cache_key method 与事件不一致")
        bypass_enabled = row.get("bypass_enabled")
        if not isinstance(bypass_enabled, bool):
            raise ValueError(f"raw.events[{index}].bypass_enabled 必须是布尔值")
        value_obj: CacheValue | None = None
        raw_value = row.get("value")
        if raw_value is not None:
            value_obj = validate_cache_value(raw_value)
            # 只有 hit，或 set 的 stored/evict，允许携带脱敏 value。
            allowed_value = outcome == "hit" or (
                operation == "set" and outcome in {"stored", "evict"}
            )
            if not allowed_value:
                raise ValueError(
                    f"raw.events[{index}] 仅 hit 或 set(stored/evict) 可携带 value"
                )
        elif outcome == "hit":
            raise ValueError(f"raw.events[{index}] hit 事件必须携带脱敏 value")
        elif operation == "set" and outcome in {"stored", "evict"}:
            raise ValueError(
                f"raw.events[{index}] set/{outcome} 事件必须携带脱敏 value"
            )
        dedupe = cache_event_key(event_id, sample_batch, sample_run)
        if dedupe in seen:
            raise ValueError(f"重复 raw 事件键: {dedupe}")
        seen.add(dedupe)
        events.append(
            CacheRawEvent(
                event_id=event_id,
                batch_id=sample_batch,
                run_id=sample_run,
                operation=operation,
                method=method,
                outcome=outcome,
                key_version=key_version,
                corpus_version=corpus_version,
                model_version=model_version,
                tool_version=tool_version,
                cache_key=cache_key,
                latency_ms=_require_non_negative_number(
                    row.get("latency_ms"),
                    f"raw.events[{index}].latency_ms",
                ),
                bypass_enabled=bypass_enabled,
                ttl_seconds=_require_positive_int(
                    row.get("ttl_seconds"),
                    f"raw.events[{index}].ttl_seconds",
                ),
                capacity=_require_positive_int(
                    row.get("capacity"),
                    f"raw.events[{index}].capacity",
                ),
                namespace_size_before=_require_non_negative_int(
                    row.get("namespace_size_before"),
                    f"raw.events[{index}].namespace_size_before",
                ),
                namespace_size_after=_require_non_negative_int(
                    row.get("namespace_size_after"),
                    f"raw.events[{index}].namespace_size_after",
                ),
                value=value_obj,
                sampled_at=_require_non_empty_str(
                    row.get("sampled_at"),
                    f"raw.events[{index}].sampled_at",
                ),
            )
        )
    return tuple(events)


def load_cache_raw(path: Path) -> tuple[tuple[CacheRawEvent, ...], str, bytes]:
    """从磁盘读取 raw，并返回事件、内容 hash 与原始字节。"""

    raw_bytes = path.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    events = parse_cache_raw(payload)
    return events, compute_sha256(raw_bytes), raw_bytes


def validate_cache_raw_against_manifest(
    events: tuple[CacheRawEvent, ...],
    manifest: CacheManifest,
) -> None:
    """校验 raw 是否与 manifest 冻结契约一致。"""

    if not events:
        raise ValueError("raw events 不能为空")
    for event in events:
        if event.batch_id != manifest.batch_id:
            raise ValueError("raw.batch_id 必须等于 manifest.batch_id")
        # capacity/ttl 必须能对上 manifest 冻结值，防止私自放大容量。
        if event.capacity != manifest.max_entries:
            raise ValueError(
                f"event {event.event_id} capacity={event.capacity} 必须等于 manifest.max_entries"
            )
        if event.ttl_seconds != manifest.default_ttl_seconds:
            raise ValueError(
                f"event {event.event_id} ttl_seconds 必须等于 manifest.default_ttl_seconds"
            )
        # 默认旁路契约：manifest.default_bypass=true 时，至少要有 bypass 证据。
    if manifest.default_bypass and not any(item.outcome == "bypass" for item in events):
        raise ValueError("default_bypass=true 时 raw 必须包含至少一条 bypass 证据")
