"""M5.2 报告发布：raw/summary/decision 必须可互相重算且不可覆写。"""

# 导入 hashlib/json/re，保证 raw 指纹与稳定 JSON 写出。
import hashlib
import json
import re
# 导入 asdict，把场景身份转成 JSON 对象。
from dataclasses import asdict
# 导入 datetime/timezone，生成默认 run_id。
from datetime import datetime, timezone
# 导入 Path。
from pathlib import Path
# 导入 Sequence。
from collections.abc import Sequence
# 导入 Any。
from typing import Any

# 导入聚合、决策与 synthetic 识别。
from app.evaluation.load.aggregate import aggregate_load_raw
from app.evaluation.load.decision import build_load_decision_record, decide_load_verdict
from app.evaluation.load.manifest import (
    compute_sha256,
    is_synthetic_load_manifest,
    parse_load_raw,
    validate_load_raw_against_manifest,
)
from app.evaluation.load.scan import scan_load_payload
from app.evaluation.load.types import LoadManifest, LoadRawSample


# raw 报告顶层允许字段。
_LOAD_REPORT_FIELDS = {
    "schema_version",
    "run_mode",
    "run_id",
    "manifest_version",
    "batch_id",
    "load_schema_version",
    "tool_name",
    "tool_version",
    "environment_ref",
    "owner_confirmed",
    "owner_confirmation_ref",
    "matrix",
    "scenarios",
    "samples",
    "aggregate",
    "raw_sha256",
    "synthetic_only",
    "capacity_claim",
}
# decision 顶层允许字段。
_DECISION_FIELDS = {
    "schema_version",
    "run_mode",
    "evidence_kind",
    "run_id",
    "manifest_version",
    "batch_id",
    "load_schema_version",
    "tool_name",
    "tool_version",
    "environment_ref",
    "raw_sha256",
    "decision",
    "reasons",
    "matrix",
    "aggregate",
    "owner_confirmed",
    "owner_confirmation_ref",
    "synthetic_only",
    "capacity_claim",
    "scan_failed",
}
# 场景身份字段。
_SCENARIO_FIELDS = {
    "scenario_id",
    "endpoint_ref",
    "model_id",
    "corpus_or_tool_version",
    "request_fixture_sha256",
}
# raw 样本字段。
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
# 单档指标字段。
_BUCKET_FIELDS = {
    "sample_count",
    "success_count",
    "error_count",
    "error_rate",
    "p50_latency_ms",
    "p95_latency_ms",
    "p99_latency_ms",
    "p50_first_token_ms",
    "p95_first_token_ms",
    "throughput_rps",
    "error_codes",
    "cpu_pct_min",
    "cpu_pct_max",
    "rss_mb_min",
    "rss_mb_max",
    "expected_sample_count",
    "sample_count_ok",
}
# 聚合顶层字段。
_AGGREGATE_FIELDS = {
    "scenarios",
    "total_sample_count",
    "warmup_sample_count",
    "measurement_sample_count",
    "has_insufficient_samples",
}
# 矩阵字段。
_MATRIX_FIELDS = {
    "concurrency_levels",
    "warmup_count",
    "measurement_count",
    "window_seconds",
}
# run_id 只允许安全文件名字符。
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_run_id(run_id: str) -> None:
    """拒绝会改变报告输出目录的 run_id。"""

    if not _RUN_ID_PATTERN.fullmatch(run_id) or ".." in run_id:
        raise ValueError(f"run_id 不合法，只能使用字母/数字/./_/-，且不能包含 ..: {run_id}")


def _stable_json_bytes(payload: object) -> bytes:
    """用固定分隔符编码 JSON，保证 hash 可复现。"""

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def samples_to_jsonable(samples: Sequence[LoadRawSample]) -> list[dict[str, Any]]:
    """把 raw 样本转成 JSON 友好列表。"""

    return [asdict(item) for item in samples]


def raw_content_sha256(samples: Sequence[LoadRawSample]) -> str:
    """对样本内容计算 SHA-256。"""

    return hashlib.sha256(_stable_json_bytes(samples_to_jsonable(samples))).hexdigest()


def validate_report_payload(
    payload: object,
    allowed: set[str],
    path: str = "payload",
) -> None:
    """只校验当前对象层的字段白名单，避免父子层白名单互相污染。"""

    # 报告结构由调用方分层校验；这里只拒绝本层未知键。
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 必须是对象")
    unknown = sorted(set(payload).difference(allowed))
    if unknown:
        raise ValueError(f"{path} 包含白名单外字段: {unknown}")


def recompute_report_from_raw(
    manifest: LoadManifest,
    samples: Sequence[LoadRawSample],
    *,
    run_id: str,
    raw_file_sha256: str,
) -> dict[str, Any]:
    """从 raw 重算 aggregate 与 decision。"""

    # 公共重算入口也必须校验冻结批次，不能只依赖 CLI 的前置检查。
    validate_load_raw_against_manifest(tuple(samples), manifest)
    if raw_file_sha256 != manifest.raw_sha256:
        raise ValueError("raw_file_sha256 必须等于 manifest.raw_sha256")
    aggregate = aggregate_load_raw(samples, manifest)
    decision = build_load_decision_record(
        manifest,
        aggregate,
        raw_sha256=raw_file_sha256,
        run_id=run_id,
    )
    return {"aggregate": aggregate, "decision": decision}


def build_markdown_summary(
    manifest: LoadManifest,
    samples: Sequence[LoadRawSample],
    *,
    run_id: str,
    raw_file_sha256: str,
) -> str:
    """生成人读 summary.md。"""

    recomputed = recompute_report_from_raw(
        manifest,
        samples,
        run_id=run_id,
        raw_file_sha256=raw_file_sha256,
    )
    aggregate = recomputed["aggregate"]
    verdict = decide_load_verdict(manifest, aggregate)
    lines = [
        f"# M5.2 负载报告 `{run_id}`",
        "",
        f"- decision: **{verdict['decision']}**",
        f"- run_mode: `{manifest.run_mode}`",
        f"- batch_id: `{manifest.batch_id}`",
        f"- tool: `{manifest.tool_name}@{manifest.tool_version}`",
        f"- environment: `{manifest.environment_ref}`",
        f"- raw_sha256: `{raw_file_sha256}`",
        "- capacity_claim: `false`（本报告不外推生产容量）",
        "",
        "## 矩阵",
        f"- concurrency_levels: `{list(manifest.concurrency_levels)}`",
        f"- warmup_count: `{manifest.warmup_count}`",
        f"- measurement_count: `{manifest.measurement_count}`",
        f"- window_seconds: `{manifest.window_seconds}`",
        "",
        "## 场景指标",
    ]
    for scenario_id, concurrency_map in aggregate["scenarios"].items():
        lines.append(f"### {scenario_id}")
        for concurrency, metrics in concurrency_map.items():
            lines.extend(
                [
                    f"- c={concurrency}: sample={metrics['sample_count']}/"
                    f"{metrics['expected_sample_count']}, "
                    f"success={metrics['success_count']}, error={metrics['error_count']}, "
                    f"p50={metrics['p50_latency_ms']}, p95={metrics['p95_latency_ms']}, "
                    f"p99={metrics['p99_latency_ms']}, rps={metrics['throughput_rps']}",
                ]
            )
        lines.append("")
    lines.extend(["## 决策理由"])
    if verdict["reasons"]:
        lines.extend(f"- {reason}" for reason in verdict["reasons"])
    else:
        lines.append("- 全部门槛与 owner gate 已通过。")
    lines.extend(
        [
            "",
            "> 本报告只保存延迟/吞吐/错误码/资源数值与稳定 ID，不包含 query、回答正文、健康信息或密钥。",
            "> 本报告是本地单次受控证据，不是生产容量、SLA 或医学正确性结论。",
        ]
    )
    return "\n".join(lines) + "\n"


def publish_load_run_report(
    output_root: Path,
    manifest: LoadManifest,
    samples: Sequence[LoadRawSample],
    *,
    raw_bytes: bytes,
    run_id: str | None = None,
) -> Path:
    """写入不可覆写的 m5-load-<run-id> 目录。"""

    if run_id is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{manifest.manifest_version}-{stamp}"
    _validate_run_id(run_id)
    target = output_root / f"m5-load-{run_id}"
    if target.exists():
        raise FileExistsError(f"报告目录已存在，拒绝覆盖: {target}")

    if not isinstance(raw_bytes, bytes):
        raise TypeError("raw_bytes 必须是原始 JSON bytes")
    raw_file_sha256 = compute_sha256(raw_bytes)
    if raw_file_sha256 != manifest.raw_sha256:
        raise ValueError("raw_bytes 的 SHA-256 与 manifest.raw_sha256 不一致")
    try:
        parsed_samples = parse_load_raw(json.loads(raw_bytes.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("raw_bytes 不是有效的 M5.2 raw JSON") from exc
    if tuple(samples) != parsed_samples:
        raise ValueError("samples 必须与 raw_bytes 解析出的样本完全一致")
    validate_load_raw_against_manifest(parsed_samples, manifest)

    recomputed = recompute_report_from_raw(
        manifest,
        samples,
        run_id=run_id,
        raw_file_sha256=raw_file_sha256,
    )
    report_payload = {
        "schema_version": 1,
        "run_mode": manifest.run_mode,
        "run_id": run_id,
        "manifest_version": manifest.manifest_version,
        "batch_id": manifest.batch_id,
        "load_schema_version": manifest.load_schema_version,
        "tool_name": manifest.tool_name,
        "tool_version": manifest.tool_version,
        "environment_ref": manifest.environment_ref,
        "owner_confirmed": manifest.owner_confirmed,
        "owner_confirmation_ref": manifest.owner_confirmation_ref or None,
        "matrix": {
            "concurrency_levels": list(manifest.concurrency_levels),
            "warmup_count": manifest.warmup_count,
            "measurement_count": manifest.measurement_count,
            "window_seconds": manifest.window_seconds,
        },
        "scenarios": [asdict(item) for item in manifest.scenarios],
        "samples": samples_to_jsonable(samples),
        "aggregate": recomputed["aggregate"],
        "raw_sha256": raw_file_sha256,
        "synthetic_only": is_synthetic_load_manifest(manifest),
        "capacity_claim": False,
    }
    # 顶层字段白名单：先挡 schema 漂移。
    validate_report_payload(report_payload, _LOAD_REPORT_FIELDS, "report")
    # 场景身份列表逐项白名单。
    for index, scenario in enumerate(report_payload["scenarios"]):
        validate_report_payload(scenario, _SCENARIO_FIELDS, f"report.scenarios[{index}]")
    # raw 样本逐项白名单。
    for index, sample in enumerate(report_payload["samples"]):
        validate_report_payload(sample, _SAMPLE_FIELDS, f"report.samples[{index}]")
    # 矩阵对象白名单。
    validate_report_payload(report_payload["matrix"], _MATRIX_FIELDS, "report.matrix")
    # aggregate 顶层白名单。
    validate_report_payload(report_payload["aggregate"], _AGGREGATE_FIELDS, "report.aggregate")
    # scenario -> concurrency -> bucket 动态键单独校验，避免和场景身份字段白名单冲突。
    scenarios_metrics = report_payload["aggregate"]["scenarios"]
    if not isinstance(scenarios_metrics, dict):
        raise ValueError("aggregate.scenarios 必须是对象")
    for scenario_id, concurrency_map in scenarios_metrics.items():
        if not isinstance(concurrency_map, dict):
            raise ValueError(f"aggregate.scenarios.{scenario_id} 必须是对象")
        for concurrency, metrics in concurrency_map.items():
            validate_report_payload(
                metrics,
                _BUCKET_FIELDS,
                f"report.aggregate.scenarios.{scenario_id}.{concurrency}",
            )
    scan_load_payload(report_payload, "report")
    scan_load_payload(recomputed["decision"], "decision")
    validate_report_payload(recomputed["decision"], _DECISION_FIELDS, "decision")
    validate_report_payload(recomputed["decision"]["matrix"], _MATRIX_FIELDS, "decision.matrix")
    validate_report_payload(
        recomputed["decision"]["aggregate"],
        _AGGREGATE_FIELDS,
        "decision.aggregate",
    )
    decision_scenarios = recomputed["decision"]["aggregate"]["scenarios"]
    if not isinstance(decision_scenarios, dict):
        raise ValueError("decision.aggregate.scenarios 必须是对象")
    for scenario_id, concurrency_map in decision_scenarios.items():
        if not isinstance(concurrency_map, dict):
            raise ValueError(f"decision.aggregate.scenarios.{scenario_id} 必须是对象")
        for concurrency, metrics in concurrency_map.items():
            validate_report_payload(
                metrics,
                _BUCKET_FIELDS,
                f"decision.aggregate.scenarios.{scenario_id}.{concurrency}",
            )

    target.mkdir(parents=True)
    (target / "raw.json").write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (target / "summary.md").write_text(
        build_markdown_summary(
            manifest,
            samples,
            run_id=run_id,
            raw_file_sha256=raw_file_sha256,
        ),
        encoding="utf-8",
        newline="\n",
    )
    (target / "decision.json").write_text(
        json.dumps(recomputed["decision"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target
