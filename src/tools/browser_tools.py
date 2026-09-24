"""Short-lived Firecrawl browser session for one agent turn."""

import json
import re
import shlex
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from uuid import UUID

from src.tools.web_tools import _NoRedirect, _check_public_url, _firecrawl_api_key


API_URL = "https://api.firecrawl.dev/v2"
MAX_RESPONSE_BYTES = 2_000_000
MAX_SNAPSHOT_CHARS = 12_000
ELEMENT_REF = re.compile(r"@e[1-9][0-9]{0,5}\Z")


def _request(path: str, key: str, method: str = "POST", body: dict | None = None,
             retry_safe: bool = False) -> dict:
    for attempt in range(2 if retry_safe else 1):
        request = Request(
            API_URL + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with build_opener(_NoRedirect()).open(request, timeout=40) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            break
        except HTTPError as exc:
            if method == "DELETE" and exc.code in {404, 410}:
                return {"success": True}
            if retry_safe and attempt == 0 and exc.code in {500, 502, 503, 504}:
                time.sleep(0.5)
                continue
            if exc.code in {401, 403}:
                raise RuntimeError("Firecrawl rejected the API key. Check ~/.mini-hermes/firecrawl.env.") from exc
            if exc.code == 402:
                raise RuntimeError("Firecrawl browser access needs available credits or a supported plan.") from exc
            if exc.code in {404, 409, 410}:
                raise RuntimeError("Firecrawl browser session expired. Open the page again.") from exc
            if exc.code == 429:
                raise RuntimeError("Firecrawl is rate limited. Try again later.") from exc
            if exc.code in {500, 502, 503, 504} and method == "POST" and "/interact" in path:
                if retry_safe:
                    raise RuntimeError(
                        f"Firecrawl returned HTTP {exc.code} during the snapshot. "
                        "Try browser_snapshot again."
                    ) from exc
                raise RuntimeError(
                    f"Firecrawl returned HTTP {exc.code}. The browser action may have run; "
                    "use browser_snapshot before repeating it."
                ) from exc
            raise RuntimeError(f"Firecrawl browser returned HTTP {exc.code}. Try again later.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            if retry_safe and attempt == 0:
                time.sleep(0.5)
                continue
            if method == "POST" and "/interact" in path and not retry_safe:
                raise RuntimeError(
                    "Lost contact with Firecrawl. The browser action may have run; "
                    "use browser_snapshot before repeating it."
                ) from exc
            raise RuntimeError("Could not reach Firecrawl browser. Try again later.") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("Firecrawl browser returned too much data for one action.")
    if method == "DELETE" and not raw:
        return {"success": True}
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Firecrawl browser returned an invalid response.") from exc
    if not isinstance(result, dict) or result.get("success") is not True:
        reason = result.get("error", "Unknown error") if isinstance(result, dict) else "Invalid response"
        raise RuntimeError(f"Firecrawl browser failed: {str(reason)[:300]}")
    return result


class BrowserSession:
    """Keep one remote page open across tool calls, then stop it after the turn."""

    def __init__(self) -> None:
        self.scrape_id: str | None = None
        self.key: str | None = None

    def open(self, url: str) -> str:
        if not isinstance(url, str):
            raise ValueError("Give a public http:// or https:// URL.")
        _check_public_url(url)
        if self.scrape_id:
            self.close()
        key = _firecrawl_api_key()
        if not key:
            raise ValueError("Browser interaction needs FIRECRAWL_API_KEY in ~/.mini-hermes/firecrawl.env.")
        result = _request("/scrape", key, body={"url": url, "formats": ["markdown"], "timeout": 20_000})
        data = result.get("data")
        metadata = data.get("metadata") if isinstance(data, dict) else None
        scrape_id = metadata.get("scrapeId") if isinstance(metadata, dict) else None
        try:
            scrape_id = str(UUID(scrape_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError("Firecrawl returned no browser session ID. Try another page.") from exc
        self.scrape_id, self.key = scrape_id, key
        try:
            return self.snapshot()
        except Exception as exc:
            try:
                self.close()
            except RuntimeError as cleanup_exc:
                raise RuntimeError(f"{exc} Cleanup also failed: {cleanup_exc}") from exc
            raise

    def _run(self, action: str = "") -> str:
        if not self.scrape_id or not self.key:
            raise ValueError("Open a page with browser_open before using browser actions.")
        code = f"{action} && " if action else ""
        code += "agent-browser get url && agent-browser snapshot"
        result = _request(
            f"/scrape/{self.scrape_id}/interact", self.key,
            body={"code": code, "language": "bash", "timeout": 20},
            retry_safe=not action,
        )
        if result.get("killed") or result.get("exitCode") not in (0, None) or result.get("error"):
            detail = result.get("stderr") or result.get("error") or "Browser action failed"
            raise RuntimeError(f"Browser action failed: {str(detail)[:300]}")
        snapshot = result.get("stdout") or result.get("result")
        if not isinstance(snapshot, str) or not snapshot.strip():
            raise RuntimeError("Browser returned no page snapshot. Try browser_snapshot again.")
        if len(snapshot) > MAX_SNAPSHOT_CHARS:
            snapshot = snapshot[:MAX_SNAPSHOT_CHARS] + "\n[Browser snapshot truncated at 12,000 characters.]"
        return snapshot

    def snapshot(self) -> str:
        return self._run()

    def click(self, ref: str) -> str:
        if not isinstance(ref, str) or not ELEMENT_REF.fullmatch(ref):
            raise ValueError("Give an element ref from the latest snapshot, such as '@e2'.")
        return self._run(f"agent-browser click {ref}")

    def fill(self, ref: str, text: str) -> str:
        if not isinstance(ref, str) or not ELEMENT_REF.fullmatch(ref):
            raise ValueError("Give an element ref from the latest snapshot, such as '@e1'.")
        if not isinstance(text, str) or not text or len(text) > 1_000 or "\0" in text:
            raise ValueError("Give 1 to 1,000 characters of text to fill the field.")
        return self._run(f"agent-browser fill {ref} {shlex.quote(text)}")

    def press(self, key: str) -> str:
        allowed = {
            "Enter", "Tab", "Escape", "Backspace", "Space", "ArrowUp", "ArrowDown",
            "ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End",
        }
        if not isinstance(key, str) or key not in allowed:
            raise ValueError(f"Unsupported key. Use one of: {', '.join(sorted(allowed))}.")
        return self._run(f"agent-browser press {key}")

    def scroll(self, direction: str, pixels: int) -> str:
        if not isinstance(direction, str) or direction not in {"up", "down", "left", "right"} or type(pixels) is not int or not 1 <= pixels <= 2000:
            raise ValueError("Scroll direction must be up/down/left/right and pixels must be 1 to 2,000.")
        return self._run(f"agent-browser scroll {direction} {pixels}")

    def wait(self, mode: str, value: str) -> str:
        if not isinstance(mode, str) or not isinstance(value, str) or not value or len(value) > 200 or "\0" in value:
            raise ValueError("Wait value must be 1 to 200 characters.")
        if mode == "ref" and ELEMENT_REF.fullmatch(value):
            target = value
        elif mode in {"text", "url"}:
            target = f"--{mode} {shlex.quote(value)}"
        elif mode == "load" and value in {"load", "domcontentloaded", "networkidle"}:
            target = f"--load {value}"
        else:
            raise ValueError("Wait for a ref (@e2), text, URL pattern, or load state.")
        return self._run(f"agent-browser wait {target} --timeout 10000")

    def back(self) -> str:
        return self._run("agent-browser back")

    def close(self) -> None:
        if not self.scrape_id or not self.key:
            return
        scrape_id, key = self.scrape_id, self.key
        try:
            _request(f"/scrape/{scrape_id}/interact", key, method="DELETE", retry_safe=True)
        except RuntimeError as exc:
            raise RuntimeError(f"Could not close Firecrawl browser session {scrape_id}: {exc}") from exc
        self.scrape_id = None
        self.key = None
