import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import chat_demo
from src.agent.system_prompt import SYSTEM_PROMPT
from src.providers.types import ModelResponse
from src.session.sqlite_store import SQLiteSessionStore


class ChatDemoTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(chat_demo, "load_project_instructions", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_write_confirmation_shows_target_and_content_and_eof_denies(self):
        output = io.StringIO()
        with patch("builtins.input", side_effect=EOFError):
            with contextlib.redirect_stdout(output):
                allowed = chat_demo._confirm_write("notes.txt", "hello", True)
        self.assertFalse(allowed)
        self.assertIn("replace notes.txt", output.getvalue())
        self.assertIn("hello", output.getvalue())

    def test_approval_preview_escapes_terminal_control_codes(self):
        self.assertEqual(chat_demo._approval_preview("first\n\x1b[2Jsecond"), "| first\n| \\x1b[2Jsecond")

    def test_keeps_history_between_turns(self):
        output = io.StringIO()
        histories = []
        advertised_tools = []
        responses = ["Hello!", "You said hi."]

        def complete(messages, tools, on_text_delta=None):
            histories.append([message.copy() for message in messages])
            advertised_tools.append({tool["name"] for tool in tools})
            return ModelResponse(responses[len(histories) - 1])

        with patch("builtins.input", side_effect=["Hi", "What did I say?", "/quit"]):
            with patch.object(chat_demo, "CodexProvider") as provider:
                with patch.object(chat_demo, "SQLiteSessionStore") as store:
                    store.return_value.load_messages.return_value = []
                    provider.return_value.complete.side_effect = complete
                    with contextlib.redirect_stdout(output):
                        chat_demo.main([])

        provider.assert_called_once_with("gpt-5.6-luna")
        self.assertIn("write_file", advertised_tools[0])
        self.assertEqual(histories[0], [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Hi"},
        ])
        self.assertEqual(histories[1], [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "What did I say?"},
        ])
        self.assertEqual(len(store.return_value.append_messages.call_args_list), 2)
        self.assertIn("assistant> You said hi.", output.getvalue())

    def test_chat_resumes_persisted_history_on_next_launch(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteSessionStore(Path(folder) / "sessions.sqlite3")
            with patch.object(chat_demo, "SQLiteSessionStore", return_value=store):
                with patch("builtins.input", side_effect=["Hi", "/quit"]):
                    with patch.object(chat_demo, "CodexProvider") as provider:
                        provider.return_value.complete.return_value = ModelResponse("Hello")
                        with contextlib.redirect_stdout(output):
                            chat_demo.main([])

                captured = []
                def complete(messages, tools, on_text_delta=None):
                    captured.append([message.copy() for message in messages])
                    return ModelResponse("Welcome back")

                with patch("builtins.input", side_effect=["Continue", "/quit"]):
                    with patch.object(chat_demo, "CodexProvider") as provider:
                        provider.return_value.complete.side_effect = complete
                        with contextlib.redirect_stdout(output):
                            chat_demo.main([])

            self.assertEqual(captured[0], [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
                {"role": "user", "content": "Continue"},
            ])
            self.assertEqual(len(store.load_messages()), 4)
        self.assertIn("Resumed chat with 2 saved messages", output.getvalue())

    def test_interrupted_turn_is_not_saved_with_an_unanswered_tool_call(self):
        output = io.StringIO()
        with patch("builtins.input", side_effect=["Do something", EOFError]):
            with patch.object(chat_demo, "SQLiteSessionStore") as store:
                store.return_value.load_messages.return_value = []
                with patch.object(chat_demo, "CodexProvider") as provider:
                    provider.return_value.complete.side_effect = KeyboardInterrupt
                    with contextlib.redirect_stdout(output):
                        chat_demo.main([])
        store.return_value.append_messages.assert_not_called()
        self.assertIn("Inspect possible tool effects before retrying", output.getvalue())

    def test_search_cli_shows_matching_saved_chat(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteSessionStore(Path(folder) / "sessions.sqlite3")
            session_id = store.create_session(Path(folder))
            store.append_messages([{"role": "user", "content": "Find the needle here"}], session_id)
            with patch.object(chat_demo, "SQLiteSessionStore", return_value=store):
                with contextlib.redirect_stdout(output):
                    chat_demo.main(["--search", "needle"])
        self.assertIn(session_id, output.getvalue())
        self.assertIn("Find the needle here", output.getvalue())
        self.assertIn("--resume ID", output.getvalue())


if __name__ == "__main__":
    unittest.main()
