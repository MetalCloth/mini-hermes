"""Bounded screenshot/action loop for local computer use."""

import os
import re
import threading
from time import monotonic
from typing import Callable

from src.computer_logging import ComputerTrace
from src.providers.computer import ComputerPlanner, InvalidComputerPlan
from src.tools.computer_accessibility import observe_active_window
from src.tools.computer_driver import HyprlandDriver, computer_driver_name


MAX_TURNS = 30
MAX_ACTIONS_PER_TURN = 3
MAX_SECONDS = 300
START_DELAY = 1
MAX_RECOVERIES = 5
MAX_PLAN_RECOVERIES = 1
MAX_OBSERVE_DELAY_SECONDS = 10
KEY = re.compile(r"[a-zA-Z0-9_:+ -]{1,60}\Z")
PLAN_FIELDS = {
    "status", "summary", "question", "actions", "expected_result",
    "requires_confirmation", "confirmation_reason", "observe_delay_seconds",
}


class ComputerCancelled(RuntimeError):
    pass


def _text(value: object, field: str, *, required: bool, limit: int = 500) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"The selected model returned an invalid {field}.")
    return value


def _action(action: object, size: tuple[int, int], element_ids: set[int]) -> dict:
    if not isinstance(action, dict) or not isinstance(action.get("type"), str):
        raise ValueError("The selected model returned an invalid desktop action.")
    name = action["type"]
    point_actions = {"click", "double_click", "right_click", "scroll"}
    expected = {
        "click": {"type", "x", "y"},
        "click_element": {"type", "element_index"},
        "double_click": {"type", "x", "y"},
        "right_click": {"type", "x", "y"},
        "scroll": {"type", "x", "y", "direction"},
        "key": {"type", "key"},
        "type": {"type", "text"},
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
    elif name == "click_element":
        index = action["element_index"]
        if type(index) is not int or index not in element_ids:
            raise ValueError("The selected model selected an element absent from the current snapshot.")
    elif name == "key":
        key = action["key"]
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise ValueError("The selected model returned an invalid key combination.")
        action["key"] = "+".join(key.replace("+", " ").split())
        if len(action["key"].split("+")) > 3:
            raise ValueError("The selected model returned too many keys in one combination.")
    elif name == "type":
        content = action["text"]
        if not isinstance(content, str) or not content or len(content) > 2000:
            raise ValueError("The selected model returned invalid text to type.")
    return action


def parse_plan(reply: dict, size: tuple[int, int], element_ids: set[int] | None = None) -> dict:
    """Validate structured function arguments before they can reach the local driver."""
    if not isinstance(reply, dict) or set(reply) not in (PLAN_FIELDS, PLAN_FIELDS - {"observe_delay_seconds"}):
        raise ValueError("The selected model returned an invalid plan format.")

    status = reply["status"]
    if not isinstance(status, str) or status not in {"actions", "observe_again", "ask_user", "done"}:
        raise ValueError("The selected model returned an unsupported plan status.")
    observe_delay = reply.get("observe_delay_seconds", 0)
    if type(observe_delay) is not int or not 0 <= observe_delay <= MAX_OBSERVE_DELAY_SECONDS:
        raise ValueError(f"observe_delay_seconds must be an integer from 0 to {MAX_OBSERVE_DELAY_SECONDS}.")
    plan = {
        "status": status,
        "summary": _text(reply["summary"], "summary", required=True),
        "question": _text(reply["question"], "question", required=False),
        "observe_delay_seconds": observe_delay,
        "expected_result": _text(reply["expected_result"], "expected result", required=False),
        "requires_confirmation": reply["requires_confirmation"],
        "confirmation_reason": _text(reply["confirmation_reason"], "confirmation reason", required=False),
        "actions": reply["actions"],
    }
    if type(plan["requires_confirmation"]) is not bool or not isinstance(plan["actions"], list):
        raise ValueError("The selected model returned invalid confirmation or action data.")
    if status == "actions":
        if observe_delay:
            raise ValueError("Action plans cannot request an observation delay.")
        if not 1 <= len(plan["actions"]) <= MAX_ACTIONS_PER_TURN:
            raise ValueError(f"The selected model must return 1–{MAX_ACTIONS_PER_TURN} actions per turn.")
        plan["actions"] = [_action(action, size, element_ids or set()) for action in plan["actions"]]
        if not plan["expected_result"].strip() or plan["question"]:
            raise ValueError("The selected model returned an incomplete action plan.")
        if plan["requires_confirmation"]:
            if len(plan["actions"]) != 1:
                raise ValueError("A confirmed plan must contain exactly one action.")
            if not plan["confirmation_reason"].strip():
                raise ValueError("A confirmed action needs a confirmation reason.")
        elif plan["confirmation_reason"]:
            raise ValueError("The selected model supplied a confirmation reason without requiring approval.")
    elif status == "observe_again":
        if (plan["actions"] or plan["question"] or plan["requires_confirmation"]
                or plan["expected_result"] or plan["confirmation_reason"]):
            raise ValueError("observe_again sends no desktop input and cannot request confirmation.")
    elif status == "ask_user":
        if (observe_delay or plan["actions"] or plan["requires_confirmation"] or not plan["question"].strip()
                or plan["expected_result"] or plan["confirmation_reason"]):
            raise ValueError("The selected model returned an incomplete clarification request.")
    elif (observe_delay or plan["actions"] or plan["question"] or plan["requires_confirmation"]
          or plan["expected_result"] or plan["confirmation_reason"]):
        raise ValueError("The selected model returned an invalid completion plan.")
    return plan


def _action_label(action: dict) -> str:
    name = action["type"]
    if name == "click_element":
        return f"click element [{action['element_index']}]"
    if name in {"click", "double_click", "right_click", "scroll"}:
        if name == "scroll":
            return f"scroll {action['direction']} at ({action['x']}, {action['y']})"
        return f"{name.replace('_', ' ')} at ({action['x']}, {action['y']})"
    if name == "key":
        return f"press {action['key']}"
    if name == "type":
        return f"type {action['text']!r}"
    raise ValueError("The selected model returned an unsupported desktop action.")


def _driver_action(action: dict) -> tuple[str, dict]:
    name = action["type"]
    if name == "click_element":
        return "click_element", {"element_index": action["element_index"]}
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
    if name == "type":
        return "type", {"content": action["text"]}
    raise ValueError("The selected model returned an unsupported desktop action.")


def _check_cancel(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise ComputerCancelled("Stopped by you.")


def _check_time(started: float) -> None:
    if monotonic() - started >= MAX_SECONDS:
        raise RuntimeError(f"Computer task stopped after {MAX_SECONDS} seconds; it may be incomplete.")


def _window_stamp(driver: HyprlandDriver):
    stamp = getattr(driver, "active_window_stamp", None)
    return stamp() if callable(stamp) else driver.active_window()


def run_computer(
    task: str, *, provider: ComputerPlanner, dry_run: bool = False,
    on_status: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    driver: HyprlandDriver | None = None,
    confirm_action: Callable[[str], bool] | None = None,
    ask_user: Callable[[str], str | None] | None = None,
    trace: ComputerTrace | None = None,
    accessibility_reader: Callable[[str | None, tuple[int, int]], dict] | None = None,
) -> str:
    if not task.strip():
        raise ValueError("Give /computer a task, for example /computer open Settings.")
    if driver is None:
        if computer_driver_name() == "cua":
            from src.tools.cua_driver import CuaDriver
            owned_driver = CuaDriver(trace)
        else:
            owned_driver = HyprlandDriver(trace)
        try:
            return run_computer(
                task, provider=provider, dry_run=dry_run, on_status=on_status,
                cancel_event=cancel_event, driver=owned_driver,
                confirm_action=confirm_action, ask_user=ask_user, trace=trace,
                accessibility_reader=accessibility_reader,
            )
        finally:
            close = getattr(owned_driver, "close", None)
            if callable(close):
                close()
    if trace and isinstance(driver, HyprlandDriver):
        driver.trace = trace
    if accessibility_reader is None:
        accessibility_reader = getattr(driver, "accessibility_observation", None)
    if (accessibility_reader is None and isinstance(driver, HyprlandDriver)
            and os.environ.get("ORYN_COMPUTER_ATSPI", "0").lower() in {"1", "true", "on"}):
        accessibility_reader = observe_active_window
    cancel_event = cancel_event or threading.Event()
    started = monotonic()
    history: list[str] = []
    action_trace: list[str] = []
    last_result = ""
    recoveries = 0
    plan_recoveries = 0
    pending_approval: dict | None = None
    pending_approval_window = None
    pending_approval_identity = None
    if trace:
        trace.write(
            "computer_task_start", task=task, dry_run=dry_run,
            driver=type(driver).__name__,
            model=provider.provider.model,
            reasoning_effort=provider.provider.reasoning_effort,
            service_tier=provider.provider.service_tier,
        )

    for turn in range(1, MAX_TURNS + 1):
        _check_cancel(cancel_event)
        _check_time(started)
        focused_stamp = _window_stamp(driver)
        focused_window = focused_stamp[0] if isinstance(focused_stamp, tuple) else focused_stamp
        if pending_approval is not None and focused_stamp != pending_approval_window:
            if trace:
                trace.write("computer_approval_expired", turn=turn, reason="window_or_geometry_changed")
            history.append(f"Turn {turn}: approval expired because the window changed or moved; no action was sent.")
            last_result = (
                "The approved action is no longer tied to the current window, so it was not sent. "
                "Recheck the current screenshot and ask again if approval is still needed."
            )
            pending_approval = None
            pending_approval_window = None
            pending_approval_identity = None
            continue
        screenshot, size = driver.screenshot()
        observed_at = monotonic()
        if trace:
            trace.write(
                "screenshot_captured", turn=turn, width=size[0], height=size[1],
                size_bytes=len(screenshot),
            )
        if _window_stamp(driver) != focused_stamp:
            if trace:
                trace.write("computer_plan_discarded", turn=turn, phase="screenshot")
            history.append(f"Turn {turn}: screenshot discarded because the window changed or moved; no input was sent.")
            last_result = "The window changed or moved during screenshot capture. Take a fresh screenshot before planning."
            continue
        accessibility = ""
        element_ids: set[int] = set()
        if accessibility_reader is not None:
            _check_cancel(cancel_event)
            try:
                observation = accessibility_reader(focused_window, size)
            except Exception:
                observation = {"status": "unavailable"}
            if isinstance(observation, dict):
                labels = observation.get("labels")
                if observation.get("status") == "available" and isinstance(labels, str):
                    accessibility = labels[:4000]
                    if accessibility_reader == getattr(driver, "accessibility_observation", None):
                        ids = observation.get("element_ids", [])
                        if isinstance(ids, list):
                            element_ids = {index for index in ids if type(index) is int}
                if trace:
                    trace.write(
                        "accessibility_observation", turn=turn,
                        status=observation.get("status", "unavailable"),
                        count=observation.get("count", 0),
                        coordinates=observation.get("coordinates", 0),
                        elapsed_ms=observation.get("elapsed_ms"),
                        characters=len(accessibility),
                    )
            if _window_stamp(driver) != focused_stamp:
                if trace:
                    trace.write("computer_plan_discarded", turn=turn, phase="accessibility")
                history.append(f"Turn {turn}: observation discarded because the window changed or moved; no input was sent.")
                last_result = "The window changed or moved during observation. Take a fresh screenshot before planning."
                continue
        if on_status:
            on_status(f"Computer model is reviewing the screen · turn {turn}/{MAX_TURNS}")
        invalid_plan: ValueError | None = None
        try:
            reply = provider.next_plan(
                task, screenshot, size, history, last_result, cancel_event,
                accessibility=accessibility,
            )
        except InterruptedError as exc:
            raise ComputerCancelled("Stopped by you.") from exc
        except InvalidComputerPlan as exc:
            invalid_plan = exc
        except Exception as exc:
            if trace:
                trace.write("planning_error", turn=turn, error_type=type(exc).__name__, error=str(exc))
            raise
        if invalid_plan is None:
            try:
                plan = parse_plan(reply, size, element_ids)
            except ValueError as exc:
                invalid_plan = exc
        if invalid_plan is not None:
            exc = invalid_plan
            if trace:
                trace.write(
                    "planning_error", turn=turn, error_type=type(exc).__name__,
                    error=str(exc), no_input_sent=True,
                )
            if plan_recoveries >= MAX_PLAN_RECOVERIES:
                raise exc
            plan_recoveries += 1
            history.append(f"Turn {turn}: Oryn rejected the invalid plan; no desktop input was sent.")
            last_result = (
                f"Oryn rejected your previous computer_plan call: {exc} No desktop input was sent. "
                "Recheck the fresh screenshot and call computer_plan exactly once with the full "
                "plan. Use 1–3 allowed actions, observe_again, or ask_user/done with an empty actions list."
            )
            continue
        if trace:
            trace.write("validated_plan", turn=turn, plan=plan)
        _check_cancel(cancel_event)
        _check_time(started)
        if _window_stamp(driver) != focused_stamp:
            if trace:
                trace.write("computer_plan_discarded", turn=turn, phase="model_planning")
            history.append(f"Turn {turn}: plan discarded because the window changed or moved; no input was sent.")
            last_result = "The window changed or moved while planning. Discard that plan, inspect the current screen, and replan."
            continue

        approved = pending_approval is not None
        if approved:
            identity = None
            if pending_approval["type"] == "click_element":
                get_identity = getattr(driver, "element_identity", None)
                if callable(get_identity):
                    identity = get_identity(pending_approval["element_index"])
            if (plan["status"] != "actions" or not plan["requires_confirmation"]
                    or plan["actions"] != [pending_approval]
                    or (pending_approval["type"] == "click_element"
                        and (identity is None or identity != pending_approval_identity))):
                if trace:
                    trace.write("computer_approval_expired", turn=turn, reason="plan_changed")
                history.append(f"Turn {turn}: approved action no longer matched the fresh plan; no input was sent.")
                last_result = "The fresh screen changed the approved action. Re-evaluate and request approval again if needed."
                pending_approval = None
                pending_approval_window = None
                pending_approval_identity = None
                continue
            pending_approval = None
            pending_approval_window = None
            pending_approval_identity = None

        if plan["status"] == "done":
            actions = "\n\nActions:\n" + "\n".join(action_trace) if action_trace else ""
            if trace:
                trace.write("computer_task_done", turn=turn, summary=plan["summary"])
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
            if on_status:
                on_status(f"Switch back to the target app now · continuing in {START_DELAY} second")
            if cancel_event.wait(START_DELAY):
                raise ComputerCancelled("Stopped by you.")
            continue

        if plan["status"] == "observe_again":
            if dry_run:
                if trace:
                    trace.write("dry_run_complete", turn=turn, observe_delay_seconds=plan["observe_delay_seconds"])
                return (
                    "Dry run: the selected model requested a fresh screenshot "
                    f"after at least {plan['observe_delay_seconds']} second(s) from this capture. "
                    "No desktop input was sent."
                )
            remaining = max(0.0, plan["observe_delay_seconds"] - (monotonic() - observed_at))
            if on_status:
                on_status("Computer model requested a fresh look at the desktop")
            if remaining and cancel_event.wait(remaining):
                raise ComputerCancelled("Stopped by you.")
            _check_cancel(cancel_event)
            _check_time(started)
            if trace:
                trace.write(
                    "computer_observe_again", turn=turn,
                    requested_delay_seconds=plan["observe_delay_seconds"],
                    waited_seconds=round(remaining, 3),
                )
            history.append(f"Turn {turn}: requested a fresh screenshot; no desktop input was sent.")
            last_result = "No desktop input was sent. Inspect this fresh screenshot before choosing another step."
            continue

        if dry_run:
            lines = [f"{index}. {_action_label(action)}" for index, action in enumerate(plan["actions"], 1)]
            preview = "\n".join(lines)
            if plan["requires_confirmation"]:
                preview += f"\nConfirmation needed: {plan['confirmation_reason']}"
            if trace:
                trace.write("dry_run_complete", turn=turn, actions=plan["actions"])
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
            pending_approval_window = focused_stamp
            if pending_approval["type"] == "click_element":
                get_identity = getattr(driver, "element_identity", None)
                pending_approval_identity = get_identity(pending_approval["element_index"]) if callable(get_identity) else None
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
        target_changed = False
        try:
            for action in plan["actions"]:
                _check_cancel(cancel_event)
                _check_time(started)
                if _window_stamp(driver) != focused_stamp:
                    target_changed = True
                    if trace:
                        trace.write("computer_plan_discarded", turn=turn, phase="before_action")
                    break
                label = _action_label(action)
                if trace:
                    trace.write("action_start", turn=turn, label=label, action=action)
                if on_status:
                    on_status(f"Computer action: {label[:120]}")
                name, args = _driver_action(action)
                driver.execute(name, args)
                if cancel_event.wait(0.15):
                    raise ComputerCancelled("Stopped by you.")
                if trace:
                    trace.write("action_complete", turn=turn, label=label)
                completed.append(label)
        except ComputerCancelled:
            raise
        except RuntimeError as exc:
            if trace:
                trace.write("desktop_recovery", turn=turn, recovery=recoveries + 1, error=str(exc))
            if recoveries >= MAX_RECOVERIES:
                raise RuntimeError(f"Desktop action failed again; computer task stopped: {exc}") from exc
            recoveries += 1
            action_trace.extend(f"Turn {turn} (partial): {label}" for label in completed)
            if on_status:
                on_status("Desktop action failed; the model is checking a fresh screenshot for one recovery")
            last_result = (
                f"The desktop driver failed after {len(completed)} action(s): {exc}. "
                "Some actions in the batch may already have happened. Use the new screenshot "
                "to re-evaluate before taking another action."
            )
            history.append(f"Turn {turn} failed after: {', '.join(completed) or '(no action completed)'}")
            continue

        if target_changed:
            action_trace.extend(f"Turn {turn} (partial): {label}" for label in completed)
            history.append(f"Turn {turn}: window changed or moved; discarded the remaining planned actions.")
            last_result = (
                "The window changed or moved before the action batch finished. Preserve completed actions, "
                "inspect the current screenshot, and replan the remaining work."
            )
            continue

        action_trace.extend(f"Turn {turn}: {label}" for label in completed)
        history.append(
            f"Turn {turn}: {plan['summary']}; driver completed input calls "
            f"{', '.join(completed)}; expected {plan['expected_result']}; "
            "visible effect not yet verified."
        )
        last_result = (
            f"The driver completed input calls for {', '.join(completed)}. "
            "This does not prove the intended UI change occurred. "
            f"Check the current screenshot for: {plan['expected_result']}"
        )

    raise RuntimeError(f"Computer task stopped after {MAX_TURNS} model calls; it may be incomplete.")
