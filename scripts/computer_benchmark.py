#!/usr/bin/env python3
"""Summarize computer JSONL traces without printing tasks or screen contents."""

import argparse
from collections import Counter
from datetime import datetime
import json
from math import ceil
from pathlib import Path
from statistics import median


END_EVENTS = {
    "computer_task_done": "reported_done",
    "computer_task_failed": "failed",
    "computer_task_cancelled": "cancelled",
}
CURRENT_END_EVENTS = {
    "computer_task_finished": "finished",
    "computer_task_failed": "failed",
    "computer_task_cancelled": "cancelled",
}


def _seconds(start: str, end: str) -> float | None:
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return None


def _stats(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "median": None, "p90": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "median": round(median(ordered), 3),
        "p90": round(ordered[ceil(len(ordered) * 0.9) - 1], 3),
    }


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def _new_run(path: Path, trace_id: str) -> dict:
    return {
        "file": path.name,
        "trace_id": trace_id,
        "status": "incomplete",
        "started_at": None,
        "ended_at": None,
        "turn_seconds": None,
        "model_requests": 0,
        "model_response_ms": [],
        "model_attempt_errors": 0,
        "retries": 0,
        "tool_calls": Counter(),
        "tool_time_ms": {},
        "tool_failures": 0,
        "observations": 0,
        "screenshots_attached": 0,
        "observation_scopes": Counter(),
        "accessibility_tree_outputs": 0,
        "accessibility_tree_excerpts": 0,
        "accessibility_controls_sent": 0,
        "active_accessibility_summaries": 0,
        "extra_back_to_back_observe_calls": 0,
        "observe_streak": 0,
        "longest_observe_streak": 0,
        "progress_hints": 0,
        "has_computer_task": False,
    }


def _add_current_record(run: dict, record: dict) -> None:
    event = record.get("event")
    if event == "computer_task_started":
        run["started_at"] = record.get("time_utc")
        run["has_computer_task"] = True
    elif event in CURRENT_END_EVENTS:
        run["ended_at"] = record.get("time_utc")
        run["status"] = CURRENT_END_EVENTS[event]
        run["has_computer_task"] = True

    if event == "computer_diagnostic":
        kind = record.get("diagnostic_type")
        if kind == "model_request":
            run["model_requests"] += 1
        elif kind == "model_response":
            elapsed = _number(record.get("elapsed_ms"))
            if elapsed is not None:
                run["model_response_ms"].append(elapsed)
        elif kind == "model_attempt_error":
            run["model_attempt_errors"] += 1
        elif kind == "retry":
            run["retries"] += 1
        elif kind in {"turn_end", "turn_error"}:
            elapsed = _number(record.get("elapsed_ms"))
            if elapsed is not None:
                run["turn_seconds"] = round(elapsed / 1000, 3)
        elif kind == "tool_end":
            name = record.get("tool_name")
            elapsed = _number(record.get("elapsed_ms"))
            if isinstance(name, str) and elapsed is not None:
                run["tool_time_ms"].setdefault(name, []).append(elapsed)
            if record.get("success") is False:
                run["tool_failures"] += 1

    if event == "tool_call" and record.get("phase") == "start":
        name = record.get("name")
        if isinstance(name, str):
            run["tool_calls"][name] += 1
            run["has_computer_task"] |= name.startswith("computer_")
            if name == "computer_observe":
                run["observe_streak"] += 1
                run["longest_observe_streak"] = max(
                    run["longest_observe_streak"], run["observe_streak"],
                )
                if run["observe_streak"] > 1:
                    run["extra_back_to_back_observe_calls"] += 1
            else:
                run["observe_streak"] = 0

    if event == "computer_observation":
        run["observations"] += 1
        scope = record.get("scope")
        if isinstance(scope, str):
            run["observation_scopes"][scope] += 1
        if record.get("screenshot_included") is True:
            run["screenshots_attached"] += 1
        if record.get("active_accessibility") is True:
            run["active_accessibility_summaries"] += 1

    if event == "computer_observation_detail":
        run["accessibility_tree_outputs"] += 1
        if isinstance(record.get("tree_excerpt"), str) and record["tree_excerpt"].strip():
            run["accessibility_tree_excerpts"] += 1
        controls = _number(record.get("model_control_count"))
        if controls is not None:
            run["accessibility_controls_sent"] += int(controls)

    if event == "computer_progress_hint":
        run["progress_hints"] += 1


def _finish_run(run: dict, path: Path, trace_id: str) -> dict:
    computer_seconds = _seconds(run["started_at"], run["ended_at"])
    return {
        "file": path.name,
        "trace_id": trace_id,
        "status": run["status"],
        "turn_seconds": run["turn_seconds"],
        "computer_seconds": round(computer_seconds, 3) if computer_seconds is not None else None,
        "model_requests": run["model_requests"],
        "model_response_ms": _stats(run["model_response_ms"]),
        "model_attempt_errors": run["model_attempt_errors"],
        "retries": run["retries"],
        "tool_calls": dict(sorted(run["tool_calls"].items())),
        "tool_time_ms": {
            name: _stats(values) for name, values in sorted(run["tool_time_ms"].items())
        },
        "tool_failures": run["tool_failures"],
        "observations": run["observations"],
        "observation_scopes": dict(sorted(run["observation_scopes"].items())),
        "screenshots_attached": run["screenshots_attached"],
        "accessibility": {
            "tree_outputs": run["accessibility_tree_outputs"],
            "tree_excerpts": run["accessibility_tree_excerpts"],
            "controls_sent": run["accessibility_controls_sent"],
            "active_summaries": run["active_accessibility_summaries"],
        },
        "repeated_observations": {
            "extra_back_to_back_observe_calls": run["extra_back_to_back_observe_calls"],
            "longest_back_to_back_observe_calls": run["longest_observe_streak"],
            "progress_hints": run["progress_hints"],
        },
    }


def _totals(runs: list[dict], model_response_ms: list[float]) -> dict:
    tool_calls: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    turn_seconds = []
    for run in runs:
        statuses[run["status"]] += 1
        tool_calls.update(run["tool_calls"])
        if run["turn_seconds"] is not None:
            turn_seconds.append(run["turn_seconds"])
    return {
        "runs": len(runs),
        "statuses": dict(sorted(statuses.items())),
        "turn_seconds": _stats(turn_seconds),
        "computer_seconds": _stats([
            run["computer_seconds"] for run in runs if run["computer_seconds"] is not None
        ]),
        "model_response_ms": _stats(model_response_ms),
        "tool_calls": dict(sorted(tool_calls.items())),
        "screenshots_attached": sum(run["screenshots_attached"] for run in runs),
        "accessibility_tree_outputs": sum(run["accessibility"]["tree_outputs"] for run in runs),
        "accessibility_controls_sent": sum(run["accessibility"]["controls_sent"] for run in runs),
        "active_accessibility_summaries": sum(run["accessibility"]["active_summaries"] for run in runs),
        "extra_back_to_back_observe_calls": sum(
            run["repeated_observations"]["extra_back_to_back_observe_calls"] for run in runs
        ),
        "longest_back_to_back_observe_calls": max(
            (run["repeated_observations"]["longest_back_to_back_observe_calls"] for run in runs),
            default=0,
        ),
    }


def summarize(paths: list[Path]) -> dict:
    """Read event names, timestamps, and counts; discard all trace content."""
    paths = list(paths)
    counts: Counter[str] = Counter()
    model_seconds: list[float] = []
    accessibility_seconds: list[float] = []
    accessibility_status: Counter[str] = Counter()
    task_seconds: dict[str, list[float]] = {status: [] for status in END_EVENTS.values()}
    model_calls: dict[str, list[float]] = {status: [] for status in END_EVENTS.values()}
    current_runs: dict[tuple[str, str], dict] = {}

    for path in paths:
        task = None
        request_time = None
        with path.open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    continue

                trace_id = record.get("trace_id")
                if isinstance(trace_id, str) and trace_id:
                    key = (path.name, trace_id)
                    run = current_runs.setdefault(key, _new_run(path, trace_id))
                    _add_current_record(run, record)

                # Keep the payload-free summary for historical planner traces.
                event = record.get("event")
                stamp = record.get("time_utc")
                if event == "computer_task_start":
                    if task is not None:
                        counts["incomplete"] += 1
                    task = {"started": stamp, "calls": 0}
                    request_time = None
                    counts["started"] += 1
                elif event == "model_request":
                    request_time = stamp
                    if task is not None:
                        task["calls"] += 1
                elif event in {"model_response", "model_error"}:
                    if request_time is not None and event == "model_response":
                        elapsed = _seconds(request_time, stamp)
                        if elapsed is not None:
                            model_seconds.append(elapsed)
                    request_time = None
                elif event == "accessibility_observation":
                    accessibility_status[str(record.get("status", "unavailable"))] += 1
                    elapsed = _number(record.get("elapsed_ms"))
                    if elapsed is not None:
                        accessibility_seconds.append(elapsed / 1000)
                elif event in END_EVENTS and task is not None:
                    status = END_EVENTS[event]
                    counts[status] += 1
                    elapsed = _seconds(task["started"], stamp)
                    if elapsed is not None:
                        task_seconds[status].append(elapsed)
                    model_calls[status].append(task["calls"])
                    task = None
                    request_time = None
        if task is not None:
            counts["incomplete"] += 1

    runs = [
        _finish_run(run, Path(file_name), trace_id)
        for (file_name, trace_id), run in current_runs.items()
        if run["has_computer_task"]
    ]
    model_response_ms = [
        elapsed for run in current_runs.values() for elapsed in run["model_response_ms"]
        if run["has_computer_task"]
    ]

    result = {
        "files": len(paths),
        "runs": runs,
        "totals": _totals(runs, model_response_ms),
    }
    if counts or model_seconds or accessibility_seconds or accessibility_status:
        result["historical_planner"] = {
            "tasks": dict(counts),
            "model_response_seconds": _stats(model_seconds),
            "accessibility_seconds": _stats(accessibility_seconds),
            "accessibility_status": dict(accessibility_status),
            "task_seconds": {key: _stats(value) for key, value in task_seconds.items()},
            "model_calls": {key: _stats(value) for key, value in model_calls.items()},
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", nargs="+", type=Path, help="Computer JSONL trace files")
    args = parser.parse_args()
    print(json.dumps(summarize(args.traces), indent=2))


if __name__ == "__main__":
    main()
