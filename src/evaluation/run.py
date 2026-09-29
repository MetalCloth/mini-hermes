"""Run small offline harness tasks with a scripted provider and real local tools."""

from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import time
from typing import Any
from threading import Event
from unittest.mock import patch

from src.agent.context import tokenizer_name
from src.agent.conversation_loop import run_turn
from src.providers.types import ModelResponse, ProviderRequestError, ToolCall
from src.tools.registry import tool_schemas


@dataclass(frozen=True)
class EvaluationCase:
    name: str
    prompt: str
    responses: tuple[ModelResponse, ...]
    answer_contains: str
    expected_tools: tuple[str, ...] = ()
    tool_result_contains: tuple[str, ...] = ()
    setup_files: tuple[tuple[str, str], ...] = ()
    expected_files: tuple[tuple[str, str | None], ...] = ()
    approve_writes: bool = False
    expected_error_class: str | None = None
    expected_error_contains: str | None = None
    partial_contains: str | None = None
    cancel_after_request: int | None = None
    provider_failure_at: int | None = None
    context_limit: int | None = None
    expected_model_requests: int | None = None


def _cases() -> tuple[EvaluationCase, ...]:
    return (
        EvaluationCase(
            "single-response", "Reply with the fixed check phrase.",
            (ModelResponse("Oryn offline evaluation ready."),),
            "offline evaluation ready",
        ),
        EvaluationCase(
            "read-project-file", "Read marker.txt and report its marker.",
            (
                ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "marker.txt"})]),
                ModelResponse("The marker is ORYN_EVAL_MARKER."),
            ),
            "ORYN_EVAL_MARKER", ("read_file",), ("ORYN_EVAL_MARKER",),
            (("marker.txt", "ORYN_EVAL_MARKER\n"),),
        ),
        EvaluationCase(
            "denied-write", "Create blocked.txt with the word blocked.",
            (
                ModelResponse(tool_calls=[ToolCall(
                    "write", "write_file", {"path": "blocked.txt", "content": "blocked"},
                )]),
                ModelResponse("The write was denied and no file was changed."),
            ),
            "write was denied", ("write_file",), ("cancelled",),
            expected_files=(("blocked.txt", None),),
        ),
        EvaluationCase(
            "provider-failure", "Answer after a provider request.", (), "",
            expected_error_class="ProviderRequestError",
            expected_error_contains="scripted provider failure",
            provider_failure_at=1,
            expected_model_requests=1,
        ),
        EvaluationCase(
            "interrupted-tool-cycle", "Read the marker and report it.",
            (
                ModelResponse("Checking marker first.", [ToolCall(
                    "read", "read_file", {"path": "marker.txt"},
                )]),
                ModelResponse("This final response should be discarded."),
            ),
            "", ("read_file",), ("ORYN_EVAL_MARKER",),
            (("marker.txt", "ORYN_EVAL_MARKER\n"),),
            expected_error_class="TurnCancelled",
            partial_contains="Checking marker first.",
            cancel_after_request=2,
            expected_model_requests=2,
        ),
        EvaluationCase(
            "current-turn-context-limit", "p" * 512, (), "",
            expected_error_class="ValueError",
            expected_error_contains="current turn exceed Oryn's 128-token context limit",
            context_limit=128,
            expected_model_requests=0,
        ),
    )


def evaluate_case(case: EvaluationCase) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="oryn-eval-") as folder:
        root = Path(folder)
        for relative, content in case.setup_files:
            (root / relative).write_text(content, encoding="utf-8")
        history: list[dict[str, Any]] = [{"role": "user", "content": case.prompt}]
        responses = iter(case.responses)
        events: list[dict[str, Any]] = []
        request_count = 0
        cancel_event = Event()

        def complete(_messages, _tools, **_kwargs):
            nonlocal request_count
            request_count += 1
            if case.provider_failure_at == request_count:
                raise ProviderRequestError("scripted provider failure without secrets")
            if case.cancel_after_request == request_count:
                cancel_event.set()
            try:
                return next(responses)
            except StopIteration as exc:
                raise AssertionError("Evaluation script ran out of provider responses.") from exc

        started = time.monotonic()
        error = None
        context_limit_patch = (
            patch("src.agent.compression.MAX_CONTEXT_TOKENS", case.context_limit)
            if case.context_limit is not None else patch("src.agent.compression.MAX_CONTEXT_TOKENS", 200_000)
        )
        with context_limit_patch:
            try:
                answer = run_turn(
                    history, complete, [] if case.context_limit is not None else tool_schemas(), root,
                    lambda _command: False,
                    lambda _path, _content, _exists: case.approve_writes,
                    confirm_edit=lambda _path, _diff: False,
                    confirm_undo=lambda _path, _diff, _created: False,
                    on_diagnostic=lambda _turn_id, event: events.append(event),
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                answer = ""
                error = type(exc).__name__
                error_message = str(exc)
            else:
                error_message = None
        elapsed_ms = round((time.monotonic() - started) * 1000)

        calls = [
            call["name"]
            for message in history if message.get("role") == "assistant"
            for call in message.get("tool_calls", [])
        ]
        results = [message.get("content", "") for message in history if message.get("role") == "tool"]
        partials = [message.get("content", "") for message in history if message.get("role") == "assistant"]
        error_matches = (
            error == case.expected_error_class
            and (case.expected_error_contains is None
                 or case.expected_error_contains.casefold() in (error_message or "").casefold())
        )
        checks = {
            "answer": case.answer_contains.casefold() in answer.casefold(),
            "tool_calls": tuple(calls) == case.expected_tools,
            "tool_results": all(
                any(expected.casefold() in result.casefold() for result in results)
                for expected in case.tool_result_contains
            ),
            "files": all(
                ((root / relative).read_text(encoding="utf-8") if (root / relative).is_file() else None) == expected
                for relative, expected in case.expected_files
            ),
            "expected_error": error_matches,
            "partial_history": case.partial_contains is None or any(
                case.partial_contains.casefold() in content.casefold() for content in partials
            ),
            "expected_request_count": (
                case.cancel_after_request is None or request_count == case.cancel_after_request
            ) and (case.provider_failure_at is None or request_count == case.provider_failure_at)
            and (case.expected_model_requests is None or request_count == case.expected_model_requests),
        }
        return {
            "name": case.name,
            "passed": all(checks.values()),
            "checks": checks,
            "expected": {
                "answer_contains": case.answer_contains,
                "tool_calls": case.expected_tools,
                "tool_result_contains": case.tool_result_contains,
                "error_class": case.expected_error_class,
                "error_contains": case.expected_error_contains,
                "partial_contains": case.partial_contains,
                "model_requests": case.expected_model_requests,
            },
            "observed": {
                "answer": answer[:500],
                "tool_calls": calls,
                "tool_results": [result[:300] for result in results],
                "partial_history": [content[:300] for content in partials],
                "error_class": error,
            },
            "expected_error_class": case.expected_error_class,
            "error_class": error,
            "error_message": error_message,
            "elapsed_ms": elapsed_ms,
            "model_requests": request_count,
            "tool_calls": len(calls),
            "estimated_context_tokens": max(
                (event.get("estimated_tokens", 0) for event in events if event["type"] == "model_request"),
                default=0,
            ),
        }


def run_evaluations() -> dict[str, Any]:
    # Force the stdlib-safe estimator so a fresh CI runner never downloads tokenizer data.
    with patch("src.agent.context._encoding", return_value=None):
        results = [evaluate_case(case) for case in _cases()]
        tokenizer = tokenizer_name()
    return {
        "suite": "oryn-offline-v2",
        "tokenizer": tokenizer,
        "network_or_credentials": False,
        "passed": sum(result["passed"] for result in results),
        "failed": sum(not result["passed"] for result in results),
        "model_requests": sum(result["model_requests"] for result in results),
        "tool_calls": sum(result["tool_calls"] for result in results),
        "tasks": results,
    }


def main() -> None:
    report = run_evaluations()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
