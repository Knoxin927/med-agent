"""M4.3 权威检索值对象：只描述来源、命中与单跳 HTTP 响应，不含原始 HTML 策略细节。"""

# 导入 ABC，定义 resolver/transport 可替换 seam。
from abc import ABC, abstractmethod
# 导入 dataclass，定义不可变值对象。
from dataclasses import dataclass
# 导入 Callable，声明 HTML 提取器类型。
from collections.abc import Callable, Sequence


# 初始允许的 source_id 白名单；公开 schema 与 registry 都必须使用同一集合。
ALLOWED_AUTHORITY_SOURCE_IDS: frozenset[str] = frozenset({"who", "nhc", "chinacdc"})
# 单次公开结果最多 5 条，客户端不可调。
AUTHORITY_MAX_RESULTS = 5
# title 最多 200 个 Unicode 字符。
AUTHORITY_TITLE_MAX_CHARS = 200
# snippet 最多 800 个 Unicode 字符。
AUTHORITY_SNIPPET_MAX_CHARS = 800
# query strip 后最短 1 字符。
AUTHORITY_QUERY_MIN_LENGTH = 1
# query strip 后最长 256 字符。
AUTHORITY_QUERY_MAX_LENGTH = 256
# 响应体硬上限 1 MiB。
AUTHORITY_MAX_BODY_BYTES = 1 * 1024 * 1024
# 单次 fetch 超时上限 5 秒；仍受 ToolRuntime deadline 约束。
AUTHORITY_FETCH_TIMEOUT_SECONDS = 5.0
# 最多跟随 2 次重定向（合计最多 3 跳请求，含初始）。
AUTHORITY_MAX_REDIRECTS = 2


@dataclass(frozen=True)
class AuthoritySearchHit:
    """单条 MCP-only 权威命中；不携带原始 HTML、响应头或重定向链。"""

    # 来源 ID：who / nhc / chinacdc。
    source_id: str
    # 人类可读来源名，例如 WHO。
    source_name: str
    # 标题，最多 200 字符。
    title: str
    # 已通过同源 HTTPS 再验证的 canonical URL。
    url: str
    # 摘要，最多 800 字符。
    snippet: str


@dataclass(frozen=True)
class AuthoritySearchPayload:
    """ToolObservation 的权威检索窄 payload；允许空 hits 列表。"""

    # 有序命中列表；空列表表示成功但无结果。
    hits: tuple[AuthoritySearchHit, ...]


@dataclass(frozen=True)
class AuthoritySource:
    """服务端拥有的单个权威来源定义；生产 entry 必须 verified=True。"""

    # 稳定 source_id。
    source_id: str
    # 显示名。
    source_name: str
    # 允许的 HTTPS 域后缀，例如 ("who.int",)。
    allowed_domain_suffixes: tuple[str, ...]
    # 固定站内搜索入口模板；GET 必须包含 {query}，POST 不允许该占位。
    search_url_template: str
    # 提取策略名，registry 内映射到固定 extractor。
    extractor_id: str
    # 服务端固定请求方法，只允许 GET 或 POST，客户端不能控制。
    request_method: str = "GET"
    # POST 时承载 query 的固定表单字段名；GET 时不参与 URL 模板替换。
    query_field: str = "query"
    # POST 时附带的固定表单字段，不能由客户端覆盖或追加。
    fixed_form_fields: tuple[tuple[str, str], ...] = ()
    # 是否已获独立网络授权并完成入口核验。
    verified: bool = False


@dataclass(frozen=True)
class AuthoritySearchRequest:
    """服务端构造的单跳搜索请求；公开调用者无法提供其中任一字段。"""

    # 请求方法已由 registry 固定为 GET 或 POST。
    method: str
    # 已编码的初始官方 URL，仍需由 fetch policy 做 URL/DNS 校验。
    origin_url: str
    # POST 的固定表单字段；GET 时为空元组。
    form_fields: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class AuthorityHttpResponse:
    """单跳 HTTP 响应：transport 禁止自动重定向，只回传本跳边界字段。"""

    # HTTP 状态码。
    status_code: int
    # 规范化后的 Content-Type 主类型，例如 text/html；未知为 None。
    content_type: str | None
    # Content-Length；未知为 None。
    content_length: int | None
    # Location 头原文；无重定向时为 None。
    location: str | None
    # 已受 1 MiB 边界保护的响应体字节。
    body: bytes


class AuthorityResolver(ABC):
    """把主机名解析为已批准的公共 IP 列表；生产实现必须拒绝私网地址。"""

    @abstractmethod
    def resolve(self, host: str) -> Sequence[str]:
        """返回该 host 的公共 IP 列表；无可用地址时返回空序列。"""


class AuthorityTransport(ABC):
    """单跳抓取 seam：只拨 approved_ips，保留 origin 的 TLS SNI/Host。"""

    @abstractmethod
    def fetch(
        self,
        request: AuthoritySearchRequest,
        approved_ips: Sequence[str],
        tls_server_name: str,
        budget_seconds: float,
    ) -> AuthorityHttpResponse:
        """执行一次不跟随重定向的请求，返回受大小边界保护的单跳响应。"""


# HTML 提取器：输入来源与搜索基准 URL、HTML 文本，输出候选命中（URL 仍需再验证）。
AuthorityExtractor = Callable[[AuthoritySource, str, str], list[AuthoritySearchHit]]
