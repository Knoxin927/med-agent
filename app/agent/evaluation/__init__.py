"""M3.7 Agent 任务评测包：只负责证据重算，不进入 graph 决策。"""

# 公开最常用入口，方便测试与 CLI 统一导入。
from app.agent.evaluation.aggregate import aggregate_agent_details
from app.agent.evaluation.decision import build_decision_record, decide_pass_hold
from app.agent.evaluation.grader import grade_task_result
from app.agent.evaluation.manifest import (
    is_synthetic_manifest,
    load_agent_manifest,
    load_agent_tasks,
    validate_agent_manifest,
)
from app.agent.evaluation.reporting import (
    build_markdown_summary,
    publish_agent_run_report,
    recompute_report_from_details,
)
from app.agent.evaluation.runner import run_agent_evaluation
from app.agent.evaluation.types import (
    AgentEvaluationManifest,
    AgentTaskCase,
    AgentTaskDetail,
    AgentTaskLayer,
)

__all__ = [
    "AgentEvaluationManifest",
    "AgentTaskCase",
    "AgentTaskDetail",
    "AgentTaskLayer",
    "aggregate_agent_details",
    "build_decision_record",
    "build_markdown_summary",
    "decide_pass_hold",
    "grade_task_result",
    "is_synthetic_manifest",
    "load_agent_manifest",
    "load_agent_tasks",
    "publish_agent_run_report",
    "recompute_report_from_details",
    "run_agent_evaluation",
    "validate_agent_manifest",
]
