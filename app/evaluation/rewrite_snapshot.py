"""构建、校验和原子发布 M2.4 的已复核 Query 改写快照。"""

# 导入 hashlib，生成快照与人工确认的稳定身份摘要。
import hashlib
# 导入 json，使用结构化格式生成可审阅的离线证据。
import json
# 导入 os，使用同卷目录替换原子发布正式快照。
import os
# 导入 shutil，发布失败时只清理本模块创建的 staging 目录。
import shutil
# 导入 tempfile，在目标父目录中创建同卷临时目录。
import tempfile
# 导入 dataclass，定义不可变的快照数据对象。
from dataclasses import dataclass
# 导入 Path，限定快照发布目录由调用方显式传入。
from pathlib import Path
# 导入 Sequence，接受列表或元组形式的改写记录。
from typing import Sequence

# 导入冻结评测输入及人工确认对象。
from app.evaluation.types import AnnotationConfirmation, EvaluationBundle
# 导入改写结果、错误和统一结果校验函数。
from app.retrieval_strategies.rewrite import (
    QueryRewriteError,
    QueryRewriteResult,
    validate_query_rewrite_result,
)


# 固定快照 JSON schema，未来字段变更必须显式迁移。
REWRITE_SNAPSHOT_SCHEMA_VERSION = 1


# 保存用户对一条改写是否保持原意的人工复核结论。
@dataclass(frozen=True)
class SemanticReview:
    """表示改写是否可作为正式检索质量证据的人工判断。"""

    # accepted 表示允许发布，rejected 表示必须阻止整个快照。
    status: str
    # 保存可解释的接受或拒绝原因。
    reason: str
    # 保存实际复核日期，避免无时间的人工声称。
    reviewed_at: str


# 保存一题真实改写及其人工语义复核结果。
@dataclass(frozen=True)
class RewriteSnapshotRecord:
    """绑定 case、原问题、改写响应证据和人工语义复核。"""

    # 保存冻结数据集中的稳定案例编号。
    case_id: str
    # 保存必须与冻结数据精确相同的原问题。
    question: str
    # 保存已绑定响应模型与 usage 的受控改写结果。
    result: QueryRewriteResult
    # 保存真实响应发生的时间文本。
    generated_at: str
    # 保存用户对该改写的语义等价判断。
    semantic_review: SemanticReview


# 保存可供离线质量评测消费的完整、已验证快照。
@dataclass(frozen=True)
class VerifiedRewriteSnapshot:
    """表示绑定冻结输入且不含任何密钥的正式改写快照。"""

    # 保存发布目录和报告引用使用的稳定快照身份。
    rewrite_snapshot_id: str
    # 保存冻结 dataset 的原始字节 hash。
    dataset_sha256: str
    # 保存冻结 manifest 的原始字节 hash。
    manifest_sha256: str
    # 保存确认对象的稳定内容 hash，防止人工确认漂移。
    confirmation_sha256: str
    # 保存按 dataset 顺序排列的全部已接受记录。
    records: tuple[RewriteSnapshotRecord, ...]


# 将对象转换为稳定 JSON 字节，供 hash 和发布共用。
def _canonical_json(value: object) -> str:
    """返回不依赖字典插入顺序的 UTF-8 JSON 文本。"""

    # 排序和紧凑分隔符确保同一逻辑内容产生同一摘要。
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# 计算人工确认所有有效字段的稳定摘要，不依赖调用方提供路径。
def _confirmation_sha256(confirmation: AnnotationConfirmation) -> str:
    """把已加载 confirmation 对象转换为可比较的内容身份。"""

    # 展平嵌套 dataclass，保证相关 identity 也参与摘要。
    payload = {
        "schema_version": confirmation.schema_version,
        "dataset_version": confirmation.dataset_version,
        "dataset_sha256": confirmation.dataset_sha256,
        "corpus_manifest_sha256": confirmation.corpus_manifest_sha256,
        "annotation_review_sha256": confirmation.annotation_review_sha256,
        "confirmed_at": confirmation.confirmed_at,
        "cases": [
            {
                "case_id": case.case_id,
                "confirmed": case.confirmed,
                "relevant": [
                    {"source_name": identity.source_name, "chunk_index": identity.chunk_index}
                    for identity in case.relevant
                ],
            }
            for case in confirmation.cases
        ],
    }
    # 对 canonical JSON 的 UTF-8 原始字节计算 SHA-256。
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# 将已校验记录转换为 JSON 原生对象，不泄露任何请求头或密钥。
def _record_payload(record: RewriteSnapshotRecord) -> dict[str, object]:
    """生成一条可写入 details.json 的非敏感记录。"""

    # usage 映射在序列化边界复制成普通 JSON 对象。
    usage = dict(record.result.usage) if record.result.usage is not None else None
    # 返回完整但只含设计允许字段的 JSON 对象。
    return {
        "case_id": record.case_id,
        "question": record.question,
        "rewritten_query": record.result.rewritten_query,
        "model": record.result.model,
        "usage": usage,
        "generated_at": record.generated_at,
        "semantic_review": {
            "status": record.semantic_review.status,
            "reason": record.semantic_review.reason,
            "reviewed_at": record.semantic_review.reviewed_at,
        },
    }


# 严格构建只包含完整且 accepted 记录的正式快照。
def build_verified_rewrite_snapshot(
    bundle: EvaluationBundle,
    confirmation: AnnotationConfirmation,
    records: Sequence[RewriteSnapshotRecord],
) -> VerifiedRewriteSnapshot:
    """验证冻结输入与人工复核后，返回可安全发布的不可变快照。"""

    # confirmation 必须仍绑定当前 bundle 的 dataset 与 manifest。
    if (
        confirmation.dataset_sha256 != bundle.dataset_sha256
        or confirmation.corpus_manifest_sha256 != bundle.manifest_sha256
        or confirmation.dataset_version != bundle.manifest.dataset_version
    ):
        # 不让过期确认支持新数据集的快照发布。
        raise ValueError("rewrite snapshot confirmation 与冻结输入不一致")
    # 建立 case_id 到原问题的精确映射，拒绝模糊匹配。
    expected_questions = {case.case_id: case.question for case in bundle.cases}
    # 记录已出现 case_id，防止同一题生成多条相互矛盾的改写。
    seen_case_ids: set[str] = set()
    # 临时保存按 dataset 顺序最终写入的记录。
    records_by_case_id: dict[str, RewriteSnapshotRecord] = {}
    # 逐条校验调用方提交的快照记录。
    for record in records:
        # case_id 必须属于当前冻结数据且不能重复。
        if record.case_id not in expected_questions or record.case_id in seen_case_ids:
            # 缺失、未知或重复均不能由后续步骤猜测修复。
            raise ValueError("rewrite snapshot case_id 不合法或重复")
        # 原问题必须与 dataset 字节身份对应的加载值精确相等。
        if record.question != expected_questions[record.case_id]:
            # 不允许只因文字相似就沿用另一题的改写。
            raise ValueError("rewrite snapshot 原问题与冻结数据不一致")
        # 改写文本、模型和 usage 必须通过共享受控契约。
        validated_result = validate_query_rewrite_result(record.result)
        # 人工复核必须是完整的 accepted 判断。
        review = record.semantic_review
        if (
            review.status != "accepted"
            or not isinstance(review.reason, str)
            or not review.reason.strip()
            or not isinstance(review.reviewed_at, str)
            or not review.reviewed_at.strip()
            or not isinstance(record.generated_at, str)
            or not record.generated_at.strip()
        ):
            # rejected、未复核或缺少可解释原因的记录一律阻止发布。
            raise ValueError("rewrite snapshot 包含未接受的语义复核")
        # 使用经过共享校验且 usage 已复制冻结的新结果对象。
        records_by_case_id[record.case_id] = RewriteSnapshotRecord(
            record.case_id,
            record.question,
            validated_result,
            record.generated_at,
            review,
        )
        # 记录本题已被消费。
        seen_case_ids.add(record.case_id)
    # 正式快照必须覆盖全部案例，不能只挑表现好的题发布。
    if set(expected_questions) != seen_case_ids:
        # 缺任一题都会使质量分母和改写分母不一致。
        raise ValueError("rewrite snapshot 必须覆盖全部冻结案例")
    # 以 dataset 原顺序组织记录，使哈希和离线评测顺序稳定。
    ordered_records = tuple(records_by_case_id[case.case_id] for case in bundle.cases)
    # 计算确认对象的内容摘要，捕获逐题相关性确认和日期的漂移。
    confirmation_sha256 = _confirmation_sha256(confirmation)
    # 生成不含本机路径或密钥的快照身份输入。
    identity_payload = {
        "dataset_sha256": bundle.dataset_sha256,
        "manifest_sha256": bundle.manifest_sha256,
        "confirmation_sha256": confirmation_sha256,
        "records": [_record_payload(record) for record in ordered_records],
    }
    # 使用输入摘要前 16 位形成可读且安全的目录名。
    record_sha256 = hashlib.sha256(
        _canonical_json(identity_payload).encode("utf-8")
    ).hexdigest()
    # 返回完整不可变快照；同输入将得到同一身份，重复发布会 fail-closed。
    return VerifiedRewriteSnapshot(
        f"rewrite-v1-{record_sha256[:16]}",
        bundle.dataset_sha256,
        bundle.manifest_sha256,
        confirmation_sha256,
        ordered_records,
    )


# 只从已验证快照精确取回改写，供质量评测保持零联网。
class SnapshotQueryRewriter:
    """以原问题精确查找冻结改写结果的纯内存 QueryRewriter。"""

    # 建立原问题到已验证结果的一对一索引。
    def __init__(self, snapshot: VerifiedRewriteSnapshot) -> None:
        # 重复问题会让精确查找不唯一，不能静默覆盖。
        questions = [record.question for record in snapshot.records]
        if len(questions) != len(set(questions)):
            # 快照构建器正常不会产生此状态，构造器仍保持防御性边界。
            raise ValueError("rewrite snapshot 包含重复原问题")
        # 保存只读语义的内部字典，不暴露给调用方。
        self._results_by_question = {
            record.question: record.result for record in snapshot.records
        }

    # 返回与真实 API rewriter 完全相同的结果对象。
    def rewrite(self, question: str) -> QueryRewriteResult:
        """仅接受与快照逐字符一致的原问题。"""

        # 字典查找避免近似匹配或回退为原问题。
        result = self._results_by_question.get(question)
        if result is None:
            # 缺项时显式失败，证明质量评测没有偷偷联网补全。
            raise QueryRewriteError("Query 改写快照缺少该原问题")
        # 返回构建阶段已验证且 usage 只读的原结果对象。
        return result


# 将完整快照原子发布到不可覆盖的正式目录。
def publish_verified_rewrite_snapshot(
    rewrites_dir: Path,
    snapshot: VerifiedRewriteSnapshot,
) -> Path:
    """写入 details.json 后整体替换目录，成功后拒绝同 ID 覆盖。"""

    # 确保用户指定的证据父目录存在。
    rewrites_dir.mkdir(parents=True, exist_ok=True)
    # 正式目录名只取安全的内部生成 snapshot ID。
    final_directory = rewrites_dir / snapshot.rewrite_snapshot_id
    # 历史证据已经存在时绝不覆盖或合并。
    if final_directory.exists():
        # 用户必须通过不同冻结输入生成新快照身份。
        raise FileExistsError("rewrite_snapshot_id 已存在，不能覆盖正式快照")
    # 在同一父目录创建 staging，保证 os.replace 不跨卷。
    staging_directory = Path(
        tempfile.mkdtemp(prefix=f".{snapshot.rewrite_snapshot_id}.staging-", dir=rewrites_dir)
    )
    # 跟踪 staging 是否已经成为正式目录。
    published = False
    try:
        # 组织可直接审阅且不含任何密钥的完整 JSON。
        details = {
            "schema_version": REWRITE_SNAPSHOT_SCHEMA_VERSION,
            "rewrite_snapshot_id": snapshot.rewrite_snapshot_id,
            "dataset_sha256": snapshot.dataset_sha256,
            "manifest_sha256": snapshot.manifest_sha256,
            "confirmation_sha256": snapshot.confirmation_sha256,
            "records": [_record_payload(record) for record in snapshot.records],
        }
        # 先完整写入 staging 文件，正式目录此时还不存在。
        (staging_directory / "details.json").write_text(
            json.dumps(details, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        # 重新读取以验证序列化后的身份未漂移。
        written = json.loads((staging_directory / "details.json").read_text(encoding="utf-8"))
        if written.get("rewrite_snapshot_id") != snapshot.rewrite_snapshot_id:
            # 不发布内部身份不一致的半可信目录。
            raise ValueError("rewrite snapshot staging 身份不一致")
        # 同卷整体替换让消费者只能看到完整目录。
        os.replace(staging_directory, final_directory)
        # 标记成功，finally 不会删除正式证据。
        published = True
    finally:
        # 只清理由本函数创建且尚未发布的 staging 目录。
        if not published and staging_directory.exists():
            # 该目录来自 mkdtemp，不会误删用户已有证据。
            shutil.rmtree(staging_directory)
    # 返回最终目录，供后续 CLI 只打印项目相对路径。
    return final_directory


# 从已经发布的 JSON 重建记录，并再次校验其冻结输入身份。
def load_verified_rewrite_snapshot(
    snapshot_directory: Path,
    bundle: EvaluationBundle,
    confirmation: AnnotationConfirmation,
) -> VerifiedRewriteSnapshot:
    """只加载与当前 bundle 和 confirmation 完全一致的正式快照。"""

    # 目录必须存在且只读取固定文件名，调用方不能传入任意 JSON 文件。
    details_path = snapshot_directory / "details.json"
    if not details_path.is_file():
        # 缺少明细说明目录不是一个可消费的正式快照。
        raise ValueError("rewrite snapshot 缺少 details.json")
    # 损坏 JSON 不能被当成空记录或部分证据继续使用。
    try:
        details = json.loads(details_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        # 文件系统和 JSON 错误都不回显文件内容。
        raise ValueError("rewrite snapshot details.json 无法读取") from error
    # 顶层 schema 必须精确匹配当前实现。
    if not isinstance(details, dict) or details.get("schema_version") != REWRITE_SNAPSHOT_SCHEMA_VERSION:
        # 未知 schema 必须走显式迁移，不能猜字段。
        raise ValueError("rewrite snapshot schema_version 不受支持")
    # 快照必须自报当前 bundle 的两份原始文件 hash。
    if (
        details.get("dataset_sha256") != bundle.dataset_sha256
        or details.get("manifest_sha256") != bundle.manifest_sha256
    ):
        # 防止把旧数据集的改写混入当前质量评测。
        raise ValueError("rewrite snapshot 冻结输入不一致")
    # confirmation 内容摘要由当前已验证对象重新计算，不信任文件自报。
    if details.get("confirmation_sha256") != _confirmation_sha256(confirmation):
        # 人工确认漂移时必须重新生成或重新审核快照。
        raise ValueError("rewrite snapshot confirmation 不一致")
    # records 必须是列表，后续逐项构造成强类型记录。
    raw_records = details.get("records")
    if not isinstance(raw_records, list):
        # 不接受对象、字符串或缺失记录。
        raise ValueError("rewrite snapshot records 必须是列表")
    # 收集已从 JSON 严格解析的记录。
    records: list[RewriteSnapshotRecord] = []
    for raw_record in raw_records:
        # 每条 record 都必须是对象。
        if not isinstance(raw_record, dict):
            # 不让部分数组项被忽略。
            raise ValueError("rewrite snapshot record 结构不正确")
        # 读取各个字符串字段。
        case_id = raw_record.get("case_id")
        question = raw_record.get("question")
        rewritten_query = raw_record.get("rewritten_query")
        model = raw_record.get("model")
        generated_at = raw_record.get("generated_at")
        # 记录的基本文本字段都必须是非空字符串。
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (case_id, question, rewritten_query, model, generated_at)
        ):
            # 不进行 str 转换，避免 JSON 类型漂移被掩盖。
            raise ValueError("rewrite snapshot record 文本字段不合法")
        # usage 可为空；存在时只接受字符串到非负整数的 JSON 映射。
        usage = raw_record.get("usage")
        if usage is not None and not isinstance(usage, dict):
            # 不让任意嵌套 JSON 进入成本证据。
            raise ValueError("rewrite snapshot usage 结构不正确")
        # semantic_review 必须是对象，并保留给构建器验证 accepted gate。
        raw_review = raw_record.get("semantic_review")
        if not isinstance(raw_review, dict):
            # 复核缺失时不能把记录变成默认 accepted。
            raise ValueError("rewrite snapshot semantic_review 结构不正确")
        # 读取人工复核三个受控字段。
        review = SemanticReview(
            raw_review.get("status"),
            raw_review.get("reason"),
            raw_review.get("reviewed_at"),
        )
        # 构造结果后由 build 函数统一验证 usage、文本和模型。
        result = QueryRewriteResult(rewritten_query, model, usage)
        # 保存仍需经 build 再次验证的强类型记录。
        records.append(RewriteSnapshotRecord(case_id, question, result, generated_at, review))
    # 重新走完整构建器，校验 case 集合、语义复核和 result 契约。
    snapshot = build_verified_rewrite_snapshot(bundle, confirmation, records)
    # details 的 ID 与目录名都必须等于重新计算出的可信身份。
    if (
        details.get("rewrite_snapshot_id") != snapshot.rewrite_snapshot_id
        or snapshot_directory.name != snapshot.rewrite_snapshot_id
    ):
        # 目录重命名或记录篡改都不能逃过该检查。
        raise ValueError("rewrite snapshot 身份不一致")
    # 返回重建且已验证的快照，供 SnapshotQueryRewriter 零联网消费。
    return snapshot
