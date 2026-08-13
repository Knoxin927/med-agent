"""M5.1 声明级 details 构建：把投影、judge 与人工复核合成逐条证据。"""

# 导入 Sequence，统一接受 tuple/list 输入。
from collections.abc import Sequence
# 导入 asdict，把不可变 dataclass 转成 JSON 友好的普通字典。
from dataclasses import asdict
# 导入 Any，标注 JSON 可序列化字典类型。
from typing import Any

# 导入 M5.1 值对象与人工裁决规则。
from app.evaluation.quality.annotations import (
    resolve_final_factuality,
    resolve_final_relevance,
    resolve_manual_factuality,
)
from app.evaluation.quality.types import (
    ClaimExclusionReason,
    FactualityAdjudication,
    FactualityReview,
    FactualityStatus,
    QualityClaimDetail,
    QualityJudgeResult,
    QualityManifest,
    QualityProjectionRow,
    RelevanceReview,
    case_key_for,
    quality_claim_key,
)


def _require_available_status(status: str, field: str) -> None:
    """拒绝 details 中出现未经允许的事实性状态。"""

    allowed = {"pass", "fail", "hold", "provisional", "not_available"}
    if status not in allowed:
        raise ValueError(f"{field} 的事实性状态不合法: {status}")


def build_quality_details(
    rows: Sequence[QualityProjectionRow],
    manifest: QualityManifest,
    judge_results: Sequence[QualityJudgeResult] = (),
    relevance_reviews: Sequence[RelevanceReview] = (),
    factuality_reviews: Sequence[FactualityReview] = (),
    factuality_adjudications: Sequence[FactualityAdjudication] = (),
) -> tuple[QualityClaimDetail, ...]:
    """把三类证据合并成逐声明 details。

    为什么在这里合并：
    - projection 只提供稳定身份与引用/参考 ID；
    - judge 提供 eligible、引用支持和自动相关性/事实性；
    - 人工 review 是最终事实性与相关性优先证据。
    三者必须都通过 claim_key/case_key 绑定，任何游离证据都会 fail-closed。
    """

    identities_by_method = {identity.method: identity for identity in manifest.methods}
    row_keys: set[str] = set()
    for row in rows:
        # 同一批次下方法+任务+重复+声明必须唯一；跨 projection 合并时也要 fail-closed。
        claim_key = quality_claim_key(
            row.batch_id,
            row.method,
            row.task_id,
            row.repetition,
            row.claim_id,
        )
        if claim_key in row_keys:
            raise ValueError(f"重复声明键: {claim_key}")
        row_keys.add(claim_key)
        # manifest 没有该方法时提前报错，避免后面 KeyError 掩盖契约问题。
        if row.method not in identities_by_method:
            raise ValueError(
                f"projection 包含 manifest 未声明的 method={row.method}"
            )

    # 拒绝无法回写到投影行的 judge 结果，避免评测器产生幽灵声明。
    unknown_judge = {
        result.claim_key for result in judge_results
    }.difference(row_keys)
    if unknown_judge:
        raise ValueError(f"judge 结果包含未知声明键: {sorted(unknown_judge)}")

    # 人工复核同样必须逐声明存在，不能出现脱管的证据。
    unknown_review = {
        review.claim_key for review in factuality_reviews
    }.difference(row_keys)
    unknown_adjudication = {
        item.claim_key for item in factuality_adjudications
    }.difference(row_keys)
    if unknown_review:
        raise ValueError(f"factuality reviews 包含未知声明键: {sorted(unknown_review)}")
    if unknown_adjudication:
        raise ValueError(
            f"factuality adjudications 包含未知声明键: {sorted(unknown_adjudication)}"
        )
    # 裁决必须挂在至少一条人工复核上；无 review 的 adjudication 会静默丢失，
    # 属于脱管证据，必须拒绝而不是忽略。
    reviewed_claim_keys = {review.claim_key for review in factuality_reviews}
    orphan_adjudications = {
        item.claim_key for item in factuality_adjudications
    }.difference(reviewed_claim_keys)
    if orphan_adjudications:
        raise ValueError(
            f"factuality adjudications 缺少对应 review: {sorted(orphan_adjudications)}"
        )

    # 相关性评分按 case_key 绑定；没有投影行的评分不能进入报告。
    observed_case_keys = {row.case_key for row in rows}
    unknown_relevance = {
        review.case_key for review in relevance_reviews
    }.difference(observed_case_keys)
    if unknown_relevance:
        raise ValueError(f"relevance reviews 包含未知 case_key: {sorted(unknown_relevance)}")

    judge_by_claim = {result.claim_key: result for result in judge_results}
    review_by_case = {review.case_key: review for review in relevance_reviews}

    # judge 版本必须与 manifest 冻结的 grader 版本一致，防止混用不同判分口径。
    unexpected_versions = {
        result.provider_version
        for result in judge_results
        if result.provider_version != manifest.grader_provider_version
    }
    if unexpected_versions:
        raise ValueError(
            "judge provider_version 与 manifest.grader_provider_version 不一致: "
            f"{sorted(unexpected_versions)}"
        )

    details: list[QualityClaimDetail] = []
    ordered_rows = sorted(rows, key=lambda row: (row.method, row.case_key, row.claim_id))

    for row in ordered_rows:
        claim_key = quality_claim_key(
            row.batch_id,
            row.method,
            row.task_id,
            row.repetition,
            row.claim_id,
        )
        judge = judge_by_claim.get(claim_key)
        # 显式 not_available 与完全缺失等价：自动判分结果不可信，必须 hold。
        judge_available = (
            judge is not None
            and judge.automatic_factuality_status != FactualityStatus.not_available
        )
        relevance_review = review_by_case.get(row.case_key)
        manual = resolve_manual_factuality(
            claim_key,
            factuality_reviews,
            factuality_adjudications,
        )

        if judge_available:
            # judge 是 eligible 机械判定的来源；没有 judge 时不能伪造 eligible。
            claim_eligible = judge.claim_eligible
            claim_exclusion_reason = judge.claim_exclusion_reason
            automatic_relevance_score = judge.automatic_relevance_score
            automatic_factuality_status = judge.automatic_factuality_status
            _require_available_status(automatic_factuality_status, claim_key)
        else:
            claim_eligible = False
            claim_exclusion_reason = ClaimExclusionReason.missing_judge
            automatic_relevance_score = None
            automatic_factuality_status = None

        citation_present = row.source_id is not None
        citation_supported = (
            judge.citation_supported
            if judge_available and citation_present
            else None
        )
        answer_relevance_score = resolve_final_relevance(judge, relevance_review)

        if manual is not None:
            final_factuality_status, adjudication_status = manual
            matched_reviews = [
                review
                for review in factuality_reviews
                if review.claim_key == claim_key
            ]
            if adjudication_status == "resolved":
                adjudication = next(
                    item
                    for item in factuality_adjudications
                    if item.claim_key == claim_key
                )
                factuality_reviewer_id = adjudication.adjudicator_id
                factuality_evidence_ref = adjudication.evidence_ref
                factuality_reviewed_at = adjudication.reviewed_at
            else:
                first_review = matched_reviews[0]
                factuality_reviewer_id = first_review.reviewer_id
                factuality_evidence_ref = first_review.evidence_ref
                factuality_reviewed_at = first_review.reviewed_at
        else:
            final_factuality_status, adjudication_status = resolve_final_factuality(
                claim_key,
                judge,
                factuality_reviews,
                factuality_adjudications,
            )
            factuality_reviewer_id = None
            factuality_evidence_ref = None
            factuality_reviewed_at = None

        identity = identities_by_method[row.method]
        factuality_eligible = bool(
            claim_eligible
            and row.reference_id is not None
            and (judge_available or manual is not None)
        )
        details.append(
            QualityClaimDetail(
                case_key=row.case_key,
                batch_id=row.batch_id,
                run_id=row.run_id,
                task_id=row.task_id,
                layer=row.layer,
                method=row.method,
                repetition=row.repetition,
                claim_id=row.claim_id,
                claim_hash=row.claim_hash,
                claim_eligible=claim_eligible,
                claim_exclusion_reason=claim_exclusion_reason,
                factuality_eligible=factuality_eligible,
                judge_available=judge_available,
                source_id=row.source_id,
                reference_id=row.reference_id,
                citation_present=citation_present,
                citation_supported=citation_supported,
                automatic_relevance_score=automatic_relevance_score,
                answer_relevance_score=answer_relevance_score,
                relevance_reviewer_id=(
                    relevance_review.reviewer_id
                    if relevance_review is not None
                    else None
                ),
                relevance_rationale_code=(
                    relevance_review.rationale_code
                    if relevance_review is not None
                    else None
                ),
                relevance_evidence_ref=(
                    relevance_review.evidence_ref
                    if relevance_review is not None
                    else None
                ),
                automatic_factuality_status=automatic_factuality_status,
                factuality_status=final_factuality_status,
                factuality_reviewer_id=factuality_reviewer_id,
                factuality_evidence_ref=factuality_evidence_ref,
                factuality_reviewed_at=factuality_reviewed_at,
                factuality_adjudication_status=adjudication_status,
                model_id=identity.model_id,
                tool_version=identity.tool_version,
                corpus_version=identity.corpus_version,
                source_manifest_sha256=identity.source_manifest_sha256,
                reference_manifest_sha256=identity.reference_manifest_sha256,
                projection_sha256=identity.projection_sha256,
                grader_provider_version=(
                    judge.provider_version
                    if judge_available
                    else "not_available"
                ),
            )
        )
    return tuple(details)


def validate_quality_details(
    details: Sequence[QualityClaimDetail],
    manifest: QualityManifest,
) -> None:
    """校验 details 与冻结 manifest 的身份、范围和分层绑定。"""

    identities_by_method = {identity.method: identity for identity in manifest.methods}
    seen_claim_keys: set[str] = set()
    for index, item in enumerate(details):
        path = f"details[{index}]"
        if item.batch_id != manifest.batch_id:
            raise ValueError(f"{path}.batch_id 与 manifest 不一致")
        identity = identities_by_method.get(item.method)
        if identity is None:
            raise ValueError(f"{path}.method 未在 manifest 声明: {item.method}")
        if item.run_id != identity.run_id:
            raise ValueError(f"{path}.run_id 与 manifest 不一致")
        if item.task_id not in identity.task_ids:
            raise ValueError(f"{path}.task_id 未在 manifest 声明: {item.task_id}")
        if item.repetition <= 0 or item.repetition > identity.repetitions:
            raise ValueError(f"{path}.repetition 超出 manifest 配置")
        expected_case_key = case_key_for(
            manifest.batch_id,
            item.task_id,
            item.repetition,
        )
        if item.case_key != expected_case_key:
            raise ValueError(f"{path}.case_key 与 manifest 身份不一致")
        claim_key = quality_claim_key(
            item.batch_id,
            item.method,
            item.task_id,
            item.repetition,
            item.claim_id,
        )
        if claim_key in seen_claim_keys:
            raise ValueError(f"重复 details 声明键: {claim_key}")
        seen_claim_keys.add(claim_key)
        if item.method == "dense" and item.layer == "agent-only":
            raise ValueError("dense details 不能包含 agent-only 层")
        if item.model_id != identity.model_id:
            raise ValueError(f"{path}.model_id 与 manifest 不一致")
        if item.tool_version != identity.tool_version:
            raise ValueError(f"{path}.tool_version 与 manifest 不一致")
        if item.corpus_version != identity.corpus_version:
            raise ValueError(f"{path}.corpus_version 与 manifest 不一致")
        if item.source_manifest_sha256 != identity.source_manifest_sha256:
            raise ValueError(f"{path}.source_manifest_sha256 与 manifest 不一致")
        if item.reference_manifest_sha256 != identity.reference_manifest_sha256:
            raise ValueError(f"{path}.reference_manifest_sha256 与 manifest 不一致")
        if item.projection_sha256 != identity.projection_sha256:
            raise ValueError(f"{path}.projection_sha256 与 manifest 不一致")
        expected_grader_version = (
            manifest.grader_provider_version
            if item.judge_available
            else "not_available"
        )
        if item.grader_provider_version != expected_grader_version:
            raise ValueError(f"{path}.grader_provider_version 与 judge 状态不一致")
        if item.citation_present != (item.source_id is not None):
            raise ValueError(f"{path}.citation_present 与 source_id 不一致")


def details_to_jsonable(details: Sequence[QualityClaimDetail]) -> list[dict[str, Any]]:
    """把逐声明 details 转成 JSON 可序列化列表。"""

    # asdict 只保留 dataclass 已声明字段，天然阻断未知字段混入。
    return [asdict(item) for item in details]
