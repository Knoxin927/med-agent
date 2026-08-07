"""加载并严格校验版本化评测语料、问题和文本块引用。"""

# 导入 hashlib，计算文件原始字节的稳定 SHA-256。
import hashlib
# 导入 json，使用标准结构化解析器读取 JSON 和 JSONL。
import json
# 导入 Path 和 PureWindowsPath，统一校验 Windows/Posix 路径边界。
from pathlib import Path, PureWindowsPath
# 导入 Any，为尚未完成字段校验的 JSON 值提供类型。
from typing import Any

# 导入 M1.1 固定切片函数，保证评测身份与真实入库一致。
from app.rag.chunking import TextChunk, chunk_text, read_utf8_text
# 导入本模块输出的稳定评测值对象。
from app.evaluation.types import (
    AnnotationConfirmation,
    ChunkIdentity,
    ConfirmedCase,
    CorpusFileRecord,
    EvaluationBundle,
    EvaluationCase,
    EvaluationManifest,
)


# 固定当前 manifest schema，未知版本必须显式迁移而不是猜测。
MANIFEST_SCHEMA_VERSION = 1
# 固定 M2.1 允许的四种互斥主分层。
PRIMARY_STRATA = frozenset(
    {"direct-hit", "paraphrase", "multi-clue", "out-of-domain"}
)
# 当前 tags 只表达是否属于资料库范围，避免自由文本造成统计漂移。
ALLOWED_TAGS = frozenset({"in-domain", "out-of-domain"})
# 发布版每个主分层至少需要六条案例。
MIN_CASES_PER_STRATUM = 6
# 发布版总样例数至少为三十。
MIN_RELEASE_CASES = 30
# Top-10 基线要求语料至少有十个唯一文本块。
MIN_RELEASE_CHUNKS = 10


# 计算文件原始字节的 SHA-256 十六进制摘要。
def file_sha256(path: Path) -> str:
    """返回文件内容 hash，不把文件路径写入结果。"""

    # read_bytes 保留换行等原始字节差异，任何改动都会改变 hash。
    return hashlib.sha256(path.read_bytes()).hexdigest()


# 检查一个字符串是否是标准的 SHA-256 十六进制值。
def _require_sha256(value: object, field_name: str) -> str:
    # SHA-256 十六进制文本必须恰好为 64 个字符。
    if not isinstance(value, str) or len(value) != 64:
        # 字段名进入错误消息，方便初学者定位 manifest 配置。
        raise ValueError(f"{field_name} 必须是 64 位 SHA-256 十六进制字符串")
    # 只允许小写十六进制，避免同一摘要出现多种文本形式。
    if any(character not in "0123456789abcdef" for character in value):
        # 非十六进制字符说明配置不是合法 hash。
        raise ValueError(f"{field_name} 必须是小写 SHA-256 十六进制字符串")
    # 返回已经验证的字符串供 dataclass 保存。
    return value


# 检查一个值是否为非空字符串。
def _require_non_empty_string(value: object, field_name: str) -> str:
    # strip 后为空表示字段只有空白，没有实际含义。
    if not isinstance(value, str) or not value.strip():
        # 不做隐式 str 转换，避免 None 等错误配置被掩盖。
        raise ValueError(f"{field_name} 必须是非空字符串")
    # 保留原文本内容；调用方可决定是否需要 strip。
    return value


# 把项目相对路径解析为仍位于项目根目录下的绝对 Path。
def _resolve_project_relative_path(
    project_root: Path,
    relative_value: object,
    field_name: str,
) -> Path:
    # 路径字段首先必须是非空字符串。
    relative_text = _require_non_empty_string(relative_value, field_name)
    # 同时使用 Path 和 PureWindowsPath 识别当前系统与 Windows 盘符。
    relative_path = Path(relative_text)
    # 绝对路径或 Windows 盘符都可能泄露本机目录，必须拒绝。
    if relative_path.is_absolute() or bool(PureWindowsPath(relative_text).drive):
        # 错误消息不回显用户传入的路径内容。
        raise ValueError(f"{field_name} 必须是项目内相对路径")
    # 父目录跳转可能逃逸 project_root，因此明确拒绝 .. 片段。
    if ".." in relative_path.parts:
        # 不允许通过路径规范化偷偷跨出项目目录。
        raise ValueError(f"{field_name} 不能包含父目录跳转")
    # resolve 生成规范绝对路径，后续可以做共同父目录检查。
    resolved_root = project_root.resolve()
    # 将安全相对值拼接到项目根目录。
    resolved_path = (resolved_root / relative_path).resolve()
    # relative_to 成功才证明最终路径仍位于项目根目录内。
    try:
        # 返回值本身不需要保存，只利用它完成边界检查。
        resolved_path.relative_to(resolved_root)
    # ValueError 表示最终路径逃出了 project_root。
    except ValueError as error:
        # 向调用方提供脱敏且可理解的路径错误。
        raise ValueError(f"{field_name} 必须位于项目根目录内") from error
    # 返回已经规范化并确认安全的路径。
    return resolved_path


# 读取 JSON 文件并要求顶层是对象。
def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    # 使用 UTF-8 读取可提交配置。
    raw_text = path.read_text(encoding="utf-8")
    # 交给标准库解析，畸形 JSON 会保留原始异常位置。
    raw_value = json.loads(raw_text)
    # manifest 和 confirmation 顶层必须通过键访问。
    if not isinstance(raw_value, dict):
        # 数组或标量不能表达当前 schema。
        raise ValueError(f"{label} 顶层必须是 JSON 对象")
    # 返回尚待逐字段校验的字典。
    return raw_value


# 把原始 manifest 字典转换为稳定值对象。
def _parse_manifest(raw: dict[str, Any]) -> EvaluationManifest:
    # 当前实现只接受 schema 1，bool 不能冒充整数 1。
    schema_version = raw.get("schema_version")
    # 使用精确类型检查拒绝 True。
    if type(schema_version) is not int or schema_version != MANIFEST_SCHEMA_VERSION:
        # 未知版本不能静默按当前字段解释。
        raise ValueError("manifest schema_version 不受支持")
    # files 必须是非空数组。
    raw_files = raw.get("files")
    # 没有语料文件就无法建立检索评测。
    if not isinstance(raw_files, list) or not raw_files:
        # 明确指出 files 结构错误。
        raise ValueError("manifest files 必须是非空数组")
    # 保存完成逐字段校验的文件记录。
    files: list[CorpusFileRecord] = []
    # 逐项解析来源、许可和 hash。
    for raw_file in raw_files:
        # 每个数组项必须是 JSON 对象。
        if not isinstance(raw_file, dict):
            # 标量无法提供文件字段。
            raise ValueError("manifest files 每一项必须是对象")
        # 创建不可变文件记录。
        files.append(
            CorpusFileRecord(
                path=_require_non_empty_string(raw_file.get("path"), "files.path"),
                sha256=_require_sha256(raw_file.get("sha256"), "files.sha256"),
                source=_require_non_empty_string(
                    raw_file.get("source"),
                    "files.source",
                ),
                license=_require_non_empty_string(
                    raw_file.get("license"),
                    "files.license",
                ),
            )
        )
    # chunk_size 必须是普通正整数。
    chunk_size = raw.get("chunk_size")
    # 精确类型检查避免 bool 冒充整数。
    if type(chunk_size) is not int or chunk_size <= 0:
        # 非正大小无法复现切片。
        raise ValueError("manifest chunk_size 必须是正整数")
    # overlap 必须是普通非负整数且小于 chunk_size。
    overlap = raw.get("overlap")
    # 同时检查类型、下界和上界。
    if type(overlap) is not int or overlap < 0 or overlap >= chunk_size:
        # 与 M1.1 的切片参数契约保持一致。
        raise ValueError("manifest overlap 必须是小于 chunk_size 的非负整数")
    # 返回字段全部完成基础校验的 manifest。
    return EvaluationManifest(
        schema_version=schema_version,
        corpus_version=_require_non_empty_string(
            raw.get("corpus_version"),
            "corpus_version",
        ),
        corpus_root=_require_non_empty_string(raw.get("corpus_root"), "corpus_root"),
        files=tuple(files),
        chunk_size=chunk_size,
        overlap=overlap,
        dataset_version=_require_non_empty_string(
            raw.get("dataset_version"),
            "dataset_version",
        ),
        dataset_path=_require_non_empty_string(
            raw.get("dataset_path"),
            "dataset_path",
        ),
        dataset_sha256=_require_sha256(
            raw.get("dataset_sha256"),
            "dataset_sha256",
        ),
        created=_require_non_empty_string(raw.get("created"), "created"),
    )


# 读取 JSONL，并把每一行解析成 EvaluationCase。
def _load_cases(dataset_path: Path) -> tuple[EvaluationCase, ...]:
    # 保存案例原顺序，便于正式运行使用固定种子重排。
    cases: list[EvaluationCase] = []
    # 记录已见 case_id，拒绝重复身份。
    seen_case_ids: set[str] = set()
    # 按行读取 UTF-8 JSONL。
    for line_number, raw_line in enumerate(
        dataset_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        # 空行可能掩盖意外编辑，JSONL 中明确拒绝。
        if not raw_line.strip():
            # 行号帮助用户定位数据错误。
            raise ValueError(f"dataset 第 {line_number} 行不能为空")
        # 使用标准 JSON 解析每一行。
        raw_case = json.loads(raw_line)
        # 每一行必须是对象。
        if not isinstance(raw_case, dict):
            # 数组和标量都不符合案例 schema。
            raise ValueError(f"dataset 第 {line_number} 行必须是对象")
        # 校验唯一案例编号。
        case_id = _require_non_empty_string(raw_case.get("case_id"), "case_id")
        # 重复案例会在宏平均中重复计权，因此必须失败。
        if case_id in seen_case_ids:
            # 错误只回显非敏感案例编号。
            raise ValueError(f"dataset 存在重复 case_id: {case_id}")
        # 记录当前身份，后续行不得复用。
        seen_case_ids.add(case_id)
        # 问题必须包含实际文本。
        question = _require_non_empty_string(raw_case.get("question"), "question")
        # 主分层必须来自固定枚举。
        primary_stratum = raw_case.get("primary_stratum")
        # 非字符串或未知值都拒绝。
        if primary_stratum not in PRIMARY_STRATA:
            # 不静默归入其他类别，避免分层统计失真。
            raise ValueError("primary_stratum 不受支持")
        # tags 必须是无重复的字符串数组。
        raw_tags = raw_case.get("tags")
        # 空数组不能说明库内或库外。
        if not isinstance(raw_tags, list) or not raw_tags:
            # 使用统一错误说明标签要求。
            raise ValueError("tags 必须是非空数组")
        # 所有 tag 都必须是受控字符串。
        if any(tag not in ALLOWED_TAGS for tag in raw_tags):
            # 自由 tag 会使后续统计口径漂移。
            raise ValueError("tags 包含不受支持的值")
        # 重复 tag 没有额外语义，应作为数据错误拒绝。
        if len(raw_tags) != len(set(raw_tags)):
            # 避免同一标签被重复保存。
            raise ValueError("tags 不能重复")
        # relevant 必须是数组；库外题使用空数组。
        raw_relevant = raw_case.get("relevant")
        # None 或对象都不符合 schema。
        if not isinstance(raw_relevant, list):
            # 明确指出 relevant 的容器类型。
            raise ValueError("relevant 必须是数组")
        # 保存当前题已经验证的相关块身份。
        relevant: list[ChunkIdentity] = []
        # 记录重复 identity。
        seen_relevant: set[ChunkIdentity] = set()
        # 逐项解析来源和块序号。
        for raw_identity in raw_relevant:
            # 每项必须是对象。
            if not isinstance(raw_identity, dict):
                # 标量不能表达复合身份。
                raise ValueError("relevant 每一项必须是对象")
            # 来源名必须是非空字符串。
            source_name = _require_non_empty_string(
                raw_identity.get("source_name"),
                "relevant.source_name",
            )
            # 相关块来源只能是纯文件名。
            if source_name != Path(source_name).name or bool(
                PureWindowsPath(source_name).drive
            ):
                # 路径型来源可能泄露本机结构。
                raise ValueError("relevant.source_name 必须是纯文件名")
            # chunk_index 必须是普通非负整数。
            chunk_index = raw_identity.get("chunk_index")
            # bool 不能冒充第零或第一块。
            if type(chunk_index) is not int or chunk_index < 0:
                # 负数或非整数都无法定位真实块。
                raise ValueError("relevant.chunk_index 必须是非负整数")
            # 创建稳定身份。
            identity = ChunkIdentity(source_name, chunk_index)
            # 同一题重复相关块会错误扩大分母。
            if identity in seen_relevant:
                # 显式失败而不是静默去重。
                raise ValueError("relevant 不能包含重复 identity")
            # 保存唯一性记录和有序列表。
            seen_relevant.add(identity)
            # 保留 JSONL 中的人工排列顺序。
            relevant.append(identity)
        # 库外题必须只有 out-of-domain 标签且 relevant 为空。
        if primary_stratum == "out-of-domain":
            # 库外题不能同时声称库内。
            if raw_tags != ["out-of-domain"] or relevant:
                # 防止空相关集进入错误统计分支。
                raise ValueError("库外题必须只有 out-of-domain 标签且 relevant 为空")
        # 三种库内分层必须带 in-domain 且至少一个相关块。
        elif "in-domain" not in raw_tags or "out-of-domain" in raw_tags or not relevant:
            # 冲突标签或空标注都会破坏 Recall 分母。
            raise ValueError("库内题必须包含 in-domain 标签和至少一个 relevant")
        # 保存当前完整案例。
        cases.append(
            EvaluationCase(
                case_id=case_id,
                question=question,
                relevant=tuple(relevant),
                primary_stratum=primary_stratum,
                tags=tuple(raw_tags),
            )
        )
    # 空 dataset 无法生成任何指标。
    if not cases:
        # 与空行错误区分，说明文件整体没有案例。
        raise ValueError("dataset 至少需要一条案例")
    # 返回不可变案例序列。
    return tuple(cases)


# 按 manifest 读取文件并复用 M1.1 参数重建所有文本块。
def _build_chunks(
    project_root: Path,
    manifest: EvaluationManifest,
) -> tuple[TextChunk, ...]:
    # 解析并校验 corpus_root 的项目边界。
    corpus_root = _resolve_project_relative_path(
        project_root,
        manifest.corpus_root,
        "corpus_root",
    )
    # 保存 manifest 顺序下的全部文本块。
    chunks: list[TextChunk] = []
    # source_name 必须唯一，否则稳定身份会碰撞。
    seen_source_names: set[str] = set()
    # 逐个读取并验证语料文件。
    for file_record in manifest.files:
        # file_record.path 只能相对于 corpus_root。
        corpus_path = _resolve_project_relative_path(
            corpus_root,
            file_record.path,
            "files.path",
        )
        # 评测语料继续只支持 M1.1 的 UTF-8 txt。
        if corpus_path.suffix.lower() != ".txt":
            # 其他格式可能采用不同解析语义。
            raise ValueError("评测语料仅支持 .txt 文件")
        # 纯文件名是稳定 identity 的 source_name。
        source_name = corpus_path.name
        # 不同子目录同名文件会产生 identity 冲突。
        if source_name in seen_source_names:
            # 拒绝碰撞而不是把路径写入 metadata。
            raise ValueError("manifest 不能包含重复 source_name")
        # 记录当前来源名。
        seen_source_names.add(source_name)
        # 文件必须真实存在，read_bytes 会保留标准 FileNotFoundError。
        actual_sha256 = file_sha256(corpus_path)
        # 内容漂移时不能继续使用旧相关性标注。
        if actual_sha256 != file_record.sha256:
            # 不在错误中打印绝对路径。
            raise ValueError(f"语料文件 hash 不匹配: {source_name}")
        # 读取 UTF-8 原文并按 manifest 固定参数切片。
        text = read_utf8_text(corpus_path)
        # 将当前文件所有块追加到全局稳定顺序。
        chunks.extend(
            chunk_text(
                text,
                corpus_path,
                chunk_size=manifest.chunk_size,
                overlap=manifest.overlap,
            )
        )
    # 空语料无法完成检索评测。
    if not chunks:
        # 单个空白文件会被 chunk_text 转为空列表。
        raise ValueError("manifest 语料必须至少生成一个文本块")
    # 返回不可变文本块序列。
    return tuple(chunks)


# 检查案例引用、发布数量和分层下限。
def _validate_bundle_contents(
    chunks: tuple[TextChunk, ...],
    cases: tuple[EvaluationCase, ...],
    *,
    enforce_release_minimums: bool,
) -> None:
    # 把真实 chunk 转换为可快速查找的稳定身份集合。
    available_identities = {
        ChunkIdentity(chunk.source_name, chunk.chunk_index) for chunk in chunks
    }
    # 集合数量不同表示文本块身份发生碰撞。
    if len(available_identities) != len(chunks):
        # 这通常意味着来源名和块序号不唯一。
        raise ValueError("语料包含重复 chunk identity")
    # 逐题确认所有人工标注都指向真实块。
    for case in cases:
        # 找出当前题不存在的相关块。
        missing_identities = set(case.relevant) - available_identities
        # 任何缺失都会让指标答案不可复核。
        if missing_identities:
            # 只回显非敏感 case_id。
            raise ValueError(f"case {case.case_id} 引用了不存在的 chunk identity")
    # 小型单元测试 fixture 不要求凑满发布数据规模。
    if not enforce_release_minimums:
        # 基础 schema 和引用校验已经完成。
        return
    # 正式 Top-10 需要至少十个唯一块。
    if len(chunks) < MIN_RELEASE_CHUNKS:
        # 避免把不足十条结果称为完整 Top-10。
        raise ValueError("发布语料至少需要 10 个唯一文本块")
    # 正式评测集总数至少三十。
    if len(cases) < MIN_RELEASE_CASES:
        # 小样本不足时不能生成正式 baseline。
        raise ValueError("发布评测集至少需要 30 条案例")
    # 逐个主分层核对最低数量。
    for stratum in PRIMARY_STRATA:
        # 统计当前主分层的案例数量。
        stratum_count = sum(case.primary_stratum == stratum for case in cases)
        # 每类少于六条都会使分层覆盖不达标。
        if stratum_count < MIN_CASES_PER_STRATUM:
            # 错误消息指出不足的受控分层名称。
            raise ValueError(f"主分层 {stratum} 至少需要 6 条案例")


# 加载并组合一套经过 schema、hash、切片和引用校验的评测输入。
def load_evaluation_bundle(
    project_root: Path,
    manifest_path: Path,
    dataset_path: Path,
    *,
    enforce_release_minimums: bool = True,
) -> EvaluationBundle:
    """返回可供审阅或评测的输入包；本函数暂不处理人工确认。"""

    # 先解析 manifest 字段。
    manifest = _parse_manifest(_read_json_object(manifest_path, "manifest"))
    # manifest 声明的 dataset 路径必须与调用方提供的入口完全一致。
    expected_dataset_path = _resolve_project_relative_path(
        project_root,
        manifest.dataset_path,
        "dataset_path",
    )
    # resolve 后比较，兼容 Windows 路径分隔符差异。
    if expected_dataset_path != dataset_path.resolve():
        # 禁止调用方用另一份同名数据绕过 manifest。
        raise ValueError("dataset 路径与 manifest 声明不一致")
    # 计算 dataset 实际 hash。
    actual_dataset_sha256 = file_sha256(dataset_path)
    # 内容有任何变化都必须更新 manifest 并重新确认。
    if actual_dataset_sha256 != manifest.dataset_sha256:
        # 不继续加载可能已漂移的标注。
        raise ValueError("dataset hash 与 manifest 不一致")
    # 按 manifest 固定参数重建语料块。
    chunks = _build_chunks(project_root, manifest)
    # 解析 JSONL 案例。
    cases = _load_cases(dataset_path)
    # 统一执行引用和发布规模校验。
    _validate_bundle_contents(
        chunks,
        cases,
        enforce_release_minimums=enforce_release_minimums,
    )
    # 返回已验证输入和两个追溯 hash。
    return EvaluationBundle(
        manifest=manifest,
        chunks=chunks,
        cases=cases,
        manifest_sha256=file_sha256(manifest_path),
        dataset_sha256=actual_dataset_sha256,
    )


# 计算文本按 UTF-8 编码后的 SHA-256，绑定人工看到的精确 chunk。
def _text_sha256(text: str) -> str:
    # 文本 hash 不包含本机路径或其他运行环境信息。
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 生成用户可以逐题阅读的精确切片审阅 Markdown。
def write_annotation_review(
    bundle: EvaluationBundle,
    output_path: Path,
) -> str:
    """写入默认未确认的审阅工件，并返回该文件的 SHA-256。"""

    # 用稳定 identity 映射到精确 TextChunk，避免人工再手算切片边界。
    chunks_by_identity = {
        ChunkIdentity(chunk.source_name, chunk.chunk_index): chunk
        for chunk in bundle.chunks
    }
    # 先写说明和三个会影响人工标注的固定输入。
    lines = [
        "# M2.1 评测标注人工审阅",
        "",
        "> 本文件由程序按固定切片参数生成。所有案例初始均为未确认；",
        "> 阅读并核对不等于程序已经获得确认，正式运行还需要独立 confirmation。",
        "",
        f"- corpus_version：`{bundle.manifest.corpus_version}`",
        f"- dataset_version：`{bundle.manifest.dataset_version}`",
        f"- manifest_sha256：`{bundle.manifest_sha256}`",
        f"- dataset_sha256：`{bundle.dataset_sha256}`",
        f"- chunk_size：`{bundle.manifest.chunk_size}`",
        f"- overlap：`{bundle.manifest.overlap}`",
        "",
    ]
    # 按 JSONL 原顺序输出全部问题，方便用户从头到尾逐题复核。
    for case in bundle.cases:
        # 每题先展示唯一编号和默认未确认状态。
        lines.extend(
            [
                f"## {case.case_id}",
                "",
                "- 状态：未确认",
                f"- 主分层：`{case.primary_stratum}`",
                f"- 标签：`{', '.join(case.tags)}`",
                f"- 问题：{case.question}",
                "",
            ]
        )
        # 库外题没有 relevant，必须明确提示用户核对“资料库确实无答案”。
        if not case.relevant:
            # 单独说明空标注不是程序漏写。
            lines.extend(
                [
                    "- relevant：空；请确认当前语料库确实没有正确文本块。",
                    "",
                ]
            )
            # 当前案例已经完整输出，继续下一题。
            continue
        # 库内题逐个展示所有候选相关块。
        for identity in case.relevant:
            # bundle 加载阶段已验证引用存在，因此这里可直接按键读取。
            chunk = chunks_by_identity[identity]
            # 组合清晰的人读 identity，例如 guide.txt#0。
            identity_text = f"{identity.source_name}#{identity.chunk_index}"
            # 展示身份、文本 hash 和完整切片正文。
            lines.extend(
                [
                    f"### relevant `{identity_text}`",
                    "",
                    f"- chunk_sha256：`{_text_sha256(chunk.text)}`",
                    "",
                    "````text",
                    chunk.text,
                    "````",
                    "",
                ]
            )
    # 确保文件以单个换行结束，跨运行 hash 保持稳定。
    review_text = "\n".join(lines).rstrip() + "\n"
    # 运行时允许创建目标父目录，用户不需要手工搭文件夹。
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # 使用 UTF-8 和显式 \n 写入可提交 Markdown。
    output_path.write_text(review_text, encoding="utf-8", newline="\n")
    # 返回真实文件字节 hash，供后续 confirmation 绑定。
    return file_sha256(output_path)


# 加载并验证人工确认是否精确对应当前 bundle 与审阅文件。
def load_and_validate_confirmation(
    bundle: EvaluationBundle,
    confirmation_path: Path,
    annotation_review_path: Path,
) -> AnnotationConfirmation:
    """只有全部案例逐项确认且三个 hash 一致时才返回确认对象。"""

    # 使用结构化 JSON 解析 confirmation 顶层。
    raw = _read_json_object(confirmation_path, "confirmation")
    # 当前只支持第一版确认 schema，bool 不能冒充版本号。
    schema_version = raw.get("schema_version")
    # 未知版本必须显式迁移。
    if type(schema_version) is not int or schema_version != 1:
        # 避免按错误字段含义读取确认。
        raise ValueError("confirmation schema_version 不受支持")
    # dataset 人读版本必须与 manifest 完全相同。
    dataset_version = _require_non_empty_string(
        raw.get("dataset_version"),
        "confirmation.dataset_version",
    )
    # 版本不同表示用户确认的不是当前问题集。
    if dataset_version != bundle.manifest.dataset_version:
        # 不允许只靠 hash 之外的模糊匹配继续。
        raise ValueError("confirmation dataset_version 不匹配")
    # 校验并读取三个固定 SHA-256。
    dataset_sha256 = _require_sha256(
        raw.get("dataset_sha256"),
        "confirmation.dataset_sha256",
    )
    # manifest hash 字段绑定语料、切片和来源许可。
    manifest_sha256 = _require_sha256(
        raw.get("corpus_manifest_sha256"),
        "confirmation.corpus_manifest_sha256",
    )
    # 审阅 hash 证明用户看到的是当前程序生成的精确切片。
    review_sha256 = _require_sha256(
        raw.get("annotation_review_sha256"),
        "confirmation.annotation_review_sha256",
    )
    # dataset 任何字节变化都会使旧确认失效。
    if dataset_sha256 != bundle.dataset_sha256:
        # 防止确认后修改问题或 relevant。
        raise ValueError("confirmation dataset_sha256 不匹配")
    # manifest 任何变化都会使旧确认失效。
    if manifest_sha256 != bundle.manifest_sha256:
        # 防止确认后切换语料或切片参数。
        raise ValueError("confirmation corpus_manifest_sha256 不匹配")
    # 计算当前审阅文件真实 hash。
    actual_review_sha256 = file_sha256(annotation_review_path)
    # 审阅文件变化后必须重新人工确认。
    if review_sha256 != actual_review_sha256:
        # 不接受过期或被编辑的审阅工件。
        raise ValueError("confirmation annotation_review_sha256 不匹配")
    # confirmed_at 必须提供实际记录时间。
    confirmed_at = _require_non_empty_string(
        raw.get("confirmed_at"),
        "confirmation.confirmed_at",
    )
    # cases 必须是数组，且数量与 dataset 完全相同。
    raw_cases = raw.get("cases")
    # 少一题或多一题都不算完整确认。
    if not isinstance(raw_cases, list) or len(raw_cases) != len(bundle.cases):
        # 明确要求覆盖全部问题。
        raise ValueError("confirmation cases 必须覆盖全部 dataset 案例")
    # 保存已验证逐题确认。
    confirmed_cases: list[ConfirmedCase] = []
    # 按 dataset 原顺序逐项比较，避免确认映射歧义。
    for expected_case, raw_case in zip(bundle.cases, raw_cases, strict=True):
        # 每条确认必须是对象。
        if not isinstance(raw_case, dict):
            # 标量不能表达复合确认内容。
            raise ValueError("confirmation cases 每一项必须是对象")
        # case_id 必须与当前 dataset 顺序和身份完全一致。
        case_id = _require_non_empty_string(
            raw_case.get("case_id"),
            "confirmation.case_id",
        )
        # 不允许跳题或用另一题确认替代。
        if case_id != expected_case.case_id:
            # 提示案例映射不一致，不回显任何路径。
            raise ValueError("confirmation case_id 与 dataset 不一致")
        # confirmed 必须是真正的 JSON true。
        if raw_case.get("confirmed") is not True:
            # 1、字符串 true 和缺失值都不能冒充人工确认。
            raise ValueError(f"case {case_id} 尚未确认")
        # relevant 必须是与 dataset 一一对应的数组。
        raw_relevant = raw_case.get("relevant")
        # 数量必须首先相同。
        if not isinstance(raw_relevant, list) or len(raw_relevant) != len(
            expected_case.relevant
        ):
            # 任何遗漏或新增都使确认失效。
            raise ValueError("confirmation relevant 与 dataset 不一致")
        # 保存当前确认中解析出的身份。
        parsed_relevant: list[ChunkIdentity] = []
        # 按人工标注原顺序逐项读取。
        for expected_identity, raw_identity in zip(
            expected_case.relevant,
            raw_relevant,
            strict=True,
        ):
            # identity 必须是对象。
            if not isinstance(raw_identity, dict):
                # 标量不能表达来源和块序号。
                raise ValueError("confirmation relevant 每一项必须是对象")
            # 来源必须是非空字符串。
            source_name = _require_non_empty_string(
                raw_identity.get("source_name"),
                "confirmation.relevant.source_name",
            )
            # 块序号必须是普通非负整数。
            chunk_index = raw_identity.get("chunk_index")
            # bool 不能作为块序号。
            if type(chunk_index) is not int or chunk_index < 0:
                # 当前确认数据本身无效。
                raise ValueError(
                    "confirmation.relevant.chunk_index 必须是非负整数"
                )
            # 组合当前确认 identity。
            identity = ChunkIdentity(source_name, chunk_index)
            # 必须与用户审阅前冻结的数据逐项一致。
            if identity != expected_identity:
                # 防止 confirmation 自行更改标准答案。
                raise ValueError("confirmation relevant 与 dataset 不一致")
            # 保存完成核对的身份。
            parsed_relevant.append(identity)
        # 保存当前题的不可变确认记录。
        confirmed_cases.append(
            ConfirmedCase(
                case_id=case_id,
                relevant=tuple(parsed_relevant),
                confirmed=True,
            )
        )
    # 返回与三个输入 hash 精确绑定的确认对象。
    return AnnotationConfirmation(
        schema_version=schema_version,
        dataset_version=dataset_version,
        dataset_sha256=dataset_sha256,
        corpus_manifest_sha256=manifest_sha256,
        annotation_review_sha256=review_sha256,
        confirmed_at=confirmed_at,
        cases=tuple(confirmed_cases),
    )
