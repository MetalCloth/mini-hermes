"""The computer benchmark counts events without exposing trace payloads."""

import json
from pathlib import Path
import tempfile
import unittest

from scripts.computer_benchmark import _stats, summarize


class ComputerBenchmarkTests(unittest.TestCase):
    def test_p90_uses_nearest_rank(self):
        self.assertEqual(_stats([1, 2, 3, 4, 5, 6])["p90"], 6)

    def test_task_and_model_timings_from_multiple_runs_in_one_trace(self):
        records = [
            {"time_utc": "2026-10-02T00:00:00+00:00", "event": "computer_task_start", "task": "secret"},
            {"time_utc": "2026-10-02T00:00:01+00:00", "event": "model_request", "developer_instructions": "private"},
            {"time_utc": "2026-10-02T00:00:02+00:00", "event": "accessibility_observation", "status": "available", "elapsed_ms": 230, "characters": 100},
            {"time_utc": "2026-10-02T00:00:06+00:00", "event": "model_response", "text": "private"},
            {"time_utc": "2026-10-02T00:00:08+00:00", "event": "computer_task_done"},
            {"time_utc": "2026-10-02T00:01:00+00:00", "event": "computer_task_start"},
            {"time_utc": "2026-10-02T00:01:02+00:00", "event": "model_request"},
            {"time_utc": "2026-10-02T00:01:04+00:00", "event": "model_error"},
            {"time_utc": "2026-10-02T00:01:07+00:00", "event": "computer_task_failed"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
            result = summarize([path])
        self.assertEqual(result["tasks"], {"started": 2, "reported_done": 1, "failed": 1})
        self.assertEqual(result["model_response_seconds"], {"count": 1, "median": 5.0, "p90": 5.0})
        self.assertEqual(result["accessibility_seconds"], {"count": 1, "median": 0.23, "p90": 0.23})
        self.assertEqual(result["accessibility_status"], {"available": 1})
        self.assertEqual(result["task_seconds"]["reported_done"]["median"], 8.0)
        self.assertEqual(result["model_calls"]["failed"]["median"], 1)
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
