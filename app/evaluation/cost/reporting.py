"""M5.5 报告发布：detail/aggregate/summary/decision 必须可互相重算且不可覆写。"""

# 导入 hashlib/json/re，保证 detail 指纹与稳定 JSON 写出。
import hashlib
import json
import re
# 导入 asdict，把明细转成 JSON 对象。
from dataclasses import asdict
# 导入 datetime/timezone，生成默认 run_id。
from datetime import datetime, timezone
# 导入 Path。
from pathlib import Path
# 导入 Sequence。
from collections.abc import Sequence
# 导入 Any。
from typing import Any

# 导入聚合、决策与 synthetic 识别。
from app.evaluation.cost.aggregate import aggregate_cost_details
from app.evaluation.cost.decision import build_cost_decision_record, decide_cost_verdict
from app.evaluation.cost.manifest import (
    compute_sha256,
    is_synthetic_cost_manifest,
    parse_cost_details,
    validate_cost_details_against_manifest,
)
from app.evaluation.cost.scan import scan_cost_payload
from app.evaluation.cost.types import CostDetail, CostManifest, USAGE_KEYS


# detail 报告顶层允许字段。
_COST_REPORT_FIELDS = {
    "schema_version",
    "run_mode",
    "run_id",
    "manifest_version",
    "batch_id",
    "cost_schema_version",
    "currency",
    "request_kinds",
    "price_source_ref",
    "price_as_of",
    "owner_confirmed",
    "owner_confirmation_ref",
    "details",
    "aggregate",
    "detail_sha256",
    "synthetic_only",
    "production_cost_claim",
}
# decision 顶层允许字段。
_DECISION_FIELDS = {
    "schema_version",
    "run_mode",
    "evidence_kind",
    "run_id",
    "manifest_version",
    "batch_id",
    "cost_schema_version",
    "currency",
    "detail_sha256",
    "decision",
    "reasons",
    "policy",
    "aggregate",
    "owner_confirmed",
    "owner_confirmation_ref",
    "synthetic_only",
    "production_cost_claim",
    "scan_failed",
}
# 单条明细字段。
_DETAIL_FIELDS = {
    "cost_schema_version",
    "detail_id",
    "batch_id",
    "run_id",
    "request_kind",
    "provider",
    "model",
    "usage_status",
    "pricing_status",
    "price_source_ref",
    "price_as_of",
    "currency",
    "unit_cost",
    "total_cost",
    "usage",
    "sampled_at",
}
# policy 字段。
_POLICY_FIELDS = {
    "currency",
    "request_kinds",
    "price_source_ref",
    "price_as_of",
}
# by_kind 桶字段。
_BUCKET_FIELDS = {
    "detail_count",
    "known_cost_sum",
    "not_available_count",
    "known_count",
    "usage_sum",
    "sample_ids",
}
# 聚合顶层字段。
_AGGREGATE_FIELDS = {
    "by_kind",
    "total_count",
    "total_known_count",
    "total_not_available_count",
    "coverage_denominator",
    "coverage_known_ratio",
    "known_cost_sum",
    "summary_amount",
    "has_complete_evidence",
}
# run_id 只允许安全文件名字符。
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_run_id(run_id: str) -> None:
    """拒绝会改变报告输出目录的 run_id。"""

    if not _RUN_ID_PATTERN.fullmatch(run_id) or ".." in run_id:
        raise ValueError(f"run_id 不合法，只能使用字母/数字/./_/-，且不能包含 ..: {run_id}")


def _stable_json_bytes(payload: object) -> bytes:
    """用固定分隔符编码 JSON，保证 hash 可复现。"""

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def details_to_jsonable(details: Sequence[CostDetail]) -> list[dict[str, Any]]:
    """把明细转成 JSON 友好列表。"""

    return [asdict(item) for item in details]


def detail_content_sha256(details: Sequence[CostDetail]) -> str:
    """对明细内容计算 SHA-256。"""

    return hashlib.sha256(_stable_json_bytes(details_to_jsonable(details))).hexdigest()


def validate_report_payload(
    payload: object,
    allowed: set[str],
    path: str = "payload",
) -> None:
    """只校验当前对象层的字段白名单，避免父子层白名单互相污染。"""

    if not isinstance(payload, dict):
        raise ValueError(f"{path} 必须是对象")
    unknown = sorted(set(payload).difference(allowed))
    if unknown:
        raise ValueError(f"{path} 包含白名单外字段: {unknown}")


def recompute_report_from_details(
    manifest: CostManifest,
    details: Sequence[CostDetail],
    *,
    run_id: str,
    detail_file_sha256: str,
) -> dict[str, Any]:
    """从 detail 重算 aggregate 与 decision。"""

    validate_cost_details_against_manifest(tuple(details), manifest)
    if detail_file_sha256 != manifest.detail_sha256:
        raise ValueError("detail_file_sha256 必须等于 manifest.detail_sha256")
    aggregate = aggregate_cost_details(details)
    decision = build_cost_decision_record(
        manifest,
        aggregate,
        detail_sha256=detail_file_sha256,
        run_id=run_id,
    )
    return {"aggregate": aggregate, "decision": decision}


def build_markdown_summary(
    manifest: CostManifest,
    details: Sequence[CostDetail],
    *,
    run_id: str,
    detail_file_sha256: str,
) -> str:
    """生成人读 summary.md。"""

    recomputed = recompute_report_from_details(
        manifest,
        details,
        run_id=run_id,
        detail_file_sha256=detail_file_sha256,
    )
    aggregate = recomputed["aggregate"]
    verdict = decide_cost_verdict(manifest, aggregate)
    summary_amount = aggregate["summary_amount"]
    amount_text = "not_available" if summary_amount is None else f"{summary_amount}"
    lines = [
        f"# M5.5 成本统计报告 `{run_id}`",
        "",
        f"- decision: **{verdict['decision']}**",
        f"- run_mode: `{manifest.run_mode}`",
        f"- batch_id: `{manifest.batch_id}`",
        f"- currency: `{manifest.currency}`",
        f"- price_source_ref: `{manifest.price_source_ref}`",
        f"- price_as_of: `{manifest.price_as_of}`",
        f"- summary_amount: `{amount_text}`",
        f"- known_cost_sum: `{aggregate['known_cost_sum']}`",
        f"- not_available_count: `{aggregate['total_not_available_count']}`",
        f"- coverage_denominator: `{aggregate['coverage_denominator']}`",
        f"- detail_sha256: `{detail_file_sha256}`",
        "- production_cost_claim: `false`（本报告不声明真实 provider 价格已授权或付费调用基线可信）",
        "",
        "## 按成本边界汇总",
    ]
    for kind, bucket in aggregate["by_kind"].items():
        lines.append(f"### {kind}")
        lines.extend(
            [
                f"- detail_count: `{bucket['detail_count']}`",
                f"- known_cost_sum: `{bucket['known_cost_sum']}`",
                f"- not_available_count: `{bucket['not_available_count']}`",
                f"- usage_sum: `{bucket['usage_sum']}`",
            ]
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
            "> 本报告只保存 provider/model 标签、token/计数、单价与金额，不包含 query、回答正文、健康信息、密钥或 provider 原始响应。",
            "> 本报告是本地合成/受控实验证据，不是真实付费成本、预算结论或医学正确性结论。",
        ]
    )
    return "\n".join(lines) + "\n"


def publish_cost_run_report(
    output_root: Path,
    manifest: CostManifest,
    details: Sequence[CostDetail],
    *,
    raw_bytes: bytes,
    run_id: str | None = None,
) -> Path:
    """写入不可覆写的 m5-cost-<run-id> 目录。"""

    if run_id is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{manifest.manifest_version}-{stamp}"
    _validate_run_id(run_id)
    target = output_root / f"m5-cost-{run_id}"
    if target.exists():
        raise FileExistsError(f"报告目录已存在，拒绝覆盖: {target}")

    if not isinstance(raw_bytes, bytes):
        raise TypeError("raw_bytes 必须是原始 JSON bytes")
    raw_file_sha256 = compute_sha256(raw_bytes)
    if raw_file_sha256 != manifest.detail_sha256:
        raise ValueError("raw_bytes 的 SHA-256 与 manifest.detail_sha256 不一致")
    try:
        parsed_details = parse_cost_details(json.loads(raw_bytes.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("raw_bytes 不是有效的 M5.5 detail JSON") from exc
    if tuple(details) != parsed_details:
        raise ValueError("details 必须与 raw_bytes 解析出的明细完全一致")
    validate_cost_details_against_manifest(parsed_details, manifest)

    recomputed = recompute_report_from_details(
        manifest,
        details,
        run_id=run_id,
        detail_file_sha256=raw_file_sha256,
    )
    report_payload = {
        "schema_version": 1,
        "run_mode": manifest.run_mode,
        "run_id": run_id,
        "manifest_version": manifest.manifest_version,
        "batch_id": manifest.batch_id,
        "cost_schema_version": manifest.cost_schema_version,
        "currency": manifest.currency,
        "request_kinds": list(manifest.request_kinds),
        "price_source_ref": manifest.price_source_ref,
        "price_as_of": manifest.price_as_of,
        "owner_confirmed": manifest.owner_confirmed,
        "owner_confirmation_ref": manifest.owner_confirmation_ref or None,
        "details": details_to_jsonable(details),
        "aggregate": recomputed["aggregate"],
        "detail_sha256": raw_file_sha256,
        "synthetic_only": is_synthetic_cost_manifest(manifest),
        "production_cost_claim": False,
    }
    # 顶层字段白名单：先挡 schema 漂移。
    validate_report_payload(report_payload, _COST_REPORT_FIELDS, "report")
    # 明细逐项白名单。
    for index, detail in enumerate(report_payload["details"]):
        validate_report_payload(detail, _DETAIL_FIELDS, f"report.details[{index}]")
        for key in detail["usage"]:
            if key not in USAGE_KEYS:
                raise ValueError(f"report.details[{index}].usage 包含白名单外键: {key}")
    # aggregate 顶层与 by_kind 分层白名单。
    validate_report_payload(report_payload["aggregate"], _AGGREGATE_FIELDS, "report.aggregate")
    by_kind = report_payload["aggregate"]["by_kind"]
    if not isinstance(by_kind, dict):
        raise ValueError("aggregate.by_kind 必须是对象")
    for kind, bucket in by_kind.items():
        validate_report_payload(bucket, _BUCKET_FIELDS, f"report.aggregate.by_kind.{kind}")
    scan_cost_payload(report_payload, "report")
    scan_cost_payload(recomputed["decision"], "decision")
    validate_report_payload(recomputed["decision"], _DECISION_FIELDS, "decision")
    validate_report_payload(recomputed["decision"]["policy"], _POLICY_FIELDS, "decision.policy")
    validate_report_payload(
        recomputed["decision"]["aggregate"],
        _AGGREGATE_FIELDS,
        "decision.aggregate",
    )

    target.mkdir(parents=True)
    (target / "raw.json").write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (target / "summary.md").write_text(
        build_markdown_summary(
            manifest,
            details,
            run_id=run_id,
            detail_file_sha256=raw_file_sha256,
        ),
        encoding="utf-8",
        newline="\n",
    )
    (target / "decision.json").write_text(
        json.dumps(recomputed["decision"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target
