"""Full-screen terminal chat for Oryn."""

import argparse
import json
import threading
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from src.agent.conversation_loop import TurnCancelled, run_turn
from src.agent.project_context import load_project_instructions
from src.agent.system_prompt import SYSTEM_PROMPT
from src.chat_demo import APP_ROOT, _resolve_project_root
from src.mcp.client import MCPClient
from src.providers.codex import CodexProvider
from src.providers.types import ToolCall
from src.session.sqlite_store import DEFAULT_DB_PATH, SESSION_ID, SQLiteSessionStore
from src.tools.file_tools import FileChange
from src.tools.registry import tool_schemas


DEFAULT_MODEL = "gpt-5.6-luna"
COMMANDS = (
    ("new", "Start a fresh session"),
    ("sessions", "Find and resume a saved session"),
    ("model", "Change the model for this session"),
    ("project", "Switch the active project folder"),
    ("tools", "See tools available to Oryn"),
    ("mcp", "Check MCP connection status"),
    ("help", "Show commands and keyboard shortcuts"),
    ("quit", "Close Oryn"),
)


class StreamChunk(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class ToolActivity(Message):
    def __init__(self, phase: str, call: ToolCall, result: str | None) -> None:
        super().__init__()
        self.phase = phase
        self.call = call
        self.result = result


class TurnFinished(Message):
    def __init__(self, answer: str | None, error: str | None, cancelled: bool = False) -> None:
        super().__init__()
        self.answer = answer
        self.error = error
        self.cancelled = cancelled


class MCPReady(Message):
    def __init__(self, statuses: list[str], schemas: list[dict[str, Any]]) -> None:
        super().__init__()
        self.statuses = statuses
        self.schemas = schemas


class ApprovalRequest(Message):
    def __init__(self, title: str, preview: str) -> None:
        super().__init__()
        self.title = title
        self.preview = preview
        self.event = threading.Event()
        self.approved = False

    def resolve(self, approved: bool) -> None:
        self.approved = approved
        self.event.set()


class MessageCard(Vertical):
    def __init__(self, role: str, content: str = "", turn_status: str | None = None) -> None:
        self.role = role
        self.content = content
        self.turn_status = turn_status if turn_status in {"cancelled", "failed"} else None
        super().__init__(classes=f"message-card {role}")

    def compose(self) -> ComposeResult:
        label = "YOU" if self.role == "user" else "ORYN"
        yield Label(label, classes="message-label")
        status = Label(
            self._status_label(),
            id="message-status",
            classes=f"message-status {self.turn_status}" if self.turn_status else "message-status",
        )
        status.display = self.turn_status is not None
        yield status
        yield Static(self._renderable(), classes="message-copy")

    def _renderable(self) -> Any:
        if not self.content:
            if self.turn_status:
                return Text("No partial answer was returned.", style="dim italic")
            return Text("Thinking…", style="dim italic")
        return Markdown(self.content)

    def _status_label(self) -> str:
        return {
            "cancelled": "Stopped before finishing",
            "failed": "Couldn't finish this reply",
        }.get(self.turn_status, "")

    def update_content(self, content: str) -> None:
        self.content = content
        self.query_one(".message-copy", Static).update(self._renderable())

    def set_turn_status(self, status: str) -> None:
        self.turn_status = status if status in {"cancelled", "failed"} else None
        label = self.query_one("#message-status", Label)
        label.update(self._status_label())
        label.display = self.turn_status is not None
        label.set_class(self.turn_status == "cancelled", "cancelled")
        label.set_class(self.turn_status == "failed", "failed")


class PaletteScreen(ModalScreen[tuple[str, str | None] | None]):
    BINDINGS = [Binding("escape", "close", "Close", show=False)]

    def __init__(self, query: str = "") -> None:
        super().__init__()
        self.search_text = query.strip().lstrip("/")

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-card"):
            yield Label("ORYN  /  COMMANDS", id="palette-title")
            yield Input(value=self.search_text, placeholder="Search sessions, model, tools…", id="palette-search")
            yield OptionList(id="palette-options", markup=False)
            yield Static("↑ ↓ navigate   ·   Enter select   ·   Esc close", id="palette-hint")

    def on_mount(self) -> None:
        self._filter(self.search_text)
        self.query_one("#palette-search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "palette-search":
            self.search_text = event.value
            self._filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip().lstrip("/")
        parts = query.split(maxsplit=1)
        known = {name for name, _ in COMMANDS}
        if parts and parts[0] in known:
            self.dismiss((parts[0], parts[1] if len(parts) > 1 else None))
            return
        highlighted = self.query_one("#palette-options", OptionList).highlighted_option
        if highlighted and highlighted.id:
            self.dismiss((highlighted.id, None))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.dismiss((event.option.id, None))

    def on_key(self, event) -> None:
        if event.key == "down" and self.query_one("#palette-search", Input).has_focus:
            options = self.query_one("#palette-options", OptionList)
            if options.option_count:
                options.focus()
                event.stop()

    def _filter(self, query: str) -> None:
        options = self.query_one("#palette-options", OptionList)
        filter_text = query.strip().lstrip("/").casefold()
        matches = [
            (name, description) for name, description in COMMANDS
            if filter_text in name.casefold() or filter_text in description.casefold()
        ]
        options.set_options([
            Option(Text(f"/{name:<12} {description}"), id=name)
            for name, description in matches
        ])
        if matches:
            options.highlighted = 0

    def action_close(self) -> None:
        self.dismiss(None)


class ChoiceScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "close", "Close", show=False)]

    def __init__(self, title: str, choices: list[tuple[str, str]]) -> None:
        super().__init__()
        self.title = title
        self.choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-card"):
            yield Label(self.title, id="picker-title")
            if self.choices:
                yield OptionList(*[
                    Option(Text(label), id=choice_id)
                    for choice_id, label in self.choices
                ], id="picker-options", markup=False)
            else:
                yield Static("No saved sessions yet.", id="picker-empty")
            yield Static("↑ ↓ navigate   ·   Enter select   ·   Esc close", classes="modal-hint")

    def on_mount(self) -> None:
        if self.choices:
            self.query_one("#picker-options", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.dismiss(event.option.id)

    def action_close(self) -> None:
        self.dismiss(None)


class TextPromptScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "close", "Close", show=False)]

    def __init__(self, title: str, initial: str, placeholder: str) -> None:
        super().__init__()
        self.title = title
        self.initial = initial
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-card"):
            yield Label(self.title, id="picker-title")
            yield Input(value=self.initial, placeholder=self.placeholder, id="value-input")
            with Horizontal(classes="modal-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Apply", variant="primary", id="apply")
            yield Static("Enter apply   ·   Esc close", classes="modal-hint")

    def on_mount(self) -> None:
        field = self.query_one("#value-input", Input)
        field.focus()
        field.cursor_position = len(field.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply":
            self.dismiss(self.query_one("#value-input", Input).value)
        else:
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class InfoScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close", show=False)]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.title = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="info-card"):
            yield Label(self.title, id="picker-title")
            with VerticalScroll(id="info-scroll"):
                yield Static(Text(self.body), markup=False)
            yield Button("Close", id="close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class ApprovalScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "allow", "Allow", show=False),
        Binding("n", "deny", "Deny", show=False),
        Binding("escape", "deny", "Deny", show=False),
    ]

    def __init__(self, title: str, preview: str) -> None:
        super().__init__()
        self.title = title
        self.preview = preview

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-card"):
            yield Label("APPROVAL REQUIRED", id="approval-eyebrow")
            yield Label(self.title, id="picker-title")
            with VerticalScroll(id="approval-preview"):
                yield Static(Text(self.preview), markup=False)
            with Horizontal(classes="modal-buttons"):
                yield Button("Deny · Esc", id="deny")
                yield Button("Allow · Y", variant="primary", id="allow")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow")

    def action_allow(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


class OrynTUI(App[None]):
    """A Textual view over Oryn's existing Python harness."""

    CSS_PATH = "tui.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+p", "open_palette", "Commands"),
        Binding("ctrl+n", "new_session", "New chat"),
        Binding("ctrl+o", "open_sessions", "Sessions"),
        Binding("f2", "choose_model", "Model"),
        Binding("ctrl+c", "stop_or_quit", "Stop/Quit", priority=True),
        Binding("ctrl+enter", "send_prompt", "Send", priority=True),
    ]

    def __init__(
        self,
        *,
        store: SQLiteSessionStore,
        session_id: str,
        project_root: Path,
        model: str,
        history: list[dict[str, Any]],
        provider: CodexProvider,
        mcp_client: MCPClient,
        initial_tools: list[dict[str, Any]],
        undo_history: list[FileChange] | None = None,
    ) -> None:
        super().__init__()
        self.store = store
        self.session_id = session_id
        self.project_root = project_root
        self.model = model
        self.history = history
        self.saved_count = len(history)
        self.provider = provider
        self.mcp_client = mcp_client
        self.tools = initial_tools
        self.undo_history = undo_history if undo_history is not None else []
        self.mcp_statuses: list[str] = []
        self.mcp_ready = False
        self.turn_active = False
        self._cancel_event: threading.Event | None = None
        self._current_reply: MessageCard | None = None
        self._reply_text = ""
        self._partial_reply_text = ""
        self._turn_start = 0
        self._pending_approval: ApprovalRequest | None = None
        self._opening_palette = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                yield Static("◉  ORYN", id="brand")
                yield Static("LOCAL CODING AGENT", classes="eyebrow")
                yield Static("PROJECT", classes="section-label")
                yield Static(self.project_root.name or str(self.project_root), id="project-name")
                yield Static(str(self.project_root), id="project-path", markup=False)
                yield Static("SESSIONS", classes="section-label")
                yield Static(self._sidebar_sessions(), id="session-list", markup=False)
                yield Static("Ctrl+N  new chat\nCtrl+O  sessions\nF2       model", id="sidebar-keys", markup=False)
            with Vertical(id="main-panel"):
                with Horizontal(id="topbar"):
                    yield Static("Oryn", id="topbar-title")
                    yield Static(self.project_root.name or "Project", id="topbar-project")
                    yield Static(self.model, id="model-chip")
                with VerticalScroll(id="transcript"):
                    visible = [
                        message for message in self.history
                        if message.get("role") in {"user", "assistant"}
                    ]
                    if visible:
                        for message in visible:
                            yield MessageCard(
                                message["role"], message.get("content", ""), message.get("turn_status"),
                            )
                    else:
                        with Vertical(id="welcome"):
                            yield Static("◉", id="welcome-mark")
                            yield Static("A clear space to think.", id="welcome-title")
                            yield Static(
                                "Ask Oryn to understand a project, explain a concept, or help change code.",
                                id="welcome-copy",
                            )
                            yield Static("Type  /  to browse commands", id="welcome-hint")
                with Horizontal(id="activity-row"):
                    yield Static("●", id="activity-dot")
                    yield Static("Ready", id="activity-label")
                    yield Static(f"{len(self.tools)} tools", id="tool-count")
                with Vertical(id="composer-frame"):
                    yield TextArea(
                        id="composer",
                        placeholder="Ask Oryn anything…",
                        tab_behavior="indent",
                        highlight_cursor_line=False,
                    )
                    with Horizontal(id="composer-footer"):
                        yield Static("/ commands   ·   Ctrl+P actions", id="composer-hint")
                        yield Static("Ctrl+Enter  send", id="send-hint")
                yield Footer()

    def on_mount(self) -> None:
        self.query_one("#composer", TextArea).focus()
        self.call_after_refresh(self._scroll_to_bottom)
        # MCP startup can be slow; keep it outside Textual's executor and event loop.
        self._mcp_thread = threading.Thread(
            target=self._connect_mcp, name="oryn-mcp-connect", daemon=True,
        )
        self._mcp_thread.start()

    def on_resize(self, event) -> None:
        compact = event.size.width < 94
        self.query_one("#sidebar").display = not compact
        self.query_one("#transcript").styles.padding = (1, 2 if compact else 5)
        margin = 1 if compact else 3
        self.query_one("#composer-frame").styles.margin = (0, margin, 1, margin)

    def on_unmount(self) -> None:
        self._cancel_active_turn()
        if self._pending_approval:
            self._pending_approval.resolve(False)
            self._pending_approval = None

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "composer" or self._opening_palette or self.turn_active:
            return
        value = event.text_area.text
        if value.startswith("/"):
            query = value[1:]
            self._opening_palette = True
            event.text_area.clear()
            self.push_screen(PaletteScreen(query), self._palette_chosen)
            self._opening_palette = False

    def on_stream_chunk(self, event: StreamChunk) -> None:
        self._reply_text += event.text
        self._partial_reply_text += event.text
        if self._current_reply:
            self._current_reply.update_content(self._reply_text)
        self._scroll_to_bottom()

    def on_tool_activity(self, event: ToolActivity) -> None:
        if event.phase == "start":
            detail = next((
                event.call.arguments.get(key)
                for key in ("path", "url", "query", "command", "ref")
                if event.call.arguments.get(key)
            ), None)
            label = f"Running {event.call.name}"
            if detail is not None:
                label += f"  ·  {str(detail)[:80]}"
            self._set_activity(label, working=True)
        elif event.result is not None:
            preview = " ".join(event.result.split())[:150]
            self._partial_reply_text = ""
            self._set_activity(f"{event.call.name} finished  ·  {preview}", working=False)

    def on_turn_finished(self, event: TurnFinished) -> None:
        self.turn_active = False
        if event.error:
            self._set_activity(
                f"Turn interrupted  ·  {event.error}" if event.cancelled else f"Turn failed  ·  {event.error}",
                working=False,
                error=True,
            )
            status = "cancelled" if event.cancelled else "failed"
            for message in self.history[self._turn_start:]:
                if message.get("role") == "assistant" and (message.get("content") or message.get("tool_calls")):
                    message["turn_status"] = status
            if self._partial_reply_text:
                self.history.append({
                    "role": "assistant",
                    "content": self._partial_reply_text,
                    "turn_status": status,
                })
            if self._current_reply:
                self._current_reply.set_turn_status(status)
                self._current_reply.update_content(self._reply_text)
        else:
            self.history.append({"role": "assistant", "content": event.answer or ""})
            self._set_activity("Ready", working=False)
        self._current_reply = None
        self._reply_text = ""
        self._partial_reply_text = ""
        try:
            self.store.append_messages(self.history[self.saved_count:], self.session_id)
            self.saved_count = len(self.history)
        except Exception as exc:
            self._set_activity(f"Could not save this session: {exc}", working=False, error=True)
        self.query_one("#composer", TextArea).focus()
        self._refresh_sidebar()

    def on_mcp_ready(self, event: MCPReady) -> None:
        self.mcp_ready = True
        self.mcp_statuses = event.statuses
        self.tools = tool_schemas(event.schemas)
        self.query_one("#tool-count", Static).update(f"{len(self.tools)} tools")
        if not self.turn_active:
            self._set_activity("Ready · MCP connected" if event.schemas else "Ready", working=False)

    def on_approval_request(self, request: ApprovalRequest) -> None:
        if self._pending_approval:
            self._pending_approval.resolve(False)
        self._pending_approval = request
        self.push_screen(
            ApprovalScreen(request.title, request.preview),
            lambda approved: self._resolve_approval(request, bool(approved)),
        )

    async def action_send_prompt(self) -> None:
        if self.turn_active:
            self._set_activity("Oryn is still working. Your draft is safe; send it when this turn finishes.", working=True)
            return
        composer = self.query_one("#composer", TextArea)
        prompt = composer.text.strip()
        if not prompt:
            return
        if prompt.startswith("/"):
            name, _, argument = prompt[1:].partition(" ")
            self.run_worker(
                self._execute_command(name, argument.strip() or None),
                group="commands",
                exclusive=True,
            )
            composer.clear()
            return

        welcome = self.query("#welcome")
        if welcome:
            await welcome.remove()
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(MessageCard("user", prompt))
        self._current_reply = MessageCard("assistant")
        await transcript.mount(self._current_reply)
        self._scroll_to_bottom()
        composer.clear()
        self.history.append({"role": "user", "content": prompt})
        self._turn_start = len(self.history) - 1
        self.turn_active = True
        self._reply_text = ""
        self._partial_reply_text = ""
        self._set_activity("Oryn is thinking", working=True)
        self._cancel_event = threading.Event()
        # Provider and tool calls block, but must not hold the TUI open at shutdown.
        self._turn_thread = threading.Thread(
            target=self._run_turn,
            args=(self.history, self.provider, list(self.tools), self.project_root, self._cancel_event),
            name="oryn-agent-turn",
            daemon=True,
        )
        self._turn_thread.start()

    def action_open_palette(self) -> None:
        if self.turn_active:
            self._set_activity("Finish the current turn before opening commands.", working=True)
            return
        self.run_worker(self._show_palette(), group="commands", exclusive=True)

    async def action_new_session(self) -> None:
        if not self.turn_active:
            await self._new_session()

    def action_stop_or_quit(self) -> None:
        if not self.turn_active:
            self.exit()
            return
        self._cancel_active_turn()
        self._set_activity("Stopping Oryn…", working=True)
        if self._pending_approval:
            request = self._pending_approval
            self._pending_approval = None
            request.resolve(False)
            self.pop_screen()

    def action_open_sessions(self) -> None:
        if not self.turn_active:
            self.run_worker(self._show_sessions(), group="commands", exclusive=True)

    def action_choose_model(self) -> None:
        if not self.turn_active:
            self.run_worker(self._change_model(), group="commands", exclusive=True)

    async def _show_palette(self, query: str = "") -> None:
        result = await self.push_screen_wait(PaletteScreen(query))
        if result:
            await self._execute_command(*result)

    def _palette_chosen(self, result: tuple[str, str | None] | None) -> None:
        if result:
            self.run_worker(self._execute_command(*result), group="commands", exclusive=True)

    async def _execute_command(self, name: str, argument: str | None = None) -> None:
        if self.turn_active and name not in {"help", "tools", "mcp"}:
            self._set_activity("Finish the current turn before changing sessions or settings.", working=True)
            return
        if name in {"new", "clear"}:
            await self._new_session()
        elif name in {"sessions", "resume", "continue"}:
            await self._show_sessions()
        elif name in {"model", "models"}:
            if argument:
                self._save_model(argument)
            else:
                await self._change_model()
        elif name == "project":
            if argument:
                self._switch_project(argument)
            else:
                await self._choose_project()
        elif name == "tools":
            body = "\n".join(
                f"{tool.get('name', 'tool')} — {tool.get('description', '').split('.')[0]}"
                for tool in self.tools
            ) or "No tools are currently available."
            self.push_screen(InfoScreen("TOOLS AVAILABLE TO ORYN", body))
        elif name == "mcp":
            body = (
                "MCP servers are still connecting. Local tools remain available."
                if not self.mcp_ready else "\n".join(self.mcp_statuses) or "No MCP servers are configured."
            )
            self.push_screen(InfoScreen("MCP STATUS", body))
        elif name == "help":
            body = (
                "COMMANDS\n"
                "/new       Start a new session\n"
                "/sessions  Resume a saved session\n"
                "/model     Change this session's model\n"
                "/project   Switch project folder\n"
                "/tools     List available tools\n"
                "/mcp       Show MCP connection status\n"
                "/help      Show this guide\n"
                "/quit      Close Oryn\n\n"
                "KEYS\n"
                "Ctrl+P    Open command palette\n"
                "Ctrl+N    New session\n"
                "Ctrl+O    Session picker\n"
                "F2        Change model\n"
                "Ctrl+Enter Send message\n"
                "Ctrl+C    Stop turn, or quit when idle\n"
                "Enter     Add a new line\n"
                "Esc       Close a dialog or deny approval"
            )
            self.push_screen(InfoScreen("ORYN QUICK GUIDE", body))
        elif name in {"quit", "exit", "q"}:
            self.exit()
        elif name:
            self._set_activity(f"Unknown command: /{name}. Type /help for commands.", working=False, error=True)

    async def _new_session(self) -> None:
        self.session_id = self.store.create_session(self.project_root)
        self.store.set_session_model(self.session_id, self.model)
        await self._load_session(self.session_id)
        self._set_activity("New session ready", working=False)

    async def _show_sessions(self) -> None:
        sessions = self.store.list_sessions()
        choices: list[tuple[str, str]] = []
        for session_id in sessions:
            root = self.store.session_project_root(session_id)
            root = root or str(APP_ROOT)
            title = self.store.session_title(session_id)
            messages = self.store.load_messages(session_id)
            first_user = next((
                " ".join(str(message.get("content", "")).split())
                for message in messages if message.get("role") == "user"
            ), "Empty session")
            title = title or first_user[:54]
            label = f"{title}   ·   {Path(root).name}   ·   {session_id[:8]}"
            choices.append((session_id, label))
        result = await self.push_screen_wait(ChoiceScreen("SAVED SESSIONS", choices))
        if result:
            await self._load_session(result)

    async def _load_session(self, session_id: str) -> None:
        saved_root = self.store.session_project_root(session_id)
        project_root = _resolve_project_root(Path(saved_root) if saved_root else APP_ROOT)
        self.project_root = project_root
        self.session_id = session_id
        self.model = self.store.session_model(session_id) or DEFAULT_MODEL
        self.provider = CodexProvider(self.model)
        self.history = _base_history(project_root)
        self.history.extend(self.store.load_messages(session_id))
        self.saved_count = len(self.history)
        self._refresh_header()
        await self._refresh_transcript()

    async def _refresh_transcript(self) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.remove_children()
        visible = [
            message for message in self.history
            if message.get("role") in {"user", "assistant"}
        ]
        if visible:
            for message in visible:
                await transcript.mount(MessageCard(
                    message["role"], message.get("content", ""), message.get("turn_status"),
                ))
        else:
            await transcript.mount(
                Static("◉\n\nA clear space to think.\n\nAsk Oryn about this project, then type / to browse commands.", id="welcome")
            )
        self._refresh_header()
        self._scroll_to_bottom()

    async def _change_model(self) -> None:
        result = await self.push_screen_wait(TextPromptScreen(
            "MODEL FOR THIS SESSION",
            self.model,
            "Enter a Codex model ID, for example gpt-5.6-luna",
        ))
        if result:
            self._save_model(result)

    def _save_model(self, model: str) -> None:
        try:
            self.store.set_session_model(self.session_id, model)
        except ValueError as exc:
            self._set_activity(str(exc), working=False, error=True)
            return
        self.model = model.strip()
        self.provider = CodexProvider(self.model)
        self._refresh_header()
        self._set_activity(f"Model set to {self.model}", working=False)

    async def _choose_project(self) -> None:
        result = await self.push_screen_wait(TextPromptScreen(
            "OPEN PROJECT FOLDER",
            str(self.project_root),
            "/path/to/project",
        ))
        if result:
            self._switch_project(result)

    def _switch_project(self, path: str) -> None:
        try:
            project_root = _resolve_project_root(Path(path))
        except (ValueError, OSError) as exc:
            self._set_activity(str(exc), working=False, error=True)
            return
        self.project_root = project_root
        self.session_id = self.store.create_session(project_root)
        self.store.set_session_model(self.session_id, self.model)
        self.history = _base_history(project_root)
        self.saved_count = len(self.history)
        self._refresh_header()
        self.run_worker(self._refresh_transcript(), group="ui")
        self._set_activity(f"Project opened · {project_root}", working=False)

    def _refresh_header(self) -> None:
        self.query_one("#project-name", Static).update(self.project_root.name or str(self.project_root))
        self.query_one("#project-path", Static).update(str(self.project_root))
        self.query_one("#topbar-project", Static).update(self.project_root.name or "Project")
        self.query_one("#model-chip", Static).update(self.model)
        self._refresh_sidebar()

    def _sidebar_sessions(self) -> str:
        return self._session_summary()

    def _session_summary(self) -> str:
        entries = []
        for session_id in self.store.list_sessions():
            root = self.store.session_project_root(session_id)
            if (root or str(APP_ROOT)) != str(self.project_root):
                continue
            title = self.store.session_title(session_id)
            if not title:
                messages = self.store.load_messages(session_id)
                title = next((
                    " ".join(str(message.get("content", "")).split())
                    for message in messages if message.get("role") == "user"
                ), "New session")
            marker = "› " if session_id == self.session_id else "  "
            entries.append(f"{marker}{title[:30]}")
            if len(entries) == 5:
                break
        return "\n".join(entries) if entries else "No chats yet"

    def _refresh_sidebar(self) -> None:
        self.query_one("#session-list", Static).update(self._session_summary())

    def _set_activity(self, text: str, *, working: bool, error: bool = False) -> None:
        dot = self.query_one("#activity-dot", Static)
        label = self.query_one("#activity-label", Static)
        dot.update("◌" if working else "●")
        dot.set_class(working, "working")
        label.update(text)
        label.set_class(error, "error")

    def _scroll_to_bottom(self) -> None:
        self.query_one("#transcript", VerticalScroll).scroll_end(animate=False)

    def _resolve_approval(self, request: ApprovalRequest, approved: bool) -> None:
        request.resolve(approved)
        if self._pending_approval is request:
            self._pending_approval = None

    def _request_approval(self, title: str, preview: str) -> bool:
        request = ApprovalRequest(title, preview[:12_000])
        if not self.post_message(request):
            return False
        request.event.wait()
        return request.approved

    def _cancel_active_turn(self) -> None:
        if hasattr(self, "_cancel_event") and self._cancel_event:
            self._cancel_event.set()

    def _run_turn(
        self,
        history: list[dict[str, Any]],
        provider: CodexProvider,
        tools: list[dict[str, Any]],
        project_root: Path,
        cancel_event: threading.Event,
    ) -> None:
        confirm_terminal = lambda command: self._request_approval(
            "Run terminal command", command,
        )
        confirm_write = lambda path, content, exists: self._request_approval(
            f"{'Replace' if exists else 'Create'} {path}", content,
        )
        confirm_edit = lambda path, diff: self._request_approval(f"Apply edit to {path}", diff)
        confirm_undo = lambda path, diff, created: self._request_approval(
            f"Undo change to {path}", diff,
        )
        confirm_mcp = lambda name, preview: self._request_approval(f"Call MCP tool {name}", preview)
        try:
            answer = run_turn(
                history,
                provider.complete,
                tools,
                project_root,
                confirm_terminal,
                confirm_write,
                on_text_delta=lambda delta: self.post_message(StreamChunk(delta)),
                on_tool_event=lambda phase, call, result: self.post_message(
                    ToolActivity(phase, call, result)
                ),
                cancel_event=cancel_event,
                confirm_edit=confirm_edit,
                confirm_undo=confirm_undo,
                undo_history=self.undo_history,
                mcp_client=self.mcp_client,
                confirm_mcp=confirm_mcp,
            )
        except TurnCancelled as exc:
            self.post_message(TurnFinished(None, str(exc) or "Stopped by you", cancelled=True))
        except Exception as exc:
            self.post_message(TurnFinished(None, str(exc)))
        else:
            self.post_message(TurnFinished(answer, None))

    def _connect_mcp(self) -> None:
        try:
            statuses = self.mcp_client.start()
            schemas = self.mcp_client.tool_schemas()
        except Exception as exc:
            statuses = [f"MCP startup failed: {exc}"]
            schemas = []
        self.post_message(MCPReady(statuses, schemas))


def _base_history(project_root: Path) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if project_root != APP_ROOT:
        history.append({
            "role": "developer",
            "content": f"Active project folder: {str(project_root)!r}. Tool paths are relative to it.",
        })
    instructions = load_project_instructions(project_root)
    if instructions:
        history.append({
            "role": "developer",
            "content": (
                "Project guidance from the root AGENTS.md follows. Apply it to work in "
                "this project unless it conflicts with the system prompt or user's request.\n\n"
                f"{instructions}"
            ),
        })
    return history


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Open Oryn's full-screen terminal chat.")
    parser.add_argument("--model", help="Codex model slug (defaults to the saved session model)")
    parser.add_argument("--project", type=Path, metavar="DIR", help="project folder (default: current folder)")
    session_options = parser.add_mutually_exclusive_group()
    session_options.add_argument("--new", action="store_true", help="start a new chat")
    session_options.add_argument("--list", action="store_true", help="list saved chats and exit")
    session_options.add_argument("--search", metavar="QUERY", help="search saved messages and exit")
    session_options.add_argument("--resume", metavar="ID", help="resume a saved chat")
    args = parser.parse_args(argv)
    if (args.list or args.search is not None) and args.project is not None:
        parser.error("--project cannot be used with --list or --search")

    try:
        store = SQLiteSessionStore()
        if args.list:
            sessions = store.list_sessions()
            print("Saved sessions:" if sessions else "No saved sessions yet.")
            for session_id in sessions:
                print(f"{session_id}  {store.session_project_root(session_id) or APP_ROOT}")
            return
        if args.search is not None:
            matches = store.search_messages(args.search)
            if not matches:
                print("No saved messages matched.")
            else:
                for session_id, project, role, snippet in matches:
                    print(f"{session_id}  {role}  {project or APP_ROOT}")
                    print(f"  {snippet}")
            return
        if args.resume:
            session_id = args.resume
            if not store.session_exists(session_id):
                parser.error(f"no saved session with ID {session_id}")
            saved_root = store.session_project_root(session_id)
            project_root = _resolve_project_root(Path(saved_root) if saved_root else APP_ROOT)
            if args.project and _resolve_project_root(args.project) != project_root:
                parser.error(f"session {session_id} belongs to {project_root}")
            if saved_root is None:
                store.bind_session_to_project(session_id, project_root)
        else:
            project_root = _resolve_project_root(args.project or Path("."))
            if args.new or args.project is not None or project_root != APP_ROOT:
                session_id = store.create_session(project_root)
            else:
                session_id = SESSION_ID
                store.bind_session_to_project(session_id, project_root)
        if args.model:
            store.set_session_model(session_id, args.model)
        model = args.model or store.session_model(session_id) or DEFAULT_MODEL
        if store.session_model(session_id) is None:
            store.set_session_model(session_id, model)
        history = _base_history(project_root)
        history.extend(store.load_messages(session_id))
        mcp_client = MCPClient()
        app = OrynTUI(
            store=store,
            session_id=session_id,
            project_root=project_root,
            model=model,
            history=history,
            provider=CodexProvider(model),
            mcp_client=mcp_client,
            initial_tools=tool_schemas(),
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    except Exception as exc:
        print(f"Could not access sessions at {DEFAULT_DB_PATH}: {exc}")
        return

    try:
        app.run()
    finally:
        mcp_client.close()


if __name__ == "__main__":
    main()
