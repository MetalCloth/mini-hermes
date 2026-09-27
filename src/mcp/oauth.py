"""Notion browser sign-in using the MCP SDK's OAuth/PKCE implementation."""

import asyncio
import json
import os
import secrets
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import (
    AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata,
    OAuthToken, ProtectedResourceMetadata,
)


NOTION_URL = "https://mcp.notion.com/mcp"
# ponytail: one login at a time on this port; use dynamic registration if parallel logins are needed.
_CALLBACK_URL = "http://127.0.0.1:8766/callback"


class NotionTokenStorage:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".mini-hermes" / "mcp-notion-auth.json"

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def has_tokens(self) -> bool:
        data = self._read()
        try:
            OAuthClientInformationFull.model_validate(data["client_info"])
            return bool(OAuthToken.model_validate(data["tokens"]).access_token)
        except (KeyError, ValueError, TypeError):
            return False

    def _save(self, **values) -> None:
        data = self._read()
        data.update(values)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(dir=self.path.parent, prefix=".notion-auth-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
                handle.flush()
            os.replace(filename, self.path)
        finally:
            Path(filename).unlink(missing_ok=True)

    async def get_tokens(self) -> OAuthToken | None:
        data = self._read()
        if not data.get("tokens"):
            return None
        tokens = OAuthToken.model_validate(data["tokens"])
        if data.get("expires_at") is not None:
            # Keep expiry absolute across restarts so the SDK refreshes expired tokens.
            tokens.expires_in = max(-1, int(data["expires_at"] - time.time())) or -1
        return tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._save(
            tokens=tokens.model_dump(mode="json"),
            expires_at=time.time() + tokens.expires_in if tokens.expires_in is not None else None,
        )

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._save(client_info=client_info.model_dump(mode="json"))


def notion_auth(redirect_handler=None, callback_handler=None, storage=None) -> OAuthClientProvider:
    storage = storage if storage is not None else NotionTokenStorage()
    provider = OAuthClientProvider(
        server_url=NOTION_URL,
        client_metadata=OAuthClientMetadata(
            client_name="Oryn", redirect_uris=[_CALLBACK_URL],
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    # The SDK does not restore expiry or discovered token endpoints from TokenStorage.
    # Restore both so an expired saved token can refresh without another browser login.
    data = storage._read()
    try:
        if data.get("oauth_metadata"):
            provider.context.oauth_metadata = OAuthMetadata.model_validate(data["oauth_metadata"])
        if data.get("resource_metadata"):
            resource = ProtectedResourceMetadata.model_validate(data["resource_metadata"])
            provider.context.protected_resource_metadata = resource
            provider.context.auth_server_url = str(resource.authorization_servers[0])
        if data.get("expires_at") is not None and type(data["expires_at"]) not in {int, float}:
            raise ValueError("Invalid saved token expiry")
        provider.context.token_expiry_time = data.get("expires_at")
    except (ValueError, TypeError, IndexError):
        raise ValueError("Saved Notion sign-in is invalid; run ./oryn mcp login notion.") from None
    return provider


async def login_notion(*, on_authorize: Callable[[str], None] | None = None) -> int:
    """Explicit sign-in: startup never opens a browser or waits for a user."""
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    loop = asyncio.get_running_loop()
    result = loop.create_future()
    expected_state = ""

    def finish(value: AuthorizationCodeResult | Exception) -> None:
        if not result.done():
            if isinstance(value, Exception):
                result.set_exception(value)
            else:
                result.set_result(value)

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)
            state = params.get("state", [""])[0]
            valid = (parsed.path == "/callback" and bool(expected_state)
                     and secrets.compare_digest(state.encode(), expected_state.encode()))
            code = params.get("code", [""])[0]
            if not valid or (not code and "error" not in params):
                self.send_error(400, "Invalid login callback")
                return
            if "error" in params:
                value = RuntimeError("Notion sign-in was declined.")
                body = b"Sign-in declined. You can close this window."
            else:
                value = AuthorizationCodeResult(
                    code=code, state=state, iss=params.get("iss", [None])[0],
                )
                body = b"Oryn received your authorization. Return to the terminal to finish."
            loop.call_soon_threadsafe(finish, value)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass  # Authorization codes must never appear in request logs.

    async def redirect(url: str) -> None:
        nonlocal expected_state
        expected_state = parse_qs(urlsplit(url).query).get("state", [""])[0]
        if on_authorize:
            on_authorize(url)
        else:
            print(f"Sign in to Notion in your browser:\n{url}", flush=True)
        await asyncio.to_thread(webbrowser.open, url)

    async def callback() -> AuthorizationCodeResult:
        return await asyncio.wait_for(asyncio.shield(result), timeout=300)

    with HTTPServer(("127.0.0.1", 8766), CallbackHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            storage = NotionTokenStorage()
            try:
                if storage._read() and not storage.has_tokens():
                    raise ValueError("Incomplete saved sign-in")
                auth = notion_auth(redirect, callback, storage)
            except ValueError:
                # Explicit login can repair an invalid cache; startup never resets it.
                storage._save(client_info=None, tokens=None, expires_at=None,
                              oauth_metadata=None, resource_metadata=None)
                auth = notion_auth(redirect, callback, storage)
            async with create_mcp_http_client(auth=auth) as http:
                async with Client(streamable_http_client(NOTION_URL, http_client=http), mode="legacy") as client:
                    page = await client.list_tools()
                    storage._save(
                        oauth_metadata=auth.context.oauth_metadata.model_dump(mode="json") if auth.context.oauth_metadata else None,
                        resource_metadata=auth.context.protected_resource_metadata.model_dump(mode="json") if auth.context.protected_resource_metadata else None,
                    )
                    return len(page.tools)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            if not result.done():
                result.cancel()


def main(argv: list[str] | None = None) -> None:
    import argparse
    from src.mcp.discovery import SERVER_NAMES, load_enabled_servers, save_enabled_servers

    parser = argparse.ArgumentParser(description="Manage Oryn MCP connections (restart Oryn after changes).")
    parser.add_argument("action", choices=["login", "enable", "disable"])
    parser.add_argument("server", choices=SERVER_NAMES)
    args = parser.parse_args(argv)
    if args.action != "login":
        enabled = load_enabled_servers()
        if args.action == "enable":
            enabled.add(args.server)
        else:
            enabled.discard(args.server)
        save_enabled_servers(enabled)
        print(f"{args.server}: {'enabled' if args.action == 'enable' else 'disabled'}. Restart Oryn.")
        return
    if args.server != "notion":
        parser.error("Browser login is currently supported for Notion. Configure other services in mcp.env.")
    try:
        count = asyncio.run(asyncio.wait_for(login_notion(), timeout=330))
    except (Exception, KeyboardInterrupt) as exc:
        parser.exit(1, f"Notion sign-in did not finish ({type(exc).__name__}). Retry ./oryn mcp login notion.\n")
    print(f"Notion connected with {count} tools. Restart Oryn to use the connection.")


if __name__ == "__main__":
    main()
