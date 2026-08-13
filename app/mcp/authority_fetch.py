"""M4.3 权威 HTTP 策略：URL/重定向/DNS/IP/MIME/大小边界，transport 只做单跳。"""

# 导入 ipaddress，识别私网/回环等不可路由地址。
import ipaddress
# 导入 socket，生产 resolver 用 getaddrinfo。
import socket
# 导入 ssl，生产 pinned transport 建立 TLS。
import ssl
# 导入 time，计算剩余预算。
import time
# 导入 urlparse，严格解析 URL。
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
# 导入 Sequence，标注 IP 列表。
from collections.abc import Sequence

# 导入值对象与 seam。
from app.mcp.authority_registry import canonicalize_result_url
from app.mcp.authority_types import (
    AUTHORITY_FETCH_TIMEOUT_SECONDS,
    AUTHORITY_MAX_BODY_BYTES,
    AUTHORITY_MAX_REDIRECTS,
    AUTHORITY_MAX_RESULTS,
    AUTHORITY_SNIPPET_MAX_CHARS,
    AUTHORITY_TITLE_MAX_CHARS,
    AuthorityHttpResponse,
    AuthorityResolver,
    AuthoritySearchRequest,
    AuthoritySearchHit,
    AuthoritySource,
    AuthorityTransport,
)


class AuthorityFetchError(Exception):
    """权威抓取业务失败；消息不得包含 URL 或异常正文细节。"""

    def __init__(self, code: str = "business_failure") -> None:
        # code 只允许稳定分类，供上层映射 ToolErrorCode。
        self.code = code
        super().__init__(code)


def host_matches_allowed_suffixes(host: str, suffixes: Sequence[str]) -> bool:
    """判断 host 是否等于或为允许后缀的子域名。"""

    normalized = host.lower().rstrip(".")
    for suffix in suffixes:
        allowed = suffix.lower().rstrip(".")
        if normalized == allowed or normalized.endswith("." + allowed):
            return True
    return False


def is_public_ip(ip_text: str) -> bool:
    """只允许 globally routable 地址；拒绝 loopback/private/link-local 等。"""

    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    # 以标准库的正向 global 判定为准，覆盖 CGNAT、保留段和其他非公网地址。
    return ip.is_global


def validate_authority_url(
    url: str,
    source: AuthoritySource,
    *,
    allow_relative_base: str | None = None,
) -> str:
    """校验并规范化权威 URL：仅 HTTPS、同源后缀、无 userinfo、仅默认 443。"""

    candidate = url
    if allow_relative_base is not None:
        candidate = urljoin(allow_relative_base, url)
    parts = urlsplit(candidate)
    if parts.scheme.lower() != "https":
        raise AuthorityFetchError("business_failure")
    if parts.username is not None or parts.password is not None:
        raise AuthorityFetchError("business_failure")
    host = parts.hostname
    if host is None or not host:
        raise AuthorityFetchError("business_failure")
    # 拒绝 IP literal 作为 host。
    try:
        ipaddress.ip_address(host)
        raise AuthorityFetchError("business_failure")
    except ValueError:
        pass
    if parts.port is not None and parts.port != 443:
        raise AuthorityFetchError("business_failure")
    if not host_matches_allowed_suffixes(host, source.allowed_domain_suffixes):
        raise AuthorityFetchError("business_failure")
    # 规范化：去掉默认端口显示与 fragment。
    netloc = host.lower()
    normalized = urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))
    return normalized


class SystemAuthorityResolver(AuthorityResolver):
    """生产 resolver：getaddrinfo 后过滤非公共 IP。"""

    def resolve(self, host: str) -> Sequence[str]:
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as error:
            raise AuthorityFetchError("transient_failure") from error
        approved: list[str] = []
        seen: set[str] = set()
        for info in infos:
            sockaddr = info[4]
            ip_text = sockaddr[0]
            if ip_text in seen:
                continue
            seen.add(ip_text)
            if is_public_ip(ip_text):
                approved.append(ip_text)
        return approved


class PinnedAuthorityTransport(AuthorityTransport):
    """生产 transport：只拨 approved IP，TLS SNI/Host 使用 origin host，禁止代理。"""

    def fetch(
        self,
        request: AuthoritySearchRequest,
        approved_ips: Sequence[str],
        tls_server_name: str,
        budget_seconds: float,
    ) -> AuthorityHttpResponse:
        if not approved_ips:
            raise AuthorityFetchError("business_failure")
        if budget_seconds <= 0:
            raise AuthorityFetchError("timeout")
        if request.method not in {"GET", "POST"}:
            raise AuthorityFetchError("business_failure")
        parts = urlsplit(request.origin_url)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        # 只尝试第一个 approved IP，避免多路并发；失败直接业务失败。
        ip_text = approved_ips[0]
        deadline = time.monotonic() + min(budget_seconds, AUTHORITY_FETCH_TIMEOUT_SECONDS)
        try:
            raw = socket.create_connection((ip_text, 443), timeout=max(0.01, deadline - time.monotonic()))
        except OSError as error:
            raise AuthorityFetchError("transient_failure") from error
        context = ssl.create_default_context()
        try:
            tls = context.wrap_socket(
                raw,
                server_hostname=tls_server_name,
            )
        except OSError as error:
            raw.close()
            raise AuthorityFetchError("transient_failure") from error
        try:
            # POST body 只由 registry 的固定字段编码，客户端不能追加控制字段。
            form_body = urlencode(request.form_fields).encode("utf-8")
            # 手写最小 HTTP/1.1 请求；不跟随重定向，不使用环境代理。
            request_lines = (
                f"{request.method} {path} HTTP/1.1\r\n"
                f"Host: {tls_server_name}\r\n"
                "User-Agent: med-agent-mcp-authority/0.4\r\n"
                "Accept: text/html\r\n"
                "Connection: close\r\n"
            )
            if request.method == "POST":
                request_lines += (
                    "Content-Type: application/x-www-form-urlencoded\r\n"
                    f"Content-Length: {len(form_body)}\r\n"
                )
            raw_request = (request_lines + "\r\n").encode("ascii") + form_body
            tls.settimeout(max(0.01, deadline - time.monotonic()))
            tls.sendall(raw_request)
            chunks: list[bytes] = []
            total = 0
            while True:
                if time.monotonic() >= deadline:
                    raise AuthorityFetchError("timeout")
                piece = tls.recv(8192)
                if not piece:
                    break
                total += len(piece)
                if total > AUTHORITY_MAX_BODY_BYTES + 4096:
                    # 头部+正文合计远超边界时直接失败；精确拆分在下方完成。
                    raise AuthorityFetchError("business_failure")
                chunks.append(piece)
            raw_response = b"".join(chunks)
        except AuthorityFetchError:
            raise
        except OSError as error:
            raise AuthorityFetchError("transient_failure") from error
        finally:
            try:
                tls.close()
            except OSError:
                pass
        return _parse_raw_http_response(raw_response)


def _parse_raw_http_response(raw: bytes) -> AuthorityHttpResponse:
    """把原始 HTTP 响应拆成 status/headers/body，并强制 1 MiB body 边界。"""

    header_end = raw.find(b"\r\n\r\n")
    if header_end < 0:
        raise AuthorityFetchError("business_failure")
    header_blob = raw[:header_end].decode("iso-8859-1", errors="replace")
    body = raw[header_end + 4 :]
    lines = header_blob.split("\r\n")
    if not lines:
        raise AuthorityFetchError("business_failure")
    status_line = lines[0]
    try:
        # HTTP/1.1 200 OK
        status_code = int(status_line.split(" ")[1])
    except (IndexError, ValueError) as error:
        raise AuthorityFetchError("business_failure") from error
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    content_type = headers.get("content-type")
    if content_type is not None:
        content_type = content_type.split(";")[0].strip().lower()
    content_length = None
    if "content-length" in headers:
        try:
            content_length = int(headers["content-length"])
        except ValueError as error:
            raise AuthorityFetchError("business_failure") from error
        if content_length > AUTHORITY_MAX_BODY_BYTES:
            raise AuthorityFetchError("business_failure")
    if len(body) > AUTHORITY_MAX_BODY_BYTES:
        raise AuthorityFetchError("business_failure")
    location = headers.get("location")
    return AuthorityHttpResponse(
        status_code=status_code,
        content_type=content_type,
        content_length=content_length,
        location=location,
        body=body,
    )


class AuthorityFetchPolicy:
    """唯一 redirect loop owner：校验 URL/DNS、调用单跳 transport、再验证结果链接。"""

    def __init__(
        self,
        resolver: AuthorityResolver,
        transport: AuthorityTransport,
    ) -> None:
        self._resolver = resolver
        self._transport = transport

    def fetch_html(
        self,
        source: AuthoritySource,
        start_request: AuthoritySearchRequest,
        *,
        budget_seconds: float,
    ) -> tuple[str, str]:
        """抓取最终 200 HTML，返回 (final_url, html_text)。"""

        current_url = validate_authority_url(start_request.origin_url, source)
        current_request = AuthoritySearchRequest(
            method=start_request.method,
            origin_url=current_url,
            form_fields=start_request.form_fields,
        )
        seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
        redirects = 0
        remaining = min(budget_seconds, AUTHORITY_FETCH_TIMEOUT_SECONDS)
        started = time.monotonic()
        while True:
            if remaining <= 0:
                raise AuthorityFetchError("timeout")
            request_identity = (
                current_request.method,
                current_request.origin_url,
                current_request.form_fields,
            )
            if request_identity in seen:
                raise AuthorityFetchError("business_failure")
            seen.add(request_identity)
            host = urlsplit(current_request.origin_url).hostname
            if host is None:
                raise AuthorityFetchError("business_failure")
            approved = list(self._resolver.resolve(host))
            # 再次过滤，防止 fake resolver 偷渡私网 IP。
            approved = [ip for ip in approved if is_public_ip(ip)]
            if not approved:
                raise AuthorityFetchError("business_failure")
            hop_budget = max(0.01, remaining - (time.monotonic() - started))
            response = self._transport.fetch(current_request, approved, host, hop_budget)
            # Content-Length 预检：读取前已由 transport/parser 拒绝超大值。
            if response.content_length is not None and response.content_length > AUTHORITY_MAX_BODY_BYTES:
                raise AuthorityFetchError("business_failure")
            if response.status_code in {301, 302, 303, 307, 308}:
                if redirects >= AUTHORITY_MAX_REDIRECTS:
                    raise AuthorityFetchError("business_failure")
                if not response.location:
                    raise AuthorityFetchError("business_failure")
                # 为避免 POST body 被重定向到不同页面，POST 搜索请求不接受重定向。
                if current_request.method != "GET":
                    raise AuthorityFetchError("business_failure")
                # 相对 Location 以当前 URL 为基准；目标必须再次过同源规则。
                redirect_url = validate_authority_url(
                    response.location,
                    source,
                    allow_relative_base=current_request.origin_url,
                )
                current_request = AuthoritySearchRequest("GET", redirect_url)
                redirects += 1
                remaining = min(budget_seconds, AUTHORITY_FETCH_TIMEOUT_SECONDS) - (
                    time.monotonic() - started
                )
                continue
            if response.status_code != 200:
                raise AuthorityFetchError("business_failure")
            # design：只接受 text/html；缺省或漂移一律 fail-closed。
            if response.content_type != "text/html":
                raise AuthorityFetchError("business_failure")
            try:
                html_text = response.body.decode("utf-8")
            except UnicodeDecodeError as error:
                raise AuthorityFetchError("business_failure") from error
            return current_request.origin_url, html_text

    def revalidate_hits(
        self,
        source: AuthoritySource,
        search_url: str,
        hits: list[AuthoritySearchHit],
    ) -> list[AuthoritySearchHit]:
        """对提取结果做 URL 再验证、去重与字段截断；不合规单条丢弃。"""

        cleaned: list[AuthoritySearchHit] = []
        seen_urls: set[str] = set()
        for hit in hits:
            try:
                url = validate_authority_url(hit.url, source, allow_relative_base=search_url)
            except AuthorityFetchError:
                continue
            # 公开给客户端前再次解析结果链接的 host；私网或不可解析目标不能进入 payload。
            result_host = urlsplit(url).hostname
            if result_host is None:
                continue
            # resolver 返回值仍要二次过滤，防止实现或 fake seam 偷渡非公网地址。
            result_ips = [ip for ip in self._resolver.resolve(result_host) if is_public_ip(ip)]
            if not result_ips:
                continue
            canonical = canonicalize_result_url(url)
            if not canonical or canonical in seen_urls:
                continue
            seen_urls.add(canonical)
            title = (hit.title or "").strip()
            snippet = (hit.snippet or "").strip()
            if not title:
                continue
            if len(title) > AUTHORITY_TITLE_MAX_CHARS:
                title = title[:AUTHORITY_TITLE_MAX_CHARS]
            if len(snippet) > AUTHORITY_SNIPPET_MAX_CHARS:
                snippet = snippet[:AUTHORITY_SNIPPET_MAX_CHARS]
            cleaned.append(
                AuthoritySearchHit(
                    source_id=source.source_id,
                    source_name=source.source_name,
                    title=title,
                    url=canonical,
                    snippet=snippet or title,
                )
            )
            if len(cleaned) >= AUTHORITY_MAX_RESULTS:
                break
        return cleaned
