"""Small client for the Codex CLI's ChatGPT-authenticated Responses endpoint."""

import argparse
import base64
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from src.providers.types import ModelResponse, ToolCall


ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_VERSION = "0.144.1"
AUTH_FILE = Path.home() / ".codex" / "auth.json"


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


def _response_text(lines: Iterable[bytes]) -> ModelResponse:
    text: list[str] = []
    tool_calls: list[ToolCall] = []
    data: list[str] = []

    def consume() -> bool:
        if not data:
            return False
        if data == ["[DONE]"]:
            data.clear()
            return True
        event = json.loads("\n".join(data))
        kind = event.get("type")
        if kind == "response.output_text.delta":
            text.append(event.get("delta", ""))
        elif kind == "response.output_item.done":
            item = event.get("item", {})
            if item.get("type") == "function_call":
                tool_calls.append(_tool_call(item))
        elif kind == "response.completed" and not text and not tool_calls:
            for item in event.get("response", {}).get("output", []):
                if item.get("type") == "function_call":
                    tool_calls.append(_tool_call(item))
                else:
                    for part in item.get("content", []):
                        if part.get("type") == "output_text":
                            text.append(part.get("text", ""))
        elif kind in {"error", "response.failed"}:
            raise RuntimeError(f"Codex request failed: {event.get('error', event)}")
        data.clear()
        return kind == "response.completed"

    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if consume():
                break
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        consume()
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
    """Translate Mini-Hermes messages and tools for the Codex Responses endpoint."""

    def __init__(self, model: str, auth_file: Path = AUTH_FILE):
        self.model = model
        self.auth_file = Path(auth_file)

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> ModelResponse:
        auth = _read_auth(self.auth_file)
        instructions = "\n\n".join(
            message["content"] for message in messages
            if message["role"] in {"system", "developer"}
        )
        input_messages = []
        for message in messages:
            role = message["role"]
            if role == "assistant" and message.get("tool_calls"):
                if message.get("content"):
                    input_messages.append({"role": "assistant", "content": message["content"]})
                for call in message["tool_calls"]:
                    input_messages.append({
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"]),
                    })
                continue
            if role == "tool":
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
            input_messages.append({
                "role": role,
                "content": [{
                    "type": "input_text" if role == "user" else "output_text",
                    "text": message["content"],
                }],
            })
        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": input_messages,
            "stream": True,
            "store": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            ENDPOINT,
            data=body,
            headers={
                "Authorization": f"Bearer {auth['tokens']['access_token']}",
                "ChatGPT-Account-ID": auth["tokens"]["account_id"],
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Originator": "codex_cli_rs",
                "Version": CODEX_VERSION,
                "User-Agent": f"codex-cli/{CODEX_VERSION}",
                "OpenAI-Beta": "responses=experimental",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return _response_text(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Codex endpoint returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach Codex endpoint: {exc.reason}") from exc


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Send one prompt to a Codex model.")
    parser.add_argument("--model", required=True, help="Codex model slug")
    parser.add_argument("prompt", help="Text prompt to send")
    args = parser.parse_args(argv)
    response = CodexProvider(args.model).complete([{"role": "user", "content": args.prompt}])
    print(response.text)


if __name__ == "__main__":
    main()
