"""Provider-neutral response shapes used by the agent loop."""

from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from math import isfinite
import time
from typing import Any


def retry_after_seconds(value: str | None) -> float:
    """Parse an HTTP Retry-After delay/date, bounded to 30 seconds."""
    if not isinstance(value, str):
        return 0.0
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    return max(0.0, min(30.0, seconds)) if isfinite(seconds) else 0.0


class ProviderRequestError(RuntimeError):
    """A request failure with explicit, conservative retry information."""

    def __init__(self, message: str, *, retryable: bool = False, retry_after: float = 0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider_data: dict[str, Any] = field(default_factory=dict)
