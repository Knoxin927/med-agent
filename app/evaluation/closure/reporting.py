"""M5.6 报告发布：summary/decision 必须可重算且不可覆写。"""

# 导入 json/re，稳定 JSON 写出与 run_id 校验。
import json
import re
# 导入 datetime/timezone，生成默认 run_id。
from datetime import datetime, timezone
# 导入 Path。
from pathlib import Path
# 导入 Any。
from typing import Any

# 导入聚合与决策。
from app.evaluation.closure.decision import build_closure_decision_record, decide_closure_verdict
from app.evaluation.closure.manifest import is_synthetic_closure_manifest
# 导入扫描。
from app.evaluation.closure.scan import scan_closure_payload
# 导入值对象。
from app.evaluation.closure.types import ClosureAggregate, ClosureManifest


# closure 报告顶层允许字段。
_CLOSURE_REPORT_FIELDS = {
    "schema_version",
    "run_mode",
    "run_id",
    "manifest_version",
    "batch_id",
    "closure_schema_version",
    "lines",
    "all_synthetic_only",
    "all_scan_ok",
    "all_claims_ok",
    "owner_confirmed",
    "owner_confirmation_ref",
    "synthetic_only",
    "production_adoption_claim",
}
# decision 顶层允许字段。
_DECISION_FIELDS = {
    "schema_version",
    "run_mode",
    "evidence_kind",
    "run_id",
    "manifest_version",
    "batch_id",
    "closure_schema_version",
    "decision",
    "reasons",
    "policy",
    "lines",
    "all_synthetic_only",
    "all_scan_ok",
    "all_claims_ok",
    "owner_confirmed",
    "owner_confirmation_ref",
    "synthetic_only",
    "production_adoption_claim",
    "scan_failed",
}
# 单条线汇总字段。
_LINE_SUMMARY_FIELDS = {
    "line",
    "evidence_kind",
    "decision",
    "synthetic_only",
    "scan_failed",
    "owner_confirmed",
    "claims_ok",
    "bad_claims",
    "run_id",
    "manifest_version",
}
# policy 字段。
_POLICY_FIELDS = {
    "lines",
    "production_adoption_claim",
}
# run_id 只允许安全文件名字符。
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_run_id(run_id: str) -> None:
    """拒绝会改变报告输出目录的 run_id。"""

    if not _RUN_ID_PATTERN.fullmatch(run_id) or ".." in run_id:
        raise ValueError(f"run_id 不合法，只能使用字母/数字/./_/-，且不能包含 ..: {run_id}")


def validate_report_payload(
    payload: object,
    allowed: set[str],
    path: str = "payload",
) -> None:
    """只校验当前对象层字段白名单。"""

    if not isinstance(payload, dict):
        raise ValueError(f"{path} 必须是对象")
    unknown = sorted(set(payload).difference(allowed))
    if unknown:
        raise ValueError(f"{path} 包含白名单外字段: {unknown}")


def recompute_report(
    manifest: ClosureManifest,
    aggregate: ClosureAggregate,
    *,
    run_id: str,
) -> dict[str, Any]:
    """从五线聚合重算 decision。"""

    decision = build_closure_decision_record(
        manifest,
        aggregate,
        run_id=run_id,
    )
    return {"aggregate": aggregate, "decision": decision}


def build_markdown_summary(
    manifest: ClosureManifest,
    aggregate: ClosureAggregate,
    *,
    run_id: str,
) -> str:
    """生成人读 summary.md。"""

    verdict = decide_closure_verdict(manifest, aggregate)
    lines = [
        f"# M5.6 工程化收尾报告 `{run_id}`",
        "",
        f"- decision: **{verdict['decision']}**",
        f"- run_mode: `{manifest.run_mode}`",
        f"- batch_id: `{manifest.batch_id}`",
        "- production_adoption_claim: `false`（本报告不授权把 M5 接入生产）",
        "",
        "## 五条线汇总",
    ]
    for item in aggregate.lines:
        lines.append(
            f"- {item.line}: decision=`{item.decision}`, synthetic_only=`{item.synthetic_only}`, "
            f"scan_failed=`{item.scan_failed}`, claims_ok=`{item.claims_ok}`, "
            f"owner_confirmed=`{item.owner_confirmed}`, run_id=`{item.run_id}`"
        )
    lines.append("")
    lines.append("## 决策理由")
    if verdict["reasons"]:
        lines.extend(f"- {reason}" for reason in verdict["reasons"])
    else:
        lines.append("- 全部门槛与 owner gate 已通过。")
    lines.extend(
        [
            "",
            "> 本报告只汇总五条线 decision 的公共字段与 production claim 合规性，不包含 query、回答正文、健康信息、密钥或各线 raw。",
            "> 本报告是本地合成工程证据汇总，不是 M5 接入生产、SLA 或医学正确性结论。",
        ]
    )
    return "\n".join(lines) + "\n"


def publish_closure_run_report(
    output_root: Path,
    manifest: ClosureManifest,
    aggregate: ClosureAggregate,
    *,
    run_id: str | None = None,
) -> Path:
    """写入不可覆写的 m5-closure-<run-id> 目录。"""

    if run_id is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{manifest.manifest_version}-{stamp}"
    _validate_run_id(run_id)
    target = output_root / f"m5-closure-{run_id}"
    if target.exists():
        raise FileExistsError(f"报告目录已存在，拒绝覆盖: {target}")

    recomputed = recompute_report(manifest, aggregate, run_id=run_id)
    report_payload = {
        "schema_version": 1,
        "run_mode": manifest.run_mode,
        "run_id": run_id,
        "manifest_version": manifest.manifest_version,
        "batch_id": manifest.batch_id,
        "closure_schema_version": manifest.closure_schema_version,
        "lines": recomputed["decision"]["lines"],
        "all_synthetic_only": aggregate.all_synthetic_only,
        "all_scan_ok": aggregate.all_scan_ok,
        "all_claims_ok": aggregate.all_claims_ok,
        "owner_confirmed": manifest.owner_confirmed,
        "owner_confirmation_ref": manifest.owner_confirmation_ref or None,
        "synthetic_only": is_synthetic_closure_manifest(manifest),
        "production_adoption_claim": False,
    }
    # 顶层字段白名单。
    validate_report_payload(report_payload, _CLOSURE_REPORT_FIELDS, "report")
    # 单条线汇总白名单。
    for index, line in enumerate(report_payload["lines"]):
        validate_report_payload(line, _LINE_SUMMARY_FIELDS, f"report.lines[{index}]")
    scan_closure_payload(report_payload, "report")
    scan_closure_payload(recomputed["decision"], "decision")
    validate_report_payload(recomputed["decision"], _DECISION_FIELDS, "decision")
    validate_report_payload(recomputed["decision"]["policy"], _POLICY_FIELDS, "decision.policy")

    target.mkdir(parents=True)
    (target / "summary.md").write_text(
        build_markdown_summary(manifest, aggregate, run_id=run_id),
        encoding="utf-8",
        newline="\n",
    )
    (target / "decision.json").write_text(
        json.dumps(recomputed["decision"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target
