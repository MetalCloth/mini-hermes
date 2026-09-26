"""SQLite-backed chat sessions and transcripts."""

import json
import os
import re
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
                    "CREATE TABLE IF NOT EXISTS sessions "
                    "(id TEXT PRIMARY KEY, project_root TEXT, title TEXT)"
                )
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(sessions)")
                }
                if "project_root" not in columns:
                    # Older session databases recorded chat IDs but no project folder.
                    connection.execute("ALTER TABLE sessions ADD COLUMN project_root TEXT")
                if "title" not in columns:
                    connection.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
                if "model" not in columns:
                    connection.execute("ALTER TABLE sessions ADD COLUMN model TEXT")
                if "updated_at" not in columns:
                    connection.execute("ALTER TABLE sessions ADD COLUMN updated_at TEXT")
                if "pinned" not in columns:
                    connection.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
                connection.execute(
                    "INSERT OR IGNORE INTO sessions (id) "
                    "SELECT DISTINCT session_id FROM messages"
                )
        finally:
            connection.close()
        os.chmod(self.db_path, 0o600)

    def create_session(self, project_root: Path | None = None) -> str:
        session_id = uuid.uuid4().hex
        connection = sqlite3.connect(self.db_path)
        try:
            with connection:
                connection.execute(
                    "INSERT INTO sessions (id, project_root, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (session_id, str(project_root) if project_root is not None else None),
                )
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

    def session_entries(self) -> list[dict[str, Any]]:
        """Session picker metadata, newest activity first; legacy dates stay unknown."""
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(
                "SELECT id, project_root, title, model, updated_at, pinned FROM sessions "
                "ORDER BY pinned DESC, updated_at DESC, rowid DESC"
            )]
        finally:
            connection.close()

    def toggle_session_pin(self, session_id: str) -> None:
        connection = sqlite3.connect(self.db_path)
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE sessions SET pinned = 1 - pinned WHERE id = ?", (session_id,),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"No saved session with ID {session_id}.")
        finally:
            connection.close()

    def search_messages(self, query: str, limit: int = 20) -> list[tuple[str, str | None, str, str]]:
        """Find recent user/assistant text across saved sessions."""
        query = query.strip()
        if not query or len(query) > 200:
            raise ValueError("Search query must be 1 to 200 characters.")
        if not 1 <= limit <= 100:
            raise ValueError("Search limit must be between 1 and 100.")
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        matches = []
        connection = sqlite3.connect(self.db_path)
        try:
            # ponytail: scan local transcripts; add SQLite FTS only if this becomes slow.
            rows = connection.execute(
                "SELECT m.session_id, s.project_root, m.message_json "
                "FROM messages AS m LEFT JOIN sessions AS s ON s.id = m.session_id "
                "ORDER BY m.id DESC"
            )
            for session_id, project_root, raw in rows:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"Saved chat history is invalid JSON in {self.db_path}.") from exc
                if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                    continue
                content = message.get("content")
                if not isinstance(content, str) or not (found := pattern.search(content)):
                    continue
                start, end = max(0, found.start() - 60), min(len(content), found.end() + 100)
                snippet = " ".join(content[start:end].split())
                snippet = ("…" if start else "") + snippet + ("…" if end < len(content) else "")
                matches.append((session_id, project_root, message["role"], snippet))
                if len(matches) == limit:
                    break
        finally:
            connection.close()
        return matches

    def session_exists(self, session_id: str) -> bool:
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone() is not None
        finally:
            connection.close()

    def session_project_root(self, session_id: str) -> str | None:
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT project_root FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        finally:
            connection.close()
        return row[0] if row else None

    def session_title(self, session_id: str) -> str | None:
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT title FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        finally:
            connection.close()
        return row[0] if row else None

    def session_model(self, session_id: str) -> str | None:
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT model FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        finally:
            connection.close()
        return row[0] if row else None

    def set_session_model(self, session_id: str, model: str) -> None:
        model = model.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", model):
            raise ValueError("Model ID must be 1 to 120 letters, digits, dots, underscores, or hyphens.")
        connection = sqlite3.connect(self.db_path)
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE sessions SET model = ? WHERE id = ?", (model, session_id)
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"No saved session with ID {session_id}.")
        finally:
            connection.close()

    def rename_session(self, session_id: str, title: str) -> bool:
        title = " ".join(title.split())
        if not title or len(title) > 80:
            raise ValueError("Chat title must be 1 to 80 characters.")
        connection = sqlite3.connect(self.db_path)
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE sessions SET title = ? WHERE id = ?", (title, session_id)
                )
                return cursor.rowcount == 1
        finally:
            connection.close()

    def delete_session(self, session_id: str) -> bool:
        connection = sqlite3.connect(self.db_path)
        try:
            with connection:
                connection.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                cursor = connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                return cursor.rowcount == 1
        finally:
            connection.close()

    def bind_session_to_project(self, session_id: str, project_root: Path) -> None:
        root = str(project_root)
        connection = sqlite3.connect(self.db_path)
        try:
            with connection:
                # Fill legacy roots once; never move an existing chat to another project.
                connection.execute(
                    "INSERT OR IGNORE INTO sessions (id, project_root) VALUES (?, ?)",
                    (session_id, root),
                )
                connection.execute(
                    "UPDATE sessions SET project_root = ? "
                    "WHERE id = ? AND project_root IS NULL",
                    (root, session_id),
                )
                saved_root = connection.execute(
                    "SELECT project_root FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()[0]
                if saved_root != root:
                    raise ValueError(f"Session {session_id} belongs to {saved_root}, not {root}.")
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
                connection.execute(
                    "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,),
                )
        finally:
            connection.close()
