"""Run one plain-text assistant turn over history supplied by the chat caller."""

from collections.abc import Callable


Message = dict[str, str]


def run_turn(messages: list[Message], complete: Callable[[list[Message]], str]) -> str:
    """Pass chat-owned history to a provider call and return its answer."""
    return complete(messages)
