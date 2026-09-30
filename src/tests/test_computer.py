"""Checks the selected-model plan boundary and bounded local computer loop."""

import base64
import copy
import io
import json
import subprocess
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from src.computer import ComputerCancelled, parse_plan, run_computer
from src.providers.codex import CodexProvider
from src.providers.computer import ComputerPlanner
from src.providers.types import ModelResponse
from src.tools.computer_driver import HyprlandDriver


def action_plan(actions=None, *, requires_confirmation=False, confirmation_reason=""):
    return {
        "status": "actions",
        "summary": "Use the visible controls",
        "question": "",
        "actions": actions or [{"type": "click", "x": 400, "y": 200}],
        "expected_result": "The selected page opens",
        "requires_confirmation": requires_confirmation,
        "confirmation_reason": confirmation_reason,
    }


def done_plan(summary="The task is complete"):
    return {
        "status": "done", "summary": summary, "question": "", "actions": [],
        "expected_result": "", "requires_confirmation": False, "confirmation_reason": "",
    }


class FakeDriver:
    def __init__(self, failures=0, change_after_action=False):
        self.captures = 0
        self.actions = []
        self.failures = failures
        self.active = "target"
        self.change_after_action = change_after_action

    def active_window(self):
        return self.active

    def screenshot(self):
        self.captures += 1
        return b"fake screenshot", (800, 450)

    def execute(self, name, args):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("input failed")
        self.actions.append((name, args))
        if self.change_after_action:
            self.active = "other"


class FakeProvider:
    def __init__(self, *plans, on_call=None):
        self.plans = list(plans)
        self.calls = []
        self.on_call = on_call

    def next_plan(self, task, screenshot, size, history, last_result, cancel_event=None):
        self.calls.append((task, screenshot, size, list(history), last_result))
        if self.on_call:
            self.on_call()
        return copy.deepcopy(self.plans.pop(0))


class ComputerTests(unittest.TestCase):
    def test_screenshot_preserves_native_monitor_resolution(self):
        driver = HyprlandDriver.__new__(HyprlandDriver)
        driver.output = "eDP-1"
        driver.width, driver.height = 1920, 1080
        raw = io.BytesIO()
        Image.new("RGB", (1920, 1080), "red").save(raw, format="PNG")

        with patch("src.tools.computer_driver.subprocess.run") as command:
            command.return_value = subprocess.CompletedProcess([], 0, raw.getvalue(), b"")
            screenshot, size = driver.screenshot()

        self.assertEqual(size, (1920, 1080))
        with Image.open(io.BytesIO(screenshot)) as image:
            self.assertEqual(image.size, (1920, 1080))

    def test_full_resolution_click_coordinates_map_to_native_monitor(self):
        driver = HyprlandDriver.__new__(HyprlandDriver)
        driver.output = "eDP-1"
        driver.width, driver.height = 1920, 1080

        with patch.object(driver, "_input") as send:
            driver.execute("click", {"point": (1440, 900)})

        self.assertEqual(send.call_args_list[0].args, ("mousemove", "--output", "eDP-1", "1440", "900"))
        self.assertEqual(send.call_args_list[1].args, ("click", "1"))

    def test_driver_identifies_only_window_on_selected_monitor(self):
        driver = HyprlandDriver.__new__(HyprlandDriver)
        driver.monitor_id = 2
        with patch("src.tools.computer_driver.subprocess.run") as command:
            command.return_value = subprocess.CompletedProcess([], 0, stdout=b'{"address":"0xabc","monitor":2}')
            self.assertEqual(driver.active_window(), "0xabc")
            command.return_value = subprocess.CompletedProcess([], 0, stdout=b'{"address":"0xdef","monitor":3}')
            with self.assertRaisesRegex(RuntimeError, "selected monitor"):
                driver.active_window()

    def test_plan_parser_accepts_bounded_gui_actions(self):
        parsed = parse_plan({
            "status": "actions", "summary": "Search the site", "question": "",
            "actions": [
                {"type": "key", "key": "ctrl l"},
                {"type": "type", "text": "example query"},
                {"type": "key", "key": " Super_L "},
            ], "expected_result": "Search results appear", "requires_confirmation": False,
            "confirmation_reason": "",
        }, (800, 450))
        self.assertEqual(parsed["actions"][0]["key"], "ctrl+l")
        self.assertEqual(parsed["actions"][2]["key"], "Super_L")
        self.assertEqual(len(parsed["actions"]), 3)

    def test_plan_parser_rejects_invalid_or_out_of_bounds_actions(self):
        invalid = [
            action_plan([{"type": "click", "x": 800, "y": 10}]),
            action_plan([{"type": "click", "x": True, "y": 10}]),
            action_plan([{"type": "shell", "command": "echo unsafe"}]),
            action_plan([{"type": "click", "x": 1, "y": 2, "extra": "ignored"}]),
            action_plan([{"type": "scroll", "x": 1, "y": 2, "direction": []}]),
            action_plan([{"type": "type", "text": ""}]),
            action_plan([{"type": "wait", "seconds": 3}]),
            action_plan([{"type": "click", "x": 1, "y": 2}] * 4),
            action_plan([{"type": "click", "x": 1, "y": 2}], requires_confirmation=True),
        ]
        for plan in invalid:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                parse_plan(plan, (800, 450))

    def test_plan_parser_requires_confirmation_reason_and_single_action(self):
        plan = action_plan(requires_confirmation=True, confirmation_reason="Send this message")
        self.assertTrue(parse_plan(plan, (800, 450))["requires_confirmation"])
        plan["confirmation_reason"] = ""
        with self.assertRaisesRegex(ValueError, "confirmation reason"):
            parse_plan(plan, (800, 450))

    def test_dry_run_previews_batch_without_desktop_input(self):
        driver = FakeDriver()
        provider = FakeProvider(action_plan([
            {"type": "key", "key": "ctrl+l"},
            {"type": "type", "text": "example.com"},
        ]))
        result = run_computer("Search example.com", driver=driver, provider=provider, dry_run=True)
        self.assertIn("1. press ctrl+l", result)
        self.assertIn("2. type 'example.com'", result)
        self.assertIn("No desktop input was sent", result)
        self.assertEqual(driver.actions, [])
        self.assertEqual(driver.captures, 1)

    def test_loop_executes_small_batch_then_reobserves_and_verifies(self):
        driver = FakeDriver()
        provider = FakeProvider(
            action_plan([
                {"type": "key", "key": "ctrl+l"},
                {"type": "type", "text": "example.com"},
            ]),
            done_plan("The browser shows example.com"),
        )
        with patch("src.computer.threading.Event.wait", return_value=False):
            result = run_computer("Open example.com", driver=driver, provider=provider)
        self.assertIn("The browser shows example.com", result)
        self.assertEqual(driver.captures, 2)
        self.assertEqual([item[0] for item in driver.actions], ["hotkey", "type"])
        self.assertIn("Expected visible result", provider.calls[1][4])
        self.assertIn("executed press ctrl+l, type 'example.com'", provider.calls[1][3][0])

    def test_model_can_ask_user_then_continue_with_answer(self):
        driver = FakeDriver()
        provider = FakeProvider(
            {
                "status": "ask_user", "summary": "Need a target", "question": "Which account?",
                "actions": [], "expected_result": "", "requires_confirmation": False,
                "confirmation_reason": "",
            },
            action_plan(), done_plan(),
        )
        answers = []
        with patch("src.computer.threading.Event.wait", return_value=False):
            run_computer(
                "Open the account", driver=driver, provider=provider,
                ask_user=lambda question: answers.append(question) or "Work account",
            )
        self.assertEqual(answers, ["Which account?"])
        self.assertIn("Work account", provider.calls[1][4])
        self.assertEqual(len(driver.actions), 1)

    def test_unapproved_sensitive_action_never_reaches_driver(self):
        driver = FakeDriver()
        provider = FakeProvider(action_plan(
            [{"type": "click", "x": 400, "y": 200}],
            requires_confirmation=True, confirmation_reason="Send this message",
        ))
        previews = []
        with self.assertRaises(ComputerCancelled):
            run_computer(
                "Send a message", driver=driver, provider=provider,
                confirm_action=lambda preview: previews.append(preview) or False,
            )
        self.assertIn("Send this message", previews[0])
        self.assertEqual(driver.actions, [])

    def test_approved_sensitive_action_runs_only_after_approval(self):
        driver = FakeDriver()
        sensitive = action_plan(
            [{"type": "click", "x": 400, "y": 200}],
            requires_confirmation=True, confirmation_reason="Send this message",
        )
        provider = FakeProvider(
            sensitive, sensitive,
            done_plan(),
        )
        previews = []
        with patch("src.computer.threading.Event.wait", return_value=False):
            run_computer(
                "Send a message", driver=driver, provider=provider,
                confirm_action=lambda preview: previews.append(preview) or True,
            )
        self.assertEqual(len(previews), 1)
        self.assertEqual(len(driver.actions), 1)
        self.assertEqual(driver.captures, 3)
        self.assertIn("no desktop input was sent yet", provider.calls[1][4])

    def test_focus_change_while_model_plans_discards_plan_and_reobserves(self):
        driver = FakeDriver()
        provider = FakeProvider(
            action_plan(), done_plan(), on_call=lambda: setattr(driver, "active", "other"),
        )
        with patch("src.computer.threading.Event.wait", return_value=False):
            result = run_computer("Click the button", driver=driver, provider=provider)
        self.assertIn("complete", result)
        self.assertEqual(driver.actions, [])
        self.assertEqual(driver.captures, 2)
        self.assertIn("Discard that plan", provider.calls[1][4])

    def test_focus_change_during_screenshot_discards_that_capture(self):
        driver = FakeDriver()
        screenshot = driver.screenshot

        def switch_after_first_capture():
            result = screenshot()
            if driver.captures == 1:
                driver.active = "other"
            return result

        driver.screenshot = switch_after_first_capture
        provider = FakeProvider(done_plan())
        result = run_computer("Check the screen", driver=driver, provider=provider)
        self.assertIn("complete", result)
        self.assertEqual(driver.captures, 2)
        self.assertEqual(len(provider.calls), 1)

    def test_focus_change_during_batch_discards_remaining_actions_and_replans(self):
        driver = FakeDriver(change_after_action=True)
        provider = FakeProvider(action_plan([
            {"type": "click", "x": 400, "y": 200},
            {"type": "type", "text": "hello"},
        ]), done_plan())
        with patch("src.computer.threading.Event.wait", return_value=False):
            result = run_computer("Fill the form", driver=driver, provider=provider)
        self.assertIn("complete", result)
        self.assertEqual(len(driver.actions), 1)
        self.assertEqual(driver.captures, 2)
        self.assertIn("before the action batch finished", provider.calls[1][4])

    def test_approval_expires_if_focus_changes(self):
        driver = FakeDriver()
        sensitive = action_plan(requires_confirmation=True, confirmation_reason="Send this message")
        provider = FakeProvider(sensitive, done_plan())

        def approve(_preview):
            driver.active = "oryn"
            return True

        with patch("src.computer.threading.Event.wait", return_value=False):
            run_computer("Send a message", driver=driver, provider=provider, confirm_action=approve)
        self.assertEqual(driver.actions, [])
        self.assertIn("it was not sent", provider.calls[1][4])

    def test_changed_approved_action_expires_and_replans(self):
        driver = FakeDriver()
        sensitive = action_plan(requires_confirmation=True, confirmation_reason="Send this message")
        different = action_plan(
            [{"type": "click", "x": 401, "y": 200}],
            requires_confirmation=True, confirmation_reason="Send this message",
        )
        provider = FakeProvider(sensitive, different, done_plan())
        with patch("src.computer.threading.Event.wait", return_value=False):
            run_computer("Send a message", driver=driver, provider=provider, confirm_action=lambda _: True)
        self.assertEqual(driver.actions, [])
        self.assertIn("changed the approved action", provider.calls[2][4])

    def test_driver_error_gets_one_screenshot_guided_recovery(self):
        driver = FakeDriver(failures=1)
        provider = FakeProvider(action_plan(), action_plan(), done_plan())
        with patch("src.computer.threading.Event.wait", return_value=False):
            result = run_computer("Click the button", driver=driver, provider=provider)
        self.assertIn("complete", result)
        self.assertEqual(driver.captures, 3)
        self.assertEqual(len(driver.actions), 1)
        self.assertIn("driver failed", provider.calls[1][4])

    def test_second_driver_error_stops_the_run(self):
        driver = FakeDriver(failures=2)
        provider = FakeProvider(action_plan(), action_plan())
        with patch("src.computer.threading.Event.wait", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "failed again"):
                run_computer("Click the button", driver=driver, provider=provider)

    def test_cancelled_run_does_not_capture_or_act(self):
        cancel = threading.Event()
        cancel.set()
        driver = FakeDriver()
        provider = FakeProvider(action_plan())
        with self.assertRaises(ComputerCancelled):
            run_computer("Click", driver=driver, provider=provider, cancel_event=cancel)
        self.assertEqual(driver.captures, 0)
        self.assertEqual(driver.actions, [])

    def test_computer_planner_uses_selected_codex_model_and_image_path(self):
        plan = done_plan()
        screenshot = io.BytesIO()
        Image.new("RGB", (1920, 1080), "red").save(screenshot, format="JPEG")
        cancel = threading.Event()
        codex = CodexProvider("gpt-6-sol")
        with patch.object(codex, "complete", return_value=ModelResponse(json.dumps(plan), [])) as complete:
            result = ComputerPlanner(codex).next_plan(
                "Finish task", screenshot.getvalue(), (1920, 1080), [], "", cancel,
            )
        self.assertEqual(result, plan)
        self.assertEqual(codex.model, "gpt-6-sol")
        messages = complete.call_args.args[0]
        self.assertEqual(messages[0]["role"], "developer")
        self.assertIn("Return one JSON object", messages[0]["content"])
        self.assertIn("Oryn wdotool operating guide:", messages[0]["content"])
        self.assertIn("`Super_L` and `super_l` are not interchangeable", messages[0]["content"])
        self.assertNotIn("120 ms", messages[0]["content"])
        self.assertIn("The screenshot is 1920x1080; coordinates start at the top-left.", messages[0]["content"])
        self.assertIn("Finish task", messages[1]["content"])
        image = messages[1]["images"][0]
        self.assertEqual((image["mime_type"], image["width"], image["height"]), ("image/jpeg", 1920, 1080))
        self.assertTrue(base64.b64decode(image["base64_data"]).startswith(b"\xff\xd8"))
        self.assertIs(complete.call_args.kwargs["cancel_event"], cancel)


if __name__ == "__main__":
    unittest.main()
