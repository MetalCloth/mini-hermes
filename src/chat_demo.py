"""Persistent terminal chat with a Codex model."""

import argparse
from pathlib import Path
from typing import Any

from src.agent.conversation_loop import run_turn
from src.agent.project_context import load_project_instructions
from src.agent.system_prompt import SYSTEM_PROMPT
from src.providers.codex import CodexProvider
from src.session.sqlite_store import DEFAULT_DB_PATH, SESSION_ID, SQLiteSessionStore
from src.tools.registry import tool_schemas


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _approval_preview(text: str) -> str:
    safe = "".join(
        char if char in "\n\t" or char.isprintable()
        else f"\\x{ord(char):02x}" if ord(char) <= 0xFF
        else f"\\u{ord(char):04x}"
        for char in text
    )
    return "\n".join(f"| {line}" for line in safe.split("\n")) or "| <empty>"


def _confirm_terminal(command: str) -> bool:
    print(f"Mini-Hermes wants to run this command from {PROJECT_ROOT}:")
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
    session_options = parser.add_mutually_exclusive_group()
    session_options.add_argument("--new", action="store_true", help="start a new chat")
    session_options.add_argument("--list", action="store_true", help="list saved chats")
    session_options.add_argument("--resume", metavar="ID", help="resume a saved chat")
    args = parser.parse_args(argv)

    try:
        store = SQLiteSessionStore()
        if args.list:
            sessions = store.list_sessions()
            print("Saved sessions:" if sessions else "No saved sessions yet.")
            for session_id in sessions:
                print(session_id)
            return
        if args.new:
            session_id = store.create_session()
        elif args.resume is not None:
            session_id = args.resume
            if not store.session_exists(session_id):
                parser.error(f"no saved session with ID {session_id}")
        else:
            session_id = SESSION_ID
        saved_messages = store.load_messages(session_id)
    except Exception as exc:
        print(f"Could not access sessions at {DEFAULT_DB_PATH}: {exc}")
        return
    provider = CodexProvider(args.model)
    try:
        project_instructions = load_project_instructions(PROJECT_ROOT)
    except ValueError as exc:
        print(f"Could not load project instructions: {exc}")
        return
    history: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
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
    if saved_messages:
        print(f"Resumed chat with {len(saved_messages)} saved messages.")
    else:
        print("Starting a new chat.")
    if project_instructions:
        print("Loaded project instructions from AGENTS.md.")
    print("Type /quit to exit.")

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
        persist_turn = True
        try:
            answer = run_turn(
                history, provider.complete, tools, PROJECT_ROOT,
                _confirm_terminal, _confirm_write,
            )
        except KeyboardInterrupt:
            del history[turn_start:]
            persist_turn = False
            print("\nTurn interrupted; it was not saved. Inspect possible tool effects before retrying.")
            break
        except Exception as exc:
            print(f"Agent turn failed: {exc}")
        else:
            history.append({"role": "assistant", "content": answer})
            print(f"assistant> {answer}")
        finally:
            if persist_turn:
                try:
                    store.append_messages(history[saved_count:], session_id)
                    saved_count = len(history)
                except Exception as exc:
                    print(f"Could not save this turn to {store.db_path}: {exc}")


if __name__ == "__main__":
    main()
