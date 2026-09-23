import contextlib
import io
import unittest
from unittest.mock import patch

from src import chat_demo
from src.providers.types import ModelResponse


class ChatDemoTests(unittest.TestCase):
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

        def complete(messages, tools):
            histories.append([message.copy() for message in messages])
            advertised_tools.append({tool["name"] for tool in tools})
            return ModelResponse(responses[len(histories) - 1])

        with patch("builtins.input", side_effect=["Hi", "What did I say?", "/quit"]):
            with patch.object(chat_demo, "CodexProvider") as provider:
                provider.return_value.complete.side_effect = complete
                with contextlib.redirect_stdout(output):
                    chat_demo.main([])

        provider.assert_called_once_with("gpt-5.6-luna")
        self.assertIn("write_file", advertised_tools[0])
        self.assertEqual(histories[0], [{"role": "user", "content": "Hi"}])
        self.assertEqual(histories[1], [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "What did I say?"},
        ])
        self.assertIn("assistant> You said hi.", output.getvalue())


if __name__ == "__main__":
    unittest.main()
