"""Private, flushed JSONL traces for computer-mode debugging."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import threading
import uuid


_REMOTE_CONTENT_FIELDS = {
    "answer", "confirmation_reason", "developer_instructions", "expected_result",
    "prompt", "question", "summary", "task", "text",
    "tree_excerpt", "user_content", "value_equals", "window_title",
}
_REMOTE_OUTPUT_FIELDS = {"error", "message", "stderr", "stdout"}


def _scrub_remote(value):
    """Drop free-form content recursively while retaining structured trace metadata."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized == "label_contains":
                result[key] = "[redacted]"
                continue
            if (normalized in _REMOTE_CONTENT_FIELDS or normalized in _REMOTE_OUTPUT_FIELDS
                    or "base64" in normalized or normalized in {"screenshot", "image"}):
                if normalized in _REMOTE_OUTPUT_FIELDS and isinstance(item, str):
                    result[f"{key}_chars"] = len(item)
                continue
            result[key] = _scrub_remote(item)
        return result
    if isinstance(value, list):
        return [_scrub_remote(item) for item in value[:100]]
    return value


def _langsmith_safe_record(record: dict) -> dict:
    """Keep trace metadata while excluding screenshots, prompts, and typed content."""
    safe = dict(record)
    event = safe.get("event")
    task = safe.pop("task", None)
    if isinstance(task, str):
        safe["task_chars"] = len(task)
    answer = safe.pop("answer", None)
    if isinstance(answer, str):
        safe["answer_chars"] = len(answer)
    safe.pop("developer_instructions", None)
    safe.pop("user_content", None)
    safe.pop("tree_excerpt", None)
    safe.pop("window_title", None)

    if event in {"computer_tool", "tool_call"}:
        result = safe.pop("result", None)
        if isinstance(result, str):
            safe["result_chars"] = len(result)
        arguments = safe.get("arguments")
        if isinstance(arguments, dict):
            safe["argument_keys"] = sorted(str(key)[:100] for key in arguments)[:40]
            action = arguments.get("action")
            if isinstance(action, dict) and isinstance(action.get("type"), str):
                safe["action_type"] = action["type"]
                if type(action.get("element_index")) is int:
                    safe["element_index"] = action["element_index"]
        safe.pop("arguments", None)

    if event == "model_response":
        text = safe.pop("text", None)
        calls = safe.pop("tool_calls", None)
        if isinstance(text, str):
            safe["response_chars"] = len(text)
        if isinstance(calls, list):
            safe["tool_call_count"] = len(calls)
            safe["tool_call_names"] = [
                str(call.get("name", "unknown"))[:100]
                for call in calls if isinstance(call, dict)
            ][:32]

    action = safe.get("action")
    if isinstance(action, dict) and action.get("type") == "type":
        action = dict(action)
        action["text"] = "[redacted]"
        safe["action"] = action

    target = safe.get("target")
    if isinstance(target, dict):
        target = dict(target)
        role = str(target.get("role") or "").casefold().replace("_", " ").strip()
        if role not in {"option", "combo box", "list box", "check box", "checkbox",
                        "combobox", "listbox", "radio button", "radio", "switch", "toggle button"}:
            target.pop("value", None)
        safe["target"] = target

    controls = safe.get("controls")
    if not isinstance(controls, list):
        controls = safe.get("model_controls")
    if isinstance(controls, list):
        safe_controls = []
        for control in controls:
            if not isinstance(control, dict):
                continue
            item = dict(control)
            role = str(item.get("role") or "").casefold().replace("_", " ").strip()
            if role not in {"option", "combo box", "list box", "check box", "checkbox",
                            "combobox", "listbox", "radio button", "radio", "switch", "toggle button"}:
                item.pop("value", None)
            safe_controls.append(item)
        safe["model_controls" if "model_controls" in safe else "controls"] = safe_controls

    if isinstance(safe.get("windows"), list):
        safe["windows"] = [
            {key: value for key, value in window.items() if key != "title"}
            for window in safe["windows"] if isinstance(window, dict)
        ]

    if isinstance(safe.get("expect"), dict):
        expect = json.loads(json.dumps(safe["expect"]))
        element = expect.get("element") if isinstance(expect, dict) else None
        if isinstance(element, dict):
            element.pop("value_equals", None)
            selector = element.get("selector")
            if isinstance(selector, dict) and "label_contains" in selector:
                selector["label_contains"] = "[redacted]"
        safe["expect"] = expect

    if event == "computer_verify" and isinstance(safe.get("result"), dict):
        result = safe.pop("result")
        safe["result_status"] = result.get("status")
        safe["result_satisfied"] = result.get("satisfied")
        safe["result_count"] = result.get("count")

    if event in {"action_start", "action_complete"}:
        safe.pop("label", None)
    if isinstance(safe.get("error"), str):
        error = safe.pop("error")
        safe["error_chars"] = len(error)

    if event == "dotool_start" and isinstance(safe.get("stdin"), str):
        safe["stdin"] = "\n".join(
            "type [redacted]" if line.startswith("type ") else line
            for line in safe["stdin"].splitlines()
        )
    return _scrub_remote(safe)


class _LangSmithExporter:
    """Best-effort asynchronous export; local logging never depends on it."""

    def __init__(self, project_name: str, local_trace_id: str, on_error) -> None:
        self._events: queue.Queue[dict | None] = queue.Queue()
        self._on_error = on_error
        self._error: str | None = None
        self._thread = threading.Thread(
            target=self._run, args=(project_name, local_trace_id), name="oryn-langsmith-trace", daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def submit(self, record: dict) -> None:
        self._events.put_nowait(record)

    def close(self) -> None:
        self._events.put_nowait(None)
        self._thread.join(timeout=0.15)

    def _run(self, project_name: str, local_trace_id: str) -> None:
        try:
            from langsmith import Client
            from langsmith.run_trees import RunTree

            client = Client()
            root = RunTree(
                name="Oryn computer task", run_type="chain",
                inputs={"source": "Oryn /computer", "local_trace_id": local_trace_id},
                project_name=project_name, client=client,
            )
            root.post()
            event_count = 0
            while True:
                record = self._events.get()
                if record is None:
                    break
                payload = _langsmith_safe_record(record)
                event = str(payload.get("event") or "trace_event")
                sequence = payload.get("seq", event_count + 1)
                child = root.create_child(
                    name=f"{sequence:04} {event}", run_type="tool",
                    inputs=payload,
                )
                child.post()
                child.end(outputs={"recorded": True})
                child.patch()
                event_count += 1
            root.end(outputs={"event_count": event_count})
            root.patch()
        except Exception as exc:
            self._error = type(exc).__name__
            self._on_error(self._error)


class ComputerTrace:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(descriptor, 0o600)
        self._file = os.fdopen(descriptor, "a", encoding="utf-8")
        self._lock = threading.Lock()
        self.trace_id = uuid.uuid4().hex[:12]
        self._sequence = 0
        self._langsmith: _LangSmithExporter | None = None
        enabled = os.environ.get("ORYN_LANGSMITH_TRACING", "").strip().casefold() in {
            "1", "true", "yes", "on",
        }
        if enabled:
            if not os.environ.get("LANGSMITH_API_KEY"):
                self._write_local("langsmith_status", status="disabled", reason="missing_api_key")
            else:
                project = os.environ.get("LANGSMITH_PROJECT", "oryn-computer")
                self._langsmith = _LangSmithExporter(project, self.trace_id, self._langsmith_error)
                self._write_local("langsmith_status", status="queued", project=project)
                self._langsmith.start()

    @classmethod
    def from_environment(cls) -> "ComputerTrace | None":
        path = os.environ.get("ORYN_COMPUTER_LOG_FILE")
        return cls(path) if path else None

    def write(self, event: str, **fields) -> None:
        record = self._write_local(event, **fields)
        if self._langsmith is not None and event != "langsmith_status":
            self._langsmith.submit(record)

    def _write_local(self, event: str, **fields) -> dict:
        with self._lock:
            self._sequence += 1
            record = {
                "time_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "trace_id": self.trace_id,
                "seq": self._sequence,
                "event": event,
                **fields,
            }
            self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._file.flush()
        return record

    def _langsmith_error(self, error_class: str) -> None:
        try:
            self._write_local("langsmith_status", status="error", error_class=error_class)
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        if self._langsmith is not None:
            self._langsmith.close()
        with self._lock:
            if not self._file.closed:
                self._file.close()
