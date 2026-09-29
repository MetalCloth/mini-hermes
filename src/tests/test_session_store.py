import tempfile
import sqlite3
import hashlib
import stat
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

from src.session.sqlite_store import SQLiteSessionStore
from src.tools.file_tools import FileChange, undo_file_change, write_file


class SQLiteSessionStoreTests(unittest.TestCase):
    def test_picker_metadata_migrates_and_persists_without_inventing_legacy_dates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sessions.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, project_root TEXT, title TEXT)")
                connection.execute("INSERT INTO sessions (id, title) VALUES ('legacy', 'Old chat')")
                connection.commit()
            store = SQLiteSessionStore(path)
            self.assertIsNone(store.session_entries()[0]["updated_at"])
            self.assertEqual(store.session_title("legacy"), "Old chat")
            self.assertEqual(store.session_model_settings("legacy", "gpt-6-astra"), {})
            astra = {"reasoning_effort": "high", "service_tier": "priority"}
            luna = {"reasoning_effort": "low", "service_tier": "default"}
            store.set_session_model_settings("legacy", "gpt-6-astra", astra)
            store.set_session_model_settings("legacy", "gpt-6-luna", luna)
            reopened = SQLiteSessionStore(path)
            self.assertEqual(reopened.session_model_settings("legacy", "gpt-6-astra"), astra)
            self.assertEqual(reopened.session_model_settings("legacy", "gpt-6-luna"), luna)
            self.assertEqual(reopened.session_model_settings("missing", "gpt-6-astra"), {})
            with self.assertRaises(ValueError):
                store.set_session_model_settings("legacy", "gpt-6-astra", {**astra, "service_tier": None})
            with self.assertRaises(ValueError):
                store.set_session_model_settings("missing", "gpt-6-astra", astra)
            self.assertEqual(store.session_model_settings("legacy", "gpt-6-astra"), astra)
            recent = store.create_session(Path(folder))
            store.append_messages([{"role": "user", "content": "hello"}], recent)
            store.toggle_session_pin("legacy")
            entries = SQLiteSessionStore(path).session_entries()
            self.assertEqual(entries[0]["id"], "legacy")
            self.assertEqual(entries[0]["pinned"], 1)
            self.assertTrue(entries[1]["updated_at"])
            store.toggle_session_pin("legacy")
            self.assertEqual(store.session_entries()[0]["id"], recent)
            with self.assertRaises(ValueError):
                store.toggle_session_pin("missing")

    def test_reopens_and_restores_ordered_messages_with_tool_data(self):
        messages = [
            {"role": "user", "content": "Read README"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "name": "read_file", "arguments": {"path": "README.md"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "# Mini-Hermes"},
            {"role": "assistant", "content": "It is Mini-Hermes."},
        ]
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "state" / "sessions.sqlite3"
            SQLiteSessionStore(db_path).append_messages(messages)
            restored = SQLiteSessionStore(db_path).load_messages()
            self.assertEqual(restored, messages)
            self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)

    def test_appends_messages_in_one_batch_and_rolls_back_invalid_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteSessionStore(Path(folder) / "sessions.sqlite3")
            store.append_messages([{"role": "user", "content": "first"}])
            with self.assertRaises(TypeError):
                store.append_messages([
                    {"role": "user", "content": "second"},
                    {"role": "user", "content": object()},
                ])
            self.assertEqual(store.load_messages(), [{"role": "user", "content": "first"}])

    def test_empty_database_loads_as_empty_history(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteSessionStore(Path(folder) / "sessions.sqlite3")
            self.assertEqual(store.load_messages(), [])

    def test_searches_saved_chat_text_across_sessions(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteSessionStore(Path(folder) / "sessions.sqlite3")
            first = store.create_session(Path(folder) / "first")
            second = store.create_session(Path(folder) / "second")
            store.append_messages([
                {"role": "user", "content": "HELLO from first"},
                {"role": "tool", "content": "hello tool output"},
            ], first)
            store.append_messages([
                {"role": "user", "content": "Where is hello?"},
                {"role": "assistant", "content": "Hello again."},
            ], second)
            matches = store.search_messages("hello")
            self.assertEqual([(item[0], item[2]) for item in matches], [
                (second, "assistant"), (second, "user"), (first, "user"),
            ])
            self.assertEqual(len(store.search_messages("hello", limit=2)), 2)
            with self.assertRaises(ValueError):
                store.search_messages(" ")

    def test_file_change_is_durable_and_can_be_undone_after_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"
            root.mkdir()
            path = root / "note.txt"
            path.write_text("before\n")
            db_path = Path(folder) / "sessions.sqlite3"
            store = SQLiteSessionStore(db_path)
            session = store.create_session(root)
            history = store.load_file_change_history(session, root)
            journal = lambda phase, change: store.record_file_change(session, root, phase, change)

            write_file("note.txt", "after\n", root, Mock(return_value=True), history, journal)
            self.assertEqual(path.read_text(), "after\n")
            self.assertEqual(len(history), 1)
            self.assertIsNotNone(history[0].change_id)

            reopened = SQLiteSessionStore(db_path)
            restored_history = reopened.load_file_change_history(session, root)
            self.assertEqual(len(restored_history), 1)
            undo_journal = lambda phase, change: reopened.record_file_change(session, root, phase, change)
            undo_file_change(root, restored_history, Mock(return_value=True), undo_journal)
            self.assertEqual(path.read_text(), "before\n")
            self.assertEqual(reopened.load_file_change_history(session, root), [])

    def test_restart_reconciles_prepared_change_using_file_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"
            root.mkdir()
            path = root / "note.txt"
            path.write_text("before")
            mode = stat.S_IMODE(path.stat().st_mode)
            db_path = Path(folder) / "sessions.sqlite3"
            store = SQLiteSessionStore(db_path)
            session = store.create_session(root)
            change = FileChange(
                "note.txt", b"before", mode, hashlib.sha256(b"after").digest(), mode,
            )
            change_id = store.record_file_change(session, root, "prepared", change)
            path.write_text("after")  # Simulate a process exit before the "applied" update.

            reopened = SQLiteSessionStore(db_path)
            history = reopened.load_file_change_history(session, root)
            self.assertEqual([item.change_id for item in history], [change_id])
            path.write_text("user edit")
            with self.assertRaisesRegex(RuntimeError, "changed after Oryn"):
                undo_file_change(root, history, Mock(return_value=True))
            self.assertEqual(path.read_text(), "user edit")

    def test_file_change_snapshots_expire_after_retention_window(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"
            root.mkdir()
            path = root / "note.txt"
            path.write_text("after")
            store = SQLiteSessionStore(Path(folder) / "sessions.sqlite3")
            session = store.create_session(root)
            mode = stat.S_IMODE(path.stat().st_mode)
            change = FileChange(
                "note.txt", b"before", mode, hashlib.sha256(b"after").digest(), mode,
            )
            change_id = store.record_file_change(session, root, "prepared", change)
            change = FileChange(
                change.path, change.previous_content, change.previous_mode,
                change.result_digest, change.result_mode, change_id,
            )
            store.record_file_change(session, root, "applied", change)
            with closing(sqlite3.connect(store.db_path)) as connection:
                connection.execute(
                    "UPDATE file_changes SET created_at = datetime('now', '-31 days') WHERE id = ?",
                    (change_id,),
                )
                connection.commit()

            self.assertEqual(store.load_file_change_history(session, root), [])

    def test_diagnostics_store_only_allowlisted_redacted_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteSessionStore(Path(folder) / "sessions.sqlite3")
            session = store.create_session(Path(folder))
            turn_id = "a" * 32
            event = {"type": "tool_end", "tool_name": "read_file", "success": True, "elapsed_ms": 17}
            store.append_diagnostic(session, turn_id, event)
            self.assertEqual(store.recent_diagnostics(session)[0]["event"], event)
            upstream = {
                "type": "tool_end", "tool_name": "browser_click", "success": False,
                "elapsed_ms": 17, "error_class": "FirecrawlHTTPError", "upstream_status": 502,
                "upstream_request_id": "req-123abc", "upstream_detail": "temporary failure",
            }
            store.append_diagnostic(session, turn_id, upstream)
            self.assertEqual(store.recent_diagnostics(session)[0]["event"], upstream)
            with self.assertRaisesRegex(ValueError, "invalid metadata"):
                store.append_diagnostic(session, turn_id, {
                    **upstream, "upstream_detail": "Bearer secret-token",
                })
            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                store.append_diagnostic(session, turn_id, {"type": "tool_end", "content": "private text"})
            with self.assertRaisesRegex(ValueError, "invalid metadata"):
                store.append_diagnostic(session, turn_id, {
                    "type": "model_request", "round": True, "estimated_tokens": 10,
                    "tool_count": 0, "loaded_tool_count": 0,
                })
            with self.assertRaisesRegex(ValueError, "saved chat"):
                store.append_diagnostic("unknown", turn_id, {"type": "turn_start"})

    def test_context_summary_checkpoint_survives_reopen_separately_from_transcript(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteSessionStore(Path(folder) / "sessions.sqlite3")
            session = store.create_session(Path(folder))
            messages = [{"role": "user", "content": "Original request"}]
            store.append_messages(messages, session)
            from src.agent.context import history_digest
            digest = history_digest(messages, 1)
            store.save_context_summary(session, "Goal: continue the request.", 1, digest)

            reopened = SQLiteSessionStore(store.db_path)
            self.assertEqual(reopened.load_context_summary(session), (
                "Goal: continue the request.", 1, digest,
            ))
            self.assertEqual(reopened.load_messages(session), messages)


if __name__ == "__main__":
    unittest.main()
