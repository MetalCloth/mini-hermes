"""Synchronous bridge from Oryn's turn loop to long-lived MCP stdio clients."""

import asyncio
import ipaddress
import os
import shutil
import socket
import threading
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from src.mcp.adapter import provider_tool, provider_tool_name, result_text
from src.mcp.discovery import MCPServerConfig, server_configs


_SAFE_PLAYWRIGHT_TOOLS = {
    "browser_navigate", "browser_snapshot", "browser_tabs", "browser_go_back",
    "browser_go_forward", "browser_reload", "browser_wait_for",
    "browser_console_messages", "browser_network_requests",
}
_HIDDEN_PLAYWRIGHT_MARKERS = ("run_code", "evaluate", "screenshot", "storage_state")
_SAFE_ENV_NAMES = {
    "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP", "LANG",
    "LC_ALL", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "DOCKER_HOST", "DOCKER_CONTEXT",
    "DOCKER_CONFIG", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
}


def _server_environment(extra: dict[str, str]) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if key in _SAFE_ENV_NAMES or key.casefold() in {
            "http_proxy", "https_proxy", "no_proxy",
        }
    }
    env.update(extra)
    return env


class MCPClient:
    """Own connected server processes for one Oryn process."""

    def __init__(self, configs: list[MCPServerConfig] | None = None) -> None:
        self.configs = configs if configs is not None else server_configs()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._stacks: dict[str, AsyncExitStack] = {}
        self._clients: dict[str, Any] = {}
        self._bindings: dict[str, tuple[str, str, Any]] = {}
        self._schemas: list[dict[str, Any]] = []
        self._statuses: list[str] = []

    def start(self) -> list[str]:
        with self._lock:
            if self._closed:
                return list(self._statuses)
            if self._started:
                return list(self._statuses)
            self._started = True
            if not self.configs:
                return []
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._run_loop, name="oryn-mcp", daemon=True)
            self._thread.start()
            future = asyncio.run_coroutine_threadsafe(self._connect_servers(), self._loop)
        try:
            future.result(timeout=240)
        except TimeoutError:
            future.cancel()
            self.close()
            self._statuses = ["MCP startup timed out; Oryn will continue without MCP tools."]
        except Exception as exc:
            self._statuses = [
                f"MCP startup failed ({type(exc).__name__}); Oryn will continue without MCP tools."
            ]
        return list(self._statuses)

    def tool_schemas(self) -> list[dict[str, Any]]:
        self.start()
        return list(self._schemas)

    def requires_approval(self, provider_name: str, arguments: dict[str, Any] | None = None) -> bool:
        binding = self._bindings.get(provider_name)
        if binding is None:
            return True
        server_name, tool_name, _ = binding
        if server_name != "playwright":
            return False
        if tool_name not in _SAFE_PLAYWRIGHT_TOOLS:
            return True
        return tool_name == "browser_navigate" and self._private_or_unverified_url(
            (arguments or {}).get("url")
        )

    @staticmethod
    def _validate_browser_url(value: Any) -> None:
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError("Playwright navigation needs an HTTP or HTTPS URL under 2,048 characters.")
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError
            parsed.port
        except ValueError as exc:
            raise ValueError("Playwright navigation only accepts a valid HTTP or HTTPS URL.") from exc

    @classmethod
    def _private_or_unverified_url(cls, value: Any) -> bool:
        try:
            cls._validate_browser_url(value)
            parsed = urlsplit(value)
            host = parsed.hostname
            assert host is not None
            if host == "localhost" or host.endswith(".localhost"):
                return True
            try:
                addresses = [ipaddress.ip_address(host)]
            except ValueError:
                addresses = [
                    ipaddress.ip_address(record[4][0])
                    for record in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
                ]
            return not addresses or any(not address.is_global for address in addresses)
        except (OSError, ValueError):
            return True

    def call_tool(
        self, provider_name: str, arguments: dict[str, Any], cancel_event: threading.Event | None = None,
    ) -> str:
        self.start()
        binding = self._bindings.get(provider_name)
        if binding is None:
            raise ValueError("That MCP tool is not connected or was not discovered.")
        if not isinstance(arguments, dict):
            raise ValueError("MCP tool arguments must be an object.")
        server_name, tool_name, client = binding
        if server_name == "playwright" and tool_name == "browser_navigate":
            self._validate_browser_url(arguments.get("url"))
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(
            client.call_tool(tool_name, arguments), self._loop
        )
        deadline = time.monotonic() + 90
        while True:
            if cancel_event and cancel_event.is_set():
                future.cancel()
                raise InterruptedError("MCP tool call cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                future.cancel()
                raise RuntimeError(f"{server_name} MCP tool timed out after 90 seconds.")
            try:
                result = future.result(timeout=min(0.2, remaining))
                break
            except TimeoutError as exc:
                if future.done():
                    raise RuntimeError(f"{server_name} MCP tool timed out.") from exc
                continue
        return result_text(result)

    async def _connect_servers(self) -> None:
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters

        async def connect(config: MCPServerConfig) -> str:
            if config.disabled_reason:
                return f"{config.name}: not connected. {config.disabled_reason}"
            command = shutil.which(config.command)
            if command is None:
                return f"{config.name}: not connected; '{config.command}' is not installed."
            try:
                count = await asyncio.wait_for(
                    self._connect_one(Client, StdioServerParameters, config, command),
                    timeout=75,
                )
            except asyncio.TimeoutError:
                return f"{config.name}: connection timed out; Oryn skipped this server."
            except Exception as exc:
                detail = " ".join(str(exc).split())
                for secret in config.env.values():
                    if secret:
                        detail = detail.replace(secret, "[redacted]")
                reason = detail[:180] or type(exc).__name__
                return f"{config.name}: unavailable ({reason}); Oryn skipped it."
            return f"{config.name}: connected with {count} tool(s)."

        self._statuses = list(await asyncio.gather(*(connect(config) for config in self.configs)))

    async def _connect_one(
        self, client_type: Any, parameters_type: Any, config: MCPServerConfig, command: str,
    ) -> int:
        stack = AsyncExitStack()
        try:
            params = parameters_type(
                command=command,
                args=list(config.args),
                env=_server_environment(config.env),
                cwd=Path.home(),
            )
            # The GitHub server rejects the SDK's auto-negotiation probe.
            mode = "legacy" if config.name == "github" else "auto"
            client = await stack.enter_async_context(client_type(params, mode=mode))
            tools = []
            cursor = None
            while True:
                page = await client.list_tools(cursor=cursor)
                tools.extend(page.tools)
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor

            registered = []
            for tool in tools:
                tool_name = tool.name
                if config.name == "playwright" and any(
                    marker in tool_name.casefold() for marker in _HIDDEN_PLAYWRIGHT_MARKERS
                ):
                    continue
                try:
                    schema = provider_tool(config.name, tool)
                except (AttributeError, TypeError, ValueError):
                    continue
                provider_name = provider_tool_name(config.name, tool_name)
                if provider_name in self._bindings:
                    continue
                self._bindings[provider_name] = (config.name, tool_name, client)
                registered.append(schema)
            self._clients[config.name] = client
            self._stacks[config.name] = stack
            self._schemas.extend(registered)
            return len(registered)
        except BaseException:
            await stack.aclose()
            raise

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
        if loop and loop.is_running():
            async def close_servers() -> None:
                for stack in reversed(list(self._stacks.values())):
                    try:
                        await stack.aclose()
                    except Exception:
                        pass
                self._stacks.clear()
                self._clients.clear()

            future = asyncio.run_coroutine_threadsafe(close_servers(), loop)
            try:
                future.result(timeout=15)
            except Exception:
                future.cancel()
            loop.call_soon_threadsafe(loop.stop)
        if thread:
            thread.join(timeout=5)
        if loop and not loop.is_running():
            loop.close()

    def __enter__(self) -> "MCPClient":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
