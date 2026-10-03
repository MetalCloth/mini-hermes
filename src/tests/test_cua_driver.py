"""CUA routing and desktop-loop ownership checks."""

import os
import unittest
from unittest.mock import patch

from src.computer import run_computer
from src.tools.computer_driver import HyprlandDriver, computer_driver_name
from src.tools.cua_driver import CuaDriver


class CuaDriverTests(unittest.TestCase):
    def test_default_and_dotool_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(computer_driver_name(), "cua")
        with patch.dict(os.environ, {"ORYN_COMPUTER_DRIVER": "dotool"}):
            self.assertEqual(computer_driver_name(), "dotool")

    def test_mapped_tree_and_action_routes(self):
        driver = CuaDriver.__new__(CuaDriver)
        driver.trace = None
        driver.width, driver.height = 1920, 1080
        driver.session = "test"
        driver._capture_id = "capture"
        driver._snapshot_stamp = ("window", 0, (81, 31), (1808, 1018))
        driver._window = {"pid": 123, "window_id": 456, "bounds": {"x": 81, "y": 31, "width": 1808, "height": 1018}}
        driver._tree = '- frame = "Test"\n    - label = "Result: empty"'
        driver._rows = [{"role": "entry", "label": "Test token input", "enabled": True,
                         "actions": ["activate"], "element_index": 12, "element_token": "token",
                         "frame": {"x": 20, "y": 53, "w": 1768, "h": 28}}]
        with patch.object(driver, "active_window_stamp", return_value=driver._snapshot_stamp), \
             patch.object(driver, "_call", return_value={"effect": "confirmed"}) as call, \
             patch.object(HyprlandDriver, "execute") as dotool:
            observation = driver.accessibility_observation()
            self.assertIn('Result: empty', observation["labels"])
            self.assertIn('[x=101, y=84, w=1768, h=28]', observation["labels"])
            self.assertIn('[12] entry:', observation["labels"])
            self.assertEqual(observation["element_ids"], [12])
            self.assertIsNotNone(driver.element_identity(12))
            driver.execute("click_element", {"element_index": 12})
            self.assertEqual(call.call_args.args[1]["element_token"], "token")
            self.assertEqual(call.call_args.args[1]["target"], {"kind": "window", "pid": 123, "window_id": 456})
            driver.execute("type", {"content": "EXACT"})
            dotool.assert_called_once_with("type", {"content": "EXACT"})
            driver.execute("click", {"point": (985, 140)})
            self.assertEqual(call.call_args.args[0], "click")
            self.assertEqual(call.call_args.args[1]["scope"], "desktop")
            self.assertEqual(call.call_args.args[1]["capture_id"], "capture")
            driver.execute("hotkey", {"key": "leftmeta"})
            self.assertEqual(dotool.call_count, 2)
            dotool.assert_any_call("hotkey", {"key": "leftmeta"})
            self.assertEqual(call.call_count, 2)

    def test_default_driver_closes_after_task(self):
        class FakeDriver:
            def __init__(self):
                self.closed = False

            def active_window(self):
                return "target"

            def screenshot(self):
                return b"image", (10, 10)

            def close(self):
                self.closed = True

        class FakeProvider:
            def next_plan(self, *_args, **_kwargs):
                return {"status": "done", "summary": "Complete", "question": "", "actions": [], "expected_result": "", "requires_confirmation": False, "confirmation_reason": ""}

        driver = FakeDriver()
        with patch.dict(os.environ, {"ORYN_COMPUTER_DRIVER": "cua"}), \
             patch("src.tools.cua_driver.CuaDriver", return_value=driver):
            self.assertIn("Complete", run_computer("Check", provider=FakeProvider()))
        self.assertTrue(driver.closed)


if __name__ == "__main__":
    unittest.main()
