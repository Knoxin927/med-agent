"""M5.1 judge adapter 与人工复核模型：自动结果只作线索，人工结果优先。"""

# 导入 Sequence，接受 tuple/list 类型的人工复核数据。
from collections.abc import Sequence

# 导入 M5.1 值对象。
from app.evaluation.quality.types import (
    FactualityAdjudication,
    FactualityReview,
    FactualityStatus,
    QualityJudgeResult,
    QualityProjection,
    RelevanceReview,
    quality_claim_key,
)
from app.evaluation.quality.scan import scan_report_payload


# 允许的自动事实性状态。
_AUTO_FACTUALITY_STATUSES = {
    FactualityStatus.pass_,
    FactualityStatus.fail,
    FactualityStatus.hold,
    FactualityStatus.not_available,
}
# 人工复核允许状态。
_MANUAL_FACTUALITY_STATUSES = {
    FactualityStatus.pass_,
    FactualityStatus.fail,
    FactualityStatus.hold,
}


def _require_non_empty_str(value: object, field: str) -> str:
    """要求非空字符串。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _require_int_in_range(
    value: object,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """要求整数且落在闭区间。"""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return value


def parse_quality_judge_results(payload: object) -> tuple[QualityJudgeResult, ...]:
    """解析 judge adapter 输出；claim_key 必须唯一。"""

    if not isinstance(payload, dict):
        raise ValueError("judge results 必须是对象")
    # judge 输出也是外部输入，正文/密钥必须在这里就 fail-closed。
    scan_report_payload(payload, "judge_results")
    unknown = set(payload).difference({"schema_version", "provider_version", "results"})
    if unknown:
        raise ValueError(f"judge results 包含未知字段: {sorted(unknown)}")
    results_raw = payload.get("results")
    if not isinstance(results_raw, list) or not results_raw:
        raise ValueError("judge results.results 必须是非空数组")
    results: list[QualityJudgeResult] = []
    seen: set[str] = set()
    for index, row in enumerate(results_raw):
        if not isinstance(row, dict):
            raise ValueError(f"judge results[{index}] 必须是对象")
        row_unknown = set(row).difference(
            {
                "claim_key",
                "claim_eligible",
                "claim_exclusion_reason",
                "citation_supported",
                "automatic_relevance_score",
                "automatic_factuality_status",
                "provider_version",
                "evidence_ref",
            }
        )
        if row_unknown:
            raise ValueError(f"judge results[{index}] 包含未知字段: {sorted(row_unknown)}")
        claim_key = _require_non_empty_str(row.get("claim_key"), f"judge results[{index}].claim_key")
        if claim_key in seen:
            raise ValueError(f"重复 judge claim_key: {claim_key}")
        seen.add(claim_key)
        claim_eligible = row.get("claim_eligible")
        if not isinstance(claim_eligible, bool):
            raise ValueError(f"judge results[{index}].claim_eligible 必须是布尔值")
        exclusion = row.get("claim_exclusion_reason")
        if claim_eligible:
            if exclusion is not None:
                raise ValueError(f"judge results[{index}] eligible=true 时不能有 exclusion")
        else:
            exclusion = _require_non_empty_str(
                exclusion,
                f"judge results[{index}].claim_exclusion_reason",
            )
        citation_supported = row.get("citation_supported")
        if not isinstance(citation_supported, bool):
            raise ValueError(f"judge results[{index}].citation_supported 必须是布尔值")
        relevance = row.get("automatic_relevance_score")
        if relevance is not None:
            relevance = _require_int_in_range(
                relevance,
                f"judge results[{index}].automatic_relevance_score",
                minimum=0,
                maximum=2,
            )
        automatic_status = _require_non_empty_str(
            row.get("automatic_factuality_status"),
            f"judge results[{index}].automatic_factuality_status",
        )
        if automatic_status not in _AUTO_FACTUALITY_STATUSES:
            raise ValueError(
                f"judge results[{index}].automatic_factuality_status 不合法"
            )
        results.append(
            QualityJudgeResult(
                claim_key=claim_key,
                claim_eligible=claim_eligible,
                claim_exclusion_reason=exclusion,
                citation_supported=citation_supported,
                automatic_relevance_score=relevance,
                automatic_factuality_status=automatic_status,
                provider_version=_require_non_empty_str(
                    row.get("provider_version"),
                    f"judge results[{index}].provider_version",
                ),
                evidence_ref=_require_non_empty_str(
                    row.get("evidence_ref"),
                    f"judge results[{index}].evidence_ref",
                ),
            )
        )
    return tuple(results)


def parse_relevance_reviews(payload: object) -> tuple[RelevanceReview, ...]:
    """解析人工相关性评分列表。"""

    if not isinstance(payload, list):
        raise ValueError("relevance reviews 必须是数组")
    scan_report_payload(payload, "relevance_reviews")
    reviews: list[RelevanceReview] = []
    seen: set[str] = set()
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise ValueError(f"relevance reviews[{index}] 必须是对象")
        unknown = set(row).difference(
            {
                "case_key",
                "relevance_score",
                "reviewer_id",
                "rationale_code",
                "evidence_ref",
                "authorized_by_ref",
            }
        )
        if unknown:
            raise ValueError(f"relevance reviews[{index}] 包含未知字段: {sorted(unknown)}")
        case_key = _require_non_empty_str(row.get("case_key"), f"relevance reviews[{index}].case_key")
        if case_key in seen:
            raise ValueError(f"重复 relevance case_key: {case_key}")
        seen.add(case_key)
        reviews.append(
            RelevanceReview(
                case_key=case_key,
                relevance_score=_require_int_in_range(
                    row.get("relevance_score"),
                    f"relevance reviews[{index}].relevance_score",
                    minimum=0,
                    maximum=2,
                ),
                reviewer_id=_require_non_empty_str(
                    row.get("reviewer_id"),
                    f"relevance reviews[{index}].reviewer_id",
                ),
                rationale_code=_require_non_empty_str(
                    row.get("rationale_code"),
                    f"relevance reviews[{index}].rationale_code",
                ),
                evidence_ref=_require_non_empty_str(
                    row.get("evidence_ref"),
                    f"relevance reviews[{index}].evidence_ref",
                ),
                authorized_by_ref=_require_non_empty_str(
                    row.get("authorized_by_ref"),
                    f"relevance reviews[{index}].authorized_by_ref",
                ),
            )
        )
    return tuple(reviews)


def parse_factuality_reviews(payload: object) -> tuple[FactualityReview, ...]:
    """解析逐声明人工事实性复核列表。"""

    if not isinstance(payload, list):
        raise ValueError("factuality reviews 必须是数组")
    scan_report_payload(payload, "factuality_reviews")
    reviews: list[FactualityReview] = []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise ValueError(f"factuality reviews[{index}] 必须是对象")
        unknown = set(row).difference(
            {
                "claim_key",
                "review_decision",
                "reviewer_id",
                "evidence_ref",
                "reviewed_at",
                "authorized_by_ref",
            }
        )
        if unknown:
            raise ValueError(f"factuality reviews[{index}] 包含未知字段: {sorted(unknown)}")
        decision = _require_non_empty_str(
            row.get("review_decision"),
            f"factuality reviews[{index}].review_decision",
        )
        if decision not in _MANUAL_FACTUALITY_STATUSES:
            raise ValueError(f"factuality reviews[{index}].review_decision 不合法")
        reviews.append(
            FactualityReview(
                claim_key=_require_non_empty_str(
                    row.get("claim_key"),
                    f"factuality reviews[{index}].claim_key",
                ),
                review_decision=decision,
                reviewer_id=_require_non_empty_str(
                    row.get("reviewer_id"),
                    f"factuality reviews[{index}].reviewer_id",
                ),
                evidence_ref=_require_non_empty_str(
                    row.get("evidence_ref"),
                    f"factuality reviews[{index}].evidence_ref",
                ),
                reviewed_at=_require_non_empty_str(
                    row.get("reviewed_at"),
                    f"factuality reviews[{index}].reviewed_at",
                ),
                authorized_by_ref=_require_non_empty_str(
                    row.get("authorized_by_ref"),
                    f"factuality reviews[{index}].authorized_by_ref",
                ),
            )
        )
    return tuple(reviews)


def parse_factuality_adjudications(payload: object) -> tuple[FactualityAdjudication, ...]:
    """解析人工复核冲突的裁决列表。"""

    if not isinstance(payload, list):
        raise ValueError("factuality adjudications 必须是数组")
    scan_report_payload(payload, "factuality_adjudications")
    adjudications: list[FactualityAdjudication] = []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise ValueError(f"factuality adjudications[{index}] 必须是对象")
        unknown = set(row).difference(
            {
                "claim_key",
                "final_decision",
                "adjudicator_id",
                "evidence_ref",
                "reviewed_at",
                "authorized_by_ref",
            }
        )
        if unknown:
            raise ValueError(
                f"factuality adjudications[{index}] 包含未知字段: {sorted(unknown)}"
            )
        decision = _require_non_empty_str(
            row.get("final_decision"),
            f"factuality adjudications[{index}].final_decision",
        )
        if decision not in _MANUAL_FACTUALITY_STATUSES:
            raise ValueError(f"factuality adjudications[{index}].final_decision 不合法")
        adjudications.append(
            FactualityAdjudication(
                claim_key=_require_non_empty_str(
                    row.get("claim_key"),
                    f"factuality adjudications[{index}].claim_key",
                ),
                final_decision=decision,
                adjudicator_id=_require_non_empty_str(
                    row.get("adjudicator_id"),
                    f"factuality adjudications[{index}].adjudicator_id",
                ),
                evidence_ref=_require_non_empty_str(
                    row.get("evidence_ref"),
                    f"factuality adjudications[{index}].evidence_ref",
                ),
                reviewed_at=_require_non_empty_str(
                    row.get("reviewed_at"),
                    f"factuality adjudications[{index}].reviewed_at",
                ),
                authorized_by_ref=_require_non_empty_str(
                    row.get("authorized_by_ref"),
                    f"factuality adjudications[{index}].authorized_by_ref",
                ),
            )
        )
    return tuple(adjudications)


def resolve_manual_factuality(
    claim_key: str,
    reviews: Sequence[FactualityReview],
    adjudications: Sequence[FactualityAdjudication],
) -> tuple[str, str | None] | None:
    """裁决人工复核：同判通过；冲突必须由第二 reviewer 裁决。"""

    matched = [review for review in reviews if review.claim_key == claim_key]
    if not matched:
        return None
    decisions = {review.review_decision for review in matched}
    if len(decisions) == 1:
        return decisions.pop(), None
    adjudication = next(
        (item for item in adjudications if item.claim_key == claim_key),
        None,
    )
    if adjudication is not None:
        return adjudication.final_decision, "resolved"
    # 冲突未裁决时 fail-closed：最终状态为 hold，而不是让自动结果擅自胜出。
    return FactualityStatus.hold, "unresolved"


def resolve_final_factuality(
    claim_key: str,
    judge: QualityJudgeResult | None,
    reviews: Sequence[FactualityReview],
    adjudications: Sequence[FactualityAdjudication],
) -> tuple[str, str | None]:
    """合成最终事实性状态：人工优先，无人工时自动只能 provisional。"""

    manual = resolve_manual_factuality(claim_key, reviews, adjudications)
    if manual is not None:
        return manual
    if judge is not None and judge.automatic_factuality_status != FactualityStatus.not_available:
        return FactualityStatus.provisional, None
    return FactualityStatus.not_available, None


def resolve_final_relevance(
    judge: QualityJudgeResult | None,
    review: RelevanceReview | None,
) -> int | None:
    """最终相关性评分：人工评分优先，否则使用自动评分。"""

    if review is not None:
        return review.relevance_score
    if (
        judge is not None
        and judge.automatic_factuality_status != FactualityStatus.not_available
        and judge.automatic_relevance_score is not None
    ):
        return judge.automatic_relevance_score
    return None


def fake_quality_judge(
    projection: QualityProjection,
    *,
    provider_version: str,
) -> tuple[QualityJudgeResult, ...]:
    """只用于 synthetic 工程验证的确定性 judge，绝不代表真实模型判断。"""

    results: list[QualityJudgeResult] = []
    for row in projection.rows:
        claim_key = quality_claim_key(
            row.batch_id,
            row.method,
            row.task_id,
            row.repetition,
            row.claim_id,
        )
        if "disclaimer" in row.claim_id:
            eligible, exclusion = False, "disclaimer"
        elif "polite" in row.claim_id:
            eligible, exclusion = False, "polite_closing"
        elif "restatement" in row.claim_id:
            eligible, exclusion = False, "question_restatement"
        elif "format" in row.claim_id:
            eligible, exclusion = False, "unverifiable_format"
        else:
            eligible, exclusion = True, None
        citation_supported = bool(
            row.source_id
            and row.reference_id
            and (row.source_id == row.reference_id or row.source_id.endswith("-supported"))
        )
        if row.source_id:
            relevance_score = 2
        elif row.reference_id:
            relevance_score = 1
        else:
            relevance_score = 0
        if row.reference_id and citation_supported:
            automatic_status = FactualityStatus.pass_
        elif row.reference_id:
            automatic_status = FactualityStatus.fail
        else:
            automatic_status = FactualityStatus.hold
        results.append(
            QualityJudgeResult(
                claim_key=claim_key,
                claim_eligible=eligible,
                claim_exclusion_reason=exclusion,
                citation_supported=citation_supported,
                automatic_relevance_score=relevance_score,
                automatic_factuality_status=automatic_status,
                provider_version=provider_version,
                evidence_ref=f"synthetic-judge:{claim_key}",
            )
        )
    return tuple(results)
