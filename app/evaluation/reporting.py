"""将同一轮评测证据以不可变 JSON 与 Markdown 目录发布。"""

# 导入 json，写入机器可读的逐轮证据。
import json
# 导入 math，拒绝报告中无法比较的 NaN 和无穷数值。
import math
# 导入 os，使用同一文件系统内的目录替换发布结果。
import os
# 导入 re，限制 run ID 不包含路径或控制字符。
import re
# 导入 shutil，仅在发布失败时清理本函数刚创建的 staging 目录。
import shutil
# 导入 tempfile，在 reports 父目录创建同卷 staging 目录。
import tempfile
# 导入 Path，统一处理报告目录。
from pathlib import Path
# 导入 Any，允许 details 保存嵌套的 JSON 证据。
from typing import Any

# 导入既有纯指标函数，发布 gate 用它从排名重新计算而非相信手填 metrics。
from app.evaluation.metrics import compute_case_metrics, linear_percentile
# 导入成功 smoke 的发布级校验，正式报告必须引用真实已通过的模型准备工件。
from app.evaluation.rerank_smoke import validate_reranker_smoke_report
# 导入构造指标函数所需的冻结案例与稳定 identity 值对象。
from app.evaluation.types import ChunkIdentity, EvaluationCase
# 导入 reranker 的唯一候选宽度常量，避免报告 gate 与实际执行参数漂移。
from app.retrieval_strategies.rerank import (
    RERANKER_BATCH_SIZE,
    RERANKER_CANDIDATE_K,
    RERANKER_MAX_LENGTH,
    RERANKER_MODEL_ID,
    RERANKER_MODEL_REVISION,
)


# run ID 只允许安全的文件名字符，避免目录逃逸。
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# 比较同一次 dense 完整候选派生的 pre Top-10 与 rerank post Top-10。
def build_dense_rerank_comparison(
    pre_case_metrics_by_case_id: dict[str, Any],
    post_case_metrics_by_case_id: dict[str, Any],
    pre_ranked_results_by_case_id: dict[str, Any],
    post_ranked_results_by_case_id: dict[str, Any],
    *,
    valid_identities: set[tuple[str, int]],
) -> dict[str, Any]:
    """按固定质量键生成 rerank 改善、退化、不变和库外变化结论。"""

    # 四份逐题证据必须覆盖完全相同的冻结 case 集合。
    case_ids = set(pre_case_metrics_by_case_id)
    # 任何缺题都会使比较只覆盖有利子集。
    if not case_ids or not (
        case_ids
        == set(post_case_metrics_by_case_id)
        == set(pre_ranked_results_by_case_id)
        == set(post_ranked_results_by_case_id)
    ):
        # fail-closed，拒绝不完整的逐题报告。
        raise ValueError("dense-rerank comparison case id 集合不一致")
    # 保存每题可供 M2.5 人工审阅的明确结论。
    cases: dict[str, dict[str, Any]] = {}
    # 按 case id 排序，保证 JSON 与 Markdown 结果稳定。
    for case_id in sorted(case_ids):
        # 读取同一题的重排前后指标。
        pre_metrics = pre_case_metrics_by_case_id[case_id]
        # 读取重排后的指标。
        post_metrics = post_case_metrics_by_case_id[case_id]
        # 双方都必须满足已有的完整单题 metrics schema。
        _validate_case_metrics(case_id, pre_metrics)
        # 后指标同样不能有隐藏的类型漂移。
        _validate_case_metrics(case_id, post_metrics)
        # 同一题的字段类型必须一致，避免库外 None 被伪造为数值。
        if _case_metric_schema(pre_metrics) != _case_metric_schema(post_metrics):
            # 不让类型漂移绕过质量比较。
            raise ValueError("dense-rerank comparison 逐题指标字段类型不一致")
        # 库内外语义必须在同一轮实验内保持不变。
        if pre_metrics["primary_stratum"] != post_metrics["primary_stratum"]:
            # 防止篡改分层以逃避退化标签。
            raise ValueError("dense-rerank comparison primary_stratum 不一致")
        # pre 保存完整候选，因此比较指标时只使用其 Top-10 前缀。
        pre_rows = pre_ranked_results_by_case_id[case_id]
        # post 也保存完整候选，比较同样使用其 Top-10 前缀。
        post_rows = post_ranked_results_by_case_id[case_id]
        # 两份完整列表都必须是至少十条的 JSON 数组。
        if not isinstance(pre_rows, list) or not isinstance(post_rows, list):
            # 不能从缺失排名推导逐题结论。
            raise ValueError("dense-rerank comparison 排名必须是列表")
        # 重排前后最终质量都由各自前十条计算。
        pre_identities = _validated_ranking_identities(
            pre_rows[:10], valid_identities=valid_identities
        )
        # post Top-10 identity 也必须属于当前冻结语料。
        post_identities = _validated_ranking_identities(
            post_rows[:10], valid_identities=valid_identities
        )
        # 库外题只记录 identity 是否变化，不制造质量胜负。
        if pre_metrics["primary_stratum"] == "out-of-domain":
            # 记录可观察的错误召回变化。
            cases[case_id] = {
                "kind": "out-of-domain",
                "top_10_identity_changed": pre_identities != post_identities,
            }
            # 当前库外题比较完成。
            continue
        # 读取固定质量键，顺序与 hybrid 比较完全一致。
        pre_key = _quality_key(pre_metrics)
        # 读取重排后质量键。
        post_key = _quality_key(post_metrics)
        # 默认完全相同为 unchanged。
        outcome = "unchanged"
        # 字典序变大时说明此题改善。
        if post_key > pre_key:
            # 保存明确改善标签。
            outcome = "improved"
        # 字典序变小时说明此题退化。
        elif post_key < pre_key:
            # 保存明确退化标签。
            outcome = "degraded"
        # 保存质量键与 Top-10 identity 变化，支持人工复核。
        cases[case_id] = {
            "kind": "in-domain",
            "outcome": outcome,
            # JSON 没有 tuple，发布前后都使用 list 才能事后重新校验。
            "pre_quality_key": list(pre_key),
            # 重排后质量键也显式保存为 JSON 原生数组。
            "post_quality_key": list(post_key),
            "top_10_identity_changed": pre_identities != post_identities,
        }
    # 返回与 hybrid comparison 同样的逐题对象结构。
    return {"cases": cases}


# 将报告中保存的冻结 case 定义还原为指标纯函数所需的不可变值对象。
def _case_from_report_definition(
    case_id: str,
    raw_definition: object,
    *,
    frozen_identities: set[tuple[str, int]],
) -> EvaluationCase:
    """严格校验 case 定义，使发布器能从 Top-10 重新计算指标。"""

    # 每题定义必须是 JSON 对象且明确保存问题、分层和人工相关块。
    if not isinstance(raw_definition, dict):
        # 不接受缺少冻结标注的半份报告。
        raise ValueError("dense-rerank details case 定义不合法")
    # 读取问题文本，指标函数不使用其语义但报告必须保留可追溯输入。
    question = raw_definition.get("question")
    # 读取固定主分层。
    primary_stratum = raw_definition.get("primary_stratum")
    # 读取相关块数组。
    relevant_rows = raw_definition.get("relevant")
    # 三项均需具有可验证类型。
    if (
        not isinstance(question, str)
        or not question.strip()
        or not isinstance(primary_stratum, str)
        or not primary_stratum
        or not isinstance(relevant_rows, list)
    ):
        # 缺失任一输入就不能重算逐题指标。
        raise ValueError("dense-rerank details case 定义字段不合法")
    # 逐项还原人工确认的 relevant identity。
    relevant: list[ChunkIdentity] = []
    # 不允许重复 identity 影响 Recall 分母。
    seen_relevant: set[tuple[str, int]] = set()
    # 逐项检查来源和块序号。
    for raw_identity in relevant_rows:
        # 每项必须是结构化对象。
        if not isinstance(raw_identity, dict):
            # 不从字符串解析 identity。
            raise ValueError("dense-rerank details relevant identity 不合法")
        # 读取稳定来源与块序号。
        source_name = raw_identity.get("source_name")
        # 读取块序号。
        chunk_index = raw_identity.get("chunk_index")
        # identity 必须真实属于已保存的冻结语料清单。
        identity = (source_name, chunk_index)
        if (
            not isinstance(source_name, str)
            or type(chunk_index) is not int
            or identity not in frozen_identities
            or identity in seen_relevant
        ):
            # 任何伪造、重复或库外引用都会阻断正式发布。
            raise ValueError("dense-rerank details relevant identity 不属于冻结语料")
        # 记录已见 identity。
        seen_relevant.add(identity)
        # 还原 metrics 纯函数依赖的值对象。
        relevant.append(ChunkIdentity(source_name, chunk_index))
    # 库内外与 relevant 空/非空关系必须保持原评测集规则。
    if (primary_stratum == "out-of-domain") != (not relevant):
        # 不允许通过修改分层逃避质量指标分母。
        raise ValueError("dense-rerank details case 库内外规则不一致")
    # tags 对指标无影响，报告中不重复保存，构造最小不可变案例。
    return EvaluationCase(
        case_id=case_id,
        question=question,
        relevant=tuple(relevant),
        primary_stratum=primary_stratum,
        tags=("out-of-domain",)
        if primary_stratum == "out-of-domain"
        else ("in-domain",),
    )


# 将计算出的 CaseMetrics 转成与 worker JSON 完全相同的可比较对象。
def _case_metrics_as_report(metrics: object) -> dict[str, Any]:
    """消除 dataclass 内 tuple 与 JSON list 的表示差异。"""

    # 指标函数返回固定 CaseMetrics，按字段显式组织避免隐式序列化漂移。
    return {
        "case_id": metrics.case_id,
        "primary_stratum": metrics.primary_stratum,
        "recall_at_5": metrics.recall_at_5,
        "recall_at_10": metrics.recall_at_10,
        "mrr_at_10": metrics.mrr_at_10,
        "all_relevant_hit_at_5": metrics.all_relevant_hit_at_5,
        "all_relevant_hit_at_10": metrics.all_relevant_hit_at_10,
        "hit_identities": [
            {"source_name": item.source_name, "chunk_index": item.chunk_index}
            for item in metrics.hit_identities
        ],
    }


# 在发布器边界验证 dense-rerank 的关键同候选和阶段计时证据。
def validate_dense_rerank_details(
    details: dict[str, Any],
    *,
    expected_input: dict[str, Any],
    smoke_directory: Path,
) -> None:
    """拒绝缺少 smoke、完整 pre/post 候选或阶段样本的 rerank 报告。"""

    # 本验证器只接受本阶段唯一的真实 evidence kind。
    if details.get("method") != "dense-rerank" or details.get(
        "evidence_kind"
    ) != "production-dense-rerank":
        # 防止其他历史报告误走 rerank schema。
        raise ValueError("dense-rerank details 身份不合法")
    # 关键顶层证据都必须存在为对象，不能以空值绕过发布。
    required_objects = (
        "input",
        "parameters",
        "timing",
        "metrics",
        "resources",
        "pre_ranked_results_by_case_id",
        "post_ranked_results_by_case_id",
        "ranked_results_by_case_id",
        "case_definitions_by_case_id",
        "pre_case_metrics_by_case_id",
        "case_metrics_by_case_id",
    )
    # 逐项验证 JSON object 结构。
    for field_name in required_objects:
        # 缺失、数组和标量都不能进入正式目录。
        if not isinstance(details.get(field_name), dict):
            # 指明字段便于调用方定位证据缺口。
            raise ValueError(f"dense-rerank details 缺少对象字段 {field_name}")
    # smoke_run_id 绑定此前已经成功的模型准备工件。
    smoke_run_id = details.get("smoke_run_id")
    # 仅接受安全非空的目录身份，不接收路径。
    if (
        not isinstance(smoke_run_id, str)
        or not smoke_run_id
        or "/" in smoke_run_id
        or "\\" in smoke_run_id
    ):
        # 没有 smoke 证据时不得发布正式 rerank 结论。
        raise ValueError("dense-rerank details smoke_run_id 不合法")
    # details 内的输入身份不能自行作为信任来源，必须精确匹配 CLI 已验证的冻结输入。
    if details["input"] != expected_input:
        # manifest、dataset、confirmation 或冻结 chunk 漂移都会破坏可比性。
        raise ValueError("dense-rerank details input 身份不一致")
    # 目录、成功 marker、固定 revision 与 smoke run ID 由独立 smoke gate 共同验证。
    validate_reranker_smoke_report(smoke_directory, smoke_run_id)
    # 读取已校验为对象的参数、计时与三组排名。
    parameters = details["parameters"]
    timing = details["timing"]
    pre_rankings = details["pre_ranked_results_by_case_id"]
    post_rankings = details["post_ranked_results_by_case_id"]
    final_rankings = details["ranked_results_by_case_id"]
    # 读取重排前后逐题指标，后续必须和排名及 comparison 相互证明。
    pre_case_metrics = details["pre_case_metrics_by_case_id"]
    # 读取重排后的逐题指标。
    post_case_metrics = details["case_metrics_by_case_id"]
    # 读取足以重算指标的冻结 case 定义。
    case_definitions = details["case_definitions_by_case_id"]
    # comparison 必须由当前 details 真实生成，不能缺失或手填。
    comparison = details.get("comparison")
    # resources 由 worker 采集，发布器必须验证其完整 schema。
    resources = details["resources"]
    # input 必须保存完整冻结语料 identity，而非只保存本轮候选。
    frozen_identity_rows = details["input"].get("frozen_chunk_identities")
    # 仅接受结构化 identity 列表。
    if not isinstance(frozen_identity_rows, list) or not frozen_identity_rows:
        # 没有冻结语料边界就不能验证报告排名的归属。
        raise ValueError("dense-rerank details 缺少冻结 identity 清单")
    # 还原并去重冻结语料 identity。
    frozen_identities: set[tuple[str, int]] = set()
    # 逐项验证不含路径的来源与非负块序号。
    for raw_identity in frozen_identity_rows:
        # 每项必须是 source_name 与 chunk_index 对象。
        if not isinstance(raw_identity, dict):
            raise ValueError("dense-rerank details 冻结 identity 不合法")
        # 读取两个稳定字段。
        source_name = raw_identity.get("source_name")
        # 读取块序号。
        chunk_index = raw_identity.get("chunk_index")
        # 格式、范围和重复都应显式拒绝。
        identity = (source_name, chunk_index)
        if (
            not isinstance(source_name, str)
            or not source_name
            or type(chunk_index) is not int
            or chunk_index < 0
            or identity in frozen_identities
        ):
            raise ValueError("dense-rerank details 冻结 identity 不合法")
        # 保存经过验证的语料 identity。
        frozen_identities.add(identity)
    # 固定实验参数不得在 worker 与报告之间漂移。
    if (
        parameters.get("candidate_k") != RERANKER_CANDIDATE_K
        or parameters.get("top_k") != 10
        or parameters.get("reranker_device") != "cpu"
        or parameters.get("reranker_batch_size") != RERANKER_BATCH_SIZE
        or parameters.get("reranker_max_length") != RERANKER_MAX_LENGTH
        or parameters.get("reranker_model_id") != RERANKER_MODEL_ID
        or parameters.get("reranker_revision") != RERANKER_MODEL_REVISION
        or parameters.get("warmup_rounds") != 1
        or parameters.get("measured_rounds") != 5
        or parameters.get("seed") != 20260726
    ):
        # 任何参数漂移都会破坏同口径比较。
        raise ValueError("dense-rerank details 固定参数不匹配")
    # 三类排名必须覆盖同一 case 集合，不能只发布共同子集。
    case_ids = set(pre_rankings)
    # pre、post 和 final 都需逐题完整存在。
    if not case_ids or not (
        case_ids
        == set(post_rankings)
        == set(final_rankings)
        == set(pre_case_metrics)
        == set(post_case_metrics)
        == set(case_definitions)
    ):
        # 不完整逐题证据不能支撑改善或退化结论。
        raise ValueError("dense-rerank details case id 集合不一致")
    # comparison 必须有完整 cases 对象，不能只保存汇总标签。
    if not isinstance(comparison, dict) or not isinstance(comparison.get("cases"), dict):
        # 缺少逐题结论时不能回答 rerank 的净收益。
        raise ValueError("dense-rerank details comparison 不合法")
    # comparison 也必须覆盖全部 case，不能遗漏退化题。
    if set(comparison["cases"]) != case_ids:
        # 不允许只发布改善子集。
        raise ValueError("dense-rerank details comparison case id 集合不一致")
    # 资源字段固定为设计承诺的可追溯运行环境信息。
    resource_string_fields = (
        "os",
        "cpu",
        "python_version",
        "torch_version",
        "sentence_transformers_version",
        "requested_device",
        "resolved_device",
    )
    # 字符串字段必须是非空文本。
    if any(
        not isinstance(resources.get(field_name), str)
        or not resources[field_name].strip()
        for field_name in resource_string_fields
    ):
        # 不让缺失的设备或版本信息伪装成正式资源证据。
        raise ValueError("dense-rerank details resources 字符串字段不合法")
    # 总物理内存必须是实际正整数，不能使用 not_measured 例外。
    if type(resources.get("total_memory_bytes")) is not int or resources[
        "total_memory_bytes"
    ] <= 0:
        # 该字段用于解释本机模型加载能力。
        raise ValueError("dense-rerank details total_memory_bytes 不合法")
    # 进程峰值允许设计批准的 not_measured，若有数值则必须为非负整数。
    peak_process_rss = resources.get("peak_process_rss")
    # 保留已批准的跨平台采集例外。
    if peak_process_rss != "not_measured" and (
        type(peak_process_rss) is not int or peak_process_rss < 0
    ):
        # 不接受其他文本、bool 或负数。
        raise ValueError("dense-rerank details peak_process_rss 不合法")
    # 正式运行固定请求并实际解析为 CPU，避免报告在设备身份上漂移。
    if resources["requested_device"] != "cpu" or resources["resolved_device"] != "cpu":
        # 当前设计没有 CUDA 分支。
        raise ValueError("dense-rerank details resources device 不匹配")
    # 每题逐项验证前后候选 identity 集合与最终前缀。
    for case_id in case_ids:
        # 读取三份当前题排名列表。
        pre = pre_rankings[case_id]
        post = post_rankings[case_id]
        final = final_rankings[case_id]
        # 完整候选必须等于固定候选宽度，最终排名必须精确十条。
        if not isinstance(pre, list) or not isinstance(post, list) or not isinstance(final, list):
            # JSON 类型错误时不允许继续发布。
            raise ValueError("dense-rerank details 排名必须是列表")
        if (
            len(pre) != RERANKER_CANDIDATE_K
            or len(post) != RERANKER_CANDIDATE_K
            or len(final) != 10
        ):
            # 不把短候选当成设计规定的完整候选实验。
            raise ValueError("dense-rerank details 排名长度不匹配")
        # 逐项提取连续排名和唯一 identity。
        def identities(rows: list[Any], method: str) -> list[tuple[str, int]]:
            # 保存已按 rank 验证的身份。
            result: list[tuple[str, int]] = []
            # 每条必须为对象并具有连续名次。
            for expected_rank, row in enumerate(rows, start=1):
                # 原始 JSON 字段需要完整、严格类型校验。
                if (
                    not isinstance(row, dict)
                    or row.get("rank") != expected_rank
                    or row.get("method") != method
                    or not isinstance(row.get("source_name"), str)
                    or type(row.get("chunk_index")) is not int
                ):
                    # 报告内排序结构不可信时停止发布。
                    raise ValueError("dense-rerank details 排名项不合法")
                # post 项额外声明真实 raw logit 的语义与方向。
                if method == "dense-rerank" and (
                    row.get("score_kind") != "bge_reranker_logit"
                    or row.get("higher_is_better") is not True
                    or type(row.get("score")) not in {int, float}
                    or not math.isfinite(float(row["score"]))
                ):
                    # 不能将 reranker logit 伪装成 distance 或概率。
                    raise ValueError("dense-rerank details post 分数元数据不合法")
                # 组合稳定 identity 并拒绝重复候选。
                identity = (row["source_name"], row["chunk_index"])
                if identity in result:
                    # 同一文本块不能重复占据一个排名列表。
                    raise ValueError("dense-rerank details 包含重复 identity")
                # 保存已经验证的 identity。
                result.append(identity)
            # 返回用于同候选和前缀比较的顺序列表。
            return result

        # dense pre 必须保留原策略方法身份。
        pre_ids = identities(pre, "dense")
        # post 必须是 reranker logit 结果。
        post_ids = identities(post, "dense-rerank")
        # final 是 post 完整列表的前十项，保持 rerank 方法身份。
        final_ids = identities(final, "dense-rerank")
        # 完整 pre/post 只能换顺序，identity 集合必须完全相同。
        if set(pre_ids) != set(post_ids):
            # 候选变化会让质量变化无法归因于 rerank。
            raise ValueError("dense-rerank details pre/post 候选集合不一致")
        # 每个候选都必须属于报告已声明的冻结语料，而不是只满足格式。
        if not set(pre_ids).issubset(frozen_identities):
            # 伪造文件名或块号不能参与质量结论。
            raise ValueError("dense-rerank details 排名 identity 不属于冻结语料")
        # final 必须是 post 排名的前缀，不能另行查询或排序。
        if final_ids != post_ids[:10]:
            # 截断来源不一致时不能发布最终 Top-10。
            raise ValueError("dense-rerank details final 不是 post 前缀")
        # 逐题 metrics 必须结构完整并回指正确 case id。
        _validate_case_metrics(case_id, pre_case_metrics[case_id])
        # post metrics 同样不能遗漏指标或命中 identity 字段。
        _validate_case_metrics(case_id, post_case_metrics[case_id])
        # 重排前后库内外分层不能在报告阶段被篡改。
        if (
            pre_case_metrics[case_id]["primary_stratum"]
            != post_case_metrics[case_id]["primary_stratum"]
        ):
            # 分层漂移会让库外题错误参与质量比较。
            raise ValueError("dense-rerank details primary_stratum 不一致")
        # 指标字段类型也必须完全一致，防止 None 与数值互换。
        if _case_metric_schema(pre_case_metrics[case_id]) != _case_metric_schema(
            post_case_metrics[case_id]
        ):
            # 类型不一致说明逐题证据口径已漂移。
            raise ValueError("dense-rerank details 逐题指标字段类型不一致")
        # 还原冻结案例，作为从 Top-10 重算指标的唯一标注来源。
        case = _case_from_report_definition(
            case_id,
            case_definitions[case_id],
            frozen_identities=frozen_identities,
        )
        # 重排前指标只能由 dense 完整候选的前十条派生。
        expected_pre_metrics = _case_metrics_as_report(
            compute_case_metrics(
                case,
                [ChunkIdentity(source_name, chunk_index) for source_name, chunk_index in pre_ids[:10]],
            )
        )
        # 重排后指标只能由最终 post 前十条派生。
        expected_post_metrics = _case_metrics_as_report(
            compute_case_metrics(
                case,
                [ChunkIdentity(source_name, chunk_index) for source_name, chunk_index in final_ids],
            )
        )
        # 指标任一字段被篡改都不能与当前排名和冻结标注重新对应。
        if pre_case_metrics[case_id] != expected_pre_metrics:
            raise ValueError("dense-rerank details pre 指标与排名不一致")
        # 同样拒绝 post 指标与最终排名不一致。
        if post_case_metrics[case_id] != expected_post_metrics:
            raise ValueError("dense-rerank details post 指标与排名不一致")
    # 冷启动必须是实际非负有限时长，不能用负值或 NaN 伪装成功运行。
    cold_start_ms = timing.get("cold_start_ms")
    if (
        type(cold_start_ms) not in {int, float}
        or not math.isfinite(float(cold_start_ms))
        or cold_start_ms < 0
    ):
        # 冷启动属于正式报告固定口径，缺失或异常时不能发布。
        raise ValueError("dense-rerank details cold_start_ms 不合法")
    # 三组延迟样本都要覆盖所有 case 和正式轮次。
    expected_sample_count = len(case_ids) * parameters.get("measured_rounds", 0)
    # 保存已逐项校验过的样本，后续从原始值重新计算所有汇总。
    latency_samples_by_field: dict[str, list[float]] = {}
    # 样本键固定并逐一检查有限非负数。
    for field_name in (
        "total_latency_samples_ms",
        "dense_latency_samples_ms",
        "rerank_latency_samples_ms",
    ):
        # 当前样本从 timing 读取，不能只提交百分位汇总。
        samples = timing.get(field_name)
        # 长度必须与 case_count * measured_rounds 精确对应。
        if not isinstance(samples, list) or len(samples) != expected_sample_count:
            # 缺一题或多一题都会让 P50/P95 失去固定口径。
            raise ValueError("dense-rerank details 延迟样本数量不匹配")
        # 每项必须是有限且非负的真实计时数值。
        if any(
            type(sample) not in {int, float}
            or not math.isfinite(float(sample))
            or sample < 0
            for sample in samples
        ):
            # 不允许 NaN、Infinity 或负耗时进入报告。
            raise ValueError("dense-rerank details 延迟样本不合法")
        # 统一为 float 后保存，避免信任报告中手填的百分位。
        latency_samples_by_field[field_name] = [float(sample) for sample in samples]
    # total 包含 dense、rerank 和必要编排校验；每个对应调用都不能比两个阶段之和更短。
    for total_ms, dense_ms, rerank_ms in zip(
        latency_samples_by_field["total_latency_samples_ms"],
        latency_samples_by_field["dense_latency_samples_ms"],
        latency_samples_by_field["rerank_latency_samples_ms"],
        strict=True,
    ):
        # 使用已验证且长度相等的三组样本，拒绝同步篡改 raw total 与百分位。
        if total_ms < dense_ms + rerank_ms:
            # 不可能的阶段关系不能构成可信热路径证据。
            raise ValueError("dense-rerank details total 延迟小于阶段之和")
    # 总延迟样本数量与设计的正式调用次数必须完全相同。
    if timing.get("latency_sample_count") != expected_sample_count:
        # 不让错误样本数误导延迟统计的分母。
        raise ValueError("dense-rerank details latency_sample_count 不一致")
    # 每项 P50/P95 都必须由对应 raw samples 按固定线性插值规则重算。
    percentile_fields = {
        "total_latency_samples_ms": ("latency_p50_ms", "latency_p95_ms"),
        "dense_latency_samples_ms": (
            "dense_latency_p50_ms",
            "dense_latency_p95_ms",
        ),
        "rerank_latency_samples_ms": (
            "rerank_latency_p50_ms",
            "rerank_latency_p95_ms",
        ),
    }
    # 对三组原始样本分别验证 P50 与 P95，阻止 JSON 落盘后手填汇总漂移。
    for sample_field, (p50_field, p95_field) in percentile_fields.items():
        # 当前样本已完成有限性与长度校验。
        samples = latency_samples_by_field[sample_field]
        # 由公共纯函数重算设计约定的两个百分位。
        expected_p50 = linear_percentile(samples, 0.50)
        # P95 同样只能由这组 raw samples 推导。
        expected_p95 = linear_percentile(samples, 0.95)
        # 检查报告中两个汇总值的类型、有限性与精确重算结果。
        for field_name, expected_value in ((p50_field, expected_p50), (p95_field, expected_p95)):
            # 读取当前手填或 worker 写入的汇总数值。
            actual_value = timing.get(field_name)
            # 浮点 JSON 往返允许极小误差，但不允许任何实质漂移。
            if (
                type(actual_value) not in {int, float}
                or not math.isfinite(float(actual_value))
                or not math.isclose(
                    float(actual_value), expected_value, rel_tol=0.0, abs_tol=1e-12
                )
            ):
                # 汇总与原始样本不一致时正式报告不可复核。
                raise ValueError("dense-rerank details 延迟百分位不一致")
    # 根据当前完整 pre/post 证据重新计算逐题结论，拒绝手填 comparison。
    expected_comparison = build_dense_rerank_comparison(
        pre_case_metrics,
        post_case_metrics,
        pre_rankings,
        post_rankings,
        valid_identities=frozen_identities,
    )
    # 只有完全一致的 comparison 才能进入正式报告。
    if comparison != expected_comparison:
        # 任意篡改 improved/degraded/unchanged 或库外变化都会被阻断。
        raise ValueError("dense-rerank details comparison 与逐题证据不一致")
    # 从 post 逐题指标重新聚合汇总质量，避免只信任 worker 的手填 summary。
    in_domain_metrics = [
        post_case_metrics[case_id]
        for case_id in sorted(case_ids)
        if post_case_metrics[case_id]["primary_stratum"] != "out-of-domain"
    ]
    # 当前冻结集必须至少有一条库内题才能定义 Recall/MRR 宏平均。
    if not in_domain_metrics:
        # 缺少质量分母时正式报告没有实验意义。
        raise ValueError("dense-rerank details 缺少库内逐题指标")
    # 计算三个固定库内质量指标的宏平均。
    expected_quality_averages = {
        field_name: sum(float(item[field_name]) for item in in_domain_metrics)
        / len(in_domain_metrics)
        for field_name in ("recall_at_5", "recall_at_10", "mrr_at_10")
    }
    # 多线索指标只由非 None 的案例参与平均，保持现有 metrics 语义。
    for field_name in ("all_relevant_hit_at_5", "all_relevant_hit_at_10"):
        # 收集有该指标语义的多线索题布尔结果。
        values = [
            item[field_name]
            for item in in_domain_metrics
            if item[field_name] is not None
        ]
        # 没有多线索题时汇总应保持 None。
        expected_quality_averages[field_name] = (
            None if not values else sum(int(value) for value in values) / len(values)
        )
    # 汇总对象必须包含与逐题结果一致的样本数量。
    if (
        details["metrics"].get("in_domain_case_count") != len(in_domain_metrics)
        or details["metrics"].get("out_of_domain_case_count")
        != len(case_ids) - len(in_domain_metrics)
    ):
        # 不能用错误分母放大或缩小质量结论。
        raise ValueError("dense-rerank details metrics 案例数量不一致")
    # 汇总数值必须是有限数，并与逐题重新计算结果一致。
    for field_name, expected_value in expected_quality_averages.items():
        # 读取当前报告中的汇总字段。
        actual_value = details["metrics"].get(field_name)
        # None 只允许匹配没有多线索题的预期。
        if expected_value is None:
            # 不能把不存在的指标伪装成零或任意分数。
            if actual_value is not None:
                raise ValueError("dense-rerank details metrics 多线索指标不一致")
            # 当前字段已验证完成。
            continue
        # 非 None 汇总必须是有限的普通数值。
        if (
            type(actual_value) not in {int, float}
            or not math.isfinite(float(actual_value))
            or not math.isclose(float(actual_value), expected_value, rel_tol=0.0, abs_tol=1e-12)
        ):
            # 发布器不接受与逐题明细脱节的平均数。
            raise ValueError("dense-rerank details metrics 质量汇总不一致")


# 从已发布 dense 报告加载并严格校验可比较基线。
def build_hybrid_comparison(
    baseline_directory: Path,
    hybrid_details: dict[str, Any],
    *,
    valid_identities: set[tuple[str, int]],
) -> dict[str, Any]:
    """验证同口径 dense 证据后，生成逐题改善、退化或 identity 变化。"""

    # 基线目录只能使用其根下不可变的 details.json。
    baseline_path = baseline_directory / "details.json"
    # 缺失机器可读证据时不能用 Markdown 猜测数据。
    if not baseline_path.is_file():
        # fail-closed，避免不完整历史报告参与正式对比。
        raise ValueError("baseline-report 缺少 details.json")
    # 读取已发布的 JSON 明细。
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    # 基线顶层必须是对象。
    if not isinstance(baseline, dict):
        # 数组或标量无法表达固定报告契约。
        raise ValueError("baseline details.json 顶层必须是对象")
    # 目录名、报告 run id 与 hybrid 引用的基线 id 必须三者一致。
    baseline_run_id = baseline.get("evaluation_run_id")
    # hybrid 必须显式携带 comparison 中的引用身份。
    if not isinstance(baseline_run_id, str) or baseline_directory.name != baseline_run_id:
        # 不允许把任意目录伪装成基线证据。
        raise ValueError("baseline-report 目录名与运行身份不一致")
    # 基线只能是历史 production-dense 结果。
    if baseline.get("method") != "dense" or baseline.get("evidence_kind") != "production-dense":
        # hybrid 或测试结果不能充当 dense 基线。
        raise ValueError("baseline-report 必须是 production-dense")
    # 读取双方输入和参数对象。
    baseline_input = baseline.get("input")
    # 读取本次 hybrid 的输入对象。
    hybrid_input = hybrid_details.get("input")
    # 读取双方运行参数。
    baseline_parameters = baseline.get("parameters")
    # 读取本次运行参数。
    hybrid_parameters = hybrid_details.get("parameters")
    # 四个嵌套对象缺失时都不能继续比较。
    if not all(
        isinstance(value, dict)
        for value in (baseline_input, hybrid_input, baseline_parameters, hybrid_parameters)
    ):
        # 防止缺字段被错误视为相同。
        raise ValueError("报告缺少可比较的 input 或 parameters")
    # 固定输入身份必须完全一致。
    for field_name in (
        "manifest_sha256",
        "dataset_sha256",
        "confirmation_confirmed_at",
    ):
        # 任一 hash 或确认时间漂移都使比较失效。
        if baseline_input.get(field_name) != hybrid_input.get(field_name):
            # 不在不同冻结输入之间比较质量指标。
            raise ValueError(f"baseline-report {field_name} 不一致")
    # 固定测量口径也必须完全相同。
    for field_name in ("top_k", "warmup_rounds", "measured_rounds", "seed"):
        # 不同 Top-K 或轮次会让质量和延迟不可比。
        if baseline_parameters.get(field_name) != hybrid_parameters.get(field_name):
            # 明确指出哪项运行参数漂移。
            raise ValueError(f"baseline-report {field_name} 不一致")
    # 读取逐题指标和排名明细。
    baseline_metrics = baseline.get("case_metrics_by_case_id")
    # 读取 hybrid 的逐题指标。
    hybrid_metrics = hybrid_details.get("case_metrics_by_case_id")
    # 读取 baseline 的 Top-10 结果。
    baseline_rankings = baseline.get("ranked_results_by_case_id")
    # 读取 hybrid 的 Top-10 结果。
    hybrid_rankings = hybrid_details.get("ranked_results_by_case_id")
    # 四份逐题证据必须都为对象。
    if not all(
        isinstance(value, dict)
        for value in (baseline_metrics, hybrid_metrics, baseline_rankings, hybrid_rankings)
    ):
        # 缺少逐题证据时不能把汇总数值发布为比较结论。
        raise ValueError("报告缺少逐题指标或排名明细")
    # 两份报告的每类 case id 集合都必须完整且相同。
    case_ids = set(hybrid_metrics)
    # 四个集合任何一个不同都表示证据不完整或输入漂移。
    if not (
        case_ids == set(baseline_metrics) == set(baseline_rankings) == set(hybrid_rankings)
    ):
        # 不允许只比较共同子集。
        raise ValueError("baseline-report 与 hybrid 的 case id 集合不一致")
    # 保存每题可供报告审阅的明确结论。
    cases: dict[str, dict[str, Any]] = {}
    # 逐题验证排序契约并比较同一组指标。
    for case_id in sorted(case_ids):
        # 取出两侧单题指标对象。
        baseline_case = baseline_metrics[case_id]
        # 取出本次单题指标对象。
        hybrid_case = hybrid_metrics[case_id]
        # 两侧都必须满足完整逐题指标 schema，不能只校验比较键字段。
        _validate_case_metrics(case_id, baseline_case)
        # 同样拒绝 hybrid 侧任何未参与比较但已漂移的字段。
        _validate_case_metrics(case_id, hybrid_case)
        # 两侧所有逐题字段类型必须严格相同，不能只比较质量键。
        if _case_metric_schema(baseline_case) != _case_metric_schema(hybrid_case):
            # 库外题的 None 被替换为数值等漂移同样必须失败。
            raise ValueError("逐题指标字段类型不一致")
        # 分层必须相同，库内外语义不能漂移。
        if (
            not isinstance(baseline_case, dict)
            or not isinstance(hybrid_case, dict)
            or baseline_case.get("primary_stratum") != hybrid_case.get("primary_stratum")
        ):
            # 指标结构漂移时不能给出改善标签。
            raise ValueError("逐题 primary_stratum 不一致")
        # 将两侧排名转换为经过校验的稳定 identity 列表。
        baseline_identities = _validated_ranking_identities(
            baseline_rankings[case_id],
            valid_identities=valid_identities,
        )
        # 同样校验 hybrid 的排名明细。
        hybrid_identities = _validated_ranking_identities(
            hybrid_rankings[case_id],
            valid_identities=valid_identities,
        )
        # 库外题不参与质量胜负，只记录返回 identity 是否变化。
        if baseline_case["primary_stratum"] == "out-of-domain":
            # 保存不带改善或退化含义的变化事实。
            cases[case_id] = {
                "kind": "out-of-domain",
                "top_10_identity_changed": baseline_identities != hybrid_identities,
            }
            # 当前库外题完成，继续下一题。
            continue
        # 指标字典序按设计固定，None 或错误类型都拒绝。
        baseline_key = _quality_key(baseline_case)
        # 读取 hybrid 的同口径质量键。
        hybrid_key = _quality_key(hybrid_case)
        # 大于基线才称改善，小于才称退化。
        outcome = "unchanged"
        # 字典序比较覆盖 Recall@10、Recall@5、MRR@10 和多线索完成度。
        if hybrid_key > baseline_key:
            # 保存库内改善事实。
            outcome = "improved"
        # 低于基线时明确登记退化。
        elif hybrid_key < baseline_key:
            # 保存库内退化事实。
            outcome = "degraded"
        # 保存质量键与身份变化，便于逐题审阅。
        cases[case_id] = {
            "kind": "in-domain",
            "outcome": outcome,
            "baseline_quality_key": baseline_key,
            "hybrid_quality_key": hybrid_key,
            "top_10_identity_changed": baseline_identities != hybrid_identities,
        }
    # 返回基线身份与逐题结论，供 hybrid details 原样保存。
    return {"baseline_run_id": baseline_run_id, "cases": cases}


# 复用 dense 基线比较的严格输入与逐题指标校验，额外补齐改写快照的可审阅文本。
def build_rewrite_dense_comparison(
    baseline_directory: Path,
    rewrite_details: dict[str, Any],
    *,
    valid_identities: set[tuple[str, int]],
) -> dict[str, Any]:
    """生成 dense 原问题检索与 rewrite-dense 检索之间的逐题可审阅比较。"""

    # 改写报告必须先声明唯一对应的生产证据身份。
    if (
        rewrite_details.get("method") != "rewrite-dense"
        or rewrite_details.get("evidence_kind") != "production-rewrite-dense"
    ):
        # 禁止其他实验方法借用此函数伪装成改写结论。
        raise ValueError("rewrite-dense comparison 需要 production-rewrite-dense 证据")
    # 先复用同一冻结输入、逐题指标和排名的完整基线校验。
    generic_comparison = build_hybrid_comparison(
        baseline_directory,
        rewrite_details,
        valid_identities=valid_identities,
    )
    # 读取 worker 回传的快照记录，不能由 CLI 手工拼写。
    rewrite_records = rewrite_details.get("rewrite_records_by_case_id")
    # 读取前后排名，供人工在报告中直接核对检索差异。
    baseline = json.loads((baseline_directory / "details.json").read_text(encoding="utf-8"))
    baseline_rankings = baseline.get("ranked_results_by_case_id")
    rewrite_rankings = rewrite_details.get("ranked_results_by_case_id")
    # 三份逐题对象都必须完整，避免只展示表现好的改写题。
    if not all(isinstance(value, dict) for value in (rewrite_records, baseline_rankings, rewrite_rankings)):
        # 快照文本或前后排名缺失时不能输出可审阅比较。
        raise ValueError("rewrite-dense comparison 缺少逐题改写或排名证据")
    # 快照、基线与本次检索必须覆盖完全相同的 case 集合。
    case_ids = set(generic_comparison["cases"])
    if case_ids != set(rewrite_records) or case_ids != set(baseline_rankings) or case_ids != set(rewrite_rankings):
        # 不允许对共同子集给出“改写有效”的结论。
        raise ValueError("rewrite-dense comparison case id 集合不一致")
    # 保存替换了 generic 命名的最终逐题结果。
    cases: dict[str, dict[str, Any]] = {}
    for case_id in sorted(case_ids):
        # 当前记录必须是 worker 从已验证快照复制的对象。
        record = rewrite_records[case_id]
        semantic_review = record.get("semantic_review") if isinstance(record, dict) else None
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("question"), str)
            or not record["question"].strip()
            or not isinstance(record.get("rewritten_query"), str)
            or not record["rewritten_query"].strip()
            or not isinstance(semantic_review, dict)
            or semantic_review.get("status") != "accepted"
        ):
            # 缺少原问题、改写文本或人工接受结论均不能发布。
            raise ValueError("rewrite-dense comparison 改写记录不合法")
        # 复用已验证的通用质量结论，并换成改写实验自身的字段名称。
        generic_case = generic_comparison["cases"][case_id]
        case = {
            "question": record["question"],
            "rewritten_query": record["rewritten_query"],
            "pre_top_10": baseline_rankings[case_id],
            "post_top_10": rewrite_rankings[case_id],
            **generic_case,
        }
        # 名称明确体现前后实验，而不是误称 hybrid。
        if case.get("kind") == "in-domain":
            case["pre_quality_key"] = case.pop("baseline_quality_key")
            case["post_quality_key"] = case.pop("hybrid_quality_key")
        cases[case_id] = case
    # 保留基线 run identity，供报告追溯其原问题检索来源。
    return {"baseline_run_id": generic_comparison["baseline_run_id"], "cases": cases}


# 校验并提取一题 Top-10 的稳定 identity 列表。
def _validated_ranking_identities(
    raw_results: object,
    *,
    valid_identities: set[tuple[str, int]],
) -> list[tuple[str, int]]:
    """拒绝重复、跳号或非法 identity 的报告排名。"""

    # 排名必须是 JSON 数组且最多十条。
    if not isinstance(raw_results, list) or len(raw_results) > 10:
        # 报告不能绕开固定 Top-10 契约。
        raise ValueError("报告排名必须是至多十条的列表")
    # 保存按 rank 顺序读取的 identity。
    identities: list[tuple[str, int]] = []
    # 逐项验证 rank、来源和块序号。
    for expected_rank, result in enumerate(raw_results, start=1):
        # 每项都必须是 JSON 对象。
        if not isinstance(result, dict):
            # 不接受字符串或数组伪装的结果。
            raise ValueError("报告排名项必须是对象")
        # 来源必须是非空纯文件名，块序号必须是普通非负整数。
        source_name = result.get("source_name")
        # 读取 chunk index。
        chunk_index = result.get("chunk_index")
        # rank 必须从一连续编号。
        if (
            result.get("rank") != expected_rank
            or not isinstance(source_name, str)
            or not source_name
            or "/" in source_name
            or "\\" in source_name
            or type(chunk_index) is not int
            or chunk_index < 0
        ):
            # 无法证明身份与顺序时停止发布。
            raise ValueError("报告排名不满足稳定 identity 契约")
        # 同一 chunk 不能在单题 Top-10 重复出现。
        identity = (source_name, chunk_index)
        # 报告引用的 identity 必须真实属于当前冻结语料快照。
        if identity not in valid_identities:
            # 格式合法但不存在的块不能参与 baseline 比较。
            raise ValueError("报告排名 identity 不属于冻结语料")
        # 重复会污染 Recall 计数和人工审阅。
        if identity in identities:
            # 不静默去重以隐藏策略 bug。
            raise ValueError("报告排名包含重复 identity")
        # 保存已经验证的 identity。
        identities.append(identity)
    # 返回可安全比较的 identity 顺序。
    return identities


# 将库内单题指标转换为设计声明的字典序比较键。
def _quality_key(metrics: dict[str, Any]) -> tuple[float, float, float, int]:
    """返回 Recall@10、Recall@5、MRR@10、全相关命中的固定比较顺序。"""

    # 读取前三个必须为数值的指标。
    values = [metrics.get(name) for name in ("recall_at_10", "recall_at_5", "mrr_at_10")]
    # None、布尔和其他类型都不具备可比较的质量语义。
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        # 拒绝缺失逐题指标。
        raise ValueError("库内逐题指标类型不合法")
    # 多线索指标为 True 记一，False 或非多线索 None 记零。
    all_relevant = metrics.get("all_relevant_hit_at_10")
    # 非 bool、非 None 的值代表报告结构漂移。
    if all_relevant is not None and type(all_relevant) is not bool:
        # 不能把字符串等真值当成通过。
        raise ValueError("all_relevant_hit_at_10 类型不合法")
    # 返回 Python 元组，天然按设计顺序执行字典序比较。
    return float(values[0]), float(values[1]), float(values[2]), int(all_relevant is True)


# 校验逐题指标的所有公开字段，保证基线比较不会遗漏结构漂移。
def _validate_case_metrics(case_id: str, metrics: object) -> None:
    """拒绝 case id、指标、命中 identity 或多线索字段的类型漂移。"""

    # 每题指标必须是 JSON 对象且回指当前 case id。
    if not isinstance(metrics, dict) or metrics.get("case_id") != case_id:
        # 不能把别题指标混入当前比较。
        raise ValueError("逐题指标 case_id 不一致")
    # 分层必须为非空字符串，后续会继续校验双方完全相同。
    if not isinstance(metrics.get("primary_stratum"), str):
        # 库内外语义缺失时不能比较。
        raise ValueError("逐题 primary_stratum 类型不合法")
    # 三个质量指标都必须同时为有限数值或同时为 None。
    quality_values = [metrics.get(name) for name in ("recall_at_5", "recall_at_10", "mrr_at_10")]
    # 不允许部分缺失、bool 或非数值伪装的指标。
    if not (
        all(value is None for value in quality_values)
        or all(
            type(value) in {int, float} and math.isfinite(float(value))
            for value in quality_values
        )
    ):
        # 库内外指标结构必须保持一致。
        raise ValueError("逐题质量指标类型不合法")
    # 两个多线索字段只允许 bool 或 None。
    for field_name in ("all_relevant_hit_at_5", "all_relevant_hit_at_10"):
        # 读取当前多线索字段。
        value = metrics.get(field_name)
        # 不接受 0、1 或字符串等隐式布尔值。
        if value is not None and type(value) is not bool:
            # 防止比较键以外的指标静默漂移。
            raise ValueError("逐题多线索指标类型不合法")
    # 命中 identity 必须是列表，供人工审阅逐项追溯。
    hit_identities = metrics.get("hit_identities")
    # 缺失或非列表都不是完整报告。
    if not isinstance(hit_identities, list):
        # 不允许字符串伪装多个 identity。
        raise ValueError("逐题 hit_identities 类型不合法")
    # 逐项验证命中 identity 的结构。
    for identity in hit_identities:
        # 每项必须有非空来源和普通非负块序号。
        if (
            not isinstance(identity, dict)
            or not isinstance(identity.get("source_name"), str)
            or not identity["source_name"]
            or type(identity.get("chunk_index")) is not int
            or identity["chunk_index"] < 0
        ):
            # 不完整命中记录会破坏逐题可审阅性。
            raise ValueError("逐题 hit_identities 结构不合法")


# 提取逐题指标的完整类型结构，用于检测两份报告之间的 schema 漂移。
def _case_metric_schema(metrics: object) -> tuple[object, ...]:
    """返回包含嵌套 hit identity 元素类型的稳定 schema 指纹。"""

    # 调用方已先运行完整校验，因此这里可以安全读取对象字段。
    assert isinstance(metrics, dict)
    # 固定公开字段顺序，避免字典插入顺序影响比较。
    field_types = tuple(
        type(metrics[name])
        for name in (
            "case_id",
            "primary_stratum",
            "recall_at_5",
            "recall_at_10",
            "mrr_at_10",
            "all_relevant_hit_at_5",
            "all_relevant_hit_at_10",
        )
    )
    # 返回标量字段和列表容器类型；列表元素已由单侧校验逐项验证。
    # 空/非空是检索质量结果，不属于跨报告的 schema 身份。
    return field_types + (type(metrics["hit_identities"]),)


# 根据同一个 details 字典生成简短的人读摘要。
def _render_summary(details: dict[str, Any]) -> str:
    # 读取已经由发布器校验过的运行身份。
    run_id = details["evaluation_run_id"]
    # 读取方法名，不存在时用明确占位说明数据不完整。
    method = details.get("method", "unknown")
    # 读取证据种类，帮助读者区分正式基线与测试产物。
    evidence_kind = details.get("evidence_kind", "unknown")
    # 读取可选汇总指标；正式 worker 会提供该对象。
    metrics = details.get("metrics", {})
    # 读取可选计时对象；正式 worker 会提供冷启动与热路径百分位。
    timing = details.get("timing", {})
    # 先输出摘要标题和可追溯身份。
    lines = [
        "# M2 检索评测摘要",
        "",
        f"- evaluation_run_id：`{run_id}`",
        f"- method：`{method}`",
        f"- evidence_kind：`{evidence_kind}`",
    ]
    # 只在 metrics 是对象时展示其键值。
    if isinstance(metrics, dict):
        # 按键排序让同一证据在不同运行中保持稳定阅读顺序。
        for key in sorted(metrics):
            # JSON 可序列化文本避免手工拼接复杂值。
            value = json.dumps(metrics[key], ensure_ascii=False)
            # 每个汇总数值独立成行，便于新手阅读。
            lines.append(f"- {key}：`{value}`")
    # 只在 timing 是对象时展示其键值，避免把缺失计时伪装成 0。
    if isinstance(timing, dict):
        # 计时字段同样按键排序，保证 Markdown 稳定。
        for key in sorted(timing):
            # 使用 JSON 编码，兼容数值和 null。
            value = json.dumps(timing[key], ensure_ascii=False)
            # 每个计时字段独立成行，方便和质量指标对照。
            lines.append(f"- {key}：`{value}`")
    # 说明报告的证据边界，避免被误读为医学基准。
    lines.extend(
        [
            "",
            "> 本报告只描述固定项目评测集上的检索表现，不代表医学基准或临床有效性。",
            "",
        ]
    )
    # 返回以单个换行结尾的 UTF-8 Markdown。
    return "\n".join(lines)


# 校验并发布同一 run 的 JSON 明细和 Markdown 摘要。
def publish_run_report(
    reports_dir: Path,
    evaluation_run_id: str,
    details: dict[str, Any],
    *,
    expected_dense_rerank_input: dict[str, Any] | None = None,
    reranker_smoke_directory: Path | None = None,
    expected_rewrite_dense_input: dict[str, Any] | None = None,
    rewrite_snapshot_id: str | None = None,
) -> Path:
    """写 staging 后整体发布，不覆盖任何既有正式 run 目录。"""

    # run ID 必须是安全的非空文件名，不能带目录分隔符。
    if not isinstance(evaluation_run_id, str) or not SAFE_RUN_ID.fullmatch(
        evaluation_run_id
    ):
        # 不允许调用方通过 run ID 控制目标路径。
        raise ValueError("evaluation_run_id 必须是安全的非空文件名")
    # details 必须包含同一个 run ID，防止 JSON 与目录错配。
    if details.get("evaluation_run_id") != evaluation_run_id:
        # 报告身份不一致时不能发布任何正式目录。
        raise ValueError("details 的 evaluation_run_id 与目标目录不一致")
    # 正式发布 gate 只接受真实 dense、hybrid 或 dense-rerank worker 的生产证据。
    # 测试 fake 或任意手填 details 即使有 run ID，也不能进入 reports 目录。
    if details.get("evidence_kind") not in {
        "production-dense",
        "production-hybrid",
        "production-dense-rerank",
        "production-rewrite-dense",
    }:
        # 该检查把“能否写正式报告”固定在发布器，而不是只靠 CLI 自觉。
        raise ValueError(
            "正式报告只接受 production-dense、production-hybrid 或 "
            "production-dense-rerank 或 production-rewrite-dense 证据"
        )
    # 方法和证据种类必须一一对应，防止伪造混合身份的正式报告。
    if details.get("evidence_kind") != f"production-{details.get('method')}":
        # 不允许 dense 内容携带 hybrid 证据标记，或反向伪装。
        raise ValueError("正式报告 method 与 evidence_kind 不一致")
    # rerank 额外在发布器内执行专用 gate，避免调用方绕过 CLI 预校验。
    if details.get("evidence_kind") == "production-dense-rerank":
        # 只有 CLI 已验证的冻结输入和独立成功 smoke 才能作为报告的信任来源。
        if expected_dense_rerank_input is None or reranker_smoke_directory is None:
            # 不让 details 自己声明的 hash 或 smoke ID 绕过发布边界。
            raise ValueError("dense-rerank 发布缺少可信输入或 smoke 上下文")
        # schema 未通过时 staging 目录尚未创建，报告目录不会出现。
        validate_dense_rerank_details(
            details,
            expected_input=expected_dense_rerank_input,
            smoke_directory=reranker_smoke_directory,
        )
    # rewrite-dense 同样必须使用 CLI 已复验的冻结输入和快照身份，不能只相信 details 自报。
    if details.get("evidence_kind") == "production-rewrite-dense":
        # 两份可信上下文缺任一项，发布器不能证明此报告消费了正确快照。
        if expected_rewrite_dense_input is None or rewrite_snapshot_id is None:
            # 不允许调用方跳过快照发布 gate。
            raise ValueError("rewrite-dense 发布缺少可信输入或快照身份")
        # 当前报告 input 必须与 CLI 从 bundle 和 confirmation 构造的身份逐字段一致。
        if details.get("input") != expected_rewrite_dense_input:
            # 拒绝篡改后的 hash、确认时间或冻结 chunk identity。
            raise ValueError("rewrite-dense details 冻结输入不一致")
        # 运行参数必须引用同一个已验证快照 ID，而不是任意文本。
        parameters = details.get("parameters")
        if not isinstance(parameters, dict) or parameters.get("rewrite_snapshot_id") != rewrite_snapshot_id:
            # 快照身份漂移会使质量比较失去可追溯性。
            raise ValueError("rewrite-dense details 快照身份不一致")
        # worker 必须提供每题快照记录，并与评测指标覆盖同一案例集合。
        records = details.get("rewrite_records_by_case_id")
        case_metrics = details.get("case_metrics_by_case_id")
        comparison = details.get("comparison")
        if (
            not isinstance(records, dict)
            or not isinstance(case_metrics, dict)
            or not isinstance(comparison, dict)
            or set(records) != set(case_metrics)
            or set(comparison.get("cases", {})) != set(case_metrics)
        ):
            # 缺题或没有逐题比较时都不能声称完成改写评测。
            raise ValueError("rewrite-dense details 缺少完整逐题证据")
    # 确保 reports 父目录存在；这是用户可提交的非敏感证据目录。
    reports_dir.mkdir(parents=True, exist_ok=True)
    # 计算不可变正式目标目录。
    final_directory = reports_dir / evaluation_run_id
    # 同名目录意味着该 run 已经发布，拒绝覆盖历史证据。
    if final_directory.exists():
        # 用户必须生成新的 run ID 才能再次发布。
        raise FileExistsError("evaluation_run_id 已存在，不能覆盖正式报告")
    # 在同一 reports 父目录创建临时目录，保证后续目录替换位于同一卷。
    staging_directory = Path(
        tempfile.mkdtemp(prefix=f".{evaluation_run_id}.staging-", dir=reports_dir)
    )
    # 记录是否已将 staging 发布为正式目录。
    published = False
    # 任何序列化或发布错误都必须删除自己创建的 staging。
    try:
        # 机器可读明细使用稳定排序和缩进，便于 Git diff 审阅。
        details_text = json.dumps(
            details,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        # 生成与 JSON 使用同一 details 的人读摘要。
        summary_text = _render_summary(details)
        # 写入 staging 中的 JSON 文件。
        (staging_directory / "details.json").write_text(
            details_text,
            encoding="utf-8",
            newline="\n",
        )
        # 写入 staging 中的 Markdown 文件。
        (staging_directory / "summary.md").write_text(
            summary_text,
            encoding="utf-8",
            newline="\n",
        )
        # 重新读取 JSON，确认序列化后仍保留同一个 run ID。
        written_details = json.loads(
            (staging_directory / "details.json").read_text(encoding="utf-8")
        )
        # 两份报告的来源 details 必须与目标目录身份一致。
        if written_details.get("evaluation_run_id") != evaluation_run_id:
            # 防止未来修改渲染代码时产生半可信证据。
            raise ValueError("staging details.json 的运行身份不一致")
        # Markdown 必须同样展示 run ID，避免人读报告失去关联。
        if f"`{evaluation_run_id}`" not in summary_text:
            # 该检查保证两个格式共同引用同一轮运行。
            raise ValueError("staging summary.md 的运行身份不一致")
        # 整体重命名 staging 目录，正式目录只会在两份文件都完成后出现。
        os.replace(staging_directory, final_directory)
        # 标记成功发布，finally 不再清理正式目录。
        published = True
    # 无论业务错误还是文件系统错误都需要保留给调用方处理。
    finally:
        # 只有还未发布时 staging 才是本函数可以安全删除的临时目录。
        if not published and staging_directory.exists():
            # 此目录由 mkdtemp 在本函数刚创建，清理不会碰用户已有报告。
            shutil.rmtree(staging_directory)
    # 返回不可变正式目录，调用方可记录相对路径。
    return final_directory
