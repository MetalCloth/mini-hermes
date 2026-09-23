from html.parser import HTMLParser
from urllib.error import URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen


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
