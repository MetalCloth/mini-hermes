import unittest
from unittest.mock import patch

from src.agent.context import CONTEXT_TARGET_TOKENS, MAX_CONTEXT_TOKENS, estimate_context_tokens, select_context


class ContextSelectionTests(unittest.TestCase):
    def test_context_compacts_at_200k_and_targets_180k(self):
        self.assertEqual(MAX_CONTEXT_TOKENS, 200_000)
        self.assertEqual(CONTEXT_TARGET_TOKENS, 180_000)

    def test_tool_definitions_reduce_space_available_for_older_turns(self):
        system = [{"role": "system", "content": "s" * 20}]
        older_turn = [
            {"role": "user", "content": "old " * 20},
            {"role": "assistant", "content": "answer " * 20},
        ]
        current_turn = [{"role": "user", "content": "latest request"}]
        tools = [{"name": "large_tool", "description": "tool schema " * 30}]
        budget = estimate_context_tokens(system + current_turn, tools)

        with patch("src.agent.context.MAX_CONTEXT_TOKENS", budget):
            selected = select_context(system + older_turn + current_turn, tools)

        self.assertEqual(selected, system + current_turn)

    def test_reports_when_instructions_and_tools_alone_exceed_budget(self):
        with patch("src.agent.context.MAX_CONTEXT_TOKENS", 100):
            with self.assertRaisesRegex(ValueError, "System instructions and tool definitions"):
                select_context(
                    [{"role": "system", "content": "Keep project rules"}],
                    [{"name": "large_tool", "description": "schema " * 500}],
                )

    def test_non_latin_and_image_content_are_counted_not_base64_characters(self):
        messages = [{"role": "user", "content": "नमस्ते 世界", "images": [{
            "name": "screen.png", "mime_type": "image/png", "width": 1024,
            "height": 1024, "size_bytes": 3, "base64_data": "YWJj",
        }]}]
        with patch("src.agent.context._encoding", return_value=None):
            count = estimate_context_tokens(messages)
        self.assertGreater(count, len("नमस्ते 世界".encode("utf-8")))
        self.assertLess(count, len("YWJj" * 100_000))


if __name__ == "__main__":
    unittest.main()
