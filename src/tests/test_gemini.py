"""Gemini protocol, multimodal history, and shared tool-cycle checks."""

import io
import json
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from src.agent.conversation_loop import run_turn
from src.images import prepare_image
from src.providers.gemini import GeminiProvider, _content_history, _response_text
from src.providers.router import available_models, provider_for_model
from src.providers.types import ModelResponse, ProviderRequestError, ToolCall
from src.tools.registry import tool_schemas


KEY = "test-gemini-key-not-real"


def sse(value: dict) -> io.BytesIO:
    return io.BytesIO(("data: " + json.dumps(value) + "\n\n").encode())


class GeminiProviderTests(unittest.TestCase):
    def test_picker_and_router_expose_gemini_as_a_real_provider(self):
        models = {model: (label, provider) for model, label, provider in available_models()}
        self.assertEqual(models["gemini-3.8-flash"], ("Gemini 3.8 Flash", "Gemini"))
        self.assertIsInstance(provider_for_model("gemini-3.8-flash"), GeminiProvider)
        self.assertEqual(provider_for_model("gpt-5.6-luna").label, "ChatGPT")

    def test_stream_parser_emits_text_and_preserves_signed_function_parts(self):
        deltas = []
        result = _response_text(sse({"candidates": [{
            "content": {"role": "model", "parts": [
                {"text": "Reading. "},
                {"functionCall": {
                    "id": "call-7", "name": "read_file", "args": {"path": "README.md"},
                }, "thoughtSignature": "opaque-signature"},
            ]},
            "finishReason": "STOP",
        }]}), deltas.append)
        self.assertEqual(result.text, "Reading. ")
        self.assertEqual(deltas, ["Reading. "])
        self.assertEqual(result.tool_calls, [ToolCall("call-7", "read_file", {"path": "README.md"})])
        self.assertEqual(result.provider_data["gemini"]["parts"][1]["thoughtSignature"], "opaque-signature")

    def test_stream_parser_rejects_incomplete_response(self):
        with self.assertRaisesRegex(ProviderRequestError, "ended before completion"):
            _response_text(io.BytesIO(b'data: {"candidates":[]}\n\n'))

    def test_missing_key_fails_before_network_request(self):
        provider = GeminiProvider("gemini-3.8-flash")
        with patch("src.providers.gemini._api_key", return_value=""), \
             patch("src.providers.gemini.urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY.*gemini.env"):
                provider.complete([{"role": "user", "content": "Hello"}])
            urlopen.assert_not_called()

    def test_reads_api_key_from_documented_user_config_file(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            config = home / ".mini-hermes"
            config.mkdir()
            (config / "gemini.env").write_text("GEMINI_API_KEY=local-test-key\n", encoding="utf-8")
            with patch("src.security.secrets.Path.home", return_value=home), \
                 patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
                self.assertIsNone(GeminiProvider("gemini-3.8-flash").setup_warning())

    def test_request_uses_google_header_and_converts_images_tools_and_history(self):
        provider = GeminiProvider("gemini-3.8-flash", reasoning_effort="high")
        image_buffer = io.BytesIO()
        Image.new("RGB", (1, 1), "red").save(image_buffer, format="PNG")
        image = prepare_image(image_buffer.getvalue())
        messages = [
            {"role": "system", "content": "System prompt."},
            {"role": "developer", "content": "Project rules."},
            {"role": "user", "content": "Read the file, then inspect this image.", "images": [image]},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "fc-1", "name": "read_file", "arguments": {"path": "README.md"}},
            ], "provider_data": {"gemini": {"parts": [{
                "functionCall": {"id": "fc-1", "name": "read_file", "args": {"path": "README.md"}},
                "thoughtSignature": "keep-me",
            }]} }},
            {"role": "tool", "tool_call_id": "fc-1", "name": "read_file", "content": "file text"},
        ]
        with patch("src.providers.gemini._api_key", return_value=KEY), \
             patch("src.providers.gemini.urllib.request.urlopen", return_value=sse({
                 "candidates": [{"content": {"parts": [{"text": "Done"}]}, "finishReason": "STOP"}],
             })) as urlopen:
            response = provider.complete(messages, tool_schemas())

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-goog-api-key"), KEY)
        self.assertNotIn(KEY, request.full_url)
        self.assertTrue(request.full_url.endswith(":streamGenerateContent?alt=sse"))
        payload = json.loads(request.data)
        self.assertEqual(payload["systemInstruction"]["parts"], [{"text": "System prompt.\n\nProject rules."}])
        self.assertEqual(payload["generationConfig"], {"thinkingConfig": {"thinkingLevel": "HIGH"}})
        declarations = payload["tools"][0]["functionDeclarations"]
        self.assertEqual(declarations[0]["name"], "terminal")
        self.assertIn("parametersJsonSchema", declarations[0])
        self.assertNotIn("strict", declarations[0])
        model_parts = payload["contents"][1]["parts"]
        self.assertEqual(model_parts[0]["thoughtSignature"], "keep-me")
        self.assertEqual(payload["contents"][2]["parts"][0]["functionResponse"], {
            "id": "fc-1", "name": "read_file", "response": {"result": "file text"},
        })
        self.assertEqual(response, ModelResponse("Done"))

    def test_tool_call_round_trip_through_shared_loop_preserves_signature(self):
        provider = GeminiProvider("gemini-3.8-flash")
        function_call = sse({"candidates": [{
            "content": {"role": "model", "parts": [{
                "functionCall": {"id": "fc-read", "name": "read_file", "args": {"path": "note.txt"}},
                "thoughtSignature": "signed-context",
            }]}, "finishReason": "STOP",
        }]})
        final_text = sse({"candidates": [{
            "content": {"role": "model", "parts": [{"text": "The note says hello."}]},
            "finishReason": "STOP",
        }]})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "note.txt").write_text("hello", encoding="utf-8")
            messages = [{"role": "user", "content": "Read note.txt"}]
            with patch("src.providers.gemini._api_key", return_value=KEY), \
                 patch("src.providers.gemini.urllib.request.urlopen", side_effect=[function_call, final_text]) as urlopen:
                answer = run_turn(
                    messages, provider.complete, tool_schemas(), root,
                    lambda _command: False, lambda *_args: False,
                )
            self.assertEqual(answer, "The note says hello.")
            second_payload = json.loads(urlopen.call_args_list[1].args[0].data)
            self.assertEqual(second_payload["contents"][1]["parts"][0]["thoughtSignature"], "signed-context")
            self.assertEqual(second_payload["contents"][2]["parts"][0]["functionResponse"]["id"], "fc-read")
            self.assertEqual(second_payload["contents"][2]["parts"][0]["functionResponse"]["response"], {
                "result": "hello",
            })
            self.assertTrue(messages[1]["provider_data"]["gemini"]["parts"])

    def test_missing_api_call_id_keeps_signed_part_unchanged_and_omits_response_id(self):
        provider = GeminiProvider("gemini-3.8-flash")
        function_call = sse({"candidates": [{
            "content": {"role": "model", "parts": [{
                "functionCall": {"name": "read_file", "args": {"path": "note.txt"}},
                "thoughtSignature": "signed-no-id",
            }]}, "finishReason": "STOP",
        }]})
        final_text = sse({"candidates": [{
            "content": {"role": "model", "parts": [{"text": "The note says hello."}]},
            "finishReason": "STOP",
        }]})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "note.txt").write_text("hello", encoding="utf-8")
            messages = [{"role": "user", "content": "Read note.txt"}]
            with patch("src.providers.gemini._api_key", return_value=KEY), \
                 patch("src.providers.gemini.urllib.request.urlopen", side_effect=[function_call, final_text]) as urlopen:
                self.assertEqual(run_turn(
                    messages, provider.complete, tool_schemas(), root,
                    lambda _command: False, lambda *_args: False,
                ), "The note says hello.")
            second_payload = json.loads(urlopen.call_args_list[1].args[0].data)
            model_call = second_payload["contents"][1]["parts"][0]
            self.assertEqual(model_call, {
                "functionCall": {"name": "read_file", "args": {"path": "note.txt"}},
                "thoughtSignature": "signed-no-id",
            })
            response = second_payload["contents"][2]["parts"][0]["functionResponse"]
            self.assertNotIn("id", response)
            self.assertEqual(response["response"], {"result": "hello"})

    def test_retryable_http_error_is_bounded_and_secret_is_not_echoed(self):
        headers = Message()
        headers["Retry-After"] = "2"
        body = json.dumps({"error": {"message": f"Invalid key {KEY}"}}).encode()
        error = urllib.error.HTTPError("https://example.test", 429, "Quota", headers, io.BytesIO(body))
        with patch("src.providers.gemini._api_key", return_value=KEY), \
             patch("src.providers.gemini.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ProviderRequestError) as raised:
                GeminiProvider("gemini-3.8-flash").complete([{"role": "user", "content": "Hello"}])
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.retry_after, 2)
        self.assertNotIn(KEY, str(raised.exception))

    def test_gemini_model_rejects_codex_only_priority_setting(self):
        provider = GeminiProvider("gemini-3.8-flash")
        with self.assertRaisesRegex(ValueError, "service_tier"):
            provider.configure(reasoning_effort="default", service_tier="priority")

    def test_picker_models_expose_supported_thinking_levels(self):
        for model, _label in GeminiProvider.cached_models():
            levels = [value for value, _name in GeminiProvider(model).model_options()["reasoning_effort"]]
            self.assertEqual(levels, ["default", "low", "medium", "high"], model)
        custom = GeminiProvider("gemini-custom-model")
        self.assertEqual(
            custom.model_options()["reasoning_effort"],
            [("default", "Model default")],
        )


if __name__ == "__main__":
    unittest.main()
