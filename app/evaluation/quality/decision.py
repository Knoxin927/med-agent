"""M5.1 pass/hold 决策：owner quality-run gate 未批准前禁止生产 pass。"""

# 导入 Any，输出 JSON 友好决策记录。
from typing import Any

# 导入 synthetic 识别，避免合成证据伪装成生产 pass。
from app.evaluation.quality.manifest import is_synthetic_quality_manifest
# 导入 manifest 与固定 method/layer 常量。
from app.evaluation.quality.types import QualityLayer, QualityManifest, QualityMethod


def _check_layer_thresholds(
    manifest: QualityManifest,
    aggregate: dict[str, Any],
    method: str,
    layer: str,
    reasons: list[str],
) -> None:
    """比较单个 method+layer 的四类指标与冻结阈值。"""

    # 读取本层指标；结构缺失也必须 fail-closed，不能跳过。
    metrics = aggregate[method][layer]

    # 引用 coverage：无 eligible claims 时保留 None 而不是 0。
    coverage = metrics["citation_coverage"]
    if coverage is None:
        reasons.append(f"{method}/{layer} citation_coverage 无可用样本")
    elif coverage < manifest.citation_coverage_threshold:
        reasons.append(
            f"{method}/{layer} citation_coverage {coverage:.4f} 低于阈值 "
            f"{manifest.citation_coverage_threshold:.4f}"
        )

    # 引用 support：只对有引用声明计算；空分母时不可用。
    support = metrics["citation_support"]
    if support is None:
        reasons.append(f"{method}/{layer} citation_support 无可用样本")
    elif support < manifest.citation_support_threshold:
        reasons.append(
            f"{method}/{layer} citation_support {support:.4f} 低于阈值 "
            f"{manifest.citation_support_threshold:.4f}"
        )

    # 相关性均值：空样本不进入分母，但必须 hold。
    mean = metrics["relevance_mean"]
    if mean is None:
        reasons.append(f"{method}/{layer} relevance_mean 无可用样本")
    elif mean < manifest.relevance_mean_threshold:
        reasons.append(
            f"{method}/{layer} relevance_mean {mean:.4f} 低于阈值 "
            f"{manifest.relevance_mean_threshold:.4f}"
        )

    # 事实性 pass_rate：reviewed_claims=0 时不可用，必须 hold。
    pass_rate = metrics["factuality_pass_rate"]
    if pass_rate is None:
        reasons.append(f"{method}/{layer} factuality_pass_rate 无可用样本")
    elif pass_rate < manifest.factuality_pass_rate_threshold:
        reasons.append(
            f"{method}/{layer} factuality_pass_rate {pass_rate:.4f} 低于阈值 "
            f"{manifest.factuality_pass_rate_threshold:.4f}"
        )

    # 人工未复核覆盖全部 factuality eligible claims 时禁止 pass。
    review_coverage = metrics["factuality_review_coverage"]
    if review_coverage is not None and review_coverage < 1.0:
        reasons.append(
            f"{method}/{layer} 人工复核未覆盖全部 factuality eligible claims"
        )


def decide_quality_verdict(
    manifest: QualityManifest,
    aggregate: dict[str, Any],
    *,
    scan_failed: bool = False,
) -> dict[str, Any]:
    """按预注册阈值与 owner gate 返回 decision 与 reasons。"""

    # 收集所有会阻止生产 pass 的独立原因。
    reasons: list[str] = []
    for method in (QualityMethod.dense, QualityMethod.agent):
        # 只检查 manifest/aggregate 实际存在的方法。
        if method not in aggregate:
            continue
        for layer in (QualityLayer.shared, QualityLayer.agent_only):
            layer_metrics = aggregate[method][layer]
            # 结构上不属于该方法的层（例如 dense 无 agent-only）没有期望样本，
            # 不应以空分母拖成 hold；只有存在样本或缺失样本时才必须独立过线。
            if (
                layer_metrics["claim_count"] == 0
                and aggregate[method]["missing_case_count"] == 0
            ):
                continue
            # 每个有期望样本或缺失样本的 method+layer 都必须独立过线。
            _check_layer_thresholds(manifest, aggregate, method, layer, reasons)

    # 样本未生成说明投影不完整，任何指标都不能代表整批。
    if aggregate.get("has_missing_cases", False):
        reasons.append("存在未生成的 case 样本，禁止发布生产 pass")
    # judge 缺失会让 eligible 判定与事实性证据断链。
    if aggregate.get("has_judge_unavailable_claims", False):
        reasons.append("存在 judge 不可用的声明，禁止发布生产 pass")
    # 扫描失败是最高优先级安全门。
    if scan_failed:
        reasons.append("敏感字段或未知字段扫描失败")

    # 指标原因先独立判定：缺失/阈值/扫描失败会阻止任何决策降级。
    metrics_ok = not reasons
    # owner gate 原因单独收集，不能混进 metrics_ok，否则 synthetic_only 永远不可达。
    gate_reasons: list[str] = []
    if not manifest.owner_confirmed:
        gate_reasons.append("owner_confirmed=false：禁止生产 pass")
    else:
        # owner_confirmed 只证明 manifest 被确认，不等于 quality-run 授权已批准。
        gate_reasons.append("quality-run owner gate 未批准：当前禁止生产 pass")

    # 合成 manifest 且指标过线时只允许 synthetic_only 工程证据。
    if metrics_ok and is_synthetic_quality_manifest(manifest):
        decision = "synthetic_only"
        reasons = ["quality-run owner gate pending：仅合成工程证据，不是生产 pass"]
    else:
        decision = "hold"
        reasons = [*reasons, *gate_reasons]

    return {
        # 当前可返回 synthetic_only 或 hold，永远不会返回 pass。
        "decision": decision,
        # 人读决策原因。
        "reasons": reasons,
        # 固定阈值，供报告与后续 owner 复核。
        "thresholds": {
            "citation_coverage_threshold": manifest.citation_coverage_threshold,
            "citation_support_threshold": manifest.citation_support_threshold,
            "relevance_mean_threshold": manifest.relevance_mean_threshold,
            "factuality_pass_rate_threshold": manifest.factuality_pass_rate_threshold,
        },
    }


def build_quality_decision_record(
    manifest: QualityManifest,
    aggregate: dict[str, Any],
    *,
    details_sha256: str,
    run_id: str,
    scan_failed: bool = False,
) -> dict[str, Any]:
    """生成可入库的 M5.1 decision record。"""

    # 决策与汇总始终同一次重算，不允许调用方分别手填。
    verdict = decide_quality_verdict(
        manifest,
        aggregate,
        scan_failed=scan_failed,
    )
    return {
        # 机器可读 schema 版本。
        "schema_version": 1,
        # 显式运行模式：synthetic/production。
        "run_mode": manifest.run_mode,
        # 证据种类，区别于 M3.7 的 m3-agent-task-evaluation。
        "evidence_kind": "m5-quality-evaluation",
        # 唯一 run 身份。
        "run_id": run_id,
        # manifest 版本。
        "manifest_version": manifest.manifest_version,
        # dense/agent 共用的配对批次。
        "batch_id": manifest.batch_id,
        # 投影 schema 版本。
        "quality_schema_version": manifest.quality_schema_version,
        # grader/judge 提供方版本。
        "grader_provider_version": manifest.grader_provider_version,
        # details 内容 hash，报告可重算验证。
        "details_sha256": details_sha256,
        # 最终 decision。
        "decision": verdict["decision"],
        # 决策原因。
        "reasons": verdict["reasons"],
        # 阈值快照。
        "thresholds": verdict["thresholds"],
        # 完整聚合结果。
        "aggregate": aggregate,
        # owner 是否确认 manifest。
        "owner_confirmed": manifest.owner_confirmed,
        # owner 授权引用；未确认时为 null。
        "owner_confirmation_ref": manifest.owner_confirmation_ref or None,
        # 是否合成工程证据。
        "synthetic_only": is_synthetic_quality_manifest(manifest),
        # 安全扫描是否失败。
        "scan_failed": scan_failed,
    }
