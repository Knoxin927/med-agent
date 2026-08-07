"""M5.2 load manifest 与 raw 的解析、冻结与严格校验。"""

# 导入 hashlib/json/math/re，计算 hash、解析 JSON、校验数值与 SHA-256。
import hashlib
import json
import math
import re
from pathlib import Path

# 导入 M5.2 值对象。
from app.evaluation.load.scan import scan_load_payload
from app.evaluation.load.types import (
    LOAD_SCENARIO_IDS,
    LoadManifest,
    LoadPhase,
    LoadRawSample,
    LoadScenarioIdentity,
    load_sample_key,
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
    "load_schema_version",
    "tool_name",
    "tool_version",
    "environment_ref",
    "owner_confirmed",
    "owner_confirmation_ref",
    "concurrency_levels",
    "warmup_count",
    "measurement_count",
    "window_seconds",
    "scenarios",
    "raw_path",
    "raw_sha256",
}
# 单场景身份允许字段。
_SCENARIO_FIELDS = {
    "scenario_id",
    "endpoint_ref",
    "model_id",
    "corpus_or_tool_version",
    "request_fixture_sha256",
}
# raw 顶层允许字段。
_RAW_FIELDS = {
    "schema_version",
    "batch_id",
    "run_id",
    "samples",
}
# raw 单样本允许字段。
_SAMPLE_FIELDS = {
    "batch_id",
    "run_id",
    "scenario_id",
    "concurrency",
    "iteration",
    "phase",
    "status_code",
    "error_code",
    "start_monotonic_ms",
    "end_monotonic_ms",
    "full_latency_ms",
    "first_token_latency_ms",
    "cpu_pct",
    "rss_mb",
}


def compute_sha256(raw_bytes: bytes) -> str:
    """对冻结输入原始字节计算 SHA-256。"""

    return hashlib.sha256(raw_bytes).hexdigest()


def is_synthetic_load_manifest(manifest: LoadManifest) -> bool:
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


def parse_load_manifest(payload: object) -> LoadManifest:
    """把 manifest JSON 解析为不可变配置。"""

    if not isinstance(payload, dict):
        raise ValueError("manifest 必须是 JSON 对象")
    scan_load_payload(payload, "manifest")
    _assert_known_fields(
        payload,
        _MANIFEST_FIELDS,
        "manifest",
        nested_allowed={"scenarios": _SCENARIO_FIELDS},
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
    concurrency_raw = payload.get("concurrency_levels")
    if not isinstance(concurrency_raw, list) or not concurrency_raw:
        raise ValueError("concurrency_levels 必须是非空数组")
    concurrency_levels = tuple(
        _require_positive_int(item, f"concurrency_levels[{index}]")
        for index, item in enumerate(concurrency_raw)
    )
    if len(concurrency_levels) != len(set(concurrency_levels)):
        raise ValueError("concurrency_levels 不能包含重复项")
    scenarios_raw = payload.get("scenarios")
    if not isinstance(scenarios_raw, list) or not scenarios_raw:
        raise ValueError("scenarios 必须是非空数组")
    scenarios: list[LoadScenarioIdentity] = []
    seen_scenarios: set[str] = set()
    for index, row in enumerate(scenarios_raw):
        if not isinstance(row, dict):
            raise ValueError(f"scenarios[{index}] 必须是对象")
        _assert_known_fields(row, _SCENARIO_FIELDS, f"scenarios[{index}]")
        scenario_id = _require_non_empty_str(row.get("scenario_id"), f"scenarios[{index}].scenario_id")
        if scenario_id not in LOAD_SCENARIO_IDS:
            raise ValueError(f"scenarios[{index}].scenario_id 不在冻结集合中")
        if scenario_id in seen_scenarios:
            raise ValueError(f"重复 scenario_id: {scenario_id}")
        seen_scenarios.add(scenario_id)
        scenarios.append(
            LoadScenarioIdentity(
                scenario_id=scenario_id,
                endpoint_ref=_require_non_empty_str(
                    row.get("endpoint_ref"),
                    f"scenarios[{index}].endpoint_ref",
                ),
                model_id=_require_non_empty_str(
                    row.get("model_id"),
                    f"scenarios[{index}].model_id",
                ),
                corpus_or_tool_version=_require_non_empty_str(
                    row.get("corpus_or_tool_version"),
                    f"scenarios[{index}].corpus_or_tool_version",
                ),
                request_fixture_sha256=_require_sha256(
                    row.get("request_fixture_sha256"),
                    f"scenarios[{index}].request_fixture_sha256",
                ),
            )
        )
    manifest = LoadManifest(
        schema_version=schema_version,
        run_mode=run_mode,
        manifest_version=_require_non_empty_str(payload.get("manifest_version"), "manifest_version"),
        batch_id=_require_non_empty_str(payload.get("batch_id"), "batch_id"),
        load_schema_version=_require_non_empty_str(
            payload.get("load_schema_version"),
            "load_schema_version",
        ),
        tool_name=_require_non_empty_str(payload.get("tool_name"), "tool_name"),
        tool_version=_require_non_empty_str(payload.get("tool_version"), "tool_version"),
        environment_ref=_require_non_empty_str(payload.get("environment_ref"), "environment_ref"),
        owner_confirmed=owner_confirmed,
        owner_confirmation_ref=owner_confirmation_ref,
        concurrency_levels=concurrency_levels,
        warmup_count=_require_positive_int(payload.get("warmup_count"), "warmup_count"),
        measurement_count=_require_positive_int(
            payload.get("measurement_count"),
            "measurement_count",
        ),
        window_seconds=_require_non_negative_number(
            payload.get("window_seconds"),
            "window_seconds",
        ),
        scenarios=tuple(scenarios),
        raw_path=_require_non_empty_str(payload.get("raw_path"), "raw_path"),
        raw_sha256=_require_sha256(payload.get("raw_sha256"), "raw_sha256"),
    )
    if owner_confirmed and not owner_confirmation_ref:
        raise ValueError("owner_confirmed=true 时必须提供 owner_confirmation_ref")
    if owner_confirmed:
        if _looks_like_placeholder(owner_confirmation_ref):
            raise ValueError("owner_confirmed=true 时 owner_confirmation_ref 不能是占位符")
        if is_synthetic_load_manifest(manifest):
            raise ValueError("synthetic manifest 不能标记 owner_confirmed=true")
    return manifest


def load_load_manifest(path: Path, *, project_root: Path | None = None) -> LoadManifest:
    """从磁盘读取并解析 load manifest。"""

    raw_bytes = path.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    manifest = parse_load_manifest(payload)
    if project_root is not None:
        # raw_path 必须相对项目根可解析，防止路径穿越。
        raw_path = (project_root / manifest.raw_path).resolve()
        if project_root.resolve() not in raw_path.parents and raw_path != project_root.resolve():
            raise ValueError(f"raw_path 必须位于项目根下: {manifest.raw_path}")
    return manifest


def parse_load_raw(payload: object) -> tuple[LoadRawSample, ...]:
    """解析 raw samples；样本键必须唯一。"""

    if not isinstance(payload, dict):
        raise ValueError("raw 必须是 JSON 对象")
    scan_load_payload(payload, "raw")
    _assert_known_fields(
        payload,
        _RAW_FIELDS,
        "raw",
        nested_allowed={"samples": _SAMPLE_FIELDS},
    )
    schema_version = _require_positive_int(payload.get("schema_version"), "raw.schema_version")
    if schema_version != _SUPPORTED_SCHEMA_VERSION:
        raise ValueError("raw 目前只支持 schema_version=1")
    batch_id = _require_non_empty_str(payload.get("batch_id"), "raw.batch_id")
    run_id = _require_non_empty_str(payload.get("run_id"), "raw.run_id")
    samples_raw = payload.get("samples")
    if not isinstance(samples_raw, list) or not samples_raw:
        raise ValueError("raw.samples 必须是非空数组")
    samples: list[LoadRawSample] = []
    seen: set[str] = set()
    for index, row in enumerate(samples_raw):
        if not isinstance(row, dict):
            raise ValueError(f"raw.samples[{index}] 必须是对象")
        _assert_known_fields(row, _SAMPLE_FIELDS, f"raw.samples[{index}]")
        sample_batch = _require_non_empty_str(row.get("batch_id"), f"raw.samples[{index}].batch_id")
        sample_run = _require_non_empty_str(row.get("run_id"), f"raw.samples[{index}].run_id")
        if sample_batch != batch_id:
            raise ValueError(f"raw.samples[{index}].batch_id 必须等于 raw.batch_id")
        if sample_run != run_id:
            raise ValueError(f"raw.samples[{index}].run_id 必须等于 raw.run_id")
        scenario_id = _require_non_empty_str(
            row.get("scenario_id"),
            f"raw.samples[{index}].scenario_id",
        )
        if scenario_id not in LOAD_SCENARIO_IDS:
            raise ValueError(f"raw.samples[{index}].scenario_id 不合法")
        concurrency = _require_positive_int(
            row.get("concurrency"),
            f"raw.samples[{index}].concurrency",
        )
        iteration = _require_positive_int(
            row.get("iteration"),
            f"raw.samples[{index}].iteration",
        )
        phase = _require_non_empty_str(row.get("phase"), f"raw.samples[{index}].phase")
        if phase not in {LoadPhase.warmup, LoadPhase.measurement}:
            raise ValueError(f"raw.samples[{index}].phase 必须是 warmup 或 measurement")
        key = load_sample_key(sample_batch, scenario_id, concurrency, iteration, phase)
        if key in seen:
            raise ValueError(f"重复 raw 样本键: {key}")
        seen.add(key)
        status_code = row.get("status_code")
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            raise ValueError(f"raw.samples[{index}].status_code 必须是整数")
        error_code = row.get("error_code")
        if error_code is not None:
            error_code = _require_non_empty_str(
                error_code,
                f"raw.samples[{index}].error_code",
            )
        start_ms = _require_non_negative_number(
            row.get("start_monotonic_ms"),
            f"raw.samples[{index}].start_monotonic_ms",
        )
        end_ms = _require_non_negative_number(
            row.get("end_monotonic_ms"),
            f"raw.samples[{index}].end_monotonic_ms",
        )
        if end_ms < start_ms:
            raise ValueError(f"raw.samples[{index}] end_monotonic_ms 不能小于 start")
        full_latency = _require_non_negative_number(
            row.get("full_latency_ms"),
            f"raw.samples[{index}].full_latency_ms",
        )
        first_token = row.get("first_token_latency_ms")
        if first_token is not None:
            first_token = _require_non_negative_number(
                first_token,
                f"raw.samples[{index}].first_token_latency_ms",
            )
        samples.append(
            LoadRawSample(
                batch_id=sample_batch,
                run_id=sample_run,
                scenario_id=scenario_id,
                concurrency=concurrency,
                iteration=iteration,
                phase=phase,
                status_code=status_code,
                error_code=error_code,
                start_monotonic_ms=start_ms,
                end_monotonic_ms=end_ms,
                full_latency_ms=full_latency,
                first_token_latency_ms=first_token,
                cpu_pct=_require_non_negative_number(
                    row.get("cpu_pct"),
                    f"raw.samples[{index}].cpu_pct",
                ),
                rss_mb=_require_non_negative_number(
                    row.get("rss_mb"),
                    f"raw.samples[{index}].rss_mb",
                ),
            )
        )
    return tuple(samples)


def load_load_raw(path: Path) -> tuple[tuple[LoadRawSample, ...], str, bytes]:
    """从磁盘读取 raw，并返回样本、内容 hash 与原始字节。"""

    raw_bytes = path.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    samples = parse_load_raw(payload)
    return samples, compute_sha256(raw_bytes), raw_bytes


def validate_load_raw_against_manifest(
    samples: tuple[LoadRawSample, ...],
    manifest: LoadManifest,
) -> None:
    """校验 raw 是否覆盖 manifest 冻结的场景与并发矩阵。"""

    scenario_ids = {item.scenario_id for item in manifest.scenarios}
    observed_scenarios = {item.scenario_id for item in samples}
    if not observed_scenarios.issubset(scenario_ids):
        raise ValueError(
            f"raw 包含 manifest 未声明场景: {sorted(observed_scenarios.difference(scenario_ids))}"
        )
    for sample in samples:
        if sample.batch_id != manifest.batch_id:
            raise ValueError("raw.batch_id 必须等于 manifest.batch_id")
        if sample.concurrency not in manifest.concurrency_levels:
            raise ValueError(
                f"raw 样本 concurrency={sample.concurrency} 不在 manifest 矩阵中"
            )
        if sample.phase == LoadPhase.warmup and sample.iteration > manifest.warmup_count:
            raise ValueError("warmup iteration 超过 manifest.warmup_count")
        if (
            sample.phase == LoadPhase.measurement
            and sample.iteration > manifest.measurement_count
        ):
            raise ValueError("measurement iteration 超过 manifest.measurement_count")
