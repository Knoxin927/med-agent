"""M3.7 逐题判分器：只根据脱敏结果与冻结规则判定。"""

# 导入 Any，承载答案与证据。
from typing import Any

# 导入任务定义。
from app.agent.evaluation.types import AgentTaskCase


def grade_task_result(
    task: AgentTaskCase,
    *,
    terminal_status: str,
    answer_text: str | None,
    tool_call_count: int,
    tool_success_count: int,
    approval_request_count: int,
    approval_resume_success_count: int,
) -> tuple[bool, dict[str, Any]]:
    """返回 (是否成功, 判分证据)。评测器不替用户做医学语义判断。"""

    evidence: dict[str, Any] = {
        "grader": task.grader,
        "expected_status": task.expected_status,
        "observed_status": terminal_status,
    }
    # 终态必须先匹配，再谈文本或工具。
    if terminal_status != task.expected_status:
        evidence["reason"] = "terminal_status_mismatch"
        return False, evidence
    if task.expect_tool_success and tool_success_count < 1:
        evidence["reason"] = "expected_tool_success_missing"
        return False, evidence
    if task.expect_approval_resume and approval_resume_success_count < 1:
        evidence["reason"] = "expected_approval_resume_missing"
        return False, evidence

    if task.grader == "terminal_status":
        evidence["reason"] = "terminal_status_matched"
        return True, evidence

    if task.grader == "tool_success":
        success = tool_success_count >= 1 and tool_call_count >= 1
        evidence["tool_call_count"] = tool_call_count
        evidence["tool_success_count"] = tool_success_count
        evidence["reason"] = "tool_success_matched" if success else "tool_success_missing"
        return success, evidence

    if task.grader == "contains_all":
        keywords = task.grader_params.get("keywords") or []
        text = answer_text or ""
        missing = [word for word in keywords if word not in text]
        evidence["keywords"] = list(keywords)
        evidence["missing_keywords"] = missing
        evidence["answer_present"] = bool(text.strip())
        if missing:
            evidence["reason"] = "keywords_missing"
            return False, evidence
        evidence["reason"] = "keywords_matched"
        return True, evidence

    evidence["reason"] = "unknown_grader"
    return False, evidence
