"""Browser dashboard checks without model or network credentials."""

import http.client
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.agent.conversation_loop import TurnLimits
from src.providers.types import ModelResponse, ToolCall
from src.mcp.discovery import MCPServerConfig, SERVER_NAMES, load_enabled_servers
from src.session.sqlite_store import SQLiteSessionStore
from src.web_app import DashboardHandler, DashboardServer


class TextProvider:
    def __init__(self, model: str) -> None:
        self.model = model

    def complete(self, messages, tools, on_text_delta=None, cancel_event=None):
        if on_text_delta:
            on_text_delta("Hello ")
            on_text_delta("from Mini-Hermes.")
        return ModelResponse(text="Hello from Mini-Hermes.")


class WriteProvider:
    def __init__(self, model: str) -> None:
        self.model = model

    def complete(self, messages, tools, on_text_delta=None, cancel_event=None):
        if any(message["role"] == "tool" for message in messages):
            if on_text_delta:
                on_text_delta("The file is ready.")
            return ModelResponse(text="The file is ready.")
        return ModelResponse(tool_calls=[ToolCall("write-1", "write_file", {
            "path": "note.txt", "content": "hello\n",
        })])


class ApprovalAwareWriteProvider(WriteProvider):
    def complete(self, messages, tools, on_text_delta=None, cancel_event=None):
        if any(message["role"] == "tool" for message in messages):
            answer = "No file was changed." if "cancelled" in messages[-1]["content"] else "The file is ready."
            if on_text_delta:
                on_text_delta(answer)
            return ModelResponse(text=answer)
        return super().complete(messages, tools, on_text_delta, cancel_event)


class PartialFailureProvider:
    def __init__(self, model: str) -> None:
        self.model = model

    def complete(self, messages, tools, on_text_delta=None, cancel_event=None):
        if on_text_delta:
            on_text_delta("Partial answer before a 502.")
        raise RuntimeError("Codex endpoint returned HTTP 502: temporary error")


class BlockingProvider:
    def __init__(self, model: str) -> None:
        self.model = model

    def complete(self, messages, tools, on_text_delta=None, cancel_event=None):
        if on_text_delta:
            on_text_delta("Text before stop.")
        while cancel_event and not cancel_event.wait(0.01):
            pass
        raise InterruptedError("Codex request cancelled")


class FakeMCPClient:
    def __init__(self):
        self.configs = [MCPServerConfig(name, "fake", (), {}, enabled=True, access="Test access")
                        for name in SERVER_NAMES]

    def set_enabled(self, enabled):
        self.configs = [replace(config, enabled=config.name in enabled) for config in self.configs]
        return self.status_snapshot()

    def status_snapshot(self):
        return [{
            "name": config.name,
            "enabled": config.enabled,
            "state": "connected" if config.enabled else "disabled",
            "message": "Connected." if config.enabled else "Disabled in Oryn settings.",
            "tool_count": 1 if config.enabled else 0,
            "access": config.access,
        } for config in self.configs]

    def close(self):
        pass


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        store = SQLiteSessionStore(root / "sessions.sqlite3")
        self.server = DashboardServer(root, 0, store, self.provider_type)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.token = self.request("GET", "/api/bootstrap")[1]["token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    provider_type = TextProvider

    def request(self, method, path, body=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if body is not None:
            headers = {
                "Content-Type": "application/json",
                "X-Mini-Hermes-Token": self.token if token is None else token,
            }
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        content = response.read()
        status = response.status
        conn.close()
        return status, json.loads(content)

    def create_session(self):
        status, data = self.request("POST", "/api/sessions", {})
        self.assertEqual(status, 201)
        return data["id"]

    def stream(self, session_id, prompt):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/turns", json.dumps({
            "session_id": session_id, "content": prompt,
        }), {"Content-Type": "application/json", "X-Mini-Hermes-Token": self.token})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        return conn, response

    def next_event(self, response):
        """Progress metadata is separate from the next action/delta/outcome."""
        for line in iter(response.readline, b""):
            event = json.loads(line)
            if event["type"] != "progress":
                return event
        self.fail("The stream ended before the next event.")

    def test_static_page_sessions_and_streamed_reply(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        page = conn.getresponse()
        self.assertEqual(page.status, 200)
        self.assertIn(b'<div id="root"></div>', page.read())
        conn.close()
        status, _ = self.request("POST", "/api/sessions", {}, token="invalid")
        self.assertEqual(status, 403)

        session_id = self.create_session()
        conn, response = self.stream(session_id, "Say hello")
        events = [json.loads(line) for line in response]
        conn.close()
        self.assertEqual([event["text"] for event in events if event["type"] == "delta"],
                         ["Hello ", "from Mini-Hermes."])
        self.assertEqual(events[-1], {"type": "done", "answer": "Hello from Mini-Hermes."})
        status, data = self.request("GET", f"/api/sessions/{session_id}")
        self.assertEqual(status, 200)
        self.assertEqual([m["role"] for m in data["messages"]], ["user", "assistant"])
        self.assertEqual(data["messages"][1]["content"], "Hello from Mini-Hermes.")

    def test_mcp_preferences_endpoint_is_validated_and_persisted(self):
        self.server.mcp_client = FakeMCPClient()
        self.server.mcp_settings_file = Path(self.temp.name) / "mcp-settings.json"
        status, bootstrap = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual([server["name"] for server in bootstrap["mcp_servers"]], list(SERVER_NAMES))

        status, invalid = self.request("POST", "/api/mcp", {"enabled": {"arbitrary": True}})
        self.assertEqual(status, 400)
        self.assertIn("known MCP server", invalid["error"])

        enabled = {name: name in {"context7", "playwright"} for name in SERVER_NAMES}
        status, data = self.request("POST", "/api/mcp", {"enabled": enabled})
        self.assertEqual(status, 200)
        self.assertEqual([server["enabled"] for server in data["mcp_servers"]], list(enabled.values()))
        self.assertEqual(load_enabled_servers(self.server.mcp_settings_file), {"context7", "playwright"})

    def test_approval_waits_then_writes(self):
        self.server.provider_factory = WriteProvider
        session_id = self.create_session()
        conn, response = self.stream(session_id, "Create a note")
        first = self.next_event(response)
        approval = self.next_event(response)
        self.assertEqual(first["type"], "tool_start")
        self.assertEqual(approval["type"], "approval")
        self.assertEqual(approval["content"], "hello\n")
        self.assertFalse((Path(self.temp.name) / "note.txt").exists())
        status, rejected = self.request("POST", "/api/turns", {"session_id": session_id, "content": "Another prompt"})
        self.assertEqual(status, 409)
        self.assertIn("already answering", rejected["error"])

        status, data = self.request("POST", "/api/approvals", {"id": approval["id"], "allow": True})
        self.assertEqual(status, 200)
        self.assertTrue(data["allowed"])
        events = [json.loads(line) for line in response]
        conn.close()
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual((Path(self.temp.name) / "note.txt").read_text(), "hello\n")
        status, data = self.request("GET", f"/api/sessions/{session_id}")
        self.assertEqual([m["role"] for m in data["messages"]],
                         ["user", "assistant", "tool", "assistant"])
        self.assertEqual([m["content"] for m in data["messages"] if m["role"] == "user"], ["Create a note"])

    def test_denied_write_is_recorded_and_never_changes_the_file(self):
        self.server.provider_factory = ApprovalAwareWriteProvider
        session_id = self.create_session()
        conn, response = self.stream(session_id, "Create a note")
        self.assertEqual(self.next_event(response)["type"], "tool_start")
        approval = self.next_event(response)
        status, decision = self.request("POST", "/api/approvals", {
            "id": approval["id"], "allow": False,
        })
        self.assertEqual(status, 200)
        self.assertFalse(decision["allowed"])
        events = [json.loads(line) for line in response]
        conn.close()

        target = Path(self.temp.name) / "note.txt"
        self.assertFalse(target.exists())
        self.assertIn("cancelled", next(event["result"] for event in events if event["type"] == "tool_result"))
        self.assertEqual(events[-1], {"type": "done", "answer": "No file was changed."})
        status, data = self.request("GET", f"/api/sessions/{session_id}")
        self.assertEqual(status, 200)
        self.assertIn("cancelled", next(message["content"] for message in data["messages"]
                                         if message["role"] == "tool"))

    def test_stopping_during_write_approval_denies_the_write(self):
        self.server.provider_factory = WriteProvider
        session_id = self.create_session()
        conn, response = self.stream(session_id, "Create a note")
        self.assertEqual(self.next_event(response)["type"], "tool_start")
        approval = self.next_event(response)
        self.assertEqual(approval["type"], "approval")
        status, result = self.request("POST", "/api/turns/cancel", {"session_id": session_id})
        self.assertEqual(status, 200)
        self.assertTrue(result["cancelled"])
        events = [json.loads(line) for line in response]
        conn.close()

        self.assertFalse((Path(self.temp.name) / "note.txt").exists())
        self.assertIn("cancelled", next(event["result"] for event in events if event["type"] == "tool_result"))
        self.assertEqual(events[-1], {"type": "cancelled"})
        status, data = self.request("GET", f"/api/sessions/{session_id}")
        self.assertEqual(status, 200)
        assistant_call = next(message for message in data["messages"]
                              if message["role"] == "assistant" and message.get("turn_status"))
        self.assertEqual(assistant_call["turn_status"], "cancelled")

    def test_stop_during_stream_saves_partial_reply_with_cancelled_status(self):
        self.server.provider_factory = BlockingProvider
        session_id = self.create_session()
        conn, response = self.stream(session_id, "Tell me something")
        first = self.next_event(response)
        self.assertEqual(first, {"type": "delta", "text": "Text before stop."})

        status, result = self.request("POST", "/api/turns/cancel", {"session_id": session_id})
        self.assertEqual(status, 200)
        self.assertTrue(result["cancelled"])
        events = [json.loads(line) for line in response]
        conn.close()
        self.assertEqual(events[-1], {"type": "cancelled"})

        status, data = self.request("GET", f"/api/sessions/{session_id}")
        self.assertEqual(status, 200)
        partial = data["messages"][-1]
        self.assertEqual(partial["content"], "Text before stop.")
        self.assertEqual(partial["turn_status"], "cancelled")

    def test_provider_failure_saves_streamed_partial_text_as_failed(self):
        self.server.provider_factory = PartialFailureProvider
        session_id = self.create_session()
        conn, response = self.stream(session_id, "Answer this")
        events = [json.loads(line) for line in response]
        conn.close()
        self.assertEqual(next(event for event in events if event["type"] == "delta"),
                         {"type": "delta", "text": "Partial answer before a 502."})
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("HTTP 502", events[-1]["message"])

        status, data = self.request("GET", f"/api/sessions/{session_id}")
        self.assertEqual(status, 200)
        partial = data["messages"][-1]
        self.assertEqual(partial["content"], "Partial answer before a 502.")
        self.assertEqual(partial["turn_status"], "failed")

    def test_budget_pause_preserves_approved_work_and_expires_unapproved_work(self):
        self.server.provider_factory = WriteProvider
        self.server.turn_limits = TurnLimits(max_rounds=1)
        session_id = self.create_session()
        conn, response = self.stream(session_id, "Create a note")
        first = json.loads(response.readline())
        self.assertEqual(first["type"], "progress")
        self.assertIn("1 rounds", first["message"])
        self.assertEqual(self.next_event(response)["type"], "tool_start")
        approval = self.next_event(response)
        status, _ = self.request("POST", "/api/approvals", {"id": approval["id"], "allow": True})
        self.assertEqual(status, 200)
        events = [json.loads(line) for line in response]
        conn.close()
        self.assertEqual(events[-1]["type"], "paused")
        note = Path(self.temp.name) / "note.txt"
        original = note.read_bytes()
        saved = self.server.store.load_messages(session_id)
        self.assertEqual(saved[1]["turn_status"], "paused")
        self.assertEqual(saved[1]["tool_calls"][0]["id"], saved[2]["tool_call_id"])

        self.server.provider_factory = TextProvider
        conn, response = self.stream(session_id, "Continue")
        events = [json.loads(line) for line in response]
        conn.close()
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(note.read_bytes(), original)

        class LateWriteProvider(TextProvider):
            def complete(self, messages, tools, on_text_delta=None, cancel_event=None):
                return ModelResponse(tool_calls=[ToolCall("late", "write_file", {
                    "path": "late.txt", "content": "Must not be written",
                })])
        self.server.provider_factory = LateWriteProvider
        self.server.turn_limits = TurnLimits(max_turn_seconds=1)
        conn, response = self.stream(session_id, "Write another note")
        self.assertEqual(self.next_event(response)["type"], "tool_start")
        self.assertEqual(self.next_event(response)["type"], "approval")
        events = [json.loads(line) for line in response]
        conn.close()
        self.assertEqual(events[-1]["type"], "paused")
        self.assertFalse((Path(self.temp.name) / "late.txt").exists())
        self.assertEqual(self.server.approvals, {})

        # Losing the UI result notification must preserve one copy of the executed pair/text.
        class IntroWriteProvider(WriteProvider):
            def complete(self, messages, tools, on_text_delta=None, cancel_event=None):
                return ModelResponse("Creating your note.", [ToolCall("disconnected", "write_file", {
                    "path": "after-disconnect.txt", "content": "Created",
                })])

        original_event = DashboardHandler._event

        def failed_result_event(handler, kind, **fields):
            if kind == "tool_result":
                raise BrokenPipeError("Result consumer disconnected")
            return original_event(handler, kind, **fields)

        self.server.provider_factory = IntroWriteProvider
        self.server.turn_limits = TurnLimits(max_rounds=1)
        disconnected_session = self.create_session()
        with patch.object(DashboardHandler, "_event", failed_result_event):
            conn, response = self.stream(disconnected_session, "Create another note")
            self.assertEqual(self.next_event(response)["type"], "delta")
            self.assertEqual(self.next_event(response)["type"], "tool_start")
            approval = self.next_event(response)
            self.request("POST", "/api/approvals", {"id": approval["id"], "allow": True})
            events = [json.loads(line) for line in response]
            conn.close()
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual((Path(self.temp.name) / "after-disconnect.txt").read_text(), "Created")
        saved = self.server.store.load_messages(disconnected_session)
        self.assertEqual(len(saved), 3)
        self.assertEqual(saved[1]["content"], "Creating your note.")
        self.assertEqual(saved[1]["turn_status"], "failed")
        self.assertEqual(saved[1]["tool_calls"][0]["id"], saved[2]["tool_call_id"])


if __name__ == "__main__":
    unittest.main()
