"""Private, flushed JSONL traces for computer-mode debugging."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading


class ComputerTrace:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(descriptor, 0o600)
        self._file = os.fdopen(descriptor, "a", encoding="utf-8")
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> "ComputerTrace | None":
        path = os.environ.get("ORYN_COMPUTER_LOG_FILE")
        return cls(path) if path else None

    def write(self, event: str, **fields) -> None:
        record = {
            "time_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            **fields,
        }
        with self._lock:
            self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.close()
