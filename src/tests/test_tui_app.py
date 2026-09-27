"""Picker interaction and geometry checks without network requests."""

import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from src import tui_app
from src.agent.context import select_context
from src.agent.conversation_loop import TurnCancelled
from src.mcp.client import MCPClient
from src.providers.codex import CodexProvider
from src.providers.types import ModelResponse, ToolCall
from src.session.sqlite_store import SQLiteSessionStore
from src.tools.registry import execute_tool, tool_schemas
from src.mcp.discovery import load_enabled_servers, save_enabled_servers
from src.tui_app import ApprovalScreen, InfoScreen, MCPManagerScreen, MCPReady, MessageCard, OrynTUI, ToolsScreen
from textual.widgets import Button, Input, Label, OptionList, Static, TextArea


class TUILayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_launch_opens_home_and_explicit_resume_keeps_saved_chats(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SQLiteSessionStore(root / "sessions.sqlite3")
            store.bind_session_to_project("main", root)
            old_messages = [
                {"role": "user", "content": "An old question"},
                {"role": "assistant", "content": "An old answer"},
            ]
            store.append_messages(old_messages, "main")
            store.set_session_model("main", "gpt-6-sol")
            with patch.object(tui_app, "SQLiteSessionStore", return_value=store), \
                 patch.object(tui_app, "APP_ROOT", root), \
                 patch.object(tui_app, "_resolve_project_root", return_value=root), \
                 patch.object(tui_app, "CodexProvider", side_effect=lambda model: CodexProvider(model, root / "auth.json")), \
                 patch.object(tui_app, "MCPClient", side_effect=lambda: MCPClient(configs=[])), \
                 patch.object(OrynTUI, "run", autospec=True) as run:
                fresh_ids = set()
                for arguments in ([], [], ["--new"], ["--project", str(root)]):
                    tui_app.main(arguments)
                    app = run.call_args.args[0]
                    self.assertNotEqual(app.session_id, "main")
                    self.assertNotIn(app.session_id, fresh_ids)
                    fresh_ids.add(app.session_id)
                    self.assertEqual(store.load_messages(app.session_id), [])
                    self.assertFalse(any(m["role"] in {"user", "assistant"} for m in app.history))
                    self.assertEqual(store.session_project_root(app.session_id), str(root))
                self.assertEqual(store.load_messages("main"), old_messages)
                self.assertEqual(store.session_model("main"), "gpt-6-sol")
                tui_app.main(["--resume", "main"])
                resumed = run.call_args.args[0]
                self.assertEqual(resumed.session_id, "main")
                self.assertEqual(resumed.model, "gpt-6-sol")
                self.assertEqual(resumed.history[-2:], old_messages)

            # Exercise the real home layout and session switch with local MCP disabled.
            app.mcp_client = MCPClient(configs=[])
            try:
                async with app.run_test(size=(100, 32)) as pilot:
                    self.assertTrue(app.query_one("#main-panel").has_class("home"))
                    self.assertEqual(len(app.query("#welcome")), 1)
                    self.assertEqual(len(app.query(MessageCard)), 0)
                    self.assertTrue(app.query_one("#composer", TextArea).has_focus)
                    await app._load_session("main")
                    self.assertFalse(app.query_one("#main-panel").has_class("home"))
                    self.assertEqual([card.content for card in app.query(MessageCard)],
                                     [m["content"] for m in old_messages])
                    await pilot.pause()
            finally:
                app.mcp_client.close()

    async def test_undo_history_stays_with_its_chat_across_switches_and_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app.py").write_text("a = 1\n")
            (root / "notes.txt").write_text("Old note\n")
            store = SQLiteSessionStore(root / "sessions.sqlite3")
            session_a = store.create_session(root)
            initial_undo = []
            client = MCPClient(configs=[])
            app = OrynTUI(
                store=store, session_id=session_a, project_root=root,
                model="gpt-5.6-luna", history=[], provider=CodexProvider("gpt-5.6-luna", root / "auth.json"),
                mcp_client=client, initial_tools=tool_schemas(), undo_history=initial_undo,
            )

            def call(name, arguments, approved=True):
                return execute_tool(
                    name, arguments, app.project_root, lambda _: False, lambda *_: approved,
                    confirm_edit=lambda *_: approved, confirm_undo=lambda *_: approved,
                    undo_history=app.undo_history,
                )

            try:
                async with app.run_test(size=(100, 32)) as pilot:
                    call("edit_file", {"path": "app.py", "old_text": "a = 1", "new_text": "a = 2"})
                    self.assertIs(app.undo_history, initial_undo)
                    await app._new_session()
                    session_b = app.session_id
                    store.rename_session(session_b, "Chat B")
                    history_b = app.undo_history
                    self.assertEqual(history_b, [])
                    self.assertIsNot(history_b, initial_undo)
                    self.assertIn("no recent", call("undo_file_change", {}))
                    self.assertEqual((root / "app.py").read_text(), "a = 2\n")
                    call("write_file", {"path": "notes.txt", "content": "New note\n"})

                    await app._load_session(session_a)
                    self.assertIs(app.undo_history, initial_undo)
                    # Exercise the TUI's actual turn dispatcher with an offline model response.
                    with patch.object(app.provider, "complete", side_effect=[
                        ModelResponse(tool_calls=[ToolCall("undo_a", "undo_file_change", {})]),
                        ModelResponse("Undone."),
                    ]), patch.object(app, "_request_approval", return_value=True) as approval:
                        app._run_turn(app.history, app.provider, app.tools, app.project_root, Event())
                        await pilot.pause()
                        approval.assert_called_once()
                        self.assertEqual(approval.call_args.args[0], "Undo change to app.py")
                    self.assertEqual((root / "app.py").read_text(), "a = 1\n")
                    self.assertEqual((root / "notes.txt").read_text(), "New note\n")
                    self.assertEqual(initial_undo, [])
                    self.assertEqual(len(history_b), 1)

                    await app._load_session(session_b)
                    self.assertIs(app.undo_history, history_b)
                    self.assertIn("cancelled", call("undo_file_change", {}, approved=False))
                    self.assertEqual(len(history_b), 1)
                    (root / "notes.txt").write_text("User's later edit\n")
                    with self.assertRaisesRegex(RuntimeError, "changed after Oryn"):
                        call("undo_file_change", {})
                    self.assertEqual((root / "notes.txt").read_text(), "User's later edit\n")
                    self.assertEqual(len(history_b), 1)

                    other_root = root / "other-project"
                    other_root.mkdir()
                    (other_root / "notes.txt").write_text("Other project's note\n")
                    app._switch_project(str(other_root))
                    await app.workers.wait_for_complete()
                    self.assertEqual(app.undo_history, [])
                    self.assertIn("no recent", call("undo_file_change", {}))
                    self.assertEqual((other_root / "notes.txt").read_text(), "Other project's note\n")
                    await app._load_session(session_a)
                    await pilot.press("ctrl+o")
                    await pilot.pause()
                    app.screen.query_one(Input).value = "Chat B"
                    await pilot.pause()
                    await pilot.press("ctrl+d", "ctrl+d")
                    self.assertFalse(store.session_exists(session_b))
                    self.assertNotIn(session_b, app.file_change_history)
                    self.assertIs(app.undo_history, initial_undo)
                    self.assertEqual((root / "notes.txt").read_text(), "User's later edit\n")
                    await pilot.press("escape")
            finally:
                client.close()

    async def test_commands_work_during_a_tool_operation_and_preserve_drafts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "notes.txt").write_text("Example note\n")
            store = SQLiteSessionStore(root / "sessions.sqlite3")
            session = store.create_session(root)
            client = MCPClient(configs=[])
            provider = CodexProvider("gpt-5.6-luna", root / "auth.json")
            app = OrynTUI(
                store=store, session_id=session, project_root=root, model=provider.model,
                history=[], provider=provider, mcp_client=client, initial_tools=tool_schemas(),
            )
            started, release = Event(), Event()

            def paused_tool(*args, **kwargs):
                started.set()
                if not release.wait(20):
                    raise RuntimeError("Timed out waiting to finish the example tool operation.")
                return execute_tool(*args, **kwargs)

            try:
                with patch.object(provider, "complete", side_effect=[
                    ModelResponse(tool_calls=[ToolCall("read_note", "read_file", {"path": "notes.txt"})]),
                    ModelResponse("Read the note."),
                ]), patch("src.agent.conversation_loop.execute_tool", side_effect=paused_tool):
                    async with app.run_test(size=(100, 32)) as pilot:
                        try:
                            composer = app.query_one("#composer", TextArea)
                            palette = app.query_one("#palette-overlay")
                            composer.load_text("Read notes.txt")
                            await pilot.press("enter")
                            await pilot.pause()
                            self.assertTrue(started.is_set())
                            self.assertTrue(app.turn_active)
                            composer.load_text("/")
                            await pilot.pause()
                            self.assertTrue(palette.display)
                            await pilot.press("down")
                            self.assertEqual(app.query_one("#palette-options", OptionList).highlighted_option.id, "help")
                            for command, screen_type in [("/help", InfoScreen), ("/to", ToolsScreen)]:
                                composer.load_text(command)
                                await pilot.pause()
                                await pilot.press("enter")
                                await pilot.pause()
                                self.assertIsInstance(app.screen, screen_type)
                                self.assertTrue(app.turn_active)
                                await pilot.press("ctrl+p")
                                self.assertFalse(palette.display)
                                await pilot.press("escape")
                            composer.load_text("Keep my next message")
                            await pilot.press("enter")
                            self.assertEqual(composer.text, "Keep my next message")
                            self.assertEqual(sum(m.get("role") == "user" for m in app.history), 1)
                            await pilot.press("ctrl+p")
                            await pilot.pause()
                            self.assertTrue(palette.display)
                            self.assertEqual(composer.text, "/")
                            await pilot.press("escape")
                            self.assertEqual(composer.text, "Keep my next message")
                            for command in ("/models", "/new"):
                                composer.load_text(command)
                                await pilot.pause()
                                await pilot.press("enter")
                                self.assertEqual(composer.text, command)
                                self.assertTrue(palette.display)
                                self.assertEqual(app.session_id, session)
                                self.assertEqual(app.model, "gpt-5.6-luna")
                                self.assertIn("Finish the current turn", str(app.query_one("#activity-label", Static).content))
                            composer.load_text("/mcps")
                            await pilot.pause()
                            await pilot.press("enter")
                            await pilot.pause()
                            self.assertIsInstance(app.screen, MCPManagerScreen)
                            self.assertTrue(app.screen.query_one("#mcp-toggle", Button).disabled)
                            release.set()
                            app._turn_thread.join(timeout=2)
                            await pilot.pause()
                            self.assertFalse(app.turn_active)
                            self.assertIsInstance(app.screen, MCPManagerScreen)
                            self.assertTrue(app.screen.query_one(Input).has_focus)
                            await pilot.press("escape")
                            self.assertTrue(composer.has_focus)
                        finally:
                            release.set()
                            if hasattr(app, "_turn_thread"):
                                app._turn_thread.join(timeout=2)
            finally:
                client.close()

    async def test_reply_duration_covers_the_turn_and_survives_reopening(self):
        for seconds, expected in [(0, "0.0s"), (12.36, "12.4s"), (59.96, "1m 0.0s"), (125.37, "2m 5.4s")]:
            self.assertEqual(MessageCard("assistant", elapsed_seconds=seconds)._meta_label().plain,
                             f"▣ Oryn · {expected}")
        for invalid in (None, True, "bad", -1, float("nan"), float("inf"), 10 ** 500):
            self.assertEqual(MessageCard("assistant", elapsed_seconds=invalid)._meta_label().plain, "▣ Oryn")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "notes.txt").write_text("Example note\n")
            store = SQLiteSessionStore(root / "sessions.sqlite3")
            session = store.create_session(root)
            store.append_messages([{"role": "assistant", "content": "Older reply without timing."}], session)
            client = MCPClient(configs=[])
            provider = CodexProvider("gpt-5.6-luna", root / "auth.json")
            app = OrynTUI(
                store=store, session_id=session, project_root=root, model=provider.model,
                history=store.load_messages(session), provider=provider, mcp_client=client,
                initial_tools=tool_schemas(),
            )
            cases = [
                (None, 12.36, "12.4s", None),
                (ConnectionError("Example failure"), 65.44, "1m 5.4s", "failed"),
                (TurnCancelled("Stopped by you"), 1.24, "1.2s", "cancelled"),
            ]
            try:
                async with app.run_test(size=(100, 32)) as pilot:
                    self.assertEqual(str(app.query_one(".message-meta", Label).content), "▣ Oryn")
                    for index, (error, duration, label, status) in enumerate(cases):
                        def partial_reply(*args, **kwargs):
                            kwargs["on_text_delta"]("Partial reply.")
                            raise error

                        responses = partial_reply if error else [
                            ModelResponse(tool_calls=[ToolCall("read_note", "read_file", {"path": "notes.txt"})]),
                            ModelResponse("Finished reply."),
                        ]
                        with patch("src.tui_app.monotonic", side_effect=[1000.0, 1000.0 + duration]), \
                             patch.object(provider, "complete", side_effect=responses):
                            app.query_one("#composer", TextArea).load_text(f"Request {index}")
                            await pilot.press("enter")
                            app._turn_thread.join(timeout=2)
                            await pilot.pause()
                        self.assertFalse(app.turn_active)
                        self.assertIsNone(app._turn_started_at)
                        card = list(app.query(MessageCard))[-1]
                        self.assertEqual(str(card.query_one(".message-meta", Label).content), f"▣ Oryn · {label}")
                        self.assertEqual(card.turn_status, status)
                        self.assertAlmostEqual(card.elapsed_seconds, duration)
                        saved = store.load_messages(session)[-1]
                        self.assertAlmostEqual(saved["elapsed_seconds"], duration)
                        self.assertEqual(saved.get("turn_status"), status)
                    prepared = select_context(app.history)
                    self.assertTrue(any("elapsed_seconds" in m for m in app.history))
                    self.assertTrue(all("elapsed_seconds" not in m for m in prepared))
            finally:
                client.close()

            reopened_store = SQLiteSessionStore(store.db_path)
            reopened_client = MCPClient(configs=[])
            reopened = OrynTUI(
                store=reopened_store, session_id=session, project_root=root, model=provider.model,
                history=reopened_store.load_messages(session), provider=CodexProvider(provider.model, root / "auth.json"),
                mcp_client=reopened_client, initial_tools=tool_schemas(),
            )
            try:
                async with reopened.run_test(size=(64, 24)) as pilot:
                    for refresh in (False, True):
                        if refresh:
                            await reopened._load_session(session)
                        labels = [
                            str(card.query_one(".message-meta", Label).content)
                            for card in reopened.query(MessageCard) if card.elapsed_seconds is not None
                        ]
                        self.assertEqual(labels, [f"▣ Oryn · {case[2]}" for case in cases])
                        await pilot.pause()
            finally:
                reopened_client.close()

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
