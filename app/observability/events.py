"""M5.3 事件解析：allowlist + 敏感扫描 + 稳定关联 ID 校验。"""

# 导入 hashlib/json/math，计算 hash、解析 JSON、校验数值。
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path

# 导入扫描与值对象。
from app.observability.scan import scan_observability_payload
from app.observability.types import (
    EVENT_STATUSES,
    LOG_EVENT_FIELDS,
    OBSERVABILITY_FIELD_VALUE_ALLOWLIST,
    CORRELATION_ID_PREFIXES,
    REQUEST_KINDS,
    ObservabilityEvent,
    ObservabilityManifest,
)


# 当前只支持 schema_version=1。
_SUPPORTED_SCHEMA_VERSION = 1
# 脚手架占位符前缀。
_PLACEHOLDER_MARKERS = ("REPLACE_", "TODO_", "CHANGEME", "<YOUR_", "YOUR_")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_OPAQUE_ID_PATTERN = re.compile(r"^(?:[0-9a-f]{16}|[0-9a-f]{32})$")


def compute_sha256(raw_bytes: bytes) -> str:
    """对冻结输入原始字节计算 SHA-256。"""

    return hashlib.sha256(raw_bytes).hexdigest()


def is_synthetic_observability_manifest(manifest: ObservabilityManifest) -> bool:
    """识别仅用于工程验证的 synthetic manifest。"""

    return manifest.run_mode == "synthetic"


def _require_non_empty_str(value: object, field: str) -> str:
    """要求非空字符串。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _require_identifier(value: object, field: str) -> str:
    """要求日志身份字段为受限标识符，避免 allowlist 字段承载自由文本。"""

    text = _require_non_empty_str(value, field)
    if not _IDENTIFIER_PATTERN.fullmatch(text):
        raise ValueError(f"{field} 必须是仅含字母、数字、点、下划线或连字符的标识符")
    return text


def _require_correlation_id(value: object, field: str) -> str:
    """要求关联 ID 只包含固定前缀和不透明十六进制主体。"""

    text = _require_non_empty_str(value, field)
    field_name = field.rsplit(".", 1)[-1]
    prefix = CORRELATION_ID_PREFIXES[field_name]
    if not text.startswith(prefix) or not _OPAQUE_ID_PATTERN.fullmatch(text[len(prefix) :]):
        raise ValueError(f"{field} 必须是固定前缀加不透明十六进制 ID")
    return text


def _require_timestamp(value: object, field: str) -> str:
    """要求采样时间为 ISO 8601 时间戳。"""

    text = _require_non_empty_str(value, field)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 ISO 8601 时间戳") from exc
    return text


def _require_allowed_identifier(value: object, field: str) -> str:
    """要求会写入事件或指标的身份值来自冻结集合。"""

    text = _require_identifier(value, field)
    field_name = field.rsplit(".", 1)[-1]
    if text not in OBSERVABILITY_FIELD_VALUE_ALLOWLIST[field_name]:
        raise ValueError(f"{field} 不在当前冻结 allowlist 中")
    return text


def _require_positive_int(value: object, field: str) -> int:
    """要求严格正整数。"""

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
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field} 必须是非负有限数值") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} 必须是非负有限数值")
    return number


def _require_unit_interval(value: object, field: str) -> float:
    """要求 [0,1] 采样率。"""

    number = _require_non_negative_number(value, field)
    if number > 1:
        raise ValueError(f"{field} 必须在 0 到 1 之间")
    return number


def _require_sha256(value: object, field: str) -> str:
    """要求 64 位 SHA-256。"""

    text = _require_non_empty_str(value, field).lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ValueError(f"{field} 必须是 SHA-256 十六进制字符串")
    return text


def _looks_like_placeholder(value: str) -> bool:
    """判断字符串是否仍是模板占位符。"""

    upper = value.upper()
    return any(marker in upper for marker in _PLACEHOLDER_MARKERS)


def _assert_known_fields(payload: object, allowed: set[str], path: str) -> None:
    """拒绝未知字段，防止 schema 漂移。"""

    if not isinstance(payload, dict):
        raise ValueError(f"{path} 必须是对象")
    unknown = sorted(set(payload).difference(allowed))
    if unknown:
        raise ValueError(f"{path} 包含白名单外字段: {unknown}")


_MANIFEST_FIELDS = {
    "schema_version",
    "run_mode",
    "manifest_version",
    "batch_id",
    "event_schema_version",
    "sample_rate",
    "retention_days",
    "label_cardinality_limit",
    "owner_confirmed",
    "owner_confirmation_ref",
    "events_path",
    "events_sha256",
}


def parse_observability_manifest(payload: object) -> ObservabilityManifest:
    """解析可观测性 manifest。"""

    if not isinstance(payload, dict):
        raise ValueError("manifest 必须是 JSON 对象")
    scan_observability_payload(payload, "manifest")
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
    manifest = ObservabilityManifest(
        schema_version=schema_version,
        run_mode=run_mode,
        manifest_version=_require_non_empty_str(payload.get("manifest_version"), "manifest_version"),
        batch_id=_require_non_empty_str(payload.get("batch_id"), "batch_id"),
        event_schema_version=_require_non_empty_str(
            payload.get("event_schema_version"),
            "event_schema_version",
        ),
        sample_rate=_require_unit_interval(payload.get("sample_rate"), "sample_rate"),
        retention_days=_require_positive_int(payload.get("retention_days"), "retention_days"),
        label_cardinality_limit=_require_positive_int(
            payload.get("label_cardinality_limit"),
            "label_cardinality_limit",
        ),
        owner_confirmed=owner_confirmed,
        owner_confirmation_ref=owner_confirmation_ref,
        events_path=_require_non_empty_str(payload.get("events_path"), "events_path"),
        events_sha256=_require_sha256(payload.get("events_sha256"), "events_sha256"),
    )
    if owner_confirmed and not owner_confirmation_ref:
        raise ValueError("owner_confirmed=true 时必须提供 owner_confirmation_ref")
    if owner_confirmed and _looks_like_placeholder(owner_confirmation_ref):
        raise ValueError("owner_confirmed=true 时 owner_confirmation_ref 不能是占位符")
    if owner_confirmed and is_synthetic_observability_manifest(manifest):
        raise ValueError("synthetic manifest 不能标记 owner_confirmed=true")
    return manifest


def load_observability_manifest(path: Path, *, project_root: Path | None = None) -> ObservabilityManifest:
    """从磁盘读取并解析 manifest。"""

    raw_bytes = path.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    manifest = parse_observability_manifest(payload)
    if project_root is not None:
        events_path = (project_root / manifest.events_path).resolve()
        if project_root.resolve() not in events_path.parents and events_path != project_root.resolve():
            raise ValueError(f"events_path 必须位于项目根下: {manifest.events_path}")
    return manifest


def parse_observability_event(payload: object, *, path: str = "event") -> ObservabilityEvent:
    """解析单条事件；未知字段或敏感内容 fail-closed。"""

    if not isinstance(payload, dict):
        raise ValueError(f"{path} 必须是对象")
    # 先扫描敏感内容，再做 allowlist，保证密钥不会因“未知字段”路径漏检。
    scan_observability_payload(payload, path)
    _assert_known_fields(payload, LOG_EVENT_FIELDS, path)
    schema_version = _require_positive_int(payload.get("schema_version"), f"{path}.schema_version")
    if schema_version != _SUPPORTED_SCHEMA_VERSION:
        raise ValueError(f"{path} 目前只支持 schema_version=1")
    request_kind = _require_non_empty_str(payload.get("request_kind"), f"{path}.request_kind")
    if request_kind not in REQUEST_KINDS:
        raise ValueError(f"{path}.request_kind 不在冻结集合中")
    status = _require_non_empty_str(payload.get("status"), f"{path}.status")
    if status not in EVENT_STATUSES:
        raise ValueError(f"{path}.status 不在冻结集合中")
    error_code = payload.get("error_code")
    if error_code is not None:
        error_code = _require_allowed_identifier(error_code, f"{path}.error_code")
    if status == "ok" and error_code is not None:
        raise ValueError(f"{path} status=ok 时 error_code 必须为 null")
    if status != "ok" and error_code is None:
        raise ValueError(f"{path} status!=ok 时必须提供 error_code")
    return ObservabilityEvent(
        schema_version=schema_version,
        event_id=_require_correlation_id(payload.get("event_id"), f"{path}.event_id"),
        run_id=_require_correlation_id(payload.get("run_id"), f"{path}.run_id"),
        call_id=_require_correlation_id(payload.get("call_id"), f"{path}.call_id"),
        request_kind=request_kind,
        tool_name=_require_allowed_identifier(payload.get("tool_name"), f"{path}.tool_name"),
        status=status,
        error_code=error_code,
        duration_ms=_require_non_negative_number(payload.get("duration_ms"), f"{path}.duration_ms"),
        step_count=_require_non_negative_int(payload.get("step_count"), f"{path}.step_count"),
        model_id=_require_allowed_identifier(payload.get("model_id"), f"{path}.model_id"),
        tool_version=_require_allowed_identifier(payload.get("tool_version"), f"{path}.tool_version"),
        corpus_version=_require_allowed_identifier(payload.get("corpus_version"), f"{path}.corpus_version"),
        sampled_at=_require_timestamp(payload.get("sampled_at"), f"{path}.sampled_at"),
    )


def parse_observability_events(payload: object) -> tuple[ObservabilityEvent, ...]:
    """解析事件文件：支持数组，或 {schema_version,events:[...]} 对象。"""

    if isinstance(payload, list):
        rows = payload
        wrapper_path = "events"
    elif isinstance(payload, dict):
        scan_observability_payload(payload, "events_file")
        unknown = set(payload).difference({"schema_version", "batch_id", "events"})
        if unknown:
            raise ValueError(f"events_file 包含白名单外字段: {sorted(unknown)}")
        schema_version = _require_positive_int(payload.get("schema_version"), "events_file.schema_version")
        if schema_version != _SUPPORTED_SCHEMA_VERSION:
            raise ValueError("events_file 目前只支持 schema_version=1")
        rows = payload.get("events")
        if not isinstance(rows, list):
            raise ValueError("events_file.events 必须是数组")
        wrapper_path = "events_file.events"
    else:
        raise ValueError("events 必须是数组或对象")
    if not rows:
        raise ValueError("events 不能为空")
    events: list[ObservabilityEvent] = []
    seen_event_ids: set[str] = set()
    for index, row in enumerate(rows):
        event = parse_observability_event(row, path=f"{wrapper_path}[{index}]")
        if event.event_id in seen_event_ids:
            raise ValueError(f"重复 event_id: {event.event_id}")
        seen_event_ids.add(event.event_id)
        events.append(event)
    return tuple(events)


def load_observability_events(path: Path) -> tuple[tuple[ObservabilityEvent, ...], str, bytes]:
    """从磁盘读取事件文件，返回事件、hash 与原始字节。"""

    raw_bytes = path.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    events = parse_observability_events(payload)
    return events, compute_sha256(raw_bytes), raw_bytes
