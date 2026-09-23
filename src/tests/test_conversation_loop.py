import unittest
from unittest.mock import Mock

from src.agent.conversation_loop import run_turn


class ConversationLoopTests(unittest.TestCase):
    def test_sends_history_to_provider_and_returns_answer(self):
        history = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "How are you?"},
        ]
        complete = Mock(return_value="Doing well.")

        answer = run_turn(history, complete)

        complete.assert_called_once_with(history)
        self.assertEqual(answer, "Doing well.")


if __name__ == "__main__":
    unittest.main()
