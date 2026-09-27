"""Synchronous bridge to hosted MCP connections and the local Playwright client."""

import asyncio
import ipaddress
import os
import shutil
import socket
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from src.mcp.adapter import provider_tool, provider_tool_name, result_text
from src.mcp.discovery import MCPServerConfig, SERVER_NAMES, server_configs


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
_TOOL_CALL_TIMEOUT_SECONDS = 90


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
        self._uses_presets = configs is None
        self.configs = configs if configs is not None else server_configs()
        self._lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._stacks: dict[str, AsyncExitStack] = {}
        self._server_tasks: dict[str, asyncio.Task[None]] = {}
        self._server_stops: dict[str, asyncio.Event] = {}
        self._clients: dict[str, Any] = {}
        self._bindings: dict[str, tuple[str, str, Any]] = {}
        self._schemas: list[dict[str, Any]] = []
        self._read_only_tools: set[str] = set()
        self._statuses: list[str] = []
        self._status_by_name: dict[str, tuple[str, str]] = {}

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
            self._status_by_name.update({
                config.name: ("unavailable", self._statuses[0]) for config in self.configs
            })
        except Exception as exc:
            message = f"MCP startup failed ({type(exc).__name__}); Oryn will continue without MCP tools."
            self._statuses = [message]
            self._status_by_name.update({config.name: ("unavailable", message) for config in self.configs})
        return list(self._statuses)

    def tool_schemas(self) -> list[dict[str, Any]]:
        self.start()
        return list(self._schemas)

    def status_snapshot(self) -> list[dict[str, Any]]:
        self.start()
        result = []
        for config in self.configs:
            state, message = self._status_by_name.get(
                config.name, ("starting", "Waiting for the MCP server to connect."),
            )
            if not config.enabled:
                state, message = "disabled", "Disabled in Oryn settings."
            result.append({
                "name": config.name,
                "enabled": config.enabled,
                "state": state,
                "message": message,
                "tool_count": sum(server == config.name for server, _, _ in self._bindings.values()),
                "access": config.access,
                "transport": "http" if config.url else "stdio",
            })
        return result

    def set_enabled(self, enabled_servers: set[str]) -> list[dict[str, Any]]:
        """Start or stop only the MCP servers whose user setting changed."""
        if not isinstance(enabled_servers, set) or not enabled_servers <= set(SERVER_NAMES):
            raise ValueError("Choose valid MCP servers.")
        with self._control_lock:
            self.start()
            previous = {config.name: config.enabled for config in self.configs}
            changed = {name for name in previous if previous[name] != (name in enabled_servers)}
            if changed:
                fresh = {c.name: c for c in server_configs(enabled_servers)} if self._uses_presets else {}
                self.configs = [
                    fresh.get(config.name, replace(config, enabled=config.name in enabled_servers))
                    if config.name in changed else config
                    for config in self.configs
                ]
            return self._update_servers(changed)

    def reconnect(self, name: str) -> list[dict[str, Any]]:
        """Reload credentials and replace one connection without restarting Oryn."""
        with self._control_lock:
            self.start()
            config = next((c for c in self.configs if c.name == name), None)
            if config is None:
                raise ValueError("Unknown MCP server.")
            if not config.enabled:
                raise ValueError("Enable this server before reconnecting.")
            if self._uses_presets:
                enabled = {c.name for c in self.configs if c.enabled}
                fresh = next(c for c in server_configs(enabled) if c.name == name)
                self.configs = [fresh if c.name == name else c for c in self.configs]
            return self._update_servers({name})

    def _update_servers(self, changed: set[str]) -> list[dict[str, Any]]:
        if not changed:
            return self.status_snapshot()
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("MCP servers are not running.")
        future = asyncio.run_coroutine_threadsafe(self._reconfigure(changed), loop)
        try:
            future.result(timeout=90 * len(changed) + 15)
        except TimeoutError:
            future.cancel()
            raise RuntimeError("MCP server update timed out; check its connection and try again.")
        self._statuses = [
            f"{config.name}: {self._status_by_name.get(config.name, ('starting', 'Connecting.'))[1]}"
            for config in self.configs
        ]
        return self.status_snapshot()

    def requires_approval(self, provider_name: str, arguments: dict[str, Any] | None = None) -> bool:
        binding = self._bindings.get(provider_name)
        if binding is None:
            return True
        server_name, tool_name, _ = binding
        if server_name != "playwright":
            if server_name in {"github", "context7", "microsoft_learn", "tavily"}:
                return False
            return provider_name not in self._read_only_tools
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
        deadline = time.monotonic() + _TOOL_CALL_TIMEOUT_SECONDS
        while True:
            if cancel_event and cancel_event.is_set():
                future.cancel()
                raise InterruptedError("MCP tool call cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                future.cancel()
                raise RuntimeError(
                    f"{server_name} MCP tool timed out after {_TOOL_CALL_TIMEOUT_SECONDS} seconds."
                )
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

        self._statuses = list(await asyncio.gather(*(
            self._start_config(config, Client, StdioServerParameters)
            for config in self.configs
        )))

    async def _start_config(self, config: MCPServerConfig, client_type: Any, parameters_type: Any) -> str:
        if not config.enabled:
            self._status_by_name[config.name] = ("disabled", "Disabled in Oryn settings.")
            return f"{config.name}: Disabled in Oryn settings."
        ready = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()
        task = asyncio.create_task(
            self._serve_config(config, client_type, parameters_type, ready, stop),
            name=f"oryn-mcp-{config.name}",
        )
        self._server_tasks[config.name] = task
        self._server_stops[config.name] = stop
        try:
            return await asyncio.wait_for(ready, timeout=75)
        except asyncio.TimeoutError:
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._server_tasks.pop(config.name, None)
            self._server_stops.pop(config.name, None)
            message = "Connection timed out; Oryn skipped this server."
            self._status_by_name[config.name] = ("unavailable", message)
            return f"{config.name}: {message}"
        except BaseException:
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._server_tasks.pop(config.name, None)
            self._server_stops.pop(config.name, None)
            raise

    async def _serve_config(
        self, config: MCPServerConfig, client_type: Any, parameters_type: Any,
        ready: asyncio.Future[str], stop: asyncio.Event,
    ) -> None:
        try:
            message = await self._connect_config(config, client_type, parameters_type)
            if not ready.done():
                ready.set_result(message)
            if self._status_by_name.get(config.name, ("", ""))[0] == "connected":
                await stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise
        finally:
            stack = self._stacks.pop(config.name, None)
            self._clients.pop(config.name, None)
            if stack:
                await stack.aclose()

    async def _connect_config(self, config: MCPServerConfig, client_type: Any, parameters_type: Any) -> str:
        if not config.enabled:
            state, message = "disabled", "Disabled in Oryn settings."
            self._status_by_name[config.name] = (state, message)
            return f"{config.name}: {message}"
        if config.disabled_reason:
            state, message = "unavailable", config.disabled_reason
            self._status_by_name[config.name] = (state, message)
            return f"{config.name}: not connected. {message}"
        command = shutil.which(config.command) if not config.url else ""
        if command is None:
            message = f"'{config.command}' is not installed."
            self._status_by_name[config.name] = ("unavailable", message)
            return f"{config.name}: not connected; {message}"
        try:
            count = await self._connect_one(client_type, parameters_type, config, command)
        except Exception as exc:
            while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
                exc = exc.exceptions[0]
            detail = str(exc)
            credentials = list(config.env.values()) + [
                value for key, value in config.headers.items()
                if key.casefold() in {"authorization", "x-api-key"}
            ]
            for secret in credentials:
                if secret:
                    detail = detail.replace(secret, "[redacted]")
                    if secret.startswith("Bearer "):
                        detail = detail.replace(secret[7:], "[redacted]")
            detail = " ".join(detail.split())
            reason = ("Sign-in expired or failed; run ./oryn mcp login notion, then restart Oryn."
                      if config.oauth else detail[:180] or type(exc).__name__)
            message = f"Unavailable ({reason}); Oryn skipped this server."
            self._status_by_name[config.name] = ("unavailable", message)
            return f"{config.name}: {message}"
        message = f"Connected with {count} tool(s)."
        self._status_by_name[config.name] = ("connected", message)
        return f"{config.name}: {message}"

    async def _reconfigure(self, changed: set[str]) -> None:
        for config in self.configs:
            if config.name not in changed:
                continue
            stop = self._server_stops.pop(config.name, None)
            task = self._server_tasks.pop(config.name, None)
            if stop:
                stop.set()
            if task:
                await asyncio.gather(task, return_exceptions=True)
            removed = [name for name, binding in self._bindings.items() if binding[0] == config.name]
            for name in removed:
                self._bindings.pop(name, None)
                self._read_only_tools.discard(name)
            self._schemas = [schema for schema in self._schemas if schema.get("name") not in removed]
            self._status_by_name[config.name] = (
                ("starting", "Connecting.") if config.enabled else ("disabled", "Disabled in Oryn settings.")
            )

        enabled = [config for config in self.configs if config.name in changed and config.enabled]
        if enabled:
            from mcp import Client
            from mcp.client.stdio import StdioServerParameters
            await asyncio.gather(*(
                self._start_config(config, Client, StdioServerParameters)
                for config in enabled
            ))

    async def _connect_one(
        self, client_type: Any, parameters_type: Any, config: MCPServerConfig, command: str,
    ) -> int:
        from mcp.client.stdio import stdio_client

        stack = AsyncExitStack()
        try:
            if config.url:
                from mcp.client.streamable_http import streamable_http_client
                from mcp.shared._httpx_utils import create_mcp_http_client
                from src.mcp.oauth import notion_auth

                http = await stack.enter_async_context(create_mcp_http_client(
                    headers=config.headers, auth=notion_auth() if config.oauth else None,
                ))
                transport = streamable_http_client(config.url, http_client=http)
            else:
                params = parameters_type(
                    command=command, args=list(config.args),
                    env=_server_environment(config.env), cwd=Path.home(),
                )
                # Subprocess notices must not write over the terminal UI.
                errlog = stack.enter_context(open(os.devnull, "w"))
                transport = stdio_client(params, errlog=errlog)
            # Hosted servers currently use the stable initialize handshake.
            mode = "legacy" if config.url or config.name == "github" else "auto"
            client = await stack.enter_async_context(client_type(transport, mode=mode))
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
                if getattr(getattr(tool, "annotations", None), "read_only_hint", None) is True:
                    self._read_only_tools.add(provider_name)
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
                for stop in self._server_stops.values():
                    stop.set()
                if self._server_tasks:
                    await asyncio.gather(*self._server_tasks.values(), return_exceptions=True)
                self._server_stops.clear()
                self._server_tasks.clear()
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
