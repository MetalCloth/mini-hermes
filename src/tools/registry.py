"""Small built-in tool catalog and dispatcher."""

import json
from pathlib import Path
from typing import Any, Callable

from src.tools.file_tools import read_file, search_files, write_file
from src.tools.terminal_tool import run_terminal
from src.tools.web_tools import web_extract, web_search


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
            "name": "search_files",
            "description": (
                "Search text inside the active project folder. Pattern uses ripgrep regex syntax "
                "unless literal is true. Restrict to a project-relative file or folder with path, "
                "and filter filenames with include/exclude globs. Empty include/exclude means no "
                "filter. Hidden and ignored files are normally skipped; private config, "
                "generated folders, binary files, and files over 2 MiB are skipped. Returns "
                "project-relative path:line:snippet matches; use read_file for more context. "
                "Search stops after 10 seconds, 12,000 output characters, or max_results."
            ),
            "parameters": {
                "type": "object", "properties": {
                    "pattern": {"type": "string", "description": "Text or regex to find, e.g. 'append_messages'."},
                    "path": {"type": "string", "description": "Project-relative file or folder; '.' searches the whole project."},
                    "include": {"type": "string", "description": "Filename glob such as '*.py'; use '' for all files."},
                    "exclude": {"type": "string", "description": "Filename glob such as '*_test.py'; use '' for none."},
                    "literal": {"type": "boolean", "description": "True for exact text; false for regex."},
                    "case_sensitive": {"type": "boolean", "description": "True to match letter case."},
                    "max_results": {"type": "integer", "description": "Maximum matching lines, from 1 to 50; use 30 normally."},
                },
                "required": ["pattern", "path", "include", "exclude", "literal", "case_sensitive", "max_results"],
                "additionalProperties": False,
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
        {
            "name": "web_extract",
            "description": (
                "Read one public web page when search snippets are not enough. "
                "Provide its full http:// or https:// URL. Returns the source URL, "
                "title, and up to 12,000 characters of page text. Uses Firecrawl "
                "when configured, otherwise reads HTML directly. If the page cannot "
                "be read, try another result."
            ),
            "parameters": {
                "type": "object", "properties": {
                    "url": {"type": "string", "description": "Full public page URL, e.g. 'https://example.com/article'."}
                }, "required": ["url"], "additionalProperties": False,
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
    elif name == "search_files":
        result = search_files(
            arguments["pattern"], project_root,
            path=arguments["path"], include=arguments["include"], exclude=arguments["exclude"],
            literal=arguments["literal"], case_sensitive=arguments["case_sensitive"],
            max_results=arguments["max_results"],
        )
    elif name == "write_file":
        result = write_file(arguments["path"], arguments["content"], project_root, confirm_write)
    elif name == "web_search":
        result = web_search(arguments["query"], arguments["max_results"])
    elif name == "web_extract":
        result = web_extract(arguments["url"])
    else:
        return f"Unknown tool '{name}'. Use one of: terminal, read_file, search_files, write_file, web_search, web_extract."
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
