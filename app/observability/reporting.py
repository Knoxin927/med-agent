"""M5.3 报告发布：events/metrics/summary/decision 必须可互相重算且不可覆写。"""

# 导入 json/re，保证稳定 JSON 写出与 run_id 校验。
import json
import re
# 导入 asdict，把事件转成 JSON 对象。
from dataclasses import asdict
# 导入 datetime/timezone，生成默认 run_id。
from datetime import datetime, timezone
# 导入 Path。
from pathlib import Path
# 导入 Sequence。
from collections.abc import Sequence
# 导入 Any。
from typing import Any

# 导入决策层。
from app.observability.decision import (
    build_observability_decision_record,
    decide_observability_verdict,
)
# 导入事件解析与 hash。
from app.observability.events import (
    compute_sha256,
    is_synthetic_observability_manifest,
    parse_observability_events,
)
# 导入指标重算。
from app.observability.metrics import build_metrics_from_events
# 导入关联导出。
from app.observability.recorder import correlate_events
# 导入扫描。
from app.observability.scan import scan_observability_payload
# 导入值对象。
from app.observability.types import ObservabilityEvent, ObservabilityManifest


# events 报告顶层允许字段。
_EVENTS_REPORT_FIELDS = {
    "schema_version",
    "run_mode",
    "run_id",
    "manifest_version",
    "batch_id",
    "event_schema_version",
    "sample_rate",
    "retention_days",
    "label_cardinality_limit",
    "owner_confirmed",
    "owner_confirmation_ref",
    "events",
    "metrics",
    "correlation",
    "events_sha256",
    "synthetic_only",
    "production_logging_claim",
}
# decision 顶层允许字段。
_DECISION_FIELDS = {
    "schema_version",
    "run_mode",
    "evidence_kind",
    "run_id",
    "manifest_version",
    "batch_id",
    "event_schema_version",
    "events_sha256",
    "decision",
    "reasons",
    "policy",
    "event_count",
    "request_kind_counts",
    "status_counts",
    "metrics",
    "owner_confirmed",
    "owner_confirmation_ref",
    "synthetic_only",
    "production_logging_claim",
    "scan_failed",
    "contract_violation_count",
}
# 单条事件字段。
_EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "run_id",
    "call_id",
    "request_kind",
    "tool_name",
    "status",
    "error_code",
    "duration_ms",
    "step_count",
    "model_id",
    "tool_version",
    "corpus_version",
    "sampled_at",
}
# 指标快照字段。
_METRICS_FIELDS = {
    "counters",
    "contract_violation_count",
    "label_cardinality",
    "label_cardinality_limit",
}
# 策略字段。
_POLICY_FIELDS = {
    "sample_rate",
    "retention_days",
    "label_cardinality_limit",
}
# run_id 只允许安全文件名字符。
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_run_id(run_id: str) -> None:
    """拒绝会改变报告输出目录的 run_id。"""

    if not _RUN_ID_PATTERN.fullmatch(run_id) or ".." in run_id:
        raise ValueError(
            f"run_id 不合法，只能使用字母/数字/./_/-，且不能包含 ..: {run_id}"
        )


def events_to_jsonable(events: Sequence[ObservabilityEvent]) -> list[dict[str, Any]]:
    """把事件转成 JSON 友好列表。"""

    return [asdict(item) for item in events]


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


def recompute_report_from_events(
    manifest: ObservabilityManifest,
    events: Sequence[ObservabilityEvent],
    *,
    events_sha256: str,
    run_id: str,
    scan_failed: bool = False,
    contract_violation_count: int = 0,
) -> dict[str, Any]:
    """从合法事件重算 metrics 与 decision，保证报告可重算。"""

    metrics = build_metrics_from_events(
        events,
        label_cardinality_limit=manifest.label_cardinality_limit,
    )
    # 如果调用方在入仓前已累计过违规，合并进快照；合法 events 重算本身不会产生 violation。
    if contract_violation_count > 0:
        metrics = {
            **metrics,
            "contract_violation_count": (
                int(metrics.get("contract_violation_count", 0)) + contract_violation_count
            ),
        }
    decision = build_observability_decision_record(
        manifest,
        events,
        metrics,
        events_sha256=events_sha256,
        run_id=run_id,
        scan_failed=scan_failed,
    )
    return {
        "metrics": metrics,
        "decision": decision,
        "correlation": correlate_events(events),
    }


def build_markdown_summary(
    manifest: ObservabilityManifest,
    events: Sequence[ObservabilityEvent],
    *,
    run_id: str,
    events_sha256: str,
    metrics: dict[str, Any],
    decision: dict[str, Any],
) -> str:
    """生成人读 summary.md。"""

    request_kind_counts = decision.get("request_kind_counts", {})
    status_counts = decision.get("status_counts", {})
    lines = [
        "# M5.3 可观测性报告",
        "",
        f"- run_id: `{run_id}`",
        f"- run_mode: `{manifest.run_mode}`",
        f"- manifest_version: `{manifest.manifest_version}`",
        f"- batch_id: `{manifest.batch_id}`",
        f"- events_sha256: `{events_sha256}`",
        f"- event_count: **{len(events)}**",
        f"- decision: **{decision.get('decision')}**",
        f"- synthetic_only: `{decision.get('synthetic_only')}`",
        f"- production_logging_claim: `{decision.get('production_logging_claim')}`",
        f"- contract_violation_count: `{decision.get('contract_violation_count')}`",
        f"- sample_rate: `{manifest.sample_rate}`",
        f"- retention_days: `{manifest.retention_days}`",
        f"- label_cardinality_limit: `{manifest.label_cardinality_limit}`",
        "",
        "## request_kind 覆盖",
        "",
    ]
    for kind, count in sorted(request_kind_counts.items()):
        lines.append(f"- `{kind}`: {count}")
    lines.extend(["", "## status 分布", ""])
    for status, count in sorted(status_counts.items()):
        lines.append(f"- `{status}`: {count}")
    lines.extend(
        [
            "",
            "## 决策原因",
            "",
        ]
    )
    for reason in decision.get("reasons", []):
        lines.append(f"- {reason}")
    lines.extend(
        [
            "",
            "## 边界声明",
            "",
            "- 本报告只证明本地脱敏事件与指标契约可运行。",
            "- 不代表生产日志级别、保留策略或外部监控已接入。",
            "- query / 回答正文 / 健康信息 / 密钥不会进入 events、metrics 或 decision。",
            "",
        ]
    )
    # 额外给出 metric 计数，方便面试时口述“本地 registry 怎么汇总”。
    counters = metrics.get("counters", {})
    if isinstance(counters, dict) and counters:
        lines.extend(["## 指标摘要", ""])
        for name, rows in sorted(counters.items()):
            total = 0
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and isinstance(row.get("value"), (int, float)):
                        total += row["value"]
            lines.append(f"- `{name}` total={total}")
        lines.append("")
    return "\n".join(lines)


def publish_observability_run_report(
    output_root: Path,
    manifest: ObservabilityManifest,
    events: Sequence[ObservabilityEvent],
    *,
    events_bytes: bytes,
    run_id: str | None = None,
    scan_failed: bool = False,
    contract_violation_count: int = 0,
) -> Path:
    """发布不可覆写的 m5-observability-<run-id> 报告目录。"""

    if run_id is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{manifest.manifest_version}-{stamp}"
    _validate_run_id(run_id)

    # 用原始字节计算 hash，保证与 manifest.events_sha256 可对账。
    events_file_sha256 = compute_sha256(events_bytes)
    if events_file_sha256 != manifest.events_sha256:
        raise ValueError("events 原始字节 SHA-256 与 manifest.events_sha256 不一致")

    # 再次从原始 bytes 解析，防止调用方传入的 events 对象与磁盘内容漂移。
    reparsed = parse_observability_events(json.loads(events_bytes.decode("utf-8")))
    if len(reparsed) != len(events):
        raise ValueError("events 对象数量与 events_bytes 解析结果不一致")
    for left, right in zip(events, reparsed, strict=True):
        if asdict(left) != asdict(right):
            raise ValueError("events 对象内容与 events_bytes 解析结果不一致")

    # 对整批事件做敏感扫描；失败则强制 hold，且绝不写出敏感字段。
    effective_scan_failed = scan_failed
    try:
        for index, event in enumerate(events):
            scan_observability_payload(asdict(event), f"events[{index}]")
    except Exception:
        effective_scan_failed = True

    recomputed = recompute_report_from_events(
        manifest,
        events,
        events_sha256=events_file_sha256,
        run_id=run_id,
        scan_failed=effective_scan_failed,
        contract_violation_count=contract_violation_count,
    )

    report_payload = {
        "schema_version": 1,
        "run_mode": manifest.run_mode,
        "run_id": run_id,
        "manifest_version": manifest.manifest_version,
        "batch_id": manifest.batch_id,
        "event_schema_version": manifest.event_schema_version,
        "sample_rate": manifest.sample_rate,
        "retention_days": manifest.retention_days,
        "label_cardinality_limit": manifest.label_cardinality_limit,
        "owner_confirmed": manifest.owner_confirmed,
        "owner_confirmation_ref": manifest.owner_confirmation_ref or None,
        "events": events_to_jsonable(events),
        "metrics": recomputed["metrics"],
        "correlation": recomputed["correlation"],
        "events_sha256": events_file_sha256,
        "synthetic_only": is_synthetic_observability_manifest(manifest),
        "production_logging_claim": False,
    }

    # 分层白名单校验，避免父子字段互相污染。
    validate_report_payload(report_payload, _EVENTS_REPORT_FIELDS, "events_report")
    validate_report_payload(report_payload["metrics"], _METRICS_FIELDS, "events_report.metrics")
    for index, event in enumerate(report_payload["events"]):
        validate_report_payload(event, _EVENT_FIELDS, f"events_report.events[{index}]")
    validate_report_payload(recomputed["decision"], _DECISION_FIELDS, "decision")
    validate_report_payload(
        recomputed["decision"]["policy"],
        _POLICY_FIELDS,
        "decision.policy",
    )
    validate_report_payload(
        recomputed["decision"]["metrics"],
        _METRICS_FIELDS,
        "decision.metrics",
    )
    # decision 必须与本地 recompute 一致，防止手写 decision 漂移。
    local_verdict = decide_observability_verdict(
        manifest,
        events,
        recomputed["metrics"],
        scan_failed=effective_scan_failed,
    )
    if recomputed["decision"]["decision"] != local_verdict["decision"]:
        raise ValueError("decision 与本地 recompute 不一致")

    target = output_root / f"m5-observability-{run_id}"
    if target.exists():
        raise FileExistsError(f"报告目录已存在，禁止覆写: {target}")

    target.mkdir(parents=True)
    (target / "events.json").write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (target / "metrics.json").write_text(
        json.dumps(recomputed["metrics"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (target / "summary.md").write_text(
        build_markdown_summary(
            manifest,
            events,
            run_id=run_id,
            events_sha256=events_file_sha256,
            metrics=recomputed["metrics"],
            decision=recomputed["decision"],
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
