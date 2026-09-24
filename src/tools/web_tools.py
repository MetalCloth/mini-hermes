import ipaddress
import json
import os
import socket
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlparse, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


MAX_PAGE_BYTES = 1_000_000
MAX_PAGE_TEXT_CHARS = 12_000
MAX_API_BYTES = 2_000_000
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"


class _Results(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self._field = None
        self._field_tag = None
        self._current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "a" and "result__a" in classes:
            self._current = {"title": "", "url": _result_url(attrs.get("href", "")), "snippet": ""}
            self._field = "title"
            self._field_tag = tag
        elif self._current and "result__snippet" in classes:
            self._field = "snippet"
            self._field_tag = tag

    def handle_data(self, data):
        if self._current and self._field:
            self._current[self._field] += data

    def handle_endtag(self, tag):
        if tag == self._field_tag:
            if self._field == "title" and self._current:
                self.items.append(self._current)
            self._field = None
            self._field_tag = None


def _result_url(href: str) -> str:
    parsed = urlparse(href)
    if "uddg" in parse_qs(parsed.query):
        return unquote(parse_qs(parsed.query)["uddg"][0])
    return href


def web_search(query: str, max_results: int = 5) -> str:
    if not query.strip():
        raise ValueError("Search query is empty. Provide a focused topic or question.")
    if not 1 <= max_results <= 5:
        raise ValueError("max_results must be between 1 and 5.")
    request = Request(
        f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
        headers={"User-Agent": "Mozilla/5.0 Mini-Hermes/1.0"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            page = response.read().decode("utf-8", errors="replace")
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Web search failed: {exc}. Try again later.") from exc
    parser = _Results()
    parser.feed(page)
    if not parser.items:
        return "No web results found. Try a shorter or more specific query."
    return "\n\n".join(
        f"{index}. {item['title'].strip()}\n{item['url']}\n{item['snippet'].strip()}"
        for index, item in enumerate(parser.items[:max_results], 1)
    )


def _check_public_url(url: str) -> None:
    if len(url) > 2048:
        raise ValueError("URL is too long. Provide a URL under 2,048 characters.")
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ValueError("Invalid URL. Provide a public http:// or https:// URL.") from exc
    if (parsed.scheme not in {"http", "https"} or not host
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("Provide a public http:// or https:// URL without credentials.")
    if host.rstrip(".").lower() == "localhost" or host.lower().endswith(".localhost"):
        raise ValueError("Local and private network URLs cannot be read.")
    try:
        addresses = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80),
                                       type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"Could not resolve {host}. Check the URL or try another result.") from exc
    # Reject any private DNS answer, including those reached through redirects.
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Local and private network URLs cannot be read.")


class _PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward the Firecrawl bearer key to a redirect destination.
        return None


class _PageText(HTMLParser):
    _SKIP = {"head", "script", "style", "noscript", "svg", "template"}
    _BLOCK = {"article", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "section", "tr"}

    def __init__(self):
        super().__init__()
        self.title: list[str] = []
        self.text: list[str] = []
        self._skip = 0
        self._title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._title = True
        elif tag in self._SKIP:
            self._skip += 1
        elif not self._skip and tag in self._BLOCK:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._title = False
        elif tag in self._SKIP and self._skip:
            self._skip -= 1
        elif not self._skip and tag in self._BLOCK:
            self.text.append("\n")

    def handle_data(self, data):
        if self._title:
            self.title.append(data)
        elif not self._skip:
            self.text.append(data)


def _format_page(url: str, title: str, body: str, note: str = "") -> str:
    title = " ".join(title.split())[:200] or "Untitled page"
    body = body.strip()
    if not body:
        raise ValueError("No readable text found on this page. Try another result or use search snippets.")
    if len(body) > MAX_PAGE_TEXT_CHARS:
        body = body[:MAX_PAGE_TEXT_CHARS]
        note = f"{note} Page text truncated after 12,000 characters.".strip()
    suffix = f"\n[{note}]" if note else ""
    return f"Source: {url}\nTitle: {title}\nContent:\n{body}{suffix}"


def _firecrawl_api_key() -> str:
    key = os.environ.get("FIRECRAWL_API_KEY")
    if key and key.strip():
        return key.strip()
    env_file = Path.home() / ".mini-hermes" / "firecrawl.env"
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    for line in lines:
        name, separator, value = line.partition("=")
        if separator and name.strip() == "FIRECRAWL_API_KEY":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return ""


def _firecrawl_extract(url: str, key: str) -> str:
    request = Request(
        FIRECRAWL_SCRAPE_URL,
        data=json.dumps({"url": url, "formats": ["markdown"], "timeout": 20_000}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(MAX_API_BYTES + 1)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise RuntimeError("Firecrawl rejected the API key. Check ~/.mini-hermes/firecrawl.env.") from exc
        if exc.code == 429:
            raise RuntimeError("Firecrawl is rate limited. Try again later or use another result.") from exc
        raise RuntimeError(f"Firecrawl returned HTTP {exc.code}. Try another result.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("Firecrawl could not be reached or timed out. Try another result.") from exc
    if len(raw) > MAX_API_BYTES:
        raise RuntimeError("Firecrawl returned too much data for one page. Try another result.")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Firecrawl returned an invalid response. Try another result.") from exc
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise RuntimeError("Firecrawl could not extract this page. Try another result.")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("markdown"), str):
        raise RuntimeError("Firecrawl returned no page text. Try another result.")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    title = metadata.get("title") if isinstance(metadata.get("title"), str) else ""
    return _format_page(url, title, data["markdown"])


def _local_extract(url: str) -> str:
    """Read HTML directly when no Firecrawl key is configured."""
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 Mini-Hermes/1.0",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "identity",
    })
    try:
        with build_opener(_PublicRedirect()).open(request, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
            if content_type and content_type not in {"text/html", "application/xhtml+xml"}:
                raise ValueError("This URL is not an HTML page. Try another result.")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("This page uses an unsupported compression format. Try another result.")
            raw = response.read(MAX_PAGE_BYTES + 1)
            charset = response.headers.get_content_charset() or "utf-8"
            source_url = response.geturl()
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Could not read page: {exc}. Try another URL or use search results.") from exc

    try:
        page = raw[:MAX_PAGE_BYTES].decode(charset, errors="replace")
    except LookupError:
        page = raw[:MAX_PAGE_BYTES].decode("utf-8", errors="replace")
    parser = _PageText()
    parser.feed(page)
    body = "\n".join(
        line for part in "".join(parser.text).splitlines()
        if (line := " ".join(part.split()))
    )
    note = "Download limited to the first 1,000,000 bytes." if len(raw) > MAX_PAGE_BYTES else ""
    return _format_page(source_url, "".join(parser.title), body, note)


def web_extract(url: str) -> str:
    """Read a public page through Firecrawl, or direct HTML when unconfigured."""
    _check_public_url(url)
    key = _firecrawl_api_key()
    return _firecrawl_extract(url, key) if key else _local_extract(url)
