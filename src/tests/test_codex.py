import contextlib
import io
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from src.providers import codex
from src.providers.types import ModelResponse, ToolCall
from src.providers.codex import _response_text


class CodexStreamTests(unittest.TestCase):
    def test_collects_text_deltas_until_completion(self):
        stream = io.BytesIO(
            b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
            b'data: {"type":"response.output_text.delta","delta":" world"}\n\n'
            b'data: {"type":"response.completed","response":{}}\n\n'
        )
        self.assertEqual(_response_text(stream), ModelResponse("Hello world"))

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


if __name__ == "__main__":
    unittest.main()
