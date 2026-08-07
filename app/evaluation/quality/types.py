"""M5.1 质量评测值对象：只保存脱敏、可重算字段。"""

# 导入 dataclass，让所有输入/输出对象在聚合过程中不可被就地改写。
from dataclasses import dataclass


class QualityMethod:
    """固定回答生成方法名，避免 dense/agent 拼写漂移。"""

    # 固定 dense RAG 回答；与 Agent 侧使用同一 batch_id+task_id+repetition 配对。
    dense = "dense"
    # Agent 回答；使用 M3.6/M3.7 runner 产生的脱敏投影。
    agent = "agent"


class QualityLayer:
    """回答分层；shared 与 agent-only 使用独立分母。"""

    # 固定 RAG 与 Agent 都能回答的共同知识题。
    shared = "shared"
    # 需要工具循环或人工审批的 Agent 专属题。
    agent_only = "agent-only"


class ClaimExclusionReason:
    """声明不可纳入引用覆盖率的固定排除原因。"""

    # 纯礼貌语，例如“祝您健康”。
    polite_closing = "polite_closing"
    # 免责声明，例如“本回答不构成医疗建议”。
    disclaimer = "disclaimer"
    # 只复述用户问题，不提供新事实。
    question_restatement = "question_restatement"
    # 无法单独核验的格式文本或元话语。
    unverifiable_format = "unverifiable_format"
    # 缺少 claim_hash，无法把声明与证据稳定绑定。
    missing_claim_hash = "missing_claim_hash"
    # judge 结果缺失，无法机械判断声明是否 eligible。
    missing_judge = "missing_judge"


class FactualityStatus:
    """事实性/幻觉风险冻结状态集合。"""

    # 人工或自动判为可被参考证据支持。
    pass_ = "pass"
    # 判为与参考证据冲突或不可支持。
    fail = "fail"
    # 证据不足、冲突未裁决或扫描失败。
    hold = "hold"
    # 只有自动结果、没有人工复核时的状态；永远不能直接视为 pass。
    provisional = "provisional"
    # judge 或评分完全不可用。
    not_available = "not_available"


def case_key_for(batch_id: str, task_id: str, repetition: int) -> str:
    """构造 dense/agent 可配对的稳定案例键。"""

    # 跨方法只共享 batch_id+task_id+repetition，不把方法名放进 case key。
    return f"{batch_id}|{task_id}|{repetition}"


def quality_claim_key(
    batch_id: str,
    method: str,
    task_id: str,
    repetition: int,
    claim_id: str,
) -> str:
    """构造声明级去重键：batch_id+method+task_id+repetition+claim_id。"""

    return f"{batch_id}|{method}|{task_id}|{repetition}|{claim_id}"


@dataclass(frozen=True)
class QualityMethodIdentity:
    """一个 dense 或 agent 运行的身份与冻结输入指纹。"""

    # dense 或 agent。
    method: str
    # 该方法独立 run_id；同 batch 的 dense/agent run_id 可以不同。
    run_id: str
    # 回答模型身份；synthetic 路径可写 synthetic。
    model_id: str
    # 工具版本；dense 侧可写 not_available。
    tool_version: str
    # 语料/知识库快照标签。
    corpus_version: str
    # 产生该投影的 M3/M2 source manifest 内容 hash。
    source_manifest_sha256: str
    # 该投影绑定的参考证据 manifest 内容 hash。
    reference_manifest_sha256: str
    # quality_input_projection 文件相对项目根路径。
    projection_path: str
    # projection 原始文件字节 hash，防止静默改输入。
    projection_sha256: str
    # 该方法应覆盖的 task_id 集合。
    task_ids: tuple[str, ...]
    # 每题重复次数。
    repetitions: int


@dataclass(frozen=True)
class QualityManifest:
    """正式质量评测前必须冻结的 manifest。"""

    # 质量评测对象 schema 版本。
    schema_version: int
    # 显式运行模式：synthetic=仅工程验证，production=真实证据候选。
    run_mode: str
    # 人读 manifest 版本。
    manifest_version: str
    # 同批 dense/agent 配对批次。
    batch_id: str
    # M5.1 质量投影 schema 版本。
    quality_schema_version: str
    # grader/judge 提供方版本；synthetic 可写 synthetic-judge-v1。
    grader_provider_version: str
    # 评测集 provenance 内容 hash。
    dataset_provenance_sha256: str
    # 参考证据 provenance 内容 hash。
    reference_evidence_sha256: str
    # 人工复核证据内容 hash；无人工复核时使用空内容 hash。
    manual_review_sha256: str
    # owner 是否确认真实运行；当前 synthetic 路径必须为 false。
    owner_confirmed: bool
    # owner 授权引用；未确认时为空字符串。
    owner_confirmation_ref: str
    # dense 与 agent 各自的运行身份。
    methods: tuple[QualityMethodIdentity, ...]
    # 引用覆盖率通过阈值。
    citation_coverage_threshold: float
    # 有引用声明中支持率通过阈值。
    citation_support_threshold: float
    # 相关性均值通过阈值；量表固定 0/1/2。
    relevance_mean_threshold: float
    # 人工复核事实性通过率阈值。
    factuality_pass_rate_threshold: float


@dataclass(frozen=True)
class QualityProjectionRow:
    """一条声明级脱敏投影；禁止复制 query、完整回答或健康词。"""

    # 跨方法稳定案例键：batch_id|task_id|repetition。
    case_key: str
    # 批次号，与 manifest.batch_id 一致。
    batch_id: str
    # 该方法 run_id，与 manifest 中对应 method identity 一致。
    run_id: str
    # dense 或 agent。
    method: str
    # 稳定任务编号。
    task_id: str
    # 重复轮次，从 1 开始。
    repetition: int
    # shared 或 agent-only。
    layer: str
    # 输入问题 hash，不保存完整 prompt。
    input_hash: str
    # 回答 hash，不保存完整回答。
    answer_hash: str
    # 声明编号；仅在本方法 task+repetition 内唯一。
    claim_id: str
    # 声明内容 hash。
    claim_hash: str
    # 引用来源 ID；无引用时为 None。
    source_id: str | None
    # 事实性核验参考证据 ID；无参考时为 None。
    reference_id: str | None
    # 可选脱敏回答文本；默认不落盘，报告层永不写出该字段。
    answer_text_redacted: str | None = None


@dataclass(frozen=True)
class QualityProjection:
    """一份 projection 文件解析后的不可变对象。"""

    # projection schema 版本。
    schema_version: int
    # 投影 schema 人读版本。
    projection_schema_version: str
    # 批次号。
    batch_id: str
    # 投影行；每行对应一条声明。
    rows: tuple[QualityProjectionRow, ...]


@dataclass(frozen=True)
class QualityJudgeResult:
    """一个可选 judge adapter 的单声明结果。"""

    # 声明级去重键。
    claim_key: str
    # 声明是否含可核验事实/行动断言且有 claim_hash。
    claim_eligible: bool
    # eligible=false 时必填的固定排除原因。
    claim_exclusion_reason: str | None
    # 引用是否被 judge 判为支持。
    citation_supported: bool
    # 自动相关性评分；缺 judge 或无法判断时为 None。
    automatic_relevance_score: int | None
    # 自动事实性状态：pass/fail/hold/not_available。
    automatic_factuality_status: str
    # grader 提供方版本。
    provider_version: str
    # 判分证据引用；只放证据 ID，不放正文。
    evidence_ref: str


@dataclass(frozen=True)
class RelevanceReview:
    """回答相关性的人工评分：按 case 记录，0/1/2 量表。"""

    # 案例键：batch_id|task_id|repetition。
    case_key: str
    # 0=不相关，1=部分相关，2=直接相关。
    relevance_score: int
    # 人工 reviewer 身份。
    reviewer_id: str
    # 固定评分理由代码。
    rationale_code: str
    # 人工评分的证据引用。
    evidence_ref: str
    # owner 授权引用，证明该评分有权进入报告。
    authorized_by_ref: str


@dataclass(frozen=True)
class FactualityReview:
    """逐声明人工事实性复核。"""

    # 声明级去重键。
    claim_key: str
    # pass/fail/hold。
    review_decision: str
    # reviewer 身份。
    reviewer_id: str
    # 证据引用，不放正文。
    evidence_ref: str
    # ISO 8601 复核时间。
    reviewed_at: str
    # owner 授权引用。
    authorized_by_ref: str


@dataclass(frozen=True)
class FactualityAdjudication:
    """人工复核冲突的第二 reviewer 裁决。"""

    # 声明级去重键。
    claim_key: str
    # 最终 pass/fail/hold。
    final_decision: str
    # 裁决人身份。
    adjudicator_id: str
    # 裁决证据引用。
    evidence_ref: str
    # ISO 8601 裁决时间。
    reviewed_at: str
    # owner 授权引用。
    authorized_by_ref: str


@dataclass(frozen=True)
class QualityClaimDetail:
    """一条质量 details；报告所有汇总必须能由该列表重算。"""

    # 跨方法案例键。
    case_key: str
    # 批次号。
    batch_id: str
    # 该方法 run_id。
    run_id: str
    # 任务编号。
    task_id: str
    # shared 或 agent-only。
    layer: str
    # dense 或 agent。
    method: str
    # 重复轮次。
    repetition: int
    # 声明编号。
    claim_id: str
    # 声明 hash。
    claim_hash: str
    # 是否进入 citation coverage 分母。
    claim_eligible: bool
    # eligible=false 时的排除原因。
    claim_exclusion_reason: str | None
    # 是否进入 factuality 分母：claim_eligible 且具备 reference evidence。
    factuality_eligible: bool
    # judge 是否对该声明返回了可用结果。
    judge_available: bool
    # 引用来源 ID。
    source_id: str | None
    # 参考证据 ID。
    reference_id: str | None
    # 是否有引用。
    citation_present: bool
    # 引用是否支持；无引用时为 None。
    citation_supported: bool | None
    # 自动相关性评分。
    automatic_relevance_score: int | None
    # 最终相关性评分：人工优先，否则自动。
    answer_relevance_score: int | None
    # 人工相关性 reviewer。
    relevance_reviewer_id: str | None
    # 人工相关性理由代码。
    relevance_rationale_code: str | None
    # 人工相关性证据引用。
    relevance_evidence_ref: str | None
    # 自动事实性状态。
    automatic_factuality_status: str | None
    # 最终事实性状态。
    factuality_status: str
    # 人工事实性 reviewer。
    factuality_reviewer_id: str | None
    # 人工事实性证据引用。
    factuality_evidence_ref: str | None
    # 人工复核时间。
    factuality_reviewed_at: str | None
    # resolved/unresolved/None。
    factuality_adjudication_status: str | None
    # 模型身份。
    model_id: str
    # 工具版本。
    tool_version: str
    # 语料版本。
    corpus_version: str
    # source manifest hash。
    source_manifest_sha256: str
    # reference manifest hash。
    reference_manifest_sha256: str
    # projection 文件 hash。
    projection_sha256: str
    # grader 提供方版本。
    grader_provider_version: str
