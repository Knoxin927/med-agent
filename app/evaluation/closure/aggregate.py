"""M5.6 聚合：从五条线 decision 抽取公共字段并校验 production claim。"""

# 导入 json/hashlib/Path，读取并校验各线 decision.json。
import json
from collections.abc import Sequence
from pathlib import Path

# 导入 manifest 哈希工具。
from app.evaluation.closure.manifest import compute_sha256, resolve_closure_decision_path
# 导入敏感扫描。
from app.evaluation.closure.scan import scan_closure_payload
# 导入值对象与冻结集合。
from app.evaluation.closure.types import (
    CLOSURE_LINES,
    EVIDENCE_KIND_BY_LINE,
    LINE_DECISION_FIELDS,
    PRODUCTION_CLAIM_FIELDS_BY_LINE,
    ClosureAggregate,
    ClosureLineRef,
    ClosureLineSummary,
)


# default_bypass 语义特殊：cache 线要求 true 才合规，其余线无此字段。
# 因此单独用字典声明每个 claim 字段的“合规值”。
_CLAIM_OK_VALUES = {
    "capacity_claim": False,
    "production_logging_claim": False,
    "hot_path_claim": False,
    "production_cost_claim": False,
    "production_quality_claim": False,
    # default_bypass 必须 True 才合规（见 cache 设计）。
    "default_bypass": True,
}


def _check_line_claims(decision: dict) -> tuple[str, ...]:
    """返回该线不合规的 production claim 字段名。"""

    bad: list[str] = []
    for field, ok_value in _CLAIM_OK_VALUES.items():
        if field in decision:
            if type(decision[field]) is not type(ok_value) or decision[field] != ok_value:
                bad.append(field)
    return tuple(bad)


def load_line_decision(
    decision_path: Path,
    expected_sha256: str,
    *,
    line: str,
) -> dict:
    """读取单条线 decision.json，校验 hash 与 evidence_kind，只保留公共字段。"""

    raw_bytes = decision_path.read_bytes()
    actual_sha = compute_sha256(raw_bytes)
    if actual_sha != expected_sha256:
        raise ValueError(
            f"{line} decision.json SHA-256 与 manifest 冻结值不一致"
        )
    payload = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{line} decision 必须是 JSON 对象")
    # 输入也走敏感扫描，防止下游夹带 query/密钥。
    scan_closure_payload(payload, f"{line}.decision")
    # 只保留公共字段和当前线已知 production claim，丢弃私有 aggregate/policy/raw_sha256 等。
    extracted: dict = {}
    for field in LINE_DECISION_FIELDS:
        if field in payload:
            extracted[field] = payload[field]
    for field in PRODUCTION_CLAIM_FIELDS_BY_LINE[line]:
        if field in payload:
            extracted[field] = payload[field]
    allowed_fields = LINE_DECISION_FIELDS | PRODUCTION_CLAIM_FIELDS_BY_LINE[line]
    unknown = sorted(set(extracted).difference(allowed_fields))
    if unknown:
        raise ValueError(f"{line} decision 抽取后出现白名单外字段: {unknown}")
    # 校验 evidence_kind 与 line 对应，防止读错文件。
    evidence_kind = extracted.get("evidence_kind")
    if evidence_kind != EVIDENCE_KIND_BY_LINE[line]:
        raise ValueError(
            f"{line} decision.evidence_kind 不匹配，期望 {EVIDENCE_KIND_BY_LINE[line]}"
        )
    return extracted


def aggregate_closure_lines(
    manifest_lines: Sequence[ClosureLineRef],
    *,
    project_root: Path,
) -> ClosureAggregate:
    """读取并聚合五条线 decision。"""

    line_names = [ref.line for ref in manifest_lines]
    if len(manifest_lines) != len(CLOSURE_LINES) or set(line_names) != CLOSURE_LINES:
        raise ValueError("closure 聚合必须输入恰好五条、每条唯一的冻结线")

    summaries: list[ClosureLineSummary] = []
    all_synthetic_only = True
    all_scan_ok = True
    all_claims_ok = True
    all_owners_pending = True

    for ref in manifest_lines:
        decision_path = resolve_closure_decision_path(ref, project_root=project_root)
        if not decision_path.exists():
            raise ValueError(f"{ref.line} decision.json 不存在: {decision_path}")
        decision = load_line_decision(decision_path, ref.decision_sha256, line=ref.line)
        line_decision = decision.get("decision")
        synthetic_only = decision.get("synthetic_only") is True
        scan_failed = decision.get("scan_failed") is not False
        owner_confirmed = decision.get("owner_confirmed") is True
        bad_claims = _check_line_claims(decision)
        claims_ok = not bad_claims

        # 任意线非 synthetic_only 或扫描失败或 claim 不合规，都会后续触发 hold。
        if line_decision != "synthetic_only" or not synthetic_only:
            all_synthetic_only = False
        if scan_failed:
            all_scan_ok = False
        if not claims_ok:
            all_claims_ok = False
        # owner_confirmed=true 表示该线已用真实证据，但 closure 仍要求 synthetic-only 证据。
        # 这里只记录 owner 状态；“all owners pending”指五线均未 confirmed。
        if decision.get("owner_confirmed") is not False:
            all_owners_pending = False

        summaries.append(
            ClosureLineSummary(
                line=ref.line,
                evidence_kind=decision.get("evidence_kind", ""),
                decision=line_decision or "",
                synthetic_only=synthetic_only,
                scan_failed=scan_failed,
                owner_confirmed=owner_confirmed,
                claims_ok=claims_ok,
                run_id=str(decision.get("run_id", "")),
                manifest_version=str(decision.get("manifest_version", "")),
                bad_claims=bad_claims,
            )
        )

    has_complete = (
        {item.line for item in summaries} == CLOSURE_LINES
        and all_synthetic_only
        and all_scan_ok
        and all_claims_ok
        and all_owners_pending
    )
    return ClosureAggregate(
        lines=tuple(summaries),
        all_synthetic_only=all_synthetic_only,
        all_scan_ok=all_scan_ok,
        all_claims_ok=all_claims_ok,
        all_owners_pending=all_owners_pending,
        has_complete_evidence=has_complete,
    )
