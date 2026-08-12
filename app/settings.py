"""加载 M1.4 运行时配置，并确保密钥只来自本机环境。"""

# 导入 os，用于读取进程环境变量。
import os
# 导入 math，验证浮点超时既是正数也是有限数。
import math
# 导入 dataclass，定义不可变、类型明确的配置对象。
from dataclasses import dataclass
# 导入 Path，表示本机 Chroma 数据目录。
from pathlib import Path
# 导入 urlparse，校验上游基础地址是 HTTP URL。
from urllib.parse import urlparse

# 导入 dotenv_values，以结构化方式读取项目 .env，避免缺字段时误用终端同名变量。
from dotenv import dotenv_values

# 保存用户确认的 DeepSeek OpenAI-compatible 基础地址。
DEFAULT_LLM_BASE_URL = "https://api.deepseek.com"
# 保存用户确认的 DeepSeek 快速模型 ID。
DEFAULT_LLM_MODEL = "deepseek-v4-flash"
# 保存 M2.5 已确认的聊天默认与回滚检索方法。
DEFAULT_RETRIEVAL_METHOD = "hybrid"
# 生产/Compose 默认走 Postgres；单测可显式切 memory。
DEFAULT_AGENT_STORE = "postgres"
# 本机 Compose 把 Postgres 映射到 5433；容器内服务名仍是 postgres:5432。
DEFAULT_AGENT_DATABASE_HOST = "127.0.0.1"
DEFAULT_AGENT_DATABASE_PORT = "5433"
DEFAULT_AGENT_DATABASE_NAME = "med_agent"
DEFAULT_AGENT_DATABASE_USER = "med_agent"
# 固定本项目的根目录，避免从终端当前目录误加载其他工具的 .env。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 本文件是医疗 Agent 唯一允许读取的本地运行时环境配置。
PROJECT_ENV_PATH = PROJECT_ROOT / ".env"


# 表示配置缺失或格式错误；路由会将它转换为脱敏 JSON 错误。
class SettingsError(ValueError):
    """运行时配置不满足 M1.4 契约。"""


# 保存已经校验过、可安全传给应用内部的非密钥配置和密钥值。
@dataclass(frozen=True)
class AppSettings:
    # 保存 OpenAI-compatible 服务的基础地址。
    llm_base_url: str
    # 保存用户确认的模型 ID。
    llm_model: str
    # 保存只在内存中使用的真实 API Key。
    llm_api_key: str
    # 保存单次上游请求的超时秒数。
    llm_timeout_seconds: float
    # 保存本机 Chroma 的持久化目录。
    rag_chroma_path: Path
    # 保存通过验证的聊天检索方法，M7 实测后仅允许 hybrid。
    retrieval_method: str
    # 保存 Agent run/approval 持久化后端：memory 或 postgres。
    agent_store: str
    # 仅 postgres 模式需要；memory 模式为 None，且永不写入日志。
    agent_database_dsn: str | None


# 从环境变量或本机 .env 读取并校验 M1.4 配置。
def load_settings() -> AppSettings:
    # 只在项目 .env 实际存在时将其作为完整 LLM 配置源，防止终端同名变量补齐漏项。
    project_env_values = dotenv_values(PROJECT_ENV_PATH) if PROJECT_ENV_PATH.is_file() else None
    # 项目 .env 存在时，LLM 字段只从该文件或项目默认值读取，保证与终端工具隔离。
    if project_env_values is not None:
        # 缺省地址使用本项目确认的 DeepSeek 地址，显式空值仍会在后续校验时失败。
        base_url_text = project_env_values.get("LLM_BASE_URL")
        base_url = (DEFAULT_LLM_BASE_URL if base_url_text is None else base_url_text).strip()
        # 缺省模型使用本项目确认的 Flash 模型，不能继承终端模型。
        model_text = project_env_values.get("LLM_MODEL")
        model = (DEFAULT_LLM_MODEL if model_text is None else model_text).strip()
        # 密钥只能来自项目 .env，缺失时必须在网络访问前失败。
        api_key = (project_env_values.get("LLM_API_KEY") or "").strip()
        # 项目未设置超时时使用项目默认值，不能继承终端的请求策略。
        timeout_value = project_env_values.get("LLM_TIMEOUT_SECONDS")
        timeout_text = ("120" if timeout_value is None else timeout_value).strip()
        # 项目 .env 存在时检索方法同样不能继承终端实验策略配置。
        retrieval_method_value = project_env_values.get("RETRIEVAL_METHOD")
        retrieval_method = (DEFAULT_RETRIEVAL_METHOD if retrieval_method_value is None else retrieval_method_value).strip()
        # Agent 持久化后端：项目 .env 优先，缺省走生产默认 postgres。
        agent_store_value = project_env_values.get("AGENT_STORE")
        agent_store = (DEFAULT_AGENT_STORE if agent_store_value is None else agent_store_value).strip().lower()
        # DSN 可直接给，或由 POSTGRES_* 在后续拼装；不继承终端同名密钥。
        agent_database_dsn_value = project_env_values.get("AGENT_DATABASE_DSN")
        agent_database_dsn = None if agent_database_dsn_value is None else agent_database_dsn_value.strip() or None
        postgres_password = (project_env_values.get("POSTGRES_PASSWORD") or "").strip()
        postgres_host = (project_env_values.get("POSTGRES_HOST") or DEFAULT_AGENT_DATABASE_HOST).strip()
        postgres_port = (project_env_values.get("POSTGRES_PORT") or DEFAULT_AGENT_DATABASE_PORT).strip()
        postgres_db = (project_env_values.get("POSTGRES_DB") or DEFAULT_AGENT_DATABASE_NAME).strip()
        postgres_user = (project_env_values.get("POSTGRES_USER") or DEFAULT_AGENT_DATABASE_USER).strip()
    # 没有项目 .env 的容器或 CI 环境仍可显式通过进程环境注入完整配置。
    else:
        # 读取基础地址并去除意外空白；未填写时使用本阶段确认的默认地址。
        base_url = os.getenv("LLM_BASE_URL", DEFAULT_LLM_BASE_URL).strip()
        # 读取模型名称并去除意外空白；未填写时使用本阶段确认的默认模型 ID。
        model = os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL).strip()
        # 读取密钥但绝不打印、记录或放进异常消息。
        api_key = os.getenv("LLM_API_KEY", "").strip()
        # 读取超时字符串，默认使用当前中转站允许的 120 秒。
        timeout_text = os.getenv("LLM_TIMEOUT_SECONDS", "120").strip()
        # 容器或 CI 没有项目 .env 时可显式注入唯一允许的 hybrid 配置。
        retrieval_method = os.getenv("RETRIEVAL_METHOD", DEFAULT_RETRIEVAL_METHOD).strip()
        # 容器默认 postgres；单测可通过 AGENT_STORE=memory 隔离。
        agent_store = os.getenv("AGENT_STORE", DEFAULT_AGENT_STORE).strip().lower()
        agent_database_dsn = (os.getenv("AGENT_DATABASE_DSN") or "").strip() or None
        postgres_password = (os.getenv("POSTGRES_PASSWORD") or "").strip()
        postgres_host = (os.getenv("POSTGRES_HOST") or DEFAULT_AGENT_DATABASE_HOST).strip()
        postgres_port = (os.getenv("POSTGRES_PORT") or DEFAULT_AGENT_DATABASE_PORT).strip()
        postgres_db = (os.getenv("POSTGRES_DB") or DEFAULT_AGENT_DATABASE_NAME).strip()
        postgres_user = (os.getenv("POSTGRES_USER") or DEFAULT_AGENT_DATABASE_USER).strip()
    # 读取 Chroma 路径，默认与既有 M1.2 目录一致。
    chroma_path = Path(os.getenv("RAG_CHROMA_PATH", "data/chroma"))
    # 空白覆盖值不能构造上游请求。
    if not base_url:
        raise SettingsError("LLM_BASE_URL 不能为空")
    # 空白覆盖值不能让上游服务猜测模型。
    if not model:
        raise SettingsError("LLM_MODEL 不能为空")
    # 空白或未批准实验策略都不能进入生产聊天链路。
    if retrieval_method != DEFAULT_RETRIEVAL_METHOD:
        raise SettingsError("RETRIEVAL_METHOD 仅允许 hybrid")
    # Agent 持久化只允许 memory/postgres 两个显式后端。
    if agent_store not in {"memory", "postgres"}:
        raise SettingsError("AGENT_STORE 仅允许 memory 或 postgres")
    # 缺密钥时必须在网络访问前失败。
    if not api_key or api_key.startswith("<"):
        raise SettingsError("缺少 LLM_API_KEY 配置")
    # 解析 URL，拒绝没有协议或主机的无效配置。
    parsed_url = urlparse(base_url)
    # 只接受 HTTP/HTTPS，避免将密钥发送到不受支持的协议。
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise SettingsError("LLM_BASE_URL 必须是有效的 HTTP 地址")
    # 将超时文本转换为浮点数。
    try:
        timeout_seconds = float(timeout_text)
    # 无法转换时给出不含密钥的配置错误。
    except ValueError as error:
        raise SettingsError("LLM_TIMEOUT_SECONDS 必须是正数") from error
    # 零、负数、nan 或无穷大都不是可预测的网络超时配置。
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise SettingsError("LLM_TIMEOUT_SECONDS 必须是有限正数")
    # postgres 模式必须有可连接 DSN；可直接给，或由本机 POSTGRES_* 拼装。
    if agent_store == "postgres":
        if not agent_database_dsn:
            if not postgres_password or postgres_password.startswith("<") or postgres_password == "change-me":
                raise SettingsError("postgres 模式需要 AGENT_DATABASE_DSN 或有效的 POSTGRES_PASSWORD")
            if not postgres_host or not postgres_port or not postgres_db or not postgres_user:
                raise SettingsError("postgres 模式的数据库连接参数不完整")
            # 仅内存拼装连接串；异常与日志不得回显密码。
            agent_database_dsn = (
                f"postgresql://{postgres_user}:{postgres_password}"
                f"@{postgres_host}:{postgres_port}/{postgres_db}"
            )
    else:
        agent_database_dsn = None
    # 返回完整且已经校验的配置快照。
    return AppSettings(
        base_url.rstrip("/"),
        model,
        api_key,
        timeout_seconds,
        chroma_path,
        retrieval_method,
        agent_store,
        agent_database_dsn,
    )
