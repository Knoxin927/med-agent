"""M5.5 pass/hold 决策：cost-run owner gate 未批准前禁止生产 pass。"""

# 导入 Any，输出 JSON 友好决策记录。
from typing import Any

# 导入 synthetic 识别。
from app.evaluation.cost.manifest import is_synthetic_cost_manifest
# 导入 manifest 类型。
from app.evaluation.cost.types import CostManifest


def decide_cost_verdict(
    manifest: CostManifest,
    aggregate: dict[str, Any],
    *,
    scan_failed: bool = False,
) -> dict[str, Any]:
    """按 evidence 完整度、扫描与 owner gate 返回 decision 与 reasons。"""

    reasons: list[str] = []
    # 扫描失败是最高优先级安全门。
    if scan_failed:
        reasons.append("敏感字段或未知字段扫描失败")
    # 缺证据时不能假装统计完整；not_available 是合规数据状态，不算失败。
    if not aggregate.get("has_complete_evidence", False):
        reasons.append("成本证据不完整：未覆盖 manifest 冻结的 request_kinds")

    metrics_ok = not reasons
    gate_reasons: list[str] = []
    if not manifest.owner_confirmed:
        gate_reasons.append("owner_confirmed=false：禁止生产 pass")
    else:
        # owner_confirmed 只证明 manifest 被确认，不等于真实 provider 价格/调用已批准。
        gate_reasons.append("cost-run owner gate 未批准：当前禁止生产 pass")

    if metrics_ok and is_synthetic_cost_manifest(manifest):
        decision = "synthetic_only"
        reasons = [
            "cost-run owner gate pending：仅合成工程证据，不是生产成本结论或付费调用基线"
        ]
    else:
        decision = "hold"
        reasons = [*reasons, *gate_reasons]

    return {
        # 当前只能返回 synthetic_only 或 hold，永远不会返回生产 pass。
        "decision": decision,
        "reasons": reasons,
        "policy": {
            "currency": manifest.currency,
            "request_kinds": list(manifest.request_kinds),
            "price_source_ref": manifest.price_source_ref,
            "price_as_of": manifest.price_as_of,
        },
    }


def build_cost_decision_record(
    manifest: CostManifest,
    aggregate: dict[str, Any],
    *,
    detail_sha256: str,
    run_id: str,
    scan_failed: bool = False,
) -> dict[str, Any]:
    """生成可入库的 M5.5 decision record。"""

    verdict = decide_cost_verdict(
        manifest,
        aggregate,
        scan_failed=scan_failed,
    )
    return {
        "schema_version": 1,
        "run_mode": manifest.run_mode,
        "evidence_kind": "m5-cost-accounting",
        "run_id": run_id,
        "manifest_version": manifest.manifest_version,
        "batch_id": manifest.batch_id,
        "cost_schema_version": manifest.cost_schema_version,
        "currency": manifest.currency,
        "detail_sha256": detail_sha256,
        "decision": verdict["decision"],
        "reasons": verdict["reasons"],
        "policy": verdict["policy"],
        "aggregate": aggregate,
        "owner_confirmed": manifest.owner_confirmed,
        "owner_confirmation_ref": manifest.owner_confirmation_ref or None,
        "synthetic_only": is_synthetic_cost_manifest(manifest),
        # 明确声明：本证据不声明真实 provider 价格已授权或付费调用基线可信。
        "production_cost_claim": False,
        "scan_failed": scan_failed,
    }
