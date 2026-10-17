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
        legacy = result["historical_planner"]
        self.assertEqual(legacy["tasks"], {"started": 2, "reported_done": 1, "failed": 1})
        self.assertEqual(legacy["model_response_seconds"], {"count": 1, "median": 5.0, "p90": 5.0})
        self.assertEqual(legacy["accessibility_seconds"], {"count": 1, "median": 0.23, "p90": 0.23})
        self.assertEqual(legacy["accessibility_status"], {"available": 1})
        self.assertEqual(legacy["task_seconds"]["reported_done"]["median"], 8.0)
        self.assertEqual(legacy["model_calls"]["failed"]["median"], 1)
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))

    def test_current_trace_scorecard_counts_tools_images_accessibility_and_repeats(self):
        trace_id = "trace-123"
        records = [
            {"trace_id": trace_id, "time_utc": "2026-10-17T00:00:00+00:00",
             "event": "computer_diagnostic", "diagnostic_type": "turn_start"},
            {"trace_id": trace_id, "time_utc": "2026-10-17T00:00:01+00:00",
             "event": "computer_task_started", "task": "private task"},
            {"trace_id": trace_id, "time_utc": "2026-10-17T00:00:02+00:00",
             "event": "computer_diagnostic", "diagnostic_type": "model_request"},
            {"trace_id": trace_id, "event": "tool_call", "phase": "start",
             "name": "computer_observe", "arguments": {"text": "private prompt"}},
            {"trace_id": trace_id, "event": "tool_call", "phase": "start",
             "name": "computer_observe"},
            {"trace_id": trace_id, "event": "tool_call", "phase": "start",
             "name": "computer_act"},
            {"trace_id": trace_id, "event": "computer_observation", "scope": "active",
             "screenshot_included": True, "active_accessibility": True},
            {"trace_id": trace_id, "event": "computer_observation_detail",
             "model_control_count": 4, "tree_excerpt": "private UI text"},
            {"trace_id": trace_id, "event": "computer_diagnostic",
             "diagnostic_type": "model_response", "elapsed_ms": 5000},
            {"trace_id": trace_id, "event": "computer_diagnostic",
             "diagnostic_type": "tool_end", "tool_name": "computer_observe",
             "elapsed_ms": 300, "success": True},
            {"trace_id": trace_id, "event": "computer_diagnostic",
             "diagnostic_type": "turn_end", "elapsed_ms": 12000},
            {"trace_id": trace_id, "time_utc": "2026-10-17T00:00:12+00:00",
             "event": "computer_task_finished", "answer": "private answer"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
            result = summarize([path])

        run = result["runs"][0]
        self.assertEqual(run["status"], "finished")
        self.assertEqual(run["turn_seconds"], 12.0)
        self.assertEqual(run["computer_seconds"], 11.0)
        self.assertEqual(run["model_requests"], 1)
        self.assertEqual(run["model_response_ms"]["median"], 5000.0)
        self.assertEqual(run["tool_calls"], {"computer_act": 1, "computer_observe": 2})
        self.assertEqual(run["tool_time_ms"]["computer_observe"]["median"], 300.0)
        self.assertEqual(run["screenshots_attached"], 1)
        self.assertEqual(run["accessibility"], {
            "tree_outputs": 1, "tree_excerpts": 1, "controls_sent": 4, "active_summaries": 1,
        })
        self.assertEqual(run["repeated_observations"], {
            "extra_back_to_back_observe_calls": 1,
            "longest_back_to_back_observe_calls": 2,
            "progress_hints": 0,
        })
        self.assertEqual(result["totals"]["screenshots_attached"], 1)
        self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
