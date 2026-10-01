import contextlib
import io
import json
import subprocess
import ssl
import sys
import tempfile
import threading
from types import SimpleNamespace
from pathlib import Path
import unittest
import urllib.error
from unittest.mock import patch

from src.providers import codex
from src.providers.types import ModelResponse, ProviderRequestError, ToolCall, retry_after_seconds
from src.providers.codex import _response_text


class CodexStartupTests(unittest.TestCase):
    def test_startup_auth_check_warns_without_refreshing_or_showing_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            auth_file = Path(folder) / "auth.json"
            warning = codex.auth_setup_warning(auth_file)
            self.assertIn("codex login", warning)
            self.assertNotIn("token", warning.casefold().replace("token values", ""))

            secret = "local-access-token-value"
            auth_file.write_text(json.dumps({"tokens": {
                "access_token": secret, "account_id": "account-id",
            }}))
            with patch.object(codex, "_refresh") as refresh:
                self.assertIsNone(codex.auth_setup_warning(auth_file))
                refresh.assert_not_called()
            self.assertNotIn(secret, repr(warning))

            auth_file.write_text("not-json")
            self.assertIn("invalid", codex.auth_setup_warning(auth_file))


class CodexStreamTests(unittest.TestCase):
    def test_image_messages_use_catalog_capability_and_responses_image_parts(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            catalog = root / "models_cache.json"
            catalog.write_text(json.dumps({"models": [{
                "slug": "gpt-6-astra", "input_modalities": ["text", "image"],
            }]}))
            provider = codex.CodexProvider("gpt-6-astra", root / "auth.json")
            self.assertTrue(provider.supports_image_input())
            from src.images import prepare_image
            pixel = io.BytesIO()
            Image.new("RGB", (2, 2), "red").save(pixel, format="PNG")
            message_image = prepare_image(pixel.getvalue())
            with patch.object(codex, "_read_auth", return_value={"tokens": {
                "access_token": "token", "account_id": "account",
            }}):
                response = io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"Seen"}\n\ndata: {"type":"response.completed","response":{}}\n\n')
                with patch.object(codex.urllib.request, "urlopen", return_value=response) as urlopen:
                    result = provider.complete([{
                        "role": "user", "content": "What is shown?", "images": [message_image],
                    }])
                request = json.loads(urlopen.call_args.args[0].data)
            content = request["input"][0]["content"]
            self.assertEqual(result.text, "Seen")
            self.assertEqual(content[0], {"type": "input_text", "text": "What is shown?"})
            self.assertEqual(content[1]["type"], "input_image")
            self.assertTrue(content[1]["image_url"].startswith("data:image/png;base64,"))
            self.assertEqual(provider.model_options()["service_tier"], [("default", "Standard")])

            catalog.write_text(json.dumps({"models": [{"slug": "gpt-6-astra", "input_modalities": ["text"]}]}))
            self.assertFalse(provider.supports_image_input())
            with patch.object(codex, "_read_auth", return_value={"tokens": {
                "access_token": "token", "account_id": "account",
            }}), patch.object(codex.urllib.request, "urlopen") as urlopen:
                with self.assertRaisesRegex(ValueError, "does not confirm image input"):
                    provider.complete([{"role": "user", "content": "Look", "images": [message_image]}])
                urlopen.assert_not_called()

    def test_model_capabilities_drive_settings_payload_and_client_version(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "models_cache.json"
            cache.write_text(json.dumps({"client_version": "0.157.1", "models": [{
                "slug": "gpt-6-astra", "default_reasoning_level": "low",
                "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
                "service_tiers": [{"id": "priority", "name": "Fast"}],
            }]}))
            provider = codex.CodexProvider("gpt-6-astra", Path(folder) / "auth.json")
            self.assertEqual(provider.model_options()["reasoning_effort"][0], ("default", "Model default (low)"))
            provider.configure(reasoning_effort="high", service_tier="fast")
            for effort in ["ultra", "none", []]:
                with self.assertRaises(ValueError):
                    provider.configure(reasoning_effort=effort, service_tier="default")
                self.assertEqual(provider.reasoning_effort, "high")
                self.assertEqual(provider.service_tier, "priority")
            with patch.object(codex, "_read_auth", return_value={"tokens": {"access_token": "a", "account_id": "b"}}):
                for explicit in [True, False]:
                    if not explicit:
                        provider.configure(reasoning_effort="default", service_tier="standard")
                    response = io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"OK"}\n\ndata: {"type":"response.completed","response":{}}\n\n')
                    with patch.object(codex.urllib.request, "urlopen", return_value=response) as urlopen:
                        provider.complete([{"role": "user", "content": "Hi"}])
                    request = urlopen.call_args.args[0]
                    payload = json.loads(request.data)
                    self.assertEqual(request.get_header("Version"), "0.157.1")
                    self.assertEqual(request.get_header("User-agent"), "codex-cli/0.157.1")
                    if explicit:
                        self.assertEqual(payload["reasoning"], {"effort": "high"})
                        self.assertEqual(payload["service_tier"], "priority")
                    else:
                        self.assertNotIn("reasoning", payload)
                        self.assertNotIn("service_tier", payload)
            cache.write_text(json.dumps({"client_version": "0.157.1\nInjected: value", "models": None}))
            with patch.object(codex.shutil, "which", return_value="/bin/codex"):
                with patch.object(codex.subprocess, "run", return_value=SimpleNamespace(stdout="codex-cli 0.158.0\n")):
                    self.assertEqual(provider.client_version(), "0.158.0")
            self.assertEqual(provider.model_options()["service_tier"], [("default", "Standard")])
            with self.assertRaises(ValueError):
                provider.configure(reasoning_effort="high", service_tier="fast")

    def test_collects_text_deltas_until_completion(self):
        stream = io.BytesIO(
            b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
            b'data: {"type":"response.output_text.delta","delta":" world"}\n\n'
            b'data: {"type":"response.completed","response":{}}\n\n'
        )
        deltas = []
        self.assertEqual(_response_text(stream, deltas.append), ModelResponse("Hello world"))
        self.assertEqual(deltas, ["Hello", " world"])

    def test_rejects_a_stream_that_ends_after_partial_text(self):
        stream = io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"half an answer"}\n\n')
        deltas = []
        with self.assertRaisesRegex(RuntimeError, "ended before completion"):
            _response_text(stream, deltas.append)
        self.assertEqual(deltas, ["half an answer"])

    def test_accepts_done_sentinel_without_a_trailing_blank_line(self):
        stream = io.BytesIO(
            b'data: {"type":"response.output_text.delta","delta":"Ready"}\n\n'
            b'data: [DONE]'
        )
        self.assertEqual(_response_text(stream), ModelResponse("Ready"))

    def test_rejects_non_object_stream_event(self):
        stream = io.BytesIO(b'data: ["unexpected"]\n\n')
        with self.assertRaisesRegex(RuntimeError, "invalid stream event"):
            _response_text(stream)

    def test_parses_function_call_from_stream(self):
        stream = io.BytesIO(
            b'data: {"type":"response.output_item.done","item":{"type":"function_call",'
            b'"call_id":"call_1","name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}}\n\n'
            b'data: {"type":"response.completed","response":{}}\n\n'
        )
        self.assertEqual(
            _response_text(stream),
            ModelResponse("", [ToolCall("call_1", "read_file", {"path": "README.md"})]),
        )

    def test_provider_sends_tool_history_and_schemas_to_codex(self):
        response = io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"Done"}\n\ndata: {"type":"response.completed","response":{}}\n\n')
        messages = [
            {"role": "user", "content": "Read README"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "name": "read_file", "arguments": {"path": "README.md"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "# Mini-Hermes"},
        ]
        tools = [{"type": "function", "name": "read_file"}]
        with patch.object(codex, "_read_auth", return_value={"tokens": {"access_token": "a", "account_id": "b"}}):
            with patch.object(codex.urllib.request, "urlopen", return_value=response) as urlopen:
                result = codex.CodexProvider("gpt-5.6-luna").complete(messages, tools)

        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(result.text, "Done")
        self.assertEqual(payload["tools"], tools)
        self.assertEqual(payload["input"][1]["type"], "function_call")
        self.assertEqual(payload["input"][2]["type"], "function_call_output")
        self.assertEqual(payload["input"][2]["output"], "# Mini-Hermes")

    def test_provider_skips_saved_tool_call_without_output(self):
        response = io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"Ready"}\n\ndata: {"type":"response.completed","response":{}}\n\n')
        messages = [
            {"role": "user", "content": "Find it"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "name": "web_search", "arguments": {"query": "example"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "One result"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_2", "name": "web_search", "arguments": {"query": "again"}}
            ]},
            {"role": "user", "content": "Continue"},
        ]
        with patch.object(codex, "_read_auth", return_value={"tokens": {"access_token": "a", "account_id": "b"}}):
            with patch.object(codex.urllib.request, "urlopen", return_value=response) as urlopen:
                codex.CodexProvider("gpt-5.6-luna").complete(messages)
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual([item.get("call_id") for item in payload["input"] if "call_id" in item],
                         ["call_1", "call_1"])
        self.assertEqual(payload["input"][-1]["content"][0]["text"], "Continue")


class CodexDemoTests(unittest.TestCase):
    def test_demo_sends_model_and_prompt_to_provider(self):
        output = io.StringIO()
        with patch.object(codex, "CodexProvider") as provider, contextlib.redirect_stdout(output):
            provider.return_value.complete.return_value = ModelResponse("Hello!")
            codex.main(["--model", "gpt-5.6-luna", "Say hello"])

        provider.assert_called_once_with("gpt-5.6-luna")
        provider.return_value.complete.assert_called_once_with([
            {"role": "user", "content": "Say hello"}
        ])
        self.assertEqual(output.getvalue(), "Hello!\n")

    def test_module_help_starts_without_making_a_request(self):
        result = subprocess.run(
            [sys.executable, "-m", "src.providers.codex", "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("Send one prompt to a Codex model", result.stdout)


class CodexFailureTests(unittest.TestCase):
    def setUp(self):
        self.auth = patch.object(codex, "_read_auth", return_value={
            "tokens": {"access_token": "a", "account_id": "b"},
        })
        self.auth.start()
        self.addCleanup(self.auth.stop)

    def test_http_error_includes_status_and_body(self):
        error = urllib.error.HTTPError(
            codex.ENDPOINT, 502, "Bad Gateway", {"x-request-id": "req-http-123"},
            io.BytesIO(b"upstream unavailable"),
        )
        with patch.object(codex.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(ProviderRequestError, "HTTP 502: upstream unavailable") as failure:
                codex.CodexProvider("gpt-5.6-luna").complete([{"role": "user", "content": "Hi"}])
        self.assertIn("request_id=req-http-123", str(failure.exception))

    def test_network_error_is_reported_as_endpoint_unavailable(self):
        with patch.object(codex.urllib.request, "urlopen", side_effect=urllib.error.URLError("DNS blocked")):
            with self.assertRaisesRegex(ProviderRequestError, "waiting for HTTP response headers.*DNS blocked"):
                codex.CodexProvider("gpt-5.6-luna").complete([{"role": "user", "content": "Hi"}])

    def test_timeout_reports_whether_it_happened_before_headers_or_during_stream(self):
        provider = codex.CodexProvider("gpt-6-luna")
        messages = [{"role": "user", "content": "Hi"}]
        with patch.object(
            codex.urllib.request, "urlopen",
            side_effect=TimeoutError("The read operation timed out"),
        ) as urlopen:
            with self.assertRaises(ProviderRequestError) as failure:
                provider.complete(messages)
        self.assertIn("opening connection and waiting for HTTP response headers", str(failure.exception))
        self.assertIn("The read operation timed out", str(failure.exception))
        self.assertIn("model output received=false", str(failure.exception))
        self.assertIn("request_id unavailable", str(failure.exception))
        self.assertEqual(urlopen.call_args.kwargs["timeout"], provider.request_timeout_seconds)

        class TimedOutStream:
            headers = {"x-request-id": "req-123456"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def __iter__(self):
                raise TimeoutError("The read operation timed out")
                yield b""

        with patch.object(codex.urllib.request, "urlopen", return_value=TimedOutStream()):
            with self.assertRaises(ProviderRequestError) as failure:
                provider.complete(messages)
        self.assertIn("reading the response stream", str(failure.exception))
        self.assertIn("request_id=req-123456", str(failure.exception))

    def test_cancelled_request_never_contacts_endpoint(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(codex.urllib.request, "urlopen") as urlopen:
            with self.assertRaisesRegex(InterruptedError, "request cancelled"):
                codex.CodexProvider("gpt-5.6-luna").complete(
                    [{"role": "user", "content": "Hi"}], cancel_event=cancel,
                )
        urlopen.assert_not_called()

    def test_cancellation_interrupts_a_blocked_codex_stream(self):
        released = threading.Event()
        cancel = threading.Event()

        class FakeSocket:
            def shutdown(self, _how):
                released.set()

        class StalledResponse:
            fp = SimpleNamespace(raw=SimpleNamespace(_sock=FakeSocket()))

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def __iter__(self):
                yield b'data: {"type":"response.output_text.delta","delta":"partial"}\n'
                yield b"\n"
                if not released.wait(2):
                    raise AssertionError("cancel watcher did not interrupt the stream")
                raise OSError("socket shut down")

        deltas = []

        def collect_and_cancel(delta):
            deltas.append(delta)
            cancel.set()

        with patch.object(codex.urllib.request, "urlopen", return_value=StalledResponse()):
            with self.assertRaisesRegex(InterruptedError, "request cancelled"):
                codex.CodexProvider("gpt-5.6-luna").complete(
                    [{"role": "user", "content": "Hi"}],
                    on_text_delta=collect_and_cancel,
                    cancel_event=cancel,
                )
        self.assertEqual(deltas, ["partial"])

    def test_retry_classification_rejects_partial_streams_permanent_errors_and_secret_echoes(self):
        with patch("src.providers.types.time.time", return_value=0):
            for value, expected in ((None, 0), ("invalid", 0), ("nan", 0), ("inf", 0), ("-2", 0),
                                    ("1.5", 1.5), ("60", 30), ("Thu, 01 Jan 1970 00:00:10 GMT", 10)):
                self.assertEqual(retry_after_seconds(value), expected)
        provider = codex.CodexProvider("gpt-5.6-luna")
        messages = [{"role": "user", "content": "Hi"}]
        for status in (400, 401, 403, 429, 500, 502, 503, 504):
            error = urllib.error.HTTPError(codex.ENDPOINT, status, "Failure", {"Retry-After": "999"},
                                           io.BytesIO(b"upstream unavailable"))
            with patch.object(codex.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(ProviderRequestError) as failure:
                    provider.complete(messages)
            self.assertEqual(failure.exception.retryable, status in {429, 500, 502, 503, 504})
            self.assertEqual(failure.exception.retry_after, 30.0)

        for reason, retryable in ((TimeoutError("Timed out"), True), (ConnectionResetError("Reset"), True),
                                  (ssl.SSLCertVerificationError("Invalid certificate"), False), ("DNS blocked", False)):
            with patch.object(codex.urllib.request, "urlopen", side_effect=urllib.error.URLError(reason)):
                with self.assertRaises(ProviderRequestError) as failure:
                    provider.complete(messages)
            self.assertEqual(failure.exception.retryable, retryable)

        class InterruptedStream:
            def __init__(self, partial):
                self.partial = partial
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return None
            def __iter__(self):
                if self.partial:
                    yield b'data: {"type":"response.output_text.delta","delta":"partial"}\n'
                    yield b"\n"
                raise ConnectionResetError("Reset stream")

        for partial in (False, True):
            with patch.object(codex.urllib.request, "urlopen", return_value=InterruptedStream(partial)):
                with self.assertRaises(ProviderRequestError) as failure:
                    provider.complete(messages)
            self.assertEqual(failure.exception.retryable, not partial)

        for stream, retryable in (
            (b'data: {"type":"response.created"}\n\n', True),
            (b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n', False),
            (b'data: {"type":"response.function_call_arguments.delta","delta":"{"}\n\n', False),
        ):
            with patch.object(codex.urllib.request, "urlopen", return_value=io.BytesIO(stream)):
                with self.assertRaises(ProviderRequestError) as failure:
                    provider.complete(messages)
            self.assertEqual(failure.exception.retryable, retryable)

        secret = "sensitive-access-token"
        error = urllib.error.HTTPError(codex.ENDPOINT, 400, "Failure", {}, io.BytesIO(
            f"Bearer another-secret-token echoed {secret} \x1b[2J".encode() + b"x" * 9000))
        with patch.object(codex, "_read_auth", return_value={"tokens": {"access_token": secret, "account_id": "account"}}), \
             patch.object(codex.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(ProviderRequestError) as failure:
                provider.complete(messages)
        self.assertNotIn(secret, str(failure.exception))
        self.assertNotIn("another-secret-token", str(failure.exception))
        self.assertNotIn("\x1b", str(failure.exception))
        self.assertLess(len(str(failure.exception)), 2100)


if __name__ == "__main__":
    unittest.main()
