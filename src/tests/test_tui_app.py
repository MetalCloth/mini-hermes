"""Picker interaction and geometry checks without network requests."""

import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.mcp.client import MCPClient
from src.providers.codex import CodexProvider
from src.session.sqlite_store import SQLiteSessionStore
from src.mcp.discovery import load_enabled_servers, save_enabled_servers
from src.tui_app import ApprovalScreen, InfoScreen, MCPManagerScreen, MCPReady, OrynTUI, ToolsScreen
from textual.widgets import Button, Input, OptionList, Static, TextArea


class TUILayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_effort_and_speed_pickers_persist_per_model_and_keep_the_draft(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "models_cache.json").write_text(json.dumps({"models": [
                {"slug": "gpt-6-astra", "supported_reasoning_levels": [
                    {"effort": "low", "description": "Fast responses with lighter reasoning."},
                    {"effort": "high", "description": "Greater reasoning depth for complex problems."},
                 ], "service_tiers": [{"id": "priority", "name": "Fast", "description": "More speed, increased usage."}]},
                {"slug": "gpt-6-luna", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "max"}],
                 "service_tiers": [{"id": "priority", "name": "Fast"}]},
            ]}))
            store = SQLiteSessionStore(root / "sessions.sqlite3")
            session = store.create_session(root)
            store.set_session_model(session, "gpt-6-astra")
            mcp = MCPClient(configs=[])
            app = OrynTUI(
                store=store, session_id=session, project_root=root, model="gpt-6-astra", history=[],
                provider=CodexProvider("gpt-6-astra", root / "auth.json"), mcp_client=mcp, initial_tools=[],
            )
            try:
                async with app.run_test(size=(100, 32)) as pilot:
                    composer = app.query_one("#composer", TextArea)
                    composer.load_text("Keep this draft")
                    await pilot.pause()
                    await pilot.click("#effort-chip")
                    await pilot.pause()
                    self.assertEqual(app.screen.styles.background.a, 0)
                    app.screen.query_one(Input).value = "high"
                    await pilot.pause()
                    self.assertEqual(str(app.screen.query_one("#picker-description", Static).content),
                                     "Greater reasoning depth for complex problems.")
                    options = app.screen.query_one(OptionList)
                    self.assertNotIn("—", str(options.highlighted_option.prompt))
                    self.assertTrue(str(options.highlighted_option.prompt).startswith("○ High"))
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(app.provider.reasoning_effort, "high")
                    self.assertEqual(composer.text, "Keep this draft")
                    self.assertTrue(composer.has_focus)
                    await pilot.press("f4")
                    await pilot.pause()
                    app.screen.query_one(Input).value = "Fast"
                    await pilot.pause()
                    self.assertEqual(str(app.screen.query_one("#picker-description", Static).content),
                                     "More speed, increased usage.")
                    self.assertNotIn("—", str(app.screen.query_one(OptionList).highlighted_option.prompt))
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(app.provider.service_tier, "priority")
                    await app._execute_command("effort", "ultra")
                    self.assertEqual(app.provider.reasoning_effort, "high")
                    self.assertIn("choose default, low, high", str(app.query_one("#activity-label", Static).content))
                    app._save_model("gpt-6-luna")
                    self.assertEqual(app._model_settings(), {"reasoning_effort": "default", "service_tier": "default"})
                    await app._execute_command("effort", "max")
                    await app._execute_command("speed", "standard")
                    app._save_model("gpt-6-astra")
                    self.assertEqual(app._model_settings(), {"reasoning_effort": "high", "service_tier": "priority"})
                    reopened = SQLiteSessionStore(store.db_path)
                    self.assertEqual(reopened.session_model_settings(session, "gpt-6-luna")["reasoning_effort"], "max")
                    await app._load_session(session)
                    self.assertEqual(app._model_settings(), {"reasoning_effort": "high", "service_tier": "priority"})
                    app.turn_active = True
                    app.action_choose_effort()
                    app.action_choose_speed()
                    self.assertEqual(len(app.screen_stack), 1)
                    await app._execute_command("speed", "standard")
                    self.assertEqual(app.provider.service_tier, "priority")
                    app.turn_active = False
                    await pilot.resize_terminal(64, 24)
                    await pilot.pause()
                    frame = app.query_one("#composer-frame").region
                    for selector in ["#model-chip", "#effort-chip", "#speed-chip"]:
                        button = app.query_one(selector, Button)
                        self.assertLessEqual(button.region.right, frame.right)
                    await pilot.press("f3")
                    await pilot.pause()
                    card = app.screen.query_one("#picker-card").region
                    self.assertLessEqual(app.screen.query_one("#picker-hint").region.bottom, card.bottom)
                    self.assertLessEqual(card.bottom, 24)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertTrue(composer.has_focus)
            finally:
                mcp.close()

    async def test_mcp_manager_updates_toggles_reconnects_and_starts_login(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SQLiteSessionStore(root / "sessions.sqlite3")
            client = MCPClient(configs=[])
            servers = [
                {"name": name, "enabled": name != "github", "state": state,
                 "message": "Add a key to mcp.env." if state == "unavailable" else "Connected.",
                 "tool_count": 1 if state == "connected" else 0,
                 "access": "Documentation tools.", "transport": "http"}
                for name, state in [("context7", "connected"), ("github", "disabled"), ("notion", "unavailable")]
            ]
            schema = {"type": "function", "name": "mcp__context7__resolve_library_id",
                      "description": "Find documentation", "parameters": {"type": "object", "properties": {}}}

            def toggle(enabled):
                for server in servers:
                    server.update(enabled=server["name"] in enabled,
                                  state="connected" if server["name"] in enabled else "disabled")
                return servers

            def reconnect(name):
                server = next(s for s in servers if s["name"] == name)
                server.update(state="connected", message="Connected after reconnect.", tool_count=1)
                return servers

            async def login(*, on_authorize):
                on_authorize("https://example.com/authorize?state=test")
                return 1

            async def pending_login(*, on_authorize):
                on_authorize("https://example.com/authorize?state=test")
                await asyncio.Future()
            app = OrynTUI(
                store=store, session_id=store.create_session(root), project_root=root,
                model="gpt-5.6-luna", history=[], provider=CodexProvider("gpt-5.6-luna", root / "auth.json"),
                mcp_client=client, initial_tools=[],
            )
            try:
                with patch.object(client, "status_snapshot", return_value=servers), \
                     patch.object(client, "tool_schemas", side_effect=lambda: [schema] if servers[0]["enabled"] else []), \
                     patch.object(client, "set_enabled", side_effect=toggle) as set_enabled, \
                     patch.object(client, "reconnect", side_effect=reconnect) as reconnect_mock, \
                     patch("src.tui_app.save_enabled_servers", side_effect=lambda enabled: save_enabled_servers(enabled, root / "mcp.json")) as save_mock, \
                     patch("src.tui_app.login_notion", side_effect=login) as login_mock:
                    async with app.run_test(size=(100, 32)) as pilot:
                        await pilot.pause()
                        composer = app.query_one("#composer", TextArea)
                        composer.load_text("Keep this draft")
                        app.mcp_ready = False
                        await app._execute_command("mcps")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, MCPManagerScreen)
                        status = app.screen.query_one("#mcp-notice", Static)
                        self.assertIn("still connecting", str(status.content))
                        self.assertTrue(app.screen.query_one("#mcp-toggle", Button).disabled)
                        statuses = [
                            "context7: Connected with 2 tool(s).",
                            "github: Disabled in Oryn settings.",
                            "playwright: Connected with 22 tool(s).",
                        ]
                        app.post_message(MCPReady(statuses, [schema], servers))
                        await pilot.pause()
                        self.assertTrue(app.mcp_ready)
                        self.assertIn(schema, app.tools)
                        self.assertEqual(app.screen.styles.background.a, 0)
                        search = app.screen.query_one(Input)
                        search.value = "context7"
                        await pilot.pause()
                        await pilot.press("enter")
                        await app.workers.wait_for_complete()
                        await pilot.pause()
                        self.assertFalse(servers[0]["enabled"])
                        self.assertNotIn(schema, app.tools)
                        self.assertNotIn("context7", load_enabled_servers(root / "mcp.json"))
                        self.assertTrue(app.screen.query_one("#mcp-reconnect", Button).disabled)
                        await pilot.press("ctrl+e")
                        await app.workers.wait_for_complete()
                        await pilot.pause()
                        self.assertIn(schema, app.tools)
                        await pilot.press("ctrl+r")
                        await app.workers.wait_for_complete()
                        reconnect_mock.assert_called_with("context7")
                        with patch.object(save_mock, "side_effect", OSError("Disk full")):
                            await pilot.press("enter")
                            await app.workers.wait_for_complete()
                        self.assertTrue(servers[0]["enabled"])
                        self.assertIn(schema, app.tools)
                        self.assertIn("update failed", app.mcp_notice)
                        app.turn_active = True
                        app._refresh_mcp_view()
                        count = set_enabled.call_count
                        await pilot.press("enter")
                        self.assertEqual(set_enabled.call_count, count)
                        app.turn_active = False
                        search.value = "notion"
                        await pilot.pause()
                        await pilot.press("ctrl+l")
                        await app.workers.wait_for_complete()
                        login_mock.assert_called_once()
                        reconnect_mock.assert_called_with("notion")
                        self.assertFalse(app.mcp_busy)
                        login_mock.side_effect = pending_login
                        await pilot.press("ctrl+l")
                        await pilot.pause()
                        self.assertTrue(app.mcp_login_active)
                        self.assertIn("Open sign-in", str(status.content))
                        await app.action_send_prompt()
                        self.assertEqual(composer.text, "Keep this draft")
                        await pilot.press("escape")
                        await app.workers.wait_for_complete()
                        await pilot.pause()
                        self.assertFalse(app.mcp_busy)
                        self.assertEqual(composer.text, "Keep this draft")
                        self.assertTrue(composer.has_focus)
                        await pilot.resize_terminal(64, 24)
                        await app._execute_command("mcps")
                        await pilot.pause()
                        card = app.screen.query_one("#picker-card").region
                        self.assertLessEqual(app.screen.query_one("#picker-hint").region.bottom, card.bottom)
                        self.assertLessEqual(card.bottom, 24)
                        await pilot.press("escape")
            finally:
                client.close()

    async def test_tools_are_searchable_single_line_rows_with_separate_details(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SQLiteSessionStore(root / "sessions.sqlite3")
            client = MCPClient(configs=[])
            app = OrynTUI(
                store=store, session_id=store.create_session(root), project_root=root,
                model="gpt-5.6-luna", history=[], provider=CodexProvider("gpt-5.6-luna", root / "auth.json"),
                mcp_client=client, initial_tools=[],
            )
            try:
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    await app._execute_command("tools")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ToolsScreen)
                    self.assertEqual(app.screen.styles.background.a, 0)
                    search = app.screen.query_one(Input)
                    search.value = "terminal"
                    await pilot.pause()
                    option = app.screen.query_one(OptionList).highlighted_option
                    self.assertEqual(option.id, "terminal")
                    self.assertTrue(option.prompt.no_wrap)
                    self.assertIn("Run a shell command", option.prompt.plain)
                    self.assertNotIn("\n", option.prompt.plain)
                    self.assertIn("Run a shell command", str(app.screen.query_one("#picker-description", Static).content))
                    await pilot.press("enter")
                    self.assertEqual(len(app.screen_stack), 2)  # Browsing never executes a tool.
                    search.value = "missing-tool"
                    await pilot.pause()
                    self.assertTrue(app.screen.query_one("#picker-empty").display)
                    search.value = ""
                    await pilot.resize_terminal(64, 24)
                    await pilot.pause()
                    card = app.screen.query_one("#picker-card").region
                    self.assertLessEqual(app.screen.query_one("#picker-hint").region.bottom, card.bottom)
                    self.assertLessEqual(card.bottom, 24)
                    await pilot.press("escape")
                    self.assertTrue(app.query_one("#composer", TextArea).has_focus)
                    app.tools = []
                    await app._execute_command("tools")
                    await pilot.pause()
                    self.assertIn("No tools", str(app.screen.query_one("#picker-empty", Static).content))
            finally:
                client.close()

    async def test_model_and_sessions_search_select_and_close(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SQLiteSessionStore(root / "sessions.sqlite3")
            current = store.create_session(root)
            other = store.create_session(root)
            store.rename_session(current, "Current chat")
            store.rename_session(other, "Explain recovery")
            provider = CodexProvider("gpt-5.6-luna", root / "auth.json")
            cache = root / "models_cache.json"
            cache.write_text(json.dumps({"models": [
                {"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "visibility": "list"},
                {"slug": "gpt-6-sol", "display_name": "GPT-6-Sol", "visibility": "list"},
                {"slug": "hidden-model", "visibility": "hide"},
                None,
            ]}))
            self.assertEqual(provider.cached_models(), [
                ("gpt-5.6-luna", "GPT-5.6-Luna"), ("gpt-6-sol", "GPT-6-Sol"),
            ])
            for content in ["not json", "[]", '{"models": null}']:
                cache.write_text(content)
                self.assertEqual(provider.cached_models(), [])
            cache.write_text('{"models": []}')
            store.set_session_model(other, "gpt-6-sol")
            mcp = MCPClient(configs=[])
            app = OrynTUI(
                store=store, session_id=current, project_root=root, model=provider.model,
                history=[{"role": "assistant", "content": "Conversation stays visible."}],
                provider=provider, mcp_client=mcp, initial_tools=[],
            )
            try:
                async with app.run_test(size=(150, 43)) as pilot:
                    await pilot.pause()
                    composer_region = app.query_one("#composer-frame").region
                    await pilot.click("#model-chip")
                    await pilot.pause()
                    self.assertEqual(len(app.screen_stack), 2)
                    self.assertEqual(app.screen.styles.background.a, 0)
                    self.assertEqual(app.screen.query_one(OptionList).highlighted_option.id, provider.model)
                    # Repeated shortcuts must not stack dialogs.
                    await pilot.press("f2", "ctrl+o")
                    await pilot.pause()
                    self.assertEqual(len(app.screen_stack), 2)
                    await pilot.press("down", "enter")
                    await pilot.pause()
                    self.assertEqual(app.model, "gpt-6-sol")
                    self.assertEqual(store.session_model(current), "gpt-6-sol")
                    self.assertTrue(app.query_one("#composer").has_focus)

                    await pilot.press("f2")
                    await pilot.pause()
                    search = app.screen.query_one(Input)
                    search.value = "new-custom-model"
                    await pilot.pause()
                    self.assertEqual(app.screen.query_one(OptionList).highlighted_option.id, search.value)
                    search.value = "invalid model!"
                    await pilot.pause()
                    self.assertEqual(app.screen.query_one(OptionList).option_count, 0)
                    await pilot.press("enter")
                    self.assertEqual(len(app.screen_stack), 2)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertEqual(app.model, "gpt-6-sol")
                    self.assertEqual(app.query_one("#composer-frame").region, composer_region)

                    await pilot.press("ctrl+o")
                    await pilot.pause()
                    search = app.screen.query_one(Input)
                    search.value = "missing session"
                    await pilot.pause()
                    self.assertTrue(app.screen.query_one("#picker-empty").display)
                    search.value = "recovery"
                    await pilot.pause()
                    self.assertEqual(app.screen.query_one(OptionList).highlighted_option.id, other)
                    await pilot.resize_terminal(64, 24)
                    await pilot.pause()
                    card = app.screen.query_one("#picker-card").region
                    self.assertGreaterEqual(card.x, 0)
                    self.assertLessEqual(card.right, 64)
                    self.assertLessEqual(card.bottom, 24)
                    self.assertTrue(app.screen.query_one(Input).has_focus)
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(app.session_id, other)
                    self.assertEqual(len(app.screen_stack), 1)
                    self.assertTrue(app.query_one("#composer").has_focus)

                    # The session actions shown in the picker must actually work.
                    await pilot.press("ctrl+o")
                    await pilot.pause()
                    app.screen.query_one(Input).value = "Current chat"
                    await pilot.pause()
                    await pilot.press("ctrl+f")
                    await pilot.pause()
                    self.assertEqual(SQLiteSessionStore(store.db_path).session_entries()[0]["pinned"], 1)
                    await pilot.press("ctrl+r")
                    await pilot.pause()
                    app.screen.query_one(Input).value = ""
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertTrue(app.screen.query_one("#picker-empty").display)
                    app.screen.query_one(Input).value = "Renamed chat"
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(store.session_title(current), "Renamed chat")
                    app.screen.query_one(Input).value = "Renamed"
                    await pilot.pause()
                    await pilot.press("ctrl+d")
                    await pilot.pause()
                    self.assertTrue(store.session_exists(current))
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertTrue(store.session_exists(current))
                    await pilot.press("ctrl+o")
                    await pilot.pause()
                    app.screen.query_one(Input).value = "Renamed"
                    await pilot.pause()
                    await pilot.press("ctrl+d", "ctrl+d")
                    await pilot.pause()
                    self.assertFalse(store.session_exists(current))
                    self.assertTrue(store.session_exists(other))
                    app.screen.query_one(Input).value = "recovery"
                    await pilot.pause()
                    await pilot.press("ctrl+d", "ctrl+d")
                    await pilot.pause()
                    self.assertFalse(store.session_exists(other))
                    self.assertNotIn(app.session_id, {current, other})
                    self.assertEqual(len(app.screen_stack), 1)
                    self.assertTrue(app.query_one("#main-panel").has_class("home"))

                    for width, height in [(150, 43), (64, 24)]:
                        await pilot.resize_terminal(width, height)
                        await pilot.pause()
                        frame = app.query_one("#composer-frame").region
                        self.assertLessEqual(abs(frame.x + frame.width / 2 - width / 2), 1)
                        app.query_one("#composer", TextArea).load_text("/")
                        await pilot.pause()
                        popup = app.query_one("#palette-overlay").region
                        self.assertGreaterEqual(popup.y, 0)
                        self.assertEqual(popup.bottom, frame.y)
                        await pilot.press("escape")
                        app.query_one("#composer", TextArea).clear()
                        await pilot.pause()

                    draft = app.query_one("#composer", TextArea)
                    draft.load_text("My unsent draft")
                    await pilot.pause()
                    await pilot.press("ctrl+p")
                    await pilot.pause()
                    self.assertEqual(draft.text, "/")
                    self.assertTrue(app.query_one("#palette-overlay").display)
                    await pilot.press("down")
                    await pilot.pause()
                    self.assertTrue(draft.has_focus)
                    self.assertEqual(app.query_one("#palette-options", OptionList).highlighted_option.id, "help")
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertEqual(draft.text, "My unsent draft")
                    self.assertFalse(app.query_one("#palette-overlay").display)
                    await pilot.press("shift+enter")
                    self.assertIn("\n", draft.text)

                    # Short dialogs fit their content; long ones keep controls visible.
                    for width, height in [(150, 43), (64, 24)]:
                        await pilot.resize_terminal(width, height)
                        for lines in [3, 100]:
                            body = "\n".join(f"Line {i}" for i in range(lines))
                            for screen, card_id, scroll_id in [
                                (InfoScreen("Tools", body), "#info-card", "#info-scroll"),
                                (ApprovalScreen("Apply edit?", body), "#approval-card", "#approval-preview"),
                            ]:
                                await app.push_screen(screen)
                                await pilot.pause()
                                card = screen.query_one(card_id).region
                                self.assertGreaterEqual(card.y, 0)
                                self.assertLessEqual(card.bottom, height)
                                for button in screen.query(Button):
                                    self.assertLessEqual(button.region.bottom, card.bottom)
                                if lines == 3:
                                    self.assertLess(card.height, height * 0.6)
                                else:
                                    scroll = screen.query_one(scroll_id)
                                    self.assertGreater(scroll.virtual_size.height, scroll.content_region.height)
                                await pilot.press("escape")
                                await pilot.pause()
            finally:
                mcp.close()

    async def test_picker_matches_composer_without_moving_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SQLiteSessionStore(root / "sessions.sqlite3")
            mcp = MCPClient(configs=[])
            app = OrynTUI(
                store=store, session_id=store.create_session(root), project_root=root,
                model="gpt-5.6-luna", history=[
                    {"role": "user", "content": "Hi"},
                    {"role": "assistant", "content": "Hello."},
                ], provider=CodexProvider("gpt-5.6-luna"), mcp_client=mcp,
                initial_tools=[],
            )
            try:
                async with app.run_test(size=(150, 43)) as pilot:
                    await pilot.pause()
                    composer = app.query_one("#composer-frame")
                    palette = app.query_one("#palette-overlay")
                    initial = composer.region

                    def assert_attached():
                        self.assertEqual(palette.region.x, composer.region.x)
                        self.assertEqual(palette.region.width, composer.region.width)
                        self.assertEqual(palette.region.bottom, composer.region.y)

                    await pilot.press("/")
                    await pilot.pause()
                    self.assertEqual(composer.region, initial)
                    assert_attached()
                    self.assertEqual(len(app.screen_stack), 1)

                    await pilot.press("m", "o")
                    await pilot.pause()
                    self.assertEqual(composer.region, initial)
                    assert_attached()
                    search = app.query_one("#composer", TextArea)
                    options = app.query_one("#palette-options", OptionList)
                    for query, expected in [
                        ("sessi", ["sessions"]),
                        ("/session", ["sessions"]),
                        ("/SESSION", ["sessions"]),
                        ("new", ["new"]),
                        ("mod", ["models"]),
                        ("fresh", []),
                        ("unknown", []),
                    ]:
                        search.load_text("/" + query.lstrip("/"))
                        await pilot.pause()
                        self.assertEqual([
                            options.get_option_at_index(i).id for i in range(options.option_count)
                        ], expected)
                        assert_attached()
                    search.load_text("/session")
                    await pilot.pause()
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(len(app.screen_stack), 2)
                    self.assertEqual(app.screen.title, "Sessions")
                    self.assertEqual(len(store.list_sessions()), 1)
                    await pilot.press("escape", "/")
                    await pilot.pause()

                    for width, height in [(86, 32), (64, 24)]:
                        await pilot.resize_terminal(width, height)
                        await pilot.pause()
                        assert_attached()
                        opened = composer.region
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertFalse(palette.display)
                        self.assertEqual(composer.region, opened)
                        self.assertTrue(app.query_one("#composer").has_focus)
                        search.load_text("")
                        await pilot.pause()
                        await pilot.press("/")
                        await pilot.pause()
                        self.assertEqual(composer.region, opened)
                        assert_attached()
            finally:
                mcp.close()


if __name__ == "__main__":
    unittest.main()
