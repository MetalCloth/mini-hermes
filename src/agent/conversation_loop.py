"""Run a model turn, executing requested tools until the model returns text."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.agent.context import select_context
from src.providers.types import ModelResponse
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
) -> str:
    """Keep the tool cycle in the harness; return only when the model is done."""
    for _ in range(8):
        response = complete(select_context(messages), tools)
        if not response.tool_calls:
            return response.text

        messages.append({
            "role": "assistant",
            "content": response.text,
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in response.tool_calls
            ],
        })
        for call in response.tool_calls:
            try:
                result = execute_tool(
                    call.name, call.arguments, project_root, confirm_terminal, confirm_write
                )
            except Exception as exc:
                result = f"Tool error: {exc}. Correct the arguments or try another approach."
            if len(result) > MAX_TOOL_RESULT_CHARS:
                marker = f"\n[Tool output truncated; original result was {len(result)} characters.]"
                result = result[:MAX_TOOL_RESULT_CHARS - len(marker)] + marker
            messages.append({
                "role": "tool", "tool_call_id": call.id,
                "name": call.name, "content": result,
            })
    raise RuntimeError("The model requested tools in 8 consecutive rounds. Please narrow the request.")
