"""READ-ONLY EXTERNAL INFORMATION CAPABILITY V0 -- network boundary.

Two pure, dependency-injectable, stdlib-only operations:

  fetch_public_url()  -- GET-only, SSRF-safe, bounded retrieval of one
                          public text/HTML resource.
  search_web()         -- a single bounded read-only call to the fixed
                           Brave Search REST endpoint.  Tests may inject
                           a fake transport/endpoint, but production
                           credentials are never redirected by config.

Matches this codebase's own established network convention (see
llama_anaxi.py's Ollama version probe, llama_launch.py's readiness
checks): stdlib `urllib`/`socket`/`ipaddress` only, always an explicit
timeout, every exception classified into a closed status vocabulary
and returned as a typed dict -- never a raw exception escaping to a
canonical write path, never a fabricated result on failure. Search
provider selection is explicit and never silent: a configured Brave key
selects Brave; with no key the keyless public DuckDuckGo HTML endpoint is
used and every result names its own provider.

SSRF defense-in-depth: scheme is restricted to http/https; the
hostname is rejected outright for known local aliases (localhost,
*.local); a literal IP host is validated directly; a DNS name is
resolved (via an injectable resolver, defaulting to
socket.getaddrinfo) and EVERY resolved address is validated before the
request is issued.  The production connection is then made directly to
one of those exact validated addresses while the original hostname is
preserved for HTTP Host and HTTPS SNI/certificate verification. Redirects
are never auto-followed by the HTTP
client -- this module disables that entirely and re-validates each
redirect target itself, exactly like the very first hop, before ever
following it, so a public URL cannot redirect its way to a blocked
target.  The validated address is the address actually connected to;
there is no second hostname resolution at connect time.

Every request/response object used by this module can be replaced by
an injected callable (`resolve_fn`, `opener_fn`, `http_get_fn`) so the
permanent test suite never performs a real DNS lookup or a real
network call.
"""
import http.client
import ipaddress
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

# --------------------------------------------------------------- bounds

DEFAULT_TIMEOUT_SECONDS = 10
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 2_000_000
MAX_EXTRACTED_TEXT_CHARS = 20_000
MAX_SEARCH_QUERY_CHARS = 400
MAX_SEARCH_RESULTS = 5
MAX_SEARCH_RESPONSE_BYTES = 1_000_000

ALLOWED_SCHEMES = frozenset({"http", "https"})
SUPPORTED_CONTENT_TYPES = frozenset({"text/html", "text/plain"})

_BLOCKED_HOSTNAME_SUFFIXES = (".local",)
_BLOCKED_HOSTNAME_EXACT = frozenset({"localhost"})

# ------------------------------------------------------------ fetch statuses

FETCH_STATUS_SUCCESS = "success"
FETCH_STATUS_TRUNCATED = "truncated_success"
FETCH_STATUS_BLOCKED_URL = "blocked_url"
FETCH_STATUS_HTTP_FAILURE = "http_failure"
FETCH_STATUS_NETWORK_FAILURE = "network_failure"
FETCH_STATUS_UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
FETCH_STATUS_EXTRACTION_FAILED = "extraction_failed"
FETCH_STATUS_TOO_MANY_REDIRECTS = "too_many_redirects"

FETCH_FAILURE_STATUSES = frozenset({
    FETCH_STATUS_BLOCKED_URL, FETCH_STATUS_HTTP_FAILURE, FETCH_STATUS_NETWORK_FAILURE,
    FETCH_STATUS_UNSUPPORTED_CONTENT_TYPE, FETCH_STATUS_EXTRACTION_FAILED,
    FETCH_STATUS_TOO_MANY_REDIRECTS,
})

# ----------------------------------------------------------- search statuses

SEARCH_STATUS_SUCCESS = "success"
SEARCH_STATUS_NOT_CONFIGURED = "not_configured"
SEARCH_STATUS_PROVIDER_FAILURE = "provider_failure"
SEARCH_STATUS_MALFORMED_RESPONSE = "malformed_response"
SEARCH_STATUS_NETWORK_FAILURE = "network_failure"
SEARCH_STATUS_INVALID_QUERY = "invalid_query"

SEARCH_FAILURE_STATUSES = frozenset({
    SEARCH_STATUS_NOT_CONFIGURED, SEARCH_STATUS_PROVIDER_FAILURE,
    SEARCH_STATUS_MALFORMED_RESPONSE, SEARCH_STATUS_NETWORK_FAILURE, SEARCH_STATUS_INVALID_QUERY,
})

# Config conventions mirror inference_provider.py's own named-constant-
# for-the-env-var-key pattern: a nonempty ANAXI_EXTERNAL_SEARCH_API_KEY
# is the ONLY thing that makes search "configured" -- no default key,
# ever. The endpoint has a real, documented default (Brave Search's own
# REST API shape).  Production has no endpoint environment override:
# redirecting a header credential is not a safe configuration surface.
# A fake endpoint is accepted only together with an injected test
# transport, which never reaches the network unless the test itself
# deliberately chooses to do so.
EXTERNAL_SEARCH_API_KEY_ENV_VAR = "ANAXI_EXTERNAL_SEARCH_API_KEY"
DEFAULT_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
SEARCH_PROVIDER = "brave_search"
# Keyless public provider used only when no ANAXI_EXTERNAL_SEARCH_API_KEY is
# configured. Fixed endpoint, GET, no credential of any kind, no redirects.
KEYLESS_SEARCH_PROVIDER = "duckduckgo_html"
KEYLESS_SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"
# Official keyless public API, used only when the DuckDuckGo page declines
# (it challenges automated clients after a few requests; a challenge is never
# worked around). Fixed endpoint, GET, no credential, no redirects.
WIKIPEDIA_SEARCH_PROVIDER = "wikipedia_search"
WIKIPEDIA_SEARCH_ENDPOINT = "https://en.wikipedia.org/w/api.php"
SEARCH_PROVIDERS = frozenset({SEARCH_PROVIDER, KEYLESS_SEARCH_PROVIDER, WIKIPEDIA_SEARCH_PROVIDER})

_USER_AGENT = "AnaxiReadOnlyExternalInformationV0/1.0"


def verified_ssl_context():
    """A fully verifying client context (hostname check + certificate
    verification stay ON). The platform default trust store is loaded and,
    when the pinned ``certifi`` requirement is importable, its CA bundle is
    added: a python.org macOS build ships with NO CA bundle until its
    "Install Certificates" step is run, which made every HTTPS search and
    fetch fail certificate verification in production. Never unverified."""
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError):
        pass
    return context


class BlockedURLError(Exception):
    """Raised by validate_and_resolve() for any URL this module refuses
    to contact -- scheme, hostname alias, or resolved-address rejection."""


def _is_blocked_ip(ip_obj) -> bool:
    return (
        ip_obj.is_loopback or ip_obj.is_private or ip_obj.is_link_local
        or ip_obj.is_multicast or ip_obj.is_unspecified or ip_obj.is_reserved
    )


def _default_resolve(hostname):
    infos = socket.getaddrinfo(hostname, None)
    return sorted({info[4][0] for info in infos})


def validate_and_resolve(url: str, *, resolve_fn=None) -> dict:
    """Pure-ish (one optional DNS call via resolve_fn). Returns
    {"scheme", "hostname", "resolved_ips"} or raises BlockedURLError.
    Called once per hop -- the first request AND every redirect target,
    identically -- so a redirect can never reach what the first hop
    could not."""
    resolver = resolve_fn or _default_resolve
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise BlockedURLError("URL contains control characters")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise BlockedURLError(f"malformed URL authority: {exc}") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedURLError(f"unsupported URL scheme: {scheme!r}")
    hostname = parsed.hostname
    if not hostname:
        raise BlockedURLError("URL has no host")
    if parsed.username is not None or parsed.password is not None:
        raise BlockedURLError("embedded URL credentials are not permitted")
    hostname_lower = hostname.lower()
    if hostname_lower in _BLOCKED_HOSTNAME_EXACT or hostname_lower.endswith(_BLOCKED_HOSTNAME_SUFFIXES):
        raise BlockedURLError(f"blocked host-local alias: {hostname!r}")

    try:
        literal = ipaddress.ip_address(hostname_lower.strip("[]"))
    except ValueError:
        literal = None

    if literal is not None:
        if _is_blocked_ip(literal):
            raise BlockedURLError(f"host is a blocked literal address: {hostname!r}")
        resolved_ips = [str(literal)]
    else:
        try:
            resolved = resolver(hostname)
        except (socket.gaierror, OSError) as exc:
            raise BlockedURLError(f"DNS resolution failed for {hostname!r}: {exc}") from exc
        if not resolved:
            raise BlockedURLError(f"DNS resolution returned no addresses for {hostname!r}")
        resolved_ips = []
        for ip_str in resolved:
            try:
                ip_obj = ipaddress.ip_address(ip_str)
            except ValueError:
                raise BlockedURLError(f"resolver returned an invalid address {ip_str!r} for {hostname!r}")
            if _is_blocked_ip(ip_obj):
                raise BlockedURLError(f"host {hostname!r} resolves to a blocked address: {ip_str}")
            resolved_ips.append(ip_str)

    return {
        "scheme": scheme, "hostname": hostname, "port": port,
        "resolved_ips": resolved_ips,
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disables urllib's automatic redirect following entirely -- the
    3xx response is returned to the caller untouched (Location header
    intact) so fetch_public_url() can validate the target itself before
    ever choosing to follow it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection whose TCP peer is an already-validated address."""

    def __init__(self, hostname, connect_ip, *, port, timeout):
        super().__init__(hostname, port=port, timeout=timeout)
        self._connect_ip = connect_ip

    def connect(self):
        self.sock = socket.create_connection(
            (self._connect_ip, self.port), self.timeout, self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Pinned TCP peer with ordinary hostname SNI and certificate checks."""

    def __init__(self, hostname, connect_ip, *, port, timeout):
        # HTTPSConnection's default context is verified and loads the
        # platform trust roots.  Never substitute an unverified context.
        super().__init__(hostname, port=port, timeout=timeout, context=verified_ssl_context())
        self._connect_ip = connect_ip

    def connect(self):
        raw_sock = socket.create_connection(
            (self._connect_ip, self.port), self.timeout, self.source_address,
        )
        try:
            # self.host remains the original URL hostname, so both SNI and
            # check_hostname validate the public name rather than the IP.
            self.sock = self._context.wrap_socket(raw_sock, server_hostname=self.host)
        except Exception:
            raw_sock.close()
            raise


def _default_opener(url, timeout, resolved):
    """Open one GET by connecting to the exact validated address.

    This intentionally bypasses ambient HTTP proxy configuration: a
    proxy would reconnect by hostname and dissolve the SSRF pinning
    guarantee.  FETCH V0 is a direct, stateless public-network read.
    """
    parsed = urllib.parse.urlsplit(url)
    scheme = resolved["scheme"]
    hostname = resolved["hostname"]
    port = resolved["port"] or (443 if scheme == "https" else 80)
    connect_ip = resolved["resolved_ips"][0]
    connection_cls = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
    connection = connection_cls(hostname, connect_ip, port=port, timeout=timeout)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    default_port = 443 if scheme == "https" else 80
    host_header = hostname
    if ":" in hostname and not hostname.startswith("["):
        host_header = f"[{hostname}]"
    if port != default_port:
        host_header = f"{host_header}:{port}"
    connection.request("GET", path, headers={
        "Host": host_header,
        "User-Agent": _USER_AGENT,
        "Accept": "text/html, text/plain;q=0.9, */*;q=0.1",
        "Connection": "close",
    })
    response = connection.getresponse()
    response._anaxi_connection = connection
    return response


def _close_response(response):
    try:
        response.close()
    finally:
        connection = getattr(response, "_anaxi_connection", None)
        if connection is not None:
            connection.close()


class _TextExtractor(HTMLParser):
    """No JavaScript execution, no external resource loading -- pure
    stdlib markup parsing. Strips script/style/noscript/template content,
    keeps <title> separately, and collects visible text in document order,
    one line per block element (inline markup such as links stays inside
    its sentence).

    Page chrome is left out by the document's own HTML semantics, never by
    site-specific rules: <nav> and <aside> anywhere, and <header>/<footer>
    outside an <article> (an article's own header carries its headline).
    When the page marks its principal content with <main>, only that text
    is kept (live 2026-09-24: a fetched encyclopedia page delivered menus,
    a table of contents and 123 language names before the first sentence
    of the article)."""

    _SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template"})
    _CHROME_TAGS = frozenset({"nav", "aside"})
    _ARTICLE_SCOPED_CHROME_TAGS = frozenset({"header", "footer"})
    _BLOCK_TAGS = frozenset({
        "address", "article", "blockquote", "br", "caption", "dd", "details", "div", "dl", "dt",
        "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
        "main", "ol", "p", "pre", "section", "summary", "table", "td", "th", "tr", "ul",
    })

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chrome_stack = []
        self._article_depth = 0
        self._main_depth = 0
        self._in_title = False
        self._line = []
        self._line_in_main = False
        self._raw_line = []
        self._all_lines = []
        self._main_lines = []
        self._raw_lines = []
        self.title_parts = []
        self.text_parts = []

    def _flush(self):
        raw = " ".join("".join(self._raw_line).split())
        if raw:
            self._raw_lines.append(raw)
        self._raw_line = []
        line = " ".join("".join(self._line).split())
        if line:
            self._all_lines.append(line)
            if self._line_in_main:
                self._main_lines.append(line)
        self._line = []
        self._line_in_main = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in self._BLOCK_TAGS:
            self._flush()
        if tag in self._CHROME_TAGS or (tag in self._ARTICLE_SCOPED_CHROME_TAGS and not self._article_depth):
            self._chrome_stack.append(tag)
        elif tag == "article":
            self._article_depth += 1
        elif tag == "main":
            self._main_depth += 1

    def handle_startendtag(self, tag, attrs):
        if tag in self._BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self._SKIPPED_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in self._BLOCK_TAGS:
            self._flush()
        if self._chrome_stack and self._chrome_stack[-1] == tag:
            self._chrome_stack.pop()
        elif tag == "article" and self._article_depth:
            self._article_depth -= 1
        elif tag == "main" and self._main_depth:
            self._main_depth -= 1

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        self._raw_line.append(data)
        if self._chrome_stack or not data:
            return
        if self._main_depth and data.strip():
            self._line_in_main = True
        self._line.append(data)

    def close(self):
        super().close()
        self._flush()
        # Malformed markup (an unclosed <nav>) must never silently empty a page.
        self.text_parts = self._main_lines or self._all_lines or self._raw_lines


def extract_html_text(html_text: str):
    """Pure. Returns (title_or_None, extracted_text). Never executes
    anything in html_text -- HTMLParser only ever tokenizes markup."""
    parser = _TextExtractor()
    parser.feed(html_text)
    parser.close()
    title = " ".join("".join(parser.title_parts).split())
    text = "\n".join(parser.text_parts)
    return (title or None), text


def _parse_content_type(header_value):
    if not header_value:
        return "application/octet-stream", "utf-8"
    parts = [p.strip() for p in header_value.split(";")]
    mime = parts[0].lower() or "application/octet-stream"
    charset = "utf-8"
    for p in parts[1:]:
        if p.lower().startswith("charset="):
            charset = p.split("=", 1)[1].strip().strip('"') or "utf-8"
    return mime, charset


def fetch_public_url(
    url: str, *, timeout=DEFAULT_TIMEOUT_SECONDS, max_redirects=MAX_REDIRECTS,
    max_bytes=MAX_RESPONSE_BYTES, max_text_chars=MAX_EXTRACTED_TEXT_CHARS,
    resolve_fn=None, opener_fn=None,
) -> dict:
    """GET-only. Returns a closed-vocabulary result dict, always with
    "status", "requested_url", "final_url" -- never raises for an
    ordinary network/HTTP/content failure (those are classified
    statuses); a caller bug (non-string url) still raises TypeError."""
    if not isinstance(url, str) or not url:
        raise TypeError("url must be a nonempty string")
    current_url = url
    response = None
    status_code = None

    for _hop in range(max_redirects + 1):
        try:
            resolved = validate_and_resolve(current_url, resolve_fn=resolve_fn)
        except BlockedURLError as exc:
            return {
                "status": FETCH_STATUS_BLOCKED_URL, "detail": str(exc),
                "requested_url": url, "final_url": current_url,
            }
        try:
            if opener_fn is None:
                response = _default_opener(current_url, timeout, resolved)
            else:
                response = opener_fn(current_url, timeout)
        except urllib.error.HTTPError as exc:
            # HTTPError is itself a valid minimal response object (.code,
            # .headers, .read()) -- an injected opener_fn may either
            # return it (the default opener's own convention) or let it
            # propagate as an exception (HTTPError is a URLError
            # subclass); both styles are handled identically here so
            # ordinary HTTP failure statuses are never misclassified as
            # a network-transport failure.
            response = exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return {
                "status": FETCH_STATUS_NETWORK_FAILURE, "detail": str(exc),
                "requested_url": url, "final_url": current_url,
            }
        status_code = getattr(response, "code", None)
        if status_code is None:
            status_code = getattr(response, "status", None)

        if status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location") if response.headers else None
            if not location:
                _close_response(response)
                return {
                    "status": FETCH_STATUS_NETWORK_FAILURE,
                    "detail": f"redirect status {status_code} carried no Location header",
                    "requested_url": url, "final_url": current_url,
                }
            next_url = urllib.parse.urljoin(current_url, location)
            _close_response(response)
            current_url = next_url
            continue
        break
    else:
        return {
            "status": FETCH_STATUS_TOO_MANY_REDIRECTS, "detail": f"exceeded {max_redirects} redirects",
            "requested_url": url, "final_url": current_url,
        }

    if status_code is None or not (200 <= status_code < 300):
        _close_response(response)
        return {
            "status": FETCH_STATUS_HTTP_FAILURE, "detail": f"HTTP {status_code}",
            "http_status": status_code, "requested_url": url, "final_url": current_url,
        }

    content_type_header = response.headers.get("Content-Type") if response.headers else None
    mime, charset = _parse_content_type(content_type_header)
    if mime not in SUPPORTED_CONTENT_TYPES:
        _close_response(response)
        return {
            "status": FETCH_STATUS_UNSUPPORTED_CONTENT_TYPE, "content_type": mime,
            "http_status": status_code, "requested_url": url, "final_url": current_url,
        }

    body = b""
    body_truncated = False
    try:
        # Each read is capped to (remaining budget + 1 byte) -- never a
        # fixed chunk size -- so a single oversized read can never blow
        # past the cap undetected (a naive fixed 64KiB read could
        # silently swallow the "is there more" signal for a small
        # max_bytes, exactly the failure this bound avoids).
        while len(body) <= max_bytes:
            read_size = min(65536, max_bytes - len(body) + 1)
            chunk = response.read(read_size)
            if not chunk:
                break
            body += chunk
        if len(body) > max_bytes:
            body_truncated = True
    except (OSError, TimeoutError) as exc:
        return {
            "status": FETCH_STATUS_NETWORK_FAILURE, "detail": f"error while reading body: {exc}",
            "requested_url": url, "final_url": current_url,
        }
    finally:
        try:
            _close_response(response)
        except Exception:
            pass

    if len(body) > max_bytes:
        body = body[:max_bytes]

    try:
        text = body.decode(charset, errors="replace")
    except (LookupError, ValueError):
        text = body.decode("utf-8", errors="replace")

    if mime == "text/html":
        try:
            title, extracted = extract_html_text(text)
        except Exception as exc:
            return {
                "status": FETCH_STATUS_EXTRACTION_FAILED, "detail": str(exc),
                "http_status": status_code, "requested_url": url, "final_url": current_url,
            }
    else:
        title, extracted = None, text

    text_truncated = body_truncated or len(extracted) > max_text_chars
    if len(extracted) > max_text_chars:
        extracted = extracted[:max_text_chars]

    return {
        "status": FETCH_STATUS_TRUNCATED if text_truncated else FETCH_STATUS_SUCCESS,
        "requested_url": url, "final_url": current_url, "http_status": status_code,
        "content_type": mime, "title": title, "text": extracted, "truncated": text_truncated,
    }


def _default_search_get(url, api_key, timeout):
    request = urllib.request.Request(url, method="GET", headers={
        "User-Agent": _USER_AGENT, "Accept": "application/json",
        "X-Subscription-Token": api_key,
    })
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=verified_ssl_context()))
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = response.read(MAX_SEARCH_RESPONSE_BYTES + 1)
        if len(body) > MAX_SEARCH_RESPONSE_BYTES:
            return None, b""
        return response.getcode(), body


def search_web(
    query: str, *, api_key=None, endpoint=None, timeout=DEFAULT_TIMEOUT_SECONDS,
    max_results=MAX_SEARCH_RESULTS, http_get_fn=None,
) -> dict:
    """One bounded GET to one configured search provider. Returns a
    closed-vocabulary result dict with "status" and "results" (always a
    list, empty on any failure). Never raises for an ordinary
    configuration/network/provider failure. The exact query text is the
    only semantic payload sent outward -- no conversation/session
    context is ever appended."""
    if not isinstance(query, str) or not query.strip():
        return {"status": SEARCH_STATUS_INVALID_QUERY, "detail": "query must be a nonempty string",
                "provider": SEARCH_PROVIDER, "results": []}
    if len(query) > MAX_SEARCH_QUERY_CHARS:
        return {"status": SEARCH_STATUS_INVALID_QUERY, "detail": "query exceeds maximum length",
                "provider": SEARCH_PROVIDER, "results": []}

    resolved_key = api_key if api_key is not None else os.environ.get(EXTERNAL_SEARCH_API_KEY_ENV_VAR)
    if not resolved_key:
        return {
            "status": SEARCH_STATUS_NOT_CONFIGURED,
            "detail": f"no search provider API key configured ({EXTERNAL_SEARCH_API_KEY_ENV_VAR} unset)",
            "provider": SEARCH_PROVIDER, "results": [],
        }
    if endpoint is not None and http_get_fn is None:
        return {
            "status": SEARCH_STATUS_PROVIDER_FAILURE,
            "detail": "custom search endpoint requires an injected test transport",
            "provider": SEARCH_PROVIDER, "results": [],
        }
    resolved_endpoint = endpoint if endpoint is not None else DEFAULT_SEARCH_ENDPOINT
    bounded_max_results = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
    params = urllib.parse.urlencode({"q": query, "count": bounded_max_results})
    url = f"{resolved_endpoint}?{params}"

    getter = http_get_fn or _default_search_get
    try:
        status_code, body_bytes = getter(url, resolved_key, timeout)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return {"status": SEARCH_STATUS_NETWORK_FAILURE, "detail": str(exc),
                "provider": SEARCH_PROVIDER, "results": []}

    if status_code != 200:
        detail = "provider response exceeded maximum size" if status_code is None else f"provider returned HTTP {status_code}"
        return {"status": SEARCH_STATUS_PROVIDER_FAILURE, "detail": detail,
                "provider": SEARCH_PROVIDER, "results": []}

    try:
        payload = json.loads(body_bytes)
    except (ValueError, TypeError) as exc:
        return {"status": SEARCH_STATUS_MALFORMED_RESPONSE, "detail": f"non-JSON provider response: {exc}",
                "provider": SEARCH_PROVIDER, "results": []}
    if not isinstance(payload, dict):
        return {"status": SEARCH_STATUS_MALFORMED_RESPONSE, "detail": "provider response is not a JSON object",
                "provider": SEARCH_PROVIDER, "results": []}

    web_section = payload.get("web")
    raw_results = web_section.get("results") if isinstance(web_section, dict) else None
    if not isinstance(raw_results, list):
        return {"status": SEARCH_STATUS_MALFORMED_RESPONSE, "detail": "missing web.results list",
                "provider": SEARCH_PROVIDER, "results": []}

    results = []
    for rank, item in enumerate(raw_results[:bounded_max_results]):
        if not isinstance(item, dict):
            continue
        item_url = item.get("url")
        if not isinstance(item_url, str) or not item_url:
            continue
        title = item.get("title")
        snippet = item.get("description")
        results.append({
            "rank": rank,
            "title": title if isinstance(title, str) else "",
            "url": item_url,
            "snippet": snippet if isinstance(snippet, str) else "",
        })

    return {"status": SEARCH_STATUS_SUCCESS, "detail": None,
            "provider": SEARCH_PROVIDER, "results": results}


class _KeylessResultParser(HTMLParser):
    """Pure tokenizer over the DuckDuckGo HTML results page: collects each
    ``result__a`` link (title + href) and the ``result__snippet`` text that
    follows it. Executes nothing."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self._mode = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        if "result__a" in classes:
            self.items.append({"href": attr.get("href") or "", "title": "", "snippet": ""})
            self._mode, self._buf = "title", []
        elif "result__snippet" in classes and self.items:
            self._mode, self._buf = "snippet", []

    def handle_data(self, data):
        if self._mode:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._mode and self.items:
            self.items[-1][self._mode] = " ".join("".join(self._buf).split())
            self._mode = None


def _keyless_result_url(href):
    """The real destination behind a DuckDuckGo redirect link, or None."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
    else:
        target = href
    target_parsed = urllib.parse.urlparse(target)
    if target_parsed.scheme not in ALLOWED_SCHEMES or not target_parsed.netloc:
        return None
    if target_parsed.netloc.endswith("duckduckgo.com"):
        return None  # an advertisement / provider-internal link, not a result
    return target


def _default_keyless_get(url, timeout):
    request = urllib.request.Request(url, method="GET", headers={
        "User-Agent": _USER_AGENT, "Accept": "text/html",
    })
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=verified_ssl_context()))
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = response.read(MAX_SEARCH_RESPONSE_BYTES + 1)
        if len(body) > MAX_SEARCH_RESPONSE_BYTES:
            return None, b""
        return response.getcode(), body


def search_keyless(query: str, *, timeout=DEFAULT_TIMEOUT_SECONDS, max_results=MAX_SEARCH_RESULTS,
                   http_get_fn=None) -> dict:
    """One bounded read-only GET to the public keyless provider. Same
    closed-vocabulary result shape as search_web(). A provider challenge
    (anything other than a plain 200 results page) is reported as a
    provider failure; it is never worked around."""
    def fail(status, detail):
        return {"status": status, "detail": detail, "provider": KEYLESS_SEARCH_PROVIDER, "results": []}

    if not isinstance(query, str) or not query.strip():
        return fail(SEARCH_STATUS_INVALID_QUERY, "query must be a nonempty string")
    if len(query) > MAX_SEARCH_QUERY_CHARS:
        return fail(SEARCH_STATUS_INVALID_QUERY, "query exceeds maximum length")
    bounded_max_results = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
    url = f"{KEYLESS_SEARCH_ENDPOINT}?{urllib.parse.urlencode({'q': query})}"
    getter = http_get_fn or (lambda u, _key, t: _default_keyless_get(u, t))
    try:
        status_code, body_bytes = getter(url, None, timeout)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return fail(SEARCH_STATUS_NETWORK_FAILURE, str(exc))
    if status_code != 200:
        detail = ("provider response exceeded maximum size" if status_code is None
                  else f"provider returned HTTP {status_code}")
        return fail(SEARCH_STATUS_PROVIDER_FAILURE, detail)
    parser = _KeylessResultParser()
    try:
        parser.feed(body_bytes.decode("utf-8", errors="replace"))
        parser.close()
    except Exception as exc:  # HTMLParser errors are malformed-response, never a crash
        return fail(SEARCH_STATUS_MALFORMED_RESPONSE, f"unparseable provider page: {exc}")
    results = []
    seen = set()
    for item in parser.items:
        item_url = _keyless_result_url(item["href"])
        if item_url is None or item_url in seen:
            continue
        seen.add(item_url)
        results.append({"rank": len(results), "title": item["title"], "url": item_url,
                        "snippet": item["snippet"]})
        if len(results) >= bounded_max_results:
            break
    return {"status": SEARCH_STATUS_SUCCESS, "detail": None,
            "provider": KEYLESS_SEARCH_PROVIDER, "results": results}


def search_wikipedia(query: str, *, timeout=DEFAULT_TIMEOUT_SECONDS, max_results=MAX_SEARCH_RESULTS,
                     http_get_fn=None) -> dict:
    """One bounded read-only GET to Wikipedia's public search API."""
    def fail(status, detail):
        return {"status": status, "detail": detail, "provider": WIKIPEDIA_SEARCH_PROVIDER, "results": []}

    if not isinstance(query, str) or not query.strip():
        return fail(SEARCH_STATUS_INVALID_QUERY, "query must be a nonempty string")
    if len(query) > MAX_SEARCH_QUERY_CHARS:
        return fail(SEARCH_STATUS_INVALID_QUERY, "query exceeds maximum length")
    bounded_max_results = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
    params = urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": query,
                                     "srlimit": bounded_max_results, "format": "json"})
    getter = http_get_fn or (lambda u, _key, t: _default_keyless_get(u, t))
    try:
        status_code, body_bytes = getter(f"{WIKIPEDIA_SEARCH_ENDPOINT}?{params}", None, timeout)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return fail(SEARCH_STATUS_NETWORK_FAILURE, str(exc))
    if status_code != 200:
        return fail(SEARCH_STATUS_PROVIDER_FAILURE, "provider response exceeded maximum size"
                    if status_code is None else f"provider returned HTTP {status_code}")
    try:
        entries = json.loads(body_bytes)["query"]["search"]
        if not isinstance(entries, list):
            raise TypeError("search is not a list")
    except (ValueError, TypeError, KeyError) as exc:
        return fail(SEARCH_STATUS_MALFORMED_RESPONSE, f"unexpected provider response: {exc}")
    results = []
    for entry in entries[:bounded_max_results]:
        title = entry.get("title") if isinstance(entry, dict) else None
        if not isinstance(title, str) or not title:
            continue
        snippet_parser = _TextExtractor()
        snippet_parser.feed(str(entry.get("snippet") or ""))
        snippet_parser.close()
        results.append({
            "rank": len(results), "title": title,
            "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"), safe="_()"),
            "snippet": " ".join(" ".join(snippet_parser.text_parts).split()),
        })
    return {"status": SEARCH_STATUS_SUCCESS, "detail": None,
            "provider": WIKIPEDIA_SEARCH_PROVIDER, "results": results}


def search_public(query: str, **kwargs) -> dict:
    """The production search entry point. A configured
    ANAXI_EXTERNAL_SEARCH_API_KEY selects the keyed provider. Otherwise the
    keyless DuckDuckGo page is tried and, if it declines or fails, Wikipedia's
    public API; the result always names the provider that actually answered,
    and a fallback says why in ``detail``."""
    if os.environ.get(EXTERNAL_SEARCH_API_KEY_ENV_VAR):
        return search_web(query, **kwargs)
    keyless_kwargs = {k: v for k, v in kwargs.items() if k in ("timeout", "max_results", "http_get_fn")}
    primary = search_keyless(query, **keyless_kwargs)
    if primary["status"] == SEARCH_STATUS_SUCCESS and primary["results"]:
        return primary
    if primary["status"] == SEARCH_STATUS_INVALID_QUERY:
        return primary
    fallback = search_wikipedia(query, **keyless_kwargs)
    if fallback["status"] == SEARCH_STATUS_SUCCESS and fallback["results"]:
        reason = primary["detail"] or "no results"
        return dict(fallback, detail=f"{KEYLESS_SEARCH_PROVIDER} unavailable ({reason}); these results are from Wikipedia only")
    return primary
