import asyncio
import json
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
    def test_browser_login_reports_its_url_without_printing_over_the_tui(self):
        import contextlib
        import io
        from src.mcp.oauth import NotionTokenStorage, login_notion

        redirect = None

        def auth_factory(on_redirect, _callback, _storage):
            nonlocal redirect
            redirect = on_redirect
            return SimpleNamespace(context=SimpleNamespace(oauth_metadata=None, protected_resource_metadata=None))

        class FakeContext:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def list_tools(self):
                await redirect("https://example.com/authorize?state=test")
                return SimpleNamespace(tools=[object()])

        with tempfile.TemporaryDirectory() as folder, \
             patch("src.mcp.oauth.HTTPServer"), \
             patch("src.mcp.oauth.NotionTokenStorage", return_value=NotionTokenStorage(Path(folder) / "auth.json")), \
             patch("src.mcp.oauth.notion_auth", side_effect=auth_factory), \
             patch("src.mcp.oauth.webbrowser.open", return_value=True) as browser, \
             patch("mcp.shared._httpx_utils.create_mcp_http_client", FakeContext), \
             patch("mcp.Client", FakeContext):
            urls = []
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                count = asyncio.run(login_notion(on_authorize=urls.append))
            self.assertEqual(count, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(urls, ["https://example.com/authorize?state=test"])
            browser.assert_called_once_with(urls[0])

    def test_mcp_cli_reuses_server_preferences(self):
        import contextlib
        import io
        from src.mcp.oauth import main

        with patch("src.mcp.discovery.load_enabled_servers", return_value={"context7"}):
            with patch("src.mcp.discovery.save_enabled_servers") as save:
                with contextlib.redirect_stdout(io.StringIO()):
                    main(["enable", "github"])
                save.assert_called_once_with({"context7", "github"})

    def test_hosted_presets_migrate_preferences_and_keep_secrets_out_of_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps({"enabled_servers": ["context7", "playwright"]}))
            enabled = load_enabled_servers(path)
            self.assertNotIn("github", enabled)
            self.assertIn("notion", enabled)
            save_enabled_servers({"context7"}, path)
            self.assertEqual(load_enabled_servers(path), {"context7"})
        with patch("src.mcp.discovery.local_secret", return_value="test-secret"):
            configs = server_configs(set(SERVER_NAMES))
        hosted = [config for config in configs if config.url]
        self.assertEqual(len(hosted), 9)
        self.assertTrue(all(config.url.startswith("https://") and not config.command for config in hosted))
        self.assertTrue(all("test-secret" not in config.url and "test-secret" not in repr(config) for config in hosted))
        self.assertEqual(next(c for c in hosted if c.name == "exa").headers, {"x-api-key": "test-secret"})
        github = next(c for c in hosted if c.name == "github")
        self.assertTrue(github.url.endswith("/readonly"))
        self.assertEqual(github.headers["X-MCP-Toolsets"], "repos,issues,pull_requests")

    def test_http_discovery_tool_call_and_write_approval(self):
        import httpx2
        from mcp import Client

        requests = []

        def respond(request):
            requests.append(request)
            self.assertEqual(request.headers["Authorization"], "Bearer test-secret")
            if request.method != "POST":
                return httpx2.Response(405)
            message = json.loads(request.content)
            if "id" not in message:
                return httpx2.Response(202)
            if message["method"] == "initialize":
                result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "http-test", "version": "1"}}
            elif message["method"] == "tools/list":
                result = {"tools": [
                    {"name": "lookup", "inputSchema": {"type": "object"},
                     "annotations": {"readOnlyHint": True}},
                    {"name": "change", "inputSchema": {"type": "object"}},
                ]}
            else:
                self.assertEqual(message["method"], "tools/call")
                result = {"content": [{"type": "text", "text": "Hosted result"}]}
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

        config = MCPServerConfig("linear", url="https://mcp.test/mcp", headers={"Authorization": "Bearer test-secret"})
        client = MCPClient([config])

        async def exercise():
            http = httpx2.AsyncClient(transport=httpx2.MockTransport(respond), headers=config.headers)
            with patch("mcp.shared._httpx_utils.create_mcp_http_client", return_value=http):
                with patch("src.mcp.client.shutil.which") as which:
                    status = await client._connect_config(config, Client, None)
                    which.assert_not_called()
            try:
                self.assertIn("Connected with 2 tool", status)
                self.assertFalse(client.requires_approval("mcp__linear__lookup"))
                self.assertTrue(client.requires_approval("mcp__linear__change"))
                result = await client._clients["linear"].call_tool("lookup", {})
                self.assertEqual(result.content[0].text, "Hosted result")
            finally:
                await client._stacks["linear"].aclose()

        asyncio.run(exercise())
        self.assertTrue(requests)

    def test_remote_failure_redacts_credentials_and_missing_keys_have_instructions(self):
        from unittest.mock import AsyncMock

        config = MCPServerConfig("tavily", url="https://mcp.tavily.com/mcp/", headers={"Authorization": "Bearer test-secret"})
        client = MCPClient([config])
        with patch.object(client, "_connect_one", new=AsyncMock(side_effect=RuntimeError("Bad token test-secret"))):
            status = asyncio.run(client._connect_config(config, None, None))
        self.assertNotIn("test-secret", status)
        self.assertIn("[redacted]", status)
        with patch("src.mcp.discovery.local_secret", return_value=""):
            with patch("src.mcp.oauth.NotionTokenStorage.has_tokens", return_value=False):
                configs = {config.name: config for config in server_configs(set(SERVER_NAMES))}
        self.assertIn("mcp login notion", configs["notion"].disabled_reason)
        self.assertIn("LINEAR_API_KEY", configs["linear"].disabled_reason)
        self.assertFalse(configs["microsoft_learn"].disabled_reason)

    def test_notion_token_storage_is_private_and_preserves_expiry(self):
        from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
        from src.mcp.oauth import NotionTokenStorage

        async def exercise(path):
            storage = NotionTokenStorage(path)
            self.assertFalse(storage.has_tokens())
            info = OAuthClientInformationFull(client_id="test-client", redirect_uris=["http://127.0.0.1:8766/callback"])
            await storage.set_client_info(info)
            with patch("src.mcp.oauth.time.time", return_value=1000):
                await storage.set_tokens(OAuthToken(access_token="test-token", token_type="Bearer", expires_in=60, refresh_token="refresh"))
            self.assertTrue(storage.has_tokens())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch("src.mcp.oauth.time.time", return_value=1200):
                tokens = await storage.get_tokens()
            self.assertLess(tokens.expires_in, 0)
            self.assertEqual(tokens.refresh_token, "refresh")
            self.assertEqual((await storage.get_client_info()).client_id, "test-client")
            path.write_text("bad json")
            self.assertFalse(storage.has_tokens())

        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(exercise(Path(folder) / "private" / "tokens.json"))

    def test_notion_oauth_pkce_and_refresh_after_restart(self):
        import base64
        import hashlib
        from urllib.parse import parse_qs, urlsplit
        import httpx2
        from src.mcp.oauth import NOTION_URL, NotionTokenStorage, notion_auth

        authorization = {}
        grants = []

        async def redirect(url):
            authorization.update(parse_qs(urlsplit(url).query))
            self.assertEqual(authorization["code_challenge_method"], ["S256"])

        async def callback():
            from mcp.shared.auth import AuthorizationCodeResult
            return AuthorizationCodeResult(code="test-code", state=authorization["state"][0])

        def respond(request):
            url = str(request.url)
            if url == NOTION_URL:
                if request.headers.get("Authorization") in {"Bearer test-token", "Bearer refreshed-token"}:
                    return httpx2.Response(200, json={"ok": True})
                return httpx2.Response(401, headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.notion.com/.well-known/oauth-protected-resource"',
                })
            if "oauth-protected-resource" in url:
                return httpx2.Response(200, json={"resource": NOTION_URL, "authorization_servers": ["https://auth.notion.test"]})
            if "oauth-authorization-server" in url or "openid-configuration" in url:
                return httpx2.Response(200, json={
                    "issuer": "https://auth.notion.test", "authorization_endpoint": "https://auth.notion.test/authorize",
                    "token_endpoint": "https://auth.notion.test/oauth/token", "registration_endpoint": "https://auth.notion.test/register",
                    "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                })
            if url.endswith("/register"):
                metadata = json.loads(request.content)
                return httpx2.Response(201, json={**metadata, "client_id": "test-client"})
            if url.endswith("/oauth/token"):
                params = parse_qs(request.content.decode())
                grant = params["grant_type"][0]
                grants.append(grant)
                if grant == "authorization_code":
                    digest = hashlib.sha256(params["code_verifier"][0].encode()).digest()
                    self.assertEqual(base64.urlsafe_b64encode(digest).rstrip(b"=").decode(), authorization["code_challenge"][0])
                else:
                    self.assertEqual(params["refresh_token"], ["test-refresh"])
                return httpx2.Response(200, json={
                    "access_token": "test-token" if grant == "authorization_code" else "refreshed-token",
                    "token_type": "Bearer", "expires_in": 60, "refresh_token": "test-refresh",
                })
            self.fail(f"Unexpected OAuth request: {url}")

        async def exercise(path):
            storage = NotionTokenStorage(path)
            auth = notion_auth(redirect, callback, storage)
            with patch("src.mcp.oauth.time.time", return_value=1000):
                async with httpx2.AsyncClient(auth=auth, transport=httpx2.MockTransport(respond)) as http:
                    self.assertEqual((await http.get(NOTION_URL)).status_code, 200)
            storage._save(oauth_metadata=auth.context.oauth_metadata.model_dump(mode="json"),
                          resource_metadata=auth.context.protected_resource_metadata.model_dump(mode="json"))
            with patch("src.mcp.oauth.time.time", return_value=1200):
                restarted = notion_auth(storage=storage)
                async with httpx2.AsyncClient(auth=restarted, transport=httpx2.MockTransport(respond)) as http:
                    self.assertEqual((await http.get(NOTION_URL)).status_code, 200)
            self.assertEqual(grants, ["authorization_code", "refresh_token"])
            self.assertEqual((await storage.get_tokens()).access_token, "refreshed-token")

        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(exercise(Path(folder) / "auth.json"))

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

    def test_reconnect_reloads_credentials_and_closes_the_previous_owner(self):
        tool = SimpleNamespace(name="lookup", description="Lookup data", title="",
                               input_schema={"type": "object", "properties": {}})

        class TaskBoundClient:
            instances = []

            def __init__(self, *_args, **_kwargs):
                self.closed_in_owner = False
                self.instances.append(self)

            async def __aenter__(self):
                self.owner = asyncio.current_task()
                return self

            async def __aexit__(self, *_args):
                self.closed_in_owner = asyncio.current_task() is self.owner

            async def list_tools(self, cursor=None):
                return SimpleNamespace(tools=[tool], next_cursor=None)

        original = MCPServerConfig("context7", "fake", env={"API_KEY": "old"})
        fresh = MCPServerConfig("context7", "fake", env={"API_KEY": "new"})
        client = MCPClient([original])
        client._uses_presets = True
        with patch("mcp.Client", TaskBoundClient), \
             patch("src.mcp.client.shutil.which", return_value="/fake"), \
             patch("src.mcp.client.server_configs", side_effect=lambda enabled: [
                 MCPServerConfig("context7", "fake", env=fresh.env, enabled="context7" in enabled)
             ]) as configs:
            try:
                client.start()
                client.reconnect("context7")
                self.assertTrue(TaskBoundClient.instances[0].closed_in_owner)
                self.assertEqual(client.configs[0].env, {"API_KEY": "new"})
                self.assertEqual(len(client.tool_schemas()), 1)
                self.assertIs(client._bindings["mcp__context7__lookup"][2], TaskBoundClient.instances[1])
                client.set_enabled(set())
                self.assertEqual(client.tool_schemas(), [])
                with self.assertRaises(ValueError):
                    client.reconnect("context7")
                with self.assertRaises(ValueError):
                    client.reconnect("unknown")
                client.set_enabled({"context7"})
                self.assertEqual(len(client.tool_schemas()), 1)
                self.assertEqual(configs.call_count, 3)
            finally:
                client.close()
        self.assertTrue(all(c.closed_in_owner for c in TaskBoundClient.instances))

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
