import contextlib
import io
import subprocess
import sys
import unittest
from unittest.mock import patch

from src.providers import codex
from src.providers.codex import _response_text


class CodexStreamTests(unittest.TestCase):
    def test_collects_text_deltas_until_completion(self):
        stream = io.BytesIO(
            b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
            b'data: {"type":"response.output_text.delta","delta":" world"}\n\n'
            b'data: {"type":"response.completed","response":{}}\n\n'
        )
        self.assertEqual(_response_text(stream), "Hello world")


class CodexDemoTests(unittest.TestCase):
    def test_demo_sends_model_and_prompt_to_provider(self):
        output = io.StringIO()
        with patch.object(codex, "CodexProvider") as provider, contextlib.redirect_stdout(output):
            provider.return_value.complete.return_value = "Hello!"
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
