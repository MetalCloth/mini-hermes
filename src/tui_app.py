"""Full-screen terminal chat for Oryn."""

import argparse
import asyncio
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from itertools import groupby
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from src.agent.conversation_loop import (
    TurnCancelled, TurnLimitReached, TurnLimits, add_turn_arguments, run_turn, turn_limits_from_args,
)
from src.agent.project_context import load_project_instructions
from src.agent.system_prompt import SYSTEM_PROMPT
from src.chat_demo import APP_ROOT, _resolve_project_root
from src.computer_logging import ComputerTrace
from src.images import MAX_IMAGES, prepare_image, read_clipboard_image
from src.mcp.client import MCPClient
from src.mcp.discovery import save_enabled_servers
from src.mcp.oauth import login_notion
from src.providers.codex import AUTH_FILE
from src.providers.router import available_models, provider_for_model, provider_setup_warning
from src.providers.types import ToolCall
from src.session.sqlite_store import DEFAULT_DB_PATH, SQLiteSessionStore
from src.tools.file_tools import FileChange
from src.tools.computer_session import ComputerSession
from src.tools.registry import tool_schemas
from src.tools.terminal_tool import TerminalJobManager


DEFAULT_MODEL = "gpt-5.6-luna"
COMMANDS = (
    ("new", "New session"),
    ("delete", "Delete this session and return home"),
    ("computer", "Use the computer for the next message"),
    ("help", "Show help"),
    ("mcps", "Manage MCP connections"),
    ("models", "Switch model"),
    ("effort", "Configure reasoning effort"),
    ("speed", "Configure model speed"),
    ("project", "Open project folder"),
    ("sessions", "Switch session"),
    ("tools", "Show available tools"),
    ("exit", "Exit the app"),
)
COMMAND_ALIASES = {"model": "models", "mcp": "mcps", "quit": "exit", "q": "exit"}
COMMANDS_DURING_TURN = {"help", "tools", "mcps", "models", "effort", "speed", "sessions", "new"}


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
    def __init__(
        self, answer: str | None, error: str | None, cancelled: bool = False,
        elapsed_seconds: float | None = None,
        *, paused: bool = False,
    ) -> None:
        super().__init__()
        self.answer = answer
        self.error = error
        self.cancelled = cancelled
        self.elapsed_seconds = elapsed_seconds
        self.paused = paused


class TurnProgress(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class MCPReady(Message):
    def __init__(
        self, statuses: list[str], schemas: list[dict[str, Any]],
        servers: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self.statuses = statuses
        self.schemas = schemas
        self.servers = servers or []


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


class ComputerQuestion(Message):
    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question
        self.event = threading.Event()
        self.answer: str | None = None

    def resolve(self, answer: str | None) -> None:
        self.answer = answer
        self.event.set()


class TranscriptScroll(VerticalScroll):
    follow_output = True

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self.follow_output = new_value >= self.max_scroll_y - 1


class MessageCard(Vertical):
    def __init__(
        self, role: str, content: str = "", turn_status: str | None = None,
        *, elapsed_seconds: float | None = None, images: list[dict[str, Any]] | None = None,
    ) -> None:
        self.role = role
        self.content = content
        self.turn_status = turn_status if turn_status in {"cancelled", "failed", "paused"} else None
        self.elapsed_seconds = elapsed_seconds
        self.images = images or []
        super().__init__(classes=f"message-card {role}")

    def compose(self) -> ComposeResult:
        with Vertical(classes="message-body"):
            status = Label(
                self._status_label(),
                id="message-status",
                classes=f"message-status {self.turn_status}" if self.turn_status else "message-status",
            )
            status.display = self.turn_status is not None
            yield status
            if self.images:
                label = Text("▧ ", style="#fab283")
                for index, image in enumerate(self.images):
                    if index:
                        label.append("  ·  ", style="#808080")
                    label.append(f"{image['name']} ({image['width']}×{image['height']})")
                yield Static(label, classes="message-images")
            yield Static(self._renderable(), classes="message-copy")
            if self.role == "assistant":
                yield Label(self._meta_label(), classes="message-meta")

    def _meta_label(self) -> Text:
        label = Text("▣ Oryn")
        seconds = self.elapsed_seconds
        if type(seconds) not in (int, float):
            return label
        try:
            if seconds < 0 or not isfinite(seconds):
                return label
            minutes, seconds = divmod(round(seconds, 1), 60)
        except (ValueError, OverflowError):
            return label
        duration = f"{int(minutes)}m {seconds:.1f}s" if minutes else f"{seconds:.1f}s"
        label.append(f" · {duration}", style="#808080")
        return label

    def set_elapsed_seconds(self, elapsed_seconds: float | None) -> None:
        self.elapsed_seconds = elapsed_seconds
        self.query_one(".message-meta", Label).update(self._meta_label())

    def _renderable(self) -> Any:
        if not self.content:
            if self.turn_status:
                return Text("No partial answer was returned.", style="dim italic")
            return Text("Thinking…", style="dim italic")
        return Text(self.content) if self.role == "user" else Markdown(self.content, code_theme="ansi_dark")

    def _status_label(self) -> str:
        return {
            "cancelled": "Stopped before finishing",
            "failed": "Couldn't finish this reply",
            "paused": "Paused at the turn budget · ask to continue",
        }.get(self.turn_status, "")

    def update_content(self, content: str) -> None:
        self.content = content
        self.query_one(".message-copy", Static).update(self._renderable())

    def set_turn_status(self, status: str) -> None:
        self.turn_status = status if status in {"cancelled", "failed", "paused"} else None
        label = self.query_one("#message-status", Label)
        label.update(self._status_label())
        label.display = self.turn_status is not None
        label.set_class(self.turn_status == "cancelled", "cancelled")
        label.set_class(self.turn_status == "failed", "failed")
        label.set_class(self.turn_status == "paused", "paused")


class Welcome(Vertical):
    def compose(self) -> ComposeResult:
        yield Static(
            " ██████╗ ██████╗ ██╗   ██╗███╗   ██╗\n"
            "██╔═══██╗██╔══██╗╚██╗ ██╔╝████╗  ██║\n"
            "██║   ██║██████╔╝ ╚████╔╝ ██╔██╗ ██║\n"
            "██║   ██║██╔══██╗  ╚██╔╝  ██║╚██╗██║\n"
            "╚██████╔╝██║  ██║   ██║   ██║ ╚████║\n"
            " ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═══╝",
            id="welcome-mark",
        )


class PalettePanel(Vertical):
    def compose(self) -> ComposeResult:
        yield OptionList(id="palette-options", markup=False)

    def on_resize(self) -> None:
        self.app.call_after_refresh(self.app._position_palette)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.app._select_palette_command(event.option.id)

    def _filter(self, query: str) -> None:
        options = self.query_one("#palette-options", OptionList)
        filter_text = query.strip().lstrip("/").casefold()
        filter_text = filter_text.split(maxsplit=1)[0] if filter_text else ""
        matches = [
            (name, description) for name, description in COMMANDS
            if name.casefold().startswith(filter_text)
        ]
        options.set_options([
            Option(Text(f"/{name:<12} {description}"), id=name)
            for name, description in matches
        ])
        if matches:
            options.highlighted = 0
        self.app.call_after_refresh(self.app._position_palette)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        options = self.query_one(OptionList)
        descriptions = dict(COMMANDS)
        for index in range(options.option_count):
            option = options.get_option_at_index(index)
            prompt = Text(f"/{option.id:<12} ")
            prompt.append(descriptions[option.id], style=None if index == event.option_index else "#808080")
            options.replace_option_prompt_at_index(index, prompt)


class ChoiceScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "close", "Close", show=False, priority=True),
        Binding("ctrl+f", "pin_session", "Pin", show=False, priority=True),
        Binding("ctrl+d", "delete_session", "Delete", show=False, priority=True),
        Binding("ctrl+r", "rename_session", "Rename", show=False, priority=True),
    ]

    def __init__(
        self, title: str, choices: list[tuple[str, str, str]],
        current: str, *, allow_custom: bool = False, store: SQLiteSessionStore | None = None,
        descriptions: dict[str, str] | None = None,
        disabled_choices: set[str] | None = None,
    ) -> None:
        super().__init__(classes="sessions-picker" if store else "model-picker")
        self.title = title
        self.choices = choices
        self.current = current
        self.allow_custom = allow_custom
        self.store = store
        self.descriptions = descriptions or {}
        self.disabled_choices = disabled_choices or set()
        self._renaming: str | None = None
        self._deleting: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-card"):
            with Horizontal(id="picker-header"):
                yield Label(self.title, id="picker-title")
                yield Static("esc", id="picker-escape")
            yield Input(placeholder="Search", id="picker-search")
            yield OptionList(id="picker-options", markup=False)
            yield Static("", id="picker-empty")
            with VerticalScroll(id="picker-details"):
                yield Static("", id="picker-description", markup=False)
            yield from self._compose_actions()
            yield Static(self._hint(), id="picker-hint", classes="modal-hint")

    def _compose_actions(self) -> ComposeResult:
        return iter(())

    def _hint(self) -> str:
        return (
            "pin/unpin ctrl+f   delete ctrl+d   rename ctrl+r"
            if self.store else "↑ ↓ navigate   enter select   esc close"
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        if action in {"pin_session", "delete_session", "rename_session"}:
            if action == "delete_session" and self.app.turn_active:
                return False
            return self.store is not None and self._renaming is None
        return True

    def on_mount(self) -> None:
        self._filter("")
        self.query_one("#picker-search", Input).focus()

    def on_resize(self, event) -> None:
        # Keep the search and hints visible in short terminals; only rows scroll.
        options = self.query_one("#picker-options", OptionList)
        options.styles.max_height = self._max_rows(event.size.height)
        self.call_after_refresh(options.scroll_to_highlight)

    def _max_rows(self, height: int) -> int:
        return max(1, min(16, int(height * 0.8) - (11 if self.descriptions else 8)))

    def _filter(self, query: str, selected: str | None = None) -> None:
        query = query.strip()
        matches = [
            choice for choice in self.choices
            if query.casefold() in " ".join(choice).casefold()
        ]
        matched_disabled_choice = any(choice[0] in self.disabled_choices for choice in matches)
        if (self.allow_custom and not matched_disabled_choice
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", query)):
            if query not in {choice[0] for choice in self.choices}:
                matches.append((query, f"Use {query}", "Custom model ID"))
        rows: list[Option] = []
        for group, group_choices in groupby(matches, key=lambda choice: choice[2]):
            if rows:
                rows.append(Option(Text(""), disabled=True))
            rows.append(Option(Text(f"  {group}"), disabled=True))
            for choice_id, label, _ in group_choices:
                marker = "●" if choice_id == self.current else ("○" if self.descriptions else " ")
                if choice_id == self._deleting:
                    label = "Press ctrl+d again to confirm deletion"
                rows.append(Option(
                    self._choice_prompt(marker, label, choice_id), id=choice_id,
                    disabled=choice_id in self.disabled_choices,
                ))
        options = self.query_one("#picker-options", OptionList)
        options.set_options(rows)
        options.display = bool(matches)
        empty = self.query_one("#picker-empty", Static)
        empty.display = not matches
        empty.update("No matches." if self.choices else "No saved sessions yet. Start with /new.")
        description = self.query_one("#picker-description", Static)
        description.display = bool(matches and self.descriptions)
        self.query_one("#picker-details").display = description.display
        highlighted = next((
            i for i, row in enumerate(rows)
            if row.id == (selected or self.current) and not row.disabled
        ), None)
        options.highlighted = highlighted if highlighted is not None else next(
            (i for i, row in enumerate(rows) if row.id and not row.disabled), None,
        )
        self.call_after_refresh(options.scroll_to_highlight)

    def _choice_prompt(self, marker: str, label: str, choice_id: str) -> Text:
        return Text(f"{marker} {label}", no_wrap=True, overflow="ellipsis")

    def on_input_changed(self, event: Input.Changed) -> None:
        if not self._renaming:
            self._deleting = None
            self._filter(event.value, self._selected_session())

    def on_input_submitted(self) -> None:
        if self._renaming:
            try:
                self.store.rename_session(self._renaming, self.query_one(Input).value)
            except ValueError as exc:
                empty = self.query_one("#picker-empty", Static)
                empty.update(str(exc))
                empty.display = True
                return
            self._finish_rename()
            self.app._refresh_header()
            return
        selected = self.query_one("#picker-options", OptionList).highlighted_option
        if selected and selected.id:
            self._choose(selected.id)

    def on_key(self, event) -> None:
        if not self._renaming and event.key in {"up", "down"} and self.query_one("#picker-search", Input).has_focus:
            options = self.query_one("#picker-options", OptionList)
            if event.key == "up":
                options.action_cursor_up()
            else:
                options.action_cursor_down()
            event.prevent_default()
            event.stop()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self._choose(event.option.id)

    def _choose(self, choice_id: str) -> None:
        if choice_id not in self.disabled_choices:
            self.dismiss(choice_id)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.query_one("#picker-description", Static).update(self.descriptions.get(self._selected_session(), ""))
        self.query_one("#picker-details", VerticalScroll).scroll_home(animate=False)
        if self._deleting and event.option.id != self._deleting and event.option.id == self._selected_session():
            self._deleting = None
            self._filter(self.query_one(Input).value, event.option.id)

    def _selected_session(self) -> str | None:
        option = self.query_one(OptionList).highlighted_option
        return option.id if option else None

    def _refresh_sessions(self, selected: str | None = None) -> None:
        self.choices = self.app._session_choices()
        self._filter(self.query_one(Input).value, selected)

    def action_pin_session(self) -> None:
        selected = self._selected_session()
        if selected:
            self.store.toggle_session_pin(selected)
            self._refresh_sessions(selected)

    def action_delete_session(self) -> None:
        selected = self._selected_session()
        if not selected:
            return
        if self._deleting != selected:
            self._deleting = selected
            self._filter(self.query_one(Input).value, selected)
            return
        if self.store.delete_session(selected):
            self.app.file_change_history.pop(selected, None)
            terminal_jobs = self.app.terminal_jobs.pop(selected, None)
            if terminal_jobs:
                terminal_jobs.close()
        self._deleting = None
        if selected == self.current:
            self.dismiss("__new_session__")
        else:
            self._refresh_sessions()

    def action_rename_session(self) -> None:
        selected = self._selected_session()
        if not selected:
            return
        self._deleting = None
        self._renaming = selected
        self.query_one("#picker-title", Label).update("Rename session")
        self.query_one(OptionList).display = False
        self.query_one("#picker-hint", Static).update("enter save   esc cancel")
        search = self.query_one(Input)
        search.value = next(label for key, label, _ in self.choices if key == selected)
        search.focus()
        search.select_all()

    def _finish_rename(self) -> None:
        selected, self._renaming = self._renaming, None
        self.query_one("#picker-title", Label).update(self.title)
        self.query_one("#picker-hint", Static).update(self._hint())
        self.query_one(Input).value = ""
        self._refresh_sessions(selected)

    def action_close(self) -> None:
        if self._renaming:
            self._finish_rename()
        else:
            self.dismiss(None)


class ToolsScreen(ChoiceScreen):
    """Browse tool names; keep the full description out of the list rows."""

    def __init__(self, tools: list[dict[str, Any]]) -> None:
        choices = []
        descriptions = {}
        for tool in tools:
            name = tool.get("name", "tool")
            parts = name.split("__", 2)
            group, label = (parts[1].replace("_", " ").title(), parts[2]) if len(parts) == 3 else ("Built-in", name)
            choices.append((name, label, group))
            descriptions[name] = str(tool.get("description") or "No description provided.")
        super().__init__("Available tools", sorted(choices, key=lambda row: (row[2] != "Built-in", row[2], row[1])), "",
                         descriptions=descriptions)
        self.add_class("tools-picker")

    def _hint(self) -> str:
        return "↑ ↓ browse   tab scroll description   esc close"

    def _max_rows(self, height: int) -> int:
        return max(1, min(14, int(height * 0.8) - 13))

    def on_resize(self, event) -> None:
        self.call_after_refresh(self._filter, self.query_one(Input).value, self._selected_session())

    def _choice_prompt(self, marker: str, label: str, choice_id: str) -> Text:
        width = min(28, max(16, (self.app.size.width - 8) // 3))
        prompt = Text(f"  {label[:width]:<{width}}  ", no_wrap=True, overflow="ellipsis")
        prompt.append(" ".join(self.descriptions.get(choice_id, "").split()),
                      style=None if choice_id == self._selected_session() else "#808080")
        # Textual converts Text to Content and drops no_wrap; bound the actual row text.
        prompt.truncate(max(1, self.query_one(OptionList).scrollable_content_region.width - 1), overflow="ellipsis")
        return prompt

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        super().on_option_list_option_highlighted(event)
        labels = {key: label for key, label, _ in self.choices}
        options = self.query_one(OptionList)
        for index in range(options.option_count):
            choice_id = options.get_option_at_index(index).id
            if choice_id:
                options.replace_option_prompt_at_index(index, self._choice_prompt("", labels[choice_id], choice_id))

    def _choose(self, choice_id: str) -> None:
        pass  # Browsing a tool never runs it.

    def _filter(self, query: str, selected: str | None = None) -> None:
        super()._filter(query, selected)
        if not self.choices:
            self.query_one("#picker-empty", Static).update("No tools are currently available.")


class MCPManagerScreen(ChoiceScreen):
    BINDINGS = [
        Binding("ctrl+e", "toggle_server", "Enable/disable", show=False, priority=True),
        Binding("ctrl+r", "reconnect_server", "Reconnect", show=False, priority=True),
        Binding("ctrl+l", "login_server", "Sign in", show=False, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__("MCP connections", [], "")
        self.add_class("mcp-picker")

    def _compose_actions(self) -> ComposeResult:
        yield Static("", id="mcp-notice", markup=False)
        with Horizontal(id="mcp-actions"):
            yield Button("Enable", id="mcp-toggle")
            yield Button("Reconnect", id="mcp-reconnect")
            yield Button("Sign in", id="mcp-login")

    def _hint(self) -> str:
        return "enter toggle   ctrl+r reconnect   ctrl+l sign in"

    def on_mount(self) -> None:
        self.refresh_servers()
        self.query_one("#picker-search", Input).focus()

    def _max_rows(self, height: int) -> int:
        return max(1, min(12, int(height * 0.8) - 17))

    def refresh_servers(self) -> None:
        selected = self._selected_session()
        self.choices = []
        self.descriptions = {}
        for server in self.app.mcp_servers:
            name = server["name"]
            state = server["state"]
            status = f"{server['tool_count']} tools" if state == "connected" else state.replace("unavailable", "Needs setup").title()
            self.choices.append((name, f"{name.replace('_', ' ').title():<18} {status}",
                                 "Hosted" if server["transport"] == "http" else "Local"))
            self.descriptions[name] = f"{server['message']}\n{server['access']}"
        self._filter(self.query_one(Input).value, selected)
        if not self.choices:
            self.query_one("#picker-empty", Static).update("No MCP servers are configured.")

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        super().on_option_list_option_highlighted(event)
        self._update_actions()

    def _filter(self, query: str, selected: str | None = None) -> None:
        super()._filter(query, selected)
        self._update_actions()

    def _update_actions(self) -> None:
        server = next((s for s in self.app.mcp_servers if s["name"] == self._selected_session()), None)
        blocked = self.app.mcp_busy or self.app.turn_active or not self.app.mcp_ready or server is None
        toggle = self.query_one("#mcp-toggle", Button)
        toggle.label = "Disable" if server and server["enabled"] else "Enable"
        toggle.disabled = blocked
        self.query_one("#mcp-reconnect", Button).disabled = blocked or not server["enabled"]
        login = self.query_one("#mcp-login", Button)
        login.display = bool(server and server["name"] == "notion")
        login.label = "Cancel sign-in" if self.app.mcp_login_active else "Sign in"
        login.disabled = blocked and not self.app.mcp_login_active
        notice = self.app.mcp_notice
        if not self.app.mcp_ready:
            notice = "Servers are still connecting. Local tools remain available."
        elif self.app.turn_active:
            notice = "Finish the current turn before changing connections."
        text = Text(notice)
        if self.app.mcp_login_url:
            text.append("  Open sign-in", style=f"underline link {self.app.mcp_login_url}")
        self.query_one("#mcp-notice", Static).update(text)

    def _choose(self, choice_id: str) -> None:
        self.action_toggle_server()

    def action_toggle_server(self) -> None:
        self.app.manage_mcp("toggle", self._selected_session())

    def action_reconnect_server(self) -> None:
        self.app.manage_mcp("reconnect", self._selected_session())

    def action_login_server(self) -> None:
        if self.app.mcp_login_active:
            self.app._mcp_worker.cancel()
        else:
            self.app.manage_mcp("login", self._selected_session())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        {"mcp-toggle": self.action_toggle_server, "mcp-reconnect": self.action_reconnect_server,
         "mcp-login": self.action_login_server}[event.button.id]()

    def action_close(self) -> None:
        if self.app.mcp_login_active:
            self.app._mcp_worker.cancel()
        super().action_close()


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


class ChatComposer(TextArea):
    BINDINGS = [
        Binding("enter", "send", "Send", priority=True),
        Binding("shift+enter", "newline", "New line", show=False, priority=True),
        Binding("escape", "close_commands", "Close commands", show=False, priority=True),
    ]

    async def action_send(self) -> None:
        await self.app.action_send_prompt()

    def action_newline(self) -> None:
        self.insert("\n")

    def action_paste(self) -> None:
        try:
            image_data = read_clipboard_image()
        except ValueError as exc:
            self.app._set_activity(str(exc), working=False, error=True)
            return
        if image_data is None:
            super().action_paste()
            return
        self.app._attach_clipboard_image(image_data)

    def action_close_commands(self) -> None:
        self.app.action_dismiss_palette()

    def on_key(self, event) -> None:
        if event.key == "backspace" and not self.text and self.app.pending_images:
            self.app._remove_last_image()
            event.prevent_default()
            event.stop()
            return
        palette = self.app.query_one("#palette-overlay", PalettePanel)
        if palette.display and event.key in {"up", "down"}:
            options = palette.query_one(OptionList)
            if event.key == "up":
                options.action_cursor_up()
            else:
                options.action_cursor_down()
            event.prevent_default()
            event.stop()


class OrynTUI(App[None]):
    """A Textual view over Oryn's existing Python harness."""

    CSS_PATH = "tui.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+p", "open_palette", "Commands"),
        Binding("ctrl+n", "new_session", "New chat"),
        Binding("ctrl+o", "open_sessions", "Sessions"),
        Binding("f2", "choose_model", "Model"),
        Binding("f3", "choose_effort", "Effort"),
        Binding("f4", "choose_speed", "Speed"),
        Binding("escape", "dismiss_palette", "Close", show=False),
        Binding("ctrl+c", "stop_or_quit", "Stop/Quit", priority=True),
    ]

    def __init__(
        self,
        *,
        store: SQLiteSessionStore,
        session_id: str,
        project_root: Path,
        model: str,
        history: list[dict[str, Any]],
        provider: Any,
        mcp_client: MCPClient,
        initial_tools: list[dict[str, Any]],
        undo_history: list[FileChange] | None = None,
        turn_limits: TurnLimits | None = None,
    ) -> None:
        super().__init__()
        self.store = store
        self.session_id = session_id
        self.project_root = project_root
        self.model = model
        self._codex_auth_file = getattr(provider, "auth_file", AUTH_FILE)
        self.model_labels = {model: label for model, label, _ in available_models(self._codex_auth_file)}
        self.model_labels.setdefault(model, model)
        self.history = history
        self.saved_count = len(history)
        self.provider = provider
        self._draft_model_settings: dict[str, dict[str, str]] = {}
        self._restore_model_settings()
        self.mcp_client = mcp_client
        self.tools = initial_tools
        self.turn_limits = turn_limits or TurnLimits()
        self.file_change_history: dict[str, list[FileChange]] = {
            session_id: undo_history if undo_history is not None else [],
        }
        self.terminal_jobs: dict[str, TerminalJobManager] = (
            {session_id: TerminalJobManager(project_root)} if session_id else {}
        )
        self.mcp_statuses: list[str] = []
        self.mcp_ready = False
        self.mcp_servers = [
            {"name": c.name, "enabled": c.enabled, "state": "starting" if c.enabled else "disabled",
             "message": "Connecting." if c.enabled else "Disabled in Oryn settings.",
             "tool_count": 0, "access": c.access, "transport": "http" if c.url else "stdio"}
            for c in mcp_client.configs
        ]
        self.mcp_busy = False
        self.mcp_login_active = False
        self.mcp_login_url: str | None = None
        self.mcp_notice = "Select a server to manage its connection."
        self.turn_active = False
        self._cancel_event: threading.Event | None = None
        self._current_reply: MessageCard | None = None
        self._reply_text = ""
        self._partial_reply_text = ""
        self._turn_start = 0
        self._turn_started_at: float | None = None
        self._pending_navigation: str | None = None
        self._pending_approval: ApprovalRequest | None = None
        self._pending_computer_question: ComputerQuestion | None = None
        self._computer_armed = False
        self._palette_draft: str | None = None
        self.pending_images: list[dict[str, Any]] = []

    @property
    def undo_history(self) -> list[FileChange]:
        return self.file_change_history.setdefault(self.session_id, [])

    def compose(self) -> ComposeResult:
        with Vertical(id="main-panel"):
            with Horizontal(id="topbar"):
                yield Static("Oryn", id="topbar-title")
                yield Static(self.project_root.name or "Project", id="topbar-project")
            with TranscriptScroll(id="transcript"):
                visible = [
                    message for message in self.history
                    if message.get("role") in {"user", "assistant"}
                ]
                if visible:
                    for message in visible:
                        yield MessageCard(
                            message["role"], message.get("content", ""), message.get("turn_status"),
                            elapsed_seconds=message.get("elapsed_seconds"), images=message.get("images"),
                        )
                else:
                    yield Welcome(id="welcome")
            with Horizontal(id="activity-row"):
                yield Static("●", id="activity-dot")
                yield Static("Ready", id="activity-label", markup=False)
                yield Static(f"{len(self.tools)} tools", id="tool-count")
            with Horizontal(id="composer-row"):
                with Vertical(id="composer-wrap"):
                    yield PalettePanel(id="palette-overlay")
                    with Vertical(id="composer-frame"):
                        yield Static("Computer · next message controls your desktop · Esc to cancel", id="computer-mode")
                        yield ChatComposer(
                            id="composer",
                            placeholder="Ask anything…",
                            tab_behavior="indent",
                            highlight_cursor_line=False,
                        )
                        yield Static(id="image-attachments", classes="image-attachments")
                        with Horizontal(id="composer-meta"):
                            yield Static("Oryn", id="agent-chip")
                            yield Static("·", id="model-separator")
                            yield Button(self.model_labels.get(self.model, self.model), id="model-chip")
                            yield Button(self._effort_label(), id="effort-chip")
                            yield Button(self._speed_label(), id="speed-chip")
                            yield Static(getattr(self.provider, "label", "ChatGPT"), id="provider-chip")
                    with Horizontal(id="composer-footer"):
                        yield Static("enter send   shift+enter new line", id="send-hint")
                        yield Static("/ commands", id="composer-commands")
            yield Static(id="home-spacer")
            with Horizontal(id="app-footer"):
                yield Static(self._project_label(), id="workspace-path")
                yield Static("ctrl+p commands   ctrl+o sessions   f2 models", id="shortcut-hints")

    def on_mount(self) -> None:
        self._sync_home()
        self._refresh_header()
        if warning := provider_setup_warning(self.provider):
            self._set_activity(warning, working=False, error=True)
        self.query_one("#composer", TextArea).focus()
        self._scroll_to_bottom(force=True)
        # MCP startup can be slow; keep it outside Textual's executor and event loop.
        self._mcp_thread = threading.Thread(
            target=self._connect_mcp, name="oryn-mcp-connect", daemon=True,
        )
        self._mcp_thread.start()

    def on_resize(self, event) -> None:
        compact = event.size.width < 94
        self.query_one("#transcript").styles.padding = (1, 1 if compact else 2)
        margin = 1 if compact else 2
        self.query_one("#composer-wrap").styles.margin = (0, margin, 1, margin)
        self.query_one("#shortcut-hints", Static).update(
            "ctrl+p commands" if compact else "ctrl+p commands   ctrl+o sessions   f2 models"
        )
        self.query_one("#send-hint", Static).update(
            "enter send · ctrl+v image" if compact
            else "enter send   shift+enter new line   ctrl+v image"
        )
        self.query_one("#provider-chip").display = not compact

    def on_unmount(self) -> None:
        self._cancel_active_turn()
        if self._pending_approval:
            self._pending_approval.resolve(False)
            self._pending_approval = None
        self._cancel_computer_question(close_screen=False)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "composer":
            return
        value = event.text_area.text
        if value.startswith("/"):
            self._show_palette(value[1:])
        else:
            self.query_one("#palette-overlay", PalettePanel).display = False

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
        self._turn_started_at = None
        timing = {"elapsed_seconds": event.elapsed_seconds} if event.elapsed_seconds is not None else {}
        if self._pending_approval:
            self._pending_approval.resolve(False)
            self._pending_approval = None
            if isinstance(self.screen, ApprovalScreen):
                self.pop_screen()
        self._cancel_computer_question()
        self._refresh_mcp_view()
        if event.error:
            self._set_activity(
                event.error if event.paused else
                f"Turn interrupted  ·  {event.error}" if event.cancelled else f"Turn failed  ·  {event.error}",
                working=False,
                error=not event.paused,
            )
            status = "paused" if event.paused else "cancelled" if event.cancelled else "failed"
            for message in self.history[self._turn_start:]:
                if message.get("role") == "assistant" and (message.get("content") or message.get("tool_calls")):
                    message["turn_status"] = status
            if self._partial_reply_text:
                self.history.append({
                    "role": "assistant",
                    "content": self._partial_reply_text,
                    "turn_status": status,
                    **timing,
                })
            elif timing:
                for message in reversed(self.history[self._turn_start:]):
                    if message.get("role") == "assistant":
                        message.update(timing)
                        break
            if self._current_reply:
                self._current_reply.set_turn_status(status)
                self._current_reply.update_content(self._reply_text)
        else:
            self.history.append({"role": "assistant", "content": event.answer or "", **timing})
            self._set_activity("Ready", working=False)
        if self._current_reply:
            self._current_reply.set_elapsed_seconds(event.elapsed_seconds)
        self._current_reply = None
        self._reply_text = ""
        self._partial_reply_text = ""
        try:
            self.store.append_messages(self.history[self.saved_count:], self.session_id)
            self.saved_count = len(self.history)
        except Exception as exc:
            self._pending_navigation = None
            self._set_activity(f"Could not save this session: {exc}", working=False, error=True)
            return
        target, self._pending_navigation = self._pending_navigation, None
        if target:
            navigation = self._new_session() if target == "__new_session__" else self._load_session(target)
            self.run_worker(navigation, group="navigation", exclusive=True)
            return
        if len(self.screen_stack) == 1:
            self.query_one("#composer", TextArea).focus()

    def on_turn_progress(self, event: TurnProgress) -> None:
        if self.turn_active:
            self._set_activity(event.text, working=True)

    @on(MCPReady)
    def on_mcp_ready(self, event: MCPReady) -> None:
        self.mcp_ready = True
        self.mcp_statuses = event.statuses
        self.mcp_servers = event.servers
        self.tools = tool_schemas(event.schemas)
        self.query_one("#tool-count", Static).update(f"{len(self.tools)} tools")
        self._refresh_mcp_view()
        if not self.turn_active:
            warning = provider_setup_warning(self.provider)
            self._set_activity(
                warning or ("Ready · MCP connected" if event.schemas else "Ready"),
                working=False, error=bool(warning),
            )

    def _refresh_mcp_view(self) -> None:
        if isinstance(self.screen, MCPManagerScreen):
            self.screen.refresh_servers()

    def manage_mcp(self, action: str, name: str | None) -> None:
        if action not in {"toggle", "reconnect", "login"}:
            return
        server = next((s for s in self.mcp_servers if s["name"] == name), None)
        if self.mcp_busy or self.turn_active or not self.mcp_ready or server is None:
            return
        if action == "login" and name != "notion":
            return
        if action == "reconnect" and not server["enabled"]:
            return
        self.mcp_busy = True
        self.mcp_notice = f"{name}: {'Opening browser sign-in' if action == 'login' else 'Updating connection'}…"
        self._refresh_mcp_view()
        self._mcp_worker = self.run_worker(self._manage_mcp(action, name), group="mcp-management")

    async def _manage_mcp(self, action: str, name: str) -> None:
        previous = {s["name"] for s in self.mcp_servers if s["enabled"]}
        try:
            if action == "login":
                self.mcp_login_active = True

                def on_authorize(url: str) -> None:
                    self.mcp_login_url = url
                    self.mcp_notice = "Finish Notion sign-in in your browser. Esc cancels."
                    self._refresh_mcp_view()

                await asyncio.wait_for(login_notion(on_authorize=on_authorize), timeout=330)
                self.mcp_login_active = False
                self.mcp_login_url = None
                self.mcp_notice = "Notion signed in. Connecting…"
                self._refresh_mcp_view()
            if action == "toggle" or (action == "login" and name not in previous):
                enabled = previous ^ {name} if action == "toggle" else previous | {name}
                await asyncio.to_thread(self.mcp_client.set_enabled, enabled)
                try:
                    await asyncio.to_thread(save_enabled_servers, enabled)
                except OSError:
                    await asyncio.to_thread(self.mcp_client.set_enabled, previous)
                    raise
            else:
                await asyncio.to_thread(self.mcp_client.reconnect, name)
            state = next(s for s in self.mcp_client.status_snapshot() if s["name"] == name)
            self.mcp_notice = f"{name}: {state['message']}"
        except asyncio.CancelledError:
            self.mcp_notice = "Notion sign-in cancelled."
        except Exception as exc:
            self.mcp_notice = f"{name}: update failed ({type(exc).__name__}). Retry or check mcp.env."
        finally:
            self.mcp_login_active = False
            self.mcp_login_url = None
            self.mcp_busy = False
            self.mcp_servers = self.mcp_client.status_snapshot()
            self.tools = tool_schemas(self.mcp_client.tool_schemas())
            self.query_one("#tool-count", Static).update(f"{len(self.tools)} tools")
            self._refresh_mcp_view()

    def on_approval_request(self, request: ApprovalRequest) -> None:
        if self._pending_approval:
            self._pending_approval.resolve(False)
        self._pending_approval = request
        self.push_screen(
            ApprovalScreen(request.title, request.preview),
            lambda approved: self._resolve_approval(request, bool(approved)),
        )

    def on_computer_question(self, request: ComputerQuestion) -> None:
        self._cancel_computer_question()
        self._pending_computer_question = request
        self.push_screen(
            TextPromptScreen(f"Computer model asks: {request.question[:160]}", "", "Type your answer"),
            lambda answer: self._resolve_computer_question(request, answer),
        )

    async def action_send_prompt(self) -> None:
        composer = self.query_one("#composer", TextArea)
        prompt = composer.text.strip()
        if not prompt and not self.pending_images:
            return
        computer_task = None
        if prompt.startswith("/"):
            parts = prompt[1:].strip().split(maxsplit=1)
            if not parts:
                parts = [""]
            name = COMMAND_ALIASES.get(parts[0].casefold(), parts[0].casefold())
            if name not in dict(COMMANDS):
                selected = self.query_one("#palette-options", OptionList).highlighted_option
                if not selected or not selected.id:
                    return
                name = selected.id
            if name == "computer" and len(parts) > 1:
                computer_task = parts[1]
            else:
                self._select_palette_command(name, parts[1] if len(parts) > 1 else None)
                return
        elif self._computer_armed:
            computer_task = prompt

        if computer_task and (
            computer_task == "--dry-run"
            or (computer_task.startswith("--dry-run ")
                and not computer_task.removeprefix("--dry-run ").strip())
        ):
            self._set_activity("Add a computer task after --dry-run.", working=False, error=True)
            return

        if (computer_task or self._computer_armed) and self.pending_images:
            self._set_activity("/computer captures the desktop itself. Remove attached images first.", working=False, error=True)
            return

        if self.mcp_busy:
            self._set_activity("MCP connections are updating. Your draft is safe; send it when they finish.", working=True)
            return
        if self.turn_active:
            self._set_activity("Oryn is still working. Your draft is safe; send it when this turn finishes.", working=True)
            return

        user_message = {"role": "user", "content": prompt}
        images = list(self.pending_images)
        if images:
            user_message["images"] = images
        if not self.session_id:
            new_id = ""
            try:
                new_id = self.store.create_session(self.project_root)
                self.store.set_session_model(new_id, self.model)
                self._draft_model_settings[self.model] = self._model_settings()
                for model, settings in self._draft_model_settings.items():
                    self.store.set_session_model_settings(new_id, model, settings)
                self.store.append_messages([user_message], new_id)
            except (ValueError, OSError, sqlite3.Error) as exc:
                if new_id:
                    self.store.delete_session(new_id)
                self._set_activity(f"Could not save this session: {exc}", working=False, error=True)
                return
            self.session_id = new_id
            self._draft_model_settings.clear()
            self.saved_count += 1

        self._turn_started_at = monotonic()
        welcome = self.query("#welcome")
        if welcome:
            await welcome.remove()
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(MessageCard("user", prompt, images=images))
        self._current_reply = MessageCard("assistant")
        await transcript.mount(self._current_reply)
        self._scroll_to_bottom(force=True)
        composer.clear()
        if computer_task:
            self._set_computer_armed(False)
        self.history.append(user_message)
        self.pending_images.clear()
        self._refresh_attachments()
        self._sync_home()
        self._refresh_header()
        self._turn_start = len(self.history) - 1
        self.turn_active = True
        self._reply_text = ""
        self._partial_reply_text = ""
        self._set_activity(f"{self.model} is preparing computer tools" if computer_task else "Oryn is thinking", working=True)
        self._cancel_event = threading.Event()
        # Provider and tool calls block, but must not hold the TUI open at shutdown.
        self._turn_thread = threading.Thread(
            target=self._run_turn,
            args=(self.history, self.provider, list(self.tools), self.project_root, self._cancel_event, computer_task),
            name="oryn-computer-turn" if computer_task else "oryn-agent-turn", daemon=True,
        )
        self._turn_thread.start()

    def action_open_palette(self) -> None:
        if len(self.screen_stack) != 1:
            return
        composer = self.query_one("#composer", TextArea)
        if not composer.text.startswith("/"):
            self._palette_draft = composer.text
            composer.load_text("/")
        self._show_palette(composer.text.lstrip("/"))

    def action_dismiss_palette(self) -> None:
        if self.query_one("#palette-overlay", PalettePanel).display:
            self._hide_palette()
        elif self._computer_armed:
            self._set_computer_armed(False)

    async def action_new_session(self) -> None:
        if len(self.screen_stack) == 1:
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
        self._cancel_computer_question()

    def action_open_sessions(self) -> None:
        if len(self.screen_stack) == 1:
            self.run_worker(self._show_sessions(), group="commands", exclusive=True)

    def action_choose_model(self) -> None:
        if len(self.screen_stack) == 1:
            self.run_worker(self._change_model(), group="commands", exclusive=True)

    def action_choose_effort(self) -> None:
        if len(self.screen_stack) == 1:
            self.run_worker(self._change_setting("reasoning_effort"), group="commands", exclusive=True)

    def action_choose_speed(self) -> None:
        if len(self.screen_stack) == 1:
            self.run_worker(self._change_setting("service_tier"), group="commands", exclusive=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "model-chip":
            self.action_choose_model()
        elif event.button.id == "effort-chip":
            self.action_choose_effort()
        elif event.button.id == "speed-chip":
            self.action_choose_speed()

    def _show_palette(self, query: str = "") -> None:
        palette = self.query_one("#palette-overlay", PalettePanel)
        palette.display = True
        palette._filter(query)
        self.query_one("#composer", TextArea).focus()
        self.call_after_refresh(self._position_palette)

    def _position_palette(self) -> None:
        palette = self.query_one("#palette-overlay", PalettePanel)
        if not palette.display:
            return
        available = max(1, self.query_one("#composer-frame").region.y)
        palette.query_one(OptionList).styles.max_height = min(10, available)
        palette.styles.offset = (0, -min(palette.region.height, available))

    def _hide_palette(self) -> None:
        self.query_one("#palette-overlay", PalettePanel).display = False
        composer = self.query_one("#composer", TextArea)
        if self._palette_draft is not None:
            draft, self._palette_draft = self._palette_draft, None
            composer.load_text(draft)
        composer.focus()

    def _set_computer_armed(self, armed: bool) -> None:
        self._computer_armed = armed
        self.query_one("#computer-mode", Static).display = armed
        if armed:
            self.query_one("#composer", TextArea).focus()

    def _select_palette_command(self, name: str, argument: str | None = None) -> None:
        if self.turn_active and COMMAND_ALIASES.get(name, name) not in COMMANDS_DURING_TURN:
            self._set_activity("Finish the current turn before using this command.", working=True)
            return
        if self._palette_draft is None:
            self.query_one("#composer", TextArea).clear()
        self._hide_palette()
        self.run_worker(self._execute_command(name, argument), group="commands", exclusive=True)

    async def _execute_command(self, name: str, argument: str | None = None) -> None:
        name = COMMAND_ALIASES.get(name, name)
        if self.turn_active and name not in COMMANDS_DURING_TURN:
            self._set_activity("Finish the current turn before using this command.", working=True)
            return
        if name in {"new", "clear"}:
            await self._new_session()
        elif name == "delete":
            await self._delete_current_session()
        elif name in {"sessions", "resume", "continue"}:
            await self._show_sessions()
        elif name in {"model", "models"}:
            if argument:
                self._save_model(argument)
            else:
                await self._change_model()
        elif name in {"effort", "speed"}:
            await self._change_setting("reasoning_effort" if name == "effort" else "service_tier", argument)
        elif name == "project":
            if argument:
                await self._switch_project(argument)
            else:
                await self._choose_project()
        elif name == "tools":
            self.push_screen(ToolsScreen(self.tools), lambda _: self.query_one("#composer", TextArea).focus())
        elif name == "mcps":
            self.push_screen(MCPManagerScreen(), lambda _: self.query_one("#composer", TextArea).focus())
        elif name == "computer":
            if argument:
                self.query_one("#composer", TextArea).load_text(f"/computer {argument}")
                await self.action_send_prompt()
            else:
                if self.pending_images:
                    self._set_activity("Remove attached images before using /computer.", working=False, error=True)
                else:
                    self._set_computer_armed(True)
        elif name == "help":
            body = (
                "COMMANDS\n"
                "/new       Start a new session\n"
                "/delete    Delete this session and return home\n"
                "/sessions  Resume a saved session\n"
                "/models    Change this session's model\n"
                "/effort    Set reasoning effort for the selected model\n"
                "/speed     Set Standard or Fast speed when supported\n"
                "/project   Switch project folder\n"
                "/tools     List available tools\n"
                "/mcps      Enable, disable, reconnect, or sign in\n"
                "/computer  Use the selected model to control the desktop for your next message; Esc cancels\n"
                "/help      Show this guide\n"
                "/exit      Close Oryn\n\n"
                "KEYS\n"
                "Ctrl+P    Open command palette\n"
                "Ctrl+N    New session\n"
                "Ctrl+O    Session picker\n"
                "F2        Change model\n"
                "F3        Change reasoning effort\n"
                "F4        Change model speed\n"
                "Enter     Send message\n"
                "Ctrl+V    Paste a PNG/JPEG image from the clipboard\n"
                "Backspace Remove the last pasted image when the draft is empty\n"
                "Shift+Enter Add a new line\n"
                "Ctrl+C    Stop turn, or quit when idle\n"
                "Esc       Close a dialog or deny approval"
            )
            self.push_screen(InfoScreen("ORYN QUICK GUIDE", body))
        elif name in {"quit", "exit", "q"}:
            self.exit()
        elif name:
            self._set_activity(f"Unknown command: /{name}. Type /help for commands.", working=False, error=True)

    async def _new_session(self) -> None:
        if self._queue_navigation("__new_session__"):
            return
        self._set_computer_armed(False)
        self.session_id = ""
        self._draft_model_settings = {self.model: self._model_settings()}
        self.file_change_history[""] = []
        self.pending_images.clear()
        self._refresh_attachments()
        self.history = _base_history(self.project_root)
        self.saved_count = len(self.history)
        await self._refresh_transcript()
        self._set_activity("New session ready", working=False)

    async def _delete_current_session(self) -> None:
        session_id = self.session_id
        if not session_id:
            self._set_activity("No saved session to delete.", working=False)
            return
        confirmed = await self.push_screen_wait(ApprovalScreen(
            "Delete this session?",
            "This permanently deletes the chat, its saved context, diagnostics, and undo journal, "
            "and stops terminal jobs belonging to this chat. Project files are kept as they are.",
        ))
        if not confirmed:
            self._set_activity("Session deletion cancelled", working=False)
            return
        if not self.store.delete_session(session_id):
            self._set_activity("This saved session no longer exists.", working=False, error=True)
            return
        self.file_change_history.pop(session_id, None)
        terminal_jobs = self.terminal_jobs.pop(session_id, None)
        if terminal_jobs:
            terminal_jobs.close()
        await self._new_session()
        self._set_activity("Session deleted · ready for a new chat", working=False)

    async def _show_sessions(self) -> None:
        self._hide_palette()
        result = await self.push_screen_wait(ChoiceScreen(
            "Sessions", self._session_choices(), self.session_id, store=self.store,
        ))
        if result == "__new_session__":
            await self._new_session()
        elif result:
            await self._load_session(result)
        self.query_one("#composer", TextArea).focus()

    def _session_choices(self) -> list[tuple[str, str, str]]:
        choices: list[tuple[str, str, str]] = []
        for entry in self.store.session_entries():
            session_id = entry["id"]
            title = entry["title"]
            if not title:
                messages = self.store.load_messages(session_id)
                if not messages:
                    continue
                title = next((
                    " ".join(str(message.get("content", "")).split())[:64]
                    for message in messages if message.get("role") == "user"
                ), "New session")
            group = "Earlier"
            if entry["updated_at"]:
                try:
                    date = datetime.fromisoformat(entry["updated_at"]).replace(tzinfo=timezone.utc).astimezone().date()
                    group = "Today" if date == datetime.now().date() else date.strftime("%a %b %d %Y")
                except ValueError:
                    pass
            if entry["pinned"]:
                group = "Pinned"
            choices.append((session_id, title, group))
        return choices

    async def _load_session(self, session_id: str) -> None:
        if self.turn_active and session_id == self.session_id:
            return
        if self._queue_navigation(session_id):
            return
        self._set_computer_armed(False)
        saved_root = self.store.session_project_root(session_id)
        project_root = _resolve_project_root(Path(saved_root) if saved_root else APP_ROOT)
        self.project_root = project_root
        self.session_id = session_id
        if session_id not in self.file_change_history:
            self.file_change_history[session_id] = self.store.load_file_change_history(session_id, project_root)
        if session_id not in self.terminal_jobs:
            self.terminal_jobs[session_id] = TerminalJobManager(project_root)
        self.pending_images.clear()
        self._refresh_attachments()
        self.model = self.store.session_model(session_id) or DEFAULT_MODEL
        self.provider = provider_for_model(self.model, self._codex_auth_file)
        self._restore_model_settings()
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
                    elapsed_seconds=message.get("elapsed_seconds"), images=message.get("images"),
                ))
        else:
            await transcript.mount(Welcome(id="welcome"))
        self._sync_home()
        self._refresh_header()
        self._scroll_to_bottom(force=True)

    def _refresh_attachments(self) -> None:
        widget = self.query_one("#image-attachments", Static)
        if not self.pending_images:
            widget.display = False
            widget.update("")
            return
        label = Text("▧ ", style="#fab283")
        for index, image in enumerate(self.pending_images):
            if index:
                label.append("  ·  ", style="#808080")
            label.append(f"{image['name']} ({image['width']}×{image['height']})")
        label.append("   Backspace removes last", style="#808080")
        widget.update(label)
        widget.display = True

    def _attach_clipboard_image(self, data: bytes) -> None:
        if len(self.pending_images) >= MAX_IMAGES:
            self._set_activity(f"A message can contain at most {MAX_IMAGES} images", working=False, error=True)
            return
        if self.provider.supports_image_input() is not True:
            self._set_activity(
                f"The model catalog does not confirm image input for {self.model}; refresh the model catalog or select an image-capable model.",
                working=False, error=True,
            )
            return
        try:
            image = prepare_image(data, f"Pasted image {len(self.pending_images) + 1}")
        except ValueError as exc:
            self._set_activity(str(exc), working=False, error=True)
            return
        self.pending_images.append(image)
        self._refresh_attachments()
        self._set_activity(f"Attached {image['name']} · {image['width']}×{image['height']}", working=False)

    def _remove_last_image(self) -> None:
        if not self.pending_images:
            return
        image = self.pending_images.pop()
        self._refresh_attachments()
        self._set_activity(f"Removed {image['name']}", working=False)

    async def _change_model(self) -> None:
        self._hide_palette()
        catalog = {model: (label, provider) for model, label, provider in available_models(self._codex_auth_file)}
        for session_id in self.store.list_sessions():
            saved_model = self.store.session_model(session_id)
            if saved_model:
                catalog.setdefault(saved_model, (saved_model, "Gemini" if saved_model.startswith("gemini-") else "ChatGPT"))
        catalog.setdefault(self.model, (self.model_labels.get(self.model, self.model),
                                        getattr(self.provider, "label", "ChatGPT")))
        choices = [(model, label, provider) for model, (label, provider) in catalog.items()]
        result = await self.push_screen_wait(ChoiceScreen(
            "Select model", choices, self.model, allow_custom=True,
        ))
        if result:
            self._save_model(result)
        self.query_one("#composer", TextArea).focus()

    def _save_model(self, model: str) -> None:
        model = model.strip()
        try:
            if self.session_id:
                self.store.set_session_model(self.session_id, model)
            elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", model):
                raise ValueError("Model ID must be 1 to 120 letters, digits, dots, underscores, or hyphens.")
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._set_activity(str(exc), working=False, error=True)
            return
        if not self.session_id:
            self._draft_model_settings[self.model] = self._model_settings()
        self.model = model
        self.provider = provider_for_model(self.model, self._codex_auth_file)
        self.model_labels.setdefault(self.model, self.model)
        self._restore_model_settings()
        self._refresh_header()
        self._set_activity(
            f"Model set to {self.model}" + (" for the next reply" if self.turn_active else ""),
            working=self.turn_active,
        )

    def _model_settings(self) -> dict[str, str]:
        return {"reasoning_effort": self.provider.reasoning_effort, "service_tier": self.provider.service_tier}

    def _restore_model_settings(self) -> None:
        saved = (self.store.session_model_settings(self.session_id, self.model) if self.session_id
                 else self._draft_model_settings.get(self.model, {}))
        options = self.provider.model_options()
        self.provider.configure(**{
            key: saved.get(key, "default") if saved.get(key, "default") in dict(choices) else "default"
            for key, choices in options.items()
        })

    def _effort_label(self) -> str:
        return f"Effort: {self.provider.reasoning_effort}"

    def _speed_label(self) -> str:
        return "Standard" if self.provider.service_tier == "default" else (
            "Fast" if self.provider.service_tier in {"priority", "fast"} else self.provider.service_tier.title()
        )

    async def _change_setting(self, field: str, argument: str | None = None) -> None:
        self._hide_palette()
        title = "Reasoning effort" if field == "reasoning_effort" else "Model speed"
        options = self.provider.model_options()[field]
        choices = []
        descriptions = {}
        for key, text in options:
            label, _, description = text.partition(" — ")
            if field == "reasoning_effort":
                label = {"xhigh": "Extra high", "max": "Maximum"}.get(key, label)
            choices.append((key, label, self.model))
            descriptions[key] = description or (
                "Use the model's recommended reasoning level." if field == "reasoning_effort"
                else "Regular processing without Fast mode."
            )
        value = argument.strip().lower() if argument else await self.push_screen_wait(ChoiceScreen(
            title, choices, getattr(self.provider, field), descriptions=descriptions,
        ))
        if value:
            previous = self._model_settings()
            settings = {**previous, field: value}
            try:
                next_provider = provider_for_model(self.model, self._codex_auth_file)
                next_provider.configure(**settings)
                if self.session_id:
                    self.store.set_session_model_settings(self.session_id, self.model, settings)
                else:
                    self._draft_model_settings[self.model] = settings
            except (ValueError, OSError, sqlite3.Error) as exc:
                self._set_activity(str(exc), working=False, error=True)
            else:
                self.provider = next_provider
                self._refresh_header()
                self._set_activity(
                    f"{self._effort_label()} · {self._speed_label()}"
                    + (" for the next reply" if self.turn_active else ""),
                    working=self.turn_active,
                )
        self.query_one("#composer", TextArea).focus()

    async def _choose_project(self) -> None:
        result = await self.push_screen_wait(TextPromptScreen(
            "OPEN PROJECT FOLDER",
            str(self.project_root),
            "/path/to/project",
        ))
        if result:
            await self._switch_project(result)

    async def _switch_project(self, path: str) -> None:
        try:
            project_root = _resolve_project_root(Path(path))
        except (ValueError, OSError) as exc:
            self._set_activity(str(exc), working=False, error=True)
            return
        self.project_root = project_root
        await self._new_session()
        self._set_activity(f"Project opened · {project_root}", working=False)

    def _refresh_header(self) -> None:
        title = (self.store.session_title(self.session_id) if self.session_id else None) or next((
            " ".join(str(message.get("content", "")).split())[:64]
            for message in self.history if message.get("role") == "user"
        ), "New session")
        self.query_one("#topbar-title", Static).update(title)
        self.query_one("#topbar-project", Static).update(self.project_root.name or "Project")
        self.query_one("#model-chip", Button).label = self.model_labels.get(self.model, self.model)
        self.query_one("#provider-chip", Static).update(getattr(self.provider, "label", "ChatGPT"))
        self.query_one("#effort-chip", Button).label = self._effort_label()
        self.query_one("#speed-chip", Button).label = self._speed_label()
        self.query_one("#workspace-path", Static).update(self._project_label())

    def _project_label(self) -> str:
        try:
            return "~/" + str(self.project_root.relative_to(Path.home()))
        except ValueError:
            return str(self.project_root)

    def _sync_home(self) -> None:
        empty = not any(message.get("role") in {"user", "assistant"} for message in self.history)
        self.query_one("#main-panel").set_class(empty, "home")

    def _set_activity(self, text: str, *, working: bool, error: bool = False) -> None:
        dot = self.query_one("#activity-dot", Static)
        label = self.query_one("#activity-label", Static)
        dot.update("◌" if working else "●")
        dot.set_class(working, "working")
        label.update(text)
        label.set_class(error, "error")
        self.query_one("#activity-row").set_class(
            not working and not error and text.startswith("Ready"), "idle",
        )

    def _scroll_to_bottom(self, *, force: bool = False) -> None:
        transcript = self.query_one("#transcript", TranscriptScroll)
        if force:
            transcript.follow_output = True
        if not transcript.follow_output:
            return

        def follow() -> None:
            if transcript.follow_output:
                transcript.scroll_end(animate=False, immediate=True)

        self.call_after_refresh(follow)

    def _resolve_approval(self, request: ApprovalRequest, approved: bool) -> None:
        request.resolve(approved)
        if self._pending_approval is request:
            self._pending_approval = None

    def _resolve_computer_question(self, request: ComputerQuestion, answer: str | None) -> None:
        request.resolve(answer.strip() if isinstance(answer, str) and answer.strip() else None)
        if self._pending_computer_question is request:
            self._pending_computer_question = None

    def _cancel_computer_question(self, *, close_screen: bool = True) -> None:
        request = self._pending_computer_question
        if request is None:
            return
        self._pending_computer_question = None
        request.resolve(None)
        if close_screen and isinstance(self.screen, TextPromptScreen):
            self.pop_screen()

    def _request_approval(self, title: str, preview: str) -> bool:
        request = ApprovalRequest(title, preview[:12_000])
        if not self.post_message(request):
            return False
        while not request.event.wait(0.1):
            if self._cancel_event and self._cancel_event.is_set():
                request.resolve(False)
                break
        return request.approved

    def _ask_computer_question(self, question: str, cancel_event: threading.Event) -> str | None:
        request = ComputerQuestion(question)
        if not self.post_message(request):
            return None
        while not request.event.wait(0.1):
            if cancel_event.is_set():
                request.resolve(None)
                break
        return request.answer

    def _cancel_active_turn(self) -> None:
        if hasattr(self, "_cancel_event") and self._cancel_event:
            self._cancel_event.set()

    def _queue_navigation(self, target: str) -> bool:
        if not self.turn_active:
            return False
        self._pending_navigation = target
        self._cancel_active_turn()
        self._set_activity("Stopping current reply before switching chats…", working=True)
        if self._pending_approval:
            request = self._pending_approval
            self._pending_approval = None
            request.resolve(False)
            if isinstance(self.screen, ApprovalScreen):
                self.pop_screen()
        self._cancel_computer_question()
        return True

    def _run_turn(
        self,
        history: list[dict[str, Any]],
        provider: Any,
        tools: list[dict[str, Any]],
        project_root: Path,
        cancel_event: threading.Event,
        computer_task: str | None = None,
    ) -> None:
        started_at = self._turn_started_at if self._turn_started_at is not None else monotonic()
        session_id = self.session_id
        undo_history = self.undo_history
        terminal_jobs = self.terminal_jobs.get(session_id)
        if terminal_jobs is None:
            terminal_jobs = self.terminal_jobs[session_id] = TerminalJobManager(project_root)
        file_change_journal = lambda phase, change: self.store.record_file_change(
            session_id, project_root, phase, change,
        )
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
        trace = None
        computer_session = None
        finished = None
        def tool_event(phase: str, call: ToolCall, result: str | None) -> None:
            self.post_message(ToolActivity(phase, call, result))
            if trace and call.name.startswith("computer_"):
                trace.write("computer_tool", phase=phase, name=call.name,
                            arguments=call.arguments if phase == "start" else None,
                            result=result[:1000] if isinstance(result, str) else None)
        def diagnostic(turn_id: str, event: dict[str, Any]) -> None:
            self.store.append_diagnostic(session_id, turn_id, event)
            if trace and event.get("type") in {
                "model_request", "tool_start", "tool_end", "retry", "turn_error", "turn_end",
            }:
                trace.write("computer_diagnostic", turn_id=turn_id,
                            diagnostic_type=event["type"],
                            **{key: value for key, value in event.items() if key != "type"})
        try:
            if computer_task:
                if provider.supports_image_input() is not True:
                    raise ValueError(f"{self.model} does not have confirmed image input. Choose a model with image support.")
                trace = ComputerTrace.from_environment()
                dry_run = computer_task.startswith("--dry-run ")
                computer_session = ComputerSession(
                    cancel_event,
                    confirm_action=lambda preview: self._request_approval("Confirm desktop action", preview),
                    ask_user=lambda question: self._ask_computer_question(question, cancel_event),
                    dry_run=dry_run, trace=trace,
                )
                if trace:
                    trace.write("computer_task_started", task=computer_task, dry_run=dry_run,
                                model=self.model, driver="cua" if computer_session.cua else "dotool")
            answer = run_turn(
                history,
                provider.complete,
                tools,
                project_root,
                confirm_terminal,
                confirm_write,
                on_text_delta=lambda delta: self.post_message(StreamChunk(delta)),
                on_tool_event=tool_event,
                cancel_event=cancel_event,
                confirm_edit=confirm_edit,
                confirm_undo=confirm_undo,
                undo_history=undo_history,
                mcp_client=self.mcp_client,
                confirm_mcp=confirm_mcp,
                limits=self.turn_limits,
                on_status=lambda text: self.post_message(TurnProgress(text)),
                load_context_summary=lambda: self.store.load_context_summary(session_id),
                save_context_summary=lambda summary, count, digest: self.store.save_context_summary(
                    session_id, summary, count, digest,
                ),
                file_change_journal=file_change_journal,
                terminal_jobs=terminal_jobs,
                on_diagnostic=diagnostic,
                computer_session=computer_session,
            )
        except TurnLimitReached as exc:
            finished = TurnFinished(None, str(exc), paused=True)
        except TurnCancelled as exc:
            finished = TurnFinished(None, str(exc) or "Stopped by you", cancelled=True)
        except Exception as exc:
            finished = TurnFinished(None, str(exc))
        else:
            finished = TurnFinished(answer, None)
        finally:
            if trace is not None and finished is not None:
                trace.write(
                    "computer_task_cancelled" if finished.cancelled else
                    "computer_task_failed" if finished.error else "computer_task_finished",
                    error=finished.error, answer=finished.answer[:1000] if finished.answer else None,
                )
            if computer_session is not None:
                try:
                    computer_session.close()
                except Exception as exc:
                    if trace is not None:
                        trace.write("computer_cleanup_error", error=str(exc)[:500])
            if trace is not None:
                trace.close()
        finished.elapsed_seconds = max(0.0, monotonic() - started_at)
        self.post_message(finished)

    def _connect_mcp(self) -> None:
        try:
            statuses = self.mcp_client.start()
            schemas = self.mcp_client.tool_schemas()
            servers = self.mcp_client.status_snapshot()
        except Exception as exc:
            statuses = [f"MCP startup failed ({type(exc).__name__}). Local tools remain available."]
            schemas = []
            servers = []
        self.post_message(MCPReady(statuses, schemas, servers))


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
    parser.add_argument("--model", help=f"Model ID (saved model when resuming, otherwise {DEFAULT_MODEL})")
    parser.add_argument("--effort", help="reasoning effort from the model catalog, or default")
    parser.add_argument("--speed", help="standard or fast, when supported by the selected model")
    parser.add_argument("--project", type=Path, metavar="DIR", help="project folder (default: current folder)")
    add_turn_arguments(parser)
    session_options = parser.add_mutually_exclusive_group()
    session_options.add_argument("--new", action="store_true", help="start a new chat (default)")
    session_options.add_argument("--list", action="store_true", help="list saved chats and exit")
    session_options.add_argument("--search", metavar="QUERY", help="search saved messages and exit")
    session_options.add_argument("--resume", metavar="ID", help="resume a saved chat")
    args = parser.parse_args(argv)
    try:
        limits = turn_limits_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
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
            session_id = ""
        if args.model and session_id:
            store.set_session_model(session_id, args.model)
        elif args.model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", args.model):
            parser.error("Model ID must be 1 to 120 letters, digits, dots, underscores, or hyphens.")
        model = args.model or store.session_model(session_id) or DEFAULT_MODEL
        if session_id and store.session_model(session_id) is None:
            store.set_session_model(session_id, model)
        history = _base_history(project_root)
        if session_id:
            history.extend(store.load_messages(session_id))
        mcp_client = MCPClient()
        app = OrynTUI(
            store=store,
            session_id=session_id,
            project_root=project_root,
            model=model,
            history=history,
            provider=provider_for_model(model),
            mcp_client=mcp_client,
            initial_tools=tool_schemas(),
            turn_limits=limits,
            undo_history=store.load_file_change_history(session_id, project_root) if session_id else [],
        )
        if args.effort or args.speed:
            app.provider.configure(
                reasoning_effort=args.effort or app.provider.reasoning_effort,
                service_tier=args.speed or app.provider.service_tier,
            )
            if session_id:
                store.set_session_model_settings(session_id, model, app._model_settings())
            else:
                app._draft_model_settings[model] = app._model_settings()
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    except Exception as exc:
        print(f"Could not access sessions at {DEFAULT_DB_PATH}: {exc}")
        return

    try:
        app.run()
    finally:
        for terminal_jobs in app.terminal_jobs.values():
            terminal_jobs.close()
        mcp_client.close()


if __name__ == "__main__":
    main()
