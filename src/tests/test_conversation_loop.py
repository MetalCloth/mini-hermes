import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.agent.conversation_loop import TurnCancelled, TurnLimitReached, TurnLimits, run_turn
from src.providers.types import ModelResponse, ProviderRequestError, ToolCall
from src.tools.registry import tool_schemas


class ConversationLoopTests(unittest.TestCase):
    def test_read_only_subagent_gets_isolated_context_and_bounded_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "marker.txt").write_text("ORYN_CHILD_MARKER\n")
            history = [{"role": "user", "content": "Inspect marker.txt independently; PARENT_PRIVATE_CONTEXT."}]
            complete = Mock(side_effect=[
                ModelResponse(tool_calls=[ToolCall(
                    "delegate", "delegate_read_only", {"task": "Read marker.txt and report the marker."},
                )]),
                ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "marker.txt"})]),
                ModelResponse("Findings: marker is ORYN_CHILD_MARKER. Evidence: marker.txt:1."),
                ModelResponse("The marker is ORYN_CHILD_MARKER."),
            ])
            diagnostics = []
            answer = run_turn(
                history, complete, tool_schemas(), root, Mock(), Mock(),
                on_diagnostic=lambda _turn_id, event: diagnostics.append(event),
            )

            worker_tools = {tool["name"] for tool in complete.call_args_list[1].args[1]}
            worker_messages = complete.call_args_list[1].args[0]
            self.assertEqual(answer, "The marker is ORYN_CHILD_MARKER.")
            self.assertIn("read_file", worker_tools)
            self.assertIn("search_files", worker_tools)
            self.assertIn("git_status", worker_tools)
            self.assertIn("git_diff", worker_tools)
            self.assertNotIn("load_skill", worker_tools)
            self.assertNotIn("write_file", worker_tools)
            self.assertNotIn("terminal", worker_tools)
            self.assertNotIn("delegate_read_only", worker_tools)
            self.assertNotIn("PARENT_PRIVATE_CONTEXT", repr(worker_messages))
            self.assertTrue(any(event["type"] == "subagent_end" for event in diagnostics))

    def test_parent_keeps_answering_after_a_subagent_failure(self):
        history = [{"role": "user", "content": "Inspect independently, then summarize."}]
        complete = Mock(side_effect=[
            ModelResponse(tool_calls=[ToolCall(
                "delegate", "delegate_read_only", {"task": "Inspect one file."},
            )]),
            RuntimeError("worker provider is unavailable"),
            ModelResponse("Delegation failed, so I continued without its findings."),
        ])
        with tempfile.TemporaryDirectory() as folder:
            answer = run_turn(history, complete, tool_schemas(), Path(folder), Mock(), Mock())
        self.assertIn("continued without", answer)
        self.assertIn('"status": "failed"', history[-1]["content"])

    def test_skill_instructions_load_only_when_selected_and_are_audited(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            skill_dir = root / ".agents" / "skills" / "short-answer"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: short-answer\ndescription: Keep answers concise.\n---\n"
                "Prefer one short paragraph.\n",
            )
            history = [{"role": "user", "content": "Explain this simply."}]
            complete = Mock(side_effect=[
                ModelResponse(tool_calls=[ToolCall("skill", "load_skill", {"name": "short-answer"})]),
                ModelResponse("A concise explanation."),
            ])
            diagnostics = []
            answer = run_turn(
                history, complete, tool_schemas(), root, Mock(), Mock(),
                on_diagnostic=lambda _turn_id, event: diagnostics.append(event),
            )

            self.assertEqual(answer, "A concise explanation.")
            first_tools = {tool["name"] for tool in complete.call_args_list[0].args[1]}
            self.assertIn("load_skill", first_tools)
            self.assertIn("Prefer one short paragraph.", repr(complete.call_args_list[1].args[0]))
            self.assertEqual(sum(event["type"] == "skill_loaded" for event in diagnostics), 1)

    def test_tool_schemas_are_passed_into_context_budgeting(self):
        from src.agent import context

        complete = Mock(return_value=ModelResponse("Done"))
        tools = [{"name": "large_tool", "description": "schema " * 40}]
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(context, "MAX_CONTEXT_TOKENS", 100):
                with self.assertRaisesRegex(ValueError, "tool definitions"):
                    run_turn(
                        [{"role": "system", "content": "Rules"}, {"role": "user", "content": "Hi"}],
                        complete, tools, Path(folder), Mock(), Mock(),
                    )
        complete.assert_not_called()

    def test_diagnostics_record_counts_and_timing_without_tool_content(self):
        history = [{"role": "user", "content": "Read private-looking file"}]
        secret = "do-not-record-this-content"
        complete = Mock(side_effect=[
            ModelResponse(tool_calls=[ToolCall("call_1", "read_file", {"path": secret})]),
            ModelResponse("Done."),
        ])
        events = []
        with tempfile.TemporaryDirectory() as folder:
            with patch("src.agent.skills.discover_skills", return_value=({}, [])), \
                 patch("src.agent.conversation_loop.execute_tool", return_value=secret):
                self.assertEqual(run_turn(
                    history, complete, [{"name": "read_file"}], Path(folder), Mock(), Mock(),
                    on_diagnostic=lambda turn_id, event: events.append((turn_id, event)),
                ), "Done.")
        self.assertTrue(all(turn_id == events[0][0] for turn_id, _ in events))
        self.assertEqual([event["type"] for _, event in events], [
            "turn_start", "model_request", "tool_start", "tool_end", "model_request", "turn_end",
        ])
        self.assertNotIn(secret, repr(events))
        self.assertEqual(events[-1][1]["tool_count"], 1)

    def test_firecrawl_failure_trace_keeps_safe_status_detail_and_request_id(self):
        from src.tools.browser_tools import FirecrawlHTTPError

        history = [{"role": "user", "content": "Click the next page."}]
        complete = Mock(side_effect=[
            ModelResponse(tool_calls=[ToolCall("call_1", "browser_click", {"ref": "@e1"})]),
            ModelResponse("Firecrawl failed; I did not retry the click."),
        ])
        failure = FirecrawlHTTPError(
            "HTTP 502. The action may have run. Firecrawl detail: temporary upstream failure "
            "Request ID: req-123abc", status_code=502, request_id="req-123abc",
            detail="temporary upstream failure",
        )
        events = []
        with tempfile.TemporaryDirectory() as folder:
            with patch("src.agent.conversation_loop.execute_tool", side_effect=failure):
                run_turn(
                    history, complete, [{"name": "browser_click"}], Path(folder), Mock(), Mock(),
                    on_diagnostic=lambda _turn_id, event: events.append(event),
                )
        tool_end = next(event for event in events if event["type"] == "tool_end")
        self.assertEqual(tool_end["upstream_status"], 502)
        self.assertEqual(tool_end["upstream_request_id"], "req-123abc")
        self.assertEqual(tool_end["upstream_detail"], "temporary upstream failure")

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
            with patch("src.agent.skills.discover_skills", return_value=({}, [])), \
                 patch("src.agent.conversation_loop.execute_tool", return_value="# Mini-Hermes") as execute:
                answer = run_turn(
                    history, complete, tools, Path(folder), confirm, confirm_write,
                    on_text_delta=streamed.append,
                    on_tool_event=lambda phase, call, result: events.append((phase, call.name, result)),
                )

        self.assertEqual(answer, "The project is Mini-Hermes.")
        self.assertEqual(complete.call_count, 2)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[:3], ("read_file", {"path": "README.md"}, Path(folder)))
        self.assertFalse(execute.call_args.args[3]("denied command"))
        confirm.assert_called_once_with("denied command")
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
        self.assertEqual(history[0], {"role": "user", "content": "Search"})
        self.assertEqual(history[1]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(history[2]["tool_call_id"], "call_1")
        self.assertEqual(history[2]["content"], "A result")

    def test_recovery_is_bounded_cancellable_and_never_replays_visible_output_or_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            temporary = ProviderRequestError("Temporary failure", retryable=True, retry_after=60)
            cancel = threading.Event()
            statuses = []
            history = [{"role": "user", "content": "Read a file"}]
            complete = Mock(side_effect=[
                ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "README.md"})]),
                temporary, temporary, ModelResponse("Read successfully."),
            ])
            with patch.object(cancel, "wait", return_value=False) as wait, \
                 patch("src.agent.conversation_loop.execute_tool", return_value="File text") as execute:
                answer = run_turn(history, complete, [], root, Mock(), Mock(),
                                  cancel_event=cancel, on_status=statuses.append)
                self.assertEqual(answer, "Read successfully.")
                execute.assert_called_once()
                self.assertEqual(complete.call_count, 4)
                self.assertEqual([call.args[0] for call in wait.call_args_list], [30.0, 30.0])
                self.assertEqual(sum("retry" in status for status in statuses), 2)
                self.assertEqual(len([m for m in history if m["role"] == "tool"]), 1)

            for retryable, expected_attempts in ((True, 3), (False, 1)):
                complete = Mock(side_effect=ProviderRequestError("Failure", retryable=retryable))
                with patch.object(cancel, "wait", return_value=False):
                    with self.assertRaises(ProviderRequestError):
                        run_turn([{"role": "user", "content": "Hi"}], complete, [], root,
                                 Mock(), Mock(), cancel_event=cancel)
                self.assertEqual(complete.call_count, expected_attempts)

            deltas = []

            def partial(_messages, _tools, **kwargs):
                kwargs["on_text_delta"]("Partial reply")
                raise temporary

            complete = Mock(side_effect=partial)
            with self.assertRaises(ProviderRequestError):
                run_turn([{"role": "user", "content": "Hi"}], complete, [], root,
                         Mock(), Mock(), on_text_delta=deltas.append)
            complete.assert_called_once()
            self.assertEqual(deltas, ["Partial reply"])

            complete = Mock(side_effect=temporary)
            def stop_on_retry(status):
                if "retry" in status:
                    cancel.set()
            with self.assertRaises(TurnCancelled):
                run_turn([{"role": "user", "content": "Hi"}], complete, [], root,
                         Mock(), Mock(), cancel_event=cancel, on_status=stop_on_retry)
            complete.assert_called_once()

    def test_long_turns_pause_with_completed_pairs_and_continue_without_replaying_changes(self):
        for values in ({"max_rounds": 0}, {"max_rounds": True}, {"max_tool_calls": 2001},
                       {"max_turn_seconds": 1.5}, {"max_turn_seconds": 7201}):
            with self.assertRaises(ValueError):
                TurnLimits(**values)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            history = [{"role": "user", "content": "Read many files"}]
            responses = [ModelResponse(tool_calls=[ToolCall(str(i), "read_file", {"path": "README.md"})])
                         for i in range(12)] + [ModelResponse("Finished a longer task.")]
            with patch("src.agent.conversation_loop.execute_tool", return_value="Text") as execute:
                self.assertEqual(run_turn(history, Mock(side_effect=responses), [], root, Mock(), Mock()),
                                 "Finished a longer task.")
                self.assertEqual(execute.call_count, 12)

            (root / "note.txt").write_text("Original")
            history = [{"role": "user", "content": "Update my note"}]
            complete = Mock(return_value=ModelResponse(tool_calls=[
                ToolCall("change", "write_file", {"path": "note.txt", "content": "Changed"}),
            ]))
            with self.assertRaisesRegex(TurnLimitReached, "1-round"):
                run_turn(history, complete, [], root, Mock(), Mock(return_value=True), limits=TurnLimits(max_rounds=1))
            self.assertEqual((root / "note.txt").read_text(), "Changed")
            self.assertEqual(history[-1]["tool_call_id"], "change")
            history[1]["turn_status"] = "paused"
            history.append({"role": "user", "content": "Continue"})
            complete = Mock(return_value=ModelResponse("The note is already changed."))
            with patch("src.agent.conversation_loop.execute_tool") as execute:
                self.assertEqual(run_turn(history, complete, [], root, Mock(), Mock()),
                                 "The note is already changed.")
                execute.assert_not_called()
            self.assertIn("execution budget", complete.call_args.args[0][1]["content"])

            history = [{"role": "user", "content": "Read two files"}]
            complete = Mock(return_value=ModelResponse(tool_calls=[
                ToolCall("one", "read_file", {"path": "note.txt"}),
                ToolCall("two", "read_file", {"path": "note.txt"}),
            ]))
            with self.assertRaisesRegex(TurnLimitReached, "1-tool-call"):
                run_turn(history, complete, [], root, Mock(), Mock(), limits=TurnLimits(max_tool_calls=1))
            self.assertEqual([call["id"] for call in history[1]["tool_calls"]], ["one"])
            self.assertEqual(history[2]["tool_call_id"], "one")

            # Approval cannot authorize a file write after its turn budget has expired.
            now = [0.0]
            def late_approval(*_args):
                now[0] = 2.0
                return True
            with patch("src.agent.conversation_loop.time.monotonic", side_effect=lambda: now[0]):
                with self.assertRaisesRegex(TurnLimitReached, "1-second"):
                    run_turn([{"role": "user", "content": "Write"}], Mock(return_value=ModelResponse(tool_calls=[
                        ToolCall("late", "write_file", {"path": "note.txt", "content": "Too late"}),
                    ])), [], root, Mock(), late_approval, limits=TurnLimits(max_turn_seconds=1))
            self.assertEqual((root / "note.txt").read_text(), "Changed")

            def blocked_request(_messages, _tools, *, cancel_event):
                self.assertTrue(cancel_event.wait(2))
                raise InterruptedError("Stopped request")
            with self.assertRaises(TurnLimitReached):
                run_turn([{"role": "user", "content": "Wait"}], blocked_request, [], root,
                         Mock(), Mock(), limits=TurnLimits(max_turn_seconds=1))


if __name__ == "__main__":
    unittest.main()
