"""Checks private JSONL computer traces and captured dotool failures."""

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
from src.providers.types import ModelResponse, ProviderRequestError
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
                        planner.next_plan("Open Brave", image.getvalue(), (800, 450), [], ""),
                        json.dumps(plan),
                    )
            finally:
                trace.close()

            request, response = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(request["event"], "model_request")
            self.assertIn("Do not use shell commands", request["developer_instructions"])
            self.assertIn("Open Brave", request["user_content"])
            self.assertEqual(request["socket_timeout_seconds"], provider.request_timeout_seconds)
            self.assertEqual(
                {key: request["screenshot"][key] for key in ("width", "height", "mime_type")},
                {"width": 800, "height": 450, "mime_type": "image/jpeg"},
            )
            self.assertIsInstance(request["screenshot"]["size_bytes"], int)
            self.assertEqual(response["event"], "model_response")
            self.assertEqual(response["text"], json.dumps(plan))
            self.assertGreaterEqual(response["elapsed_seconds"], 0)

    def test_planner_logs_elapsed_model_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "computer.jsonl"
            trace = ComputerTrace(path)
            image = io.BytesIO()
            Image.new("RGB", (8, 8), "black").save(image, format="JPEG")
            provider = CodexProvider("gpt-6-luna")
            try:
                with patch.object(
                    provider, "complete",
                    side_effect=ProviderRequestError("Codex timed out waiting for headers"),
                ):
                    with self.assertRaisesRegex(ProviderRequestError, "waiting for headers"):
                        ComputerPlanner(provider, trace).next_plan(
                            "Open Brave", image.getvalue(), (8, 8), [], "",
                        )
            finally:
                trace.close()

            request, error = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(request["event"], "model_request")
            self.assertEqual(error["event"], "model_error")
            self.assertGreaterEqual(error["elapsed_seconds"], 0)
            self.assertIn("waiting for headers", error["error"])

    def test_driver_logs_exact_dotool_action_stream_and_failure_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "computer.jsonl"
            trace = ComputerTrace(path)
            driver = HyprlandDriver.__new__(HyprlandDriver)
            driver.trace = trace
            driver.dotool = "/usr/bin/dotool"
            try:
                with patch("src.tools.computer_driver.subprocess.run") as run:
                    run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
                    driver._input(["type hello"])
                    run.return_value = subprocess.CompletedProcess(
                        [], 0, b"", b"dotool: WARNING: impossible key for layout: windows",
                    )
                    with self.assertRaisesRegex(RuntimeError, "impossible key for layout"):
                        driver._input(["key windows"])
            finally:
                trace.close()

            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[0]["event"], "dotool_start")
            self.assertEqual(records[0]["argv"], ["/usr/bin/dotool"])
            self.assertEqual(records[0]["stdin"], "type hello\n")
            self.assertEqual(records[-1]["stderr"], "dotool: WARNING: impossible key for layout: windows")
            self.assertFalse(records[-1]["success"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_driver_types_multiline_text_and_enter_as_one_action_stream(self):
        driver = HyprlandDriver.__new__(HyprlandDriver)
        with patch.object(driver, "_input") as send:
            driver.execute("type", {"content": "hello"})
            send.assert_called_once_with(["type hello"])
            send.reset_mock()
            driver.execute("type", {"content": "hello\n"})
            send.assert_called_once_with(["type hello", "key enter"])
            send.reset_mock()
            driver.execute("type", {"content": "hello\nworld"})
            send.assert_called_once_with(["type hello", "key enter", "type world"])


if __name__ == "__main__":
    unittest.main()
