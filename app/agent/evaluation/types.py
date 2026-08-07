"""M3.7 Agent 评测值对象：只保存脱敏、可重算字段。"""

# 导入 dataclass，保证任务与结果在聚合过程中不被就地改写。
from dataclasses import dataclass
# 导入 Any，承载 JSON 基础类型的判分证据。
from typing import Any


# 任务分层：shared 与 agent-only 必须使用独立分母。
class AgentTaskLayer:
    """用常量表达任务分层，避免魔法字符串散落。"""

    # 固定 RAG 与 Agent 都能回答的共同知识题。
    shared = "shared"
    # 需要工具循环或人工审批的 Agent 专属题。
    agent_only = "agent-only"


@dataclass(frozen=True)
class AgentTaskCase:
    """一条冻结的非敏感 Agent 任务。"""

    # 跨版本稳定的任务编号。
    task_id: str
    # shared 或 agent-only。
    layer: str
    # 交给 Agent/API 的非敏感问题。
    question: str
    # 判分器类型：contains_all / terminal_status / tool_success。
    grader: str
    # 判分参数，例如必须出现的关键词列表。
    grader_params: dict[str, Any]
    # 期望终态：completed/failed/cancelled/running。
    expected_status: str
    # 是否期望至少一次成功工具调用。
    expect_tool_success: bool = False
    # 是否期望发生审批恢复。
    expect_approval_resume: bool = False
    # 可选人工备注，不参与自动聚合。
    notes: str = ""


@dataclass(frozen=True)
class AgentEvaluationManifest:
    """正式运行前必须冻结的评测配置。"""

    # schema 版本，便于未来显式迁移。
    schema_version: int
    # 人读 manifest 版本。
    manifest_version: str
    # 任务集相对路径。
    tasks_path: str
    # 任务集内容 hash，防止静默改题。
    tasks_sha256: str
    # 模型身份；真实运行由用户填写，合成运行可写 synthetic。
    model_id: str
    # 工具注册表版本标签。
    tool_version: str
    # 语料/知识库快照标签。
    corpus_version: str
    # 检索 top_k。
    top_k: int
    # 温度；合成路径可为 0。
    temperature: float
    # 每题重复次数。
    repetitions: int
    # 固定运行顺序：task_id 列表。
    run_order: tuple[str, ...]
    # 完整回答延迟定义说明。
    latency_definition: str
    # 通过阈值：shared 任务成功率下限。
    shared_success_threshold: float
    # 通过阈值：agent-only 任务成功率下限。
    agent_only_success_threshold: float
    # 通过阈值：工具成功率下限。
    tool_success_threshold: float
    # 通过阈值：审批恢复成功率下限；无审批题时不生效。
    approval_resume_threshold: float
    # 是否已由 owner 确认可真实运行。
    owner_confirmed: bool
    # owner 确认引用，未确认时为空。
    owner_confirmation_ref: str = ""
    # 无法获得的字段统一标记。
    unavailable_fields: tuple[str, ...] = ("usage", "cost")


@dataclass(frozen=True)
class AgentTaskDetail:
    """一题一次运行的脱敏轨迹与结果。"""

    # 任务编号。
    task_id: str
    # 分层。
    layer: str
    # 输入问题 hash，避免把完整 prompt 写入报告时可追溯。
    input_hash: str
    # 重复轮次，从 1 开始。
    repetition: int
    # 终态：completed/failed/cancelled/running。
    terminal_status: str
    # 是否判为任务成功。
    task_success: bool
    # 工具调用总次数。
    tool_call_count: int
    # 工具成功次数。
    tool_success_count: int
    # 审批请求次数。
    approval_request_count: int
    # 审批成功恢复次数。
    approval_resume_success_count: int
    # graph step 数。
    step_count: int
    # 完整回答端到端延迟毫秒。
    full_answer_latency_ms: float
    # 模型身份。
    model_id: str
    # 工具版本。
    tool_version: str
    # 语料版本。
    corpus_version: str
    # 判分证据，仅白名单字段。
    grade_evidence: dict[str, Any]
    # 安全计数：批准前副作用。
    side_effect_before_approval: int = 0
    # 安全计数：重复写入。
    duplicate_writes: int = 0
    # 安全计数：非法工具/参数漏放。
    illegal_tool_leaks: int = 0
    # 安全计数：未收敛 unknown outcome。
    unresolved_unknown_outcomes: int = 0
    # usage/cost 默认不可得。
    usage: str = "not_available"
    cost: str = "not_available"
