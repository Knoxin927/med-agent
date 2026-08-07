"""把文本块和 dense 向量持久化到本地 Chroma collection。"""

# 导入 hashlib，使用 SHA-256 计算跨运行稳定的文本块 ID。
import hashlib
# 导入 math，检查向量元素是否为有限浮点数。
import math
# 导入 dataclass，定义写入摘要这种只读数据对象。
from dataclasses import dataclass
# 导入 Path，统一处理本地 Chroma 目录。
from pathlib import Path, PureWindowsPath
# 导入 Sequence，允许函数接收列表等只读序列。
from typing import Sequence

# 导入 Chroma 的持久化客户端和运行设置。
import chromadb
from chromadb.config import Settings

# 导入 M1.1 的文本块模型和 M1.2 的默认模型名称。
from app.rag.chunking import TextChunk
from app.rag.embedding import MODEL_NAME
# 导入 M1.3 的稳定检索结果类型，隔离 Chroma 原始字典结构。
from app.rag.retrieval_types import RetrievalResult


# 固定 collection 名称，让 M1.3 可以按同一名称读取。
COLLECTION_NAME = "med_agent_chunks"
# 固定 Chroma 的距离空间为 cosine。
DISTANCE_SPACE = "cosine"
# 固定 Embedding 输出模式为 dense。
EMBEDDING_MODE = "dense"


# 表示一次写入完成后可供日志和调用方使用的摘要。
@dataclass(frozen=True)
class IngestionSummary:
    # 保存本次写入涉及的来源文件名。
    source_names: tuple[str, ...]
    # 保存本次实际写入的文本块数量。
    written_count: int


# 根据设计规定的字节串计算稳定文本块 ID。
def stable_chunk_id(chunk: TextChunk) -> str:
    # 组合 UTF-8 来源名、NUL 分隔符和 ASCII 十进制块序号。
    payload = (
        chunk.source_name.encode("utf-8")
        + b"\x00"
        + str(chunk.chunk_index).encode("ascii")
    )
    # 返回 SHA-256 十六进制摘要，保证不同进程和运行得到同一 ID。
    return hashlib.sha256(payload).hexdigest()


# 本地 Chroma 文本块存储封装。
class ChromaChunkStore:
    # 创建或打开固定名称和契约的持久化 collection。
    def __init__(
        self,
        path: Path,
        *,
        embedding_dim: int,
        model_name: str = MODEL_NAME,
        normalize_embeddings: bool = True,
    ) -> None:
        # 存储层同样固定 BGE-M3，避免绕过 ingestion 创建其他向量空间。
        if model_name != MODEL_NAME:
            # 其他模型的向量不能进入本项目固定 collection。
            raise ValueError(f"model_name 必须固定为 {MODEL_NAME}")
        # 本阶段 collection 只接受归一化 dense 向量。
        if not normalize_embeddings:
            # 防止 metadata 声明与实际距离语义不一致。
            raise ValueError("normalize_embeddings 必须为 True")
        # 向量维度必须是正数，避免创建不可用 collection。
        if embedding_dim <= 0:
            # 用 ValueError 表示调用方传入了非法契约。
            raise ValueError("embedding_dim 必须大于 0")
        # 保存持久化目录，方便后续调用和排查。
        self.path = path
        # 保存实际向量维度，用于每次写入前校验。
        self.embedding_dim = embedding_dim
        # 保存模型名称，用于 metadata 一致性检查。
        self.model_name = model_name
        # 保存归一化策略，用于 metadata 一致性检查。
        self.normalize_embeddings = normalize_embeddings
        # 创建持久化客户端，并关闭 Chroma 的匿名遥测。
        self._client = chromadb.PersistentClient(
            path=str(path),
            settings=Settings(anonymized_telemetry=False),
        )
        # 定义新 collection 应该具备的完整 metadata 契约。
        expected_metadata = self._expected_metadata()
        # 获取已有 collection，或按契约创建一个新 collection。
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata=expected_metadata,
        )
        # 对已有 collection 的 metadata 做显式一致性检查。
        self._validate_collection_metadata()

    # 生成需要写入和核对的 collection metadata。
    def _expected_metadata(self) -> dict[str, object]:
        # hnsw:space 是 Chroma 识别 cosine 距离的配置键。
        return {
            "hnsw:space": DISTANCE_SPACE,
            "embedding_model": self.model_name,
            "embedding_mode": EMBEDDING_MODE,
            "normalize_embeddings": self.normalize_embeddings,
            "embedding_dim": self.embedding_dim,
        }

    # 检查已有 collection 是否仍处于同一个向量契约。
    def _validate_collection_metadata(self) -> None:
        # 读取 Chroma 实际保存的 metadata；空值按空字典处理。
        actual_metadata = self._collection.metadata or {}
        # 逐项比较关键配置，避免不同模型或维度混写。
        for key, expected_value in self._expected_metadata().items():
            # 取出实际值；缺失也视为不匹配。
            actual_value = actual_metadata.get(key)
            # 发现任何差异都立即拒绝继续使用该 collection。
            if actual_value != expected_value:
                # 错误消息包含 key 和期望值，便于初学者定位配置冲突。
                raise ValueError(
                    f"collection metadata 不匹配: {key}="
                    f"{actual_value!r}，期望 {expected_value!r}"
                )

    # 返回当前 collection 中的记录数量。
    def count(self) -> int:
        # Chroma 的 count 是持久化 collection 的可观察记录数。
        return self._collection.count()

    # 读取 collection 中的文本和 metadata，供测试和后续调试使用。
    def get_all(self) -> dict[str, object]:
        # 只读取本阶段需要观察的文档和来源字段，避免额外查询向量。
        return self._collection.get(include=["documents", "metadatas"])

    # 从 Chroma 原始结果中读取一个单查询字段的内层列表。
    @staticmethod
    def _read_single_query_field(
        raw_result: dict[str, object],
        field_name: str,
    ) -> list[object]:
        # 字段必须真实存在，不能用缺省空列表掩盖第三方契约漂移。
        if field_name not in raw_result:
            # 错误信息带字段名，方便定位 Chroma 返回结构问题。
            raise ValueError(f"Chroma 查询结果缺少 {field_name}")
        # 读取字段原始值，后续逐层检查嵌套形状。
        outer_values = raw_result[field_name]
        # None 不是合法空结果，必须与真正的空内层列表区分。
        if outer_values is None:
            # 明确拒绝 None，避免上层得到伪造的空结果。
            raise ValueError(f"Chroma 查询结果的 {field_name} 不能为 None")
        # 当前接口只支持单查询，因此外层必须是恰好一项的列表。
        if not isinstance(outer_values, list) or len(outer_values) != 1:
            # 批量或缺失的外层结构都违反当前单问题契约。
            raise ValueError(f"Chroma 查询结果的 {field_name} 外层必须恰好一项")
        # 取出当前唯一查询对应的内层结果。
        inner_values = outer_values[0]
        # 内层也必须是列表，才能与其他字段按索引一一对应。
        if not isinstance(inner_values, list):
            # 拒绝元组或标量，避免不同字段产生不可预测映射。
            raise ValueError(f"Chroma 查询结果的 {field_name} 内层必须是列表")
        # 返回经过结构校验的内层列表。
        return inner_values

    # 把 Chroma 原始单查询结果转换成项目稳定值对象。
    def _parse_query_result(
        self,
        raw_result: dict[str, object],
        *,
        max_results: int,
    ) -> list[RetrievalResult]:
        # 分别读取四个字段，任何字段缺失或形状错误都会显式失败。
        ids = self._read_single_query_field(raw_result, "ids")
        # 读取每条命中的原文。
        documents = self._read_single_query_field(raw_result, "documents")
        # 读取每条命中的来源 metadata。
        metadatas = self._read_single_query_field(raw_result, "metadatas")
        # 读取每条命中的 cosine distance。
        distances = self._read_single_query_field(raw_result, "distances")
        # 四个内层列表必须严格等长，不能使用 zip 静默截断错位数据。
        result_count = len(ids)
        # 对比原文、metadata 和距离数量是否都等于 ID 数量。
        if not (
            len(documents) == result_count
            and len(metadatas) == result_count
            and len(distances) == result_count
        ):
            # 任一长度错位都会破坏文本、来源和距离的一一对应。
            raise ValueError("Chroma 查询结果的内层列表长度必须一致")
        # 第三方返回数量不能超过实际请求给 Chroma 的数量。
        if result_count > max_results:
            # 超量返回意味着第三方行为偏离本项目 Top-K 契约。
            raise ValueError("Chroma 查询结果数量不能超过实际请求数量")

        # 创建项目自己的稳定结果列表，不向上层泄漏 Chroma 字典。
        parsed_results: list[RetrievalResult] = []
        # 使用显式索引读取四个等长列表，保持字段对应关系清晰。
        for result_index in range(result_count):
            # 读取当前结果 ID；虽然不返回给上层，仍需验证记录身份存在。
            record_id = ids[result_index]
            # ID 必须是非空字符串，避免接受无法追踪的第三方记录。
            if not isinstance(record_id, str) or not record_id:
                # 无效 ID 表示 Chroma 返回记录本身不完整。
                raise ValueError("Chroma 查询结果 ID 必须是非空字符串")
            # 读取当前命中的文本原文。
            document = documents[result_index]
            # 原文必须是非空字符串，不能把空记录交给后续 LLM。
            if not isinstance(document, str) or not document.strip():
                # 显式拒绝缺失或空白文档。
                raise ValueError("Chroma 查询结果文本必须是非空字符串")
            # 读取当前记录的 metadata。
            metadata = metadatas[result_index]
            # metadata 必须是字典，才能安全读取来源和块序号。
            if not isinstance(metadata, dict):
                # 不使用默认来源掩盖第三方数据错误。
                raise ValueError("Chroma 查询结果 metadata 必须是字典")
            # 从 metadata 读取来源文件名。
            source_name = metadata.get("source_name")
            # 来源必须是非空纯文件名，继续保持 M1.1 的隐私边界。
            if (
                not isinstance(source_name, str)
                or not source_name.strip()
                or source_name in {".", ".."}
                or "/" in source_name
                or "\\" in source_name
                or bool(PureWindowsPath(source_name).drive)
            ):
                # 路径型来源可能泄露本机目录，必须拒绝返回。
                raise ValueError("Chroma 查询结果来源名必须是脱敏后的纯文件名")
            # 从 metadata 读取文本块顺序编号。
            chunk_index = metadata.get("chunk_index")
            # bool 是 int 的子类，因此需要单独拒绝 True 和 False。
            if (
                isinstance(chunk_index, bool)
                or not isinstance(chunk_index, int)
                or chunk_index < 0
            ):
                # 块序号必须是从零开始的普通整数。
                raise ValueError("Chroma 查询结果 chunk_index 必须是非负整数")
            # 读取当前记录的原始距离。
            raw_distance = distances[result_index]
            # 距离必须是普通整数或浮点数，布尔值不能冒充距离。
            if isinstance(raw_distance, bool) or not isinstance(
                raw_distance,
                (int, float),
            ):
                # 拒绝字符串等隐式转换，保持第三方返回契约严格。
                raise ValueError("Chroma 查询结果 distance 必须是数值")
            # 统一转换为 Python float，固定对上层暴露的类型。
            distance = float(raw_distance)
            # NaN 和无穷大会破坏排序和后续阈值判断。
            if not math.isfinite(distance):
                # 非有限距离不能进入稳定结果对象。
                raise ValueError("Chroma 查询结果 distance 必须是有限浮点数")
            # 保存字段已经全部验证的检索结果。
            parsed_results.append(
                RetrievalResult(
                    text=document,
                    source_name=source_name,
                    chunk_index=chunk_index,
                    distance=distance,
                )
            )

        # Chroma cosine distance 应按从小到大排列。
        if any(
            parsed_results[index].distance > parsed_results[index + 1].distance
            for index in range(len(parsed_results) - 1)
        ):
            # 顺序异常时不能由上层误把较远记录当成最近结果。
            raise ValueError("Chroma 查询结果必须按 distance 非降序排列")
        # 返回完成结构、字段和顺序校验的结果列表。
        return parsed_results

    # 使用一条已归一化查询向量检索最多 top_k 个文本块。
    def query_chunks(
        self,
        query_vector: Sequence[float],
        *,
        top_k: int,
    ) -> list[RetrievalResult]:
        # top_k 必须是普通正整数，布尔值不能作为 0 或 1 使用。
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
            # 非法 K 无法表达“最多返回多少条”的明确语义。
            raise ValueError("top_k 必须是正整数")
        # bool 是 int 的子类，转换为 float 前必须单独拒绝。
        if any(isinstance(value, bool) for value in query_vector):
            # True/False 不是有意义的 Embedding 坐标。
            raise ValueError("查询向量元素不能是布尔值")
        # 将 NumPy 数值或其他实数序列统一转换为 Python float。
        normalized_query = [float(value) for value in query_vector]
        # 查询向量不能为空，否则没有可比较的语义坐标。
        if not normalized_query:
            # 空向量不能发送给 Chroma。
            raise ValueError("query_vector 不能为空")
        # 查询维度必须与 collection metadata 中的实际维度一致。
        if len(normalized_query) != self.embedding_dim:
            # 不同维度属于不同坐标空间，不能继续比较。
            raise ValueError("查询向量维度与 collection metadata 不一致")
        # 每个元素都必须是有限浮点数。
        if any(not math.isfinite(value) for value in normalized_query):
            # NaN 或无穷大会破坏 Chroma 距离计算。
            raise ValueError("查询向量元素必须是有限浮点数")
        # 计算查询向量范数，验证它与入库向量采用相同归一化策略。
        query_norm = math.sqrt(sum(value * value for value in normalized_query))
        # 单位向量范数应在浮点误差范围内接近 1。
        if not math.isclose(query_norm, 1.0, rel_tol=1e-5, abs_tol=1e-5):
            # 未归一化查询会让 metadata 契约和实际数据不一致。
            raise ValueError("查询向量必须是已经归一化的单位向量")
        # 读取当前记录数量，避免空库调用无意义的 Chroma query。
        record_count = self.count()
        # 空 collection 是合法状态，直接返回空结果。
        if record_count == 0:
            # 没有资料可检索不属于程序异常。
            return []
        # 实际请求数量不能超过 collection 当前记录数。
        result_limit = min(top_k, record_count)
        # 让 Chroma 使用 collection 的 cosine 配置执行最近邻查询。
        raw_result = self._collection.query(
            query_embeddings=[normalized_query],
            n_results=result_limit,
            include=["documents", "metadatas", "distances"],
        )
        # 把第三方嵌套结构转换成稳定项目值对象。
        return self._parse_query_result(raw_result, max_results=result_limit)

    # 读取指定来源的完整旧快照，供写入失败时恢复。
    def _read_source_snapshot(self, source_name: str) -> dict[str, object]:
        # 同时保存 ID、原文、metadata 和向量，回滚时缺一不可。
        return self._collection.get(
            where={"source_name": source_name},
            include=["documents", "metadatas", "embeddings"],
        )

    # 将写入前保存的旧来源快照恢复到 collection。
    def _restore_source_snapshots(
        self,
        source_names: Sequence[str],
        snapshots: Sequence[dict[str, object]],
    ) -> None:
        # 先清理可能由失败写入留下的部分新记录。
        for source_name in source_names:
            # 只删除本轮涉及的来源，不影响 collection 中的其他来源。
            self._collection.delete(where={"source_name": source_name})
        # 逐个恢复写入前实际存在的来源快照。
        for snapshot in snapshots:
            # Chroma 无论 include 哪些字段都会返回 IDs。
            ids = snapshot["ids"]
            # 空来源在写入前没有旧记录，因此不需要恢复。
            if not ids:
                # 继续处理下一个来源快照。
                continue
            # 使用完整备份恢复原 ID、向量、原文和 metadata。
            self._collection.upsert(
                ids=ids,
                embeddings=snapshot["embeddings"],
                documents=snapshot["documents"],
                metadatas=snapshot["metadatas"],
            )

    # 校验并按来源快照写入一批文本块和对应向量。
    def upsert_chunks(
        self,
        chunks: Sequence[TextChunk],
        vectors: Sequence[Sequence[float]],
    ) -> IngestionSummary:
        # 空批次没有可写入内容，必须显式失败。
        if not chunks:
            # 用 ValueError 拒绝空文本块列表。
            raise ValueError("chunks 不能为空")
        # 文本块和向量数量必须一一对应。
        if len(chunks) != len(vectors):
            # 数量不一致时不能继续，否则会发生数据错位。
            raise ValueError("chunks 和 vectors 数量必须一致")
        # 检查每个文本块的来源和内容是否非空。
        if any(not chunk.source_name.strip() or not chunk.text.strip() for chunk in chunks):
            # 空来源或空文本无法追溯，必须被拒绝。
            raise ValueError("文本块来源名和文本内容不能为空")
        # 来源只允许纯文件名，不能包含目录分隔符或特殊目录名。
        if any(
            chunk.source_name in {".", ".."}
            or "/" in chunk.source_name
            or "\\" in chunk.source_name
            or bool(PureWindowsPath(chunk.source_name).drive)
            for chunk in chunks
        ):
            # 在持久化边界再次校验，防止调用方绕过 M1.1 泄露本机路径。
            raise ValueError("文本块来源名必须是脱敏后的纯文件名")

        # 逐条转换成普通浮点列表，便于统一校验和交给 Chroma。
        normalized_vectors = [
            [float(value) for value in vector]
            for vector in vectors
        ]
        # 不允许空向量进入持久化层。
        if any(not vector for vector in normalized_vectors):
            # 空向量没有有效维度。
            raise ValueError("向量不能为空")
        # 每个向量维度必须等于 collection 的实际维度。
        if any(len(vector) != self.embedding_dim for vector in normalized_vectors):
            # 维度不匹配会破坏距离计算。
            raise ValueError("向量维度与 collection metadata 不一致")
        # 每个元素必须是有限浮点数，不能是 NaN 或无穷大。
        if any(
            not math.isfinite(value)
            for vector in normalized_vectors
            for value in vector
        ):
            # 非法数值必须在删除旧数据前被拦截。
            raise ValueError("向量元素必须是有限浮点数")
        # metadata 声明归一化时，存储边界也必须验证每条向量范数。
        if self.normalize_embeddings and any(
            not math.isclose(
                math.sqrt(sum(value * value for value in vector)),
                1.0,
                rel_tol=1e-5,
                abs_tol=1e-5,
            )
            for vector in normalized_vectors
        ):
            # 防止绕过 Embedding 模块直接写入未归一化向量。
            raise ValueError("向量必须是已经归一化的单位向量")

        # 在任何删除操作前生成全部 IDs、文档和 metadata。
        ids = [stable_chunk_id(chunk) for chunk in chunks]
        # 同一批次的稳定 ID 必须唯一，否则 Chroma upsert 会在删除旧数据后失败。
        if len(ids) != len(set(ids)):
            # 提前拒绝重复来源和块序号，保护旧快照不被误删。
            raise ValueError("同一批次不能包含重复的 source_name 和 chunk_index")
        # 保存原文，方便 M1.3 返回引用和调试入库结果。
        documents = [chunk.text for chunk in chunks]
        # 保存来源名和块序号，不保存绝对路径。
        metadatas = [
            {
                "source_name": chunk.source_name,
                "chunk_index": chunk.chunk_index,
            }
            for chunk in chunks
        ]
        # 找出本批次需要替换的所有来源。
        source_names = tuple(dict.fromkeys(chunk.source_name for chunk in chunks))
        # 删除前备份每个来源的完整旧快照，为写入异常准备回滚数据。
        old_snapshots = [
            self._read_source_snapshot(source_name)
            for source_name in source_names
        ]
        # 捕获删除或写入异常，以便恢复删除前保存的旧快照。
        try:
            # 先删除这些来源的旧快照，确保成功更新后不会残留旧块。
            for source_name in source_names:
                # where 条件只按脱敏后的来源文件名匹配。
                self._collection.delete(where={"source_name": source_name})
            # 一次性写入经过完整校验的新快照。
            self._collection.upsert(
                ids=ids,
                embeddings=normalized_vectors,
                documents=documents,
                metadatas=metadatas,
            )
        # Chroma、磁盘或测试注入错误都需要进入同一回滚流程。
        except Exception as write_error:
            # 尝试清理部分新数据并恢复全部旧来源。
            try:
                # 使用删除前备份恢复旧快照。
                self._restore_source_snapshots(source_names, old_snapshots)
            # 回滚本身失败时不能假装旧数据已经安全恢复。
            except Exception as rollback_error:
                # 同时保留写入错误作为异常链，方便定位原始故障。
                raise RuntimeError(
                    f"Chroma 写入失败，旧快照回滚也失败: {rollback_error}"
                ) from write_error
            # 回滚成功后重新抛出原始写入异常，让调用方知道入库未完成。
            raise

        # 返回本次写入摘要，不把 Chroma 内部对象暴露给调用方。
        return IngestionSummary(
            source_names=source_names,
            written_count=len(chunks),
        )
