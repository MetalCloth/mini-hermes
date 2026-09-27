"""SQLite-backed chat sessions and transcripts."""

import json
import hashlib
import os
import re
import sqlite3
import stat
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
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS context_summaries "
                    "(session_id TEXT PRIMARY KEY, summary TEXT NOT NULL, "
                    "covered_messages INTEGER NOT NULL, covered_digest TEXT NOT NULL, "
                    "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS file_changes ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
                    "project_root TEXT NOT NULL, path TEXT NOT NULL, "
                    "previous_content BLOB, previous_mode INTEGER, result_digest BLOB NOT NULL, "
                    "result_mode INTEGER NOT NULL, state TEXT NOT NULL, "
                    "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS file_changes_session "
                    "ON file_changes(session_id, id DESC)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS diagnostic_events ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
                    "turn_id TEXT NOT NULL, event_json TEXT NOT NULL, "
                    "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS diagnostic_events_session "
                    "ON diagnostic_events(session_id, id DESC)"
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
                if "model_settings" not in columns:
                    connection.execute("ALTER TABLE sessions ADD COLUMN model_settings TEXT NOT NULL DEFAULT '{}'")
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

    def session_model_settings(self, session_id: str, model: str) -> dict[str, str]:
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT model_settings FROM sessions WHERE id = ?", (session_id,),
            ).fetchone()
        finally:
            connection.close()
        try:
            saved = json.loads(row[0]) if row else {}
            settings = saved.get(model, {}) if isinstance(saved, dict) else {}
            return {key: value for key, value in settings.items()
                    if key in {"reasoning_effort", "service_tier"} and isinstance(value, str)} if isinstance(settings, dict) else {}
        except (ValueError, TypeError):
            return {}

    def set_session_model_settings(self, session_id: str, model: str, settings: dict[str, str]) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", model):
            raise ValueError("Invalid model ID.")
        if (set(settings) != {"reasoning_effort", "service_tier"}
                or any(not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value)
                       for value in settings.values())):
            raise ValueError("Invalid model settings.")
        connection = sqlite3.connect(self.db_path)
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT model_settings FROM sessions WHERE id = ?", (session_id,),
                ).fetchone()
                if not row:
                    raise ValueError(f"No saved session with ID {session_id}.")
                saved = json.loads(row[0])
                if not isinstance(saved, dict):
                    raise ValueError("Saved model settings are invalid.")
                saved[model] = settings
                connection.execute(
                    "UPDATE sessions SET model_settings = ? WHERE id = ?", (json.dumps(saved), session_id),
                )
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
                connection.execute("DELETE FROM context_summaries WHERE session_id = ?", (session_id,))
                connection.execute("DELETE FROM file_changes WHERE session_id = ?", (session_id,))
                connection.execute("DELETE FROM diagnostic_events WHERE session_id = ?", (session_id,))
                cursor = connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                return cursor.rowcount == 1
        finally:
            connection.close()

    def load_context_summary(self, session_id: str) -> tuple[str, int, str] | None:
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT summary, covered_messages, covered_digest FROM context_summaries WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return None
        summary, covered_messages, digest = row
        if (not isinstance(summary, str) or not isinstance(covered_messages, int)
                or covered_messages < 1 or not re.fullmatch(r"[0-9a-f]{64}", digest or "")):
            return None
        return summary, covered_messages, digest

    def save_context_summary(
        self, session_id: str, summary: str, covered_messages: int, covered_digest: str,
    ) -> None:
        if (not isinstance(summary, str) or not summary or len(summary) > 50_000
                or type(covered_messages) is not int or covered_messages < 1
                or not re.fullmatch(r"[0-9a-f]{64}", covered_digest or "")):
            raise ValueError("Invalid context summary checkpoint.")
        connection = sqlite3.connect(self.db_path)
        try:
            with connection:
                connection.execute(
                    "INSERT INTO sessions (id) VALUES (?) ON CONFLICT(id) DO NOTHING", (session_id,),
                )
                connection.execute(
                    "INSERT INTO context_summaries (session_id, summary, covered_messages, covered_digest, updated_at) "
                    "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(session_id) DO UPDATE SET "
                    "summary = excluded.summary, covered_messages = excluded.covered_messages, "
                    "covered_digest = excluded.covered_digest, updated_at = CURRENT_TIMESTAMP",
                    (session_id, summary, covered_messages, covered_digest),
                )
        finally:
            connection.close()

    def record_file_change(self, session_id: str, project_root: Path, phase: str, change) -> int | None:
        """Durably journal approved file writes and undo transitions."""
        root = str(project_root.resolve())
        if phase not in {"prepared", "applied", "undoing", "undone"}:
            raise ValueError("Invalid file-change journal transition.")
        connection = sqlite3.connect(self.db_path)
        try:
            with connection:
                if phase == "prepared":
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT project_root FROM sessions WHERE id = ?", (session_id,),
                    ).fetchone()
                    if not row or row[0] != root:
                        raise ValueError("File-change journal does not match this chat's project folder.")
                    cursor = connection.execute(
                        "INSERT INTO file_changes (session_id, project_root, path, previous_content, "
                        "previous_mode, result_digest, result_mode, state) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared')",
                        (session_id, root, change.path, change.previous_content, change.previous_mode,
                         change.result_digest, change.result_mode),
                    )
                    return cursor.lastrowid
                if type(change.change_id) is not int:
                    raise ValueError("File-change journal record is missing its ID.")
                expected, target = {
                    "applied": ("prepared", "applied"),
                    "undoing": ("applied", "undoing"),
                    "undone": ("undoing", "undone"),
                }[phase]
                cursor = connection.execute(
                    "UPDATE file_changes SET state = ? WHERE id = ? AND session_id = ? "
                    "AND project_root = ? AND state = ?",
                    (target, change.change_id, session_id, root, expected),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("File-change journal state changed unexpectedly.")
                if phase == "applied":
                    connection.execute(
                        "DELETE FROM file_changes WHERE session_id = ? AND id NOT IN "
                        "(SELECT id FROM file_changes WHERE session_id = ? AND state = 'applied' "
                        "ORDER BY id DESC LIMIT 20) AND state IN ('applied','undone','aborted','conflict')",
                        (session_id, session_id),
                    )
        finally:
            connection.close()

    def load_file_change_history(self, session_id: str, project_root: Path):
        """Reconcile crash-interrupted edits, then return up to 20 safe undo records."""
        from src.tools.file_tools import FileChange

        root = project_root.resolve()
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT * FROM file_changes WHERE session_id = ? AND project_root = ? "
                "AND state IN ('prepared','undoing') ORDER BY id",
                (session_id, str(root)),
            ).fetchall()
            with connection:
                for row in rows:
                    candidate = root / row["path"]
                    try:
                        target = candidate.resolve(strict=False)
                        safe = target.is_relative_to(root) and target == candidate and not candidate.is_symlink()
                    except (OSError, ValueError):
                        safe = False
                    if not safe:
                        connection.execute("UPDATE file_changes SET state = 'conflict' WHERE id = ?", (row["id"],))
                        continue
                    try:
                        current = candidate.read_bytes() if candidate.is_file() else None
                        current_mode = stat.S_IMODE(candidate.stat().st_mode) if current is not None else None
                    except OSError:
                        current, current_mode = None, None
                    result_matches = (
                        current is not None and hashlib.sha256(current).digest() == row["result_digest"]
                        and current_mode == row["result_mode"]
                    )
                    previous_matches = current == row["previous_content"] and current_mode == row["previous_mode"]
                    if result_matches:
                        state = "applied"
                    elif previous_matches:
                        state = "aborted" if row["state"] == "prepared" else "undone"
                    else:
                        state = "conflict"
                    connection.execute("UPDATE file_changes SET state = ? WHERE id = ?", (state, row["id"]))
            records = connection.execute(
                "SELECT * FROM file_changes WHERE session_id = ? AND project_root = ? AND state = 'applied' "
                "ORDER BY id DESC LIMIT 20",
                (session_id, str(root)),
            ).fetchall()
        finally:
            connection.close()
        return [FileChange(
            path=row["path"], previous_content=row["previous_content"],
            previous_mode=row["previous_mode"], result_digest=row["result_digest"],
            result_mode=row["result_mode"], change_id=row["id"],
        ) for row in reversed(records)]

    def append_diagnostic(self, session_id: str, turn_id: str, event: dict[str, Any]) -> None:
        """Append allowlisted metadata only; prompts, tool arguments, and outputs are rejected."""
        fields = {
            "turn_start": set(),
            "model_request": {"round", "estimated_tokens", "tool_count", "loaded_tool_count"},
            "retry": {"retry_attempt", "delay_ms"},
            "tool_start": {"tool_name"},
            "tool_end": {"tool_name", "success", "elapsed_ms", "error_class"},
            "compaction": {"estimated_tokens", "summary_tokens"},
            "turn_end": {"elapsed_ms", "tool_count", "model_requests"},
            "turn_error": {"elapsed_ms", "tool_count", "model_requests", "error_class"},
        }
        if (not isinstance(session_id, str) or not session_id or len(session_id) > 128
                or not isinstance(turn_id, str) or not re.fullmatch(r"[0-9a-f]{32}", turn_id)
                or not isinstance(event, dict) or not isinstance(event.get("type"), str)):
            raise ValueError("Diagnostic event contains unsupported fields.")
        event_type = event.get("type")
        expected_fields = fields.get(event_type)
        if expected_fields is None:
            raise ValueError("Invalid diagnostic event type.")
        required_fields = (
            expected_fields - ({"error_class"} if event_type == "tool_end" else set())
        )
        if set(event) - (expected_fields | {"type"}):
            raise ValueError("Diagnostic event contains unsupported fields.")
        if not required_fields <= set(event):
            raise ValueError("Invalid diagnostic event type.")
        for key, value in event.items():
            if key == "type":
                continue
            if key in {"tool_name", "error_class"}:
                pattern = r"[A-Za-z_][A-Za-z0-9_:-]{0,159}" if key == "tool_name" else r"[A-Za-z_][A-Za-z0-9_]{0,63}"
                if not isinstance(value, str) or not re.fullmatch(pattern, value):
                    raise ValueError("Diagnostic event contains invalid metadata.")
            elif key == "success":
                if type(value) is not bool:
                    raise ValueError("Diagnostic event contains invalid metadata.")
            elif type(value) is not int or value < 0 or value > 10**9:
                raise ValueError("Diagnostic event contains invalid metadata.")
        encoded = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
        if len(encoded) > 2000:
            raise ValueError("Diagnostic event is too large.")
        connection = sqlite3.connect(self.db_path)
        try:
            with connection:
                if not connection.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
                    raise ValueError("Diagnostics must belong to a saved chat.")
                connection.execute(
                    "INSERT INTO diagnostic_events (session_id, turn_id, event_json) VALUES (?, ?, ?)",
                    (session_id, turn_id, encoded),
                )
                connection.execute(
                    "DELETE FROM diagnostic_events WHERE id NOT IN "
                    "(SELECT id FROM diagnostic_events ORDER BY id DESC LIMIT 5000)"
                )
        finally:
            connection.close()

    def recent_diagnostics(self, session_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Diagnostic limit must be between 1 and 500.")
        connection = sqlite3.connect(self.db_path)
        try:
            if session_id is None:
                rows = connection.execute(
                    "SELECT session_id, turn_id, event_json, created_at FROM diagnostic_events "
                    "ORDER BY id DESC LIMIT ?", (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT session_id, turn_id, event_json, created_at FROM diagnostic_events "
                    "WHERE session_id = ? ORDER BY id DESC LIMIT ?", (session_id, limit),
                ).fetchall()
        finally:
            connection.close()
        return [{"session_id": row[0], "turn_id": row[1], "event": json.loads(row[2]), "created_at": row[3]}
                for row in rows]

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
