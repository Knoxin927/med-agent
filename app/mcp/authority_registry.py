"""M4.3 权威来源 registry：source_id allowlist 与固定站内搜索入口。"""

# 导入 html/re/urlparse，实现测试用 extractor 与 URL 拼接。
import html
import re
from html.parser import HTMLParser
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit

# 导入权威值对象与提取器类型。
from app.mcp.authority_types import (
    ALLOWED_AUTHORITY_SOURCE_IDS,
    AuthorityExtractor,
    AuthoritySearchRequest,
    AuthoritySearchHit,
    AuthoritySource,
)


class AuthoritySourceRegistry:
    """AuthoritySource 的唯一注册表；未知 source_id 在查找前失败。"""

    def __init__(
        self,
        sources: list[AuthoritySource],
        extractors: dict[str, AuthorityExtractor] | None = None,
    ) -> None:
        # 按 source_id 索引，保证 O(1) 查找。
        registry: dict[str, AuthoritySource] = {}
        for source in sources:
            if source.source_id not in ALLOWED_AUTHORITY_SOURCE_IDS:
                raise ValueError(f"不允许的 source_id: {source.source_id}")
            if source.source_id in registry:
                raise ValueError(f"重复的 source_id: {source.source_id}")
            if not source.allowed_domain_suffixes:
                raise ValueError(f"source 缺少域后缀: {source.source_id}")
            if source.request_method not in {"GET", "POST"}:
                raise ValueError(f"不支持的搜索方法: {source.source_id}")
            if source.request_method == "GET" and "{query}" not in source.search_url_template:
                raise ValueError(f"GET 搜索模板必须包含 {{query}}: {source.source_id}")
            if source.request_method == "POST" and "{query}" in source.search_url_template:
                raise ValueError(f"POST 搜索 URL 不得包含 {{query}}: {source.source_id}")
            if source.request_method == "POST" and not source.query_field:
                raise ValueError(f"POST 搜索缺少 query 字段: {source.source_id}")
            registry[source.source_id] = source
        self._sources = registry
        # extractor 可注入；缺省时使用通用链接提取器，仅供 fake/测试。
        self._extractors = dict(extractors or {})

    def get(self, source_id: str) -> AuthoritySource | None:
        """按 source_id 查找；未命中返回 None，不猜测近似名。"""

        return self._sources.get(source_id)

    def require(self, source_id: str) -> AuthoritySource:
        """要求 source 存在；否则抛出稳定业务异常。"""

        source = self.get(source_id)
        if source is None:
            raise ValueError("未知权威来源")
        return source

    def list_source_ids(self) -> list[str]:
        """返回稳定排序的 source_id 列表。"""

        return sorted(self._sources)

    def build_search_request(self, source: AuthoritySource, query: str) -> AuthoritySearchRequest:
        """用服务端配置构造 GET 或 POST 请求；客户端只能贡献已校验 query。"""

        # GET 的 query 仅进入 URL 中预置的 {query} 占位，不能影响 host/path。
        if source.request_method == "GET":
            encoded = quote_plus(query, safe="")
            return AuthoritySearchRequest(
                method="GET",
                origin_url=source.search_url_template.format(query=encoded),
            )
        # POST 的 URL 与静态字段完全来自 registry，query 只填入指定表单字段。
        return AuthoritySearchRequest(
            method="POST",
            origin_url=source.search_url_template,
            form_fields=source.fixed_form_fields + ((source.query_field, query),),
        )

    def extract_hits(
        self,
        source: AuthoritySource,
        search_url: str,
        html_text: str,
    ) -> list[AuthoritySearchHit]:
        """按 source.extractor_id 提取候选命中；未知 extractor 失败。"""

        extractor = self._extractors.get(source.extractor_id)
        if extractor is None:
            raise ValueError("未知提取策略")
        return extractor(source, search_url, html_text)


def build_fake_authority_registry() -> AuthoritySourceRegistry:
    """构造离线测试用 registry：三条假入口 + 通用链接提取器。"""

    sources = [
        AuthoritySource(
            source_id="who",
            source_name="WHO",
            allowed_domain_suffixes=("who.int",),
            search_url_template="https://www.who.int/search?q={query}",
            extractor_id="generic_anchor",
            verified=True,
        ),
        AuthoritySource(
            source_id="nhc",
            source_name="国家卫健委",
            allowed_domain_suffixes=("nhc.gov.cn",),
            search_url_template="https://www.nhc.gov.cn/search?q={query}",
            extractor_id="generic_anchor",
            verified=True,
        ),
        AuthoritySource(
            source_id="chinacdc",
            source_name="中国疾控中心",
            allowed_domain_suffixes=("chinacdc.cn",),
            search_url_template="https://www.chinacdc.cn/was5/web/search",
            extractor_id="generic_anchor",
            request_method="POST",
            query_field="searchword",
            fixed_form_fields=(("prepage", "30"),),
            verified=True,
        ),
    ]
    return AuthoritySourceRegistry(
        sources,
        extractors={"generic_anchor": extract_generic_anchor_hits},
    )


def build_production_authority_registry() -> AuthoritySourceRegistry:
    """生产 registry：当前缺少独立授权的真实入口，必须启动失败。

    design 要求：未联网核验前不得猜测真实端点或 selector。
    调用方应捕获并改写为固定 McpAuthorityStartupError。
    """

    raise RuntimeError("production authority entries are not verified")


class _AnchorParser(HTMLParser):
    """极简 HTML a 标签解析器：只收集 href 与锚文本。"""

    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = None
        for key, value in attrs:
            if key.lower() == "href" and value is not None:
                href = value
                break
        self._current_href = href
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current_href is None:
            return
        title = html.unescape("".join(self._parts)).strip()
        title = re.sub(r"\s+", " ", title)
        self.anchors.append((self._current_href, title))
        self._current_href = None
        self._parts = []


def extract_generic_anchor_hits(
    source: AuthoritySource,
    search_url: str,
    html_text: str,
) -> list[AuthoritySearchHit]:
    """通用 a[href] 提取：只产出候选 hit，URL 再验证由 fetch policy 完成。

    这是 fake/测试提取器，不是真实 WHO/NHC/CDC 页面结构承诺。
    """

    parser = _AnchorParser()
    parser.feed(html_text)
    parser.close()
    hits: list[AuthoritySearchHit] = []
    for href, title in parser.anchors:
        # 空 href 直接丢弃。
        if not href or not href.strip():
            continue
        # 用搜索页 URL 作基准解析相对链接；fragment 在后续 policy 再剥离。
        absolute = urljoin(search_url, href.strip())
        # 暂时保留完整 URL 字符串；snippet 用 title 兜底，避免空摘要。
        display_title = title or absolute
        hits.append(
            AuthoritySearchHit(
                source_id=source.source_id,
                source_name=source.source_name,
                title=display_title,
                url=absolute,
                snippet=display_title,
            )
        )
    return hits


def canonicalize_result_url(url: str) -> str:
    """去掉 fragment，保留 scheme/netloc/path/query，供结果 URL 去重。"""

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
