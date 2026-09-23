import io
import unittest

from src.providers.codex import _response_text


class CodexStreamTests(unittest.TestCase):
    def test_collects_text_deltas_until_completion(self):
        stream = io.BytesIO(
            b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
            b'data: {"type":"response.output_text.delta","delta":" world"}\n\n'
            b'data: {"type":"response.completed","response":{}}\n\n'
        )
        self.assertEqual(_response_text(stream), "Hello world")


if __name__ == "__main__":
    unittest.main()
