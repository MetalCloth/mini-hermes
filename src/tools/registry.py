"""Small built-in tool catalog and dispatcher."""

import json
from pathlib import Path
from typing import Any, Callable

from src.tools.file_tools import read_file, write_file
from src.tools.terminal_tool import run_terminal
from src.tools.web_tools import web_search


def tool_schemas() -> list[dict[str, Any]]:
    schemas = [
        {
            "name": "terminal",
            "description": (
                "Run a shell command from the project folder when the user asks you to "
                "inspect or change this project. The user must approve every command. "
                "Bubblewrap limits writes to the project folder and hides the user's home; "
                "network access remains enabled. Returns stdout/stderr and exit code; "
                "commands time out after 30 seconds."
            ),
            "parameters": {
                "type": "object", "properties": {
                    "command": {"type": "string", "description": "Command to run, e.g. 'git status --short'."}
                }, "required": ["command"], "additionalProperties": False,
            },
        },
        {
            "name": "read_file",
            "description": (
                "Read a UTF-8 text file inside the project folder when the user asks "
                "about its contents. Give a project-relative path such as 'README.md'. "
                "Files outside the project folder are rejected. Returns file text."
            ),
            "parameters": {
                "type": "object", "properties": {
                    "path": {"type": "string", "description": "Project-relative file path."}
                }, "required": ["path"], "additionalProperties": False,
            },
        },
        {
            "name": "write_file",
            "description": (
                "Create or replace one UTF-8 text file inside the project folder. "
                "Use a project-relative path and provide the complete file content. "
                "The user approves every write; existing file content will be replaced. "
                "Files outside the project are rejected. Parent folders must already exist. "
                "Returns a confirmation or an actionable error."
            ),
            "parameters": {
                "type": "object", "properties": {
                    "path": {"type": "string", "description": "Project-relative destination, e.g. 'notes.txt'."},
                    "content": {"type": "string", "description": "Complete UTF-8 text to write; up to 100,000 characters."},
                }, "required": ["path", "content"], "additionalProperties": False,
            },
        },
        {
            "name": "web_search",
            "description": (
                "Search the public web when the user needs current or external information. "
                "Returns a short list of result titles, URLs, and snippets."
            ),
            "parameters": {
                "type": "object", "properties": {
                    "query": {"type": "string", "description": "Focused search query."},
                    "max_results": {"type": "integer", "description": "Number of results, from 1 to 5."},
                }, "required": ["query", "max_results"], "additionalProperties": False,
            },
        },
    ]
    return [
        {"type": "function", "name": schema["name"], "description": schema["description"],
         "parameters": schema["parameters"], "strict": True}
        for schema in schemas
    ]


def execute_tool(name: str, arguments: dict[str, Any], project_root: Path,
                 confirm_terminal: Callable[[str], bool],
                 confirm_write: Callable[[str, str, bool], bool]) -> str:
    if name == "terminal":
        result = run_terminal(arguments["command"], project_root, confirm_terminal)
    elif name == "read_file":
        result = read_file(arguments["path"], project_root)
    elif name == "write_file":
        result = write_file(arguments["path"], arguments["content"], project_root, confirm_write)
    elif name == "web_search":
        result = web_search(arguments["query"], arguments["max_results"])
    else:
        return f"Unknown tool '{name}'. Use one of: terminal, read_file, write_file, web_search."
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
