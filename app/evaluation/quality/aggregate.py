"""M5.1 声明级聚合：每个汇总数字都必须能从 details 重算，缺失不填 0。"""

# 导入 Sequence，统一接受 tuple/list 输入。
from collections.abc import Sequence
# 导入 Any，输出 JSON 友好的聚合字典。
from typing import Any

# 导入值对象与固定 method/layer 常量。
from app.evaluation.quality.types import (
    FactualityStatus,
    QualityClaimDetail,
    QualityLayer,
    QualityManifest,
    QualityMethod,
    case_key_for,
)


# 自动事实性状态白名单：aggregate 不接受 details 中出现未冻结状态。
_FINAL_FACTUALITY_STATUSES = {
    FactualityStatus.pass_,
    FactualityStatus.fail,
    FactualityStatus.hold,
    FactualityStatus.provisional,
    FactualityStatus.not_available,
}
# 人工已复核才计入 pass_rate 的状态；provisional/not_available 不算人工结论。
_REVIEWED_FACTUALITY_STATUSES = {
    FactualityStatus.pass_,
    FactualityStatus.fail,
    FactualityStatus.hold,
}


def _rate(numerator: int, denominator: int) -> float | None:
    """计算比率；分母为 0 时返回 None，而不是伪造 0。"""

    # 空分母必须保留 not_available，避免无样本被误读成 0 分。
    if denominator <= 0:
        return None
    return numerator / denominator


def _validate_detail(item: QualityClaimDetail) -> None:
    """聚合前校验 details 中影响分母的关键字段。"""

    # method 只允许 dense/agent，防止未知方法混入分母。
    if item.method not in {QualityMethod.dense, QualityMethod.agent}:
        raise ValueError(f"{item.claim_id} 的 method 不合法: {item.method}")
    # layer 只允许 shared/agent-only，保证两层分母永远独立。
    if item.layer not in {QualityLayer.shared, QualityLayer.agent_only}:
        raise ValueError(f"{item.claim_id} 的 layer 不合法: {item.layer}")
    # judge_available 必须是明确布尔值，None 会让缺失传播不可靠。
    if not isinstance(item.judge_available, bool):
        raise ValueError(f"{item.claim_id} 的 judge_available 必须是布尔值")
    # judge 可用时有引用必须给出支持结果，缺失说明 details 构造有断链。
    if item.citation_present and item.judge_available and item.citation_supported is None:
        raise ValueError(f"{item.claim_id} 有引用但 judge 可用时缺少 citation_supported")
    # judge 不可用时有引用必须显式保留 None，让缺失传播到 decision=hold。
    if item.citation_present and not item.judge_available and item.citation_supported is not None:
        raise ValueError(f"{item.claim_id} 有引用但 judge 不可用时 citation_supported 必须为 None")
    # 没有引用时不得填写引用支持结果，避免无引用声明被误算成已支持。
    if not item.citation_present and item.citation_supported is not None:
        raise ValueError(f"{item.claim_id} 无引用时 citation_supported 必须为 None")
    # 相关性评分必须落在固定 0/1/2 量表。
    score = item.answer_relevance_score
    if score is not None and (
        isinstance(score, bool)
        or not isinstance(score, int)
        or not 0 <= score <= 2
    ):
        raise ValueError(f"{item.claim_id} 的 relevance 评分必须在 0 到 2 之间")
    # 事实性最终状态必须来自冻结集合。
    if item.factuality_status not in _FINAL_FACTUALITY_STATUSES:
        raise ValueError(f"{item.claim_id} 的事实性状态不合法")


def _layer_metrics(details: Sequence[QualityClaimDetail], layer: str) -> dict[str, Any]:
    """对单一 method+layer 计算四类质量指标。"""

    # 先按 layer 过滤，避免 shared 与 agent-only 混算。
    rows = [item for item in details if item.layer == layer]
    # eligible claims 是 citation coverage 的分母来源。
    eligible_claims = [item for item in rows if item.claim_eligible]
    # 有引用声明只统计 eligible claims，不能把礼貌语等排除项算进去。
    claims_with_citation = [
        item for item in eligible_claims if item.citation_present
    ]
    # supported 只统计引用存在且 judge 判为支持。
    supported_cited_claims = [
        item for item in claims_with_citation if item.citation_supported is True
    ]

    # 相关性按 case 汇总：同一题多个声明时取最高分，避免重复计案例。
    scores_by_case: dict[str, int] = {}
    for item in rows:
        if item.answer_relevance_score is not None:
            current = scores_by_case.get(item.case_key)
            if current is None or item.answer_relevance_score > current:
                scores_by_case[item.case_key] = item.answer_relevance_score
    relevance_scores = list(scores_by_case.values())
    # 0=不相关、1=部分相关、2=直接相关；1 和 2 都算相关案例。
    relevant_cases = sum(1 for score in relevance_scores if score >= 1)

    # factuality 只统计同时具备 reference evidence 的 eligible claims。
    factuality_eligible_claims = [item for item in rows if item.factuality_eligible]
    # reviewed_claims 只统计有人工最终结论的声明。
    reviewed_claims = [
        item
        for item in factuality_eligible_claims
        if item.factuality_status in _REVIEWED_FACTUALITY_STATUSES
    ]
    pass_claims = sum(
        1
        for item in reviewed_claims
        if item.factuality_status == FactualityStatus.pass_
    )
    return {
        # 引用 coverage 分母：所有 eligible claims，无论是否有引用。
        "eligible_claims": len(eligible_claims),
        # 引用 coverage 分子：有引用且 eligible 的声明数。
        "claims_with_citation": len(claims_with_citation),
        # citation support 分子：引用被支持且 eligible 的声明数。
        "supported_cited_claims": len(supported_cited_claims),
        # 无 eligible 声明时返回 None，而不是 0。
        "citation_coverage": _rate(len(claims_with_citation), len(eligible_claims)),
        # 有引用声明数为 0 时 support 不可用。
        "citation_support": _rate(
            len(supported_cited_claims),
            len(claims_with_citation),
        ),
        # 有相关性评分的独立案例数。
        "scored_cases": len(relevance_scores),
        # 评分 >=1 的独立案例数。
        "relevant_cases": relevant_cases,
        # relevant/scored 比例，空样本时为 None。
        "relevance_relevant_rate": _rate(relevant_cases, len(relevance_scores)),
        # 相关性均值，空样本时为 None。
        "relevance_mean": (
            sum(relevance_scores) / len(relevance_scores)
            if relevance_scores
            else None
        ),
        # factuality pass_rate 分母：具备 reference evidence 的 eligible claims。
        "factuality_eligible_claims": len(factuality_eligible_claims),
        # 人工复核且给出最终 pass/fail/hold 的声明数。
        "reviewed_claims": len(reviewed_claims),
        # 人工判为 pass 的声明数。
        "pass_claims": pass_claims,
        # reviewed_claims 为 0 时保留 None，表示 hold。
        "factuality_pass_rate": _rate(pass_claims, len(reviewed_claims)),
        # 人工复核覆盖率；无 eligible factuality 声明时为 None。
        "factuality_review_coverage": _rate(
            len(reviewed_claims),
            len(factuality_eligible_claims),
        ),
        # judge 缺失声明数；这些声明不能进入引用分母且应使 decision hold。
        "judge_unavailable_claim_count": sum(
            1 for item in rows if not item.judge_available
        ),
        # 本层实际观察到的声明数，供人读报告核对。
        "claim_count": len(rows),
    }


def _method_missing_counts(
    details: Sequence[QualityClaimDetail],
    manifest: QualityManifest,
    method: str,
) -> dict[str, int]:
    """按方法统计期望案例与实际案例，识别未生成样本。"""

    # 从 manifest 找到该方法的冻结 run identity。
    identity = next(
        item for item in manifest.methods if item.method == method
    )
    # 期望案例由 task_ids 与 repetitions 完全展开得到。
    expected_cases = {
        case_key_for(manifest.batch_id, task_id, repetition)
        for task_id in identity.task_ids
        for repetition in range(1, identity.repetitions + 1)
    }
    # 实际案例来自该方法的投影 details。
    observed_cases = {
        item.case_key for item in details if item.method == method
    }
    # 出现 manifest 未声明的案例说明输入身份漂移，必须 fail-closed。
    unknown_cases = observed_cases.difference(expected_cases)
    if unknown_cases:
        raise ValueError(
            f"{method} 包含 manifest 未声明的 case_key: {sorted(unknown_cases)}"
        )
    expected_count = len(expected_cases)
    observed_count = len(observed_cases)
    return {
        # 期望案例数来自 manifest，不随 details 消失。
        "expected_case_count": expected_count,
        # 实际案例数来自投影，用于识别缺失样本。
        "observed_case_count": observed_count,
        # 缺失样本数；只向上取正数，不把重复案例当成补足。
        "missing_case_count": max(0, expected_count - observed_count),
    }


def aggregate_quality_details(
    details: Sequence[QualityClaimDetail],
    manifest: QualityManifest,
) -> dict[str, Any]:
    """从逐声明 details 重算 dense/agent x shared/agent-only 质量汇总。"""

    # 空 details 不能生成可信报告，必须 fail-closed。
    if not details:
        raise ValueError("details 不能为空")
    # 先整体校验字段，再分 method/layer 计算。
    for item in details:
        _validate_detail(item)

    # 确认 manifest 中声明的 method 集合，避免循环不存在的分母。
    method_names = {identity.method for identity in manifest.methods}
    observed_methods = {item.method for item in details}
    if not observed_methods.issubset(method_names):
        raise ValueError(
            f"details 包含 manifest 未声明的 method: {sorted(observed_methods.difference(method_names))}"
        )

    # 双层结果：method 外层，layer 内层，保证 shared 与 agent-only 独立。
    result: dict[str, Any] = {}
    has_missing_cases = False
    has_judge_unavailable_claims = False
    for method in (QualityMethod.dense, QualityMethod.agent):
        # 只输出 manifest 明确声明的 method，避免报告出现幽灵方法。
        if method not in method_names:
            continue
        method_details = [item for item in details if item.method == method]
        method_metrics = {
            QualityLayer.shared: _layer_metrics(method_details, QualityLayer.shared),
            QualityLayer.agent_only: _layer_metrics(
                method_details,
                QualityLayer.agent_only,
            ),
        }
        # 把缺失样本数挂到 method 层；未知缺失题无法安全归入 layer。
        method_metrics.update(_method_missing_counts(details, manifest, method))
        has_missing_cases = has_missing_cases or method_metrics["missing_case_count"] > 0
        has_judge_unavailable_claims = has_judge_unavailable_claims or any(
            metrics["judge_unavailable_claim_count"] > 0
            for metrics in (
                method_metrics[QualityLayer.shared],
                method_metrics[QualityLayer.agent_only],
            )
        )
        result[method] = method_metrics

    # 顶层保留整体计数与缺失传播标记，decision 不需要重复遍历明细。
    result["total_detail_count"] = len(details)
    result["has_missing_cases"] = has_missing_cases
    result["has_judge_unavailable_claims"] = has_judge_unavailable_claims
    return result
