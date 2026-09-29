"""Small client for the Codex CLI's ChatGPT-authenticated Responses endpoint."""

import argparse
import base64
import errno
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

from src.images import provider_image_parts
from src.providers.types import ModelResponse, ProviderRequestError, ToolCall, retry_after_seconds


ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_VERSION = "0.157.1"
AUTH_FILE = Path.home() / ".codex" / "auth.json"


def _temporary_connection_error(error: object) -> bool:
    if isinstance(error, ssl.SSLError):
        return False
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    return isinstance(error, OSError) and error.errno in {
        errno.EAGAIN, errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNREFUSED,
        errno.ENETUNREACH, errno.EHOSTUNREACH,
    }


def _jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}


def _expired(token: str) -> bool:
    exp = _jwt_payload(token).get("exp")
    return isinstance(exp, (int, float)) and exp * 1000 - 60_000 < time.time() * 1000


def _refresh(auth: dict, auth_file: Path) -> None:
    tokens = auth["tokens"]
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": CLIENT_ID,
    }).encode()
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            refreshed = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("Codex token refresh failed; run `codex login` again") from exc
    if not refreshed.get("access_token"):
        raise RuntimeError("Codex token refresh returned no access token")
    tokens["access_token"] = refreshed["access_token"]
    tokens["refresh_token"] = refreshed.get("refresh_token", tokens["refresh_token"])
    auth["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=auth_file.parent, delete=False
        ) as temp:
            temp_path = Path(temp.name)
            temp.write(json.dumps(auth, indent=2) + "\n")
        os.replace(temp_path, auth_file)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def _read_auth(auth_file: Path) -> dict:
    try:
        auth = json.loads(auth_file.read_text())
        tokens = auth["tokens"]
        if not tokens["access_token"] or not tokens["account_id"]:
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"No valid Codex login at {auth_file}; run `codex login`") from exc
    if _expired(tokens["access_token"]):
        _refresh(auth, auth_file)
    return auth


def auth_setup_warning(auth_file: Path = AUTH_FILE) -> str | None:
    """Check local Codex login shape at startup without refreshing or revealing secrets."""
    if not isinstance(auth_file, (str, os.PathLike)):
        return None
    try:
        auth = json.loads(Path(auth_file).read_text(encoding="utf-8"))
        tokens = auth["tokens"]
        if (not isinstance(tokens, dict)
                or not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]
                or not isinstance(tokens.get("account_id"), str) or not tokens["account_id"]):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return "Codex login is missing or invalid. Run `codex login`; Oryn will not display token values."
    if _expired(tokens["access_token"]) and not tokens.get("refresh_token"):
        return "Codex login has an expired access token without a refresh token. Run `codex login`; token values stay hidden."
    return None


def _response_text(
    lines: Iterable[bytes], on_text_delta: Callable[[str], None] | None = None,
    on_response_data: Callable[[], None] | None = None,
) -> ModelResponse:
    text: list[str] = []
    tool_calls: list[ToolCall] = []
    data: list[str] = []
    finished = False
    saw_response_data = False

    def mark_response_data() -> None:
        nonlocal saw_response_data
        saw_response_data = True
        if on_response_data:
            on_response_data()

    def consume() -> bool:
        if not data:
            return False
        if data == ["[DONE]"]:
            data.clear()
            return True
        event = json.loads("\n".join(data))
        if not isinstance(event, dict):
            raise RuntimeError("Codex returned an invalid stream event")
        kind = event.get("type")
        if kind == "response.function_call_arguments.delta" or (
            kind in {"response.output_item.added", "response.output_item.done"}
            and event.get("item", {}).get("type") == "function_call"
        ):
            mark_response_data()
        if kind == "response.output_text.delta":
            delta = event.get("delta", "")
            if not isinstance(delta, str):
                raise RuntimeError("Codex returned an invalid text delta")
            if delta:
                mark_response_data()
                text.append(delta)
                if on_text_delta:
                    on_text_delta(delta)
        elif kind == "response.output_item.done":
            item = event.get("item", {})
            if item.get("type") == "function_call":
                tool_calls.append(_tool_call(item))
        elif kind == "response.completed":
            output = event.get("response", {}).get("output", [])
            if not text:
                for item in output:
                    for part in item.get("content", []):
                        if part.get("type") == "output_text":
                            delta = part.get("text", "")
                            if not isinstance(delta, str):
                                raise RuntimeError("Codex returned invalid output text")
                            if delta:
                                mark_response_data()
                                text.append(delta)
                                if on_text_delta:
                                    on_text_delta(delta)
            if not tool_calls:
                if any(item.get("type") == "function_call" for item in output):
                    mark_response_data()
                tool_calls.extend(
                    _tool_call(item) for item in output if item.get("type") == "function_call"
                )
        elif kind in {"error", "response.failed"}:
            raise RuntimeError(f"Codex request failed: {event.get('error', event)}")
        data.clear()
        return kind == "response.completed"

    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if consume():
                finished = True
                break
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        finished = consume()
    if not finished:
        raise ProviderRequestError("Codex stream ended before completion", retryable=not saw_response_data)
    if not text and not tool_calls:
        raise RuntimeError("Codex returned no text response")
    return ModelResponse("".join(text), tool_calls)


def _tool_call(item: dict[str, Any]) -> ToolCall:
    try:
        arguments = json.loads(item["arguments"])
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        return ToolCall(item["call_id"], item["name"], arguments)
    except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Codex returned an invalid tool call: {item}") from exc


class CodexProvider:
    """Translate Oryn messages and tools for the Codex Responses endpoint."""

    def __init__(
        self, model: str, auth_file: Path = AUTH_FILE, *,
        reasoning_effort: str = "default", service_tier: str = "default",
    ):
        self.model = model
        self.auth_file = Path(auth_file)
        self.configure(reasoning_effort=reasoning_effort, service_tier=service_tier)

    def _model_catalog(self) -> dict:
        try:
            catalog = json.loads(self.auth_file.with_name("models_cache.json").read_text())
        except (OSError, ValueError):
            return {}
        return catalog if isinstance(catalog, dict) else {}

    def cached_models(self) -> list[tuple[str, str]]:
        """Read the CLI's model catalog without a network request or credentials."""
        models = self._model_catalog().get("models", [])
        if not isinstance(models, list):
            return []
        return [
            (model["slug"], model.get("display_name") or model["slug"])
            for model in models
            if isinstance(model, dict) and model.get("visibility") == "list"
            and isinstance(model.get("slug"), str) and model["slug"]
            and isinstance(model.get("display_name", ""), str)
        ]

    def supports_image_input(self) -> bool | None:
        """Return the local model catalog's explicit image input capability."""
        models = self._model_catalog().get("models", [])
        if not isinstance(models, list):
            return None
        model = next((item for item in models
                      if isinstance(item, dict) and item.get("slug") == self.model), None)
        modalities = model.get("input_modalities") if model else None
        if not isinstance(modalities, list) or not all(isinstance(item, str) for item in modalities):
            return None
        return "image" in modalities

    def model_options(self) -> dict[str, list[tuple[str, str]]]:
        """Offer only capabilities advertised for this model by the Codex catalog."""
        options = {
            "reasoning_effort": [("default", "Model default")],
            "service_tier": [("default", "Standard")],
        }
        models = self._model_catalog().get("models", [])
        if not isinstance(models, list):
            return options
        model = next((m for m in models if isinstance(m, dict) and m.get("slug") == self.model), {})
        for field, source, key in (
            ("reasoning_effort", "supported_reasoning_levels", "effort"),
            ("service_tier", "service_tiers", "id"),
        ):
            entries = model.get(source, [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                value = entry.get(key)
                if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value):
                    continue
                if value in dict(options[field]):
                    continue
                label = entry.get("name") or value.replace("xhigh", "extra high").title()
                description = entry.get("description", "")
                if isinstance(description, str) and description:
                    label = f"{label} — {description}"
                options[field].append((value, str(label)))
        default = model.get("default_reasoning_level")
        if isinstance(default, str) and default in dict(options["reasoning_effort"]):
            options["reasoning_effort"][0] = ("default", f"Model default ({default})")
        return options

    def configure(self, *, reasoning_effort: str, service_tier: str) -> None:
        if not isinstance(reasoning_effort, str) or not isinstance(service_tier, str):
            raise ValueError("Model settings must be strings.")
        options = self.model_options()
        if service_tier == "standard":
            service_tier = "default"
        elif service_tier == "fast" and "priority" in dict(options["service_tier"]):
            service_tier = "priority"
        for field, value in (("reasoning_effort", reasoning_effort), ("service_tier", service_tier)):
            if value not in dict(options[field]):
                allowed = ", ".join(dict(options[field]))
                raise ValueError(f"{self.model} {field}: choose {allowed}. Refresh the Codex model catalog if needed.")
        self.reasoning_effort = reasoning_effort
        self.service_tier = service_tier

    def client_version(self) -> str:
        """Use the version that discovered the models, then the installed CLI."""
        version = self._model_catalog().get("client_version")
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
            try:
                cli = shutil.which("codex")
                version = subprocess.run(
                    [cli, "--version"], capture_output=True, text=True, timeout=2, check=True,
                ).stdout.strip().removeprefix("codex-cli ") if cli else ""
            except (OSError, subprocess.SubprocessError):
                version = ""
        return version if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version) else CODEX_VERSION

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ModelResponse:
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Codex request cancelled")
        auth = _read_auth(self.auth_file)
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Codex request cancelled")
        instructions = "\n\n".join(
            message["content"] for message in messages
            if message["role"] in {"system", "developer"}
        )
        completed_calls = set()
        for index, message in enumerate(messages):
            if message["role"] != "assistant" or not message.get("tool_calls"):
                continue
            outputs = set()
            next_index = index + 1
            while next_index < len(messages) and messages[next_index]["role"] == "tool":
                outputs.add(messages[next_index].get("tool_call_id"))
                next_index += 1
            completed_calls.update(call["id"] for call in message["tool_calls"] if call["id"] in outputs)
        input_messages = []
        sent_calls = set()
        for message in messages:
            role = message["role"]
            if role == "assistant" and message.get("tool_calls"):
                if message.get("content"):
                    input_messages.append({"role": "assistant", "content": message["content"]})
                for call in message["tool_calls"]:
                    if call["id"] not in completed_calls:
                        continue
                    input_messages.append({
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"]),
                    })
                    sent_calls.add(call["id"])
                continue
            if role == "tool":
                if message["tool_call_id"] not in sent_calls:
                    continue
                input_messages.append({
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": message["content"],
                })
                continue
            if role not in {"user", "assistant"}:
                if role in {"system", "developer"}:
                    continue
                raise ValueError(f"Unsupported message role: {role}")
            content = []
            if message.get("content"):
                content.append({
                    "type": "input_text" if role == "user" else "output_text",
                    "text": message["content"],
                })
            images = message.get("images", [])
            if images and role != "user":
                raise ValueError("Only user messages can contain image attachments.")
            if images and self.supports_image_input() is not True:
                raise ValueError(
                    f"The model catalog does not confirm image input for {self.model}. "
                    "Refresh the model catalog or select a model that supports images."
                )
            content.extend(provider_image_parts(images))
            if not content:
                raise ValueError("A user message must contain text or an image.")
            input_messages.append({"role": role, "content": content})
        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": input_messages,
            "stream": True,
            "store": False,
        }
        if self.reasoning_effort != "default":
            payload["reasoning"] = {"effort": self.reasoning_effort}
        if self.service_tier != "default":
            payload["service_tier"] = self.service_tier
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = json.dumps(payload).encode()
        client_version = self.client_version()
        request = urllib.request.Request(
            ENDPOINT,
            data=body,
            headers={
                "Authorization": f"Bearer {auth['tokens']['access_token']}",
                "ChatGPT-Account-ID": auth["tokens"]["account_id"],
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Originator": "codex_cli_rs",
                "Version": client_version,
                "User-Agent": f"codex-cli/{client_version}",
                "OpenAI-Beta": "responses=experimental",
            },
        )
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Codex request cancelled")
        saw_stream_data = False

        def mark_stream_data() -> None:
            nonlocal saw_stream_data
            saw_stream_data = True

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                finished = threading.Event()
                watcher = None
                if cancel_event:
                    def interrupt_on_cancel() -> None:
                        while not finished.wait(0.1):
                            if cancel_event.is_set():
                                try:
                                    # CPython's urllib response exposes its socket; shutdown wakes a blocked SSE read.
                                    response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
                                except (AttributeError, OSError):
                                    response.close()
                                return

                    watcher = threading.Thread(target=interrupt_on_cancel, daemon=True)
                    watcher.start()
                try:
                    return _response_text(response, on_text_delta, mark_stream_data)
                finally:
                    finished.set()
                    if watcher:
                        watcher.join(timeout=0.2)
        except urllib.error.HTTPError as exc:
            if cancel_event and cancel_event.is_set():
                exc.close()
                raise InterruptedError("Codex request cancelled") from exc
            try:
                detail = exc.read(4000).decode("utf-8", errors="replace")
            finally:
                exc.close()
            for secret in auth["tokens"].values():
                if isinstance(secret, str) and len(secret) >= 8:
                    detail = detail.replace(secret, "[redacted]")
            detail = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [redacted]", detail)
            detail = "".join(char for char in detail if char.isprintable() or char in "\n\t")[:2000]
            raise ProviderRequestError(
                f"Codex endpoint returned HTTP {exc.code}: {detail}",
                retryable=not saw_stream_data and exc.code in {429, 500, 502, 503, 504},
                retry_after=retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None),
            ) from exc
        except urllib.error.URLError as exc:
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("Codex request cancelled") from exc
            raise ProviderRequestError(
                f"Could not reach Codex endpoint: {exc.reason}",
                retryable=not saw_stream_data and _temporary_connection_error(exc.reason),
            ) from exc
        except OSError as exc:
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("Codex request cancelled") from exc
            raise ProviderRequestError(
                f"Codex connection failed ({type(exc).__name__}).",
                retryable=not saw_stream_data and _temporary_connection_error(exc),
            ) from exc


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Send one prompt to a Codex model.")
    parser.add_argument("--model", required=True, help="Codex model slug")
    parser.add_argument("prompt", help="Text prompt to send")
    args = parser.parse_args(argv)
    response = CodexProvider(args.model).complete([{"role": "user", "content": args.prompt}])
    print(response.text)


if __name__ == "__main__":
    main()
