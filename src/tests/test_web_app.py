"""Browser dashboard checks without model or network credentials."""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from src.providers.types import ModelResponse, ToolCall
from src.session.sqlite_store import SQLiteSessionStore
from src.web_app import DashboardServer


class TextProvider:
    def __init__(self, model: str) -> None:
        self.model = model

    def complete(self, messages, tools, on_text_delta=None):
        if on_text_delta:
            on_text_delta("Hello ")
            on_text_delta("from Mini-Hermes.")
        return ModelResponse(text="Hello from Mini-Hermes.")


class WriteProvider:
    def __init__(self, model: str) -> None:
        self.model = model

    def complete(self, messages, tools, on_text_delta=None):
        if any(message["role"] == "tool" for message in messages):
            if on_text_delta:
                on_text_delta("The file is ready.")
            return ModelResponse(text="The file is ready.")
        return ModelResponse(tool_calls=[ToolCall("write-1", "write_file", {
            "path": "note.txt", "content": "hello\n",
        })])


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

    def test_approval_waits_then_writes(self):
        self.server.provider_factory = WriteProvider
        session_id = self.create_session()
        conn, response = self.stream(session_id, "Create a note")
        first = json.loads(response.readline())
        approval = json.loads(response.readline())
        self.assertEqual(first["type"], "tool_start")
        self.assertEqual(approval["type"], "approval")
        self.assertEqual(approval["content"], "hello\n")
        self.assertFalse((Path(self.temp.name) / "note.txt").exists())

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


if __name__ == "__main__":
    unittest.main()
