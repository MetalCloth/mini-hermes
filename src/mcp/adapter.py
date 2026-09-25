"""Convert MCP tool descriptions and results to Oryn's text tool format."""

import json
import re
from typing import Any


_MODEL_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def provider_tool_name(server_name: str, tool_name: str) -> str:
    name = f"mcp__{server_name}__{tool_name}"
    if not _MODEL_TOOL_NAME.fullmatch(name):
        raise ValueError("MCP tool name cannot be represented by the model API.")
    return name


def provider_tool(server_name: str, tool: Any) -> dict[str, Any]:
    schema = tool.input_schema
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError(f"MCP tool '{tool.name}' has an unsupported input schema.")
    description = tool.description or tool.title or f"Call {tool.name} on the {server_name} MCP server."
    return {
        "type": "function",
        "name": provider_tool_name(server_name, tool.name),
        "description": description[:4_000],
        "parameters": schema,
        "strict": False,
    }


def result_text(result: Any) -> str:
    parts = [
        block.text for block in (result.content or [])
        if isinstance(getattr(block, "text", None), str)
    ]
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False, default=str))
    if not parts:
        parts.append("MCP tool returned no readable text.")
    text = "\n".join(parts)
    if result.is_error:
        return f"MCP server reported a tool error:\n{text}"
    return text
