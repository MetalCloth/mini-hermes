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
from src.providers.computer import COMPUTER_PLAN_TOOL, ComputerPlanner, InvalidComputerPlan
from src.providers.types import ModelResponse, ToolCall
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
        self.accessibility = []
        self.on_call = on_call

    def next_plan(self, task, screenshot, size, history, last_result, cancel_event=None, *, accessibility=""):
        self.calls.append((task, screenshot, size, list(history), last_result))
        self.accessibility.append(accessibility)
        if self.on_call:
            self.on_call()
        plan = self.plans.pop(0)
        if isinstance(plan, Exception):
            raise plan
        return copy.deepcopy(plan)


class ComputerTests(unittest.TestCase):
    def test_computer_loop_allows_30_model_calls(self):
        driver = FakeDriver()
        provider = FakeProvider(*[action_plan() for _ in range(30)])
        with patch("src.computer.threading.Event.wait", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "after 30 model calls"):
                run_computer("Keep going", driver=driver, provider=provider)
        self.assertEqual(len(provider.calls), 30)

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
        driver.monitor_rect = (0, 0, 1920, 1080)
        driver.desktop_bounds = (0, 0, 1920, 1080)

        with patch.object(driver, "_input") as send:
            driver.execute("click", {"point": (1440, 900)})

        send.assert_called_once_with(["mouseto 0.750000 0.833333", "click left"])

    def test_coordinates_include_selected_monitor_position_in_desktop_layout(self):
        driver = HyprlandDriver.__new__(HyprlandDriver)
        driver.width, driver.height = 1920, 1080
        driver.monitor_rect = (1920, 0, 1920, 1080)
        driver.desktop_bounds = (0, 0, 3840, 1080)

        self.assertEqual(driver._move((960, 540)), "mouseto 0.750000 0.500000")

    def test_monitor_layout_mapping_accounts_for_scaled_outputs(self):
        monitors = [
            {"id": 1, "name": "DP-1", "width": 3840, "height": 2160,
             "x": 0, "y": 0, "scale": 2, "focused": False, "disabled": False},
            {"id": 2, "name": "eDP-1", "width": 1920, "height": 1080,
             "x": 1920, "y": 0, "scale": 1, "focused": True, "disabled": False},
        ]
        driver = HyprlandDriver.__new__(HyprlandDriver)
        with (
            patch("src.tools.computer_driver.shutil.which", return_value="/usr/bin/tool"),
            patch("src.tools.computer_driver.subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0, json.dumps(monitors).encode(), b"")
            HyprlandDriver.__init__(driver)

        self.assertEqual(driver.output, "eDP-1")
        self.assertEqual(driver.monitor_rect, (1920, 0, 1920, 1080))
        self.assertEqual(driver.desktop_bounds, (0, 0, 3840, 1080))
        self.assertEqual(driver._move((960, 540)), "mouseto 0.750000 0.500000")

    def test_dotool_clicks_scrolls_and_types_as_stdin_action_streams(self):
        driver = HyprlandDriver.__new__(HyprlandDriver)
        driver.width, driver.height = 800, 450
        driver.monitor_rect = (0, 0, 800, 450)
        driver.desktop_bounds = (0, 0, 800, 450)

        with patch.object(driver, "_input") as send:
            driver.execute("left_double", {"point": (10, 20)})
            send.assert_called_once_with([
                "mouseto 0.012500 0.044444", "click left", "click left",
            ])
            send.reset_mock()
            for direction, command in (
                ("up", "wheel 3"), ("down", "wheel -3"),
                ("left", "hwheel 3"), ("right", "hwheel -3"),
            ):
                with self.subTest(direction=direction):
                    driver.execute("scroll", {"point": (400, 200), "direction": direction})
                    send.assert_called_once_with(["mouseto 0.500000 0.444444", command])
                    send.reset_mock()
            driver.execute("type", {"content": "first\nsecond\n"})
            send.assert_called_once_with(["type first", "key enter", "type second", "key enter"])

    def test_driver_identifies_only_window_on_selected_monitor(self):
        driver = HyprlandDriver.__new__(HyprlandDriver)
        driver.monitor_id = 2
        with patch("src.tools.computer_driver.subprocess.run") as command:
            command.return_value = subprocess.CompletedProcess([], 0, stdout=b'{"address":"0xabc","monitor":2}')
            self.assertEqual(driver.active_window(), "0xabc")
            command.return_value = subprocess.CompletedProcess(
                [], 0, stdout=b'{"address":"0xabc","monitor":2,"at":[81,31],"size":[800,450]}'
            )
            self.assertEqual(driver.active_window_stamp(), ("0xabc", 2, (81, 31), (800, 450)))
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
        parsed = parse_plan(action_plan([{"type": "key", "key": "x:Super_L"}]), (800, 450))
        self.assertEqual(parsed["actions"][0]["key"], "x:Super_L")

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
            action_plan([{"type": "click_element", "element_index": 3}]),
        ]
        for plan in invalid:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                parse_plan(plan, (800, 450))

    def test_numbered_click_requires_an_element_from_this_snapshot(self):
        action = {"type": "click_element", "element_index": 12}
        self.assertEqual(parse_plan(action_plan([action]), (800, 450), {12})["actions"], [action])
        for index in (13, True, -1):
            with self.subTest(index=index), self.assertRaises(ValueError):
                parse_plan(action_plan([{"type": "click_element", "element_index": index}]), (800, 450), {12})

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

    def test_invalid_plan_gets_one_fresh_screenshot_retry_without_partial_input(self):
        driver = FakeDriver()
        provider = FakeProvider(
            action_plan([{"type": "click", "x": 10, "y": 10}] * 4),
            action_plan([
                {"type": "key", "key": "ctrl+l"},
                {"type": "type", "text": "youtube.com"},
                {"type": "key", "key": "enter"},
            ]),
            done_plan(),
        )

        with patch("src.computer.threading.Event.wait", return_value=False):
            result = run_computer("Open YouTube", driver=driver, provider=provider)

        self.assertIn("complete", result)
        self.assertEqual(driver.captures, 3)
        self.assertEqual([item[0] for item in driver.actions], ["hotkey", "type", "hotkey"])
        self.assertIn("No desktop input was sent", provider.calls[1][4])
        self.assertIn("1–3 allowed actions", provider.calls[1][4])
        self.assertIn("no desktop input was sent", provider.calls[1][3][-1])

    def test_unstructured_text_gets_retried_without_sending_input(self):
        driver = FakeDriver()
        provider = FakeProvider(
            "We need click the first result. " + json.dumps(action_plan()),
            action_plan(),
            done_plan(),
        )

        with patch("src.computer.threading.Event.wait", return_value=False):
            result = run_computer("Click the first result", driver=driver, provider=provider)

        self.assertIn("complete", result)
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(len(driver.actions), 1)
        self.assertIn("No desktop input was sent", provider.calls[1][4])
        self.assertIn("call computer_plan exactly once", provider.calls[1][4])

    def test_missing_function_call_gets_one_retry_without_sending_input(self):
        driver = FakeDriver()
        provider = FakeProvider(
            InvalidComputerPlan("Expected exactly one computer_plan function call; received none."),
            action_plan(), done_plan(),
        )

        with patch("src.computer.threading.Event.wait", return_value=False):
            result = run_computer("Click the button", driver=driver, provider=provider)

        self.assertIn("complete", result)
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(len(driver.actions), 1)
        self.assertIn("call computer_plan exactly once", provider.calls[1][4])
        self.assertIn("no desktop input was sent", provider.calls[1][3][-1])

    def test_second_invalid_plan_stops_without_sending_desktop_input(self):
        driver = FakeDriver()
        invalid = action_plan([{"type": "click", "x": 10, "y": 10}] * 4)
        provider = FakeProvider(invalid, invalid)

        with patch("src.computer.threading.Event.wait", return_value=False):
            with self.assertRaisesRegex(ValueError, "1–3 actions"):
                run_computer("Open YouTube", driver=driver, provider=provider)

        self.assertEqual(driver.captures, 2)
        self.assertEqual(driver.actions, [])
        self.assertIn("No desktop input was sent", provider.calls[1][4])

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

    def test_window_move_while_model_plans_discards_pixel_plan(self):
        driver = FakeDriver()
        driver.position = (81, 31)
        driver.active_window_stamp = lambda: (driver.active, driver.position)
        provider = FakeProvider(
            action_plan(), done_plan(), on_call=lambda: setattr(driver, "position", (200, 31)),
        )
        with patch("src.computer.threading.Event.wait", return_value=False):
            run_computer("Click the button", driver=driver, provider=provider)
        self.assertEqual(driver.actions, [])
        self.assertEqual(driver.captures, 2)

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

    def test_accessibility_hints_share_existing_model_turn_and_fall_back_to_pixels(self):
        driver = FakeDriver()
        provider = FakeProvider(action_plan(), done_plan())
        observations = iter([
            {"status": "available", "labels": 'link: "Search result"', "count": 1},
            {"status": "sparse", "labels": "", "count": 0},
        ])
        with patch("src.computer.threading.Event.wait", return_value=False):
            run_computer(
                "Open the result", driver=driver, provider=provider,
                accessibility_reader=lambda _window, _size: next(observations),
            )
        self.assertEqual(provider.accessibility, ['link: "Search result"', ""])
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(len(driver.actions), 1)

    def test_numbered_control_flows_from_current_observation_to_driver(self):
        class SemanticDriver(FakeDriver):
            def accessibility_observation(self, _window, _size):
                return {"status": "available", "labels": '[12] entry: "Search"', "element_ids": [12]}

        driver = SemanticDriver()
        provider = FakeProvider(action_plan([{"type": "click_element", "element_index": 12}]), done_plan())
        with patch("src.computer.threading.Event.wait", return_value=False):
            run_computer("Focus search", driver=driver, provider=provider)
        self.assertEqual(driver.actions, [("click_element", {"element_index": 12})])
        self.assertIn('[12] entry:', provider.accessibility[0])

    def test_approved_numbered_control_expires_if_target_changes(self):
        class SemanticDriver(FakeDriver):
            def accessibility_observation(self, _window, _size):
                return {"status": "available", "labels": '[12] button: "Send"', "element_ids": [12]}

            def element_identity(self, _index):
                return ("Send", self.captures)

        driver = SemanticDriver()
        action = action_plan(
            [{"type": "click_element", "element_index": 12}],
            requires_confirmation=True, confirmation_reason="Send the message",
        )
        provider = FakeProvider(action, action, done_plan())
        with patch("src.computer.threading.Event.wait", return_value=False):
            run_computer("Send a message", driver=driver, provider=provider, confirm_action=lambda _: True)
        self.assertEqual(driver.actions, [])
        self.assertIn("approved action", provider.calls[2][4])

    def test_focus_change_during_accessibility_discards_both_observations(self):
        driver = FakeDriver()
        provider = FakeProvider(done_plan())
        calls = 0

        def observe(_window, _size):
            nonlocal calls
            calls += 1
            if calls == 1:
                driver.active = "other"
                return {"status": "available", "labels": 'button: "Old window"'}
            return {"status": "available", "labels": 'button: "New window"'}

        run_computer("Check the screen", driver=driver, provider=provider, accessibility_reader=observe)
        self.assertEqual(driver.captures, 2)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(driver.actions), 0)
        self.assertIn("New window", provider.accessibility[0])
        self.assertNotIn("Old window", provider.accessibility[0])

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

    def test_sixth_driver_error_stops_the_run(self):
        driver = FakeDriver(failures=6)
        provider = FakeProvider(*[action_plan() for _ in range(6)])
        with patch("src.computer.threading.Event.wait", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "failed again"):
                run_computer("Click the button", driver=driver, provider=provider)
        self.assertEqual(len(provider.calls), 6)

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
        plan = action_plan([{
            "type": "click", "element_index": None, "x": 120, "y": 80,
            "direction": None, "key": None, "text": None,
        }])
        expected = action_plan([{"type": "click", "x": 120, "y": 80}])
        screenshot = io.BytesIO()
        Image.new("RGB", (1920, 1080), "red").save(screenshot, format="JPEG")
        cancel = threading.Event()
        codex = CodexProvider("gpt-6-sol")
        with patch.object(codex, "complete", return_value=ModelResponse(
            "", [ToolCall("call-1", "computer_plan", plan)],
        )) as complete:
            result = ComputerPlanner(codex, driver_name="cua").next_plan(
                "Finish task", screenshot.getvalue(), (1920, 1080), [], "", cancel,
            )
        self.assertEqual(result, expected)
        self.assertEqual(codex.model, "gpt-6-sol")
        messages = complete.call_args.args[0]
        self.assertEqual(messages[0]["role"], "developer")
        self.assertIn("Call computer_plan exactly once", messages[0]["content"])
        self.assertNotIn("raw JSON object", messages[0]["content"])
        self.assertIn("Never return four or more actions", messages[0]["content"])
        self.assertIn("do not click the page first", messages[0]["content"])
        self.assertIn("Oryn cua operating guide:", messages[0]["content"])
        self.assertIn("Never use bare `super`", messages[0]["content"])
        self.assertIn("Caelestia Hyprland environment profile:", messages[0]["content"])
        self.assertIn("Tapping and releasing it by itself opens the Caelestia launcher.", messages[0]["content"])
        self.assertIn("`Super+W` opens Brave", messages[0]["content"])
        self.assertIn("`Super+F` focuses or isolates the current screen/window", messages[0]["content"])
        self.assertIn("`Super+Alt+F` fullscreen the active window", messages[0]["content"])
        self.assertNotIn("120 ms", messages[0]["content"])
        self.assertIn("The screenshot is 1920x1080; coordinates start at the top-left.", messages[0]["content"])
        self.assertIn("Finish task", messages[1]["content"])
        image = messages[1]["images"][0]
        self.assertEqual((image["mime_type"], image["width"], image["height"]), ("image/jpeg", 1920, 1080))
        self.assertTrue(base64.b64decode(image["base64_data"]).startswith(b"\xff\xd8"))
        self.assertIs(complete.call_args.kwargs["cancel_event"], cancel)
        self.assertEqual(complete.call_args.kwargs["forced_tool"], "computer_plan")
        self.assertEqual(complete.call_args.kwargs["tools"], [COMPUTER_PLAN_TOOL])

    def test_computer_planner_normalizes_numbered_control_action(self):
        raw = action_plan([{
            "type": "click_element", "element_index": 12, "x": None, "y": None,
            "direction": None, "key": None, "text": None,
        }])
        screenshot = io.BytesIO()
        Image.new("RGB", (8, 8), "black").save(screenshot, format="JPEG")
        codex = CodexProvider("gpt-6-sol")
        with patch.object(codex, "complete", return_value=ModelResponse(
            "", [ToolCall("call-1", "computer_plan", raw)],
        )):
            result = ComputerPlanner(codex, driver_name="cua").next_plan(
                "Open the control", screenshot.getvalue(), (8, 8), [], "",
                accessibility='[12] button: "Open"',
            )
        self.assertEqual(result["actions"], [{"type": "click_element", "element_index": 12}])

    def test_computer_planner_receives_session_chat_history(self):
        screenshot = io.BytesIO()
        Image.new("RGB", (8, 8), "black").save(screenshot, format="JPEG")
        codex = CodexProvider("gpt-6-sol")
        history = [
            {"role": "user", "content": "Open Spotify and play Winner Takes It All by ABBA."},
            {"role": "assistant", "content": "I opened Spotify."},
        ]
        with patch.object(codex, "complete", return_value=ModelResponse(
            "", [ToolCall("call-1", "computer_plan", done_plan())],
        )) as complete:
            planner = ComputerPlanner(codex, conversation_history=history)
            planner.next_plan("do it again", screenshot.getvalue(), (8, 8), [], "")

        messages = complete.call_args.args[0]
        self.assertEqual(messages[1]["content"], history[0]["content"])
        self.assertEqual(messages[2]["content"], history[1]["content"])
        self.assertIn("Earlier approvals never authorize a new action", messages[0]["content"])
        self.assertIn("User task:\ndo it again", messages[3]["content"])

    def test_computer_planner_rejects_text_only_wrong_or_multiple_calls(self):
        screenshot = io.BytesIO()
        Image.new("RGB", (8, 8), "black").save(screenshot, format="JPEG")
        codex = CodexProvider("gpt-6-sol")
        responses = [
            ModelResponse("I should click the first result."),
            ModelResponse("", [ToolCall("call-1", "other_tool", done_plan())]),
            ModelResponse("", [
                ToolCall("call-1", "computer_plan", done_plan()),
                ToolCall("call-2", "computer_plan", done_plan()),
            ]),
        ]
        for response in responses:
            with self.subTest(response=response), patch.object(codex, "complete", return_value=response):
                with self.assertRaises(InvalidComputerPlan):
                    ComputerPlanner(codex).next_plan(
                        "Click the first result", screenshot.getvalue(), (8, 8), [], "",
                    )


if __name__ == "__main__":
    unittest.main()
