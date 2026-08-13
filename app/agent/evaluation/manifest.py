"""M3.7 manifest/task 加载与严格校验。"""

# 导入 hashlib，为任务集计算稳定内容 hash。
import hashlib
# 导入 json，读取 JSON 任务与 manifest。
import json
# 导入 math，拒绝非有限阈值。
import math
# 导入 Path，统一相对路径解析。
from pathlib import Path
# 导入评测值对象。
from app.agent.evaluation.types import AgentEvaluationManifest, AgentTaskCase, AgentTaskLayer


# 允许的分层集合。
_ALLOWED_LAYERS = {AgentTaskLayer.shared, AgentTaskLayer.agent_only}
# 允许的 grader 集合。
_ALLOWED_GRADERS = {"contains_all", "terminal_status", "tool_success"}
# 允许的期望终态。
_ALLOWED_STATUSES = {"completed", "failed", "cancelled", "running"}
# 真实运行可复现的操作场景；超时由专门的 ToolRuntime 测试覆盖，不能伪造为 Provider 结果。
_ALLOWED_SCENARIOS = {"direct", "multi-clue", "ood-refusal", "approval", "cancel", "recovery"}
# 明确占位符前缀：模板未替换时禁止正式确认。
_PLACEHOLDER_MARKERS = ("REPLACE_", "TODO_", "CHANGEME", "<YOUR_", "YOUR_")


def _require_non_empty_str(value: object, field: str) -> str:
    """要求非空字符串，失败时给出稳定字段名。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _require_finite_number(value: object, field: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    """要求有限数值，可选上下界。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{field} 必须是有限数值")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} 不能小于 {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field} 不能大于 {maximum}")
    return number


def _require_positive_int(value: object, field: str) -> int:
    """要求严格正整数。"""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} 必须是正整数")
    return value


def _looks_like_placeholder(value: str) -> bool:
    """判断字符串是否仍是脚手架占位符。"""

    text = value.strip()
    upper = text.upper()
    if not text:
        return False
    return any(marker in upper for marker in _PLACEHOLDER_MARKERS)


def is_synthetic_manifest(manifest: AgentEvaluationManifest) -> bool:
    """识别仅用于工程验证的合成 manifest，不得当作生产 owner 配置。"""

    version = manifest.manifest_version.lower()
    model = manifest.model_id.lower()
    return "synthetic" in version or model.startswith("synthetic") or model == "synthetic-model"


def compute_tasks_sha256(raw_bytes: bytes) -> str:
    """对任务集原始字节计算 SHA-256。"""

    return hashlib.sha256(raw_bytes).hexdigest()


def hash_question(question: str) -> str:
    """对问题文本做稳定 hash，避免 details 直接存完整 prompt 时仍可追溯。"""

    return hashlib.sha256(question.encode("utf-8")).hexdigest()


def parse_agent_tasks(payload: object) -> tuple[AgentTaskCase, ...]:
    """把 JSON 任务列表解析为不可变任务元组。"""

    if not isinstance(payload, list) or not payload:
        raise ValueError("任务集必须是非空 JSON 数组")
    tasks: list[AgentTaskCase] = []
    seen: set[str] = set()
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise ValueError(f"任务[{index}] 必须是对象")
        task_id = _require_non_empty_str(row.get("task_id"), f"任务[{index}].task_id")
        if task_id in seen:
            raise ValueError(f"重复 task_id: {task_id}")
        seen.add(task_id)
        layer = _require_non_empty_str(row.get("layer"), f"任务[{index}].layer")
        if layer not in _ALLOWED_LAYERS:
            raise ValueError(f"任务[{index}].layer 不合法")
        question = _require_non_empty_str(row.get("question"), f"任务[{index}].question")
        grader = _require_non_empty_str(row.get("grader"), f"任务[{index}].grader")
        if grader not in _ALLOWED_GRADERS:
            raise ValueError(f"任务[{index}].grader 不合法")
        grader_params = row.get("grader_params", {})
        if not isinstance(grader_params, dict):
            raise ValueError(f"任务[{index}].grader_params 必须是对象")
        expected_status = _require_non_empty_str(row.get("expected_status"), f"任务[{index}].expected_status")
        if expected_status not in _ALLOWED_STATUSES:
            raise ValueError(f"任务[{index}].expected_status 不合法")
        expect_tool_success = bool(row.get("expect_tool_success", False))
        expect_approval_resume = bool(row.get("expect_approval_resume", False))
        scenario = _require_non_empty_str(row.get("scenario", "direct"), f"任务[{index}].scenario")
        if scenario not in _ALLOWED_SCENARIOS:
            raise ValueError(f"任务[{index}].scenario 不合法")
        notes = row.get("notes", "")
        if notes is None:
            notes = ""
        if not isinstance(notes, str):
            raise ValueError(f"任务[{index}].notes 必须是字符串")
        if grader == "contains_all":
            keywords = grader_params.get("keywords")
            if not isinstance(keywords, list) or not keywords or any(not isinstance(item, str) or not item.strip() for item in keywords):
                raise ValueError(f"任务[{index}] contains_all 需要非空 keywords")
        tasks.append(
            AgentTaskCase(
                task_id=task_id,
                layer=layer,
                question=question,
                grader=grader,
                grader_params=dict(grader_params),
                expected_status=expected_status,
                expect_tool_success=expect_tool_success,
                expect_approval_resume=expect_approval_resume,
                scenario=scenario,
                notes=notes,
            )
        )
    return tuple(tasks)


def load_agent_tasks(path: Path) -> tuple[tuple[AgentTaskCase, ...], str, bytes]:
    """加载任务集文件，返回任务、内容 hash 与原始字节。"""

    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("任务集必须是 UTF-8 JSON") from error
    tasks = parse_agent_tasks(payload)
    return tasks, compute_tasks_sha256(raw), raw


def parse_agent_manifest(payload: object) -> AgentEvaluationManifest:
    """解析 manifest 对象为不可变配置。"""

    if not isinstance(payload, dict):
        raise ValueError("manifest 必须是 JSON 对象")
    schema_version = _require_positive_int(payload.get("schema_version"), "schema_version")
    if schema_version != 1:
        raise ValueError("目前只支持 schema_version=1")
    run_order_raw = payload.get("run_order")
    if not isinstance(run_order_raw, list) or not run_order_raw or any(not isinstance(item, str) or not item.strip() for item in run_order_raw):
        raise ValueError("run_order 必须是非空字符串数组")
    unavailable = payload.get("unavailable_fields", ["usage", "cost"])
    if not isinstance(unavailable, list) or any(not isinstance(item, str) or not item.strip() for item in unavailable):
        raise ValueError("unavailable_fields 必须是字符串数组")
    owner_confirmed = payload.get("owner_confirmed", False)
    if not isinstance(owner_confirmed, bool):
        raise ValueError("owner_confirmed 必须是布尔值")
    return AgentEvaluationManifest(
        schema_version=schema_version,
        manifest_version=_require_non_empty_str(payload.get("manifest_version"), "manifest_version"),
        tasks_path=_require_non_empty_str(payload.get("tasks_path"), "tasks_path"),
        tasks_sha256=_require_non_empty_str(payload.get("tasks_sha256"), "tasks_sha256"),
        model_id=_require_non_empty_str(payload.get("model_id"), "model_id"),
        tool_version=_require_non_empty_str(payload.get("tool_version"), "tool_version"),
        corpus_version=_require_non_empty_str(payload.get("corpus_version"), "corpus_version"),
        top_k=_require_positive_int(payload.get("top_k"), "top_k"),
        temperature=_require_finite_number(payload.get("temperature"), "temperature", minimum=0.0),
        repetitions=_require_positive_int(payload.get("repetitions"), "repetitions"),
        run_order=tuple(item.strip() for item in run_order_raw),
        latency_definition=_require_non_empty_str(payload.get("latency_definition"), "latency_definition"),
        shared_success_threshold=_require_finite_number(payload.get("shared_success_threshold"), "shared_success_threshold", minimum=0.0, maximum=1.0),
        agent_only_success_threshold=_require_finite_number(payload.get("agent_only_success_threshold"), "agent_only_success_threshold", minimum=0.0, maximum=1.0),
        tool_success_threshold=_require_finite_number(payload.get("tool_success_threshold"), "tool_success_threshold", minimum=0.0, maximum=1.0),
        approval_resume_threshold=_require_finite_number(payload.get("approval_resume_threshold"), "approval_resume_threshold", minimum=0.0, maximum=1.0),
        owner_confirmed=owner_confirmed,
        owner_confirmation_ref=str(payload.get("owner_confirmation_ref") or ""),
        unavailable_fields=tuple(item.strip() for item in unavailable),
    )


def validate_agent_manifest(manifest: AgentEvaluationManifest, tasks: tuple[AgentTaskCase, ...], *, tasks_sha256: str) -> None:
    """校验 manifest 与任务集绑定关系；非法配置 fail-closed。"""

    if manifest.tasks_sha256 != tasks_sha256:
        raise ValueError("tasks_sha256 与任务集内容不一致")
    task_ids = {task.task_id for task in tasks}
    if set(manifest.run_order) != task_ids:
        raise ValueError("run_order 必须恰好覆盖全部 task_id")
    if len(manifest.run_order) != len(task_ids):
        raise ValueError("run_order 不能包含重复 task_id")
    if not any(task.layer == AgentTaskLayer.shared for task in tasks):
        raise ValueError("任务集至少需要一条 shared 任务")
    if not any(task.layer == AgentTaskLayer.agent_only for task in tasks):
        raise ValueError("任务集至少需要一条 agent-only 任务")
    if manifest.owner_confirmed and not manifest.owner_confirmation_ref.strip():
        raise ValueError("owner_confirmed=true 时必须提供 owner_confirmation_ref")
    # 正式确认后禁止保留脚手架占位符，避免模板被当成生产证据。
    if manifest.owner_confirmed:
        if _looks_like_placeholder(manifest.model_id):
            raise ValueError("owner_confirmed=true 时 model_id 不能是占位符")
        if _looks_like_placeholder(manifest.owner_confirmation_ref):
            raise ValueError("owner_confirmed=true 时 owner_confirmation_ref 不能是占位符")
        if is_synthetic_manifest(manifest):
            raise ValueError("synthetic manifest 不能标记 owner_confirmed=true")


def load_agent_manifest(path: Path, *, project_root: Path | None = None) -> tuple[AgentEvaluationManifest, tuple[AgentTaskCase, ...]]:
    """加载并校验 manifest 与其绑定的任务集。"""

    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("manifest 必须是合法 JSON") from error
    manifest = parse_agent_manifest(payload)
    # 默认：evaluation/agent/manifests/x.json -> parents[3] 是项目根。
    root = project_root if project_root is not None else path.resolve().parents[3]
    tasks_path = (root / manifest.tasks_path).resolve()
    tasks, tasks_hash, _raw = load_agent_tasks(tasks_path)
    validate_agent_manifest(manifest, tasks, tasks_sha256=tasks_hash)
    return manifest, tasks
