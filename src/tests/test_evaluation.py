import unittest

from src.evaluation.run import run_evaluations


class OfflineEvaluationTests(unittest.TestCase):
    def test_fixed_tasks_use_no_network_and_cover_tools_failures_and_context(self):
        report = run_evaluations()
        self.assertEqual(report["suite"], "oryn-offline-v2")
        self.assertFalse(report["network_or_credentials"])
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["passed"], 6)
        self.assertEqual([task["name"] for task in report["tasks"]], [
            "single-response", "read-project-file", "denied-write", "provider-failure",
            "interrupted-tool-cycle", "current-turn-context-limit",
        ])
        self.assertEqual(report["tasks"][1]["tool_calls"], 1)
        self.assertTrue(report["tasks"][2]["checks"]["files"])
        self.assertEqual(report["tasks"][3]["error_class"], "ProviderRequestError")
        self.assertEqual(report["tasks"][4]["error_class"], "TurnCancelled")
        self.assertTrue(report["tasks"][4]["checks"]["partial_history"])
        self.assertEqual(report["tasks"][5]["model_requests"], 0)
        self.assertTrue(report["tasks"][5]["checks"]["expected_error"])


if __name__ == "__main__":
    unittest.main()
