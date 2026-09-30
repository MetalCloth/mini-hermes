"""Checks private JSONL computer traces and captured wdotool failures."""

import io
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from src.computer_logging import ComputerTrace
from src.providers.codex import CodexProvider
from src.providers.computer import ComputerPlanner
from src.providers.types import ModelResponse
from src.tools.computer_driver import HyprlandDriver


class ComputerLoggingTests(unittest.TestCase):
    def test_planner_logs_exact_request_context_and_model_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "computer.jsonl"
            trace = ComputerTrace(path)
            image = io.BytesIO()
            Image.new("RGB", (800, 450), "black").save(image, format="JPEG")
            plan = {
                "status": "actions", "summary": "Open the launcher", "question": "",
                "actions": [{"type": "key", "key": "super"}],
                "expected_result": "The launcher opens", "requires_confirmation": False,
                "confirmation_reason": "",
            }
            provider = CodexProvider("gpt-6-sol")
            planner = ComputerPlanner(provider, trace)
            try:
                with patch.object(
                    provider, "complete", return_value=ModelResponse(json.dumps(plan), []),
                ):
                    self.assertEqual(
                        planner.next_plan("Open Brave", image.getvalue(), (800, 450), [], ""), plan,
                    )
            finally:
                trace.close()

            request, response = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(request["event"], "model_request")
            self.assertIn("Do not use shell commands", request["developer_instructions"])
            self.assertIn("Open Brave", request["user_content"])
            self.assertEqual(
                {key: request["screenshot"][key] for key in ("width", "height", "mime_type")},
                {"width": 800, "height": 450, "mime_type": "image/jpeg"},
            )
            self.assertIsInstance(request["screenshot"]["size_bytes"], int)
            self.assertEqual(response["event"], "model_response")
            self.assertEqual(response["text"], json.dumps(plan))

    def test_driver_logs_exact_wdotool_command_stdin_and_failure_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "computer.jsonl"
            trace = ComputerTrace(path)
            driver = HyprlandDriver.__new__(HyprlandDriver)
            driver.trace = trace
            driver.wdotool = "/repo/.venv/bin/wdotool"
            try:
                with patch("src.tools.computer_driver.subprocess.run") as run:
                    run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
                    driver._input("type", "--file", "-", input_data=b"hello")
                    run.side_effect = subprocess.CalledProcessError(
                        1, [driver.wdotool], stderr=b"unknown key name: windows",
                    )
                    with self.assertRaisesRegex(RuntimeError, "unknown key name"):
                        driver._input("key", "windows")
            finally:
                trace.close()

            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[0]["event"], "wdotool_start")
            self.assertEqual(records[0]["argv"], [
                "/repo/.venv/bin/wdotool", "--backend", "wlr-protocols", "type", "--file", "-",
            ])
            self.assertEqual(records[0]["stdin"], "hello")
            self.assertEqual(records[-1]["stderr"], "unknown key name: windows")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_driver_still_types_text_with_or_without_return(self):
        driver = HyprlandDriver.__new__(HyprlandDriver)
        with patch.object(driver, "_input") as send:
            driver.execute("type", {"content": "hello"})
            send.assert_called_once_with("type", "--file", "-", input_data=b"hello")
            send.reset_mock()
            driver.execute("type", {"content": "hello\n"})
            self.assertEqual(send.call_count, 2)
            self.assertEqual(send.call_args_list[0].kwargs["input_data"], b"hello")
            self.assertEqual(send.call_args_list[1].args, ("key", "Return"))


if __name__ == "__main__":
    unittest.main()
