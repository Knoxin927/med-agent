"""M5.4 报告发布：raw/summary/decision 必须可互相重算且不可覆写。"""

# 导入 hashlib/json/re，保证 raw 指纹与稳定 JSON 写出。
import hashlib
import json
import re
# 导入 asdict，把事件与 value 转成 JSON 对象。
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
from app.evaluation.cache.aggregate import aggregate_cache_raw
from app.evaluation.cache.decision import build_cache_decision_record, decide_cache_verdict
from app.evaluation.cache.manifest import (
    compute_sha256,
    is_synthetic_cache_manifest,
    parse_cache_raw,
    validate_cache_raw_against_manifest,
)
from app.evaluation.cache.scan import scan_cache_payload
from app.evaluation.cache.types import CACHE_VALUE_FIELDS, CacheManifest, CacheRawEvent


# raw 报告顶层允许字段。
_CACHE_REPORT_FIELDS = {
    "schema_version",
    "run_mode",
    "run_id",
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
    "events",
    "aggregate",
    "raw_sha256",
    "synthetic_only",
    "hot_path_claim",
}
# decision 顶层允许字段。
_DECISION_FIELDS = {
    "schema_version",
    "run_mode",
    "evidence_kind",
    "run_id",
    "manifest_version",
    "batch_id",
    "cache_schema_version",
    "raw_sha256",
    "decision",
    "reasons",
    "policy",
    "aggregate",
    "owner_confirmed",
    "owner_confirmation_ref",
    "synthetic_only",
    "hot_path_claim",
    "default_bypass",
    "scan_failed",
}
# 单事件字段。
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
# 聚合顶层字段。
_AGGREGATE_FIELDS = {
    "total_event_count",
    "outcome_counts",
    "method_counts",
    "attempt_count",
    "hit_count",
    "hit_rate",
    "miss_rate",
    "eviction_count",
    "bypass_count",
    "p50_latency_ms",
    "p95_latency_ms",
    "p50_get_latency_ms",
    "p95_get_latency_ms",
    "p50_set_latency_ms",
    "p95_set_latency_ms",
    "p50_hit_latency_ms",
    "p50_miss_like_latency_ms",
    "max_namespace_size_after",
    "manifest_key_version",
    "manifest_max_entries",
    "manifest_default_ttl_seconds",
    "default_bypass",
    "required_evidence",
    "missing_evidence",
    "has_complete_evidence",
}
# policy 字段。
_POLICY_FIELDS = {
    "key_version",
    "default_ttl_seconds",
    "max_entries",
    "default_bypass",
    "secret_source_ref",
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


def events_to_jsonable(events: Sequence[CacheRawEvent]) -> list[dict[str, Any]]:
    """把 raw 事件转成 JSON 友好列表。"""

    rows: list[dict[str, Any]] = []
    for item in events:
        row = asdict(item)
        rows.append(row)
    return rows


def raw_content_sha256(events: Sequence[CacheRawEvent]) -> str:
    """对事件内容计算 SHA-256。"""

    return hashlib.sha256(_stable_json_bytes(events_to_jsonable(events))).hexdigest()


def validate_report_payload(
    payload: object,
    allowed: set[str],
    path: str = "payload",
) -> None:
    """只校验当前对象层的字段白名单，避免父子层白名单互相污染。"""

    if not isinstance(payload, dict):
        raise ValueError(f"{path} 必须是对象")
    unknown = sorted(set(payload).difference(allowed))
    if unknown:
        raise ValueError(f"{path} 包含白名单外字段: {unknown}")


def recompute_report_from_raw(
    manifest: CacheManifest,
    events: Sequence[CacheRawEvent],
    *,
    run_id: str,
    raw_file_sha256: str,
) -> dict[str, Any]:
    """从 raw 重算 aggregate 与 decision。"""

    validate_cache_raw_against_manifest(tuple(events), manifest)
    if raw_file_sha256 != manifest.raw_sha256:
        raise ValueError("raw_file_sha256 必须等于 manifest.raw_sha256")
    aggregate = aggregate_cache_raw(events, manifest)
    decision = build_cache_decision_record(
        manifest,
        aggregate,
        raw_sha256=raw_file_sha256,
        run_id=run_id,
    )
    return {"aggregate": aggregate, "decision": decision}


def build_markdown_summary(
    manifest: CacheManifest,
    events: Sequence[CacheRawEvent],
    *,
    run_id: str,
    raw_file_sha256: str,
) -> str:
    """生成人读 summary.md。"""

    recomputed = recompute_report_from_raw(
        manifest,
        events,
        run_id=run_id,
        raw_file_sha256=raw_file_sha256,
    )
    aggregate = recomputed["aggregate"]
    verdict = decide_cache_verdict(manifest, aggregate)
    lines = [
        f"# M5.4 缓存策略报告 `{run_id}`",
        "",
        f"- decision: **{verdict['decision']}**",
        f"- run_mode: `{manifest.run_mode}`",
        f"- batch_id: `{manifest.batch_id}`",
        f"- key_version: `{manifest.key_version}`",
        f"- corpus/model/tool: `{manifest.corpus_version}` / `{manifest.model_version}` / `{manifest.tool_version}`",
        f"- default_bypass: `{manifest.default_bypass}`",
        f"- secret_source_ref: `{manifest.secret_source_ref}`",
        f"- raw_sha256: `{raw_file_sha256}`",
        "- hot_path_claim: `false`（本报告不声明缓存可接入生产热路径）",
        "",
        "## 指标",
        f"- total_event_count: `{aggregate['total_event_count']}`",
        f"- attempt_count: `{aggregate['attempt_count']}`",
        f"- hit_count: `{aggregate['hit_count']}`",
        f"- hit_rate: `{aggregate['hit_rate']}`",
        f"- miss_rate: `{aggregate['miss_rate']}`",
        f"- eviction_count: `{aggregate['eviction_count']}`",
        f"- bypass_count: `{aggregate['bypass_count']}`",
        f"- p50_latency_ms: `{aggregate['p50_latency_ms']}`",
        f"- p95_latency_ms: `{aggregate['p95_latency_ms']}`",
        f"- outcome_counts: `{aggregate['outcome_counts']}`",
        "",
        "## 决策理由",
    ]
    if verdict["reasons"]:
        lines.extend(f"- {reason}" for reason in verdict["reasons"])
    else:
        lines.append("- 全部门槛与 owner gate 已通过。")
    lines.extend(
        [
            "",
            "> 本报告只保存脱敏 cache key/value 投影、命中/失效与延迟数值，不包含 query、回答正文、健康信息或密钥。",
            "> 本报告是本地合成/受控实验证据，不是生产缓存就绪、SLA 或医学正确性结论。",
        ]
    )
    return "\n".join(lines) + "\n"


def publish_cache_run_report(
    output_root: Path,
    manifest: CacheManifest,
    events: Sequence[CacheRawEvent],
    *,
    raw_bytes: bytes,
    run_id: str | None = None,
) -> Path:
    """写入不可覆写的 m5-cache-<run-id> 目录。"""

    if run_id is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{manifest.manifest_version}-{stamp}"
    _validate_run_id(run_id)
    target = output_root / f"m5-cache-{run_id}"
    if target.exists():
        raise FileExistsError(f"报告目录已存在，拒绝覆盖: {target}")

    if not isinstance(raw_bytes, bytes):
        raise TypeError("raw_bytes 必须是原始 JSON bytes")
    raw_file_sha256 = compute_sha256(raw_bytes)
    if raw_file_sha256 != manifest.raw_sha256:
        raise ValueError("raw_bytes 的 SHA-256 与 manifest.raw_sha256 不一致")
    try:
        parsed_events = parse_cache_raw(json.loads(raw_bytes.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("raw_bytes 不是有效的 M5.4 raw JSON") from exc
    if tuple(events) != parsed_events:
        raise ValueError("events 必须与 raw_bytes 解析出的事件完全一致")
    validate_cache_raw_against_manifest(parsed_events, manifest)

    recomputed = recompute_report_from_raw(
        manifest,
        events,
        run_id=run_id,
        raw_file_sha256=raw_file_sha256,
    )
    report_payload = {
        "schema_version": 1,
        "run_mode": manifest.run_mode,
        "run_id": run_id,
        "manifest_version": manifest.manifest_version,
        "batch_id": manifest.batch_id,
        "cache_schema_version": manifest.cache_schema_version,
        "key_version": manifest.key_version,
        "corpus_version": manifest.corpus_version,
        "model_version": manifest.model_version,
        "tool_version": manifest.tool_version,
        "default_ttl_seconds": manifest.default_ttl_seconds,
        "max_entries": manifest.max_entries,
        "default_bypass": manifest.default_bypass,
        "secret_source_ref": manifest.secret_source_ref,
        "owner_confirmed": manifest.owner_confirmed,
        "owner_confirmation_ref": manifest.owner_confirmation_ref or None,
        "events": events_to_jsonable(events),
        "aggregate": recomputed["aggregate"],
        "raw_sha256": raw_file_sha256,
        "synthetic_only": is_synthetic_cache_manifest(manifest),
        "hot_path_claim": False,
    }
    # 顶层字段白名单：先挡 schema 漂移。
    validate_report_payload(report_payload, _CACHE_REPORT_FIELDS, "report")
    # 事件逐项白名单。
    for index, event in enumerate(report_payload["events"]):
        validate_report_payload(event, _EVENT_FIELDS, f"report.events[{index}]")
        if event.get("value") is not None:
            validate_report_payload(
                event["value"],
                CACHE_VALUE_FIELDS,
                f"report.events[{index}].value",
            )
    # aggregate 顶层白名单。
    validate_report_payload(report_payload["aggregate"], _AGGREGATE_FIELDS, "report.aggregate")
    scan_cache_payload(report_payload, "report")
    scan_cache_payload(recomputed["decision"], "decision")
    validate_report_payload(recomputed["decision"], _DECISION_FIELDS, "decision")
    validate_report_payload(recomputed["decision"]["policy"], _POLICY_FIELDS, "decision.policy")
    validate_report_payload(
        recomputed["decision"]["aggregate"],
        _AGGREGATE_FIELDS,
        "decision.aggregate",
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
            events,
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
