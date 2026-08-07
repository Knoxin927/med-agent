"""用固定 RRF 规则融合 dense 与 BM25 的有序候选。"""

# 导入统一策略协议与结果契约。
from app.retrieval_strategies.types import (
    RankedChunk,
    RetrievalStrategy,
    validate_ranked_chunks,
)


# 固定两路候选数量，给 Top-10 融合留出补位空间。
CANDIDATE_K = 20
# 固定 RRF 常数，避免不同实验悄悄改变融合曲线。
RRF_K = 60


# 将 dense 与 BM25 候选按排名而不是 raw score 融合。
class HybridRrfRetrievalStrategy:
    """分别请求两路 Top-20，再输出经 RRF 排序的唯一 Top-K。"""

    # 明确区分 dense、bm25 与已经融合的最终结果。
    method_name = "hybrid_rrf"

    # 保存两条策略通道；调用方不能传递任意候选大小或 RRF 常数。
    def __init__(
        self,
        dense_strategy: RetrievalStrategy,
        bm25_strategy: RetrievalStrategy,
    ) -> None:
        # 保存 dense 候选来源，不读取或比较其 cosine distance。
        self._dense_strategy = dense_strategy
        # 保存 BM25 候选来源，不读取或比较其 BM25 分数。
        self._bm25_strategy = bm25_strategy

    # 对问题执行固定候选获取、RRF 累加和稳定截断。
    def retrieve(self, question: str, *, top_k: int) -> list[RankedChunk]:
        """只消费两路 rank，绝不将异量纲 raw score 相加。"""

        # 公共调用方只能请求正整数数量。
        if type(top_k) is not int or top_k <= 0:
            # bool 不能作为 K 使用。
            raise ValueError("top_k 必须是正整数")
        # 两路都固定请求 20 条，而非最终 Top-10。
        dense_candidates = self._dense_strategy.retrieve(question, top_k=CANDIDATE_K)
        # BM25 也独立生成同样大小的候选集合。
        bm25_candidates = self._bm25_strategy.retrieve(question, top_k=CANDIDATE_K)
        # 防止底层策略在融合前就违反公共结果契约。
        validate_ranked_chunks(
            dense_candidates,
            method_name=self._dense_strategy.method_name,
            top_k=CANDIDATE_K,
        )
        # 同样验证 BM25 候选的 rank 与 identity。
        validate_ranked_chunks(
            bm25_candidates,
            method_name=self._bm25_strategy.method_name,
            top_k=CANDIDATE_K,
        )
        # 按稳定 identity 收集 RRF 总分与首次出现的完整文本块。
        fused: dict[tuple[str, int], tuple[float, RankedChunk]] = {}
        # 依次处理两路列表；每条贡献只取其排名。
        for candidates in (dense_candidates, bm25_candidates):
            # 当前结果的 rank 已由公共契约验证为连续整数。
            for candidate in candidates:
                # 使用来源名与块序号作为跨策略去重键。
                identity = (candidate.source_name, candidate.chunk_index)
                # 计算当前通道的 RRF 排名贡献。
                contribution = 1.0 / (RRF_K + candidate.rank)
                # 读取已有总分；首次出现时从零开始。
                previous = fused.get(identity)
                # 首次出现时保留完整文本，后续只累加分数。
                if previous is None:
                    # 保存当前贡献和用于最终输出的块内容。
                    fused[identity] = (contribution, candidate)
                    # 当前 identity 已处理完毕。
                    continue
                # 叠加另一通道的排名贡献，保持首次文本块不变。
                fused[identity] = (previous[0] + contribution, previous[1])
        # 分数降序，完全并列时按稳定 identity 字典序排序。
        ordered = sorted(
            fused.items(),
            key=lambda item: (-item[1][0], item[0][0], item[0][1]),
        )
        # 只在 RRF 融合后截取调用方请求的最终数量。
        ranked_results = [
            RankedChunk(
                text=stored.text,
                source_name=stored.source_name,
                chunk_index=stored.chunk_index,
                rank=rank,
                method=self.method_name,
                score=score,
                score_kind="rrf_score",
                higher_is_better=True,
            )
            for rank, (_, (score, stored)) in enumerate(ordered[:top_k], start=1)
        ]
        # 融合后再次确认没有重复 identity、rank 连续且 metadata 正确。
        validate_ranked_chunks(
            ranked_results,
            method_name=self.method_name,
            top_k=top_k,
        )
        # 返回可直接交给统一评测 runner 的最终排名。
        return ranked_results
