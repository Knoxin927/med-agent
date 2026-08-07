"""定义不依赖模型、Chroma 或文件格式的评测值对象。"""

# 导入 dataclass，用简洁语法定义创建后不可变的数据对象。
from dataclasses import dataclass

# 导入 M1.1 的 TextChunk，复用已经稳定的文本块身份和原文结构。
from app.rag.chunking import TextChunk


# frozen=True 防止评测过程中意外修改相关块身份。
@dataclass(frozen=True)
class ChunkIdentity:
    """使用来源文件名和块序号唯一定位固定语料快照中的文本块。"""

    # 保存脱敏后的纯文件名，不允许保存绝对路径。
    source_name: str
    # 保存从零开始的文本块序号。
    chunk_index: int


# frozen=True 保证一轮评测中问题和人工标注不会被算法修改。
@dataclass(frozen=True)
class EvaluationCase:
    """保存一条评测问题、相关块和预先冻结的分层。"""

    # 保存跨版本可读的唯一案例编号。
    case_id: str
    # 保存实际交给检索策略的问题。
    question: str
    # 保存用户人工核对后认为相关的全部文本块身份。
    relevant: tuple[ChunkIdentity, ...]
    # 保存四种互斥主分层之一。
    primary_stratum: str
    # 保存受控的库内或库外标签。
    tags: tuple[str, ...]


# frozen=True 让 manifest 加载后成为本轮不可变配置。
@dataclass(frozen=True)
class CorpusFileRecord:
    """描述一个可提交语料文件及其来源、许可和内容 hash。"""

    # 保存相对于 corpus_root 的安全路径。
    path: str
    # 保存文件原始字节的 SHA-256 十六进制值。
    sha256: str
    # 保存可供人工复核的来源说明。
    source: str
    # 保存该文本允许用于项目评测的许可说明。
    license: str


# frozen=True 固定语料、切片和 dataset 的共同版本入口。
@dataclass(frozen=True)
class EvaluationManifest:
    """保存一轮评测输入必须一致的语料与切片配置。"""

    # 保存 manifest schema 版本，便于未来显式迁移。
    schema_version: int
    # 保存语料快照的人读版本。
    corpus_version: str
    # 保存相对于项目根目录的语料目录。
    corpus_root: str
    # 保存每个语料文件的 hash、来源和许可。
    files: tuple[CorpusFileRecord, ...]
    # 保存 M1.1 固定字符切片大小。
    chunk_size: int
    # 保存相邻文本块重叠字符数。
    overlap: int
    # 保存评测问题集版本。
    dataset_version: str
    # 保存相对于项目根目录的 JSONL 路径。
    dataset_path: str
    # 保存 JSONL 原始字节的 SHA-256。
    dataset_sha256: str
    # 保存 manifest 创建日期文本。
    created: str


# frozen=True 表示加载完成的输入包不会在运行中更换成员。
@dataclass(frozen=True)
class EvaluationBundle:
    """把已验证 manifest、文本块、问题和输入 hash 组合起来。"""

    # 保存已经完成字段校验的 manifest。
    manifest: EvaluationManifest
    # 保存按 manifest 固定参数重建的全部文本块。
    chunks: tuple[TextChunk, ...]
    # 保存按 JSONL 顺序加载的全部评测问题。
    cases: tuple[EvaluationCase, ...]
    # 保存 manifest 文件本身的 SHA-256，供人工确认绑定。
    manifest_sha256: str
    # 保存 dataset 文件本身的 SHA-256，供报告追溯。
    dataset_sha256: str


# frozen=True 防止校验通过后修改单题人工确认内容。
@dataclass(frozen=True)
class ConfirmedCase:
    """保存一条与 dataset 精确对应的人工确认记录。"""

    # 保存被确认的案例编号。
    case_id: str
    # 保存用户逐块核对过的相关身份。
    relevant: tuple[ChunkIdentity, ...]
    # 只接受真实布尔值 True，不接受 1 或非空字符串。
    confirmed: bool


# frozen=True 把整份人工确认锁定到三个输入 hash。
@dataclass(frozen=True)
class AnnotationConfirmation:
    """表示用户已经核对指定 manifest、dataset 和审阅文件。"""

    # 保存 confirmation schema 版本。
    schema_version: int
    # 保存被确认的 dataset 人读版本。
    dataset_version: str
    # 绑定问题文件的原始字节 hash。
    dataset_sha256: str
    # 绑定 manifest 文件的原始字节 hash。
    corpus_manifest_sha256: str
    # 绑定用户实际阅读的 Markdown 原始字节 hash。
    annotation_review_sha256: str
    # 保存用户明确确认的日期或时间文本。
    confirmed_at: str
    # 保存覆盖全部 dataset 案例的逐题确认记录。
    cases: tuple[ConfirmedCase, ...]


# frozen=True 防止聚合阶段修改已经计算完成的单题结果。
@dataclass(frozen=True)
class CaseMetrics:
    """保存一题在固定 Top-10 排名上的可解释指标。"""

    # 保存对应案例编号。
    case_id: str
    # 保存主分层，供多线索和失败案例统计。
    primary_stratum: str
    # 库内题保存 Recall@5；库外题为 None。
    recall_at_5: float | None
    # 库内题保存 Recall@10；库外题为 None。
    recall_at_10: float | None
    # 库内题保存 MRR@10；库外题为 None。
    mrr_at_10: float | None
    # 多线索题保存是否在 Top-5 找全；其他题为 None。
    all_relevant_hit_at_5: bool | None
    # 多线索题保存是否在 Top-10 找全；其他题为 None。
    all_relevant_hit_at_10: bool | None
    # 保存 Top-10 中实际命中的相关身份。
    hit_identities: tuple[ChunkIdentity, ...]


# frozen=True 防止生成报告后被调用方改写汇总指标。
@dataclass(frozen=True)
class MetricsSummary:
    """保存所有库内案例的宏平均与固定失败案例定义。"""

    # 保存参与 Recall/MRR 分母的库内案例数。
    in_domain_case_count: int
    # 保存只观察误召回、不进入质量分母的库外案例数。
    out_of_domain_case_count: int
    # 保存库内案例 Recall@5 的宏平均。
    recall_at_5: float
    # 保存库内案例 Recall@10 的宏平均。
    recall_at_10: float
    # 保存库内案例 MRR@10 的宏平均。
    mrr_at_10: float
    # 保存多线索案例中全部相关块进入 Top-5 的比例。
    all_relevant_hit_at_5: float | None
    # 保存多线索案例中全部相关块进入 Top-10 的比例。
    all_relevant_hit_at_10: float | None
    # 保存库内且 Recall@10 小于 1 的固定失败案例编号。
    failed_case_ids: tuple[str, ...]
    # 保存 Top-10 没有命中任何相关块的案例编号。
    no_hit_case_ids: tuple[str, ...]
