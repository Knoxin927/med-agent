"""M5.2 负载评测值对象：只保存脱敏、可重算字段。"""

# 导入 dataclass，保证输入/输出在聚合过程中不可被就地改写。
from dataclasses import dataclass


class LoadScenarioId:
    """固定压测场景名，避免拼写漂移。"""

    # 协议健康检查，只验证连通与基线延迟。
    health = "health"
    # 固定 dense /chat/stream 场景。
    dense_chat = "dense-chat"
    # Agent 只读 API/SSE 场景，不触发写操作。
    agent_read_only = "agent-read-only"
    # MCP knowledge-only stdio：initialize/list/call。
    mcp_knowledge = "mcp-knowledge"


class LoadPhase:
    """样本阶段：warmup 不进正式分母，measurement 进入统计。"""

    # 预热请求，用于填满连接/缓存，不计入正式指标。
    warmup = "warmup"
    # 正式测量请求，失败也不能从分母静默删除。
    measurement = "measurement"


# 设计冻结的四个场景。
LOAD_SCENARIO_IDS = (
    LoadScenarioId.health,
    LoadScenarioId.dense_chat,
    LoadScenarioId.agent_read_only,
    LoadScenarioId.mcp_knowledge,
)


@dataclass(frozen=True)
class LoadScenarioIdentity:
    """单个压测场景的冻结身份。"""

    # 场景稳定 ID，必须属于 LOAD_SCENARIO_IDS。
    scenario_id: str
    # 该场景使用的 endpoint 标签；synthetic 可写 fake-local。
    endpoint_ref: str
    # 模型身份；无模型场景可写 not_available。
    model_id: str
    # 语料/工具版本标签。
    corpus_or_tool_version: str
    # 请求 fixture 内容 hash，禁止落盘 query/正文。
    request_fixture_sha256: str


@dataclass(frozen=True)
class LoadManifest:
    """正式负载评测前必须冻结的 manifest。"""

    # 机器可读 schema 版本。
    schema_version: int
    # 显式运行模式：synthetic=工程验证，production=真实证据候选。
    run_mode: str
    # 人读 manifest 版本。
    manifest_version: str
    # 本批压测批次号。
    batch_id: str
    # 负载 raw schema 版本。
    load_schema_version: str
    # 压测工具标签；当前 synthetic 使用 fake-fixed-delay，不安装 Locust。
    tool_name: str
    # 工具版本；未批准安装时写 not_installed。
    tool_version: str
    # 环境标签，例如 local-windows-dev。
    environment_ref: str
    # owner 是否确认真实运行。
    owner_confirmed: bool
    # owner 授权引用；未确认时为空字符串。
    owner_confirmation_ref: str
    # 并发矩阵；production 设计为 1/2/4/8，synthetic 可缩小但仍需显式列出。
    concurrency_levels: tuple[int, ...]
    # 每档预热次数。
    warmup_count: int
    # 每档正式测量次数；也是 sample_count 目标。
    measurement_count: int
    # 统计窗口秒数；用于吞吐 = success_count / window。
    window_seconds: float
    # 场景身份列表。
    scenarios: tuple[LoadScenarioIdentity, ...]
    # raw 文件相对路径。
    raw_path: str
    # raw 文件内容 hash。
    raw_sha256: str


@dataclass(frozen=True)
class LoadRawSample:
    """单次请求的原始样本；禁止包含 query/正文/健康信息/密钥。"""

    # 批次号，与 manifest.batch_id 一致。
    batch_id: str
    # 本 run 的唯一 ID。
    run_id: str
    # 场景 ID。
    scenario_id: str
    # 并发档。
    concurrency: int
    # 在该并发档内的序号，从 1 开始。
    iteration: int
    # warmup 或 measurement。
    phase: str
    # HTTP/协议状态码；stdio 成功可用 0 或 200 约定，这里统一用 int。
    status_code: int
    # 错误码；成功时为 None。
    error_code: str | None
    # 单调时钟开始毫秒。
    start_monotonic_ms: float
    # 单调时钟结束毫秒。
    end_monotonic_ms: float
    # 完整响应延迟毫秒。
    full_latency_ms: float
    # 首 token 延迟毫秒；不适用时为 None。
    first_token_latency_ms: float | None
    # 进程 CPU 百分比采样。
    cpu_pct: float
    # 进程 RSS 内存 MB。
    rss_mb: float


def load_sample_key(
    batch_id: str,
    scenario_id: str,
    concurrency: int,
    iteration: int,
    phase: str,
) -> str:
    """构造 raw 样本去重键。"""

    return f"{batch_id}|{scenario_id}|{concurrency}|{phase}|{iteration}"
