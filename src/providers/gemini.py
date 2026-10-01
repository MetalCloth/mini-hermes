"""Gemini GenerateContent client using Oryn's provider-neutral message types."""

import json
import re
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Iterable

from src.providers.types import ModelResponse, ProviderRequestError, ToolCall, retry_after_seconds
from src.security.secrets import local_secret


API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
API_KEY_FILE = "gemini.env"
_MODEL_CHOICES = (
    ("gemini-3.8-flash", "Gemini 3.8 Flash"),
    ("gemini-3.7-flash", "Gemini 3.7 Flash"),
    ("gemini-3.6-flash", "Gemini 3.6 Flash"),
    ("gemini-3.5-flash", "Gemini 3.5 Flash"),
    ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite"),
    ("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite"),
    ("gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview"),
    ("gemini-3-flash-preview", "Gemini 3 Flash Preview"),
)


def _api_key() -> str:
    return local_secret("GEMINI_API_KEY", API_KEY_FILE)


def _completed_call_ids(messages: list[dict[str, Any]], index: int) -> set[str]:
    calls = messages[index].get("tool_calls", [])
    if not isinstance(calls, list):
        return set()
    outputs: set[str] = set()
    next_index = index + 1
    while next_index < len(messages) and messages[next_index].get("role") == "tool":
        call_id = messages[next_index].get("tool_call_id")
        if isinstance(call_id, str):
            outputs.add(call_id)
        next_index += 1
    return {
        call["id"] for call in calls
        if isinstance(call, dict) and isinstance(call.get("id"), str) and call["id"] in outputs
    }


def _content_history(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    instructions = "\n\n".join(
        message["content"] for message in messages
        if message.get("role") in {"system", "developer"} and message.get("content")
    )
    contents: list[dict[str, Any]] = []
    sent_calls: set[str] = set()
    response_ids: dict[str, str | None] = {}

    def append(role: str, parts: list[dict[str, Any]]) -> None:
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    for index, message in enumerate(messages):
        role = message.get("role")
        if role in {"system", "developer"}:
            continue
        if role == "assistant" and message.get("tool_calls"):
            completed = _completed_call_ids(messages, index)
            provider_data = message.get("provider_data", {})
            gemini_data = provider_data.get("gemini", {}) if isinstance(provider_data, dict) else {}
            original_parts = gemini_data.get("parts") if isinstance(gemini_data, dict) else None
            if isinstance(original_parts, list):
                saved_call_ids = gemini_data.get("function_call_ids", [])
                call_ids = iter(saved_call_ids if isinstance(saved_call_ids, list) else [])
                parts = []
                for part in original_parts:
                    if not isinstance(part, dict):
                        continue
                    function = part.get("functionCall")
                    if isinstance(function, dict):
                        call_id = next(call_ids, function.get("id"))
                        if call_id not in completed:
                            continue
                    parts.append(part)
            else:
                parts = ([{"text": message["content"]}] if message.get("content") else [])
                parts.extend({"functionCall": call} for call in message["tool_calls"]
                             if isinstance(call, dict) and call.get("id") in completed)
            append("model", parts)
            sent_calls.update(completed)
            call_response_ids = gemini_data.get("call_response_ids", {})
            if isinstance(call_response_ids, dict):
                response_ids.update({
                    call_id: call_response_ids[call_id]
                    for call_id in completed if call_id in call_response_ids
                })
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if call_id in sent_calls:
                function_response = {
                    "name": message.get("name") or "unknown_tool",
                    "response": {"result": message.get("content", "")},
                }
                response_id = response_ids.get(call_id, call_id)
                if isinstance(response_id, str) and response_id:
                    function_response["id"] = response_id
                append("user", [{"functionResponse": function_response}])
            continue
        if role == "assistant":
            if message.get("content"):
                append("model", [{"text": message["content"]}])
            continue
        if role != "user":
            raise ValueError(f"Unsupported message role: {role}")

        parts = []
        if message.get("content"):
            parts.append({"text": message["content"]})
        for image in message.get("images", []):
            if not isinstance(image, dict):
                raise ValueError("The saved chat contains an invalid image attachment.")
            mime_type, data = image.get("mime_type"), image.get("base64_data")
            if mime_type not in {"image/png", "image/jpeg"} or not isinstance(data, str) or not data:
                raise ValueError("The saved chat contains an invalid PNG or JPEG attachment.")
            parts.append({"inlineData": {"mimeType": mime_type, "data": data}})
        append("user", parts)

    if not contents:
        raise ValueError("A Gemini request needs at least one user or assistant message.")
    return instructions, contents


def _function_declarations(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    declarations = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type", "function") != "function":
            raise ValueError("Gemini supports Oryn's function tools only.")
        name, description, schema = tool.get("name"), tool.get("description"), tool.get("parameters")
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", name)
                or not isinstance(description, str) or not isinstance(schema, dict)):
            raise ValueError("Oryn supplied an invalid function tool definition to Gemini.")
        # Oryn's existing parameters are JSON Schemas; Gemini accepts these directly here.
        declarations.append({
            "name": name,
            "description": description,
            "parametersJsonSchema": schema,
        })
    return declarations


def _response_text(
    lines: Iterable[bytes], on_text_delta: Callable[[str], None] | None = None,
) -> ModelResponse:
    text: list[str] = []
    tool_calls: list[ToolCall] = []
    response_parts: list[dict[str, Any]] = []
    function_call_ids: list[str] = []
    call_response_ids: dict[str, str | None] = {}
    data: list[str] = []
    saw_response_data = False
    finished = False
    blocked_reason: str | None = None
    call_ids: set[str] = set()

    def consume() -> None:
        nonlocal saw_response_data, finished, blocked_reason
        if not data:
            return
        payload = "\n".join(data)
        data.clear()
        if payload == "[DONE]":
            finished = True
            return
        event = json.loads(payload)
        if not isinstance(event, dict):
            raise RuntimeError("Gemini returned an invalid stream event.")
        saw_response_data = True
        prompt_feedback = event.get("promptFeedback")
        if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
            blocked_reason = str(prompt_feedback["blockReason"])
        candidates = event.get("candidates", [])
        if not isinstance(candidates, list):
            raise RuntimeError("Gemini returned an invalid candidate list.")
        if not candidates:
            return
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise RuntimeError("Gemini returned an invalid candidate.")
        content = candidate.get("content", {})
        parts = content.get("parts", []) if isinstance(content, dict) else []
        if not isinstance(parts, list):
            raise RuntimeError("Gemini returned invalid response parts.")
        for original in parts:
            if not isinstance(original, dict):
                raise RuntimeError("Gemini returned an invalid response part.")
            part = dict(original)
            response_parts.append(part)
            delta = part.get("text")
            if isinstance(delta, str) and delta:
                text.append(delta)
                if on_text_delta:
                    on_text_delta(delta)
            function = part.get("functionCall")
            if function is not None:
                if not isinstance(function, dict):
                    raise RuntimeError("Gemini returned an invalid function call.")
                name, arguments = function.get("name"), function.get("args", {})
                provider_call_id = function.get("id")
                if not isinstance(name, str) or not name or not isinstance(arguments, dict):
                    raise RuntimeError("Gemini returned invalid function-call arguments.")
                call_id = (
                    provider_call_id
                    if isinstance(provider_call_id, str) and provider_call_id
                    else f"gemini_{uuid.uuid4().hex}"
                )
                function_call_ids.append(call_id)
                call_response_ids[call_id] = provider_call_id if isinstance(provider_call_id, str) and provider_call_id else None
                if call_id not in call_ids:
                    call_ids.add(call_id)
                    tool_calls.append(ToolCall(call_id, name, arguments))
        if candidate.get("finishReason"):
            finished = True

    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            consume()
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        consume()
    if blocked_reason:
        raise RuntimeError(f"Gemini blocked this request: {blocked_reason}.")
    if not finished:
        raise ProviderRequestError(
            "Gemini stream ended before completion.", retryable=not saw_response_data,
        )
    if not text and not tool_calls:
        raise RuntimeError("Gemini returned no text or function calls.")
    provider_data = (
        {"gemini": {
            "parts": response_parts,
            "function_call_ids": function_call_ids,
            "call_response_ids": call_response_ids,
        }}
        if tool_calls else {}
    )
    return ModelResponse("".join(text), tool_calls, provider_data)


class GeminiProvider:
    """Use Google's Gemini GenerateContent API for text, images, and Oryn tools."""

    label = "Gemini"

    def __init__(
        self, model: str, *, reasoning_effort: str = "default", service_tier: str = "default",
    ):
        if not isinstance(model, str) or not re.fullmatch(r"gemini-[A-Za-z0-9._-]{1,112}", model):
            raise ValueError(
                "Gemini model IDs must start with 'gemini-' and contain only letters, numbers, dots, _ or -."
            )
        self.model = model
        self.configure(reasoning_effort=reasoning_effort, service_tier=service_tier)

    @classmethod
    def cached_models(cls) -> list[tuple[str, str]]:
        """Return the supported Gemini text models exposed in Oryn's model picker."""
        return list(_MODEL_CHOICES)

    def supports_image_input(self) -> bool | None:
        return self.model in dict(_MODEL_CHOICES)

    def model_options(self) -> dict[str, list[tuple[str, str]]]:
        effort = [("default", "Model default")]
        if self.model in dict(_MODEL_CHOICES):
            effort.extend((value, value.title()) for value in ("low", "medium", "high"))
        return {
            "reasoning_effort": effort,
            "service_tier": [("default", "Standard")],
        }

    def configure(self, *, reasoning_effort: str, service_tier: str) -> None:
        if service_tier == "standard":
            service_tier = "default"
        options = self.model_options()
        for field, value in (("reasoning_effort", reasoning_effort), ("service_tier", service_tier)):
            if not isinstance(value, str) or value not in dict(options[field]):
                allowed = ", ".join(dict(options[field]))
                raise ValueError(f"{self.model} {field}: choose {allowed}.")
        self.reasoning_effort = reasoning_effort
        self.service_tier = service_tier

    def setup_warning(self) -> str | None:
        if not _api_key():
            return "Gemini API key is missing. Add GEMINI_API_KEY to ~/.mini-hermes/gemini.env."
        return None

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ModelResponse:
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Gemini request cancelled")
        api_key = _api_key()
        if not api_key:
            raise RuntimeError("Gemini API key is missing. Add GEMINI_API_KEY to ~/.mini-hermes/gemini.env.")
        if any(message.get("images") for message in messages if message.get("role") == "user"):
            if self.supports_image_input() is not True:
                raise ValueError(f"Gemini image input is not confirmed for {self.model}.")
        instructions, contents = _content_history(messages)
        payload: dict[str, Any] = {"contents": contents}
        if instructions:
            payload["systemInstruction"] = {"parts": [{"text": instructions}]}
        if tools:
            payload["tools"] = [{"functionDeclarations": _function_declarations(tools)}]
        if self.reasoning_effort != "default":
            payload["generationConfig"] = {"thinkingConfig": {"thinkingLevel": self.reasoning_effort.upper()}}

        endpoint = f"{API_ROOT}/models/{urllib.parse.quote(self.model, safe='')}:streamGenerateContent?alt=sse"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "X-Goog-Api-Key": api_key,
                "User-Agent": "Oryn/1.0",
            },
        )
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Gemini request cancelled")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                finished = threading.Event()
                watcher = None
                if cancel_event:
                    def interrupt_on_cancel() -> None:
                        while not finished.wait(0.1):
                            if cancel_event.is_set():
                                try:
                                    response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
                                except (AttributeError, OSError):
                                    response.close()
                                return

                    watcher = threading.Thread(target=interrupt_on_cancel, daemon=True)
                    watcher.start()
                try:
                    return _response_text(response, on_text_delta)
                finally:
                    finished.set()
                    if watcher:
                        watcher.join(timeout=0.2)
        except urllib.error.HTTPError as exc:
            if cancel_event and cancel_event.is_set():
                exc.close()
                raise InterruptedError("Gemini request cancelled") from exc
            try:
                detail = exc.read(4000).decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(detail)
                    detail = parsed.get("error", {}).get("message", detail)
                except (json.JSONDecodeError, AttributeError):
                    pass
            finally:
                retry_after = retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
                exc.close()
            detail = str(detail).replace(api_key, "[redacted]")[:1200]
            retryable = exc.code == 429 or exc.code in {500, 502, 503, 504}
            raise ProviderRequestError(
                f"Gemini API HTTP {exc.code}: {detail}", retryable=retryable,
                retry_after=retry_after,
            ) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("Gemini request cancelled") from exc
            reason = getattr(exc, "reason", exc)
            retryable = not isinstance(reason, ssl.SSLError)
            raise ProviderRequestError(
                f"Gemini network request failed: {reason}", retryable=retryable,
            ) from exc
