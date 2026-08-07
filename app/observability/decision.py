"""M5.3 pass/hold 决策：生产日志装配 owner gate 未批准前禁止生产 pass。"""

# 导入 Sequence，统计事件类型分布。
from collections.abc import Sequence
# 导入 Any，输出 JSON 友好决策记录。
from typing import Any

# 导入 synthetic 识别，避免合成证据伪装成生产 pass。
from app.observability.events import is_synthetic_observability_manifest
# 导入事件与 manifest 值对象。
from app.observability.types import ObservabilityEvent, ObservabilityManifest


def decide_observability_verdict(
    manifest: ObservabilityManifest,
    events: Sequence[ObservabilityEvent],
    metrics: dict[str, Any],
    *,
    scan_failed: bool = False,
    contract_violation_count: int | None = None,
) -> dict[str, Any]:
    """按扫描、契约违规与 owner gate 返回 decision 与 reasons。"""

    # 收集会阻止任何 pass 的独立原因。
    reasons: list[str] = []
    # 扫描失败是最高优先级安全门：敏感内容绝不能进证据。
    if scan_failed:
        reasons.append("敏感字段或未知字段扫描失败")
    # 指标快照里的违规计数；调用方也可显式传入。
    violation_count = (
        metrics.get("contract_violation_count", 0)
        if contract_violation_count is None
        else contract_violation_count
    )
    if not isinstance(violation_count, int) or violation_count < 0:
        raise ValueError("contract_violation_count 必须是非负整数")
    if violation_count > 0:
        reasons.append(f"存在 {violation_count} 次 observability_contract_violation")
    # 空事件批次无法证明 allowlist/关联键契约。
    if not events:
        reasons.append("事件列表为空，无法形成可观测性证据")

    # 指标原因先独立判定，避免和 owner gate 混在一起。
    metrics_ok = not reasons
    gate_reasons: list[str] = []
    if not manifest.owner_confirmed:
        gate_reasons.append("owner_confirmed=false：禁止生产 pass")
    else:
        # owner_confirmed 只证明 manifest 被确认，不等于生产日志/保留策略已批准。
        gate_reasons.append(
            "production observability owner gate 未批准：当前禁止生产 pass"
        )

    # 合成 manifest 且指标过线时，只允许 synthetic_only 工程证据。
    if metrics_ok and is_synthetic_observability_manifest(manifest):
        decision = "synthetic_only"
        reasons = [
            "production observability owner gate pending：仅合成工程证据，不是生产日志/监控结论"
        ]
    else:
        decision = "hold"
        reasons = [*reasons, *gate_reasons]

    return {
        # 当前只能返回 synthetic_only 或 hold，永远不会返回生产 pass。
        "decision": decision,
        "reasons": reasons,
        "policy": {
            "sample_rate": manifest.sample_rate,
            "retention_days": manifest.retention_days,
            "label_cardinality_limit": manifest.label_cardinality_limit,
        },
    }


def build_observability_decision_record(
    manifest: ObservabilityManifest,
    events: Sequence[ObservabilityEvent],
    metrics: dict[str, Any],
    *,
    events_sha256: str,
    run_id: str,
    scan_failed: bool = False,
    contract_violation_count: int | None = None,
) -> dict[str, Any]:
    """生成可入库的 M5.3 decision record。"""

    verdict = decide_observability_verdict(
        manifest,
        events,
        metrics,
        scan_failed=scan_failed,
        contract_violation_count=contract_violation_count,
    )
    # 按 request_kind 统计，便于 summary 一眼看出三类协议是否都覆盖到。
    request_kind_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for event in events:
        request_kind_counts[event.request_kind] = (
            request_kind_counts.get(event.request_kind, 0) + 1
        )
        status_counts[event.status] = status_counts.get(event.status, 0) + 1

    return {
        "schema_version": 1,
        "run_mode": manifest.run_mode,
        "evidence_kind": "m5-observability",
        "run_id": run_id,
        "manifest_version": manifest.manifest_version,
        "batch_id": manifest.batch_id,
        "event_schema_version": manifest.event_schema_version,
        "events_sha256": events_sha256,
        "decision": verdict["decision"],
        "reasons": verdict["reasons"],
        "policy": verdict["policy"],
        "event_count": len(events),
        "request_kind_counts": dict(sorted(request_kind_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "metrics": metrics,
        "owner_confirmed": manifest.owner_confirmed,
        "owner_confirmation_ref": manifest.owner_confirmation_ref or None,
        "synthetic_only": is_synthetic_observability_manifest(manifest),
        "production_logging_claim": False,
        "scan_failed": scan_failed,
        "contract_violation_count": (
            metrics.get("contract_violation_count", 0)
            if contract_violation_count is None
            else contract_violation_count
        ),
    }
