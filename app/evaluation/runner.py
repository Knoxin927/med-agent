"""执行固定预热与热路径轮次，并把排名交给统一指标函数。"""

# 导入 dataclass，定义运行完成后可供报告消费的只读结果容器。
from dataclasses import dataclass
# 导入 Random，使用固定 seed 生成可复现但不固定原顺序的轮次。
from random import Random
# 导入 Callable，为真实 perf_counter 和测试假时钟使用同一签名。
from typing import Callable

# 导入单题、汇总与百分位所需的纯函数。
from app.evaluation.metrics import aggregate_case_metrics, compute_case_metrics
# 导入评测案例、单题指标和汇总类型。
from app.evaluation.types import (
    CaseMetrics,
    ChunkIdentity,
    EvaluationCase,
    MetricsSummary,
)
# 导入统一策略协议、结果对象与边界校验。
from app.retrieval_strategies.types import (
    RankedChunk,
    RetrievalStrategy,
    validate_ranked_chunks,
)
# 导入重排纯节点，确保 runner 只负责编排和计时。
from app.retrieval_strategies.rerank import (
    RerankOutcome,
    RerankerScorer,
    rerank_dense_candidates,
)


# frozen=True 防止报告生成阶段意外修改本轮已冻结的排名和计时样本。
@dataclass(frozen=True)
class HotEvaluationResult:
    """保存预热后的规范排名、逐题指标、汇总和全部热路径样本。"""

    # 保存策略方法名，例如 dense。
    method_name: str
    # 保存每题第一轮正式结果，后续轮次必须与它保持同一 identity 顺序。
    ranked_results_by_case_id: dict[str, tuple[RankedChunk, ...]]
    # 保存每题由规范排名计算出的指标。
    case_metrics_by_case_id: dict[str, CaseMetrics]
    # 保存所有正式检索调用的毫秒样本。
    latency_samples_ms: tuple[float, ...]
    # 保存只含库内题的质量宏平均和固定失败案例。
    metrics_summary: MetricsSummary


# 保存 dense-rerank 实验的完整前后候选、指标和分阶段热路径证据。
@dataclass(frozen=True)
class RerankHotEvaluationResult:
    """保存同一 dense 候选快照派生的重排评测结果。"""

    # 保存每题第一轮正式的完整重排结果。
    outcomes_by_case_id: dict[str, RerankOutcome]
    # 保存每题从 dense Top-10 前缀计算的重排前指标。
    pre_case_metrics_by_case_id: dict[str, CaseMetrics]
    # 保存每题从 rerank Top-10 前缀计算的重排后指标。
    post_case_metrics_by_case_id: dict[str, CaseMetrics]
    # 保存重排后库内题宏平均指标。
    post_metrics_summary: MetricsSummary
    # 保存完整编排与校验的热路径毫秒样本。
    total_latency_samples_ms: tuple[float, ...]
    # 保存单次 dense 完整候选调用的热路径毫秒样本。
    dense_latency_samples_ms: tuple[float, ...]
    # 保存打分、排序和候选集合校验的热路径毫秒样本。
    rerank_latency_samples_ms: tuple[float, ...]


# 检查整轮输入是否具备固定评测所需的基本契约。
def _validate_runner_inputs(
    cases: list[EvaluationCase],
    warmup_rounds: int,
    measured_rounds: int,
) -> None:
    # 空案例列表无法形成质量分母或延迟分布。
    if not cases:
        # 调用方应先通过数据加载器获得发布级输入。
        raise ValueError("cases 至少需要一条案例")
    # case_id 必须唯一，否则结果字典会覆盖证据。
    case_ids = [case.case_id for case in cases]
    # 重复身份会使逐题报告无法追溯。
    if len(case_ids) != len(set(case_ids)):
        # 不依赖 loader，runner 也拒绝重复。
        raise ValueError("cases 不能包含重复 case_id")
    # 预热轮次是普通非负整数。
    if type(warmup_rounds) is not int or warmup_rounds < 0:
        # bool 同样会被 type 检查拒绝。
        raise ValueError("warmup_rounds 必须是非负整数")
    # 正式轮次必须至少为一，发布命令会固定为五。
    if type(measured_rounds) is not int or measured_rounds <= 0:
        # 零轮不能生成热路径样本。
        raise ValueError("measured_rounds 必须是正整数")


# 运行热路径评测；调用方负责在此之前完成模型/索引准备与冷启动测量。
def run_hot_evaluation(
    cases: list[EvaluationCase],
    strategy: RetrievalStrategy,
    *,
    warmup_rounds: int,
    measured_rounds: int,
    seed: int,
    clock: Callable[[], float],
) -> HotEvaluationResult:
    """预热后交错执行固定轮次，并记录每次 strategy.retrieve 的耗时。"""

    # 在任何策略调用前检查输入结构。
    _validate_runner_inputs(cases, warmup_rounds, measured_rounds)
    # 读取策略主动声明的方法名。
    method_name = strategy.method_name
    # 方法名为空会让报告无法区分检索策略。
    if not isinstance(method_name, str) or not method_name.strip():
        # 不猜测默认方法名。
        raise ValueError("strategy 必须声明非空 method_name")
    # 预热始终沿案例原顺序执行，便于日志和调试定位。
    for _ in range(warmup_rounds):
        # 预热每一个问题，但不调用 clock、不写入报告排名。
        for case in cases:
            # 统一使用 Top-10，供同一份排名计算 @5 和 @10。
            warmup_results = strategy.retrieve(case.question, top_k=10)
            # 预热也要检查策略没有违反公共结果契约。
            validate_ranked_chunks(
                warmup_results,
                method_name=method_name,
                top_k=10,
            )
    # 固定 seed 的随机对象只在当前运行内部使用。
    random = Random(seed)
    # 保存第一轮正式结果作为所有后续轮次的稳定排名基准。
    ranked_results_by_case_id: dict[str, tuple[RankedChunk, ...]] = {}
    # 保存从稳定排名计算出的逐题指标。
    case_metrics_by_case_id: dict[str, CaseMetrics] = {}
    # 保存每一个正式调用的耗时毫秒。
    latency_samples_ms: list[float] = []
    # measured_rounds 由调用方固定为至少五，函数本身保持通用。
    for _ in range(measured_rounds):
        # 复制案例列表，避免原地打乱调用方输入。
        round_cases = list(cases)
        # 按固定 seed 推进随机状态，得到可复现的交错顺序。
        random.shuffle(round_cases)
        # 逐题执行正式检索。
        for case in round_cases:
            # 计时从调用策略前开始，不包含报告写入。
            started_at = clock()
            # 同一题只请求一次 Top-10。
            results = strategy.retrieve(case.question, top_k=10)
            # 计时在策略返回后立即停止。
            finished_at = clock()
            # 公共结果契约必须在指标计算前成立。
            validate_ranked_chunks(results, method_name=method_name, top_k=10)
            # perf_counter 差值以秒为单位，报告统一换算为毫秒。
            elapsed_ms = (finished_at - started_at) * 1000.0
            # 时钟倒退会得到负延迟，不能进入百分位。
            if elapsed_ms < 0.0:
                # 显式拒绝错误时钟或外部注入值。
                raise ValueError("clock 不能返回倒退的时间")
            # 保存这一题这一轮的热路径样本。
            latency_samples_ms.append(elapsed_ms)
            # 只保留稳定 identity 顺序，分数轻微差异不影响相关性比较。
            current_identities = tuple(
                (result.source_name, result.chunk_index) for result in results
            )
            # 第一次正式遇到该题时建立规范排名和指标。
            if case.case_id not in ranked_results_by_case_id:
                # 保存不可变 tuple，避免后续列表被修改。
                ranked_results_by_case_id[case.case_id] = tuple(results)
                # 指标只从这同一份规范 Top-10 排名计算。
                case_metrics_by_case_id[case.case_id] = compute_case_metrics(
                    case,
                    [
                        ChunkIdentity(source_name, chunk_index)
                        for source_name, chunk_index in current_identities
                    ],
                )
                # 当前题已经完成首轮记录，继续下一题。
                continue
            # 后续轮次必须返回同一 identity 与相同顺序。
            expected_identities = tuple(
                (result.source_name, result.chunk_index)
                for result in ranked_results_by_case_id[case.case_id]
            )
            # 漂移说明索引或策略不是当前实验口径下的确定性结果。
            if current_identities != expected_identities:
                # 不能静默保留更好或更差的一轮。
                raise ValueError("同一 case 跨轮检索 identity 排名发生漂移")
    # 使用原案例顺序收集指标，保证 failed_case_ids 稳定。
    ordered_metrics = [case_metrics_by_case_id[case.case_id] for case in cases]
    # 宏平均只由纯函数完成，runner 不复制公式。
    metrics_summary = aggregate_case_metrics(ordered_metrics)
    # 返回本轮生成报告所需的全部非模型证据。
    return HotEvaluationResult(
        method_name=method_name,
        ranked_results_by_case_id=ranked_results_by_case_id,
        case_metrics_by_case_id=case_metrics_by_case_id,
        latency_samples_ms=tuple(latency_samples_ms),
        metrics_summary=metrics_summary,
    )


# 运行一次 dense 完整候选与同一快照的重排，返回完整候选和三个阶段耗时。
def _run_rerank_case(
    question: str,
    dense_strategy: RetrievalStrategy,
    scorer: RerankerScorer,
    *,
    candidate_k: int,
    top_k: int,
    clock: Callable[[], float],
) -> tuple[RerankOutcome, float, float, float]:
    """对单题生成可审计 pre/post 候选，并分别测量 dense 与 rerank 阶段。"""

    # total 从 dense 调用前开始，包含后续必要的输出校验。
    total_started_at = clock()
    # dense 阶段只围绕一次固定宽度的候选检索。
    dense_started_at = clock()
    # 只调用一次 dense，避免 pre/post 来自不同候选快照。
    dense_results = dense_strategy.retrieve(question, top_k=candidate_k)
    # dense 返回后立即停止本阶段计时。
    dense_finished_at = clock()
    # 候选数必须精确等于设计值，短集不能伪装成正式证据。
    if len(dense_results) != candidate_k:
        # 调用方必须先解决语料或候选宽度不足。
        raise ValueError("dense candidates 数量必须等于 candidate_k")
    # 公共契约验证前置 dense 输出的连续排名与唯一身份。
    validate_ranked_chunks(
        dense_results,
        method_name="dense",
        top_k=candidate_k,
    )
    # rerank 阶段包含模型打分、排序以及完整 post 输出校验。
    rerank_started_at = clock()
    # 纯节点只接收本次 dense 快照，禁止再次检索。
    outcome = rerank_dense_candidates(
        question,
        tuple(dense_results),
        scorer,
        top_k=top_k,
    )
    # rerank 节点返回后停止阶段计时。
    rerank_finished_at = clock()
    # total 直到所有强制校验完成后才停止。
    total_finished_at = clock()
    # 统一把秒换算为毫秒，便于报告使用同一百分位函数。
    total_ms = (total_finished_at - total_started_at) * 1000.0
    # dense 阶段计时不含 reranker 工作。
    dense_ms = (dense_finished_at - dense_started_at) * 1000.0
    # rerank 阶段计时包含模型打分与排序校验。
    rerank_ms = (rerank_finished_at - rerank_started_at) * 1000.0
    # 任一阶段出现时钟倒退都不能进入百分位或正式报告。
    if total_ms < 0.0 or dense_ms < 0.0 or rerank_ms < 0.0:
        # 测试 fake 或系统时钟异常都应显式暴露。
        raise ValueError("clock 不能返回倒退的时间")
    # total 必须至少覆盖两个显式阶段，防止不完整计时。
    if total_ms < dense_ms + rerank_ms:
        # 时钟精度或调用位置错误不能被悄悄忽略。
        raise ValueError("total 延迟不能小于 dense 与 rerank 阶段之和")
    # 返回完整证据和三组可聚合样本。
    return outcome, total_ms, dense_ms, rerank_ms


# 运行固定预热和交错测量，比较同一次 dense 完整候选的重排前后质量。
def run_hot_rerank_evaluation(
    cases: list[EvaluationCase],
    dense_strategy: RetrievalStrategy,
    scorer: RerankerScorer,
    *,
    candidate_k: int,
    top_k: int,
    warmup_rounds: int,
    measured_rounds: int,
    seed: int,
    clock: Callable[[], float],
) -> RerankHotEvaluationResult:
    """保存同候选 pre/post 排名、指标及 total/dense/rerank 延迟。"""

    # 复用通用 runner 的案例与轮次输入边界。
    _validate_runner_inputs(cases, warmup_rounds, measured_rounds)
    # 本实验的 dense 候选宽度必须大于最终 Top-K。
    if type(candidate_k) is not int or type(top_k) is not int or candidate_k < top_k:
        # 不接受 bool、负数或没有重排空间的参数。
        raise ValueError("candidate_k 必须是不小于 top_k 的正整数")
    # 预热也完整执行候选与模型输出契约，但不记录规范排名或延迟。
    for _ in range(warmup_rounds):
        # 保持案例原顺序，便于真实模型异常时定位具体问题。
        for case in cases:
            # 预热使用真实时钟但丢弃样本，正式样本只来自 measured rounds。
            _run_rerank_case(
                case.question,
                dense_strategy,
                scorer,
                candidate_k=candidate_k,
                top_k=top_k,
                clock=clock,
            )
    # 固定 seed 保证每轮案例交错顺序可复现。
    random = Random(seed)
    # 保存每题首轮规范结果与逐题 pre/post 指标。
    outcomes_by_case_id: dict[str, RerankOutcome] = {}
    pre_case_metrics_by_case_id: dict[str, CaseMetrics] = {}
    post_case_metrics_by_case_id: dict[str, CaseMetrics] = {}
    # 三组计时样本必须一一对应每个正式 case 调用。
    total_latency_samples_ms: list[float] = []
    dense_latency_samples_ms: list[float] = []
    rerank_latency_samples_ms: list[float] = []
    # 按固定轮次执行实际评测。
    for _ in range(measured_rounds):
        # 复制后打乱，不修改调用方的冻结案例顺序。
        round_cases = list(cases)
        # 推进同一个确定性随机对象。
        random.shuffle(round_cases)
        # 逐题生成同候选的 pre/post 证据。
        for case in round_cases:
            # 一次调用完成 dense 与 rerank，避免任何 side-channel。
            outcome, total_ms, dense_ms, rerank_ms = _run_rerank_case(
                case.question,
                dense_strategy,
                scorer,
                candidate_k=candidate_k,
                top_k=top_k,
                clock=clock,
            )
            # 记录所有热路径样本。
            total_latency_samples_ms.append(total_ms)
            dense_latency_samples_ms.append(dense_ms)
            rerank_latency_samples_ms.append(rerank_ms)
            # 将完整 pre/post identity 分别转换为最终 Top-10 指标输入。
            pre_identities = [
                ChunkIdentity(item.source_name, item.chunk_index)
                for item in outcome.dense_candidates[:top_k]
            ]
            post_identities = [
                ChunkIdentity(item.source_name, item.chunk_index)
                for item in outcome.final_results
            ]
            # 首轮结果成为报告的不可变规范证据。
            if case.case_id not in outcomes_by_case_id:
                # 保存纯节点返回的 frozen outcome。
                outcomes_by_case_id[case.case_id] = outcome
                # 指标只从本次 outcome 的各自 Top-10 前缀计算。
                pre_case_metrics_by_case_id[case.case_id] = compute_case_metrics(
                    case,
                    pre_identities,
                )
                post_case_metrics_by_case_id[case.case_id] = compute_case_metrics(
                    case,
                    post_identities,
                )
                # 当前题已建立稳定基准，继续下一题。
                continue
            # 后续轮次的完整 pre/post 排名均必须与首轮一致。
            expected = outcomes_by_case_id[case.case_id]
            # 比较稳定 identity 顺序，而不比较可能有微小差异的浮点分数。
            if (
                tuple((item.source_name, item.chunk_index) for item in outcome.dense_candidates)
                != tuple((item.source_name, item.chunk_index) for item in expected.dense_candidates)
                or tuple((item.source_name, item.chunk_index) for item in outcome.reranked_candidates)
                != tuple((item.source_name, item.chunk_index) for item in expected.reranked_candidates)
            ):
                # 候选或模型排序漂移时不能发布选择性结果。
                raise ValueError("同一 case 跨轮 rerank identity 排名发生漂移")
    # 按冻结案例原顺序聚合 post 指标，保证失败 case 排序稳定。
    ordered_post_metrics = [post_case_metrics_by_case_id[case.case_id] for case in cases]
    # 汇总只复用既有指标纯函数，避免复制 Recall/MRR 公式。
    post_metrics_summary = aggregate_case_metrics(ordered_post_metrics)
    # 返回完整可发布前验证的 rerank 评测结果。
    return RerankHotEvaluationResult(
        outcomes_by_case_id=outcomes_by_case_id,
        pre_case_metrics_by_case_id=pre_case_metrics_by_case_id,
        post_case_metrics_by_case_id=post_case_metrics_by_case_id,
        post_metrics_summary=post_metrics_summary,
        total_latency_samples_ms=tuple(total_latency_samples_ms),
        dense_latency_samples_ms=tuple(dense_latency_samples_ms),
        rerank_latency_samples_ms=tuple(rerank_latency_samples_ms),
    )
