"""M5.1 报告发布：details/summary/decision 必须可互相重算且不可覆写。"""

# 导入 hashlib/json，保证 details 指纹与稳定 JSON 写出。
import hashlib
import json
# 导入 re，校验 run_id 字符集，防止报告路径穿越。
import re
# 导入 dataclass fields/asdict，按声明字段生成白名单与 JSON 对象。
from dataclasses import asdict, fields
# 导入 datetime/timezone，生成默认 run_id 时间戳。
from datetime import datetime, timezone
# 导入 Path，统一报告目录路径。
from pathlib import Path
# 导入 Sequence，统一接受 tuple/list details。
from collections.abc import Sequence
# 导入 Any，标注 JSON 友好字典。
from typing import Any

# 导入聚合、决策、序列化与 synthetic 识别。
from app.evaluation.quality.aggregate import aggregate_quality_details
from app.evaluation.quality.decision import (
    build_quality_decision_record,
    decide_quality_verdict,
)
from app.evaluation.quality.details import (
    details_to_jsonable,
    validate_quality_details,
)
from app.evaluation.quality.manifest import is_synthetic_quality_manifest
from app.evaluation.quality.scan import scan_report_payload
from app.evaluation.quality.types import QualityClaimDetail, QualityManifest


# details.json 顶层允许字段。
_QUALITY_REPORT_FIELDS = {
    "schema_version",
    "run_mode",
    "run_id",
    "manifest_version",
    "batch_id",
    "quality_schema_version",
    "grader_provider_version",
    "dataset_provenance_sha256",
    "reference_evidence_sha256",
    "manual_review_sha256",
    "owner_confirmed",
    "owner_confirmation_ref",
    "methods",
    "details",
    "aggregate",
    "details_sha256",
    "synthetic_only",
}
# decision.json 顶层允许字段。
_DECISION_RECORD_FIELDS = {
    "schema_version",
    "run_mode",
    "evidence_kind",
    "run_id",
    "manifest_version",
    "batch_id",
    "quality_schema_version",
    "grader_provider_version",
    "details_sha256",
    "decision",
    "reasons",
    "thresholds",
    "aggregate",
    "owner_confirmed",
    "owner_confirmation_ref",
    "synthetic_only",
    "scan_failed",
}
# 单方法 identity 允许字段。
_METHOD_FIELDS = {
    "method",
    "run_id",
    "model_id",
    "tool_version",
    "corpus_version",
    "source_manifest_sha256",
    "reference_manifest_sha256",
    "projection_path",
    "projection_sha256",
    "task_ids",
    "repetitions",
}
# details 行字段由 dataclass 声明生成，未知字段无法进入。
_DETAIL_FIELDS = {field.name for field in fields(QualityClaimDetail)}
# 单层质量指标允许字段。
_LAYER_METRIC_FIELDS = {
    "eligible_claims",
    "claims_with_citation",
    "supported_cited_claims",
    "citation_coverage",
    "citation_support",
    "scored_cases",
    "relevant_cases",
    "relevance_relevant_rate",
    "relevance_mean",
    "factuality_eligible_claims",
    "reviewed_claims",
    "pass_claims",
    "factuality_pass_rate",
    "factuality_review_coverage",
    "judge_unavailable_claim_count",
    "claim_count",
}
# 单方法聚合允许字段：两层指标加缺失样本计数。
_METHOD_METRIC_FIELDS = {
    "shared",
    "agent-only",
    "expected_case_count",
    "observed_case_count",
    "missing_case_count",
}
# 聚合顶层允许字段。
_AGGREGATE_FIELDS = {
    "dense",
    "agent",
    "total_detail_count",
    "has_missing_cases",
    "has_judge_unavailable_claims",
}
# 阈值对象允许字段。
_THRESHOLD_FIELDS = {
    "citation_coverage_threshold",
    "citation_support_threshold",
    "relevance_mean_threshold",
    "factuality_pass_rate_threshold",
}
# run_id 只允许安全文件名字符，任何路径分隔符都会破坏“一个 run 一个目录”的边界。
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_run_id(run_id: str) -> None:
    """拒绝会改变报告输出目录的 run_id。"""

    # 字符集先排除 / \ 等路径分隔符，再显式拒绝 .. 这种父目录跳转写法。
    if not _RUN_ID_PATTERN.fullmatch(run_id) or ".." in run_id:
        raise ValueError(f"run_id 不合法，只能使用字母/数字/./_/-，且不能包含 ..: {run_id}")


def _stable_json_bytes(payload: object) -> bytes:
    """用固定分隔符编码 JSON，保证 hash 可复现。"""

    # sort_keys/固定分隔符让同一内容永远得到同一字节流。
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def details_sha256(details: Sequence[QualityClaimDetail]) -> str:
    """对 details 内容计算 SHA-256。"""

    # hash 输入来自 JSON 原生对象，任何字段变化都会改变指纹。
    return hashlib.sha256(_stable_json_bytes(details_to_jsonable(details))).hexdigest()


def validate_report_payload(
    payload: object,
    allowed: set[str],
    path: str = "payload",
    *,
    nested_allowed: dict[str, set[str]] | None = None,
) -> None:
    """按白名单递归拒绝未知字段，防止报告 schema 漂移。"""

    # 字典必须只包含声明过的键；未知字段立即失败。
    if isinstance(payload, dict):
        unknown = sorted(set(payload).difference(allowed))
        if unknown:
            raise ValueError(f"{path} 包含白名单外字段: {unknown}")
        # 递归进入每个子对象；列表结构沿用当前层白名单。
        for key, value in payload.items():
            child_allowed = (nested_allowed or {}).get(key, allowed)
            validate_report_payload(
                value,
                child_allowed,
                f"{path}.{key}",
                nested_allowed=nested_allowed,
            )
    # 列表逐项递归，确保 details/rows 内部也不能出现未知键。
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            validate_report_payload(
                value,
                allowed,
                f"{path}[{index}]",
                nested_allowed=nested_allowed,
            )


def _method_identity_jsonable(manifest: QualityManifest) -> list[dict[str, Any]]:
    """把 manifest 方法身份转成 JSON 原生对象。"""

    # task_ids 是 tuple，JSON 需要先转成 list。
    return [
        {**asdict(identity), "task_ids": list(identity.task_ids)}
        for identity in manifest.methods
    ]


def recompute_report_from_details(
    manifest: QualityManifest,
    details: Sequence[QualityClaimDetail],
    *,
    run_id: str,
    scan_failed: bool = False,
) -> dict[str, Any]:
    """仅根据 details + manifest 重算 aggregate 与 decision。"""

    # 公共重算入口也必须校验冻结身份，不能只依赖 CLI 的 projection 前置检查。
    validate_quality_details(details, manifest)
    # aggregate 是唯一数字来源，decision 使用同一个 aggregate。
    aggregate = aggregate_quality_details(details, manifest)
    decision = build_quality_decision_record(
        manifest,
        aggregate,
        details_sha256=details_sha256(details),
        run_id=run_id,
        scan_failed=scan_failed,
    )
    return {
        # 固定 run_id。
        "run_id": run_id,
        # 完整聚合。
        "aggregate": aggregate,
        # 完整 decision record。
        "decision": decision,
        # 精简 verdict，方便 summary 与测试读取。
        "verdict": decide_quality_verdict(
            manifest,
            aggregate,
            scan_failed=scan_failed,
        ),
    }


def build_markdown_summary(
    manifest: QualityManifest,
    details: Sequence[QualityClaimDetail],
    *,
    run_id: str,
    scan_failed: bool = False,
) -> str:
    """生成人读 Markdown summary；所有数字来自重算结果。"""

    # summary 不允许自己再算一遍，必须复用 recompute 输出。
    recomputed = recompute_report_from_details(
        manifest,
        details,
        run_id=run_id,
        scan_failed=scan_failed,
    )
    aggregate = recomputed["aggregate"]
    verdict = recomputed["verdict"]

    # 统一格式化 None 为 not_available。
    def fmt_rate(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.4f}"

    lines = [
        f"# M5.1 回答级质量评测报告（{run_id}）",
        "",
        "## 元信息",
        f"- manifest_version: `{manifest.manifest_version}`",
        f"- batch_id: `{manifest.batch_id}`",
        f"- quality_schema_version: `{manifest.quality_schema_version}`",
        f"- grader_provider_version: `{manifest.grader_provider_version}`",
        f"- owner_confirmed: `{manifest.owner_confirmed}`",
        f"- decision: **{verdict['decision']}**",
    ]
    # 未确认或合成 run 都必须明确标注非生产证据。
    if not manifest.owner_confirmed or verdict["decision"] == "synthetic_only":
        lines.extend(
            [
                "",
                "> **非生产指标**：本报告 owner 未确认或仅为合成工程证据，禁止当作真实质量 pass。",
            ]
        )

    # 按 method 输出，dense 与 agent 的 shared/agent-only 分母互不混用。
    for method in ("dense", "agent"):
        if method not in aggregate:
            continue
        method_metrics = aggregate[method]
        lines.extend(
            [
                "",
                f"## {method}",
                (
                    f"- expected_case_count / observed_case_count / missing_case_count: "
                    f"{method_metrics['expected_case_count']} / "
                    f"{method_metrics['observed_case_count']} / "
                    f"{method_metrics['missing_case_count']}"
                ),
            ]
        )
        # shared 与 agent-only 独立展示，避免空分母被平均值掩盖。
        for layer in ("shared", "agent-only"):
            metrics = method_metrics[layer]
            lines.extend(
                [
                    "",
                    f"### {layer}",
                    f"- eligible_claims / claims_with_citation: "
                    f"{metrics['eligible_claims']} / {metrics['claims_with_citation']}",
                    f"- citation_coverage: {fmt_rate(metrics['citation_coverage'])}",
                    f"- citation_support: {fmt_rate(metrics['citation_support'])} "
                    f"({metrics['supported_cited_claims']}/{metrics['claims_with_citation']})",
                    f"- scored_cases / relevant_cases: "
                    f"{metrics['scored_cases']} / {metrics['relevant_cases']}",
                    f"- relevance_relevant_rate: "
                    f"{fmt_rate(metrics['relevance_relevant_rate'])}",
                    f"- relevance_mean: {fmt_rate(metrics['relevance_mean'])}",
                    f"- factuality_pass_rate: {fmt_rate(metrics['factuality_pass_rate'])} "
                    f"({metrics['pass_claims']}/{metrics['reviewed_claims']})",
                    f"- factuality_review_coverage: "
                    f"{fmt_rate(metrics['factuality_review_coverage'])}",
                    f"- judge_unavailable_claim_count: "
                    f"{metrics['judge_unavailable_claim_count']}",
                ]
            )

    lines.extend(["", "## 决策理由"])
    if verdict["reasons"]:
        lines.extend(f"- {reason}" for reason in verdict["reasons"])
    else:
        lines.append("- 全部阈值与 owner gate 已通过。")
    lines.extend(
        [
            "",
            "> 本报告只保存声明/引用/身份 hash 与证据引用，不包含完整 query、回答正文、健康信息或密钥。",
        ]
    )
    return "\n".join(lines) + "\n"


def publish_quality_run_report(
    output_root: Path,
    manifest: QualityManifest,
    details: Sequence[QualityClaimDetail],
    *,
    run_id: str | None = None,
) -> Path:
    """写入不可覆写的 m5-quality-<run-id> 目录。"""

    # 未指定 run_id 时用 manifest 版本加 UTC 时间戳。
    if run_id is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{manifest.manifest_version}-{stamp}"
    # 路径拼接前先校验，避免把外部输入当成目录片段。
    _validate_run_id(run_id)
    # 目标目录固定带 m5-quality- 前缀，与设计路径一致。
    target = output_root / f"m5-quality-{run_id}"
    # 已存在目录必须拒绝覆盖，保证历史证据不可变。
    if target.exists():
        raise FileExistsError(f"报告目录已存在，拒绝覆盖: {target}")

    # 所有数字都在内存中先重算，避免落盘时手填漂移。
    recomputed = recompute_report_from_details(
        manifest,
        details,
        run_id=run_id,
    )
    aggregate = recomputed["aggregate"]
    details_payload = {
        # 机器可读 schema 版本。
        "schema_version": 1,
        # 显式运行模式：synthetic/production。
        "run_mode": manifest.run_mode,
        # 唯一 run_id。
        "run_id": run_id,
        # manifest 版本。
        "manifest_version": manifest.manifest_version,
        # 配对批次。
        "batch_id": manifest.batch_id,
        # 投影 schema 版本。
        "quality_schema_version": manifest.quality_schema_version,
        # grader/judge 版本。
        "grader_provider_version": manifest.grader_provider_version,
        # 评测集 provenance hash。
        "dataset_provenance_sha256": manifest.dataset_provenance_sha256,
        # 参考证据 provenance hash。
        "reference_evidence_sha256": manifest.reference_evidence_sha256,
        # 人工复核证据 hash。
        "manual_review_sha256": manifest.manual_review_sha256,
        # owner 是否确认 manifest。
        "owner_confirmed": manifest.owner_confirmed,
        # owner 授权引用。
        "owner_confirmation_ref": manifest.owner_confirmation_ref or None,
        # 方法身份列表。
        "methods": _method_identity_jsonable(manifest),
        # 逐声明 details。
        "details": details_to_jsonable(details),
        # 聚合结果。
        "aggregate": aggregate,
        # details 内容 hash。
        "details_sha256": details_sha256(details),
        # 合成证据标记。
        "synthetic_only": is_synthetic_quality_manifest(manifest),
    }
    # 写盘前同时执行白名单与敏感扫描；任一失败都不得创建目录。
    validate_report_payload(
        details_payload,
        _QUALITY_REPORT_FIELDS,
        "report",
        nested_allowed={
            "methods": _METHOD_FIELDS,
            "details": _DETAIL_FIELDS,
            "aggregate": _AGGREGATE_FIELDS,
            "dense": _METHOD_METRIC_FIELDS,
            "agent": _METHOD_METRIC_FIELDS,
            "shared": _LAYER_METRIC_FIELDS,
            "agent-only": _LAYER_METRIC_FIELDS,
        },
    )
    scan_report_payload(details_payload)
    # decision 也单独扫描，防止未来扩展时绕过 details 白名单。
    scan_report_payload(recomputed["decision"])

    # 所有 gate 通过后才创建目录。
    target.mkdir(parents=True)
    # details.json 是机器可读主证据；显式 LF 避免 Windows 自动写 CRLF。
    (target / "details.json").write_text(
        json.dumps(details_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    # summary.md 供人读复核；同样固定 LF，便于跨平台 diff。
    (target / "summary.md").write_text(
        build_markdown_summary(manifest, details, run_id=run_id),
        encoding="utf-8",
        newline="\n",
    )
    # decision.json 是最终 pass/hold 结论；固定 LF 后与冻结输入风格一致。
    (target / "decision.json").write_text(
        json.dumps(recomputed["decision"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target
