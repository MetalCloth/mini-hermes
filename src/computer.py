"""Bounded screenshot/action loop for local computer use."""

import json
import re
import threading
from time import monotonic
from typing import Callable

from src.providers.computer import ComputerPlanner
from src.tools.computer_driver import HyprlandDriver


MAX_TURNS = 20
MAX_ACTIONS_PER_TURN = 3
MAX_SECONDS = 300
START_DELAY = 1
MAX_RECOVERIES = 1
KEY = re.compile(r"[a-zA-Z0-9_+ -]{1,60}\Z")
PLAN_FIELDS = {
    "status", "summary", "question", "actions", "expected_result",
    "requires_confirmation", "confirmation_reason",
}


class ComputerCancelled(RuntimeError):
    pass


class ComputerFocusChanged(RuntimeError):
    pass


def _text(value: object, field: str, *, required: bool, limit: int = 500) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"The selected model returned an invalid {field}.")
    return value


def _action(action: object, size: tuple[int, int]) -> dict:
    if not isinstance(action, dict) or not isinstance(action.get("type"), str):
        raise ValueError("The selected model returned an invalid desktop action.")
    name = action["type"]
    point_actions = {"click", "double_click", "right_click", "scroll"}
    expected = {
        "click": {"type", "x", "y"},
        "double_click": {"type", "x", "y"},
        "right_click": {"type", "x", "y"},
        "scroll": {"type", "x", "y", "direction"},
        "key": {"type", "key"},
        "type": {"type", "text"},
        "wait": {"type", "seconds"},
    }
    if name not in expected or set(action) != expected[name]:
        raise ValueError("The selected model returned an unsupported desktop action.")
    if name in point_actions:
        x, y = action["x"], action["y"]
        if (type(x) is not int or type(y) is not int or
                not 0 <= x < size[0] or not 0 <= y < size[1]):
            raise ValueError("The selected model returned coordinates outside the screenshot.")
        if name == "scroll" and (
            not isinstance(action["direction"], str)
            or action["direction"] not in {"up", "down", "left", "right"}
        ):
            raise ValueError("The selected model returned an invalid scroll direction.")
    elif name == "key":
        key = action["key"]
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise ValueError("The selected model returned an invalid key combination.")
        action["key"] = "+".join(key.lower().replace("+", " ").split())
        if len(action["key"].split("+")) > 3:
            raise ValueError("The selected model returned too many keys in one combination.")
    elif name == "type":
        content = action["text"]
        if not isinstance(content, str) or not content or len(content) > 2000:
            raise ValueError("The selected model returned invalid text to type.")
    elif name == "wait" and (type(action["seconds"]) is not int or not 1 <= action["seconds"] <= 5):
        raise ValueError("The selected model returned an invalid wait duration.")
    return action


def parse_plan(reply: str | dict, size: tuple[int, int]) -> dict:
    """Validate the model's JSON plan before it can reach the local driver."""
    if isinstance(reply, str):
        if len(reply) > 16_000:
            raise ValueError("The model's plan was too large.")
        try:
            reply = json.loads(reply)
        except json.JSONDecodeError as exc:
            raise ValueError("The selected model did not return a valid JSON plan.") from exc
    if not isinstance(reply, dict) or set(reply) != PLAN_FIELDS:
        raise ValueError("The selected model returned an invalid plan format.")

    status = reply["status"]
    if not isinstance(status, str) or status not in {"actions", "ask_user", "done"}:
        raise ValueError("The selected model returned an unsupported plan status.")
    plan = {
        "status": status,
        "summary": _text(reply["summary"], "summary", required=True),
        "question": _text(reply["question"], "question", required=False),
        "expected_result": _text(reply["expected_result"], "expected result", required=False),
        "requires_confirmation": reply["requires_confirmation"],
        "confirmation_reason": _text(reply["confirmation_reason"], "confirmation reason", required=False),
        "actions": reply["actions"],
    }
    if type(plan["requires_confirmation"]) is not bool or not isinstance(plan["actions"], list):
        raise ValueError("The selected model returned invalid confirmation or action data.")
    if status == "actions":
        if not 1 <= len(plan["actions"]) <= MAX_ACTIONS_PER_TURN:
            raise ValueError(f"The selected model must return 1–{MAX_ACTIONS_PER_TURN} actions per turn.")
        plan["actions"] = [_action(action, size) for action in plan["actions"]]
        if not plan["expected_result"].strip() or plan["question"]:
            raise ValueError("The selected model returned an incomplete action plan.")
        if plan["requires_confirmation"]:
            if len(plan["actions"]) != 1:
                raise ValueError("A confirmed plan must contain exactly one action.")
            if not plan["confirmation_reason"].strip():
                raise ValueError("A confirmed action needs a confirmation reason.")
        elif plan["confirmation_reason"]:
            raise ValueError("The selected model supplied a confirmation reason without requiring approval.")
    elif status == "ask_user":
        if (plan["actions"] or plan["requires_confirmation"] or not plan["question"].strip()
                or plan["expected_result"] or plan["confirmation_reason"]):
            raise ValueError("The selected model returned an incomplete clarification request.")
    elif (plan["actions"] or plan["question"] or plan["requires_confirmation"]
          or plan["expected_result"] or plan["confirmation_reason"]):
        raise ValueError("The selected model returned an invalid completion plan.")
    return plan


def _action_label(action: dict) -> str:
    name = action["type"]
    if name in {"click", "double_click", "right_click", "scroll"}:
        if name == "scroll":
            return f"scroll {action['direction']} at ({action['x']}, {action['y']})"
        return f"{name.replace('_', ' ')} at ({action['x']}, {action['y']})"
    if name == "key":
        return f"press {action['key']}"
    if name == "type":
        return f"type {action['text']!r}"
    return f"wait {action['seconds']} seconds"


def _driver_action(action: dict) -> tuple[str, dict]:
    name = action["type"]
    if name in {"click", "double_click", "right_click", "scroll"}:
        driver_name = {
            "click": "click", "double_click": "left_double",
            "right_click": "right_single", "scroll": "scroll",
        }[name]
        args = {"point": (action["x"], action["y"])}
        if name == "scroll":
            args["direction"] = action["direction"]
        return driver_name, args
    if name == "key":
        return "hotkey", {"key": action["key"]}
    return "type", {"content": action["text"]}


def _check_cancel(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise ComputerCancelled("Stopped by you.")


def _check_time(started: float) -> None:
    if monotonic() - started >= MAX_SECONDS:
        raise RuntimeError(f"Computer task stopped after {MAX_SECONDS} seconds; it may be incomplete.")


def run_computer(
    task: str, *, provider: ComputerPlanner, dry_run: bool = False,
    on_status: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    driver: HyprlandDriver | None = None,
    confirm_action: Callable[[str], bool] | None = None,
    ask_user: Callable[[str], str | None] | None = None,
) -> str:
    if not task.strip():
        raise ValueError("Give /computer a task, for example /computer open Settings.")
    driver = driver or HyprlandDriver()
    cancel_event = cancel_event or threading.Event()
    started = monotonic()
    history: list[str] = []
    trace: list[str] = []
    last_result = ""
    recoveries = 0
    pending_approval: dict | None = None
    return_to_window: str | None = None
    needs_return = False

    for turn in range(1, MAX_TURNS + 1):
        _check_cancel(cancel_event)
        _check_time(started)
        focused_window = driver.active_window()
        if needs_return and focused_window != return_to_window:
            raise ComputerFocusChanged("Switch back to the original target window before continuing; no desktop input was sent.")
        needs_return = False
        screenshot, size = driver.screenshot()
        if driver.active_window() != focused_window:
            raise ComputerFocusChanged("The active window changed during the screenshot; no desktop input was sent.")
        if on_status:
            on_status(f"Computer model is reviewing the screen · turn {turn}/{MAX_TURNS}")
        try:
            reply = provider.next_plan(task, screenshot, size, history, last_result, cancel_event)
        except InterruptedError as exc:
            raise ComputerCancelled("Stopped by you.") from exc
        plan = parse_plan(reply, size)
        _check_cancel(cancel_event)
        _check_time(started)
        if driver.active_window() != focused_window:
            raise ComputerFocusChanged("The active window changed while the model planned; no desktop input was sent.")

        approved = pending_approval is not None
        if approved:
            if (plan["status"] != "actions" or not plan["requires_confirmation"]
                    or plan["actions"] != [pending_approval]):
                raise ComputerFocusChanged("The approved action changed on the fresh screenshot; no desktop input was sent.")
            pending_approval = None

        if plan["status"] == "done":
            actions = "\n\nActions:\n" + "\n".join(trace) if trace else ""
            return f"Computer model reports: {plan['summary']}{actions}"
        if plan["status"] == "ask_user":
            if ask_user is None:
                raise RuntimeError(f"The selected model needs clarification: {plan['question']}")
            if on_status:
                on_status("The selected model needs a clarification")
            answer = ask_user(plan["question"])
            _check_cancel(cancel_event)
            if not isinstance(answer, str) or not answer.strip():
                raise ComputerCancelled("Clarification was cancelled; desktop task stopped.")
            history.append(f"User clarification: {answer[:1000]}")
            last_result = f"The user clarified: {answer[:1000]}"
            return_to_window = focused_window
            needs_return = True
            if on_status:
                on_status(f"Switch back to the target app now · continuing in {START_DELAY} second")
            if cancel_event.wait(START_DELAY):
                raise ComputerCancelled("Stopped by you.")
            continue

        if dry_run:
            lines = [f"{index}. {_action_label(action)}" for index, action in enumerate(plan["actions"], 1)]
            preview = "\n".join(lines)
            if plan["requires_confirmation"]:
                preview += f"\nConfirmation needed: {plan['confirmation_reason']}"
            return f"Dry run: the selected model proposed:\n{preview}\nExpected: {plan['expected_result']}\nNo desktop input was sent."

        if on_status:
            on_status(f"The selected model proposed {len(plan['actions'])} desktop action(s)")
        if plan["requires_confirmation"] and not approved:
            preview = (
                f"Why confirmation is needed: {plan['confirmation_reason']}\n"
                f"Action: {_action_label(plan['actions'][0])}\n"
                f"Plan: {plan['summary']}\n"
                f"Expected result: {plan['expected_result']}"
            )
            _check_cancel(cancel_event)
            if confirm_action is None or not confirm_action(preview):
                raise ComputerCancelled("Desktop action was not approved; task stopped.")
            _check_cancel(cancel_event)
            _check_time(started)
            pending_approval = plan["actions"][0]
            return_to_window = focused_window
            needs_return = True
            last_result = (
                "The user approved this exact action, but no desktop input was sent yet. "
                "Recheck the fresh screenshot. Return the same single action with confirmation "
                "if it is still appropriate; otherwise ask the user."
            )
            if on_status:
                on_status(f"Switch back to the target app now · continuing in {START_DELAY} second")
            if cancel_event.wait(START_DELAY):
                raise ComputerCancelled("Stopped by you.")
            continue

        completed: list[str] = []
        try:
            for action in plan["actions"]:
                _check_cancel(cancel_event)
                _check_time(started)
                if driver.active_window() != focused_window:
                    raise ComputerFocusChanged("The active window changed before a desktop action; remaining actions were stopped.")
                label = _action_label(action)
                if on_status:
                    on_status(f"Computer action: {label[:120]}")
                if action["type"] == "wait":
                    if cancel_event.wait(action["seconds"]):
                        raise ComputerCancelled("Stopped by you.")
                else:
                    name, args = _driver_action(action)
                    driver.execute(name, args, size)
                    if cancel_event.wait(0.35):
                        raise ComputerCancelled("Stopped by you.")
                completed.append(label)
        except (ComputerCancelled, ComputerFocusChanged):
            raise
        except RuntimeError as exc:
            if recoveries >= MAX_RECOVERIES:
                raise RuntimeError(f"Desktop action failed again; computer task stopped: {exc}") from exc
            recoveries += 1
            trace.extend(f"Turn {turn} (partial): {label}" for label in completed)
            if on_status:
                on_status("Desktop action failed; the model is checking a fresh screenshot for one recovery")
            last_result = (
                f"The desktop driver failed after {len(completed)} action(s): {exc}. "
                "Some actions in the batch may already have happened. Use the new screenshot "
                "to re-evaluate before taking another action."
            )
            history.append(f"Turn {turn} failed after: {', '.join(completed) or '(no action completed)'}")
            continue

        trace.extend(f"Turn {turn}: {label}" for label in completed)
        history.append(
            f"Turn {turn}: {plan['summary']}; executed {', '.join(completed)}; "
            f"expected {plan['expected_result']}."
        )
        last_result = f"Executed {', '.join(completed)}. Expected visible result: {plan['expected_result']}"

    raise RuntimeError(f"Computer task stopped after {MAX_TURNS} model calls; it may be incomplete.")
