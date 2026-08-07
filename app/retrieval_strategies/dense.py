"""把 M1.3 dense 检索结果适配为 M2.1 统一策略结果。"""

# 导入 Path，保存 M1.3 Chroma 持久化目录。
from pathlib import Path
# 导入 Any，允许真实模型和测试确定性编码器使用相同接口。
from typing import Any

# 导入现有问题向量化与 Chroma 检索编排，不改变其语义。
from app.rag.retrieval import retrieve_chunks
# 导入统一策略契约与结果校验器。
from app.retrieval_strategies.types import RankedChunk, validate_ranked_chunks


# 用一个窄对象把现有 dense 函数装配为可替换策略。
class DenseRetrievalStrategy:
    """复用 M1.3 的 BGE-M3 与 Chroma dense 检索。"""

    # 固定方法名，供报告和统一校验追溯。
    method_name = "dense"

    # 保存查询所需的编码器与本地 Chroma 路径。
    def __init__(self, encoder: Any, chroma_path: Path) -> None:
        # 不在构造器加载模型，真实资源生命周期由调用方控制。
        self._encoder = encoder
        # 保留 Chroma 路径，不转换成字符串以匹配 M1.3 接口。
        self._chroma_path = chroma_path

    # 将 M1.3 RetrievalResult 转换为 M2 的 RankedChunk 列表。
    def retrieve(self, question: str, *, top_k: int) -> list[RankedChunk]:
        """返回连续 rank、唯一 identity 和方向明确的 dense 结果。"""

        # 直接委托已经验证问题、K、模型身份与向量契约的 M1.3 入口。
        retrieval_results = retrieve_chunks(
            question,
            self._encoder,
            self._chroma_path,
            top_k=top_k,
        )
        # 按 M1.3 返回的由近到远顺序生成从一开始的最终排名。
        ranked_results = [
            RankedChunk(
                text=result.text,
                source_name=result.source_name,
                chunk_index=result.chunk_index,
                rank=rank,
                method=self.method_name,
                score=result.distance,
                score_kind="cosine_distance",
                higher_is_better=False,
            )
            for rank, result in enumerate(retrieval_results, start=1)
        ]
        # 在返回前执行统一契约，后续评测器无需了解 dense 特例。
        validate_ranked_chunks(
            ranked_results,
            method_name=self.method_name,
            top_k=top_k,
        )
        # 返回已经完成公共边界校验的结果。
        return ranked_results
