"""M5.6 pass/hold 决策：M5 整体生产接入 owner gate 未批准前禁止生产 pass。"""

# 导入 Any，输出 JSON 友好决策记录。
from typing import Any

# 导入 synthetic 识别。
from app.evaluation.closure.manifest import is_synthetic_closure_manifest
# 导入值对象。
from app.evaluation.closure.types import ClosureAggregate, ClosureManifest


def decide_closure_verdict(
    manifest: ClosureManifest,
    aggregate: ClosureAggregate,
    *,
    scan_failed: bool = False,
) -> dict[str, Any]:
    """按五线证据、扫描与 owner gate 返回 closure decision 与 reasons。"""

    reasons: list[str] = []
    # closure 输入扫描失败是最高优先级安全门。
    if scan_failed:
        reasons.append("敏感字段或未知字段扫描失败")
    # 五线未齐 synthetic_only：列出违规线。
    if not aggregate.all_synthetic_only:
        bad_lines = [
            item.line
            for item in aggregate.lines
            if item.decision != "synthetic_only" or not item.synthetic_only
        ]
        reasons.append(f"存在非 synthetic_only 线: {bad_lines}")
    # 任一线扫描失败。
    if not aggregate.all_scan_ok:
        bad_lines = [item.line for item in aggregate.lines if item.scan_failed]
        reasons.append(f"存在 scan_failed=true 的线: {bad_lines}")
    # 任一 production claim 不合规：列出线与字段。
    if not aggregate.all_claims_ok:
        bad = [
            f"{item.line}:{item.bad_claims}"
            for item in aggregate.lines
            if not item.claims_ok
        ]
        reasons.append(f"存在不符合契约的 production claim: {bad}")
    if not aggregate.all_owners_pending:
        reasons.append("存在 owner_confirmed 非 false 的线，不能作为纯 synthetic closure")
    if not aggregate.has_complete_evidence:
        reasons.append("五线 synthetic closure 证据不完整")

    metrics_ok = not reasons
    gate_reasons: list[str] = []
    if not manifest.owner_confirmed:
        gate_reasons.append("owner_confirmed=false：禁止生产 pass")
    else:
        # owner_confirmed 只证明 manifest 被确认，不等于 M5 整体生产接入已批准。
        gate_reasons.append("M5 整体生产接入 owner gate 未批准：当前禁止生产 pass")

    if metrics_ok and is_synthetic_closure_manifest(manifest):
        decision = "synthetic_only"
        reasons = [
            "M5 整体生产接入 owner gate pending：仅汇总五线合成工程证据，不声明可接入生产"
        ]
    else:
        decision = "hold"
        reasons = [*reasons, *gate_reasons]

    return {
        # 当前只能返回 synthetic_only 或 hold，永远不会返回生产 pass。
        "decision": decision,
        "reasons": reasons,
        "policy": {
            "lines": [item.line for item in aggregate.lines],
            "production_adoption_claim": False,
        },
    }


def build_closure_decision_record(
    manifest: ClosureManifest,
    aggregate: ClosureAggregate,
    *,
    run_id: str,
    scan_failed: bool = False,
) -> dict[str, Any]:
    """生成可入库的 M5.6 closure decision record。"""

    verdict = decide_closure_verdict(
        manifest,
        aggregate,
        scan_failed=scan_failed,
    )
    return {
        "schema_version": 1,
        "run_mode": manifest.run_mode,
        "evidence_kind": "m5-engineering-closure",
        "run_id": run_id,
        "manifest_version": manifest.manifest_version,
        "batch_id": manifest.batch_id,
        "closure_schema_version": manifest.closure_schema_version,
        "decision": verdict["decision"],
        "reasons": verdict["reasons"],
        "policy": verdict["policy"],
        "lines": [
            {
                "line": item.line,
                "evidence_kind": item.evidence_kind,
                "decision": item.decision,
                "synthetic_only": item.synthetic_only,
                "scan_failed": item.scan_failed,
                "owner_confirmed": item.owner_confirmed,
                "claims_ok": item.claims_ok,
                "bad_claims": list(item.bad_claims),
                "run_id": item.run_id,
                "manifest_version": item.manifest_version,
            }
            for item in aggregate.lines
        ],
        "all_synthetic_only": aggregate.all_synthetic_only,
        "all_scan_ok": aggregate.all_scan_ok,
        "all_claims_ok": aggregate.all_claims_ok,
        "owner_confirmed": manifest.owner_confirmed,
        "owner_confirmation_ref": manifest.owner_confirmation_ref or None,
        "synthetic_only": is_synthetic_closure_manifest(manifest),
        # 明确声明：本证据不授权把 M5 接入生产。
        "production_adoption_claim": False,
        "scan_failed": scan_failed or not aggregate.all_scan_ok,
    }
