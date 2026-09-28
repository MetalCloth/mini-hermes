"""Convert MCP tool descriptions and results to Oryn's text tool format."""

import json
import re
import base64
from dataclasses import dataclass
from typing import Any

from src.images import prepare_image


_MODEL_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def mcp_loader_tool(directory: list[dict[str, Any]]) -> dict[str, Any]:
    """Advertise a compact service directory instead of every MCP function schema."""
    return {
        "type": "function",
        "name": "load_mcp_tools",
        "description": (
            "Load a connected MCP server's full tool definitions when its capabilities "
            "are needed for the user's task. This only loads definitions; it does not run "
            "an external action. Returns the loaded tool names; their full argument schemas "
            "will be available in the next model request and for the rest of this user turn. "
            "Load before calling mcp__ tools, even if their names appear in chat history. "
            "Use exact names and arguments from the loaded definitions. Disabled or "
            "unavailable servers must first be configured by the user in /mcps. "
            "MCP directory: " + json.dumps(directory, ensure_ascii=False, separators=(",", ":"))
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "enum": [entry["server"] for entry in directory],
                           "description": "Exact MCP server name from the directory, e.g. github."},
            },
            "required": ["server"],
            "additionalProperties": False,
        },
        "strict": True,
    }


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


@dataclass(frozen=True)
class MCPImageResult:
    text: str
    images: list[dict[str, Any]]


def result_with_images(result: Any) -> str | MCPImageResult:
    """Keep desktop screenshots for one model request, without saving them in chat history."""
    text = result_text(result)
    if result.is_error:
        return text
    images = []
    for block in result.content or []:
        if getattr(block, "type", None) != "image":
            continue
        raw = base64.b64decode(block.data, validate=True)
        images.append(prepare_image(raw, "Computer screenshot"))
        if len(images) >= 1:
            break
    return MCPImageResult(text, images) if images else text
