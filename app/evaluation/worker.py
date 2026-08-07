"""在独立进程中构建 M2.1 索引或执行真实 dense 检索评测。"""

# 导入 hashlib，计算持久化文本快照的可追溯 hash。
import hashlib
# 导入 json，读写父子进程之间的小型结构化协议。
import json
# 导入 os，记录正式评测的操作系统资源身份。
import os
# 导入 platform，记录 CPU 与 Python 环境信息。
import platform
# 导入 sys，向操作系统返回明确的 worker 退出码。
import sys
# 导入 dataclass，定义经过校验的 worker 请求。
from dataclasses import asdict, dataclass
# 导入 Path，统一处理只位于临时目录中的 request/result 路径。
from pathlib import Path
# 导入 perf_counter，计量索引构建、冷启动和热路径。
from time import perf_counter
# 导入 Any，接收 JSON 解码后的待校验值。
from typing import Any

# 导入 Chroma collection 名称，读取 builder 实际写入的快照。
from app.rag.vector_store import COLLECTION_NAME
# 导入真实本地 BGE-M3 编码器。
from app.rag.embedding import BgeM3Embedder
# 导入固定入库编排，不改变 M1.2 语义。
from app.rag.ingestion import ingest_chunks
# 导入 bundle 加载器，worker 也独立校验输入 hash。
from app.evaluation.data import load_evaluation_bundle
# 导入人工确认加载器，使 evaluator 在子进程内复验快照绑定。
from app.evaluation.data import load_and_validate_confirmation
# 导入已发布快照加载器和只读改写器。
from app.evaluation.rewrite_snapshot import (
    SnapshotQueryRewriter,
    load_verified_rewrite_snapshot,
)
# 导入热路径评测循环。
from app.evaluation.runner import _run_rerank_case, run_hot_evaluation, run_hot_rerank_evaluation
# 导入 M2 dense production adapter。
from app.retrieval_strategies.dense import DenseRetrievalStrategy
# 导入改写到 dense 的实验策略，保持公共检索契约。
from app.retrieval_strategies.rewrite import RewriteDenseRetrievalStrategy
# 导入在内存中构建的 BM25 策略和受控 tokenizer 身份校验。
from app.retrieval_strategies.bm25 import Bm25RetrievalStrategy, get_tokenizer
# 导入固定 RRF 融合策略与其不可变运行参数。
from app.retrieval_strategies.hybrid import (
    CANDIDATE_K,
    RRF_K,
    HybridRrfRetrievalStrategy,
)
# 导入固定 reranker 参数和真实 CrossEncoder 适配器。
from app.retrieval_strategies.rerank import (
    BgeCrossEncoderScorer,
    RERANKER_CANDIDATE_K,
    RERANKER_MAX_LENGTH,
    RERANKER_MODEL_ID,
    RERANKER_MODEL_REVISION,
)


# 固定父子进程 request/result schema 版本。
WORKER_SCHEMA_VERSION = 1
# 允许的 worker 职责名称。
WORKER_KINDS = frozenset({"builder", "evaluator"})
# method 只允许已设计的两种离线评测策略。
METHODS = frozenset({"dense", "hybrid", "dense-rerank", "rewrite-dense"})


# 读取当前主机的总物理内存，不把“未测进程峰值”误用于该字段。
def _get_total_memory_bytes() -> int:
    """返回操作系统报告的总物理内存字节数，无法读取时显式失败。"""

    # Windows 是本项目的当前正式运行平台，使用内置 ctypes 不新增依赖。
    if os.name == "nt":
        # 延迟导入 ctypes，保持非 Windows 的模块导入兼容。
        import ctypes

        # 对应 Windows MEMORYSTATUSEX 结构，dwLength 必须由调用方初始化。
        class MemoryStatusEx(ctypes.Structure):
            """映射 GlobalMemoryStatusEx 所需的最小系统内存结构。"""

            # 按 Windows API 声明字段顺序定义结构成员。
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        # 创建结构并填写 API 要求的自身长度。
        memory_status = MemoryStatusEx()
        # Windows 依赖该字段决定可写入的结构大小。
        memory_status.dwLength = ctypes.sizeof(MemoryStatusEx)
        # 调用系统 API 获取当前主机内存信息。
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory_status)):
            # API 失败时不伪造一个资源数值。
            raise OSError("无法读取 Windows 总物理内存")
        # 总物理内存必须是正常正整数。
        total_memory_bytes = int(memory_status.ullTotalPhys)
    # 非 Windows 环境使用 POSIX 标准 sysconf 的物理页数和页大小。
    else:
        # 读取物理内存页数量。
        page_count = os.sysconf("SC_PHYS_PAGES")
        # 读取每个物理页的字节数。
        page_size = os.sysconf("SC_PAGE_SIZE")
        # 两值相乘得到总物理内存字节数。
        total_memory_bytes = int(page_count) * int(page_size)
    # 零、负数或 bool 都不是可发布的主机资源数据。
    if type(total_memory_bytes) is not int or total_memory_bytes <= 0:
        # 不允许把异常环境写成可信整数。
        raise ValueError("total_memory_bytes 必须是正整数")
    # 返回可 JSON 序列化的 Python int。
    return total_memory_bytes


# 保存已经通过字段校验的内部 worker 请求。
@dataclass(frozen=True)
class WorkerRequest:
    """父进程交给单个 builder 或 evaluator 的最小运行配置。"""

    # 保存本协议版本。
    schema_version: int
    # 保存 builder 或 evaluator 职责。
    worker_kind: str
    # 保存项目根目录，仅存在于临时进程协议中。
    project_root: Path
    # 保存版本化 manifest 路径。
    manifest_path: Path
    # 保存版本化 dataset 路径。
    dataset_path: Path
    # 保存当前 TemporaryDirectory 内的全新 Chroma 路径。
    chroma_path: Path
    # 保存 dense 或 hybrid 的受控方法选择。
    method: str
    # 保存 hybrid 使用的固定 tokenizer；dense 必须为空。
    tokenizer_id: str | None
    # 保存 hybrid 的固定两路候选数；dense 必须为空。
    candidate_k: int | None
    # 保存 hybrid 的固定 RRF 常数；dense 必须为空。
    rrf_k: int | None
    # 保存 dense-rerank 引用的已验证 smoke 工件身份；其他方法必须为空。
    reranker_smoke_id: str | None
    # 保存 rewrite-dense 唯一允许消费的已发布快照目录；其他方法必须为空。
    rewrite_snapshot_path: Path | None
    # 保存人工 confirmation 路径，仅 rewrite-dense 在子进程中重新校验它。
    confirmation_path: Path | None
    # 保存与 confirmation 绑定的 annotation review 路径。
    annotation_review_path: Path | None
    # 保存固定预热轮次。
    warmup_rounds: int
    # 保存固定正式轮次。
    measured_rounds: int
    # 保存固定问题顺序 seed。
    seed: int


# 读取并校验父进程写入的 JSON 请求。
def load_worker_request(request_path: Path) -> WorkerRequest:
    """将不可信 JSON 转换为只能用于当前 worker 的明确配置。"""

    # 读取 UTF-8 临时协议文件。
    raw_value = json.loads(request_path.read_text(encoding="utf-8"))
    # request 顶层必须是对象。
    if not isinstance(raw_value, dict):
        # 不接受数组或标量。
        raise ValueError("worker request 顶层必须是对象")
    # schema 必须与当前 worker 精确相同。
    if raw_value.get("schema_version") != WORKER_SCHEMA_VERSION:
        # 未知协议不能猜测字段含义。
        raise ValueError("worker request schema_version 不受支持")
    # worker_kind 只允许两个固定值。
    worker_kind = raw_value.get("worker_kind")
    # 不允许任意模块或命令注入。
    if worker_kind not in WORKER_KINDS:
        # 正式 CLI 只会创建固定 production worker。
        raise ValueError("worker request worker_kind 不受支持")
    # method 必须是受控枚举，禁止动态装配任意策略。
    method = raw_value.get("method")
    # 未知 method 会让正式报告失去可比较口径。
    if method not in METHODS:
        # 仅允许 dense 与 hybrid 两种已批准方法。
        raise ValueError("worker request method 不受支持")
    # 读取 hybrid 专属参数，dense 不允许夹带无效配置。
    tokenizer_id = raw_value.get("tokenizer_id")
    # 读取 hybrid 固定候选数量。
    candidate_k = raw_value.get("candidate_k")
    # 读取 hybrid 固定 RRF 常数。
    rrf_k = raw_value.get("rrf_k")
    # hybrid 必须携带设计指定的全部固定参数。
    if method == "hybrid":
        # tokenizer 必须是字符串，随后通过受控候选表验证。
        if not isinstance(tokenizer_id, str):
            # 不接受空值或调用方对象。
            raise ValueError("hybrid request tokenizer_id 必须是字符串")
        # 读取 tokenizer 只为验证身份，不在 request 中接受可执行对象。
        get_tokenizer(tokenizer_id)
        # candidate_k 与 rrf_k 必须精确匹配设计常量。
        if candidate_k != CANDIDATE_K or rrf_k != RRF_K:
            # 防止命令调用者悄悄改变融合实验参数。
            raise ValueError("hybrid request 固定参数不匹配")
    # dense-rerank 固定只接受设计中的候选宽度与成功 smoke 身份。
    elif method == "dense-rerank":
        # tokenizer 和 RRF 只属于 hybrid，不能混入本实验。
        if tokenizer_id is not None or rrf_k is not None:
            # 防止报告混淆 dense-rerank 与 hybrid 参数。
            raise ValueError("dense-rerank request 不能包含 hybrid 参数")
        # 候选宽度必须精确匹配设计固定值。
        if candidate_k != RERANKER_CANDIDATE_K:
            # 不允许命令调用方静默改变 rerank 因果实验矩阵。
            raise ValueError("dense-rerank request candidate_k 不匹配")
    # rewrite-dense 与 dense 一样不使用候选池、BM25 或 reranker 参数。
    elif method == "rewrite-dense":
        # 三项参数只属于 hybrid 或 rerank，不能混入快照实验。
        if any(value is not None for value in (tokenizer_id, candidate_k, rrf_k)):
            # 防止方法间参数混用破坏实验因果边界。
            raise ValueError("rewrite-dense request 不能包含其他策略参数")
    # dense 不需要，也不允许传入其他实验参数。
    elif any(value is not None for value in (tokenizer_id, candidate_k, rrf_k)):
        # 防止报告出现不属于 dense 的伪参数。
        raise ValueError("dense request 不能包含 hybrid 参数")
    # smoke 身份只允许 dense-rerank 声明，且必须是安全的非空名称。
    reranker_smoke_id = raw_value.get("reranker_smoke_id")
    # 正式 rerank 必须已通过独立模型 smoke。
    if method == "dense-rerank":
        # 只接受不带路径分隔符的稳定目录名。
        if (
            not isinstance(reranker_smoke_id, str)
            or not reranker_smoke_id
            or "/" in reranker_smoke_id
            or "\\" in reranker_smoke_id
        ):
            # 防止请求绕过 smoke gate 或控制本机路径。
            raise ValueError("dense-rerank request 缺少合法 reranker_smoke_id")
    # 其他方法不得夹带 rerank 证据身份。
    elif reranker_smoke_id is not None:
        # 保持方法参数严格互斥。
        raise ValueError("非 dense-rerank request 不能包含 reranker_smoke_id")
    # 快照路径只允许 rewrite-dense 使用，避免历史方法默默忽略额外证据。
    rewrite_snapshot_path = raw_value.get("rewrite_snapshot_path")
    if method == "rewrite-dense" and worker_kind == "evaluator":
        # evaluator 必须有非空快照，后续资源装配会加载并重新校验该目录。
        if not isinstance(rewrite_snapshot_path, str) or not rewrite_snapshot_path.strip():
            # 不允许 evaluator 在没有冻结改写时回退为原问题。
            raise ValueError("rewrite-dense evaluator 缺少 rewrite_snapshot_path")
        # 转为 Path 但不在协议解析阶段访问文件系统。
        parsed_rewrite_snapshot_path = Path(rewrite_snapshot_path)
    elif method == "rewrite-dense" and worker_kind == "builder":
        # builder 仅重建向量索引，不查询也不消费改写快照，因此该字段必须为空。
        if rewrite_snapshot_path is not None:
            # 避免调用方误以为 builder 已验证或执行了改写。
            raise ValueError("rewrite-dense builder 不能包含 rewrite_snapshot_path")
        # 保存空值以明确 builder 与 evaluator 的职责边界。
        parsed_rewrite_snapshot_path = None
    elif rewrite_snapshot_path is not None:
        # 其他方法携带该字段说明调用方混淆了实验矩阵。
        raise ValueError("非 rewrite-dense request 不能包含 rewrite_snapshot_path")
    else:
        # 历史方法显式保存 None，保持 dataclass 字段完整。
        parsed_rewrite_snapshot_path = None
    # rewrite-dense 必须在子进程中重新加载人工确认及其审阅文件。
    raw_confirmation_path = raw_value.get("confirmation_path")
    raw_annotation_review_path = raw_value.get("annotation_review_path")
    if method == "rewrite-dense" and worker_kind == "evaluator":
        # evaluator 的两个路径都必须是非空字符串，不能跳过冻结输入复验。
        if (
            not isinstance(raw_confirmation_path, str)
            or not raw_confirmation_path.strip()
            or not isinstance(raw_annotation_review_path, str)
            or not raw_annotation_review_path.strip()
        ):
            # 不允许只验证快照而忽略确认或审阅文件。
            raise ValueError("rewrite-dense evaluator 缺少确认输入路径")
        # 转换为 Path，具体存在性和 hash 在 evaluator 装配时验证。
        parsed_confirmation_path = Path(raw_confirmation_path)
        parsed_annotation_review_path = Path(raw_annotation_review_path)
    elif (
        raw_confirmation_path is not None
        or raw_annotation_review_path is not None
    ):
        # builder 或历史方法带入改写专用输入同样属于协议混淆。
        raise ValueError("非 rewrite-dense evaluator 不能包含确认输入路径")
    else:
        # 历史方法保持空值，避免影响现有 dense/hybrid/rerank。
        parsed_confirmation_path = None
        parsed_annotation_review_path = None
    # 路径字段必须都是非空字符串。
    path_fields = ["project_root", "manifest_path", "dataset_path", "chroma_path"]
    # 收集经过基础检查的路径对象。
    paths: dict[str, Path] = {}
    # 逐个转换路径字段。
    for field_name in path_fields:
        # 读取 JSON 值。
        raw_path = raw_value.get(field_name)
        # 空文本、非字符串都不是合法路径。
        if not isinstance(raw_path, str) or not raw_path.strip():
            # 不在错误中输出本机临时路径。
            raise ValueError(f"worker request {field_name} 必须是非空字符串")
        # 转换为 Path，具体边界由调用的 bundle loader 再检查。
        paths[field_name] = Path(raw_path)
    # 数值字段必须是普通整数；bool 不能冒充配置。
    for field_name in ["warmup_rounds", "measured_rounds", "seed"]:
        # 读取当前数值。
        value = raw_value.get(field_name)
        # 拒绝 bool 和其他类型。
        if type(value) is not int:
            # 明确指出当前协议字段有误。
            raise ValueError(f"worker request {field_name} 必须是整数")
    # 预热轮次可以为零，但不能为负数。
    if raw_value["warmup_rounds"] < 0:
        # 负轮次没有实际语义。
        raise ValueError("worker request warmup_rounds 不能为负数")
    # 正式轮次必须至少一轮。
    if raw_value["measured_rounds"] <= 0:
        # 零轮无法生成热路径证据。
        raise ValueError("worker request measured_rounds 必须为正整数")
    # 返回字段均已校验的不可变请求。
    return WorkerRequest(
        schema_version=WORKER_SCHEMA_VERSION,
        worker_kind=worker_kind,
        project_root=paths["project_root"],
        manifest_path=paths["manifest_path"],
        dataset_path=paths["dataset_path"],
        chroma_path=paths["chroma_path"],
        method=method,
        tokenizer_id=tokenizer_id,
        candidate_k=candidate_k,
        rrf_k=rrf_k,
        reranker_smoke_id=reranker_smoke_id,
        rewrite_snapshot_path=parsed_rewrite_snapshot_path,
        confirmation_path=parsed_confirmation_path,
        annotation_review_path=parsed_annotation_review_path,
        warmup_rounds=raw_value["warmup_rounds"],
        measured_rounds=raw_value["measured_rounds"],
        seed=raw_value["seed"],
    )


# 将成功结果原子写入 worker result 文件。
def write_worker_result(result_path: Path, payload: dict[str, Any]) -> None:
    """只写入 status=success 的完整 JSON，不留下半份 result。"""

    # result payload 必须显式声明成功，父进程不能靠缺异常判断。
    if payload.get("status") != "success":
        # worker 失败路径不写成功 result。
        raise ValueError("worker result 必须使用 success 状态")
    # 在同目录构造临时文件，保证 replace 位于同一卷。
    temporary_path = result_path.with_suffix(result_path.suffix + ".tmp")
    # 写入可读、可审计的 UTF-8 JSON。
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    # 完整写入后一次性替换 result。
    temporary_path.replace(result_path)


# 读取 Chroma 实际快照并核对身份、文本和数量。
def _verify_written_snapshot(request: WorkerRequest) -> dict[str, Any]:
    """证明 builder 写入的 collection 与 manifest 重建 chunks 完全一致。"""

    # 再次加载输入，得到 manifest 固定参数重建的真实 chunks。
    bundle = load_evaluation_bundle(
        request.project_root,
        request.manifest_path,
        request.dataset_path,
    )
    # 延迟导入 Chroma，普通 pytest 导入 worker 时不初始化数据库。
    import chromadb
    # 导入与 ChromaChunkStore 相同的设置，保证同一进程可复用同一路径。
    from chromadb.config import Settings
    # 打开 builder 刚写入的持久化路径。
    # 必须与 vector_store.py 中入库 client 的设置完全相同；否则 Chroma
    # 会把同一路径视作不同系统配置，并在 Windows builder 内报错。
    client = chromadb.PersistentClient(
        path=str(request.chroma_path),
        settings=Settings(anonymized_telemetry=False),
    )
    # 读取固定 collection。
    collection = client.get_collection(COLLECTION_NAME)
    # 获取文本与 metadata，不读取 embedding 以减少无关数据。
    raw_snapshot = collection.get(include=["documents", "metadatas"])
    # Chroma 返回的 documents 和 metadatas 必须是列表。
    documents = raw_snapshot.get("documents")
    # metadata 与文档需要一一对应。
    metadatas = raw_snapshot.get("metadatas")
    # 不接受第三方结构漂移。
    if not isinstance(documents, list) or not isinstance(metadatas, list):
        # 无法验证时必须阻止正式评测。
        raise ValueError("Chroma 快照结构不受支持")
    # 长度不同会破坏来源与文本对应关系。
    if len(documents) != len(metadatas):
        # 不进行 zip 静默截断。
        raise ValueError("Chroma 快照文本与 metadata 数量不一致")
    # 将实际 collection 转为稳定 identity -> 文本 hash 映射。
    actual: dict[tuple[str, int], str] = {}
    # 显式用索引读取两列。
    for index in range(len(documents)):
        # 当前文档必须是非空字符串。
        document = documents[index]
        # metadata 必须是对象。
        metadata = metadatas[index]
        # 基础类型错误不能进入快照证明。
        if not isinstance(document, str) or not isinstance(metadata, dict):
            # Chroma 返回不完整记录时显式失败。
            raise ValueError("Chroma 快照记录结构不受支持")
        # 读取稳定来源和块序号。
        source_name = metadata.get("source_name")
        # 读取块序号。
        chunk_index = metadata.get("chunk_index")
        # 不接受不完整 metadata。
        if not isinstance(source_name, str) or type(chunk_index) is not int:
            # 无法验证 identity 时不能继续。
            raise ValueError("Chroma 快照缺少稳定 chunk identity")
        # 组合实际 identity。
        identity = (source_name, chunk_index)
        # collection 内重复 identity 也是快照错误。
        if identity in actual:
            # 不允许额外记录掩盖为同一文本。
            raise ValueError("Chroma 快照包含重复 chunk identity")
        # 保存当前文档的 UTF-8 hash。
        actual[identity] = hashlib.sha256(document.encode("utf-8")).hexdigest()
    # 重建期望 identity -> 文本 hash 映射。
    expected = {
        (chunk.source_name, chunk.chunk_index): hashlib.sha256(
            chunk.text.encode("utf-8")
        ).hexdigest()
        for chunk in bundle.chunks
    }
    # 集合或任一文本不同都说明索引不是当前冻结语料。
    if actual != expected:
        # 不返回部分差异，避免日志泄露无关路径。
        raise ValueError("Chroma 快照与 manifest 重建 chunks 不一致")
    # JSON 对象不能使用 Python 元组作为键；将 identity 和文本 hash 转为
    # 排序后的普通列表，才能产生跨进程稳定且可 JSON 序列化的快照指纹。
    snapshot_records = [
        {
            "source_name": source_name,
            "chunk_index": chunk_index,
            "text_sha256": text_sha256,
        }
        for (source_name, chunk_index), text_sha256 in sorted(expected.items())
    ]
    # 返回只含数量和 hash 的非敏感快照证明。
    return {
        "chunk_count": len(expected),
        "snapshot_sha256": hashlib.sha256(
            json.dumps(snapshot_records, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest(),
    }


# 在独立 builder 进程内加载模型、入库和核对快照。
def run_builder_worker(request: WorkerRequest) -> dict[str, Any]:
    """创建全新临时索引；函数返回后进程将释放 BGE-M3。"""

    # 加载并校验完整发布输入。
    bundle = load_evaluation_bundle(
        request.project_root,
        request.manifest_path,
        request.dataset_path,
    )
    # index build 计时包含真实模型加载和全部文本入库。
    started_at = perf_counter()
    # builder 是唯一会在本进程持有入库模型的地方。
    encoder = BgeM3Embedder()
    # 固定使用 M1.2 经过校验的入库流程。
    ingestion_summary = ingest_chunks(list(bundle.chunks), encoder, request.chroma_path)
    # 计算独立诊断时间，不放进 cold/hot 延迟。
    index_build_ms = (perf_counter() - started_at) * 1000.0
    # 读取真实 collection，证明没有空库、缺块或额外记录。
    snapshot = _verify_written_snapshot(request)
    # 返回可 JSON 化的成功证据。
    return {
        "status": "success",
        "schema_version": WORKER_SCHEMA_VERSION,
        "worker_kind": "builder",
        "evidence_kind": f"production-{request.method}",
        "method": request.method,
        "index_build_ms": index_build_ms,
        "written_count": ingestion_summary.written_count,
        "snapshot": snapshot,
    }


# 在独立 evaluator 进程内测冷启动、预热和热路径。
def run_evaluator_worker(request: WorkerRequest) -> dict[str, Any]:
    """重新加载查询模型，并输出 dense 或 hybrid 的生产评测证据。"""

    # 重新加载并校验发布输入，避免 worker 使用被父进程替换的数据。
    bundle = load_evaluation_bundle(
        request.project_root,
        request.manifest_path,
        request.dataset_path,
    )
    # 冷启动从创建全新查询编码器开始。
    cold_started_at = perf_counter()
    # evaluator 与 builder 处于不同进程，因此此处是新的模型生命周期。
    encoder = BgeM3Embedder()
    # 用同一个临时索引创建 M2 dense adapter。
    dense_strategy = DenseRetrievalStrategy(encoder, request.chroma_path)
    # dense 没有独立的 BM25 建索引时间。
    bm25_index_build_ms: float | None = None
    # dense-rerank 额外加载本地已由 smoke 准备的固定 CrossEncoder revision。
    if request.method == "dense-rerank":
        # 延迟导入真实模型类，普通协议测试不会触发模型加载。
        from sentence_transformers import CrossEncoder

        # local_files_only 防止正式评测在没有 smoke 证据时联网下载模型。
        cross_encoder = CrossEncoder(
            RERANKER_MODEL_ID,
            revision=RERANKER_MODEL_REVISION,
            device="cpu",
            max_length=RERANKER_MAX_LENGTH,
            local_files_only=True,
        )
        # 适配器固定使用 Identity activation 与 batch size 4。
        reranker_scorer = BgeCrossEncoderScorer(cross_encoder)
    # hybrid 在同一冻结 chunks 上重建纯内存 BM25 索引。
    if request.method == "hybrid":
        # 单独开始 BM25 建索引计时，不混入热路径延迟。
        bm25_started_at = perf_counter()
        # request 已验证 tokenizer_id 在此分支一定存在。
        bm25_strategy = Bm25RetrievalStrategy(
            list(bundle.chunks),
            tokenizer_id=request.tokenizer_id,
        )
        # 记录从已验证 chunks 到索引完成的独立耗时。
        bm25_index_build_ms = (perf_counter() - bm25_started_at) * 1000.0
        # 两路各取固定 Top-20，再由 RRF 输出最终 Top-10。
        strategy = HybridRrfRetrievalStrategy(dense_strategy, bm25_strategy)
    # rewrite-dense 从已发布快照重新加载改写，质量评测保持零联网。
    elif request.method == "rewrite-dense":
        # request 解析器已保证三项路径存在，此处只供类型收窄。
        assert request.confirmation_path is not None
        assert request.annotation_review_path is not None
        assert request.rewrite_snapshot_path is not None
        # 重新验证 confirmation 与 bundle，避免父进程验证后文件被替换。
        confirmation = load_and_validate_confirmation(
            bundle,
            request.confirmation_path,
            request.annotation_review_path,
        )
        # 加载器会再次验证快照的 inputs、records 与计算出的 snapshot ID。
        rewrite_snapshot = load_verified_rewrite_snapshot(
            request.rewrite_snapshot_path,
            bundle,
            confirmation,
        )
        # 策略只从快照取改写文本，再委托同一个 dense 实例。
        strategy = RewriteDenseRetrievalStrategy(
            SnapshotQueryRewriter(rewrite_snapshot),
            dense_strategy,
        )
    # dense 维持 M2.1 的原策略和语义。
    else:
        # 不改变历史 dense 的检索行为。
        strategy = dense_strategy
    # dense-rerank 的冷启动必须包含首次 dense 完整候选与 rerank 输出校验。
    if request.method == "dense-rerank":
        # 只读取第一条冻结问题，模型下载不属于本计时口径。
        _run_rerank_case(
            bundle.cases[0].question,
            dense_strategy,
            reranker_scorer,
            candidate_k=RERANKER_CANDIDATE_K,
            top_k=10,
            clock=perf_counter,
        )
    # 其他历史方法保持原来首次 Top-10 检索的冷启动定义。
    else:
        # 至少有一条案例，加载器已保证；取第一条完成首次真实查询。
        strategy.retrieve(bundle.cases[0].question, top_k=10)
    # 冷启动包含加载模型、打开索引和首次检索，不含 builder 入库。
    cold_start_ms = (perf_counter() - cold_started_at) * 1000.0
    # dense-rerank 使用专用 runner，明确保存同候选 pre/post 与分阶段计时。
    if request.method == "dense-rerank":
        # 执行固定预热和交错热路径测量。
        rerank_hot_result = run_hot_rerank_evaluation(
            list(bundle.cases),
            dense_strategy,
            reranker_scorer,
            candidate_k=RERANKER_CANDIDATE_K,
            top_k=10,
            warmup_rounds=request.warmup_rounds,
            measured_rounds=request.measured_rounds,
            seed=request.seed,
            clock=perf_counter,
        )
        # 完整 pre 候选是同候选比较的原始证据。
        pre_ranked_results = {
            case_id: [asdict(result) for result in outcome.dense_candidates]
            for case_id, outcome in rerank_hot_result.outcomes_by_case_id.items()
        }
        # 完整 post 候选保存 raw logit 与重排后的 identity 顺序。
        post_ranked_results = {
            case_id: [asdict(result) for result in outcome.reranked_candidates]
            for case_id, outcome in rerank_hot_result.outcomes_by_case_id.items()
        }
        # 最终 Top-10 必须由 post 完整列表前缀派生。
        ranked_results = {
            case_id: [asdict(result) for result in outcome.final_results]
            for case_id, outcome in rerank_hot_result.outcomes_by_case_id.items()
        }
        # 将重排前逐题指标转为 JSON 原生结构。
        pre_case_metrics = {
            case_id: {
                **asdict(metrics),
                "hit_identities": [asdict(identity) for identity in metrics.hit_identities],
            }
            for case_id, metrics in rerank_hot_result.pre_case_metrics_by_case_id.items()
        }
        # 将重排后逐题指标转为 JSON 原生结构。
        case_metrics = {
            case_id: {
                **asdict(metrics),
                "hit_identities": [asdict(identity) for identity in metrics.hit_identities],
            }
            for case_id, metrics in rerank_hot_result.post_case_metrics_by_case_id.items()
        }
        # 返回 rerank 专用证据，供 CLI 与发布器继续 fail-closed 校验。
        return {
            "status": "success",
            "schema_version": WORKER_SCHEMA_VERSION,
            "worker_kind": "evaluator",
            "evidence_kind": "production-dense-rerank",
            "method": "dense-rerank",
            "cold_start_ms": cold_start_ms,
            "total_latency_samples_ms": list(rerank_hot_result.total_latency_samples_ms),
            "dense_latency_samples_ms": list(rerank_hot_result.dense_latency_samples_ms),
            "rerank_latency_samples_ms": list(rerank_hot_result.rerank_latency_samples_ms),
            "metrics_summary": asdict(rerank_hot_result.post_metrics_summary),
            "pre_ranked_results_by_case_id": pre_ranked_results,
            "post_ranked_results_by_case_id": post_ranked_results,
            "ranked_results_by_case_id": ranked_results,
            "pre_case_metrics_by_case_id": pre_case_metrics,
            "case_metrics_by_case_id": case_metrics,
            "resources": {
                "os": os.name,
                "cpu": platform.processor() or "unknown",
                "total_memory_bytes": _get_total_memory_bytes(),
                "python_version": platform.python_version(),
                "torch_version": __import__("torch").__version__,
                "sentence_transformers_version": __import__("sentence_transformers").__version__,
                "requested_device": "cpu",
                "resolved_device": "cpu",
                "peak_process_rss": "not_measured",
            },
        }
    # 预热和五轮正式热路径都由纯 runner 统一编排。
    hot_result = run_hot_evaluation(
        list(bundle.cases),
        strategy,
        warmup_rounds=request.warmup_rounds,
        measured_rounds=request.measured_rounds,
        seed=request.seed,
        clock=perf_counter,
    )
    # 将逐题排名转换为 JSON 原生字典和列表。
    ranked_results = {
        case_id: [asdict(result) for result in results]
        for case_id, results in hot_result.ranked_results_by_case_id.items()
    }
    # 将含 ChunkIdentity 的单题指标转换为 JSON 原生结构。
    case_metrics = {
        case_id: {
            **asdict(metrics),
            "hit_identities": [
                asdict(identity) for identity in metrics.hit_identities
            ],
        }
        for case_id, metrics in hot_result.case_metrics_by_case_id.items()
    }
    # rewrite-dense 额外回传已验证快照的非敏感逐题证据，供报告展示原问题和改写文本。
    rewrite_records_by_case_id = None
    if request.method == "rewrite-dense":
        # 本分支已加载并验证快照；此处仅复制允许进入正式报告的字段。
        rewrite_records_by_case_id = {
            record.case_id: {
                "question": record.question,
                "rewritten_query": record.result.rewritten_query,
                "model": record.result.model,
                "usage": dict(record.result.usage)
                if record.result.usage is not None
                else None,
                "generated_at": record.generated_at,
                "semantic_review": {
                    "status": record.semantic_review.status,
                    "reason": record.semantic_review.reason,
                    "reviewed_at": record.semantic_review.reviewed_at,
                },
            }
            for record in rewrite_snapshot.records
        }
    # 返回生产 worker 唯一能产生的官方证据。
    return {
        "status": "success",
        "schema_version": WORKER_SCHEMA_VERSION,
        "worker_kind": "evaluator",
        "evidence_kind": f"production-{request.method}",
        "method": hot_result.method_name,
        "cold_start_ms": cold_start_ms,
        "bm25_index_build_ms": bm25_index_build_ms,
        "latency_samples_ms": list(hot_result.latency_samples_ms),
        "metrics_summary": asdict(hot_result.metrics_summary),
        "ranked_results_by_case_id": ranked_results,
        "case_metrics_by_case_id": case_metrics,
        # 非改写方法保持 null，避免伪造不存在的快照证据。
        "rewrite_records_by_case_id": rewrite_records_by_case_id,
    }


# 执行 request 指定的单一 worker，并返回进程退出码。
def main(argv: list[str] | None = None) -> int:
    """读取 request、运行固定 worker，并原子写入成功 result。"""

    # 只接受 request/result 两个固定参数，不暴露模型或策略注入。
    if argv is None:
        # 直接执行模块时去掉 Python 自身的第一个参数。
        argv = sys.argv[1:]
    # 参数数量必须恰好为四，形式为 --request path --result path。
    if len(argv) != 4 or argv[0] != "--request" or argv[2] != "--result":
        # 只输出脱敏用法，不输出内部状态。
        print("worker_usage_error", file=sys.stderr)
        # 使用非零退出码通知父进程。
        return 2
    # 将两个路径转换为 Path。
    request_path = Path(argv[1])
    # 结果只写父进程指定的临时目录。
    result_path = Path(argv[3])
    # 任意加载、模型、Chroma 或指标异常都不能写 success result。
    try:
        # 读取并校验 request。
        request = load_worker_request(request_path)
        # builder 与 evaluator 由固定分支选择，不允许动态导入。
        if request.worker_kind == "builder":
            # builder 负责创建并验证临时索引。
            payload = run_builder_worker(request)
        else:
            # evaluator 负责冷启动和热路径真实检索。
            payload = run_evaluator_worker(request)
        # 成功时原子写入结构化 result。
        write_worker_result(result_path, payload)
    # 不把异常文本输出到 stderr，避免临时绝对路径或第三方细节泄露。
    except Exception:
        # 父进程只凭退出码和固定错误码处理失败。
        print("worker_failed", file=sys.stderr)
        # 使用统一失败退出码。
        return 1
    # 完整 result 写入后返回成功。
    return 0


# 仅在 `python -m app.evaluation.worker` 时运行真实 worker。
if __name__ == "__main__":
    # 把明确退出码交给操作系统和父进程。
    raise SystemExit(main())
