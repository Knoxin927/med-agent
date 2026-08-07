"""只读比较 M2 已发布检索证据，并生成可追溯的候选决策报告。"""

# 导入 json，使用标准解析器读取已发布的机器可读证据。
import json
# 导入 os，使用不会覆盖既有文件的硬链接完成原子发布。
import os
# 导入 Path 和 PureWindowsPath，安全限制 manifest 只能引用项目内文件。
from pathlib import Path, PureWindowsPath
# 导入 tempfile，在目标目录创建仅供本次发布使用的唯一临时文件。
import tempfile
# 导入 Any，承接尚未通过 schema 校验的 JSON 值。
from typing import Any

# 复用既有文件 hash 实现，确保比较器与评测数据使用同一原始字节口径。
from app.evaluation.data import file_sha256


# 表示比较证据不满足冻结契约，调用方必须停止而非猜测或补零。
class ComparisonError(ValueError):
    """M2 跨方法比较输入不可信或结构不完整。"""


# 固定本阶段支持的四种真实检索方法，未知方法不能悄悄进入比较表。
METHODS = ("dense", "hybrid", "dense-rerank", "rewrite-dense")
# 固定质量展示顺序，避免不同方法报告以不同字段顺序造成视觉误导。
METRIC_NAMES = (
    "recall_at_5",
    "recall_at_10",
    "mrr_at_10",
    "all_relevant_hit_at_5",
    "all_relevant_hit_at_10",
)
# 明确历史报告缺少环境指纹，禁止据此产出跨运行速度名次。
PERFORMANCE_COMPARABILITY = "not_comparable_missing_environment"
# 固定 M2.5 唯一允许消费的已提交 evidence manifest 相对路径。
FROZEN_MANIFEST_RELATIVE_PATH = Path("evaluation/rewrite-online/m2-retrieval-comparison-evidence-v2.json")
# 固定该 manifest 的原始字节 SHA-256，防止调用方替换路径后同步伪造六份证据 hash。
FROZEN_MANIFEST_SHA256 = "0d419b15a3c45362a4507e44262b8e9b6467055a4a23bc5dd1ae12046ddc477c"
# 候选报告只能发布到项目内这个单一目录，避免证据散落或写入任意位置。
CANDIDATE_OUTPUT_DIRECTORY = Path("evaluation/decisions")


# 要求值是 JSON 对象，不能从数组或标量推测字段。
def _require_object(value: object, label: str) -> dict[str, Any]:
    """返回已验证的对象，否则抛出 ComparisonError。"""

    # 所有后续字段都依赖键访问，因此顶层结构错误必须立即停止。
    if not isinstance(value, dict):
        raise ComparisonError(f"{label} 必须是对象")
    # 返回原对象，不在校验前复制或修改证据。
    return value


# 要求值是非空字符串，并统一去掉首尾空白。
def _require_text(value: object, label: str) -> str:
    """返回受控文本字段，空白或非字符串时失败。"""

    # 标识、方法和路径都不能通过隐式转换获得。
    if not isinstance(value, str) or not value.strip():
        raise ComparisonError(f"{label} 必须是非空字符串")
    # 返回规范化文本，避免空白造成同一标识比较失败。
    return value.strip()


# 要求值是普通整数，拒绝 bool 冒充零或一。
def _require_int(value: object, label: str) -> int:
    """返回普通整数，类型错误时失败。"""

    # 精确类型检查保护 case 数和 K 值等离散契约。
    if type(value) is not int:
        raise ComparisonError(f"{label} 必须是整数")
    # 返回已验证整数。
    return value


# 将 manifest 相对路径安全解析到项目目录内。
def _resolve_project_path(project_root: Path, value: object, label: str) -> Path:
    """拒绝绝对路径和父目录跳转后返回项目内真实路径。"""

    # 路径文本先满足非空字符串契约。
    relative_text = _require_text(value, label)
    # 同时检查当前平台与 Windows 盘符形式的绝对路径。
    relative_path = Path(relative_text)
    if relative_path.is_absolute() or bool(PureWindowsPath(relative_text).drive):
        raise ComparisonError(f"{label} 必须是项目内相对路径")
    # 父目录跳转会逃逸项目根，因此显式拒绝。
    if ".." in relative_path.parts:
        raise ComparisonError(f"{label} 不能包含父目录跳转")
    # resolve 后再次验证最终路径仍属于项目根。
    resolved_root = project_root.resolve()
    resolved_path = (resolved_root / relative_path).resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as error:
        raise ComparisonError(f"{label} 必须位于项目根目录内") from error
    # 比较证据必须已经存在，不能临时生成或联网补取。
    if not resolved_path.is_file():
        raise ComparisonError(f"{label} 指向的证据文件不存在")
    return resolved_path


# 将 CLI 传入的候选输出限制为固定 decisions 目录下的新 JSON 文件。
def resolve_candidate_output_path(project_root: Path, output_path: Path) -> Path:
    """返回受控候选输出路径，拒绝绝对路径、越界和非 decisions 目录。"""

    # 输出必须是相对项目根的文本路径，拒绝任意绝对本机位置。
    if output_path.is_absolute() or bool(PureWindowsPath(str(output_path)).drive):
        raise ComparisonError("候选输出必须是 evaluation/decisions 下的相对路径")
    # 父目录跳转会绕过固定发布目录，因此在 resolve 前拒绝。
    if ".." in output_path.parts:
        raise ComparisonError("候选输出不能包含父目录跳转")
    # 仅允许直接写入固定目录，不开放子目录或项目内其他目录。
    if output_path.parent != CANDIDATE_OUTPUT_DIRECTORY:
        raise ComparisonError("候选输出只能位于 evaluation/decisions 目录")
    # 候选报告固定为 JSON，避免同一发布器被复用为任意文件写入器。
    if output_path.suffix != ".json" or output_path.name == ".json":
        raise ComparisonError("候选输出必须是非空 .json 文件名")
    # 解析后的最终路径仍需位于项目根，作为对平台路径规则的最后一道校验。
    resolved_root = project_root.resolve()
    resolved_output = (resolved_root / output_path).resolve()
    try:
        resolved_output.relative_to(resolved_root)
    except ValueError as error:
        raise ComparisonError("候选输出必须位于项目根目录内") from error
    # 返回唯一可发布的绝对目标路径。
    return resolved_output


# 读取 UTF-8 JSON，并把损坏内容统一映射为比较领域错误。
def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    """读取一份 JSON 对象证据，不泄露项目外路径。"""

    # JSONDecodeError 的文本只包含行列号，不包含 API 或运行时密钥。
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ComparisonError(f"{label} 不是有效 JSON") from error
    # 当前所有输入都以对象表达，因此数组不能继续消费。
    return _require_object(value, label)


# 将 Top-10 结果转换为稳定 identity 序列，同时验证连续排名。
def _ordered_identities(
    value: object,
    label: str,
    *,
    expected_count: int = 10,
) -> list[dict[str, object]]:
    """返回指定数量的有序 source_name/chunk_index 身份对象。"""

    # 每个调用点明确其宽度：正式排名固定十条，rerank 内部排名使用 candidate_k。
    if not isinstance(value, list) or len(value) != expected_count:
        raise ComparisonError(f"{label} 必须包含 {expected_count} 个结果")
    # 保存按排名顺序的最小身份，不复制 text 或 score 等方法私有字段。
    identities: list[dict[str, object]] = []
    # 用集合检测同一 chunk 重复占据多个名次。
    seen: set[tuple[str, int]] = set()
    for expected_rank, raw_result in enumerate(value, start=1):
        # 每项必须是对象，不能从字符串拼出 identity。
        result = _require_object(raw_result, label)
        # rank 必须严格连续，顺序即诊断含义的一部分。
        if _require_int(result.get("rank"), f"{label}.rank") != expected_rank:
            raise ComparisonError(f"{label} 的 rank 必须从 1 连续到 10")
        # source 和 index 是项目既有的稳定 chunk identity。
        source_name = _require_text(result.get("source_name"), f"{label}.source_name")
        chunk_index = _require_int(result.get("chunk_index"), f"{label}.chunk_index")
        identity = (source_name, chunk_index)
        if identity in seen:
            raise ComparisonError(f"{label} 不能包含重复 identity")
        seen.add(identity)
        # 输出只保留跨方法可比且不含原始医疗文本的最小身份。
        identities.append({"source_name": source_name, "chunk_index": chunk_index})
    return identities


# 从每题指标构造项目已固定的四元质量键。
def _quality_key(metric: object, label: str) -> tuple[float, float, float, int]:
    """返回 Recall@10、Recall@5、MRR@10、全相关命中键。"""

    # 单题指标必须是对象，避免用汇总均值替代逐题结论。
    raw_metric = _require_object(metric, label)
    # 三个连续指标必须是数字且不接受 bool。
    values: list[float] = []
    for name in ("recall_at_10", "recall_at_5", "mrr_at_10"):
        value = raw_metric.get(name)
        if type(value) not in {int, float}:
            raise ComparisonError(f"{label}.{name} 必须是数字")
        values.append(float(value))
    # direct/paraphrase 单题没有多线索命中含义，按既有报告约定归为 0。
    all_relevant = raw_metric.get("all_relevant_hit_at_10")
    if all_relevant is None:
        all_relevant_value = 0
    elif type(all_relevant) is bool:
        all_relevant_value = int(all_relevant)
    else:
        raise ComparisonError(f"{label}.all_relevant_hit_at_10 必须是 bool 或 null")
    return values[0], values[1], values[2], all_relevant_value


# 由两个质量键严格得出改善、退化或不变，不依赖旧比较字段的自述。
def _outcome(baseline: tuple[float, ...], candidate: tuple[float, ...]) -> str:
    """按既有字典序质量键计算单题 outcome。"""

    # Python 元组字典序与 M2 已有比较器的质量键顺序一致。
    if candidate > baseline:
        return "improved"
    if candidate < baseline:
        return "degraded"
    return "unchanged"


# 校验一份质量报告的冻结输入、运行身份、Top-K 和案例覆盖。
def _validate_quality_report(
    raw: dict[str, Any],
    *,
    method: str,
    run_id: str,
    reference_input: dict[str, Any] | None,
    reference_snapshot: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回已校验报告与其标准输入身份。"""

    # manifest 中的方法必须与报告声明一致，避免交换文件顺序却继续比较。
    if _require_text(raw.get("method"), "report.method") != method:
        raise ComparisonError("质量报告 method 与 manifest 不一致")
    # run ID 也是文件身份的一部分，不能只凭目录名推断。
    if _require_text(raw.get("evaluation_run_id"), "report.evaluation_run_id") != run_id:
        raise ComparisonError("质量报告 run_id 与 manifest 不一致")
    # production 前缀证明它不是 fake 或临时评测输出。
    if _require_text(raw.get("evidence_kind"), "report.evidence_kind") != f"production-{method}":
        raise ComparisonError("质量报告 evidence_kind 不正确")
    # input、parameters、metrics 和 builder snapshot 都是跨方法口径的必要节点。
    report_input = _require_object(raw.get("input"), "report.input")
    parameters = _require_object(raw.get("parameters"), "report.parameters")
    metrics = _require_object(raw.get("metrics"), "report.metrics")
    builder_snapshot = _require_object(raw.get("builder_snapshot"), "report.builder_snapshot")
    # 所有正式报告固定 Top-10；候选宽度只记录，不要求相等。
    if _require_int(parameters.get("top_k"), "report.parameters.top_k") != 10:
        raise ComparisonError("质量报告 top_k 必须为 10")
    # 所有运行均为一轮预热与五轮正式测量，避免把不同轮次的质量表混入。
    if _require_int(parameters.get("warmup_rounds"), "report.parameters.warmup_rounds") != 1:
        raise ComparisonError("质量报告 warmup_rounds 必须为 1")
    if _require_int(parameters.get("measured_rounds"), "report.parameters.measured_rounds") != 5:
        raise ComparisonError("质量报告 measured_rounds 必须为 5")
    # 固定数据身份字段必须与第一份 dense 报告完全一致。
    input_fields = ("dataset_sha256", "manifest_sha256", "confirmation_confirmed_at", "chunk_size", "overlap")
    if reference_input is not None:
        for field_name in input_fields:
            if report_input.get(field_name) != reference_input.get(field_name):
                raise ComparisonError(f"质量报告 input.{field_name} 与 dense 基线不一致")
    # builder snapshot 保证四份报告面向同一 12 chunk 冻结语料。
    if _require_int(builder_snapshot.get("chunk_count"), "builder_snapshot.chunk_count") != 12:
        raise ComparisonError("质量报告 builder_snapshot.chunk_count 必须为 12")
    if reference_snapshot is not None and builder_snapshot != reference_snapshot:
        raise ComparisonError("质量报告 builder_snapshot 与 dense 基线不一致")
    # 汇总数量不能掩盖逐题 map 缺项。
    if _require_int(metrics.get("in_domain_case_count"), "metrics.in_domain_case_count") != 24:
        raise ComparisonError("质量报告必须包含 24 条库内 case")
    if _require_int(metrics.get("out_of_domain_case_count"), "metrics.out_of_domain_case_count") != 6:
        raise ComparisonError("质量报告必须包含 6 条库外 case")
    # 三个逐题对象必须拥有完全相同的 30 个 case 键。
    case_metrics = _require_object(raw.get("case_metrics_by_case_id"), "case_metrics_by_case_id")
    ranked_results = _require_object(raw.get("ranked_results_by_case_id"), "ranked_results_by_case_id")
    if set(case_metrics) != set(ranked_results) or len(case_metrics) != 30:
        raise ComparisonError("质量报告逐题指标与 Top-10 必须覆盖相同 30 条 case")
    # 逐题 metrics 与分层计数必须一致，且每条排名都具备十个有序 identity。
    in_domain_count = 0
    out_of_domain_count = 0
    for case_id, metric in case_metrics.items():
        raw_metric = _require_object(metric, f"case_metrics.{case_id}")
        if _require_text(raw_metric.get("case_id"), f"case_metrics.{case_id}.case_id") != case_id:
            raise ComparisonError("质量报告 case_metrics 的 case_id 不一致")
        stratum = _require_text(raw_metric.get("primary_stratum"), f"case_metrics.{case_id}.primary_stratum")
        if stratum == "out-of-domain":
            out_of_domain_count += 1
            # 库外题不进入 Recall/MRR 分母，三个质量指标必须保持既有的 null 语义。
            if any(
                raw_metric.get(name) is not None
                for name in ("recall_at_5", "recall_at_10", "mrr_at_10")
            ):
                raise ComparisonError("库外 case 的质量指标必须为 null")
        else:
            in_domain_count += 1
            # 只有库内题才可构造用于改善/退化判断的质量键。
            _quality_key(raw_metric, f"case_metrics.{case_id}")
        _ordered_identities(ranked_results[case_id], f"ranked_results.{case_id}")
    if in_domain_count != 24 or out_of_domain_count != 6:
        raise ComparisonError("质量报告 case 分层数量不满足 24/6")
    # rewrite 报告还必须绑定 evidence manifest 指定的冻结快照身份。
    if method == "rewrite-dense":
        if reference_input is None:
            raise ComparisonError("rewrite-dense 缺少 dense 输入基线")
        if _require_text(parameters.get("rewrite_snapshot_id"), "rewrite_snapshot_id") != _require_text(reference_input.get("rewrite_snapshot_id"), "manifest rewrite_snapshot_id"):
            raise ComparisonError("rewrite-dense 快照 ID 与 manifest 不一致")
    return raw, report_input


# 校验 rewrite 在线记录与快照逐题绑定，并返回可展示的分段摘要。
def _validate_online_evidence(
    online: dict[str, Any],
    snapshot: dict[str, Any],
    expected_input: dict[str, Any],
) -> dict[str, Any]:
    """验证 1+5 在线样本覆盖和模型绑定，返回同次组件比较。"""

    # 在线记录必须是生产级真实 API 证据。
    if _require_text(online.get("evidence_kind"), "online.evidence_kind") != "production-rewrite-online":
        raise ComparisonError("online evidence_kind 不正确")
    # online input 必须与四份离线报告使用相同冻结 dataset/manifest/confirmation。
    online_input = _require_object(online.get("input"), "online.input")
    for field_name in ("dataset_sha256", "manifest_sha256", "confirmation_confirmed_at"):
        if online_input.get(field_name) != expected_input.get(field_name):
            raise ComparisonError(f"online input.{field_name} 与质量报告不一致")
    # 运行参数固定为 Top-10、一轮预热和五轮正式测量。
    parameters = _require_object(online.get("parameters"), "online.parameters")
    if _require_int(parameters.get("top_k"), "online.parameters.top_k") != 10:
        raise ComparisonError("online top_k 必须为 10")
    if _require_int(parameters.get("warmup_rounds"), "online.parameters.warmup_rounds") != 1:
        raise ComparisonError("online warmup_rounds 必须为 1")
    if _require_int(parameters.get("measured_rounds"), "online.parameters.measured_rounds") != 5:
        raise ComparisonError("online measured_rounds 必须为 5")
    # 快照记录是在线样本 model 与 case 身份的可信对照来源。
    records = snapshot.get("records")
    if not isinstance(records, list) or len(records) != 30:
        raise ComparisonError("rewrite snapshot 必须包含 30 条 records")
    snapshot_by_case: dict[str, dict[str, Any]] = {}
    for raw_record in records:
        record = _require_object(raw_record, "rewrite snapshot record")
        case_id = _require_text(record.get("case_id"), "rewrite snapshot case_id")
        if case_id in snapshot_by_case:
            raise ComparisonError("rewrite snapshot 不能包含重复 case_id")
        snapshot_by_case[case_id] = record
    # timing 保存原始样本；只允许指定字段，不从汇总值猜测覆盖。
    timing = _require_object(online.get("timing"), "online.timing")
    warmup_samples = timing.get("warmup_samples")
    measured_samples = timing.get("measured_samples")
    if not isinstance(warmup_samples, list) or not isinstance(measured_samples, list):
        raise ComparisonError("online timing 必须包含样本列表")
    if len(warmup_samples) != 30 or len(measured_samples) != 150:
        raise ComparisonError("online 必须包含 30 条预热和 150 条正式样本")
    # 每条样本都应与快照的模型、文本和 case 对应，且总耗时不得小于两个顺序阶段。
    seen_by_round: dict[tuple[str, int], set[str]] = {}
    for expected_kind, samples, expected_rounds in (("warmup", warmup_samples, {1}), ("measured", measured_samples, {1, 2, 3, 4, 5})):
        for raw_sample in samples:
            sample = _require_object(raw_sample, "online sample")
            if _require_text(sample.get("round_kind"), "online sample round_kind") != expected_kind:
                raise ComparisonError("online sample round_kind 不一致")
            round_index = _require_int(sample.get("round_index"), "online sample round_index")
            if round_index not in expected_rounds:
                raise ComparisonError("online sample round_index 不正确")
            case_id = _require_text(sample.get("case_id"), "online sample case_id")
            snapshot_record = snapshot_by_case.get(case_id)
            if snapshot_record is None:
                raise ComparisonError("online sample 包含快照外 case")
            if sample.get("model") != snapshot_record.get("model"):
                raise ComparisonError("online sample model 与 rewrite snapshot 不一致")
            numeric_values = (sample.get("rewrite_ms"), sample.get("dense_ms"), sample.get("total_ms"))
            if any(type(value) not in {int, float} for value in numeric_values):
                raise ComparisonError("online sample 耗时必须是数字")
            if float(sample["total_ms"]) + 1e-9 < float(sample["rewrite_ms"]) + float(sample["dense_ms"]):
                raise ComparisonError("online sample total_ms 小于阶段耗时之和")
            seen_by_round.setdefault((expected_kind, round_index), set()).add(case_id)
    # 每轮必须刚好覆盖快照的全部 30 条 case，避免重复样本掩盖漏题。
    expected_cases = set(snapshot_by_case)
    if any(case_ids != expected_cases for case_ids in seen_by_round.values()):
        raise ComparisonError("online 每轮必须覆盖全部 30 条 snapshot case")
    # cost 字段保留语义，不允许把未知单价或金额伪装成零。
    cost = _require_object(online.get("cost"), "online.cost")
    if cost.get("amount") != "not_available" or cost.get("price_evidence") != "not_available":
        raise ComparisonError("online 未核验价格必须明确为 not_available")
    # 返回同次分段统计，明确它只说明 rewrite 链路的组件等待。
    return {
        "comparison_kind": "same_run_component_comparison",
        "rewrite_ms": {"p50": timing.get("rewrite_p50_ms"), "p95": timing.get("rewrite_p95_ms")},
        "dense_ms": {"p50": timing.get("dense_p50_ms"), "p95": timing.get("dense_p95_ms")},
        "total_ms": {"p50": timing.get("total_p50_ms"), "p95": timing.get("total_p95_ms")},
        "cost": cost,
    }


# 读取 manifest 和六份冻结证据，生成只读候选比较对象。
def build_m2_comparison(project_root: Path, manifest_path: Path) -> dict[str, Any]:
    """重新校验证据后返回四种方法的同口径比较结果。"""

    # manifest 自身也必须位于项目中，调用方不能借 CLI 读取任意路径。
    resolved_root = project_root.resolve()
    resolved_manifest = manifest_path.resolve()
    try:
        resolved_manifest.relative_to(resolved_root)
    except ValueError as error:
        raise ComparisonError("manifest 必须位于项目根目录内") from error
    # 调用方不能用另一个 manifest 悄悄替换 M2.5 已提交的证据集合。
    frozen_manifest_path = (resolved_root / FROZEN_MANIFEST_RELATIVE_PATH).resolve()
    if resolved_manifest != frozen_manifest_path:
        raise ComparisonError("manifest 必须是固定的 M2.5 evidence manifest")
    # 即使路径相同，文件内容被替换也必须在读取任何条目前停止。
    if not frozen_manifest_path.is_file() or file_sha256(frozen_manifest_path) != FROZEN_MANIFEST_SHA256:
        raise ComparisonError("固定 M2.5 evidence manifest 的 SHA-256 不匹配")
    manifest = _load_json_object(resolved_manifest, "comparison manifest")
    if _require_int(manifest.get("schema_version"), "manifest.schema_version") != 1:
        raise ComparisonError("comparison manifest schema_version 不受支持")
    if _require_text(manifest.get("evidence_kind"), "manifest.evidence_kind") != "m2-retrieval-comparison-evidence":
        raise ComparisonError("comparison manifest evidence_kind 不正确")
    # 四份质量报告必须完整且不重复方法。
    raw_reports = manifest.get("quality_reports")
    if not isinstance(raw_reports, list) or len(raw_reports) != len(METHODS):
        raise ComparisonError("comparison manifest 必须包含四份质量报告")
    reports: dict[str, dict[str, Any]] = {}
    report_hashes: dict[str, str] = {}
    report_run_ids: dict[str, str] = {}
    for raw_entry in raw_reports:
        entry = _require_object(raw_entry, "quality_reports entry")
        method = _require_text(entry.get("method"), "quality_reports.method")
        if method not in METHODS or method in reports:
            raise ComparisonError("quality_reports 方法集合不正确")
        path = _resolve_project_path(resolved_root, entry.get("path"), "quality_reports.path")
        expected_hash = _require_text(entry.get("sha256"), "quality_reports.sha256")
        if file_sha256(path) != expected_hash:
            raise ComparisonError("质量报告 SHA-256 不匹配")
        reports[method] = _load_json_object(path, f"{method} quality report")
        report_hashes[method] = expected_hash
        report_run_ids[method] = _require_text(entry.get("run_id"), "quality_reports.run_id")
    if set(reports) != set(METHODS):
        raise ComparisonError("quality_reports 缺少必需方法")
    # rewrite snapshot 的 hash 和 ID 是在线模型绑定的锚点。
    snapshot_entry = _require_object(manifest.get("rewrite_snapshot"), "rewrite_snapshot")
    snapshot_path = _resolve_project_path(resolved_root, snapshot_entry.get("path"), "rewrite_snapshot.path")
    snapshot_hash = _require_text(snapshot_entry.get("sha256"), "rewrite_snapshot.sha256")
    if file_sha256(snapshot_path) != snapshot_hash:
        raise ComparisonError("rewrite snapshot SHA-256 不匹配")
    snapshot = _load_json_object(snapshot_path, "rewrite snapshot")
    snapshot_id = _require_text(snapshot_entry.get("snapshot_id"), "rewrite_snapshot.snapshot_id")
    if _require_text(snapshot.get("rewrite_snapshot_id"), "rewrite snapshot id") != snapshot_id:
        raise ComparisonError("rewrite snapshot ID 与 manifest 不一致")
    # 先把 snapshot ID 注入 reference input，仅用于 rewrite 报告的额外绑定。
    dense_reference_input: dict[str, Any] | None = None
    dense_snapshot: dict[str, Any] | None = None
    for method in METHODS:
        reference_input = dense_reference_input
        if method == "rewrite-dense" and dense_reference_input is not None:
            reference_input = {**dense_reference_input, "rewrite_snapshot_id": snapshot_id}
        _, report_input = _validate_quality_report(
            reports[method],
            method=method,
            run_id=report_run_ids[method],
            reference_input=reference_input,
            reference_snapshot=dense_snapshot,
        )
        if method == "dense":
            dense_reference_input = report_input
            dense_snapshot = _require_object(reports[method].get("builder_snapshot"), "dense builder_snapshot")
    # hybrid/rewrite 必须显式声明同一个 dense 基线；rerank 用其同次 pre/post 自证。
    dense_run_id = report_run_ids["dense"]
    for method in ("hybrid", "rewrite-dense"):
        comparison = _require_object(reports[method].get("comparison"), f"{method}.comparison")
        if _require_text(comparison.get("baseline_run_id"), f"{method}.baseline_run_id") != dense_run_id:
            raise ComparisonError(f"{method} baseline_run_id 与 dense 基线不一致")
    rerank = reports["dense-rerank"]
    pre_metrics = _require_object(rerank.get("pre_case_metrics_by_case_id"), "rerank pre_case_metrics")
    pre_ranked = _require_object(rerank.get("pre_ranked_results_by_case_id"), "rerank pre_ranked_results")
    post_ranked = _require_object(rerank.get("post_ranked_results_by_case_id"), "rerank post_ranked_results")
    post_metrics = _require_object(rerank.get("case_metrics_by_case_id"), "rerank case_metrics")
    rerank_cases = _require_object(
        _require_object(rerank.get("comparison"), "rerank comparison").get("cases"),
        "rerank comparison.cases",
    )
    # pre/post 指标、pre/post 排名与既有 comparison 必须覆盖同一批案例。
    if not (
        set(pre_metrics)
        == set(pre_ranked)
        == set(post_ranked)
        == set(post_metrics)
        == set(rerank_cases)
    ):
        raise ComparisonError("rerank pre/post case 集合不一致")
    # rerank 的内部 pre/post 候选宽度独立于正式 Top-10，且必须至少容纳十条。
    rerank_parameters = _require_object(rerank.get("parameters"), "rerank parameters")
    rerank_candidate_k = _require_int(rerank_parameters.get("candidate_k"), "rerank candidate_k")
    if rerank_candidate_k < 10:
        raise ComparisonError("rerank candidate_k 不能小于正式 Top-10")
    # 逐题重算已有 rerank comparison，避免只校验 map 键却相信报告自述。
    for case_id in pre_metrics:
        # 两端 Top-10 都要重新校验连续排名和稳定 identity。
        pre_identities = _ordered_identities(
            pre_ranked[case_id],
            f"rerank pre ranked {case_id}",
            expected_count=rerank_candidate_k,
        )
        # 后排名同时是 rerank 对 dense 比较时使用的正式排名。
        post_identities = _ordered_identities(
            post_ranked[case_id],
            f"rerank post ranked {case_id}",
            expected_count=rerank_candidate_k,
        )
        # 正式报告只保留重排后前十条，它必须与内部 post 候选的前十条一致。
        formal_post_identities = _ordered_identities(
            _require_object(rerank.get("ranked_results_by_case_id"), "rerank ranked_results")[case_id],
            f"rerank formal ranked {case_id}",
        )
        if post_identities[:10] != formal_post_identities:
            raise ComparisonError("rerank post Top-10 与正式排名不一致")
        # 已有逐题 comparison 仅是可复核摘要，不能替代这里的原始证据。
        rerank_case = _require_object(rerank_cases[case_id], f"rerank comparison case {case_id}")
        # 库内题需要完整质量键和胜负方向；库外题只保留 identity 诊断。
        if _require_text(rerank_case.get("kind"), f"rerank comparison kind {case_id}") == "in-domain":
            # 每题都必须是完整指标对象，不能用汇总数代替 pre/post 质量证据。
            pre_key = _quality_key(pre_metrics[case_id], f"rerank pre metric {case_id}")
            # 后指标与正式 case metrics 同源，供重算 post 质量键。
            post_key = _quality_key(post_metrics[case_id], f"rerank post metric {case_id}")
            if rerank_case.get("pre_quality_key") != list(pre_key):
                raise ComparisonError("rerank comparison pre_quality_key 不一致")
            if rerank_case.get("post_quality_key") != list(post_key):
                raise ComparisonError("rerank comparison post_quality_key 不一致")
            if rerank_case.get("outcome") != _outcome(pre_key, post_key):
                raise ComparisonError("rerank comparison outcome 不一致")
        elif rerank_case.get("kind") != "out-of-domain":
            raise ComparisonError("rerank comparison kind 不正确")
        # 所有题都必须准确记录 pre/post 的有序 Top-10 是否发生变化。
        if rerank_case.get("top_10_identity_changed") != (pre_identities[:10] != post_identities[:10]):
            raise ComparisonError("rerank comparison top_10_identity_changed 不一致")
    # online 证据也逐字节校验，然后与 snapshot/冻结输入交叉验证。
    online_entry = _require_object(manifest.get("online_evidence"), "online_evidence")
    online_path = _resolve_project_path(resolved_root, online_entry.get("path"), "online_evidence.path")
    online_hash = _require_text(online_entry.get("sha256"), "online_evidence.sha256")
    if file_sha256(online_path) != online_hash:
        raise ComparisonError("rewrite online SHA-256 不匹配")
    online = _load_json_object(online_path, "rewrite online")
    online_comparison = _validate_online_evidence(online, snapshot, dense_reference_input or {})
    # 为四种方法归一展示层，历史延迟只作为原始观察值，不进行名次比较。
    method_views: dict[str, Any] = {}
    for method in METHODS:
        report = reports[method]
        timing = _require_object(report.get("timing"), f"{method}.timing")
        method_views[method] = {
            "run_id": report_run_ids[method],
            "quality_metrics": {name: _require_object(report.get("metrics"), f"{method}.metrics").get(name) for name in METRIC_NAMES},
            "candidate_k": _require_object(report.get("parameters"), f"{method}.parameters").get("candidate_k"),
            "raw_latency_ms": {"p50": timing.get("latency_p50_ms"), "p95": timing.get("latency_p95_ms"), "sample_count": timing.get("latency_sample_count")},
            "resources": report.get("resources", "not_measured"),
            "cost": "not_applicable" if method != "rewrite-dense" else online_comparison["cost"],
        }
    # 基于 dense 的同一 30 个 case 生成质量 outcome 与库外 Top-10 诊断。
    dense_metrics = _require_object(reports["dense"].get("case_metrics_by_case_id"), "dense case_metrics")
    dense_ranked = _require_object(reports["dense"].get("ranked_results_by_case_id"), "dense ranked_results")
    in_domain_outcomes: dict[str, Any] = {}
    out_of_domain_changes: dict[str, Any] = {}
    for case_id, dense_metric in dense_metrics.items():
        stratum = _require_text(_require_object(dense_metric, f"dense metric {case_id}").get("primary_stratum"), "dense primary_stratum")
        dense_identities = _ordered_identities(dense_ranked[case_id], f"dense ranked {case_id}")
        candidate_entries: dict[str, Any] = {}
        for method in METHODS:
            report = reports[method]
            candidate_metric = _require_object(report.get("case_metrics_by_case_id"), f"{method} case_metrics")[case_id]
            candidate_ranked = _require_object(report.get("ranked_results_by_case_id"), f"{method} ranked_results")[case_id]
            candidate_identities = _ordered_identities(candidate_ranked, f"{method} ranked {case_id}")
            # 库外题不构造质量键，只保留相对于 dense 的有序 identity 变化。
            if stratum == "out-of-domain":
                candidate_entries[method] = {"top_10_identity_changed": candidate_identities != dense_identities}
            else:
                # 库内题才按既有质量键形成逐题改善、退化或不变结论。
                dense_key = _quality_key(dense_metric, f"dense metric {case_id}")
                candidate_key = _quality_key(candidate_metric, f"{method} metric {case_id}")
                candidate_entries[method] = {"quality_key": list(candidate_key), "outcome": _outcome(dense_key, candidate_key), "top_10_identity_changed": candidate_identities != dense_identities}
        if stratum == "out-of-domain":
            out_of_domain_changes[case_id] = {method: entry["top_10_identity_changed"] for method, entry in candidate_entries.items()}
        else:
            in_domain_outcomes[case_id] = candidate_entries
    # 返回候选报告对象；发布器负责处理不可覆盖写入和自身 hash。
    return {
        "schema_version": 1,
        "evidence_kind": "m2-retrieval-comparison-candidate",
        "performance_comparability": PERFORMANCE_COMPARABILITY,
        "inputs": {"quality_reports": report_hashes, "rewrite_snapshot_sha256": snapshot_hash, "rewrite_online_sha256": online_hash},
        "methods": method_views,
        "rewrite_same_run_components": online_comparison,
        "in_domain_outcomes": in_domain_outcomes,
        "out_of_domain_identity_changes": out_of_domain_changes,
        "conclusion": "pending_user_decision",
    }


# 原子发布候选比较报告，拒绝覆盖已有用户可复核结论。
def publish_m2_comparison(project_root: Path, output_path: Path, comparison: dict[str, Any]) -> None:
    """将完整候选对象原子写入新路径。"""

    # 先将调用方路径收敛到固定 decisions 目录，发布器自身不能被 CLI 绕过。
    resolved_output = resolve_candidate_output_path(project_root, output_path)
    # 任何已有路径都说明历史候选已存在，必须使用新的版本名重跑。
    if resolved_output.exists():
        raise FileExistsError("M2 比较输出已存在，不能覆盖")
    # 创建项目内的 decisions 父目录，避免调用方预先手工建目录。
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    # 创建唯一临时文件，避免两个并发发布者争用固定 .tmp 文件名。
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{resolved_output.stem}.",
        suffix=".tmp",
        dir=resolved_output.parent,
    )
    # mkstemp 返回的描述符不再需要，后续由 Path 以 UTF-8 写入完整 JSON。
    os.close(descriptor)
    # 将临时名称转为 Path，便于统一清理。
    temporary_path = Path(temporary_name)
    try:
        # 稳定格式便于 Git diff 和用户审阅；不写入任何请求头或密钥。
        temporary_path.write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        try:
            # hard link 仅在目标不存在时成功，因此并发竞争也不能覆盖历史候选。
            os.link(temporary_path, resolved_output)
        except FileExistsError as error:
            # 统一错误语义，让 CLI 与单元测试都能明确识别不可覆盖契约。
            raise FileExistsError("M2 比较输出已存在，不能覆盖") from error
    finally:
        # 无论发布成功或竞争失败，临时文件都只属于本次调用且必须清理。
        temporary_path.unlink(missing_ok=True)
