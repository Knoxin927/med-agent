"""M3.7 报告发布：details / summary / decision 必须可互相重算。"""

# 导入 hashlib/json，保证 details 指纹与 JSON 稳定写出。
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 导入聚合、决策与 runner 辅助。
from app.agent.evaluation.aggregate import aggregate_agent_details
from app.agent.evaluation.decision import build_decision_record, decide_pass_hold
from app.agent.evaluation.runner import details_to_jsonable
from app.agent.evaluation.types import AgentEvaluationManifest, AgentTaskDetail


def _stable_json_bytes(payload: object) -> bytes:
    """用固定分隔符编码 JSON，保证 hash 可复现。"""

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def details_sha256(details: list[AgentTaskDetail]) -> str:
    """对 details 内容计算 SHA-256。"""

    return hashlib.sha256(_stable_json_bytes(details_to_jsonable(details))).hexdigest()


def recompute_report_from_details(
    manifest: AgentEvaluationManifest,
    details: list[AgentTaskDetail],
    *,
    run_id: str,
) -> dict[str, Any]:
    """仅根据 details + manifest 重算 aggregate 与 decision。"""

    aggregate = aggregate_agent_details(details)
    decision = build_decision_record(
        manifest,
        aggregate,
        details_sha256=details_sha256(details),
        run_id=run_id,
    )
    return {
        "run_id": run_id,
        "aggregate": aggregate,
        "decision": decision,
        "verdict": decide_pass_hold(manifest, aggregate),
    }


def build_markdown_summary(
    manifest: AgentEvaluationManifest,
    details: list[AgentTaskDetail],
    *,
    run_id: str,
) -> str:
    """生成人读 Markdown summary；所有数字来自重算结果。"""

    recomputed = recompute_report_from_details(manifest, details, run_id=run_id)
    aggregate = recomputed["aggregate"]
    verdict = recomputed["verdict"]
    shared = aggregate["shared"]
    agent_only = aggregate["agent_only"]
    safety = aggregate["safety_gates"]

    def fmt_rate(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.4f}"

    def fmt_ms(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2f}"

    lines = [
        f"# M3.7 Agent 任务评测报告（{run_id}）",
        "",
        "## 元信息",
        f"- manifest_version: `{manifest.manifest_version}`",
        f"- model_id: `{manifest.model_id}`",
        f"- tool_version: `{manifest.tool_version}`",
        f"- corpus_version: `{manifest.corpus_version}`",
        f"- top_k: `{manifest.top_k}`",
        f"- repetitions: `{manifest.repetitions}`",
        f"- latency_definition: {manifest.latency_definition}",
        f"- owner_confirmed: `{manifest.owner_confirmed}`",
        f"- decision: **{verdict['decision']}**",
    ]
    if verdict["decision"] == "synthetic_only" or not manifest.owner_confirmed:
        lines.extend(
            [
                "",
                "> **非生产指标**：本报告 owner 未确认或仅为合成工程证据，禁止当作真实 Agent 质量 pass。",
            ]
        )
    lines.extend(
        [
            "",
            "## shared（只比任务判定与完整回答延迟）",
            f"- task_count / success_count: {shared['task_count']} / {shared['success_count']}",
            f"- task_success_rate: {fmt_rate(shared['task_success_rate'])}",
            f"- full_answer_latency_p50_ms: {fmt_ms(shared['full_answer_latency_p50_ms'])}",
            f"- full_answer_latency_p95_ms: {fmt_ms(shared['full_answer_latency_p95_ms'])}",
            "",
            "## agent-only",
            f"- task_count / success_count: {agent_only['task_count']} / {agent_only['success_count']}",
            f"- task_success_rate: {fmt_rate(agent_only['task_success_rate'])}",
            f"- tool_success_rate: {fmt_rate(agent_only['tool_success_rate'])} ({agent_only['tool_success_count']}/{agent_only['tool_call_count']})",
            f"- approval_resume_success_rate: {fmt_rate(agent_only['approval_resume_success_rate'])} ({agent_only['approval_resume_success_count']}/{agent_only['approval_request_count']})",
            f"- step_count_mean: {fmt_ms(agent_only['step_count_mean'])}",
            f"- full_answer_latency_p50_ms: {fmt_ms(agent_only['full_answer_latency_p50_ms'])}",
            f"- full_answer_latency_p95_ms: {fmt_ms(agent_only['full_answer_latency_p95_ms'])}",
            "",
            "## 安全门（必须全 0 才可能 pass）",
            f"- side_effect_before_approval: {safety['side_effect_before_approval']}",
            f"- duplicate_writes: {safety['duplicate_writes']}",
            f"- illegal_tool_leaks: {safety['illegal_tool_leaks']}",
            f"- unresolved_unknown_outcomes: {safety['unresolved_unknown_outcomes']}",
            "",
            "## 失败分类",
        ]
    )
    if aggregate["failure_categories"]:
        lines.extend(
            f"- `{reason}`: {count}"
            for reason, count in aggregate["failure_categories"].items()
        )
    else:
        lines.append("- 无任务失败。")
    lines.extend(
        [
            "",
            "## 决策理由",
        ]
    )
    if verdict["reasons"]:
        lines.extend(f"- {reason}" for reason in verdict["reasons"])
    else:
        lines.append("- 全部阈值与安全门通过。")
    lines.extend(
        [
            "",
            "## 不可用字段",
            f"- {', '.join(manifest.unavailable_fields)} = `not_available`",
            "",
            "> 本报告不包含真实健康数据、密钥、完整 prompt 或数据库 checkpoint。",
        ]
    )
    return "\n".join(lines) + "\n"


def publish_agent_run_report(
    output_root: Path,
    manifest: AgentEvaluationManifest,
    details: list[AgentTaskDetail],
    *,
    run_id: str | None = None,
) -> Path:
    """写入不可覆写的 run 目录：details.json / summary.md / decision.json。"""

    if run_id is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"agent-{manifest.manifest_version}-{stamp}"
    target = output_root / run_id
    if target.exists():
        raise FileExistsError(f"报告目录已存在，拒绝覆盖: {target}")
    target.mkdir(parents=True)

    recomputed = recompute_report_from_details(manifest, details, run_id=run_id)
    details_payload = {
        "schema_version": 1,
        "run_id": run_id,
        "manifest_version": manifest.manifest_version,
        "model_id": manifest.model_id,
        "tool_version": manifest.tool_version,
        "corpus_version": manifest.corpus_version,
        "top_k": manifest.top_k,
        "temperature": manifest.temperature,
        "repetitions": manifest.repetitions,
        "latency_definition": manifest.latency_definition,
        "owner_confirmed": manifest.owner_confirmed,
        "details": details_to_jsonable(details),
        "aggregate": recomputed["aggregate"],
        "details_sha256": details_sha256(details),
    }
    (target / "details.json").write_text(
        json.dumps(details_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (target / "summary.md").write_text(
        build_markdown_summary(manifest, details, run_id=run_id),
        encoding="utf-8",
    )
    (target / "decision.json").write_text(
        json.dumps(recomputed["decision"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target
