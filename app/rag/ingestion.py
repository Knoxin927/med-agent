"""连接 TextChunk、Embedding 和 Chroma 的线性入库流程。"""

# 导入 Any，允许接收真实模型或测试替身编码器。
from typing import Any
# 导入 Path，指定本地 Chroma 持久化目录。
from pathlib import Path

# 导入 M1.1 的文本块模型。
from app.rag.chunking import TextChunk
# 导入 M1.2 的默认模型名称和向量生成函数。
from app.rag.embedding import MODEL_NAME, generate_embeddings
# 导入 Chroma 写入器和写入摘要类型。
from app.rag.vector_store import ChromaChunkStore, IngestionSummary


# 把文本块批量生成向量并写入本地 Chroma。
def ingest_chunks(
    chunks: list[TextChunk],
    encoder: Any,
    chroma_path: Path,
    *,
    model_name: str = MODEL_NAME,
) -> IngestionSummary:
    # 本阶段只允许固定的 BGE-M3，调用方不能改用其他向量空间。
    if model_name != MODEL_NAME:
        # 明确拒绝参数覆盖，保证 collection metadata 始终可信。
        raise ValueError(f"model_name 必须固定为 {MODEL_NAME}")
    # 只提取文本内容，保持 TextChunk 的来源和序号由存储层保存。
    texts = [chunk.text for chunk in chunks]
    # 读取编码器实际模型名；缺失属性时保留 None，不能猜测身份。
    encoder_model_name = getattr(encoder, "model_name", None)
    # 编码器必须主动声明模型身份，类型标注不能替代运行时证明。
    if encoder_model_name is None:
        # 未知向量空间不能被静默标记成 BGE-M3。
        raise ValueError("编码器必须声明 model_name")
    # 编码器实际模型必须与本阶段固定模型一致。
    if encoder_model_name != MODEL_NAME:
        # 不允许把其他模型的向量伪装成 BGE-M3 写入 collection。
        raise ValueError("编码器模型名与入库 metadata 不一致")
    # 模型契约一致后再生成并完整校验向量，失败时还不会触碰 Chroma 旧数据。
    vectors = generate_embeddings(texts, encoder)
    # 使用模型实际返回的维度创建 collection，不硬编码未经实测的数字。
    embedding_dim = len(vectors[0])
    # 按固定模型、归一化和 cosine 契约创建本地持久化存储。
    store = ChromaChunkStore(
        chroma_path,
        embedding_dim=embedding_dim,
        model_name=model_name,
    )
    # 将已校验的文本块和向量一次性写入，并返回写入摘要。
    return store.upsert_chunks(chunks, vectors)
