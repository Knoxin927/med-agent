"""把用户问题向量化，并从 Chroma 返回可追溯的 Top-K 文本块。"""

# 导入 Path，统一表示本地 Chroma 持久化目录。
from pathlib import Path
# 导入 Any，允许函数接收真实模型或实现相同接口的测试替身。
from typing import Any

# 导入固定模型名称和现有向量生成函数，复用 M1.2 的完整校验。
from app.rag.embedding import MODEL_NAME, generate_embeddings
# 导入稳定检索结果类型，明确编排函数的返回契约。
from app.rag.retrieval_types import RetrievalResult
# 导入 Chroma 存储适配器，执行 collection 契约检查和最近邻查询。
from app.rag.vector_store import ChromaChunkStore


# 将一个非空问题转换为查询向量，并返回最多 top_k 条最近文本块。
def retrieve_chunks(
    question: str,
    encoder: Any,
    chroma_path: Path,
    *,
    top_k: int = 3,
) -> list[RetrievalResult]:
    # 问题必须是包含实际内容的字符串。
    if not isinstance(question, str) or not question.strip():
        # 无意义问题不能消耗模型计算，也没有可检索的语义。
        raise ValueError("question 必须是非空字符串")
    # top_k 必须是普通正整数，布尔值不能冒充 0 或 1。
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        # 非法 K 无法表达最多返回多少条结果。
        raise ValueError("top_k 必须是正整数")
    # 读取编码器主动声明的模型身份；缺失时不使用默认值猜测。
    encoder_model_name = getattr(encoder, "model_name", None)
    # 未声明模型身份时无法证明查询和入库处于同一向量空间。
    if encoder_model_name is None:
        # 在调用模型前失败，避免错误向量进入查询。
        raise ValueError("编码器必须声明 model_name")
    # 查询端只能使用 M1.2 入库时固定的 BGE-M3。
    if encoder_model_name != MODEL_NAME:
        # 不同模型生成的坐标不能与现有文档向量直接比较。
        raise ValueError("编码器模型名与入库模型不一致")

    # 复用批量 Embedding 接口，把单个问题包装成单元素列表。
    query_vectors = generate_embeddings([question], encoder)
    # generate_embeddings 已保证输入一条就返回一条合法单位向量。
    query_vector = query_vectors[0]
    # 从真实查询向量读取维度，不硬编码 BGE-M3 的实测数字。
    embedding_dim = len(query_vector)
    # 用查询维度和固定模型契约打开现有 collection。
    store = ChromaChunkStore(
        chroma_path,
        embedding_dim=embedding_dim,
        model_name=MODEL_NAME,
    )
    # 把已校验查询向量交给存储层，并返回稳定结果对象。
    return store.query_chunks(query_vector, top_k=top_k)
