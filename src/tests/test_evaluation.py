import unittest

from src.evaluation.run import run_evaluations


class OfflineEvaluationTests(unittest.TestCase):
    def test_fixed_tasks_use_no_network_and_check_tools_and_denials(self):
        report = run_evaluations()
        self.assertEqual(report["suite"], "oryn-offline-v1")
        self.assertFalse(report["network_or_credentials"])
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["passed"], 3)
        self.assertEqual([task["name"] for task in report["tasks"]], [
            "single-response", "read-project-file", "denied-write",
        ])
        self.assertEqual(report["tasks"][1]["tool_calls"], 1)
        self.assertTrue(report["tasks"][2]["checks"]["files"])


if __name__ == "__main__":
    unittest.main()
