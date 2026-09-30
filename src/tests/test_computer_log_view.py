"""Small checks for the readable computer trace view."""

import unittest

from scripts.computer_log_view import format_record


class ComputerLogViewTests(unittest.TestCase):
    def test_hides_repeated_prompt_and_formats_plan_and_driver_error(self):
        request = format_record({
            "time_utc": "2026-09-30T10:18:15.120+00:00",
            "event": "model_request",
            "model": "gpt-5.6-luna",
            "developer_instructions": "a very long repeated prompt",
            "screenshot": {"width": 1920, "height": 1080, "size_bytes": 150225},
        })
        self.assertIn("MODEL INPUT · gpt-5.6-luna · screen 1920×1080", request)
        self.assertNotIn("repeated prompt", request)

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
            "event": "wdotool_result",
            "returncode": 1,
            "stderr": "\x1b[2mINFO using forced backend\x1b[0m\nError: keysym 'super_l' not found",
        })
        self.assertIn("INPUT FAILED · exit 1", failure)
        self.assertIn("keysym 'super_l' not found", failure)
        self.assertNotIn("\x1b", failure)

        task_failure = format_record({
            "time_utc": "2026-09-30T10:18:50.582+00:00",
            "event": "computer_task_failed",
            "error_type": "RuntimeError",
            "error": "INFO using forced backend backend=\"wlr-protocols\"\nError: keysym 'super' not found",
        })
        self.assertIn("keysym 'super' not found", task_failure)
        self.assertNotIn("INFO using forced backend", task_failure)


if __name__ == "__main__":
    unittest.main()
