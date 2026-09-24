import tempfile
import unittest
from pathlib import Path

from src.session.sqlite_store import SQLiteSessionStore


class SQLiteSessionStoreTests(unittest.TestCase):
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
