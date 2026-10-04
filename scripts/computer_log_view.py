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

    if event in {"computer_task_start", "computer_task_started"}:
        driver = f" · {record['driver']}" if record.get("driver") else ""
        return f"{stamp}  TASK STARTED · {record.get('model', 'unknown model')}{driver}\n  Task: {_short(record.get('task', ''))}"
    if event == "computer_tool":
        phase = record.get("phase", "?")
        name = record.get("name", "?")
        detail = record.get("arguments") if phase == "start" else record.get("result")
        return f"{stamp}  TOOL {str(phase).upper()} · {name} · {_short(detail, 180)}"
    if event == "computer_diagnostic":
        kind = record.get("diagnostic_type", "?")
        if kind == "model_request":
            return (f"{stamp}  MODEL REQUEST · round {record.get('round', '?')} · "
                    f"~{record.get('estimated_tokens', '?')} tokens · {record.get('tool_count', '?')} tools")
        if kind in {"tool_start", "tool_end"}:
            return f"{stamp}  {kind.replace('_', ' ').upper()} · {record.get('tool_name', '?')}"
        return f"{stamp}  {str(kind).replace('_', ' ').upper()} · {_short(record.get('error_class', ''), 180)}"
    if event == "computer_observation":
        scope = record.get("scope", "?")
        image_scope = record.get("image_scope")
        image_label = f" · image {image_scope}" if image_scope else ""
        elements = f" · {record['element_count']} controls" if "element_count" in record else ""
        return (f"{stamp}  OBSERVE · {scope}{image_label} · "
                f"{record.get('width', '?')}×{record.get('height', '?')}{elements}")
    if event == "computer_action_start":
        return f"{stamp}  ACTION · {_action(record.get('action', {}))}"
    if event == "computer_action_result":
        outcome = record.get("outcome", {})
        route = f" · {outcome['route']}" if isinstance(outcome, dict) and outcome.get("route") else ""
        effect = outcome.get("effect", "?") if isinstance(outcome, dict) else "?"
        return f"{stamp}  ACTION RESULT · {effect}{route}"
    if event == "computer_verify":
        result = record.get("result", {})
        return f"{stamp}  VERIFY · {_short(result, 180)}"
    if event == "screenshot_captured":
        return (
            f"{stamp}  SCREENSHOT · turn {record.get('turn', '?')} · "
            f"{record.get('width', '?')}×{record.get('height', '?')} · "
            f"{record.get('size_bytes', 0) // 1024} KiB"
        )
    if event == "accessibility_observation":
        duration = record.get("elapsed_ms")
        timing = f" · {duration:.0f}ms" if isinstance(duration, (int, float)) else ""
        return (
            f"{stamp}  ACCESSIBILITY · turn {record.get('turn', '?')} · "
            f"{record.get('status', 'unavailable')} · {record.get('count', 0)} labels · "
            f"{record.get('coordinates', 0)} boxes{timing}"
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
    if event == "cua_call_start":
        return f"{stamp}  CUA · {record.get('tool', '?')}"
    if event == "cua_call_result":
        label = "CUA OK" if record.get("success") else "CUA FAILED"
        route = f" · {record['route']}" if record.get("route") else ""
        effect = f" · {record['effect']}" if record.get("effect") else ""
        duration = f" · {record['elapsed_ms']}ms" if record.get("elapsed_ms") is not None else ""
        error = f"\n  {_short(record['error'], 500)}" if record.get("error") else ""
        return f"{stamp}  {label} · {record.get('tool', '?')}{route}{effect}{duration}{error}"
    if event == "cua_dotool_fallback":
        return f"{stamp}  CUA FALLBACK · dotool {record.get('action', '?')}"
    if event == "cua_accessibility_unavailable":
        return f"{stamp}  CUA ACCESSIBILITY · {_short(record.get('error', ''), 300)}"
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
