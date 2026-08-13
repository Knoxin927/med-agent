"""准备固定 BGE Reranker 本地缓存，并发布不可覆盖的 smoke 证据。"""

# 导入 json，保存机器可读 smoke 证据。
import json
# 导入 math，校验真实模型输出的 raw logit。
import math
# 导入 platform，记录实际运行设备和 Python 环境。
import platform
# 导入 re，限制 smoke ID 不可控制目录路径。
import re
# 导入 Path，统一处理本地 smoke 工件目录。
from pathlib import Path
# 导入 perf_counter，将下载准备、加载与推理解耦计时。
from time import perf_counter
# 导入 Any，校验和组织 JSON 对象。
from typing import Any

# 导入固定模型身份、真实 adapter 与实验参数。
from app.retrieval_strategies.rerank import (
    BgeCrossEncoderScorer,
    RERANKER_BATCH_SIZE,
    RERANKER_MAX_LENGTH,
    RERANKER_MODEL_ID,
    RERANKER_MODEL_REVISION,
)
# 导入构造非敏感内置候选的统一结果对象。
from app.retrieval_strategies.types import RankedChunk


# smoke ID 只允许安全文件名字符，避免目录逃逸。
SAFE_SMOKE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# 校验成功 smoke 的发布级 schema，供 CLI 在启动 worker 前调用。
def validate_reranker_smoke_report(smoke_directory: Path, smoke_id: str) -> dict[str, Any]:
    """读取并严格验证成功 smoke，失败时拒绝正式 dense-rerank 运行。"""

    # 请求 ID 必须是安全目录名。
    if not isinstance(smoke_id, str) or not SAFE_SMOKE_ID.fullmatch(smoke_id):
        # 禁止请求通过 ID 访问任意本机路径。
        raise ValueError("smoke_id 必须是安全的非空文件名")
    # 目录名也必须与请求 ID 精确相同。
    if smoke_directory.name != smoke_id:
        # 三方身份不一致时不能使用该模型缓存。
        raise ValueError("reranker smoke 目录名与 smoke_id 不一致")
    # 只读取标准 details.json，不从 Markdown 推测模型状态。
    details_path = smoke_directory / "details.json"
    # 缺文件说明 smoke 未完成或遭到破坏。
    if not details_path.is_file():
        # 正式运行必须停在模型准备 gate。
        raise ValueError("reranker smoke 缺少 details.json")
    # 解析机器可读对象。
    details = json.loads(details_path.read_text(encoding="utf-8"))
    # 成功 marker、目录名与 JSON 运行 ID 必须同时匹配。
    if (
        not isinstance(details, dict)
        or details.get("evidence_kind") != "production-reranker-smoke"
        or details.get("status") != "success"
        or details.get("smoke_run_id") != smoke_id
    ):
        # 不允许失败记录、fake 或其他实验充当正式模型准备证据。
        raise ValueError("reranker smoke 身份或状态不合法")
    # 精确模型与运行参数不能在 smoke 和正式评测之间漂移。
    expected = {
        "model_id": RERANKER_MODEL_ID,
        "revision": RERANKER_MODEL_REVISION,
        "activation_fn": "torch.nn.Identity",
        "requested_device": "cpu",
        "resolved_device": "cpu",
        "batch_size": RERANKER_BATCH_SIZE,
        "max_length": RERANKER_MAX_LENGTH,
        "local_files_only_load_succeeded": True,
    }
    # 逐项精确比对，缺失也等同不匹配。
    for key, value in expected.items():
        # 防止调用者用不同模型或运行参数绕过正式实验矩阵。
        if details.get(key) != value:
            # 错误信息只暴露字段名，不输出本机路径或模型内部异常。
            raise ValueError(f"reranker smoke {key} 不一致")
    # cache 状态必须被显式记录，不能用缺字段掩盖首次下载事实。
    if details.get("cache_state_before") not in {"present", "missing"}:
        # 只允许本 smoke 定义的两个状态值。
        raise ValueError("reranker smoke cache_state_before 不合法")
    # 两个固定内置 pairs 必须都产生有限 raw logit。
    scores = details.get("scores")
    # 非空数组才能证明真实 predict 已经执行。
    if not isinstance(scores, list) or not scores:
        # 不接受空列表或标量伪装模型可用。
        raise ValueError("reranker smoke scores 不合法")
    # 逐项拒绝 bool、NaN 和无穷。
    for score in scores:
        # 只接受有限的真实数值。
        if type(score) not in {int, float} or not math.isfinite(float(score)):
            # raw logit 不可比较时不得开始正式评测。
            raise ValueError("reranker smoke score 必须是有限数值")
    # 三段耗时必须是非负有限数，不接受手填文本。
    for key in ("prepare_ms", "load_ms", "inference_ms"):
        # 读取已发布的时间证据。
        value = details.get(key)
        # bool、NaN、无穷和负值都会污染后续解释。
        if type(value) not in {int, float} or not math.isfinite(float(value)) or value < 0:
            # 失败时不让半可信 smoke 被正式评测引用。
            raise ValueError(f"reranker smoke {key} 不合法")
    # 仅返回已验证对象，调用方可写入正式 details 的 smoke_run_id。
    return details


# 创建两条内置非敏感候选，避免 smoke 读取项目语料或评测数据。
def _smoke_candidates() -> tuple[RankedChunk, ...]:
    """返回可公开的 query-document 对，专用于证明真实模型可推理。"""

    # 第一条候选与问题主题相关。
    relevant = RankedChunk(
        text="高血压日常管理包括规律监测血压和遵医嘱生活方式调整。",
        source_name="smoke-a.txt",
        chunk_index=0,
        rank=1,
        method="dense",
        score=0.1,
        score_kind="cosine_distance",
        higher_is_better=False,
    )
    # 第二条候选主题不同，保证模型至少处理两组真实 pairs。
    unrelated = RankedChunk(
        text="流感常见表现包括发热、咳嗽和全身不适。",
        source_name="smoke-b.txt",
        chunk_index=0,
        rank=2,
        method="dense",
        score=0.2,
        score_kind="cosine_distance",
        higher_is_better=False,
    )
    # 返回不可变 tuple，保持 adapter 的输入契约。
    return relevant, unrelated


# 运行一次允许联网的模型准备，随后证明相同 revision 能从本地 cache 读取。
def run_reranker_smoke(reports_dir: Path, smoke_id: str) -> Path:
    """发布不可覆盖的真实模型 smoke details.json，并返回其目录。"""

    # 输入 ID 必须是安全文件名。
    if not isinstance(smoke_id, str) or not SAFE_SMOKE_ID.fullmatch(smoke_id):
        # 不允许以 smoke ID 控制报告父目录外的路径。
        raise ValueError("smoke_id 必须是安全的非空文件名")
    # 先确保父目录存在，最终目录仍然禁止覆盖。
    reports_dir.mkdir(parents=True, exist_ok=True)
    # 目标目录以 smoke ID 固定，形成不可覆盖证据身份。
    smoke_directory = reports_dir / smoke_id
    # 已存在即拒绝，不覆盖任何成功或失败的历史工件。
    if smoke_directory.exists():
        # 用户应选择新的 smoke ID 重试。
        raise FileExistsError("reranker smoke 目录已存在，不能覆盖")
    # 延迟导入模型类，使 fake 单测不触发真实依赖加载。
    from sentence_transformers import CrossEncoder
    # 仅导入 Hugging Face 明确表示“本地缓存缺失”的异常类型。
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import LocalEntryNotFoundError

    # 先用纯下载 API 检查固定 revision 的完整本地 snapshot，不构造模型。
    try:
        # 该调用只定位 cache 文件，避免把权重载入 PyTorch 内存。
        snapshot_download(
            repo_id=RERANKER_MODEL_ID,
            revision=RERANKER_MODEL_REVISION,
            local_files_only=True,
        )
        # 本地缓存已存在，不需要联网准备。
        cache_state_before = "present"
    # 只有明确的本地缓存缺失才允许本 smoke 发起唯一一次联网下载。
    except LocalEntryNotFoundError:
        # 记录本次 smoke 开始前不存在完整本地缓存。
        cache_state_before = "missing"
    # 缓存缺失时才执行本 smoke 唯一允许联网的固定 revision 下载。
    if cache_state_before == "missing":
        # 模型准备计时只覆盖首次下载或解析。
        prepare_started_at = perf_counter()
        # 本 smoke 允许下载固定 snapshot；此处不构造 CrossEncoder。
        snapshot_download(
            repo_id=RERANKER_MODEL_ID,
            revision=RERANKER_MODEL_REVISION,
            local_files_only=False,
        )
        # 保存准备阶段耗时，不能混入正式 cold start。
        prepare_ms = (perf_counter() - prepare_started_at) * 1000.0
    # 缓存已存在时没有下载准备成本，显式写零而非缺失字段。
    else:
        # 该数值不计入正式 cold start，只描述本 smoke 没有联网准备。
        prepare_ms = 0.0
    # snapshot 已准备后只构造一次模型，并强制只从本地 cache 加载。
    load_started_at = perf_counter()
    # 此唯一实例同时证明 local-only 可用并执行后续真实 smoke 推理。
    local_cross_encoder = CrossEncoder(
        RERANKER_MODEL_ID,
        revision=RERANKER_MODEL_REVISION,
        device="cpu",
        max_length=RERANKER_MAX_LENGTH,
        local_files_only=True,
    )
    # 保存唯一模型加载的实际耗时。
    load_ms = (perf_counter() - load_started_at) * 1000.0
    # 通过真实 adapter 显式触发 Identity activation 的 raw logit 推理。
    scorer = BgeCrossEncoderScorer(local_cross_encoder)
    # 固定 smoke 问题不包含私人语料或评测问题。
    question = "高血压日常如何管理？"
    # 计时仅围绕两个内置 pairs 的真实推理。
    inference_started_at = perf_counter()
    # 输出保持模型返回顺序，供 schema gate 验证为有限 raw logit。
    scores = list(scorer.score(question, _smoke_candidates()))
    # 记录实际推理耗时。
    inference_ms = (perf_counter() - inference_started_at) * 1000.0
    # 先在内存中组装完整 evidence，避免创建半份目录。
    details: dict[str, Any] = {
        "evidence_kind": "production-reranker-smoke",
        "status": "success",
        "smoke_run_id": smoke_id,
        "model_id": RERANKER_MODEL_ID,
        "revision": RERANKER_MODEL_REVISION,
        "activation_fn": "torch.nn.Identity",
        "requested_device": "cpu",
        "resolved_device": "cpu",
        "batch_size": RERANKER_BATCH_SIZE,
        "max_length": RERANKER_MAX_LENGTH,
        "cache_state_before": cache_state_before,
        "local_files_only_load_succeeded": True,
        "prepare_ms": prepare_ms,
        "load_ms": load_ms,
        "inference_ms": inference_ms,
        "scores": scores,
        "python_version": platform.python_version(),
    }
    # 发布前用同一个 gate 重读内存对象前的关键字段，防止实现漂移。
    for score in scores:
        # 推理异常必须在创建任何正式目录前失败。
        if type(score) not in {int, float} or not math.isfinite(float(score)):
            # 不发布任何 fake、NaN 或无穷的 smoke 结果。
            raise ValueError("reranker smoke 推理没有产生有限 raw logit")
    # 所有模型调用成功后才创建不可覆盖正式目录。
    smoke_directory.mkdir()
    # 写入稳定、可审计的 UTF-8 JSON。
    (smoke_directory / "details.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    # 重新走磁盘 gate，证明目录名、JSON 身份和 schema 三者一致。
    validate_reranker_smoke_report(smoke_directory, smoke_id)
    # 返回可由正式 CLI 显式引用的成功工件目录。
    return smoke_directory
