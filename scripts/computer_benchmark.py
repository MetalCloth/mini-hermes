#!/usr/bin/env python3
"""Summarize /computer timings without printing tasks, prompts, or screenshots."""

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


def _seconds(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def _stats(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "median": None, "p90": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "median": round(median(ordered), 3),
        "p90": round(ordered[ceil(len(ordered) * 0.9) - 1], 3),
    }


def summarize(paths: list[Path]) -> dict:
    """Read only event names, timestamps, and counts from private trace files."""
    counts: Counter[str] = Counter()
    model_seconds: list[float] = []
    accessibility_seconds: list[float] = []
    accessibility_status: Counter[str] = Counter()
    task_seconds: dict[str, list[float]] = {status: [] for status in END_EVENTS.values()}
    model_calls: dict[str, list[float]] = {status: [] for status in END_EVENTS.values()}
    for path in paths:
        task = None
        request_time = None
        with path.open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                record = json.loads(line)
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
                        model_seconds.append(_seconds(request_time, stamp))
                    request_time = None
                elif event == "accessibility_observation":
                    accessibility_status[str(record.get("status", "unavailable"))] += 1
                    elapsed = record.get("elapsed_ms")
                    if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and elapsed >= 0:
                        accessibility_seconds.append(elapsed / 1000)
                elif event in END_EVENTS and task is not None:
                    status = END_EVENTS[event]
                    counts[status] += 1
                    task_seconds[status].append(_seconds(task["started"], stamp))
                    model_calls[status].append(task["calls"])
                    task = None
                    request_time = None
        if task is not None:
            counts["incomplete"] += 1
    return {
        "files": len(paths),
        "tasks": dict(counts),
        "model_response_seconds": _stats(model_seconds),
        "accessibility_seconds": _stats(accessibility_seconds),
        "accessibility_status": dict(accessibility_status),
        "task_seconds": {key: _stats(value) for key, value in task_seconds.items()},
        "model_calls": {key: _stats(value) for key, value in model_calls.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", nargs="+", type=Path, help="Computer JSONL trace files")
    args = parser.parse_args()
    print(json.dumps(summarize(args.traces), indent=2))


if __name__ == "__main__":
    main()
