"""Run a model turn, executing requested tools until the model returns text."""

import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.agent.context import select_context
from src.providers.types import ModelResponse, ToolCall
from src.tools.browser_tools import BrowserSession
from src.tools.registry import execute_tool


Message = dict[str, Any]
MAX_TOOL_RESULT_CHARS = 20_000


def run_turn(
    messages: list[dict[str, Any]],
    complete: Callable[..., ModelResponse],
    tools: list[dict[str, Any]],
    project_root: Path,
    confirm_terminal: Callable[[str], bool],
    confirm_write: Callable[[str, str, bool], bool],
    on_text_delta: Callable[[str], None] | None = None,
    on_tool_event: Callable[[str, ToolCall, str | None], None] | None = None,
) -> str:
    """Keep the tool cycle in the harness; return only when the model is done."""
    browser = BrowserSession()
    try:
        for _ in range(8):
            if on_text_delta:
                saw_delta = False

                def emit(delta: str) -> None:
                    nonlocal saw_delta
                    if delta:
                        saw_delta = True
                        on_text_delta(delta)

                response = complete(select_context(messages), tools, on_text_delta=emit)
                if response.text and not saw_delta:
                    # A non-streaming provider still gets one visible reply.
                    on_text_delta(response.text)
            else:
                response = complete(select_context(messages), tools)
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
            tool_messages = []
            for call in response.tool_calls:
                if on_tool_event:
                    on_tool_event("start", call, None)
                try:
                    args = (call.name, call.arguments, project_root, confirm_terminal, confirm_write)
                    result = execute_tool(*args, browser=browser) if call.name.startswith("browser_") else execute_tool(*args)
                except Exception as exc:
                    result = f"Tool error: {exc}. Correct the arguments or try another approach."
                if len(result) > MAX_TOOL_RESULT_CHARS:
                    marker = f"\n[Tool output truncated; original result was {len(result)} characters.]"
                    result = result[:MAX_TOOL_RESULT_CHARS - len(marker)] + marker
                if on_tool_event:
                    on_tool_event("result", call, result)
                tool_messages.append({
                    "role": "tool", "tool_call_id": call.id,
                    "name": call.name, "content": result,
                })
            # Store the call and every output together so a failed turn cannot save an orphan call.
            messages.extend([call_message, *tool_messages])
        raise RuntimeError("The model requested tools in 8 consecutive rounds. Please narrow the request.")
    finally:
        try:
            browser.close()
        except RuntimeError as exc:
            warnings.warn(f"Could not close Firecrawl browser session: {exc}", RuntimeWarning)
