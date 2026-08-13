"""重排固定 dense 候选，并保留可审计的前后排名快照。"""

# 导入 math，拒绝无法可靠排序的 NaN 与无穷分数。
import math
# 导入 dataclass，定义不可变的重排计算结果。
from dataclasses import dataclass
# 导入 Protocol，声明真实模型与测试 fake 共享的窄打分接口。
from typing import Protocol

# 导入跨策略结果对象与公共排名校验器。
from app.retrieval_strategies.types import RankedChunk, validate_ranked_chunks


# 固定 BGE Reranker 的发布模型身份，避免模型仓库默认分支发生漂移。
RERANKER_MODEL_ID = "BAAI/bge-reranker-v2-m3"
# 固定已在设计中确认的 Hugging Face revision。
RERANKER_MODEL_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
# 固定 CPU 环境的保守批量大小，真实报告必须原样记录。
RERANKER_BATCH_SIZE = 4
# 固定 tokenizer 最大长度，限制单条候选的计算量。
RERANKER_MAX_LENGTH = 1024
# v2 语料已有 975 个块，固定重排 dense Top-50 候选，再输出最终 Top-10。
RERANKER_CANDIDATE_K = 50


# 用真实 sentence-transformers CrossEncoder 实现窄 scorer 接口。
class BgeCrossEncoderScorer:
    """以固定模型 revision 计算 query-document 对的 raw logit。"""

    # 保存由调用方控制生命周期的已加载 CrossEncoder。
    def __init__(self, cross_encoder: object) -> None:
        # 不在此处联网或加载模型，使 smoke 和正式评测能分别计时。
        self._cross_encoder = cross_encoder

    # 对同一问题和候选顺序批量计算一一对应的 raw logit。
    def score(
        self,
        question: str,
        candidates: tuple[RankedChunk, ...],
    ) -> tuple[float, ...]:
        """显式使用 Identity activation，拒绝 CrossEncoder 默认 Sigmoid。"""

        # 延迟导入 torch，普通 fake 单测无需初始化模型运行时。
        import torch

        # CrossEncoder 需要 query-document 对列表，顺序必须与候选保持一致。
        pairs = [(question, candidate.text) for candidate in candidates]
        # 通过显式 Identity 获得真实 raw logit，而非单标签概率。
        raw_scores = self._cross_encoder.predict(
            pairs,
            batch_size=RERANKER_BATCH_SIZE,
            show_progress_bar=False,
            activation_fn=torch.nn.Identity(),
        )
        # numpy array 或 list 都可迭代；逐项转 float 交由公共契约检验。
        return tuple(float(score) for score in raw_scores)


# 声明重排器只需要的输入输出边界，隔离模型张量和 tokenizer 细节。
class RerankerScorer(Protocol):
    """为同一问题与固定候选返回一一对应的 raw logit。"""

    # 每个候选必须恰好对应一个有限、非 bool 的分数。
    def score(
        self,
        question: str,
        candidates: tuple[RankedChunk, ...],
    ) -> tuple[float, ...]:
        """计算问题和候选文本对的相关性 raw logit。"""


# 冻结完整 dense、完整 rerank 与最终 Top-K，防止报告阶段意外修改证据。
@dataclass(frozen=True)
class RerankOutcome:
    """保存从同一 dense 候选快照派生的所有重排结果。"""

    # 保存重排前完整且有序的 dense 候选。
    dense_candidates: tuple[RankedChunk, ...]
    # 保存已按 reranker logit 重排的完整候选。
    reranked_candidates: tuple[RankedChunk, ...]
    # 保存 reranked_candidates 的固定 Top-K 前缀。
    final_results: tuple[RankedChunk, ...]


# 验证本阶段只接受完全合法且唯一的 dense 候选快照。
def _validate_dense_candidates(candidates: tuple[RankedChunk, ...]) -> None:
    """拒绝空、重复或非 dense 的候选，避免重排污染因果比较。"""

    # 没有候选时无法定义候选集合不变的实验。
    if not candidates:
        # 调用方必须显式处理 dense 召回不足。
        raise ValueError("rerank candidates 不能为空")
    # 公共校验同时检查连续 rank、唯一 identity 与 dense 分数元数据。
    validate_ranked_chunks(
        list(candidates),
        method_name="dense",
        top_k=len(candidates),
    )


# 验证模型输出能和候选一一配对并形成确定性排序。
def _validate_scores(scores: tuple[float, ...], candidate_count: int) -> None:
    """拒绝错数量、bool 或非有限 logit，不进行隐式转换。"""

    # 每个候选都必须有一个分数，缺失或额外项都会破坏对应关系。
    if len(scores) != candidate_count:
        # 不截断也不填充，避免悄悄改变候选集合。
        raise ValueError("reranker scores 数量必须与 candidates 一致")
    # 逐项拒绝 bool、字符串、NaN 和无穷等不可发布值。
    for score in scores:
        # bool 是 int 的子类，必须先单独拒绝。
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
        ):
            # 只有有限 raw logit 才能用于可复核的确定性排序。
            raise ValueError("reranker score 必须是有限非 bool 数值")


# 对一次 dense 候选快照打分并输出完整重排与最终前缀。
def rerank_dense_candidates(
    question: str,
    candidates: tuple[RankedChunk, ...],
    scorer: RerankerScorer,
    *,
    top_k: int,
) -> RerankOutcome:
    """只重排已有 dense 候选，绝不引入候选集外的文本块。"""

    # 问题必须有实际内容，避免把空输入交给真实模型。
    if not isinstance(question, str) or not question.strip():
        # 该边界与检索问题的语义一致。
        raise ValueError("question 必须是非空字符串")
    # 最终 K 必须是候选范围内的普通正整数。
    if type(top_k) is not int or top_k <= 0 or top_k > len(candidates):
        # 不允许 bool、零、负数或超过候选数的截断请求。
        raise ValueError("top_k 必须是 candidates 范围内的正整数")
    # 在调用模型前验证 dense 快照，确保错误不会被模型分数掩盖。
    _validate_dense_candidates(candidates)
    # 让 scorer 对同一批、同一顺序的候选进行一次性打分。
    scores = scorer.score(question, candidates)
    # 模型输出先完成严格校验，之后才允许排序。
    _validate_scores(scores, len(candidates))
    # 配对原 dense 结果、raw logit 和原名次，作为确定性 tie-break 的依据。
    scored_candidates = list(zip(candidates, scores, strict=True))
    # 高 logit 优先；同分保持原 dense rank，identity 仅作为额外稳定保护。
    scored_candidates.sort(
        key=lambda item: (
            -float(item[1]),
            item[0].rank,
            item[0].source_name,
            item[0].chunk_index,
        )
    )
    # 用新的连续 rank 与 reranker 分数构造完整后排名。
    reranked_candidates = tuple(
        RankedChunk(
            text=candidate.text,
            source_name=candidate.source_name,
            chunk_index=candidate.chunk_index,
            rank=rank,
            method="dense-rerank",
            score=float(score),
            score_kind="bge_reranker_logit",
            higher_is_better=True,
        )
        for rank, (candidate, score) in enumerate(scored_candidates, start=1)
    )
    # 公共校验再次证明 post 列表具有连续排名、唯一身份和正确方法名。
    validate_ranked_chunks(
        list(reranked_candidates),
        method_name="dense-rerank",
        top_k=len(reranked_candidates),
    )
    # 截取只在完整前后快照都已合法后进行，保留所有可审计候选。
    final_results = reranked_candidates[:top_k]
    # 返回不可变证据对象，供 runner 和报告共同复用。
    return RerankOutcome(
        dense_candidates=candidates,
        reranked_candidates=reranked_candidates,
        final_results=final_results,
    )
