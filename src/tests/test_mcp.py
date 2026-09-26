import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.agent.conversation_loop import run_turn
from src.mcp.adapter import provider_tool
from src.mcp.client import MCPClient
from src.mcp.discovery import (
    MCPServerConfig,
    SERVER_NAMES,
    load_enabled_servers,
    save_enabled_servers,
    server_configs,
)
from src.providers.types import ModelResponse, ToolCall


class MCPTests(unittest.TestCase):
    def test_stdio_server_notices_do_not_leak_to_the_terminal(self):
        server = '''
import json, os, sys
os.write(2, b"npm notice PLAYWRIGHT_STARTUP_NOTICE\\n")
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    result = ({"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
               "serverInfo": {"name": "quiet-test", "version": "1"}}
              if request["method"] == "initialize" else {"tools": []})
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
'''
        script = f'''
import sys
from src.mcp.client import MCPClient
from src.mcp.discovery import MCPServerConfig
client = MCPClient([MCPServerConfig("github", sys.executable, ("-u", "-c", {server!r}), {{}})])
try:
    assert "Connected with 0 tool" in client.start()[0]
finally:
    client.close()
'''
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("PLAYWRIGHT_STARTUP_NOTICE", result.stderr)

    def test_server_preferences_default_to_current_servers_and_persist(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prefs" / "mcp-settings.json"
            self.assertEqual(load_enabled_servers(path), set(SERVER_NAMES))

            enabled = {"context7", "playwright"}
            save_enabled_servers(enabled, path)
            self.assertEqual(load_enabled_servers(path), enabled)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            configs = server_configs(enabled)
            self.assertEqual({config.name for config in configs if config.enabled}, enabled)

    def test_preferences_reject_unknown_server_names(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "Unknown MCP server"):
                save_enabled_servers({"arbitrary-command"}, Path(folder) / "prefs.json")

    def test_invalid_tool_schemas_are_rejected(self):
        malformed = SimpleNamespace(name="bad_schema", input_schema=["not", "an object"])
        with self.assertRaisesRegex(ValueError, "unsupported input schema"):
            provider_tool("fake", malformed)

    def test_duplicate_tool_names_are_registered_once(self):
        tool = SimpleNamespace(
            name="lookup", description="Lookup data", title="",
            input_schema={"type": "object", "properties": {}},
        )
        bad_tool = SimpleNamespace(name="bad", input_schema={"type": "string"})

        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def list_tools(self, cursor=None):
                return SimpleNamespace(tools=[tool, tool, bad_tool], next_cursor=None)

        config = MCPServerConfig("fake", "fake", (), {})
        client = MCPClient([config])

        async def connect_and_close():
            count = await client._connect_one(FakeClient, lambda **_kwargs: None, config, "/fake")
            await client._stacks["fake"].aclose()
            return count

        count = asyncio.run(connect_and_close())
        self.assertEqual(count, 1)
        self.assertEqual([schema["name"] for schema in client._schemas], ["mcp__fake__lookup"])
        self.assertEqual(list(client._bindings), ["mcp__fake__lookup"])

    def test_live_session_closes_in_the_task_that_opened_it(self):
        tool = SimpleNamespace(
            name="lookup", description="Lookup data", title="",
            input_schema={"type": "object", "properties": {}},
        )

        class TaskBoundClient:
            instances = []

            def __init__(self, *_args, **_kwargs):
                self.owner = None
                self.closed_in_owner = False
                self.instances.append(self)

            async def __aenter__(self):
                self.owner = asyncio.current_task()
                return self

            async def __aexit__(self, *_args):
                self.closed_in_owner = asyncio.current_task() is self.owner

            async def list_tools(self, cursor=None):
                return SimpleNamespace(tools=[tool], next_cursor=None)

        config = MCPServerConfig("fake", "fake", (), {})
        client = MCPClient([config])

        async def connect_and_stop():
            with patch("src.mcp.client.shutil.which", return_value="/fake"):
                status = await client._start_config(config, TaskBoundClient, lambda **_kwargs: None)
            self.assertIn("Connected with 1 tool", status)
            client._server_stops["fake"].set()
            await client._server_tasks["fake"]

        asyncio.run(connect_and_stop())
        self.assertTrue(TaskBoundClient.instances[0].closed_in_owner)

    def test_disabling_a_server_removes_its_tools_and_schema(self):
        config = MCPServerConfig("context7", "unused", (), {}, enabled=False)
        client = MCPClient([config])
        client._bindings["mcp__context7__lookup"] = ("context7", "lookup", object())
        client._schemas.append({"type": "function", "name": "mcp__context7__lookup"})
        asyncio.run(client._reconfigure({"context7"}))
        self.assertEqual(client._bindings, {})
        self.assertEqual(client._schemas, [])
        self.assertEqual(client._status_by_name["context7"][0], "disabled")

    def test_missing_server_command_marks_only_that_server_unavailable(self):
        config = MCPServerConfig("context7", "missing-mcp-command", (), {})
        client = MCPClient([config])
        with patch("src.mcp.client.shutil.which", return_value=None):
            status = asyncio.run(client._connect_config(config, None, None))
        self.assertIn("not installed", status)
        self.assertEqual(client._status_by_name["context7"][0], "unavailable")
        client._started = True
        self.assertEqual(client.status_snapshot()[0]["tool_count"], 0)

    def test_slow_server_tool_call_times_out(self):
        class PendingFuture:
            cancelled = False

            def result(self, timeout=None):
                import time
                time.sleep(timeout or 0)
                raise TimeoutError

            def done(self):
                return False

            def cancel(self):
                self.cancelled = True

        class SlowClient:
            def call_tool(self, _name, _arguments):
                return self

            def __await__(self):
                yield

        client = MCPClient([])
        client._loop = object()
        client._bindings["mcp__context7__slow"] = ("context7", "slow", SlowClient())
        future = PendingFuture()
        with patch("src.mcp.client._TOOL_CALL_TIMEOUT_SECONDS", 0.01):
            with patch("src.mcp.client.asyncio.run_coroutine_threadsafe", return_value=future):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    client.call_tool("mcp__context7__slow", {})
        self.assertTrue(future.cancelled)

    def test_oversized_mcp_result_is_truncated_before_model_history(self):
        complete = Mock(side_effect=[
            ModelResponse(tool_calls=[ToolCall("mcp-call", "mcp__context7__lookup", {})]),
            ModelResponse("The result was safely shortened."),
        ])
        mcp = SimpleNamespace(
            requires_approval=Mock(return_value=False),
            call_tool=Mock(return_value="x" * 20_001),
        )
        history = [{"role": "user", "content": "Look up a large page."}]
        with tempfile.TemporaryDirectory() as folder:
            run_turn(
                history, complete, [], Path(folder), Mock(), Mock(), mcp_client=mcp,
            )
        result = history[-1]["content"]
        self.assertLessEqual(len(result), 20_000)
        self.assertIn("original result was 20001 characters", result)
        mcp.call_tool.assert_called_once()


if __name__ == "__main__":
    unittest.main()
