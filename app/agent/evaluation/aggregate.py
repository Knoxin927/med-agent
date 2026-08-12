"""M3.7 分层聚合：shared 与 agent-only 分母绝不混用。"""

# 导入 math，保证延迟样本有限。
import math
# 导入 Any，输出 JSON 友好字典。
from typing import Any

# 复用 M2 已验证的线性百分位公式，保证 P50/P95 口径一致。
from app.evaluation.metrics import linear_percentile
# 导入 details 与分层常量。
from app.agent.evaluation.types import AgentTaskDetail, AgentTaskLayer


def _rate(numerator: int, denominator: int) -> float | None:
    """计算成功率；分母为 0 时返回 None，而不是伪造 0。"""

    if denominator <= 0:
        return None
    return numerator / denominator


def _layer_metrics(details: list[AgentTaskDetail]) -> dict[str, Any]:
    """对单一分层计算任务成功率、步数与完整回答延迟。"""

    total = len(details)
    success = sum(1 for item in details if item.task_success)
    latencies = [item.full_answer_latency_ms for item in details if item.task_success]
    steps = [item.step_count for item in details]
    return {
        "task_count": total,
        "success_count": success,
        "task_success_rate": _rate(success, total),
        "step_count_sum": sum(steps) if steps else 0,
        "step_count_mean": (sum(steps) / len(steps)) if steps else None,
        "full_answer_latency_p50_ms": linear_percentile(latencies, 0.50) if latencies else None,
        "full_answer_latency_p95_ms": linear_percentile(latencies, 0.95) if latencies else None,
        "latency_sample_count": len(latencies),
    }


def aggregate_agent_details(details: list[AgentTaskDetail]) -> dict[str, Any]:
    """从逐题 details 重算 shared/agent-only/安全门指标。"""

    if not details:
        raise ValueError("details 不能为空")
    for item in details:
        if (
            isinstance(item.full_answer_latency_ms, bool)
            or not isinstance(item.full_answer_latency_ms, (int, float))
            or not math.isfinite(float(item.full_answer_latency_ms))
            or float(item.full_answer_latency_ms) < 0
        ):
            raise ValueError(f"{item.task_id} 的 latency 必须是非负有限数值")
        if item.layer not in {AgentTaskLayer.shared, AgentTaskLayer.agent_only}:
            raise ValueError(f"{item.task_id} 的 layer 不合法")

    shared = [item for item in details if item.layer == AgentTaskLayer.shared]
    agent_only = [item for item in details if item.layer == AgentTaskLayer.agent_only]

    tool_calls = sum(item.tool_call_count for item in agent_only)
    tool_successes = sum(item.tool_success_count for item in agent_only)
    approval_requests = sum(item.approval_request_count for item in agent_only)
    approval_resumes = sum(item.approval_resume_success_count for item in agent_only)

    safety = {
        "side_effect_before_approval": sum(item.side_effect_before_approval for item in details),
        "duplicate_writes": sum(item.duplicate_writes for item in details),
        "illegal_tool_leaks": sum(item.illegal_tool_leaks for item in details),
        "unresolved_unknown_outcomes": sum(item.unresolved_unknown_outcomes for item in details),
    }
    failure_categories: dict[str, int] = {}
    for item in details:
        if item.task_success:
            continue
        reason = str(item.grade_evidence.get("reason", "unclassified"))
        failure_categories[reason] = failure_categories.get(reason, 0) + 1
    return {
        "shared": _layer_metrics(shared),
        "agent_only": {
            **_layer_metrics(agent_only),
            "tool_call_count": tool_calls,
            "tool_success_count": tool_successes,
            "tool_success_rate": _rate(tool_successes, tool_calls),
            "approval_request_count": approval_requests,
            "approval_resume_success_count": approval_resumes,
            "approval_resume_success_rate": _rate(approval_resumes, approval_requests),
        },
        "safety_gates": safety,
        "failure_categories": dict(sorted(failure_categories.items())),
        "total_detail_count": len(details),
    }
