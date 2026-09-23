"""SQLite-backed transcript for Mini-Hermes's single resumable chat."""

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path.home() / ".mini-hermes" / "sessions.sqlite3"
SESSION_ID = "main"


class SQLiteSessionStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.db_path == DEFAULT_DB_PATH:
            os.chmod(self.db_path.parent, 0o700)
        connection = sqlite3.connect(self.db_path)
        try:
            with connection:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        message_json TEXT NOT NULL
                    )"""
                )
        finally:
            connection.close()
        os.chmod(self.db_path, 0o600)

    def load_messages(self) -> list[dict[str, Any]]:
        connection = sqlite3.connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT message_json FROM messages WHERE session_id = ? ORDER BY id",
                (SESSION_ID,),
            ).fetchall()
        finally:
            connection.close()
        try:
            messages = [json.loads(row[0]) for row in rows]
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Saved chat history is invalid JSON in {self.db_path}.") from exc
        if not all(isinstance(message, dict) for message in messages):
            raise RuntimeError(f"Saved chat history contains an invalid message in {self.db_path}.")
        return messages

    def append_messages(self, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        rows = [
            (SESSION_ID, json.dumps(message, ensure_ascii=False, separators=(",", ":")))
            for message in messages
        ]
        connection = sqlite3.connect(self.db_path)
        try:
            with connection:
                connection.executemany(
                    "INSERT INTO messages (session_id, message_json) VALUES (?, ?)", rows
                )
        finally:
            connection.close()
