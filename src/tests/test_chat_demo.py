import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import chat_demo
from src.agent.system_prompt import SYSTEM_PROMPT
from src.providers.types import ModelResponse, ToolCall
from src.session.sqlite_store import SQLiteSessionStore


class ChatDemoTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(chat_demo, "load_project_instructions", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        mcp_patcher = patch.object(chat_demo, "MCPClient")
        self.mcp_client_type = mcp_patcher.start()
        self.mcp_client_type.return_value.start.return_value = []
        self.mcp_client_type.return_value.tool_schemas.return_value = []
        self.mcp_client_type.return_value.tool_directory.return_value = []
        self.addCleanup(mcp_patcher.stop)

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

        def complete(messages, tools, on_text_delta=None, cancel_event=None):
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
        self.mcp_client_type.return_value.start.assert_called_once_with()
        self.mcp_client_type.return_value.tool_schemas.assert_called_once_with()
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
                def complete(messages, tools, on_text_delta=None, cancel_event=None):
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

    def test_repl_budget_pause_keeps_work_and_a_followup_continues_from_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = SQLiteSessionStore(root / "sessions.sqlite3")
            output = io.StringIO()
            with patch.object(chat_demo, "SQLiteSessionStore", return_value=store), \
                 patch("builtins.input", side_effect=["Make a note", "y", "Continue", "/quit"]), \
                 patch.object(chat_demo, "CodexProvider") as provider, contextlib.redirect_stdout(output):
                provider.return_value.complete.side_effect = [
                    ModelResponse(tool_calls=[ToolCall("write", "write_file", {"path": "note.txt", "content": "Created"})]),
                    ModelResponse("The note is already created."),
                ]
                chat_demo.main(["--project", str(root), "--max-rounds", "1"])
            self.assertEqual((root / "note.txt").read_text(), "Created")
            saved = store.load_messages(store.list_sessions()[0])
            self.assertEqual(saved[1]["turn_status"], "paused")
            self.assertEqual(saved[2]["tool_call_id"], "write")
            self.assertEqual(saved[-1]["content"], "The note is already created.")
            self.assertIn("Agent turn paused", output.getvalue())

            # A failed result renderer runs after the paired call/text has been retained.
            with patch.object(chat_demo, "SQLiteSessionStore", return_value=store), \
                 patch("builtins.input", side_effect=["Make another note", "/quit"]), \
                 patch.object(chat_demo, "_confirm_write", return_value=True), \
                 patch.object(chat_demo, "_approval_preview", side_effect=RuntimeError("Result display failed")), \
                 patch.object(chat_demo, "CodexProvider") as provider, contextlib.redirect_stdout(io.StringIO()):
                provider.return_value.complete.return_value = ModelResponse("Creating another note.", [
                    ToolCall("failed-ui", "write_file", {"path": "another.txt", "content": "Created"}),
                ])
                chat_demo.main(["--new", "--project", str(root)])
            failed_session = next(s for s in store.list_sessions() if len(store.load_messages(s)) == 3)
            saved = store.load_messages(failed_session)
            self.assertEqual(saved[1]["content"], "Creating another note.")
            self.assertEqual(saved[1]["turn_status"], "failed")
            self.assertEqual(saved[2]["tool_call_id"], "failed-ui")
            self.assertEqual((root / "another.txt").read_text(), "Created")


if __name__ == "__main__":
    unittest.main()
