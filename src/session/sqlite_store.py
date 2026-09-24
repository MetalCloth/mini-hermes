"""SQLite-backed chat sessions and transcripts."""

import json
import os
import sqlite3
import uuid
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
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY)"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO sessions (id) "
                    "SELECT DISTINCT session_id FROM messages"
                )
        finally:
            connection.close()
        os.chmod(self.db_path, 0o600)

    def create_session(self) -> str:
        session_id = uuid.uuid4().hex
        connection = sqlite3.connect(self.db_path)
        try:
            with connection:
                connection.execute("INSERT INTO sessions (id) VALUES (?)", (session_id,))
        finally:
            connection.close()
        return session_id

    def list_sessions(self) -> list[str]:
        connection = sqlite3.connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT id FROM sessions ORDER BY rowid DESC"
            ).fetchall()
        finally:
            connection.close()
        return [row[0] for row in rows]

    def session_exists(self, session_id: str) -> bool:
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone() is not None
        finally:
            connection.close()

    def load_messages(self, session_id: str = SESSION_ID) -> list[dict[str, Any]]:
        connection = sqlite3.connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT message_json FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
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

    def append_messages(
        self, messages: list[dict[str, Any]], session_id: str = SESSION_ID
    ) -> None:
        if not messages:
            return
        rows = [
            (session_id, json.dumps(message, ensure_ascii=False, separators=(",", ":")))
            for message in messages
        ]
        connection = sqlite3.connect(self.db_path)
        try:
            with connection:
                connection.execute(
                    "INSERT OR IGNORE INTO sessions (id) VALUES (?)", (session_id,)
                )
                connection.executemany(
                    "INSERT INTO messages (session_id, message_json) VALUES (?, ?)", rows
                )
        finally:
            connection.close()
