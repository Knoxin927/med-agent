"""M5.2 pass/hold 决策：load-run owner gate 未批准前禁止生产 pass。"""

# 导入 Any，输出 JSON 友好决策记录。
from typing import Any

# 导入 synthetic 识别。
from app.evaluation.load.manifest import is_synthetic_load_manifest
# 导入 manifest 类型。
from app.evaluation.load.types import LoadManifest


def decide_load_verdict(
    manifest: LoadManifest,
    aggregate: dict[str, Any],
    *,
    scan_failed: bool = False,
) -> dict[str, Any]:
    """按样本量、扫描与 owner gate 返回 decision 与 reasons。"""

    reasons: list[str] = []
    # 任一 scenario/concurrency 未达预注册 sample_count 时禁止生产 pass。
    if aggregate.get("has_insufficient_samples", False):
        reasons.append("存在未达到预注册 sample_count 的 concurrency 档位")
    # 扫描失败是最高优先级安全门。
    if scan_failed:
        reasons.append("敏感字段或未知字段扫描失败")
    # 检查每个档位是否有样本缺口详情，便于人读报告。
    for scenario_id, concurrency_map in aggregate.get("scenarios", {}).items():
        for concurrency, metrics in concurrency_map.items():
            if not metrics.get("sample_count_ok", False):
                reasons.append(
                    f"{scenario_id}/c={concurrency} sample_count="
                    f"{metrics.get('sample_count')} < expected="
                    f"{metrics.get('expected_sample_count')}"
                )

    metrics_ok = not reasons
    gate_reasons: list[str] = []
    if not manifest.owner_confirmed:
        gate_reasons.append("owner_confirmed=false：禁止生产 pass")
    else:
        # owner_confirmed 只证明 manifest 被确认，不等于 load-run 授权已批准。
        gate_reasons.append("load-run owner gate 未批准：当前禁止生产 pass")

    if metrics_ok and is_synthetic_load_manifest(manifest):
        decision = "synthetic_only"
        reasons = ["load-run owner gate pending：仅合成工程证据，不是生产 pass 或容量结论"]
    else:
        decision = "hold"
        reasons = [*reasons, *gate_reasons]

    return {
        # 当前可返回 synthetic_only 或 hold，永远不会返回生产 pass。
        "decision": decision,
        "reasons": reasons,
        "matrix": {
            "concurrency_levels": list(manifest.concurrency_levels),
            "warmup_count": manifest.warmup_count,
            "measurement_count": manifest.measurement_count,
            "window_seconds": manifest.window_seconds,
        },
    }


def build_load_decision_record(
    manifest: LoadManifest,
    aggregate: dict[str, Any],
    *,
    raw_sha256: str,
    run_id: str,
    scan_failed: bool = False,
) -> dict[str, Any]:
    """生成可入库的 M5.2 decision record。"""

    verdict = decide_load_verdict(
        manifest,
        aggregate,
        scan_failed=scan_failed,
    )
    return {
        "schema_version": 1,
        "run_mode": manifest.run_mode,
        "evidence_kind": "m5-load-performance",
        "run_id": run_id,
        "manifest_version": manifest.manifest_version,
        "batch_id": manifest.batch_id,
        "load_schema_version": manifest.load_schema_version,
        "tool_name": manifest.tool_name,
        "tool_version": manifest.tool_version,
        "environment_ref": manifest.environment_ref,
        "raw_sha256": raw_sha256,
        "decision": verdict["decision"],
        "reasons": verdict["reasons"],
        "matrix": verdict["matrix"],
        "aggregate": aggregate,
        "owner_confirmed": manifest.owner_confirmed,
        "owner_confirmation_ref": manifest.owner_confirmation_ref or None,
        "synthetic_only": is_synthetic_load_manifest(manifest),
        "capacity_claim": False,
        "scan_failed": scan_failed,
    }
