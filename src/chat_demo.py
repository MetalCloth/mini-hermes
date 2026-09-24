"""Persistent terminal chat with a Codex model."""

import argparse
import json
from pathlib import Path
from typing import Any

from src.agent.conversation_loop import run_turn
from src.agent.project_context import load_project_instructions
from src.agent.system_prompt import SYSTEM_PROMPT
from src.providers.codex import CodexProvider
from src.providers.types import ToolCall
from src.session.sqlite_store import DEFAULT_DB_PATH, SESSION_ID, SQLiteSessionStore
from src.tools.registry import tool_schemas
from src.tools.terminal_tool import validate_project_root


APP_ROOT = Path(__file__).resolve().parent.parent


def _resolve_project_root(path: Path) -> Path:
    try:
        root = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Could not open project folder {path}: {exc}") from exc
    if not root.is_dir():
        raise ValueError(f"Project path is not a folder: {root}")
    return validate_project_root(root)


def _safe_terminal_text(text: str) -> str:
    return "".join(
        char if char in "\n\t" or char.isprintable()
        else f"\\x{ord(char):02x}" if ord(char) <= 0xFF
        else f"\\u{ord(char):04x}"
        for char in text
    )


def _approval_preview(text: str) -> str:
    return "\n".join(f"| {line}" for line in _safe_terminal_text(text).split("\n")) or "| <empty>"


def _confirm_terminal(command: str, project_root: Path) -> bool:
    print(f"Mini-Hermes wants to run this command from {project_root}:")
    print(_approval_preview(command))
    try:
        return input("Allow this command? [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def _confirm_write(path: str, content: str, exists: bool) -> bool:
    action = "replace" if exists else "create"
    print(f"Mini-Hermes wants to {action} {path} ({len(content)} characters):")
    print("----- proposed file content (each line starts with |) -----")
    print(_approval_preview(content))
    print("----- end proposed content -----")
    try:
        return input(f"Allow this {action}? [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Chat with a Codex model.")
    parser.add_argument("--model", default="gpt-5.6-luna", help="Codex model slug")
    parser.add_argument("--project", type=Path, metavar="DIR", help="project folder (default: current folder)")
    session_options = parser.add_mutually_exclusive_group()
    session_options.add_argument("--new", action="store_true", help="start a new chat")
    session_options.add_argument("--list", action="store_true", help="list saved chats")
    session_options.add_argument("--search", metavar="QUERY", help="search saved user and assistant messages")
    session_options.add_argument("--resume", metavar="ID", help="resume a saved chat")
    args = parser.parse_args(argv)
    if (args.list or args.search is not None) and args.project is not None:
        parser.error("--project cannot be used with --list or --search")

    try:
        store = SQLiteSessionStore()
        if args.list:
            sessions = store.list_sessions()
            print("Saved sessions:" if sessions else "No saved sessions yet.")
            for session_id in sessions:
                print(f"{session_id}  {store.session_project_root(session_id) or APP_ROOT}")
            return
        if args.search is not None:
            matches = store.search_messages(args.search)
            if not matches:
                print("No saved messages matched.")
            else:
                for session_id, project, role, snippet in matches:
                    print(f"{session_id}  {role}  {_safe_terminal_text(project or str(APP_ROOT))}")
                    print(f"  {_safe_terminal_text(snippet)}")
                print("Use --resume ID to reopen a matching chat.")
            return
        if args.resume is not None:
            session_id = args.resume
            if not store.session_exists(session_id):
                parser.error(f"no saved session with ID {session_id}")
            saved_root = store.session_project_root(session_id)
            # Old chats always worked in the Mini-Hermes repo.
            project_root = _resolve_project_root(Path(saved_root) if saved_root else APP_ROOT)
            if args.project is not None and _resolve_project_root(args.project) != project_root:
                parser.error(f"session {session_id} belongs to {project_root}")
            if saved_root is None:
                store.bind_session_to_project(session_id, project_root)
        else:
            project_root = _resolve_project_root(args.project or Path("."))
            if args.new or args.project is not None or project_root != APP_ROOT:
                session_id = store.create_session(project_root)
            else:
                # Bare launches from this repo still continue the original main chat.
                session_id = SESSION_ID
                store.bind_session_to_project(session_id, project_root)
        saved_messages = store.load_messages(session_id)
    except ValueError as exc:
        parser.error(str(exc))
    except Exception as exc:
        print(f"Could not access sessions at {DEFAULT_DB_PATH}: {exc}")
        return
    provider = CodexProvider(args.model)
    try:
        project_instructions = load_project_instructions(project_root)
    except ValueError as exc:
        print(f"Could not load project instructions: {exc}")
        return
    history: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if project_root != APP_ROOT:
        history.append({
            "role": "developer",
            "content": f"Active project folder: {str(project_root)!r}. Tool paths are relative to it.",
        })
    if project_instructions:
        history.append({
            "role": "developer",
            "content": (
                "Project guidance from the root AGENTS.md follows. Apply it to work in "
                "this project unless it conflicts with the system prompt or user's request.\n\n"
                f"{project_instructions}"
            ),
        })
    history.extend(saved_messages)
    saved_count = len(history)
    tools = tool_schemas()
    print(f"Session ID: {session_id}")
    print(f"Project: {project_root}")
    if saved_messages:
        print(f"Resumed chat with {len(saved_messages)} saved messages.")
    else:
        print("Starting a new chat.")
    if project_instructions:
        print("Loaded project instructions from AGENTS.md.")
    print("Type /quit to exit.")
    confirm_terminal = lambda command: _confirm_terminal(command, project_root)
    text_open = False
    text_ends_newline = False

    def finish_text() -> None:
        nonlocal text_open
        if text_open:
            if not text_ends_newline:
                print(flush=True)
            text_open = False

    def show_text(delta: str) -> None:
        nonlocal text_open, text_ends_newline
        if not text_open:
            print("assistant> ", end="", flush=True)
            text_open = True
        print(_safe_terminal_text(delta), end="", flush=True)
        text_ends_newline = delta.endswith("\n")

    def show_tool(phase: str, call: ToolCall, result: str | None) -> None:
        finish_text()
        if phase == "start":
            detail = next((call.arguments[key] for key in ("path", "ref", "url", "query", "key", "direction", "mode")
                           if key in call.arguments), None)
            label = (
                f" {json.dumps(str(detail)[:120], ensure_ascii=False)}"
                if detail is not None else ""
            )
            print(f"tool> {call.name}{label}", flush=True)
        elif result is not None:
            preview = result[:600] + ("\n…" if len(result) > 600 else "")
            print(_approval_preview(preview), flush=True)

    while True:
        try:
            prompt = input("you> ")
        except EOFError:
            break
        if prompt.strip().lower() in {"/quit", "/exit"}:
            break
        if not prompt.strip():
            continue

        turn_start = len(history)
        history.append({"role": "user", "content": prompt})
        text_open = False
        persist_turn = True
        try:
            answer = run_turn(
                history, provider.complete, tools, project_root,
                confirm_terminal, _confirm_write,
                on_text_delta=show_text, on_tool_event=show_tool,
            )
        except KeyboardInterrupt:
            finish_text()
            del history[turn_start:]
            persist_turn = False
            print("\nTurn interrupted; it was not saved. Inspect possible tool effects before retrying.")
            break
        except Exception as exc:
            finish_text()
            print(f"Agent turn failed: {exc}")
        else:
            finish_text()
            history.append({"role": "assistant", "content": answer})
        finally:
            if persist_turn:
                try:
                    store.append_messages(history[saved_count:], session_id)
                    saved_count = len(history)
                except Exception as exc:
                    print(f"Could not save this turn to {store.db_path}: {exc}")


if __name__ == "__main__":
    main()
