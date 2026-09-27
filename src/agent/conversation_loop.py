"""Run a model turn, executing requested tools until the model returns text."""

import json
import warnings
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any

from src.agent.context import select_context
from src.mcp.adapter import mcp_loader_tool
from src.providers.types import ModelResponse, ToolCall
from src.tools.browser_tools import BrowserSession
from src.tools.file_tools import FileChange
from src.tools.registry import execute_tool


Message = dict[str, Any]
MAX_TOOL_RESULT_CHARS = 20_000


class TurnCancelled(Exception):
    """The user stopped the active agent turn."""


def run_turn(
    messages: list[dict[str, Any]],
    complete: Callable[..., ModelResponse],
    tools: list[dict[str, Any]],
    project_root: Path,
    confirm_terminal: Callable[[str], bool],
    confirm_write: Callable[[str, str, bool], bool],
    on_text_delta: Callable[[str], None] | None = None,
    on_tool_event: Callable[[str, ToolCall, str | None], None] | None = None,
    cancel_event: Event | None = None,
    confirm_edit: Callable[[str, str], bool] | None = None,
    confirm_undo: Callable[[str, str, bool], bool] | None = None,
    undo_history: list[FileChange] | None = None,
    mcp_client: Any | None = None,
    confirm_mcp: Callable[[str, str], bool] | None = None,
) -> str:
    """Keep the tool cycle in the harness; return only when the model is done."""
    browser = BrowserSession()
    native_tools = [
        tool for tool in tools
        if not tool["name"].startswith("mcp__") and tool["name"] != "load_mcp_tools"
    ]
    loaded_servers: set[str] = set()

    def check_cancelled() -> None:
        if cancel_event and cancel_event.is_set():
            raise TurnCancelled

    try:
        for _ in range(8):
            check_cancelled()
            tools = list(native_tools)
            if mcp_client is not None:
                directory = mcp_client.tool_directory()
                if directory:
                    tools.append(mcp_loader_tool(directory))
                for server in sorted(loaded_servers):
                    try:
                        tools.extend(mcp_client.tool_schemas(server))
                    except ValueError:
                        # A disconnected server must not remain callable from stale definitions.
                        loaded_servers.discard(server)
            advertised_names = {tool["name"] for tool in tools}
            check_cancelled()
            if on_text_delta:
                saw_delta = False

                def emit(delta: str) -> None:
                    nonlocal saw_delta
                    check_cancelled()
                    if delta:
                        saw_delta = True
                        on_text_delta(delta)

                try:
                    kwargs = {"on_text_delta": emit}
                    if cancel_event is not None:
                        kwargs["cancel_event"] = cancel_event
                    response = complete(select_context(messages), tools, **kwargs)
                except Exception as exc:
                    if cancel_event and cancel_event.is_set():
                        raise TurnCancelled from exc
                    raise
                if response.text and not saw_delta:
                    # A non-streaming provider still gets one visible reply.
                    check_cancelled()
                    on_text_delta(response.text)
            else:
                try:
                    kwargs = {"cancel_event": cancel_event} if cancel_event is not None else {}
                    response = complete(select_context(messages), tools, **kwargs)
                except Exception as exc:
                    if cancel_event and cancel_event.is_set():
                        raise TurnCancelled from exc
                    raise
            check_cancelled()
            if not response.tool_calls:
                return response.text

            call_message = {
                "role": "assistant",
                "content": response.text,
                "tool_calls": [
                    {"id": call.id, "name": call.name, "arguments": call.arguments}
                    for call in response.tool_calls
                ],
            }
            completed_calls = []
            tool_messages = []
            for call in response.tool_calls:
                if cancel_event and cancel_event.is_set():
                    break
                if on_tool_event:
                    on_tool_event("start", call, None)
                if cancel_event and cancel_event.is_set():
                    break
                try:
                    if call.name == "load_mcp_tools":
                        if mcp_client is None:
                            raise RuntimeError("MCP loading is unavailable without an active MCP client.")
                        if not isinstance(call.arguments, dict) or set(call.arguments) != {"server"}:
                            raise ValueError("load_mcp_tools requires exactly one argument: server.")
                        server = call.arguments["server"]
                        if not isinstance(server, str) or not server:
                            raise ValueError("server must be a name from the MCP directory, e.g. github.")
                        schemas = mcp_client.tool_schemas(server)
                        if not schemas:
                            raise ValueError("This server has no usable tools. Reconnect it in /mcps.")
                        loaded_servers.add(server)
                        result = json.dumps({
                            "server": server, "tools": [schema["name"] for schema in schemas],
                            "message": "Definitions loaded. Call these tools using the schemas in the next request.",
                        }, ensure_ascii=False)
                    elif call.name.startswith("mcp__"):
                        if mcp_client is None:
                            raise RuntimeError("MCP tool was requested without an active MCP client.")
                        if call.name not in advertised_names:
                            raise ValueError(
                                "This MCP tool is not loaded for this request. Use load_mcp_tools "
                                "with a connected server from the MCP directory, then choose an advertised tool."
                            )
                        if mcp_client.requires_approval(call.name, call.arguments):
                            if confirm_mcp is None:
                                raise RuntimeError("This MCP action needs user approval; no approval handler is available.")
                            preview = json.dumps(call.arguments, ensure_ascii=False, indent=2)
                            if len(preview) > 12_000:
                                raise ValueError("MCP action arguments are too large to review safely.")
                            if not confirm_mcp(call.name, preview):
                                result = "The user denied this MCP action; it was not run."
                            else:
                                check_cancelled()
                                result = mcp_client.call_tool(
                                    call.name, call.arguments, cancel_event=cancel_event
                                )
                        else:
                            result = mcp_client.call_tool(
                                call.name, call.arguments, cancel_event=cancel_event
                            )
                    else:
                        args = (call.name, call.arguments, project_root, confirm_terminal, confirm_write)
                        if call.name == "write_file":
                            result = execute_tool(*args, undo_history=undo_history)
                        elif call.name == "edit_file":
                            result = execute_tool(
                                *args, confirm_edit=confirm_edit, undo_history=undo_history,
                            )
                        elif call.name == "undo_file_change":
                            result = execute_tool(
                                *args, confirm_undo=confirm_undo, undo_history=undo_history,
                            )
                        elif call.name.startswith("browser_"):
                            result = execute_tool(*args, browser=browser)
                        else:
                            result = execute_tool(*args)
                except Exception as exc:
                    result = f"Tool error: {exc}. Correct the arguments or try another approach."
                if len(result) > MAX_TOOL_RESULT_CHARS:
                    marker = f"\n[Tool output truncated; original result was {len(result)} characters.]"
                    result = result[:MAX_TOOL_RESULT_CHARS - len(marker)] + marker
                if on_tool_event:
                    on_tool_event("result", call, result)
                completed_calls.append(call)
                tool_messages.append({
                    "role": "tool", "tool_call_id": call.id,
                    "name": call.name, "content": result,
                })
            call_message["tool_calls"] = [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in completed_calls
            ]
            # Store the call and every output together so a failed turn cannot save an orphan call.
            if completed_calls:
                messages.extend([call_message, *tool_messages])
            check_cancelled()
        raise RuntimeError("The model requested tools in 8 consecutive rounds. Please narrow the request.")
    finally:
        try:
            browser.close()
        except RuntimeError as exc:
            warnings.warn(f"Could not close Firecrawl browser session: {exc}", RuntimeWarning)
