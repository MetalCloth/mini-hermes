"""Small built-in tool catalog and dispatcher."""

import json
from pathlib import Path
from typing import Any, Callable

from src.tools.browser_tools import BrowserSession
from src.tools.file_tools import (
    FileChange,
    edit_file,
    read_file,
    search_files,
    undo_file_change,
    write_file,
)
from src.tools.terminal_tool import run_terminal
from src.tools.web_tools import web_extract, web_search


def tool_schemas(mcp_tools: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
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
                "Files outside the project and private config paths (such as .env and key files) are rejected. "
                "Parent folders must already exist. "
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
            "name": "edit_file",
            "description": (
                "Make one focused replacement in an existing UTF-8 project file. "
                "Read the file first, then provide exact old_text that occurs once and new_text. "
                "Oryn shows the unified diff and must get user approval before applying it. "
                "Paths stay inside the project and private config paths are rejected; use write_file to create a file or replace its full contents."
            ),
            "parameters": {
                "type": "object", "properties": {
                    "path": {"type": "string", "description": "Project-relative path to an existing file."},
                    "old_text": {"type": "string", "description": "Exact, unique text from the current file; include context if needed."},
                    "new_text": {"type": "string", "description": "Replacement text; an empty string deletes the matched text."},
                }, "required": ["path", "old_text", "new_text"], "additionalProperties": False,
            },
        },
        {
            "name": "undo_file_change",
            "description": (
                "Undo the most recent approved write_file or edit_file change made by Oryn in this chat session. "
                "Restores previous bytes and permissions, or deletes a file Oryn created. "
                "The user must approve the undo. It refuses if the file changed afterward. "
                "The 20 most recent changes are kept in memory until this Oryn process exits. "
                "Changes to existing files over 1 MB are refused so Oryn can keep a safe snapshot."
            ),
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        },
        {
            "name": "web_search",
            "description": (
                "Search the public web with Tavily when the user needs current or external information. "
                "Returns up to five source titles, URLs, and short snippets. Requires a Tavily API key."
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
                "be read, try another result. Use browser_open for pages needing clicks or forms."
            ),
            "parameters": {
                "type": "object", "properties": {
                    "url": {"type": "string", "description": "Full public page URL, e.g. 'https://example.com/article'."}
                }, "required": ["url"], "additionalProperties": False,
            },
        },
        {
            "name": "browser_open",
            "description": (
                "Open a public web page in a short-lived Firecrawl cloud browser when you need "
                "to click, fill a field, or inspect changing page content. Returns the current "
                "URL and an accessibility snapshot with element refs such as '@e2'. "
                "The browser stays open for this agent turn only and closes after the final answer. "
                "Browser sessions use Firecrawl credits, so use web_extract for a simple one-page read. "
                "Requires FIRECRAWL_API_KEY."
            ),
            "parameters": {
                "type": "object", "properties": {
                    "url": {"type": "string", "description": "Full public http:// or https:// URL to open."}
                }, "required": ["url"], "additionalProperties": False,
            },
        },
        {
            "name": "browser_snapshot",
            "description": (
                "Refresh the current Firecrawl browser page's URL and accessibility snapshot. "
                "Use after browser_open if the page changes without a click or needs another look. "
                "Returns page text and current element refs."
            ),
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        },
        {
            "name": "browser_click",
            "description": (
                "Click an element in the current Firecrawl browser using a ref from the latest "
                "snapshot, such as '@e2'. Returns the updated URL and page snapshot. "
                "Open a page first; only click controls needed for the user's request."
            ),
            "parameters": {
                "type": "object", "properties": {
                    "ref": {"type": "string", "description": "Element ref from the latest snapshot, e.g. '@e2'."}
                }, "required": ["ref"], "additionalProperties": False,
            },
        },
        {
            "name": "browser_fill",
            "description": (
                "Clear and type text into an input in the current Firecrawl browser using a "
                "ref from the latest snapshot. Returns the updated page snapshot. "
                "This does not submit the form; use browser_click on its submit control if needed. "
                "The page runs in Firecrawl's cloud browser; do not enter passwords or secrets."
            ),
            "parameters": {
                "type": "object", "properties": {
                    "ref": {"type": "string", "description": "Input ref from the latest snapshot, e.g. '@e1'."},
                    "text": {"type": "string", "description": "Text to enter, up to 1,000 characters."},
                }, "required": ["ref", "text"], "additionalProperties": False,
            },
        },
        {
            "name": "browser_press",
            "description": (
                "Press a key on the current Firecrawl page after browser_open. Use Enter to submit "
                "a filled form, or Tab/Escape/arrow keys to operate a control. This can trigger "
                "page actions. Returns the updated URL and text snapshot."
            ),
            "parameters": {
                "type": "object", "properties": {
                    "key": {"type": "string", "description": "Key such as Enter, Tab, Escape, ArrowDown, or PageDown."}
                }, "required": ["key"], "additionalProperties": False,
            },
        },
        {
            "name": "browser_scroll",
            "description": (
                "Scroll the current Firecrawl page after browser_open to reveal content below, "
                "above, or to a side. Returns the updated URL and text snapshot."
            ),
            "parameters": {
                "type": "object", "properties": {
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                    "pixels": {"type": "integer", "description": "Distance from 1 to 2,000 pixels, e.g. 500."},
                }, "required": ["direction", "pixels"], "additionalProperties": False,
            },
        },
        {
            "name": "browser_wait",
            "description": (
                "Wait up to 10 seconds for the current Firecrawl page to show a known element, "
                "text, URL pattern, or load state after an action. Returns the updated URL and "
                "text snapshot. Prefer text or a known element over networkidle on busy pages."
            ),
            "parameters": {
                "type": "object", "properties": {
                    "mode": {"type": "string", "enum": ["ref", "text", "url", "load"]},
                    "value": {"type": "string", "description": (
                        "For ref: @e2; text: visible words; url: **/results; "
                        "load: load, domcontentloaded, or networkidle."
                    )},
                }, "required": ["mode", "value"], "additionalProperties": False,
            },
        },
        {
            "name": "browser_back",
            "description": (
                "Go back one page in the current Firecrawl browser history after browser_open. "
                "Returns the previous URL and text snapshot."
            ),
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        },
    ]
    tools = [
        {"type": "function", "name": schema["name"], "description": schema["description"],
         "parameters": schema["parameters"], "strict": True}
        for schema in schemas
    ]
    return [*tools, *(mcp_tools or [])]


def execute_tool(name: str, arguments: dict[str, Any], project_root: Path,
                 confirm_terminal: Callable[[str], bool],
                 confirm_write: Callable[[str, str, bool], bool],
                 browser: BrowserSession | None = None,
                 confirm_edit: Callable[[str, str], bool] | None = None,
                 confirm_undo: Callable[[str, str, bool], bool] | None = None,
                 undo_history: list[FileChange] | None = None) -> str:
    if name.startswith("browser_") and browser is None:
        raise RuntimeError("Browser actions need an active agent turn. Open a page in the chat first.")
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
        result = write_file(
            arguments["path"], arguments["content"], project_root, confirm_write, undo_history,
        )
    elif name == "edit_file":
        if confirm_edit is None:
            raise RuntimeError("edit_file needs a user-approval handler; no changes were made.")
        result = edit_file(
            arguments["path"], arguments["old_text"], arguments["new_text"],
            project_root, confirm_edit, undo_history,
        )
    elif name == "undo_file_change":
        if confirm_undo is None or undo_history is None:
            raise RuntimeError("Undo is unavailable in this chat session; no changes were made.")
        result = undo_file_change(project_root, undo_history, confirm_undo)
    elif name == "web_search":
        result = web_search(arguments["query"], arguments["max_results"])
    elif name == "web_extract":
        result = web_extract(arguments["url"])
    elif name == "browser_open":
        result = browser.open(arguments["url"])
    elif name == "browser_snapshot":
        result = browser.snapshot()
    elif name == "browser_click":
        result = browser.click(arguments["ref"])
    elif name == "browser_fill":
        result = browser.fill(arguments["ref"], arguments["text"])
    elif name == "browser_press":
        result = browser.press(arguments["key"])
    elif name == "browser_scroll":
        result = browser.scroll(arguments["direction"], arguments["pixels"])
    elif name == "browser_wait":
        result = browser.wait(arguments["mode"], arguments["value"])
    elif name == "browser_back":
        result = browser.back()
    else:
        return f"Unknown tool '{name}'. Use one of: {', '.join(tool['name'] for tool in tool_schemas())}."
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
