"""M5.6 closure manifest 解析、冻结与严格校验。"""

# 导入 hashlib/json/re，计算 hash、解析 JSON、校验 SHA-256。
import hashlib
import json
import re
from pathlib import Path

# 导入敏感扫描。
from app.evaluation.closure.scan import scan_closure_payload
# 导入值对象与冻结集合。
from app.evaluation.closure.types import (
    CLOSURE_LINES,
    ClosureLineRef,
    ClosureManifest,
)


# 当前只支持 schema_version=1。
_SUPPORTED_SCHEMA_VERSION = 1
# 必填 hash 统一为 64 位小写十六进制。
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
# manifest 顶层允许字段。
_MANIFEST_FIELDS = {
    "schema_version",
    "run_mode",
    "manifest_version",
    "batch_id",
    "closure_schema_version",
    "owner_confirmed",
    "owner_confirmation_ref",
    "lines",
}
# 单条线引用允许字段。
_LINE_FIELDS = {
    "line",
    "decision_path",
    "decision_sha256",
}


def compute_sha256(raw_bytes: bytes) -> str:
    """对冻结输入原始字节计算 SHA-256。"""

    return hashlib.sha256(raw_bytes).hexdigest()


def is_synthetic_closure_manifest(manifest: ClosureManifest) -> bool:
    """识别仅用于工程验证的 synthetic manifest。"""

    return manifest.run_mode == "synthetic"


def _require_non_empty_str(value: object, field: str) -> str:
    """要求非空字符串。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _require_positive_int(value: object, field: str) -> int:
    """要求严格正整数。"""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} 必须是正整数")
    return value


def _require_sha256(value: object, field: str) -> str:
    """要求 64 位 SHA-256 小写十六进制。"""

    text = _require_non_empty_str(value, field).lower()
    if not _SHA256_PATTERN.fullmatch(text):
        raise ValueError(f"{field} 必须是 SHA-256 十六进制字符串")
    return text


def _assert_known_fields(payload: object, allowed: set[str], path: str) -> None:
    """只校验当前 dict 本层字段白名单。"""

    if isinstance(payload, dict):
        unknown = sorted(set(payload).difference(allowed))
        if unknown:
            raise ValueError(f"{path} 包含白名单外字段: {unknown}")


def parse_closure_manifest(payload: object) -> ClosureManifest:
    """把 manifest JSON 解析为不可变配置。"""

    if not isinstance(payload, dict):
        raise ValueError("manifest 必须是 JSON 对象")
    scan_closure_payload(payload, "manifest")
    _assert_known_fields(payload, _MANIFEST_FIELDS, "manifest")
    schema_version = _require_positive_int(payload.get("schema_version"), "manifest.schema_version")
    if schema_version != _SUPPORTED_SCHEMA_VERSION:
        raise ValueError("manifest 目前只支持 schema_version=1")
    run_mode = _require_non_empty_str(payload.get("run_mode"), "manifest.run_mode")
    if run_mode not in {"synthetic", "production"}:
        raise ValueError("manifest.run_mode 必须为 synthetic 或 production")
    owner_confirmed = payload.get("owner_confirmed", False)
    if not isinstance(owner_confirmed, bool):
        raise ValueError("owner_confirmed 必须是布尔值")
    owner_confirmation_ref = str(payload.get("owner_confirmation_ref") or "").strip()
    lines_raw = payload.get("lines")
    if not isinstance(lines_raw, list) or not lines_raw:
        raise ValueError("manifest.lines 必须是非空数组")
    if len(lines_raw) != len(CLOSURE_LINES):
        raise ValueError(f"manifest.lines 必须包含五条线，当前 {len(lines_raw)}")
    seen: set[str] = set()
    line_refs: list[ClosureLineRef] = []
    for index, row in enumerate(lines_raw):
        if not isinstance(row, dict):
            raise ValueError(f"manifest.lines[{index}] 必须是对象")
        scan_closure_payload(row, f"manifest.lines[{index}]")
        _assert_known_fields(row, _LINE_FIELDS, f"manifest.lines[{index}]")
        line = _require_non_empty_str(row.get("line"), f"manifest.lines[{index}].line")
        if line not in CLOSURE_LINES:
            raise ValueError(f"manifest.lines[{index}].line 不合法: {line}")
        if line in seen:
            raise ValueError(f"manifest.lines[{index}].line 重复: {line}")
        seen.add(line)
        decision_path = _require_non_empty_str(
            row.get("decision_path"),
            f"manifest.lines[{index}].decision_path",
        )
        decision_sha256 = _require_sha256(
            row.get("decision_sha256"),
            f"manifest.lines[{index}].decision_sha256",
        )
        line_refs.append(
            ClosureLineRef(
                line=line,
                decision_path=decision_path,
                decision_sha256=decision_sha256,
            )
        )
    manifest = ClosureManifest(
        schema_version=schema_version,
        run_mode=run_mode,
        manifest_version=_require_non_empty_str(payload.get("manifest_version"), "manifest_version"),
        batch_id=_require_non_empty_str(payload.get("batch_id"), "batch_id"),
        closure_schema_version=_require_non_empty_str(
            payload.get("closure_schema_version"),
            "closure_schema_version",
        ),
        owner_confirmed=owner_confirmed,
        owner_confirmation_ref=owner_confirmation_ref,
        lines=tuple(sorted(line_refs, key=lambda item: item.line)),
    )
    if owner_confirmed and not owner_confirmation_ref:
        raise ValueError("owner_confirmed=true 时必须提供 owner_confirmation_ref")
    if owner_confirmed and is_synthetic_closure_manifest(manifest):
        raise ValueError("synthetic manifest 不能标记 owner_confirmed=true")
    return manifest


def load_closure_manifest(path: Path, *, project_root: Path | None = None) -> ClosureManifest:
    """从磁盘读取并解析 closure manifest。"""

    raw_bytes = path.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    manifest = parse_closure_manifest(payload)
    if project_root is not None:
        for ref in manifest.lines:
            resolve_closure_decision_path(ref, project_root=project_root)
    return manifest


def resolve_closure_decision_path(ref: ClosureLineRef, *, project_root: Path) -> Path:
    """解析并限制单线 decision 到本线 canonical report 目录。"""

    relative = Path(ref.decision_path)
    expected_prefix = f"m5-{ref.line}-"
    if (
        relative.is_absolute()
        or len(relative.parts) != 4
        or relative.parts[0:2] != ("evaluation", "reports")
        or not relative.parts[2].startswith(expected_prefix)
        or relative.name != "decision.json"
    ):
        raise ValueError(
            f"decision_path 必须绑定 {ref.line} 的 canonical report: {ref.decision_path}"
        )
    decision_path = (project_root / relative).resolve()
    if project_root.resolve() not in decision_path.parents:
        raise ValueError(f"decision_path 必须位于项目根下: {ref.decision_path}")
    return decision_path
