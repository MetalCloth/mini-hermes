import tempfile
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

from src.session.sqlite_store import SQLiteSessionStore


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


if __name__ == "__main__":
    unittest.main()
