import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.agent.conversation_loop import run_turn
from src.providers.types import ModelResponse, ToolCall


class ConversationLoopTests(unittest.TestCase):
    def test_executes_tool_then_returns_final_model_text(self):
        history = [{"role": "user", "content": "Read README"}]
        complete = Mock(side_effect=[
            ModelResponse("", [ToolCall("call_1", "read_file", {"path": "README.md"})]),
            ModelResponse("The project is Mini-Hermes."),
        ])
        tools = [{"type": "function", "name": "read_file"}]
        confirm = Mock(return_value=False)
        confirm_write = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as folder:
            with patch("src.agent.conversation_loop.execute_tool", return_value="# Mini-Hermes") as execute:
                answer = run_turn(history, complete, tools, Path(folder), confirm, confirm_write)

        self.assertEqual(answer, "The project is Mini-Hermes.")
        self.assertEqual(complete.call_count, 2)
        execute.assert_called_once_with(
            "read_file", {"path": "README.md"}, Path(folder), confirm, confirm_write
        )
        self.assertEqual(complete.call_args.args[1], tools)
        self.assertEqual(history[1]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(history[2]["content"], "# Mini-Hermes")
        self.assertEqual(history[2]["tool_call_id"], "call_1")


if __name__ == "__main__":
    unittest.main()
