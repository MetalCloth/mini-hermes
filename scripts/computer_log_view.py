#!/usr/bin/env python3
"""Render computer JSONL traces as short, readable terminal events."""

import json
import re
import shlex
import sys
from pathlib import Path


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _short(value: object, limit: int = 240) -> str:
    lines = (
        line for line in ANSI.sub("", str(value)).splitlines()
        if "INFO using forced backend" not in line
    )
    text = " ".join(" ".join(lines).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _time(record: dict) -> str:
    value = record.get("time_utc", "")
    return f"{value[11:23]}Z" if isinstance(value, str) and len(value) >= 23 else "--:--:--Z"


def _elapsed(record: dict) -> str:
    value = record.get("elapsed_seconds")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return f" · {value:.1f}s"
    return ""


def _action(action: dict) -> str:
    kind = action.get("type", "unknown")
    if kind in {"click", "double_click", "right_click", "scroll"}:
        point = f"({action.get('x')}, {action.get('y')})"
        direction = f" {action.get('direction')}" if kind == "scroll" else ""
        return f"{kind.replace('_', ' ')}{direction} at {point}"
    if kind == "key":
        return f"press {action.get('key', '?')}"
    if kind == "type":
        return f"type {_short(action.get('text', ''), 100)!r}"
    if kind == "wait":
        return f"wait {action.get('seconds', '?')}s"
    return kind


def format_record(record: dict) -> str:
    """Format one trace event without dumping repeated prompt/image metadata."""
    stamp = _time(record)
    event = record.get("event", "unknown")

    if event == "computer_task_start":
        return f"{stamp}  TASK STARTED · {record.get('model', 'unknown model')}\n  Task: {_short(record.get('task', ''))}"
    if event == "screenshot_captured":
        return (
            f"{stamp}  SCREENSHOT · turn {record.get('turn', '?')} · "
            f"{record.get('width', '?')}×{record.get('height', '?')} · "
            f"{record.get('size_bytes', 0) // 1024} KiB"
        )
    if event == "model_request":
        image = record.get("screenshot", {})
        settings = [
            f"effort {record.get('reasoning_effort', 'default')}",
            f"speed {record.get('service_tier', 'default')}",
        ]
        timeout = record.get("socket_timeout_seconds")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            settings.append(f"socket timeout {timeout:g}s")
        return (
            f"{stamp}  MODEL INPUT · {record.get('model', 'unknown model')} · "
            f"screen {image.get('width', '?')}×{image.get('height', '?')} · "
            f"{image.get('size_bytes', 0) // 1024} KiB · " + " · ".join(settings)
        )
    if event == "model_response":
        text = record.get("text", "")
        tool_calls = record.get("tool_calls", [])
        if isinstance(tool_calls, list) and tool_calls:
            call = tool_calls[0]
            if isinstance(call, dict):
                arguments = call.get("arguments", {})
                if isinstance(arguments, dict) and "status" in arguments:
                    return (
                        f"{stamp}  MODEL RESPONSE{_elapsed(record)} · {arguments.get('status', '?')} · "
                        f"{_short(arguments.get('summary', ''))}"
                    )
                return (
                    f"{stamp}  MODEL RESPONSE{_elapsed(record)} · {call.get('name', 'function')} · "
                    f"{_short(json.dumps(arguments, ensure_ascii=False))}"
                )
        try:
            plan = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return f"{stamp}  MODEL RESPONSE{_elapsed(record)} · {_short(text)}"
        return (
            f"{stamp}  MODEL RESPONSE{_elapsed(record)} · "
            f"{plan.get('status', '?')} · {_short(plan.get('summary', ''))}"
        )
    if event == "validated_plan":
        plan = record.get("plan", {})
        lines = [
            f"{stamp}  PLAN · turn {record.get('turn', '?')} · {plan.get('status', '?')}",
            f"  {_short(plan.get('summary', ''))}",
        ]
        lines.extend(
            f"  {index}. {_action(action)}"
            for index, action in enumerate(plan.get("actions", []), 1)
        )
        if plan.get("requires_confirmation"):
            lines.append(f"  Approval: {_short(plan.get('confirmation_reason', 'required'))}")
        return "\n".join(lines)
    if event in {"action_start", "action_complete"}:
        label = "ACTION" if event == "action_start" else "ACTION DONE"
        return f"{stamp}  {label} · turn {record.get('turn', '?')} · {_short(record.get('label', ''))}"
    if event == "dotool_start":
        argv = list(record.get("argv", []))
        if argv:
            argv[0] = Path(argv[0]).name
        lines = [f"{stamp}  INPUT · {shlex.join(argv)}"]
        if record.get("stdin") is not None:
            lines.append(f"  Actions: {_short(record['stdin'], 240)!r}")
        return "\n".join(lines)
    if event == "dotool_result":
        code = record.get("returncode")
        if record.get("success", code == 0):
            return f"{stamp}  INPUT OK"
        details = record.get("stderr") or record.get("error") or record.get("stdout") or ""
        details = "\n".join(
            line.strip() for line in ANSI.sub("", str(details)).splitlines()
            if line.strip() and "INFO using forced backend" not in line
        )
        lines = [f"{stamp}  INPUT FAILED · exit {code if code is not None else 'unknown'}"]
        if details:
            lines.append(f"  {_short(details, 600)}")
        return "\n".join(lines)
    if event in {"computer_approval_expired", "computer_plan_discarded"}:
        detail = record.get("reason", record.get("phase", ""))
        label = "APPROVAL EXPIRED" if event == "computer_approval_expired" else "PLAN DISCARDED"
        return f"{stamp}  {label} · turn {record.get('turn', '?')} · {_short(detail)}"
    if event in {"desktop_recovery", "planning_error", "model_error"}:
        return (
            f"{stamp}  {event.replace('_', ' ').upper()}{_elapsed(record)} · "
            f"{_short(record.get('error', ''), 600)}"
        )
    if event in {"computer_task_done", "computer_task_finished"}:
        detail = record.get("summary", record.get("answer", ""))
        return f"{stamp}  TASK FINISHED · {_short(detail)}"
    if event in {"computer_task_failed", "computer_task_cancelled"}:
        label = "TASK FAILED" if event == "computer_task_failed" else "TASK CANCELLED"
        error = record.get("error", "")
        if record.get("error_type"):
            error = f"{record['error_type']}: {error}"
        return f"{stamp}  {label} · {_short(error, 500)}"

    fields = ", ".join(
        f"{key}={_short(value, 100)}"
        for key, value in record.items()
        if key not in {"time_utc", "event"}
    )
    return f"{stamp}  {event.replace('_', ' ').upper()} · {fields}".rstrip(" ·")


def main() -> None:
    for line in sys.stdin:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            print(f"INVALID JSON · {_short(line)}", flush=True)
            continue
        if isinstance(record, dict):
            print(format_record(record), flush=True)


if __name__ == "__main__":
    main()
