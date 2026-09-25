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
        streamed = []
        events = []
        with tempfile.TemporaryDirectory() as folder:
            with patch("src.agent.conversation_loop.execute_tool", return_value="# Mini-Hermes") as execute:
                answer = run_turn(
                    history, complete, tools, Path(folder), confirm, confirm_write,
                    on_text_delta=streamed.append,
                    on_tool_event=lambda phase, call, result: events.append((phase, call.name, result)),
                )

        self.assertEqual(answer, "The project is Mini-Hermes.")
        self.assertEqual(complete.call_count, 2)
        execute.assert_called_once_with(
            "read_file", {"path": "README.md"}, Path(folder), confirm, confirm_write
        )
        self.assertEqual(complete.call_args.args[1], tools)
        self.assertEqual(history[1]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(history[2]["content"], "# Mini-Hermes")
        self.assertEqual(history[2]["tool_call_id"], "call_1")
        self.assertEqual(streamed, ["The project is Mini-Hermes."])
        self.assertEqual(events, [
            ("start", "read_file", None),
            ("result", "read_file", "# Mini-Hermes"),
        ])

    def test_truncates_oversized_tool_result_before_adding_it_to_history(self):
        history = [{"role": "user", "content": "Read a large file"}]
        complete = Mock(side_effect=[
            ModelResponse("", [ToolCall("call_1", "read_file", {"path": "large.txt"})]),
            ModelResponse("The result was truncated."),
        ])
        with tempfile.TemporaryDirectory() as folder:
            with patch("src.agent.conversation_loop.execute_tool", return_value="x" * 20_001):
                run_turn(history, complete, [], Path(folder), Mock(), Mock())

        result = history[-1]["content"]
        self.assertLessEqual(len(result), 20_000)
        self.assertIn("original result was 20001 characters", result)

    def test_executes_multiple_tool_calls_and_keeps_each_result_with_its_call(self):
        history = [{"role": "user", "content": "Read two files"}]
        first = ToolCall("call_1", "read_file", {"path": "one.txt"})
        second = ToolCall("call_2", "read_file", {"path": "two.txt"})
        complete = Mock(side_effect=[
            ModelResponse(tool_calls=[first, second]),
            ModelResponse("Both files are read."),
        ])
        with tempfile.TemporaryDirectory() as folder:
            with patch("src.agent.conversation_loop.execute_tool", side_effect=["one", "two"]) as execute:
                answer = run_turn(history, complete, [], Path(folder), Mock(), Mock())

        self.assertEqual(answer, "Both files are read.")
        self.assertEqual([call.args[1] for call in execute.call_args_list], [first.arguments, second.arguments])
        self.assertEqual([call["id"] for call in history[1]["tool_calls"]], ["call_1", "call_2"])
        self.assertEqual([message["content"] for message in history[2:4]], ["one", "two"])
        self.assertEqual([message["tool_call_id"] for message in history[2:4]], ["call_1", "call_2"])

    def test_tool_exception_is_returned_to_model_as_a_tool_result(self):
        history = [{"role": "user", "content": "Read a missing file"}]
        complete = Mock(side_effect=[
            ModelResponse(tool_calls=[ToolCall("call_1", "read_file", {"path": "missing.txt"})]),
            ModelResponse("That file does not exist."),
        ])
        with tempfile.TemporaryDirectory() as folder:
            with patch("src.agent.conversation_loop.execute_tool", side_effect=FileNotFoundError("missing.txt")):
                answer = run_turn(history, complete, [], Path(folder), Mock(), Mock())

        self.assertEqual(answer, "That file does not exist.")
        self.assertEqual(history[2]["tool_call_id"], "call_1")
        self.assertIn("Tool error: missing.txt", history[2]["content"])

    def test_cancellation_between_tools_keeps_only_completed_call_pairs(self):
        from threading import Event
        from src.agent.conversation_loop import TurnCancelled

        history = [{"role": "user", "content": "Read two files"}]
        calls = [
            ToolCall("call_1", "read_file", {"path": "one.txt"}),
            ToolCall("call_2", "read_file", {"path": "two.txt"}),
        ]
        complete = Mock(return_value=ModelResponse(tool_calls=calls))
        cancel = Event()

        def event(phase, call, result):
            if phase == "result":
                cancel.set()

        with tempfile.TemporaryDirectory() as folder:
            with patch("src.agent.conversation_loop.execute_tool", return_value="one") as execute:
                with self.assertRaises(TurnCancelled):
                    run_turn(history, complete, [], Path(folder), Mock(), Mock(),
                             on_tool_event=event, cancel_event=cancel)

        execute.assert_called_once()
        self.assertEqual([call["id"] for call in history[1]["tool_calls"]], ["call_1"])
        self.assertEqual(history[2]["tool_call_id"], "call_1")

    def test_failed_tool_event_does_not_leave_an_unanswered_call(self):
        history = [{"role": "user", "content": "Search"}]
        complete = Mock(return_value=ModelResponse(tool_calls=[
            ToolCall("call_1", "web_search", {"query": "example", "max_results": 1})
        ]))
        def show_tool(phase, call, result):
            if phase == "result":
                raise ConnectionResetError("Browser closed")

        with tempfile.TemporaryDirectory() as folder:
            with patch("src.agent.conversation_loop.execute_tool", return_value="A result"):
                with self.assertRaises(ConnectionResetError):
                    run_turn(history, complete, [], Path(folder), Mock(), Mock(), on_tool_event=show_tool)
        self.assertEqual(history, [{"role": "user", "content": "Search"}])


if __name__ == "__main__":
    unittest.main()
