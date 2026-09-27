"""Select recent, complete chat turns for a model request."""

import json
from typing import Any

from src.images import MAX_CONTEXT_IMAGE_BYTES, image_context_bytes


MAX_HISTORY_CHARS = 80_000


def _mark_incomplete_replies(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    notes = {
        "cancelled": "The previous Oryn reply was stopped before finishing.",
        "failed": "The previous Oryn reply failed before finishing.",
    }
    prepared = []
    for original in messages:
        message = dict(original)
        message.pop("elapsed_seconds", None)
        status = message.pop("turn_status", None)
        if message.get("role") == "assistant" and status in notes:
            note = (
                f"{notes[status]} The assistant text below is partial, not a complete answer. "
                "If the user asks to continue, continue from it without assuming the missing parts."
            )
            message["content"] = f"[{note}]\n\n{message.get('content', '')}".rstrip()
        prepared.append(message)
    return prepared


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
        total = 0
        for item in items:
            sized = item
            images = item.get("images")
            if isinstance(images, list):
                sized = {**item, "images": [
                    {key: value for key, value in image.items() if key != "base64_data"}
                    if isinstance(image, dict) else image
                    for image in images
                ]}
            total += len(json.dumps(sized, ensure_ascii=False, separators=(",", ":"))) + 1
        return total

    selected_turns: list[list[dict[str, Any]]] = []
    used = size(pinned)
    used_image_bytes = 0
    if used > MAX_HISTORY_CHARS:
        raise ValueError(f"System instructions exceed the {MAX_HISTORY_CHARS:,}-character context budget.")
    for turn in reversed(turns):
        turn_size = size(turn)
        turn_images = image_context_bytes(turn)
        if (used + turn_size > MAX_HISTORY_CHARS
                or used_image_bytes + turn_images > MAX_CONTEXT_IMAGE_BYTES):
            if not selected_turns:
                raise ValueError(
                    f"System instructions and the current chat turn exceed the "
                    f"{MAX_HISTORY_CHARS:,}-character or {MAX_CONTEXT_IMAGE_BYTES // (1024 * 1024)} MiB image context budget. "
                    "Shorten the message or remove images."
                )
            break
        selected_turns.append(turn)
        used += turn_size
        used_image_bytes += turn_images

    selected = pinned + [message for turn in reversed(selected_turns) for message in turn]
    return _mark_incomplete_replies(selected)
