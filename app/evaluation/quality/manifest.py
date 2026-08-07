"""M5.1 quality manifest 与 projection 的解析、冻结与严格校验。"""

# 导入 hashlib/re，计算内容 hash 与校验 SHA-256 格式。
import hashlib
import json
import math
import re
from pathlib import Path

# 导入 M5.1 值对象。
from app.evaluation.quality.types import (
    QualityLayer,
    QualityManifest,
    QualityMethod,
    QualityMethodIdentity,
    QualityProjection,
    QualityProjectionRow,
    case_key_for,
)
from app.evaluation.quality.scan import scan_report_payload


# 当前只支持 schema_version=1 的 manifest 与 projection。
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
    "quality_schema_version",
    "grader_provider_version",
    "dataset_provenance_sha256",
    "reference_evidence_sha256",
    "manual_review_sha256",
    "owner_confirmed",
    "owner_confirmation_ref",
    "methods",
    "citation_coverage_threshold",
    "citation_support_threshold",
    "relevance_mean_threshold",
    "factuality_pass_rate_threshold",
}
# 单方法身份允许字段。
_METHOD_FIELDS = {
    "method",
    "run_id",
    "model_id",
    "tool_version",
    "corpus_version",
    "source_manifest_sha256",
    "reference_manifest_sha256",
    "projection_path",
    "projection_sha256",
    "task_ids",
    "repetitions",
}
# projection 顶层允许字段。
_PROJECTION_FIELDS = {
    "schema_version",
    "projection_schema_version",
    "batch_id",
    "rows",
}
# projection 单行允许字段；task_id/repetition/method 是配对必需身份，不是正文。
_PROJECTION_ROW_FIELDS = {
    "case_key",
    "batch_id",
    "run_id",
    "method",
    "task_id",
    "repetition",
    "layer",
    "input_hash",
    "answer_hash",
    "claim_id",
    "claim_hash",
    "source_id",
    "reference_id",
    "answer_text_redacted",
}


def compute_sha256(raw_bytes: bytes) -> str:
    """对冻结输入原始字节计算 SHA-256。"""

    return hashlib.sha256(raw_bytes).hexdigest()


def is_synthetic_quality_manifest(manifest: QualityManifest) -> bool:
    """识别仅用于工程验证的 synthetic manifest。"""

    # 只相信 manifest 显式声明的 run_mode，不用 model_id/manifest_version 的
    # 字符串猜测；否则 model_id 里带 "synthetic" 就能绕过 owner gate。
    return manifest.run_mode == "synthetic"


def _require_non_empty_str(value: object, field: str) -> str:
    """要求非空字符串，失败时给出稳定字段名。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _require_positive_int(value: object, field: str) -> int:
    """要求严格正整数，bool 不能被当作数字。"""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} 必须是正整数")
    return value


def _require_finite_number(
    value: object,
    field: str,
    *,
    minimum: float = 0.0,
    maximum: float = 1.0,
) -> float:
    """要求有限数值，用于阈值必须在 [0,1] 或 [0,2]。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数值")
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise ValueError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
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
    """按结构层递归拒绝未知字段，structural 列表使用独立白名单。"""

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


def parse_quality_manifest(payload: object) -> QualityManifest:
    """把 manifest JSON 解析为不可变配置。"""

    if not isinstance(payload, dict):
        raise ValueError("manifest 必须是 JSON 对象")
    # manifest 本身也可能被误放密钥/健康正文，解析入口同样 fail-closed。
    scan_report_payload(payload, "manifest")
    _assert_known_fields(
        payload,
        _MANIFEST_FIELDS,
        "manifest",
        nested_allowed={"methods": _METHOD_FIELDS},
    )
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
    methods_raw = payload.get("methods")
    if not isinstance(methods_raw, list) or not methods_raw:
        raise ValueError("methods 必须是非空数组")
    methods: list[QualityMethodIdentity] = []
    seen_methods: set[str] = set()
    for index, row in enumerate(methods_raw):
        if not isinstance(row, dict):
            raise ValueError(f"methods[{index}] 必须是对象")
        _assert_known_fields(row, _METHOD_FIELDS, f"methods[{index}]")
        method = _require_non_empty_str(row.get("method"), f"methods[{index}].method")
        if method not in {QualityMethod.dense, QualityMethod.agent}:
            raise ValueError(f"methods[{index}].method 必须为 dense 或 agent")
        if method in seen_methods:
            raise ValueError(f"重复 method: {method}")
        seen_methods.add(method)
        task_ids_raw = row.get("task_ids")
        if (
            not isinstance(task_ids_raw, list)
            or not task_ids_raw
            or any(not isinstance(item, str) or not item.strip() for item in task_ids_raw)
        ):
            raise ValueError(f"methods[{index}].task_ids 必须是非空字符串数组")
        task_ids = tuple(item.strip() for item in task_ids_raw)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError(f"methods[{index}].task_ids 不能包含重复项")
        methods.append(
            QualityMethodIdentity(
                method=method,
                run_id=_require_non_empty_str(row.get("run_id"), f"methods[{index}].run_id"),
                model_id=_require_non_empty_str(row.get("model_id"), f"methods[{index}].model_id"),
                tool_version=_require_non_empty_str(row.get("tool_version"), f"methods[{index}].tool_version"),
                corpus_version=_require_non_empty_str(row.get("corpus_version"), f"methods[{index}].corpus_version"),
                source_manifest_sha256=_require_sha256(
                    row.get("source_manifest_sha256"),
                    f"methods[{index}].source_manifest_sha256",
                ),
                reference_manifest_sha256=_require_sha256(
                    row.get("reference_manifest_sha256"),
                    f"methods[{index}].reference_manifest_sha256",
                ),
                projection_path=_require_non_empty_str(
                    row.get("projection_path"),
                    f"methods[{index}].projection_path",
                ),
                projection_sha256=_require_sha256(
                    row.get("projection_sha256"),
                    f"methods[{index}].projection_sha256",
                ),
                task_ids=task_ids,
                repetitions=_require_positive_int(
                    row.get("repetitions"),
                    f"methods[{index}].repetitions",
                ),
            )
        )
    manifest = QualityManifest(
        schema_version=schema_version,
        run_mode=run_mode,
        manifest_version=_require_non_empty_str(payload.get("manifest_version"), "manifest_version"),
        batch_id=_require_non_empty_str(payload.get("batch_id"), "batch_id"),
        quality_schema_version=_require_non_empty_str(
            payload.get("quality_schema_version"),
            "quality_schema_version",
        ),
        grader_provider_version=_require_non_empty_str(
            payload.get("grader_provider_version"),
            "grader_provider_version",
        ),
        dataset_provenance_sha256=_require_sha256(
            payload.get("dataset_provenance_sha256"),
            "dataset_provenance_sha256",
        ),
        reference_evidence_sha256=_require_sha256(
            payload.get("reference_evidence_sha256"),
            "reference_evidence_sha256",
        ),
        manual_review_sha256=_require_sha256(
            payload.get("manual_review_sha256"),
            "manual_review_sha256",
        ),
        owner_confirmed=owner_confirmed,
        owner_confirmation_ref=owner_confirmation_ref,
        methods=tuple(methods),
        citation_coverage_threshold=_require_finite_number(
            payload.get("citation_coverage_threshold"),
            "citation_coverage_threshold",
        ),
        citation_support_threshold=_require_finite_number(
            payload.get("citation_support_threshold"),
            "citation_support_threshold",
        ),
        relevance_mean_threshold=_require_finite_number(
            payload.get("relevance_mean_threshold"),
            "relevance_mean_threshold",
            minimum=0.0,
            maximum=2.0,
        ),
        factuality_pass_rate_threshold=_require_finite_number(
            payload.get("factuality_pass_rate_threshold"),
            "factuality_pass_rate_threshold",
        ),
    )
    if owner_confirmed and not owner_confirmation_ref:
        raise ValueError("owner_confirmed=true 时必须提供 owner_confirmation_ref")
    if owner_confirmed:
        if _looks_like_placeholder(owner_confirmation_ref):
            raise ValueError("owner_confirmed=true 时 owner_confirmation_ref 不能是占位符")
        if is_synthetic_quality_manifest(manifest):
            raise ValueError("synthetic manifest 不能标记 owner_confirmed=true")
    return manifest


def load_quality_manifest(path: Path, *, project_root: Path | None = None) -> QualityManifest:
    """加载 manifest JSON 并校验顶层结构。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("quality manifest 必须是 UTF-8 JSON") from error
    return parse_quality_manifest(payload)


def parse_quality_projection(payload: object) -> QualityProjection:
    """把 projection JSON 解析为不可变对象，拒绝白名单外字段。"""

    if not isinstance(payload, dict):
        raise ValueError("projection 必须是 JSON 对象")
    # projection 是第一个允许脱敏正文的输入层，必须在解析前扫描敏感内容。
    scan_report_payload(payload, "projection")
    _assert_known_fields(
        payload,
        _PROJECTION_FIELDS,
        "projection",
        nested_allowed={"rows": _PROJECTION_ROW_FIELDS},
    )
    schema_version = _require_positive_int(payload.get("schema_version"), "projection.schema_version")
    if schema_version != _SUPPORTED_SCHEMA_VERSION:
        raise ValueError("projection 目前只支持 schema_version=1")
    batch_id = _require_non_empty_str(payload.get("batch_id"), "projection.batch_id")
    rows_raw = payload.get("rows")
    if not isinstance(rows_raw, list) or not rows_raw:
        raise ValueError("projection.rows 必须是非空数组")
    rows: list[QualityProjectionRow] = []
    seen_claim_keys: set[str] = set()
    for index, row in enumerate(rows_raw):
        if not isinstance(row, dict):
            raise ValueError(f"projection.rows[{index}] 必须是对象")
        _assert_known_fields(row, _PROJECTION_ROW_FIELDS, f"projection.rows[{index}]")
        method = _require_non_empty_str(row.get("method"), f"projection.rows[{index}].method")
        if method not in {QualityMethod.dense, QualityMethod.agent}:
            raise ValueError(f"projection.rows[{index}].method 必须为 dense 或 agent")
        task_id = _require_non_empty_str(row.get("task_id"), f"projection.rows[{index}].task_id")
        repetition = _require_positive_int(
            row.get("repetition"),
            f"projection.rows[{index}].repetition",
        )
        layer = _require_non_empty_str(row.get("layer"), f"projection.rows[{index}].layer")
        if layer not in {QualityLayer.shared, QualityLayer.agent_only}:
            raise ValueError(f"projection.rows[{index}].layer 不合法")
        claim_id = _require_non_empty_str(row.get("claim_id"), f"projection.rows[{index}].claim_id")
        from app.evaluation.quality.types import quality_claim_key

        claim_key = quality_claim_key(batch_id, method, task_id, repetition, claim_id)
        if claim_key in seen_claim_keys:
            raise ValueError(f"重复声明键: {claim_key}")
        seen_claim_keys.add(claim_key)
        source_id = row.get("source_id")
        reference_id = row.get("reference_id")
        if source_id is not None and (not isinstance(source_id, str) or not source_id.strip()):
            raise ValueError(f"projection.rows[{index}].source_id 必须为非空字符串或 null")
        if reference_id is not None and (not isinstance(reference_id, str) or not reference_id.strip()):
            raise ValueError(f"projection.rows[{index}].reference_id 必须为非空字符串或 null")
        answer_text_redacted = row.get("answer_text_redacted")
        if answer_text_redacted is not None and (
            not isinstance(answer_text_redacted, str) or not answer_text_redacted.strip()
        ):
            raise ValueError(f"projection.rows[{index}].answer_text_redacted 必须为非空字符串或 null")
        expected_case_key = case_key_for(batch_id, task_id, repetition)
        actual_case_key = _require_non_empty_str(
            row.get("case_key"),
            f"projection.rows[{index}].case_key",
        )
        if actual_case_key != expected_case_key:
            raise ValueError(
                f"projection.rows[{index}].case_key 必须为 {expected_case_key}"
            )
        rows.append(
            QualityProjectionRow(
                case_key=actual_case_key,
                batch_id=batch_id,
                run_id=_require_non_empty_str(row.get("run_id"), f"projection.rows[{index}].run_id"),
                method=method,
                task_id=task_id,
                repetition=repetition,
                layer=layer,
                input_hash=_require_sha256(row.get("input_hash"), f"projection.rows[{index}].input_hash"),
                answer_hash=_require_sha256(row.get("answer_hash"), f"projection.rows[{index}].answer_hash"),
                claim_id=claim_id,
                claim_hash=_require_sha256(row.get("claim_hash"), f"projection.rows[{index}].claim_hash"),
                source_id=source_id.strip() if isinstance(source_id, str) else None,
                reference_id=reference_id.strip() if isinstance(reference_id, str) else None,
                answer_text_redacted=(
                    answer_text_redacted.strip()
                    if isinstance(answer_text_redacted, str)
                    else None
                ),
            )
        )
    return QualityProjection(
        schema_version=schema_version,
        projection_schema_version=_require_non_empty_str(
            payload.get("projection_schema_version"),
            "projection.projection_schema_version",
        ),
        batch_id=batch_id,
        rows=tuple(rows),
    )


def load_quality_projection(path: Path) -> tuple[QualityProjection, str, bytes]:
    """加载 projection 文件，返回对象、原始字节 hash 与原始字节。"""

    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("quality projection 必须是 UTF-8 JSON") from error
    return parse_quality_projection(payload), compute_sha256(raw), raw


def validate_quality_projection(
    projection: QualityProjection,
    manifest: QualityManifest,
) -> None:
    """校验 projection 与 manifest 的批次、run_id、task 与重复次数绑定。"""

    if projection.batch_id != manifest.batch_id:
        raise ValueError("projection.batch_id 与 manifest 不一致")
    if projection.projection_schema_version != manifest.quality_schema_version:
        raise ValueError("projection.projection_schema_version 与 manifest 不一致")
    methods_by_name = {identity.method: identity for identity in manifest.methods}
    for row in projection.rows:
        identity = methods_by_name.get(row.method)
        if identity is None:
            raise ValueError(f"manifest 缺少 method={row.method}")
        if row.run_id != identity.run_id:
            raise ValueError(
                f"{row.method} projection.run_id 与 manifest 不一致"
            )
        if row.task_id not in identity.task_ids:
            raise ValueError(
                f"{row.method} projection 包含 manifest 未声明的 task_id={row.task_id}"
            )
        if row.repetition > identity.repetitions:
            raise ValueError(
                f"{row.method} projection repetition 超过 manifest 配置"
            )
        # agent-only 是 Agent 专属层，dense 固定 RAG 不能混入，保证分母语义。
        if row.method == "dense" and row.layer == "agent-only":
            raise ValueError("dense projection 不能包含 agent-only 层")
