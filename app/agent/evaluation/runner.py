"""M3.7 评测 runner：消费脱敏 trace，不偷偷调用真实模型。"""

# 导入 time，仅在真实执行器自己计时时使用；合成路径由调用方提供 latency。
from collections.abc import Callable, Sequence
from dataclasses import asdict
from typing import Any

# 导入判分与 hash。
from app.agent.evaluation.grader import grade_task_result
from app.agent.evaluation.manifest import hash_question
from app.agent.evaluation.types import (
    AgentEvaluationManifest,
    AgentTaskCase,
    AgentTaskDetail,
)


# 单次执行器输入/输出约定：接收 task，返回脱敏运行观察值。
AgentTaskExecutor = Callable[[AgentTaskCase, int], dict[str, Any]]


def _require_executor_payload(payload: dict[str, Any], task: AgentTaskCase) -> dict[str, Any]:
    """校验执行器返回的白名单字段，拒绝敏感键。"""

    forbidden = {"api_key", "prompt", "messages", "secret", "authorization", "checkpoint", "raw_state"}
    hit = forbidden.intersection(payload)
    if hit:
        raise ValueError(f"{task.task_id} 执行结果包含敏感字段: {sorted(hit)}")
    required = {
        "terminal_status",
        "answer_text",
        "tool_call_count",
        "tool_success_count",
        "approval_request_count",
        "approval_resume_success_count",
        "step_count",
        "full_answer_latency_ms",
    }
    missing = required.difference(payload)
    if missing:
        raise ValueError(f"{task.task_id} 执行结果缺少字段: {sorted(missing)}")
    return payload


def run_agent_evaluation(
    manifest: AgentEvaluationManifest,
    tasks: Sequence[AgentTaskCase],
    executor: AgentTaskExecutor,
    *,
    require_owner_confirmation: bool = True,
) -> list[AgentTaskDetail]:
    """按冻结顺序与重复次数运行任务，返回脱敏 details。

    为什么 require_owner_confirmation：
    真实模型运行必须有 owner gate；合成 fixture 测试可显式关闭。
    """

    if require_owner_confirmation and not manifest.owner_confirmed:
        raise ValueError("manifest 未 owner 确认，禁止正式运行")

    by_id = {task.task_id: task for task in tasks}
    details: list[AgentTaskDetail] = []
    for task_id in manifest.run_order:
        task = by_id[task_id]
        for repetition in range(1, manifest.repetitions + 1):
            payload = _require_executor_payload(executor(task, repetition), task)
            success, evidence = grade_task_result(
                task,
                terminal_status=str(payload["terminal_status"]),
                answer_text=payload.get("answer_text"),
                tool_call_count=int(payload["tool_call_count"]),
                tool_success_count=int(payload["tool_success_count"]),
                approval_request_count=int(payload["approval_request_count"]),
                approval_resume_success_count=int(payload["approval_resume_success_count"]),
            )
            details.append(
                AgentTaskDetail(
                    task_id=task.task_id,
                    layer=task.layer,
                    input_hash=hash_question(task.question),
                    repetition=repetition,
                    terminal_status=str(payload["terminal_status"]),
                    task_success=success,
                    tool_call_count=int(payload["tool_call_count"]),
                    tool_success_count=int(payload["tool_success_count"]),
                    approval_request_count=int(payload["approval_request_count"]),
                    approval_resume_success_count=int(payload["approval_resume_success_count"]),
                    step_count=int(payload["step_count"]),
                    full_answer_latency_ms=float(payload["full_answer_latency_ms"]),
                    model_id=manifest.model_id,
                    tool_version=manifest.tool_version,
                    corpus_version=manifest.corpus_version,
                    grade_evidence=evidence,
                    side_effect_before_approval=int(payload.get("side_effect_before_approval", 0)),
                    duplicate_writes=int(payload.get("duplicate_writes", 0)),
                    illegal_tool_leaks=int(payload.get("illegal_tool_leaks", 0)),
                    unresolved_unknown_outcomes=int(payload.get("unresolved_unknown_outcomes", 0)),
                    usage=str(payload.get("usage", "not_available")),
                    cost=str(payload.get("cost", "not_available")),
                )
            )
    return details


def details_to_jsonable(details: Sequence[AgentTaskDetail]) -> list[dict[str, Any]]:
    """把 details 转成可 JSON 序列化的字典列表。"""

    return [asdict(item) for item in details]
