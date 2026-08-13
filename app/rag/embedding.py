"""使用本地 BGE-M3 生成并校验归一化 dense Embedding。"""

# 导入 math，用来检查向量元素是否为有限浮点数。
import math
# 导入 Any，表示第三方编码器返回值的具体数组类型由运行时决定。
from typing import Any


# 固定模型名称，确保写入和后续检索使用同一个向量空间。
MODEL_NAME = "BAAI/bge-m3"


# 定义真实模型的最小接口，测试可以用确定性替身实现同样的方法。
class BgeM3Embedder:
    # 加载本地 BGE-M3 模型；首次调用时可能从模型仓库下载文件。
    def __init__(self, model_name: str = MODEL_NAME) -> None:
        # 在加载重量级模型前拒绝其他模型，确保类本身也遵守固定契约。
        if model_name != MODEL_NAME:
            # 不允许通过直接构造编码器绕过 BGE-M3 向量空间约束。
            raise ValueError(f"model_name 必须固定为 {MODEL_NAME}")
        # 延迟导入重量级依赖，让只测试校验逻辑时不必初始化模型。
        from sentence_transformers import SentenceTransformer

        # 保存模型名称，便于 smoke 脚本和调试输出确认配置。
        self.model_name = model_name
        # 创建 SentenceTransformer 实例，模型缓存由库放在仓库外的默认位置。
        self._model = SentenceTransformer(model_name)

    # 暴露与测试替身相同的编码方法签名。
    def encode(
        self,
        texts: list[str],
        *,
        normalize_embeddings: bool,
    ) -> Any:
        # 将文本和归一化选项交给真实模型执行 dense 编码。
        return self._model.encode(
            texts,
            normalize_embeddings=normalize_embeddings,
        )


# 生成并校验一批文本的归一化 dense 向量。
def generate_embeddings(texts: list[str], encoder: Any) -> list[list[float]]:
    # 空列表没有任何文本可供模型编码，必须显式失败。
    if not texts:
        # 用 ValueError 表示调用方输入违反了本阶段契约。
        raise ValueError("texts 不能为空")
    # 逐条检查文本，避免空文本占用一个没有意义的向量位置。
    if any(not text.strip() for text in texts):
        # 空文本不能被静默跳过，否则会破坏文本块和向量的数量对应关系。
        raise ValueError("texts 不能包含空文本")

    # 要求编码器返回归一化结果，让后续 cosine 距离语义保持一致。
    raw_vectors = encoder.encode(texts, normalize_embeddings=True)
    # 将 NumPy 数组或其他序列转换成普通 Python 浮点列表，便于 Chroma 接收。
    vectors = [[float(value) for value in vector] for vector in raw_vectors]

    # 模型返回数量必须与输入文本数量完全一致。
    if len(vectors) != len(texts):
        # 不允许 zip 静默截断多出来或缺少的结果。
        raise ValueError("向量数量必须与文本数量一致")
    # 向量列表不能为空，且每个向量至少要有一个维度。
    if not vectors or any(not vector for vector in vectors):
        # 空向量无法表达文本语义，也无法写入向量库。
        raise ValueError("向量不能为空")
    # 读取第一条向量的长度，作为本批次的统一维度。
    dimension = len(vectors[0])
    # 同一批次内所有向量必须拥有相同维度。
    if any(len(vector) != dimension for vector in vectors):
        # 不同维度无法放入同一个 collection 的统一向量空间。
        raise ValueError("同一批次向量的维度必须一致")
    # 每一个元素都必须是有限数，不能是 NaN 或正负无穷大。
    if any(not math.isfinite(value) for vector in vectors for value in vector):
        # 非法浮点数会破坏距离计算，因此在持久化前拒绝它。
        raise ValueError("向量元素必须是有限浮点数")
    # 计算每条向量的欧几里得范数，验证编码器确实执行了归一化。
    vector_norms = [
        math.sqrt(sum(value * value for value in vector))
        for vector in vectors
    ]
    # 单位向量的范数应接近 1，容许浮点计算产生极小误差。
    if any(
        not math.isclose(norm, 1.0, rel_tol=1e-5, abs_tol=1e-5)
        for norm in vector_norms
    ):
        # 不能只相信调用参数，否则编码器忽略参数时 metadata 会说谎。
        raise ValueError("向量必须是已经归一化的单位向量")

    # 返回经过完整校验的向量列表。
    return vectors
