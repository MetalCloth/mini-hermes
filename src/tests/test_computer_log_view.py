"""Small checks for the readable computer trace view."""

import unittest

from scripts.computer_log_view import format_record


class ComputerLogViewTests(unittest.TestCase):
    def test_formats_structured_computer_plan_function_call(self):
        response = format_record({
            "time_utc": "2026-10-02T10:18:43.797+00:00",
            "event": "model_response",
            "elapsed_seconds": 4.2,
            "text": "",
            "tool_calls": [{"name": "computer_plan", "arguments": {
                "status": "actions", "summary": "Open the first result",
            }}],
        })
        self.assertIn("MODEL RESPONSE · 4.2s · actions · Open the first result", response)

    def test_hides_repeated_prompt_and_formats_plan_and_driver_error(self):
        request = format_record({
            "time_utc": "2026-09-30T10:18:15.120+00:00",
            "event": "model_request",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
            "service_tier": "priority",
            "socket_timeout_seconds": 120,
            "developer_instructions": "a very long repeated prompt",
            "screenshot": {"width": 1920, "height": 1080, "size_bytes": 150225},
        })
        self.assertIn("MODEL INPUT · gpt-5.6-luna · screen 1920×1080", request)
        self.assertIn("effort max · speed priority · socket timeout 120s", request)
        self.assertNotIn("repeated prompt", request)

        timeout = format_record({
            "time_utc": "2026-09-30T10:18:43.797+00:00",
            "event": "model_error",
            "elapsed_seconds": 125.0,
            "error_type": "ProviderRequestError",
            "error": "Codex request failed during opening connection and waiting for HTTP response headers "
                     "(TimeoutError): timed out; model output received=false; request_id unavailable",
        })
        self.assertIn("MODEL ERROR · 125.0s", timeout)
        self.assertIn("waiting for HTTP response headers", timeout)
        self.assertIn("model output received=false", timeout)

        plan = format_record({
            "time_utc": "2026-09-30T10:18:43.766+00:00",
            "event": "validated_plan",
            "turn": 2,
            "plan": {
                "status": "actions", "summary": "Open the launcher",
                "actions": [{"type": "key", "key": "super_l"}],
            },
        })
        self.assertIn("PLAN · turn 2 · actions", plan)
        self.assertIn("1. press super_l", plan)

        failure = format_record({
            "time_utc": "2026-09-30T10:18:43.797+00:00",
            "event": "dotool_result",
            "returncode": 1,
            "success": False,
            "stderr": "\x1b[2mdotool: WARNING\x1b[0m\nimpossible key for layout: super_l",
        })
        self.assertIn("INPUT FAILED · exit 1", failure)
        self.assertIn("impossible key for layout: super_l", failure)
        self.assertNotIn("\x1b", failure)

        input_command = format_record({
            "time_utc": "2026-09-30T10:18:43.797+00:00",
            "event": "dotool_start",
            "argv": ["/usr/bin/dotool"],
            "stdin": "mouseto 0.5 0.5\nclick left\n",
        })
        self.assertIn("INPUT · dotool", input_command)
        self.assertIn("Actions:", input_command)

        warning = format_record({
            "time_utc": "2026-09-30T10:18:43.797+00:00",
            "event": "dotool_result",
            "returncode": 0,
            "success": False,
            "stderr": "dotool: WARNING: impossible key for layout: windows",
        })
        self.assertIn("INPUT FAILED · exit 0", warning)

        task_failure = format_record({
            "time_utc": "2026-09-30T10:18:50.582+00:00",
            "event": "computer_task_failed",
            "error_type": "RuntimeError",
            "error": "dotool: WARNING: impossible key for layout: super",
        })
        self.assertIn("impossible key for layout: super", task_failure)


if __name__ == "__main__":
    unittest.main()
