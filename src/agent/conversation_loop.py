"""Run a model turn, executing requested tools until the model returns text."""

import json
import re
import time
import uuid
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Timer
from typing import Any

from src.agent.compression import compact_for_request
from src.computer import (
    MAX_CALLS as COMPUTER_MAX_CALLS, MAX_SECONDS as COMPUTER_MAX_SECONDS,
    ComputerBoundaryError, ComputerScope, calculator_windows,
)
from src.mcp.adapter import MCPImageResult, mcp_loader_tool
from src.providers.types import ModelResponse, ProviderRequestError, ToolCall
from src.tools.browser_tools import BrowserSession, FirecrawlHTTPError
from src.tools.file_tools import FileChange
from src.tools.registry import execute_tool, tool_schemas
from src.tools.terminal_tool import TerminalJobManager


Message = dict[str, Any]
MAX_TOOL_RESULT_CHARS = 20_000


class TurnCancelled(Exception):
    """The user stopped the active agent turn."""


class TurnLimitReached(RuntimeError):
    """A bounded turn paused; its completed work remains available to continue."""


@dataclass(frozen=True)
class TurnLimits:
    max_rounds: int = 40
    max_tool_calls: int = 200
    max_turn_seconds: int = 1200

    def __post_init__(self) -> None:
        for name, ceiling in (("max_rounds", 200), ("max_tool_calls", 2000), ("max_turn_seconds", 7200)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError(f"{name} must be an integer between 1 and {ceiling}.")


def add_turn_arguments(parser) -> None:
    """Share the same bounded turn options across all three launchers."""
    for name, default, help_text in (
        ("max-rounds", 40, "model rounds per turn (1–200; default: 40)"),
        ("max-tool-calls", 200, "tool calls per turn (1–2000; default: 200)"),
        ("max-turn-seconds", 1200, "turn wall-time budget (1–7200; default: 1200)"),
    ):
        parser.add_argument(f"--{name}", type=int, default=default, help=help_text)


def turn_limits_from_args(args) -> TurnLimits:
    return TurnLimits(args.max_rounds, args.max_tool_calls, args.max_turn_seconds)


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
    limits: TurnLimits | None = None,
    on_status: Callable[[str], None] | None = None,
    load_context_summary: Callable[[], tuple[str, int, str] | None] | None = None,
    save_context_summary: Callable[[str, int, str], None] | None = None,
    file_change_journal: Callable[[str, FileChange], int | None] | None = None,
    terminal_jobs: TerminalJobManager | None = None,
    on_diagnostic: Callable[[str, dict[str, Any]], None] | None = None,
    tool_allowlist: set[str] | None = None,
    computer_scope: ComputerScope | None = None,
) -> str:
    """Keep the tool cycle in the harness; return only when the model is done."""
    browser = BrowserSession()
    native_tools = [
        tool for tool in tools
        if not tool["name"].startswith("mcp__") and tool["name"] != "load_mcp_tools"
    ]
    if computer_scope is not None:
        native_tools = []
        tool_allowlist = {"load_mcp_tools"} | {
            f"mcp__computer__{name}" for name in ("get_app_state", "click")
        }
    local_tool_names = {tool["name"] for tool in tool_schemas()} | {"load_mcp_tools", "load_skill"}
    from src.agent.skills import discover_skills, skill_loader_tool
    if tool_allowlist is None or "load_skill" in tool_allowlist:
        available_skills, skill_problems = discover_skills(project_root)
    else:
        available_skills, skill_problems = {}, []
    loaded_skills: set[str] = set()
    loaded_servers: set[str] = set()
    limits = limits or TurnLimits()
    if computer_scope is not None:
        limits = TurnLimits(limits.max_rounds, min(limits.max_tool_calls, COMPUTER_MAX_CALLS),
                            min(limits.max_turn_seconds, COMPUTER_MAX_SECONDS))
    transient_images: dict[str, list[dict[str, Any]]] = {}
    turn_cancel = cancel_event if cancel_event is not None else Event()
    deadline = time.monotonic() + limits.max_turn_seconds
    timer = Timer(limits.max_turn_seconds, turn_cancel.set)
    timer.daemon = True
    timer.start()
    tool_count = 0
    model_requests = 0
    turn_id = uuid.uuid4().hex
    turn_started = time.monotonic()

    def diagnostic(event_type: str, **fields: Any) -> None:
        if on_diagnostic is not None:
            try:
                on_diagnostic(turn_id, {"type": event_type, **fields})
            except Exception:
                # Diagnostics must never break a user turn.
                pass

    summary, covered_messages, covered_digest = "", 0, ""
    model_name = getattr(getattr(complete, "__self__", None), "model", "unknown")
    if not isinstance(model_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./_:-]{0,119}", model_name):
        model_name = "unknown"

    def check_cancelled() -> None:
        if time.monotonic() >= deadline:
            raise TurnLimitReached(
                f"Turn paused at its {limits.max_turn_seconds}-second time budget. "
                "Completed work is retained. Ask to continue with a new turn budget."
            )
        if turn_cancel.is_set():
            raise TurnCancelled

    def guarded_approval(callback):
        if callback is None:
            return None

        def approve(*args):
            check_cancelled()
            approved = callback(*args)
            check_cancelled()
            return approved

        return approve

    confirm_terminal = guarded_approval(confirm_terminal)
    confirm_write = guarded_approval(confirm_write)
    confirm_edit = guarded_approval(confirm_edit)
    confirm_undo = guarded_approval(confirm_undo)
    confirm_mcp = guarded_approval(confirm_mcp)

    def call_mcp(call: ToolCall, arguments: dict[str, Any] | None = None) -> str | MCPImageResult:
        kwargs = {"cancel_event": turn_cancel}

        def retry(attempt: int, delay: float) -> None:
            diagnostic("retry", retry_attempt=attempt, delay_ms=round(delay * 1000))
            if on_status:
                on_status(f"Temporary MCP read failure · {call.name} · retry {attempt}/3 in {delay:g}s")

        kwargs["on_retry"] = retry
        return mcp_client.call_tool(call.name, arguments if arguments is not None else call.arguments, **kwargs)

    try:
        saved_summary = load_context_summary() if load_context_summary else None
        if isinstance(saved_summary, tuple) and len(saved_summary) == 3:
            summary, covered_messages, covered_digest = saved_summary
        diagnostic("turn_start", model=model_name)
        if on_status:
            on_status(f"Turn budget: {limits.max_rounds} rounds · {limits.max_tool_calls} tools · {limits.max_turn_seconds}s")
        for round_number in range(1, limits.max_rounds + 1):
            check_cancelled()
            tools = list(native_tools)
            remaining_skills = {
                name: skill for name, skill in available_skills.items() if name not in loaded_skills
            }
            if remaining_skills and (tool_allowlist is None or "load_skill" in tool_allowlist):
                tools.append(skill_loader_tool(remaining_skills))
            if mcp_client is not None:
                directory = mcp_client.tool_directory()
                if computer_scope is not None:
                    directory = [entry for entry in directory if entry["server"] == "computer"]
                else:
                    directory = [entry for entry in directory if entry["server"] != "computer"]
                if directory:
                    tools.append(mcp_loader_tool(directory))
                for server in sorted(loaded_servers):
                    try:
                        schemas = mcp_client.tool_schemas(server)
                        tools.extend(computer_scope.schemas(schemas) if computer_scope else schemas)
                    except ValueError:
                        # A disconnected server must not remain callable from stale definitions.
                        loaded_servers.discard(server)
            advertised_names = {tool["name"] for tool in tools}
            check_cancelled()
            for attempt in range(3):
                saw_delta = False

                def emit(delta: str) -> None:
                    nonlocal saw_delta
                    check_cancelled()
                    if delta:
                        saw_delta = True
                        if on_text_delta:
                            on_text_delta(delta)

                try:
                    kwargs = {"cancel_event": turn_cancel}
                    if on_text_delta:
                        kwargs["on_text_delta"] = emit
                    def summary_request(estimated_tokens: int) -> None:
                        nonlocal model_requests
                        model_requests += 1
                        diagnostic(
                            "model_request", round=round_number,
                            estimated_tokens=estimated_tokens, tool_count=0,
                            loaded_tool_count=0,
                        )

                    def compaction_event(source_tokens: int, summary_tokens: int) -> None:
                        diagnostic(
                            "compaction", estimated_tokens=source_tokens,
                            summary_tokens=summary_tokens,
                        )

                    request_messages, summary, covered_messages, covered_digest, request_tokens = compact_for_request(
                        messages, tools, complete, summary, covered_messages, covered_digest,
                        cancel_event=turn_cancel, on_status=on_status,
                        on_summary_request=summary_request,
                        on_compaction=compaction_event,
                        save_summary=save_context_summary,
                        model=getattr(getattr(complete, "__self__", None), "model", None),
                    )
                    if transient_images:
                        request_messages = [
                            {**message, "images": transient_images[message["tool_call_id"]]}
                            if message.get("role") == "tool" and message.get("tool_call_id") in transient_images
                            else message for message in request_messages
                        ]
                    if computer_scope is not None:
                        request_messages.append({"role": "developer", "content": computer_scope.instruction()})
                    model_requests += 1
                    diagnostic(
                        "model_request", round=round_number,
                        estimated_tokens=request_tokens, tool_count=len(tools),
                        loaded_tool_count=sum(name.startswith("mcp__") for name in advertised_names),
                    )
                    response = complete(request_messages, tools, **kwargs)
                except ProviderRequestError as exc:
                    check_cancelled()
                    if not exc.retryable or saw_delta or attempt == 2:
                        raise
                    delay = max(0.5 * 2 ** attempt, min(30.0, exc.retry_after))
                    diagnostic("retry", retry_attempt=attempt + 1, delay_ms=round(delay * 1000))
                    if on_status:
                        on_status(f"Temporary model request failure · retry {attempt + 2}/3 in {delay:g}s")
                    turn_cancel.wait(min(delay, max(0.0, deadline - time.monotonic())))
                    check_cancelled()
                    continue
                except Exception as exc:
                    if turn_cancel.is_set():
                        check_cancelled()
                    raise
                if on_text_delta and response.text and not saw_delta:
                    # A non-streaming provider still gets one visible reply.
                    check_cancelled()
                    on_text_delta(response.text)
                break
            check_cancelled()
            if not response.tool_calls:
                diagnostic(
                    "turn_end", elapsed_ms=round((time.monotonic() - turn_started) * 1000),
                    tool_count=tool_count, model_requests=model_requests,
                )
                return response.text

            call_message = {
                "role": "assistant",
                "content": response.text,
                "tool_calls": [],
            }
            completed_calls = []
            for call in response.tool_calls:
                if turn_cancel.is_set():
                    break
                if tool_count >= limits.max_tool_calls:
                    break
                diagnostic_tool_name = (
                    call.name if call.name in local_tool_names else
                    "mcp_tool" if call.name.startswith("mcp__") and call.name in advertised_names else
                    "unknown_tool"
                )
                if on_tool_event:
                    on_tool_event("start", call, None)
                tool_started = time.monotonic()
                diagnostic("tool_start", tool_name=diagnostic_tool_name)
                if turn_cancel.is_set():
                    break
                check_cancelled()
                tool_count += 1
                tool_error_class = None
                upstream_metadata: dict[str, Any] = {}
                try:
                    if tool_allowlist is not None and call.name not in tool_allowlist:
                        raise ValueError("This tool is outside the allowed set for this turn and was not run.")
                    if call.name == "load_skill":
                        from src.agent.skills import load_skill

                        if not isinstance(call.arguments, dict) or set(call.arguments) != {"name"}:
                            raise ValueError("load_skill requires exactly one argument: name.")
                        skill = load_skill(project_root, call.arguments["name"])
                        loaded_skills.add(skill.name)
                        diagnostic("skill_loaded", skill_name=skill.name)
                        if on_status:
                            on_status(f"Loaded skill · {skill.name}")
                        result = (
                            f"Loaded skill '{skill.name}' for this user turn. Apply its instructions "
                            f"only where relevant to the user's request.\n\n{skill.instructions}"
                        )
                    elif call.name == "delegate_read_only":
                        from src.agent.subagents import run_read_only_subagent

                        if not isinstance(call.arguments, dict) or set(call.arguments) != {"task"}:
                            raise ValueError("delegate_read_only requires exactly one argument: task.")
                        task = call.arguments["task"]
                        diagnostic("subagent_start", task_chars=len(task) if isinstance(task, str) else 0,
                                   allowed_tool_count=4)
                        if on_status:
                            on_status("Delegating a bounded, read-only inspection")
                        worker_result = run_read_only_subagent(task, complete, project_root, turn_cancel)
                        usage = worker_result.get("usage", {})
                        diagnostic(
                            "subagent_end", status=worker_result.get("status", "failed"),
                            elapsed_ms=usage.get("elapsed_ms", 0), tool_count=usage.get("tool_calls", 0),
                            model_requests=usage.get("model_requests", 0),
                        )
                        result = json.dumps(worker_result, ensure_ascii=False)
                    elif call.name == "load_mcp_tools":
                        if mcp_client is None:
                            raise RuntimeError("MCP loading is unavailable without an active MCP client.")
                        if not isinstance(call.arguments, dict) or set(call.arguments) != {"server"}:
                            raise ValueError("load_mcp_tools requires exactly one argument: server.")
                        server = call.arguments["server"]
                        if not isinstance(server, str) or not server:
                            raise ValueError("server must be a name from the MCP directory, e.g. github.")
                        if server == "computer" and computer_scope is None:
                            raise ValueError("Use /computer to arm a selected desktop window first.")
                        if computer_scope is not None and server != "computer":
                            raise ValueError("Only the selected computer server is available for this task.")
                        schemas = mcp_client.tool_schemas(server)
                        if computer_scope is not None:
                            schemas = computer_scope.schemas(schemas)
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
                        if computer_scope is not None:
                            tool_name = call.name.removeprefix("mcp__computer__")
                            arguments = computer_scope.prepare(tool_name, call.arguments)
                            raw_windows = mcp_client.call_tool(
                                "mcp__computer__list_windows", {}, cancel_event=turn_cancel,
                            )
                            try:
                                current = next((window for window in calculator_windows(raw_windows)
                                                if window["window_id"] == computer_scope.window_id), None)
                            except ValueError as exc:
                                raise ComputerBoundaryError("Could not verify the selected Calculator window.") from exc
                            if current is None or (current["width"], current["height"]) != (
                                computer_scope.width, computer_scope.height,
                            ):
                                raise ComputerBoundaryError(
                                    "Selected Calculator window changed; /computer must be armed again."
                                )
                            focus = mcp_client.call_tool(
                                "mcp__computer__activate_window",
                                {"window_id": computer_scope.window_id}, cancel_event=turn_cancel,
                            )
                            try:
                                focused = isinstance(focus, str) and json.loads(focus).get("focus", {}).get(
                                    "exact_window_focused"
                                ) is True
                            except (ValueError, AttributeError):
                                focused = False
                            if not focused:
                                raise ComputerBoundaryError("Could not focus the selected Calculator window.")
                            result = call_mcp(call, arguments)
                            text_result = result.text if isinstance(result, MCPImageResult) else result
                            if text_result.startswith("MCP server reported a tool error:"):
                                raise ComputerBoundaryError(text_result[:500])
                            if tool_name == "click":
                                try:
                                    clicked = json.loads(text_result).get("ok") is True
                                except (ValueError, AttributeError):
                                    clicked = False
                                if not clicked:
                                    raise ComputerBoundaryError("Calculator click failed; computer task stopped.")
                        elif mcp_client.requires_approval(call.name, call.arguments):
                            if confirm_mcp is None:
                                raise RuntimeError("This MCP action needs user approval; no approval handler is available.")
                            preview = json.dumps(call.arguments, ensure_ascii=False, indent=2)
                            if len(preview) > 12_000:
                                raise ValueError("MCP action arguments are too large to review safely.")
                            if not confirm_mcp(call.name, preview):
                                result = "The user denied this MCP action; it was not run."
                            else:
                                check_cancelled()
                                result = call_mcp(call)
                        else:
                            result = call_mcp(call)
                    else:
                        args = (call.name, call.arguments, project_root, confirm_terminal, confirm_write)
                        if call.name == "write_file":
                            result = execute_tool(
                                *args, undo_history=undo_history,
                                file_change_journal=file_change_journal,
                            )
                        elif call.name == "edit_file":
                            result = execute_tool(
                                *args, confirm_edit=confirm_edit, undo_history=undo_history,
                                file_change_journal=file_change_journal,
                            )
                        elif call.name == "undo_file_change":
                            result = execute_tool(
                                *args, confirm_undo=confirm_undo, undo_history=undo_history,
                                file_change_journal=file_change_journal,
                            )
                        elif call.name.startswith("browser_"):
                            result = execute_tool(*args, browser=browser)
                        elif call.name.startswith("terminal"):
                            result = execute_tool(
                                *args, terminal_jobs=terminal_jobs, cancel_event=turn_cancel,
                            )
                        else:
                            result = execute_tool(*args)
                except (TurnCancelled, InterruptedError) as exc:
                    tool_error_class = type(exc).__name__
                    result = f"Tool cancelled or interrupted: {exc}. Inspect any effects before repeating it."
                except TurnLimitReached as exc:
                    tool_error_class = type(exc).__name__
                    result = f"Tool paused before completion: {exc}"
                except ComputerBoundaryError:
                    raise
                except Exception as exc:
                    tool_error_class = type(exc).__name__
                    if isinstance(exc, FirecrawlHTTPError):
                        upstream_metadata = {
                            "upstream_status": exc.status_code,
                            **({"upstream_request_id": exc.request_id} if exc.request_id else {}),
                            **({"upstream_detail": exc.detail} if exc.detail else {}),
                        }
                    result = f"Tool error: {exc}. Correct the arguments or try another approach."
                result_images = []
                if isinstance(result, MCPImageResult):
                    result_images, result = result.images, result.text
                result_lower = result.casefold()
                tool_succeeded = (
                    tool_error_class is None
                    and not result_lower.startswith("tool error:")
                    and "cancelled" not in result_lower
                    and "denied" not in result_lower
                )
                if len(result) > MAX_TOOL_RESULT_CHARS:
                    marker = f"\n[Tool output truncated; original result was {len(result)} characters.]"
                    result = result[:MAX_TOOL_RESULT_CHARS - len(marker)] + marker
                completed_calls.append(call)
                # Record each completed pair before notifying the UI, which can disconnect.
                if len(completed_calls) == 1:
                    messages.append(call_message)
                call_message["tool_calls"].append({
                    "id": call.id, "name": call.name, "arguments": call.arguments,
                })
                messages.append({
                    "role": "tool", "tool_call_id": call.id,
                    "name": call.name, "content": result,
                })
                if result_images:
                    transient_images.clear()
                    transient_images[call.id] = result_images
                diagnostic(
                    "tool_end", tool_name=diagnostic_tool_name, success=tool_succeeded,
                    elapsed_ms=round((time.monotonic() - tool_started) * 1000),
                    **({"error_class": tool_error_class} if tool_error_class else {}),
                    **upstream_metadata,
                )
                if on_tool_event:
                    on_tool_event("result", call, result)
            check_cancelled()
            if len(completed_calls) < len(response.tool_calls):
                raise TurnLimitReached(
                    f"Turn paused at its {limits.max_tool_calls}-tool-call budget. "
                    "Completed work is retained. Ask to continue with a new turn budget."
                )
        raise TurnLimitReached(
            f"Turn paused at its {limits.max_rounds}-round budget. "
            "Completed work is retained. Ask to continue with a new turn budget."
        )
    except Exception as exc:
        diagnostic(
            "turn_error", elapsed_ms=round((time.monotonic() - turn_started) * 1000),
            tool_count=tool_count, model_requests=model_requests,
            error_class=type(exc).__name__,
        )
        raise
    finally:
        timer.cancel()
        try:
            browser.close()
        except RuntimeError as exc:
            warnings.warn(f"Could not close Firecrawl browser session: {exc}", RuntimeWarning)
