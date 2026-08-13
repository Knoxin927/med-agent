"""M5.5 cost manifest 与 detail 的解析、冻结与严格校验。"""

# 导入 hashlib/json/math/re，计算 hash、解析 JSON、校验数值与 SHA-256。
import hashlib
import json
import math
import re
from pathlib import Path

# 导入敏感扫描。
from app.evaluation.cost.scan import scan_cost_payload
# 导入值对象与冻结集合。
from app.evaluation.cost.types import (
    COST_REQUEST_KINDS,
    CostUsageStatus,
    CostPricingStatus,
    USAGE_KEYS_BY_KIND,
    CostDetail,
    CostManifest,
    cost_detail_key,
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
    "cost_schema_version",
    "request_kinds",
    "currency",
    "price_source_ref",
    "price_as_of",
    "owner_confirmed",
    "owner_confirmation_ref",
    "detail_path",
    "detail_sha256",
}
# detail 顶层允许字段（usage 单独校验）。
_DETAIL_FIELDS = {
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
# detail 文件顶层允许字段。
_DETAILS_FILE_FIELDS = {
    "schema_version",
    "batch_id",
    "run_id",
    "details",
}


def compute_sha256(raw_bytes: bytes) -> str:
    """对冻结输入原始字节计算 SHA-256。"""

    return hashlib.sha256(raw_bytes).hexdigest()


def is_synthetic_cost_manifest(manifest: CostManifest) -> bool:
    """识别仅用于工程验证的 synthetic manifest。"""

    # 只相信显式 run_mode，不用 provider/currency 字符串猜测。
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


def _require_optional_non_negative_number(value: object, field: str) -> float | None:
    """允许 None 或非负有限数值；None 表示 not_available 金额。"""

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} 不能是布尔值")
    return _require_non_negative_number(value, field)


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


def _assert_known_fields(payload: object, allowed: set[str], path: str) -> None:
    """只校验当前 dict 的本层字段白名单，不递归到嵌套对象。"""

    if isinstance(payload, dict):
        unknown = sorted(set(payload).difference(allowed))
        if unknown:
            raise ValueError(f"{path} 包含白名单外字段: {unknown}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            _assert_known_fields(value, allowed, f"{path}[{index}]")


def parse_cost_manifest(payload: object) -> CostManifest:
    """把 manifest JSON 解析为不可变配置。"""

    if not isinstance(payload, dict):
        raise ValueError("manifest 必须是 JSON 对象")
    scan_cost_payload(payload, "manifest")
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
    request_kinds_raw = payload.get("request_kinds")
    if not isinstance(request_kinds_raw, list) or not request_kinds_raw:
        raise ValueError("request_kinds 必须是非空数组")
    request_kinds: list[str] = []
    for index, item in enumerate(request_kinds_raw):
        kind = _require_non_empty_str(item, f"request_kinds[{index}]")
        if kind not in COST_REQUEST_KINDS:
            raise ValueError(f"request_kinds[{index}] 不合法: {kind}")
        request_kinds.append(kind)
    if len(request_kinds) != len(set(request_kinds)):
        raise ValueError("request_kinds 不能重复")
    manifest = CostManifest(
        schema_version=schema_version,
        run_mode=run_mode,
        manifest_version=_require_non_empty_str(payload.get("manifest_version"), "manifest_version"),
        batch_id=_require_non_empty_str(payload.get("batch_id"), "batch_id"),
        cost_schema_version=_require_non_empty_str(
            payload.get("cost_schema_version"),
            "cost_schema_version",
        ),
        request_kinds=tuple(request_kinds),
        currency=_require_non_empty_str(payload.get("currency"), "currency"),
        price_source_ref=_require_non_empty_str(
            payload.get("price_source_ref"),
            "price_source_ref",
        ),
        price_as_of=_require_non_empty_str(payload.get("price_as_of"), "price_as_of"),
        owner_confirmed=owner_confirmed,
        owner_confirmation_ref=owner_confirmation_ref,
        detail_path=_require_non_empty_str(payload.get("detail_path"), "detail_path"),
        detail_sha256=_require_sha256(payload.get("detail_sha256"), "detail_sha256"),
    )
    if owner_confirmed and not owner_confirmation_ref:
        raise ValueError("owner_confirmed=true 时必须提供 owner_confirmation_ref")
    if owner_confirmed:
        if _looks_like_placeholder(owner_confirmation_ref):
            raise ValueError("owner_confirmed=true 时 owner_confirmation_ref 不能是占位符")
        if is_synthetic_cost_manifest(manifest):
            raise ValueError("synthetic manifest 不能标记 owner_confirmed=true")
    return manifest


def load_cost_manifest(path: Path, *, project_root: Path | None = None) -> CostManifest:
    """从磁盘读取并解析 cost manifest。"""

    raw_bytes = path.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    manifest = parse_cost_manifest(payload)
    if project_root is not None:
        # detail_path 必须相对项目根可解析，防止路径穿越。
        detail_path = (project_root / manifest.detail_path).resolve()
        if (
            project_root.resolve() not in detail_path.parents
            and detail_path != project_root.resolve()
        ):
            raise ValueError(f"detail_path 必须位于项目根下: {manifest.detail_path}")
    return manifest


def _validate_usage(usage: object, request_kind: str, path: str) -> dict[str, int]:
    """校验 usage 白名单与计数数值。"""

    if not isinstance(usage, dict):
        raise ValueError(f"{path} 必须是对象")
    allowed_keys = USAGE_KEYS_BY_KIND[request_kind]
    unknown = sorted(set(usage).difference(allowed_keys))
    if unknown:
        raise ValueError(f"{path} 包含白名单外字段: {unknown}")
    result: dict[str, int] = {}
    for key in usage:
        result[key] = _require_non_negative_int(usage[key], f"{path}.{key}")
    return result


def parse_cost_detail(payload: object) -> CostDetail:
    """解析单条 cost detail。"""

    if not isinstance(payload, dict):
        raise ValueError("detail 必须是 JSON 对象")
    scan_cost_payload(payload, "detail")
    _assert_known_fields(payload, _DETAIL_FIELDS, "detail")
    cost_schema_version = _require_positive_int(
        payload.get("cost_schema_version"),
        "detail.cost_schema_version",
    )
    if cost_schema_version != _SUPPORTED_SCHEMA_VERSION:
        raise ValueError("detail 目前只支持 cost_schema_version=1")
    detail_id = _require_non_empty_str(payload.get("detail_id"), "detail.detail_id")
    batch_id = _require_non_empty_str(payload.get("batch_id"), "detail.batch_id")
    run_id = _require_non_empty_str(payload.get("run_id"), "detail.run_id")
    request_kind = _require_non_empty_str(payload.get("request_kind"), "detail.request_kind")
    if request_kind not in COST_REQUEST_KINDS:
        raise ValueError(f"detail.request_kind 不合法: {request_kind}")
    provider = _require_non_empty_str(payload.get("provider"), "detail.provider")
    model = _require_non_empty_str(payload.get("model"), "detail.model")
    usage_status = _require_non_empty_str(payload.get("usage_status"), "detail.usage_status")
    if usage_status not in {CostUsageStatus.known, CostUsageStatus.not_available}:
        raise ValueError(f"detail.usage_status 不合法: {usage_status}")
    pricing_status = _require_non_empty_str(payload.get("pricing_status"), "detail.pricing_status")
    if pricing_status not in {CostPricingStatus.known, CostPricingStatus.not_available}:
        raise ValueError(f"detail.pricing_status 不合法: {pricing_status}")
    price_source_ref = _require_non_empty_str(
        payload.get("price_source_ref"),
        "detail.price_source_ref",
    )
    price_as_of = _require_non_empty_str(payload.get("price_as_of"), "detail.price_as_of")
    currency = _require_non_empty_str(payload.get("currency"), "detail.currency")
    unit_cost = _require_optional_non_negative_number(payload.get("unit_cost"), "detail.unit_cost")
    total_cost = _require_optional_non_negative_number(payload.get("total_cost"), "detail.total_cost")
    usage = _validate_usage(payload.get("usage", {}), request_kind, "detail.usage")

    # usage_status 与 usage 内容必须双向一致，避免"声称缺失却附带数据"或"声称已知却为空"。
    if usage_status == CostUsageStatus.not_available and usage:
        raise ValueError("usage_status=not_available 时 usage 必须为空")
    if usage_status == CostUsageStatus.known and not usage:
        raise ValueError("usage_status=known 时 usage 必须包含至少一个计数")

    # 关键契约：金额必须与 usage/pricing 状态一致，禁止“缺失却填 0”。
    if usage_status == CostUsageStatus.not_available or pricing_status == CostPricingStatus.not_available:
        if total_cost is not None:
            raise ValueError(
                "usage_status 或 pricing_status 为 not_available 时 total_cost 必须为 null"
            )
        if pricing_status == CostPricingStatus.not_available and unit_cost is not None:
            raise ValueError("pricing_status=not_available 时 unit_cost 必须为 null")
    else:
        # 两者均 known 时 total_cost 必须是数字，不能为 null。
        if total_cost is None:
            raise ValueError("usage 与 pricing 均 known 时 total_cost 必须是数字")
        if unit_cost is None:
            raise ValueError("pricing_status=known 时 unit_cost 必须是数字")

    sampled_at = payload.get("sampled_at")
    if sampled_at is None:
        sampled_at = "not_available"
    sampled_at = _require_non_empty_str(sampled_at, "detail.sampled_at")

    return CostDetail(
        cost_schema_version=cost_schema_version,
        detail_id=detail_id,
        batch_id=batch_id,
        run_id=run_id,
        request_kind=request_kind,
        provider=provider,
        model=model,
        usage_status=usage_status,
        pricing_status=pricing_status,
        price_source_ref=price_source_ref,
        price_as_of=price_as_of,
        currency=currency,
        unit_cost=unit_cost,
        total_cost=total_cost,
        usage=usage,
        sampled_at=sampled_at,
    )


def parse_cost_details(payload: object) -> tuple[CostDetail, ...]:
    """解析 detail 文件；明细键必须唯一。"""

    if not isinstance(payload, dict):
        raise ValueError("detail 文件必须是 JSON 对象")
    scan_cost_payload(payload, "details")
    _assert_known_fields(payload, _DETAILS_FILE_FIELDS, "details")
    schema_version = _require_positive_int(payload.get("schema_version"), "details.schema_version")
    if schema_version != _SUPPORTED_SCHEMA_VERSION:
        raise ValueError("detail 文件目前只支持 schema_version=1")
    batch_id = _require_non_empty_str(payload.get("batch_id"), "details.batch_id")
    run_id = _require_non_empty_str(payload.get("run_id"), "details.run_id")
    details_raw = payload.get("details")
    if not isinstance(details_raw, list) or not details_raw:
        raise ValueError("details 必须是非空数组")
    details: list[CostDetail] = []
    seen: set[str] = set()
    for index, row in enumerate(details_raw):
        detail = parse_cost_detail(row)
        if detail.batch_id != batch_id:
            raise ValueError(f"details[{index}].batch_id 必须等于 details.batch_id")
        if detail.run_id != run_id:
            raise ValueError(f"details[{index}].run_id 必须等于 details.run_id")
        dedupe = cost_detail_key(detail.detail_id, detail.batch_id, detail.run_id)
        if dedupe in seen:
            raise ValueError(f"重复 detail 键: {dedupe}")
        seen.add(dedupe)
        details.append(detail)
    return tuple(details)


def load_cost_details(path: Path) -> tuple[tuple[CostDetail, ...], str, bytes]:
    """从磁盘读取 detail 文件，返回明细、内容 hash 与原始字节。"""

    raw_bytes = path.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    details = parse_cost_details(payload)
    return details, compute_sha256(raw_bytes), raw_bytes


def validate_cost_details_against_manifest(
    details: tuple[CostDetail, ...],
    manifest: CostManifest,
) -> None:
    """校验 detail 是否覆盖 manifest 冻结的 request_kinds 并遵守边界。"""

    if not details:
        raise ValueError("details 不能为空")
    for detail in details:
        if detail.batch_id != manifest.batch_id:
            raise ValueError("detail.batch_id 必须等于 manifest.batch_id")
        if detail.request_kind not in manifest.request_kinds:
            raise ValueError(
                f"detail {detail.detail_id} request_kind 不在 manifest 冻结集合中"
            )
        # 币种必须与 manifest 绑定一致，禁止跨币种合并金额。
        if detail.currency != manifest.currency:
            raise ValueError(
                f"detail {detail.detail_id} currency 必须等于 manifest.currency"
            )
        # 价格来源/日期必须与 manifest 一致，保证可追溯。
        if (
            detail.pricing_status == CostPricingStatus.known
            and detail.price_source_ref != manifest.price_source_ref
        ):
            raise ValueError(
                f"detail {detail.detail_id} price_source_ref 与 manifest 不一致"
            )
        if (
            detail.pricing_status == CostPricingStatus.known
            and detail.price_as_of != manifest.price_as_of
        ):
            raise ValueError(
                f"detail {detail.detail_id} price_as_of 与 manifest 不一致"
            )
    # 每个声明的 request_kind 至少有一条明细，否则不构成完整证据。
    covered = {detail.request_kind for detail in details}
    missing = sorted(set(manifest.request_kinds).difference(covered))
    if missing:
        raise ValueError(f"未覆盖 manifest 声明的 request_kinds: {missing}")
