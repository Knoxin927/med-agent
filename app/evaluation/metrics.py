"""用纯函数计算 Recall、MRR、多线索全召回和延迟百分位。"""

# 导入 math，用于验证百分位输入是有限数值。
import math

# 导入当前步骤需要的评测身份、案例和指标类型。
from app.evaluation.types import (
    CaseMetrics,
    ChunkIdentity,
    EvaluationCase,
    MetricsSummary,
)


# 对一条已冻结案例和有序检索身份计算固定 M2.1 指标。
def compute_case_metrics(
    case: EvaluationCase,
    ranked_identities: list[ChunkIdentity],
) -> CaseMetrics:
    """从同一份 Top-10 排名同时计算 @5 和 @10 指标。"""

    # 同一块重复占据多个排名属于策略错误，不能静默集合去重。
    if len(ranked_identities) != len(set(ranked_identities)):
        # 在公式计算前失败，避免重复结果改变首个命中排名。
        raise ValueError("检索排名不能包含重复 chunk identity")
    # M2.1 只以最多十条结果计算固定指标。
    top_10 = ranked_identities[:10]
    # 库外题的 relevant 按契约为空，不进入 Recall/MRR 分母。
    if case.primary_stratum == "out-of-domain":
        # 返回可保存错误 Top-K、但不含质量分数的单题结果。
        return CaseMetrics(
            case_id=case.case_id,
            primary_stratum=case.primary_stratum,
            recall_at_5=None,
            recall_at_10=None,
            mrr_at_10=None,
            all_relevant_hit_at_5=None,
            all_relevant_hit_at_10=None,
            hit_identities=(),
        )
    # 库内题必须至少有一个人工相关块，否则分母没有意义。
    if not case.relevant:
        # defense-in-depth 防止调用方绕过 dataset 加载器。
        raise ValueError("库内案例必须至少包含一个 relevant identity")
    # 使用集合完成身份命中判断，分母仍来自人工相关块总数。
    relevant_set = set(case.relevant)
    # Top-5 是同一 Top-10 排名的前五项。
    top_5_set = set(top_10[:5])
    # Top-10 身份集合用于 Recall@10 和多线索全召回。
    top_10_set = set(top_10)
    # 计算 Top-5 命中相关块数量占全部相关块的比例。
    recall_at_5 = len(top_5_set & relevant_set) / len(relevant_set)
    # 计算 Top-10 命中相关块数量占全部相关块的比例。
    recall_at_10 = len(top_10_set & relevant_set) / len(relevant_set)
    # 默认 Top-10 没有命中时 MRR 为零。
    mrr_at_10 = 0.0
    # 按一开始的排名顺序寻找第一个相关块。
    for rank, identity in enumerate(top_10, start=1):
        # 第一次命中就能确定倒数排名，后续命中不影响 MRR。
        if identity in relevant_set:
            # 例如第 5 名命中得到 1/5。
            mrr_at_10 = 1.0 / rank
            # 停止循环，保持“第一个相关结果”的定义。
            break
    # 按排名顺序保存 Top-10 中实际命中的身份，供报告解释。
    hit_identities = tuple(
        identity for identity in top_10 if identity in relevant_set
    )
    # 只有 multi-clue 主分层需要额外全召回布尔指标。
    is_multi_clue = case.primary_stratum == "multi-clue"
    # 返回公式结果，不依赖模型、数据库或真实计时。
    return CaseMetrics(
        case_id=case.case_id,
        primary_stratum=case.primary_stratum,
        recall_at_5=recall_at_5,
        recall_at_10=recall_at_10,
        mrr_at_10=mrr_at_10,
        all_relevant_hit_at_5=(relevant_set <= top_5_set) if is_multi_clue else None,
        all_relevant_hit_at_10=(
            relevant_set <= top_10_set
        ) if is_multi_clue else None,
        hit_identities=hit_identities,
    )


# 对一组单题指标进行库内宏平均，并保存固定失败案例定义。
def aggregate_case_metrics(metrics: list[CaseMetrics]) -> MetricsSummary:
    """汇总库内 Recall/MRR；库外题只计数而不进入质量分母。"""

    # 空列表没有可解释的宏平均，调用方必须先运行至少一题。
    if not metrics:
        # 不返回假装为零的汇总，避免掩盖评测循环没有执行。
        raise ValueError("metrics 至少需要一条单题结果")
    # 通过 None 区分按契约排除的库外题。
    in_domain_metrics = [
        item for item in metrics if item.recall_at_10 is not None
    ]
    # 其余项目都是库外观察案例。
    out_of_domain_count = len(metrics) - len(in_domain_metrics)
    # 没有库内题时 Recall/MRR 没有分母。
    if not in_domain_metrics:
        # 数据加载器不会产生这种发布数据，但纯函数仍做防御检查。
        raise ValueError("metrics 至少需要一条库内结果")
    # 类型收窄后 Recall@5 不再为 None。
    recall_at_5 = sum(
        item.recall_at_5 for item in in_domain_metrics if item.recall_at_5 is not None
    ) / len(in_domain_metrics)
    # 类型收窄后 Recall@10 不再为 None。
    recall_at_10 = sum(
        item.recall_at_10
        for item in in_domain_metrics
        if item.recall_at_10 is not None
    ) / len(in_domain_metrics)
    # 类型收窄后 MRR@10 不再为 None。
    mrr_at_10 = sum(
        item.mrr_at_10 for item in in_domain_metrics if item.mrr_at_10 is not None
    ) / len(in_domain_metrics)
    # 多线索指标只在 multi-clue 案例中定义。
    multi_clue_metrics = [
        item
        for item in in_domain_metrics
        if item.primary_stratum == "multi-clue"
    ]
    # 没有多线索题时明确返回 None，而不是伪造零分。
    if not multi_clue_metrics:
        # 把两个额外指标同时标为空。
        all_relevant_hit_at_5 = None
        # 把两个额外指标同时标为空。
        all_relevant_hit_at_10 = None
    else:
        # bool 在 Python 中可按 1/0 求和，前提是过滤掉 None。
        all_relevant_hit_at_5 = sum(
            item.all_relevant_hit_at_5 is True for item in multi_clue_metrics
        ) / len(multi_clue_metrics)
        # Top-10 使用同样的多线索分母。
        all_relevant_hit_at_10 = sum(
            item.all_relevant_hit_at_10 is True for item in multi_clue_metrics
        ) / len(multi_clue_metrics)
    # Recall@10 不满分代表至少一个人工相关块没有被完整找回。
    failed_case_ids = tuple(
        item.case_id
        for item in in_domain_metrics
        if item.recall_at_10 is not None and item.recall_at_10 < 1.0
    )
    # 没有任何命中是失败案例中的更严格子集。
    no_hit_case_ids = tuple(
        item.case_id
        for item in in_domain_metrics
        if not item.hit_identities
    )
    # 返回所有聚合结果，排序沿用输入案例顺序。
    return MetricsSummary(
        in_domain_case_count=len(in_domain_metrics),
        out_of_domain_case_count=out_of_domain_count,
        recall_at_5=recall_at_5,
        recall_at_10=recall_at_10,
        mrr_at_10=mrr_at_10,
        all_relevant_hit_at_5=all_relevant_hit_at_5,
        all_relevant_hit_at_10=all_relevant_hit_at_10,
        failed_case_ids=failed_case_ids,
        no_hit_case_ids=no_hit_case_ids,
    )


# 使用设计固定的线性插值规则计算百分位。
def linear_percentile(samples: list[float], quantile: float) -> float:
    """按排序位置 (n - 1) * quantile 进行线性插值。"""

    # 百分位没有样本时没有意义。
    if not samples:
        # 不能把未计时误报为零毫秒。
        raise ValueError("samples 不能为空")
    # q 必须位于闭区间 [0, 1] 且不能是 bool、NaN 或无穷。
    if (
        isinstance(quantile, bool)
        or not isinstance(quantile, (int, float))
        or not math.isfinite(float(quantile))
        or not 0.0 <= float(quantile) <= 1.0
    ):
        # 非法 q 无法表达 P50/P95。
        raise ValueError("quantile 必须是 0 到 1 之间的有限数值")
    # 样本必须全部是普通有限数值。
    if any(
        isinstance(sample, bool)
        or not isinstance(sample, (int, float))
        or not math.isfinite(float(sample))
        for sample in samples
    ):
        # 拒绝 NaN、无穷和字符串，防止排序产生假结果。
        raise ValueError("samples 必须全部是有限数值")
    # 把整数统一转换为 float 后排序，不修改调用方原列表。
    ordered_samples = sorted(float(sample) for sample in samples)
    # 计算设计固定的零基插值位置。
    position = (len(ordered_samples) - 1) * float(quantile)
    # 左侧整数下标总是小于等于当前位置。
    lower_index = int(math.floor(position))
    # 右侧下标不超过最后一个样本。
    upper_index = int(math.ceil(position))
    # 恰好落在一个样本上时无需插值。
    if lower_index == upper_index:
        # 返回该固定样本。
        return ordered_samples[lower_index]
    # 位置的小数部分是从左值走向右值的比例。
    fraction = position - lower_index
    # 按线性插值返回确定性百分位。
    return (
        ordered_samples[lower_index]
        + (ordered_samples[upper_index] - ordered_samples[lower_index]) * fraction
    )
