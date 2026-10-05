#!/usr/bin/env python3
"""Render computer JSONL traces as short, readable terminal events."""

import json
import os
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
    if kind == "click_element":
        return f"click element [{action.get('element_index', '?')}]"
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
        delay = record.get("elapsed_since_turn_start_ms")
        queue = record.get("worker_queue_delay_ms")
        delay_text = f" · start +{delay}ms (queue {queue}ms)" if isinstance(delay, int) else ""
        return f"{stamp}  TASK STARTED · {record.get('model', 'unknown model')}{driver}{delay_text}\n  Task: {_short(record.get('task', ''))}"
    if event == "computer_preflight":
        return (
            f"{stamp}  PREFLIGHT · {record.get('stage', '?')} · "
            f"{'ok' if record.get('success') else 'failed'} · {record.get('elapsed_ms', '?')}ms"
        )
    if event == "computer_driver_started":
        return f"{stamp}  DRIVER READY · {record.get('driver', '?')} · {record.get('elapsed_ms', '?')}ms"
    if event in {"computer_tool", "tool_call"}:
        phase = record.get("phase", "?")
        name = record.get("name", "?")
        detail = record.get("arguments") if phase == "start" else record.get("result")
        call_id = f" · call {record['call_id']}" if record.get("call_id") else ""
        if phase == "start" and record.get("arguments_truncated"):
            detail = f"{_short(detail, 240)} [preview; {record.get('arguments_chars', '?')} chars total]"
        if phase != "start" and record.get("result_truncated"):
            detail = f"{_short(detail, 240)} [preview; {record.get('result_chars', '?')} chars total]"
        return f"{stamp}  TOOL {str(phase).upper()} · {name}{call_id} · {_short(detail, 300)}"
    if event == "mcp_status":
        servers = _short(record.get("server_states", ""), 500)
        return (
            f"{stamp}  MCP STATUS · {record.get('phase', '?')} · {record.get('startup_state', '?')} · "
            f"startup {record.get('startup_elapsed_ms', '?')}ms · "
            f"{record.get('connected_count', 0)}/{record.get('enabled_count', 0)} connected · "
            f"{record.get('starting_count', 0)} starting · {record.get('unavailable_count', 0)} unavailable"
            + (f"\n  {servers}" if servers else "")
        )
    if event == "computer_diagnostic":
        kind = record.get("diagnostic_type", "?")
        if kind == "model_request":
            preparation = record.get("preparation_elapsed_ms")
            prep_text = f" · prep {preparation}ms" if isinstance(preparation, int) else ""
            return (f"{stamp}  MODEL REQUEST · round {record.get('round', '?')} · "
                    f"~{record.get('estimated_tokens', '?')} tokens · {record.get('tool_count', '?')} tools{prep_text}")
        if kind == "turn_start":
            return f"{stamp}  HARNESS READY · {record.get('model', '?')} · turn {record.get('turn_id', '?')}"
        if kind == "mcp_directory":
            servers = _short(record.get("server_states", ""), 500)
            return (
                f"{stamp}  MCP DIRECTORY · {record.get('directory_elapsed_ms', '?')}ms · "
                f"startup {record.get('startup_state', '?')} ({record.get('startup_elapsed_ms', '?')}ms) · "
                f"{record.get('connected_count', 0)}/{record.get('enabled_count', 0)} connected · "
                f"{record.get('starting_count', 0)} starting · {record.get('unavailable_count', 0)} unavailable"
                + (f"\n  {servers}" if servers else "")
            )
        if kind == "model_response":
            return (
                f"{stamp}  MODEL RESPONSE · round {record.get('round', '?')} · "
                f"{record.get('elapsed_ms', '?')}ms · {record.get('tool_call_count', '?')} tool calls · "
                f"{record.get('response_chars', '?')} text chars"
            )
        if kind == "model_attempt_error":
            return (
                f"{stamp}  MODEL ATTEMPT FAILED · round {record.get('round', '?')} · "
                f"attempt {record.get('attempt', '?')} · {record.get('elapsed_ms', '?')}ms · "
                f"{record.get('error_class', '?')} · provider call "
                f"{'entered' if record.get('provider_call_entered') else 'not entered'} · "
                f"retryable {str(record.get('retryable', '?')).lower()}"
            )
        if kind in {"tool_start", "tool_end"}:
            elapsed = f" · {record['elapsed_ms']}ms" if isinstance(record.get("elapsed_ms"), int) else ""
            success = f" · {'ok' if record.get('success') else 'failed'}" if kind == "tool_end" else ""
            return f"{stamp}  {kind.replace('_', ' ').upper()} · {record.get('tool_name', '?')}{elapsed}{success}"
        return f"{stamp}  {str(kind).replace('_', ' ').upper()} · {_short(record.get('error_class', ''), 180)}"
    if event == "computer_observation":
        scope = record.get("scope", "?")
        image_scope = record.get("image_scope")
        image_label = f" · image {image_scope}" if image_scope else ""
        elements = f" · {record['element_count']} controls" if "element_count" in record else ""
        image_state = "screenshot attached" if record.get("screenshot_included") else "no screenshot"
        details = []
        if record.get("window_title"):
            details.append(_short(record["window_title"], 100))
        if record.get("pid") is not None:
            details.append(f"pid {record['pid']} · window {record.get('window_id', '?')}")
        cause = record.get("cause")
        if isinstance(cause, dict) and cause.get("id"):
            details.append(f"after {cause.get('kind', 'event')} {cause['id']}")
        elapsed = record.get("elapsed_ms")
        timing = f" · {elapsed}ms" if isinstance(elapsed, (int, float)) else ""
        suffix = f"\n  {' · '.join(details)}" if details else ""
        return (f"{stamp}  OBSERVE · {scope}{image_label} · "
                f"{record.get('width', '?')}×{record.get('height', '?')}{elements} · "
                f"{image_state} · id {record.get('observation_id', '?')}{timing}{suffix}")
    if event == "computer_windows":
        windows = record.get("windows", [])
        window_list = windows if isinstance(windows, list) else []
        lines = [f"{stamp}  WINDOWS · {record.get('window_count', len(window_list))} found · "
                 f"{record.get('elapsed_ms', '?')}ms"]
        for window in window_list[:8]:
            if not isinstance(window, dict):
                continue
            title = window.get("title") or window.get("app_name") or "(untitled)"
            lines.append(
                f"  · {window.get('app_name', '?')} · {_short(title, 100)} · "
                f"pid {window.get('pid', '?')} · id {window.get('window_id', '?')} · "
                f"{_short(window.get('bounds', {}), 100)}"
            )
        if record.get("truncated"):
            lines.append("  · list capped in this trace")
        return "\n".join(lines)
    if event == "computer_observation_detail":
        controls = record.get("model_controls", [])
        control_list = controls if isinstance(controls, list) else []
        mapping_frames = record.get("mapping_frames", [])
        frame_list = mapping_frames if isinstance(mapping_frames, list) else []
        frames = {
            item.get("index"): item.get("frame") for item in frame_list if isinstance(item, dict)
        }
        lines = [
            f"{stamp}  CONTROLS · {record.get('model_control_count', len(control_list))} sent to model · "
            f"{record.get('elapsed_ms', '?')}ms"
        ]
        for control in control_list[:10]:
            if not isinstance(control, dict):
                continue
            role = control.get("role") or "control"
            label = _short(control.get("label") or "(no label)", 90)
            value = control.get("value")
            normalized_role = str(role).casefold().replace("_", " ").strip()
            choice_roles = {
                "option", "combo box", "combobox", "list box", "listbox",
                "check box", "checkbox", "radio", "radio button", "switch", "toggle button",
            }
            if value and normalized_role not in choice_roles:
                value = "[redacted in view]"
            value_text = f" · value={_short(value, 60)!r}" if value not in (None, "") else ""
            frame = control.get("image_frame") or frames.get(control.get("index"))
            frame_text = f" · frame={_short(frame, 80)}" if frame else ""
            lines.append(
                f"    [{control.get('index', '?')}] {role} · {_short(label, 90)}"
                f"{value_text}{frame_text}"
            )
        omitted = record.get("model_controls_omitted_count", 0)
        if omitted:
            lines.append(f"    · {omitted} native elements not sent in the control list")
        excerpt = record.get("tree_excerpt")
        if excerpt:
            lines.append(f"    TREE · {_short(excerpt, 360)}")
        return "\n".join(lines)
    if event == "computer_action_start":
        action_id = f" · action {record['action_id']}" if record.get("action_id") else ""
        observation_id = f" · from {record['observation_id']}" if record.get("observation_id") else ""
        return f"{stamp}  ACTION{action_id} · {_action(record.get('action', {}))}{observation_id}"
    if event == "computer_action_target":
        target = record.get("target")
        if isinstance(target, dict):
            target_text = (
                f"[{target.get('element_index', '?')}] {target.get('role', 'control')} · "
                f"{_short(target.get('label') or '(no label)', 90)} · "
                f"frame {_short(target.get('frame', {}), 90)}"
            )
        else:
            point = record.get("requested_point") or {}
            target_text = f"image point ({point.get('x', '?')}, {point.get('y', '?')})"
        mapped = record.get("mapped_monitor_point", {})
        return (
            f"{stamp}  TARGET · {target_text} · mapped monitor point "
            f"({mapped.get('x', '?')}, {mapped.get('y', '?')}) · "
            f"action {record.get('action_id', '?')}"
        )
    if event == "computer_action_result":
        outcome = record.get("outcome", {})
        route = f" · {outcome['route']}" if isinstance(outcome, dict) and outcome.get("route") else ""
        effect = outcome.get("effect", "?") if isinstance(outcome, dict) else "?"
        detail = ""
        if isinstance(outcome, dict):
            reason = outcome.get("reason") or outcome.get("error") or outcome.get("message")
            if reason:
                detail = f" · {_short(reason, 180)}"
        return f"{stamp}  ACTION RESULT · {effect}{route}{detail} · action {record.get('action_id', '?')}"
    if event == "computer_action_complete":
        error = f" · {_short(record.get('observation_error'), 180)}" if record.get("observation_error") else ""
        return (
            f"{stamp}  AFTER ACTION · {record.get('resulting_observation_id') or 'no observation'} · "
            f"screenshot {'yes' if record.get('screenshot_included') else 'no'} · "
            f"action {record.get('action_id', '?')}{error}"
        )
    if event == "computer_wait_start":
        return (
            f"{stamp}  WAIT · {record.get('timeout_ms', '?')}ms · "
            f"observation {record.get('observation_id', '?')} · id {record.get('wait_id', '?')}"
        )
    if event == "computer_wait_complete":
        error = f" · {_short(record.get('error_class'), 100)}" if record.get("error_class") else ""
        return f"{stamp}  WAIT DONE · {record.get('observation_id') or 'no observation'}{error} · id {record.get('wait_id', '?')}"
    if event == "computer_verify":
        result = record.get("result", {})
        correlation = record.get("action_id") or record.get("wait_id")
        link = f" · action/wait {correlation}" if correlation else ""
        expectation = f" · expect {_short(record.get('expect'), 100)}" if record.get("expect") else ""
        duration = f" · {record['timeout_ms']}ms max" if isinstance(record.get("timeout_ms"), int) else ""
        return f"{stamp}  VERIFY · {_short(result, 180)}{link}{expectation}{duration}"
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
        action_id = f" · action {record['action_id']}" if record.get("action_id") else ""
        lines = [f"{stamp}  INPUT{action_id} · {shlex.join(argv)}"]
        if record.get("stdin") is not None:
            lines.append(f"  Actions: {_short(record['stdin'], 240)!r}")
        return "\n".join(lines)
    if event == "dotool_result":
        code = record.get("returncode")
        if record.get("success", code == 0):
            duration = f" · {record['elapsed_ms']}ms" if isinstance(record.get("elapsed_ms"), int) else ""
            return f"{stamp}  INPUT OK{duration} · action {record.get('action_id', '?')}"
        details = record.get("stderr") or record.get("error") or record.get("stdout") or ""
        details = "\n".join(
            line.strip() for line in ANSI.sub("", str(details)).splitlines()
            if line.strip() and "INFO using forced backend" not in line
        )
        duration = f" · {record['elapsed_ms']}ms" if isinstance(record.get("elapsed_ms"), int) else ""
        lines = [f"{stamp}  INPUT FAILED · exit {code if code is not None else 'unknown'}{duration} · action {record.get('action_id', '?')}"]
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
    if event == "langsmith_status":
        detail = record.get("reason") or record.get("error_class") or record.get("project") or ""
        return f"{stamp}  LANGSMITH · {record.get('status', '?')} · {_short(detail, 160)}"

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
            rendered = format_record(record)
            sequence = record.get("seq")
            if isinstance(sequence, int):
                rendered = f"#{sequence:04}  {rendered}"
            if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
                event = record.get("event", "")
                if (event in {"computer_task_failed", "model_error", "turn_error"}
                        or (event == "computer_preflight" and not record.get("success"))):
                    color = "\033[31m"
                elif event in {"computer_task_started", "computer_task_start", "computer_task_finished", "computer_task_done"}:
                    color = "\033[35m"
                elif event in {"computer_observation", "computer_observation_detail", "computer_windows"}:
                    color = "\033[36m"
                elif event in {"computer_action_result", "computer_action_complete", "computer_wait_complete"}:
                    color = "\033[32m"
                elif event in {"computer_action_start", "computer_action_target", "dotool_start", "dotool_result"}:
                    color = "\033[33m"
                elif event == "tool_call":
                    color = "\033[33m" if record.get("phase") == "start" else "\033[32m"
                else:
                    color = "\033[37m"
                rendered = f"{color}{rendered}\033[0m"
            print(rendered, flush=True)


if __name__ == "__main__":
    main()
