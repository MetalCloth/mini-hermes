"""Select recent, complete chat turns for a model request."""

import json
from typing import Any


MAX_HISTORY_CHARS = 80_000


def select_context(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep system instructions and the newest whole turns within the history budget."""
    pinned = [message for message in messages if message["role"] in {"system", "developer"}]
    turns: list[list[dict[str, Any]]] = []
    for message in messages:
        if message["role"] in {"system", "developer"}:
            continue
        if message["role"] == "user":
            turns.append([])
        if not turns:
            turns.append([])
        turns[-1].append(message)

    def size(items: list[dict[str, Any]]) -> int:
        return sum(len(json.dumps(item, ensure_ascii=False, separators=(",", ":"))) + 1 for item in items)

    selected_turns: list[list[dict[str, Any]]] = []
    used = size(pinned)
    if used > MAX_HISTORY_CHARS:
        raise ValueError(f"System instructions exceed the {MAX_HISTORY_CHARS:,}-character context budget.")
    for turn in reversed(turns):
        turn_size = size(turn)
        if used + turn_size > MAX_HISTORY_CHARS:
            if not selected_turns:
                raise ValueError(
                    f"System instructions and the current chat turn exceed the "
                    f"{MAX_HISTORY_CHARS:,}-character context budget. Shorten the message or reduce the tool output."
                )
            break
        selected_turns.append(turn)
        used += turn_size

    return pinned + [message for turn in reversed(selected_turns) for message in turn]
