"""Tiny terminal chat with in-memory history."""

import argparse
from pathlib import Path
from typing import Any

from src.agent.conversation_loop import run_turn
from src.providers.codex import CodexProvider
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
    args = parser.parse_args(argv)

    provider = CodexProvider(args.model)
    history: list[dict[str, Any]] = []
    tools = tool_schemas()
    print("Chat started. Type /quit to exit.")

    while True:
        try:
            prompt = input("you> ")
        except EOFError:
            break
        if prompt.strip().lower() in {"/quit", "/exit"}:
            break
        if not prompt.strip():
            continue

        history.append({"role": "user", "content": prompt})
        try:
            answer = run_turn(
                history, provider.complete, tools, PROJECT_ROOT,
                _confirm_terminal, _confirm_write,
            )
        except Exception as exc:
            print(f"Agent turn failed: {exc}")
            continue
        history.append({"role": "assistant", "content": answer})
        print(f"assistant> {answer}")


if __name__ == "__main__":
    main()
