"""M5.4 pass/hold 决策：cache 热路径 owner gate 未批准前禁止生产 pass。"""

# 导入 Any，输出 JSON 友好决策记录。
from typing import Any

# 导入 synthetic 识别。
from app.evaluation.cache.manifest import is_synthetic_cache_manifest
# 导入 manifest 类型。
from app.evaluation.cache.types import CacheManifest


def decide_cache_verdict(
    manifest: CacheManifest,
    aggregate: dict[str, Any],
    *,
    scan_failed: bool = False,
) -> dict[str, Any]:
    """按证据完整度、扫描与 owner gate 返回 decision 与 reasons。"""

    reasons: list[str] = []
    # 扫描失败是最高优先级安全门。
    if scan_failed:
        reasons.append("敏感字段或未知字段扫描失败")
    # 缺证据时不能假装实验完整。
    if not aggregate.get("has_complete_evidence", False):
        missing = aggregate.get("missing_evidence") or []
        reasons.append(f"缓存证据不完整: {missing}")
    # 默认旁路必须仍为 true；任何默认开启都需新 feature。
    if manifest.default_bypass is not True:
        reasons.append("default_bypass 不是 true：禁止把缓存当作默认热路径")

    metrics_ok = not reasons
    gate_reasons: list[str] = []
    if not manifest.owner_confirmed:
        gate_reasons.append("owner_confirmed=false：禁止生产 pass")
    else:
        # owner_confirmed 只证明 manifest 被确认，不等于 cache 热路径已批准。
        gate_reasons.append("cache-run owner gate 未批准：当前禁止生产 pass")

    if metrics_ok and is_synthetic_cache_manifest(manifest):
        decision = "synthetic_only"
        reasons = [
            "cache-run owner gate pending：仅合成工程证据，不是生产缓存结论或热路径就绪声明"
        ]
    else:
        decision = "hold"
        reasons = [*reasons, *gate_reasons]

    return {
        # 当前可返回 synthetic_only 或 hold，永远不会返回生产 pass。
        "decision": decision,
        "reasons": reasons,
        "policy": {
            "key_version": manifest.key_version,
            "default_ttl_seconds": manifest.default_ttl_seconds,
            "max_entries": manifest.max_entries,
            "default_bypass": manifest.default_bypass,
            "secret_source_ref": manifest.secret_source_ref,
        },
    }


def build_cache_decision_record(
    manifest: CacheManifest,
    aggregate: dict[str, Any],
    *,
    raw_sha256: str,
    run_id: str,
    scan_failed: bool = False,
) -> dict[str, Any]:
    """生成可入库的 M5.4 decision record。"""

    verdict = decide_cache_verdict(
        manifest,
        aggregate,
        scan_failed=scan_failed,
    )
    return {
        "schema_version": 1,
        "run_mode": manifest.run_mode,
        "evidence_kind": "m5-cache-strategy",
        "run_id": run_id,
        "manifest_version": manifest.manifest_version,
        "batch_id": manifest.batch_id,
        "cache_schema_version": manifest.cache_schema_version,
        "raw_sha256": raw_sha256,
        "decision": verdict["decision"],
        "reasons": verdict["reasons"],
        "policy": verdict["policy"],
        "aggregate": aggregate,
        "owner_confirmed": manifest.owner_confirmed,
        "owner_confirmation_ref": manifest.owner_confirmation_ref or None,
        "synthetic_only": is_synthetic_cache_manifest(manifest),
        # 明确声明：本证据不代表热路径可接入。
        "hot_path_claim": False,
        "default_bypass": manifest.default_bypass,
        "scan_failed": scan_failed,
    }
