"""Local browser chat for the existing Oryn agent loop."""

import argparse
import json
import mimetypes
import re
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from src.agent.conversation_loop import (
    TurnCancelled, TurnLimitReached, TurnLimits, add_turn_arguments, run_turn, turn_limits_from_args,
)
from src.agent.project_context import load_project_instructions
from src.agent.system_prompt import SYSTEM_PROMPT
from src.mcp.client import MCPClient
from src.mcp.discovery import SERVER_NAMES, mcp_settings_path, save_enabled_servers
from src.providers.router import provider_for_model, provider_setup_warning
from src.providers.types import ToolCall
from src.session.sqlite_store import SQLiteSessionStore
from src.tools.file_tools import FileChange
from src.tools.registry import tool_schemas
from src.tools.terminal_tool import TerminalJobManager, validate_project_root


APP_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = APP_ROOT / "web" / "dist"
MODEL = "gpt-5.6-luna"
SESSION_ID_PATTERN = re.compile(r"(?:[0-9a-f]{32}|main)\Z")


@dataclass
class Approval:
    session_id: str
    decision: bool | None = None
    ready: threading.Event = field(default_factory=threading.Event)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        project_root: Path,
        port: int = 9119,
        store: SQLiteSessionStore | None = None,
        provider_factory: Callable[[str], Any] = provider_for_model,
        mcp_client: MCPClient | None = None,
        mcp_settings_file: Path | None = None,
        turn_limits: TurnLimits | None = None,
        model: str = MODEL,
    ) -> None:
        super().__init__(("127.0.0.1", port), DashboardHandler)
        self.project_root = validate_project_root(project_root.expanduser().resolve(strict=True))
        self.store = store if store is not None else SQLiteSessionStore()
        self.provider_factory = provider_factory
        self.mcp_client = mcp_client
        self.mcp_settings_file = mcp_settings_file
        self.model = model
        self.turn_limits = turn_limits or TurnLimits()
        self.token = secrets.token_urlsafe(32)
        self.state_lock = threading.Lock()
        self.active_turns: dict[str, threading.Event] = {}
        self.mcp_reconfiguring = False
        self.approvals: dict[str, Approval] = {}
        self.file_change_history: dict[str, list[FileChange]] = {}
        self.terminal_jobs: dict[str, TerminalJobManager] = {}

    def server_close(self) -> None:
        try:
            for terminal_jobs in self.terminal_jobs.values():
                terminal_jobs.close()
            if self.mcp_client:
                self.mcp_client.close()
        finally:
            super().server_close()

    def session_root(self, session_id: str) -> Path | None:
        if not SESSION_ID_PATTERN.fullmatch(session_id) or not self.store.session_exists(session_id):
            return None
        root = self.store.session_project_root(session_id)
        if root is None:
            root = str(APP_ROOT) if session_id == "main" else None
        if root is None:
            return None
        try:
            path = validate_project_root(Path(root).resolve(strict=True))
        except (OSError, ValueError):
            return None
        return path if path.is_dir() and str(path) == root else None

    def projects(self) -> list[str]:
        roots = [str(self.project_root)]
        for session_id in self.store.list_sessions():
            root = self.session_root(session_id)
            if root is not None and str(root) not in roots:
                roots.append(str(root))
        return roots

    def sessions(self) -> list[dict[str, Any]]:
        result = []
        for session_id in self.store.list_sessions():
            root = self.session_root(session_id)
            if root is None:
                continue
            messages = self.store.load_messages(session_id)
            user_text = [m.get("content", "") for m in messages if m.get("role") == "user"]
            title = self.store.session_title(session_id)
            if not title:
                title = " ".join(str(user_text[0]).split())[:72] if user_text else "New session"
            result.append({"id": session_id, "title": title, "message_count": len(user_text), "project_root": str(root)})
        return result


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, format: str, *args: Any) -> None:
        print(f"web> {self.address_string()} {format % args}")

    def _json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _valid_host(self) -> bool:
        port = self.server.server_address[1]
        if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self._json(403, {"error": "Open the dashboard through its local address."})
            return False
        return True

    def _valid_mutation(self) -> bool:
        if not secrets.compare_digest(self.headers.get("X-Mini-Hermes-Token", ""), self.server.token):
            self._json(403, {"error": "Invalid dashboard token. Reload the page."})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers['Host']}":
            self._json(403, {"error": "Requests must come from this dashboard."})
            return False
        return True

    def _body(self) -> dict[str, Any] | None:
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self._json(415, {"error": "Send a JSON request."})
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 0 < length <= 60_000:
                raise ValueError
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "Invalid JSON request."})
            return None
        return data

    def do_GET(self) -> None:
        if not self._valid_host():
            return
        path = urlsplit(self.path).path
        if path == "/api/bootstrap":
            self._json(200, {
                "token": self.server.token,
                "project": str(self.server.project_root),
                "projects": self.server.projects(),
                "model": self.server.model,
                "sessions": self.server.sessions(),
                "mcp_servers": self.server.mcp_client.status_snapshot() if self.server.mcp_client else [],
            })
            return
        if path.startswith("/api/sessions/"):
            session_id = path.removeprefix("/api/sessions/")
            if self.server.session_root(session_id) is None:
                self._json(404, {"error": "Session not found."})
                return
            messages = self.server.store.load_messages(session_id)
            self._json(200, {"id": session_id, "messages": [
                {
                    "role": m.get("role"),
                    "content": m.get("content", ""),
                    "name": m.get("name"),
                    **({"turn_status": m["turn_status"]} if m.get("turn_status") else {}),
                }
                for m in messages if m.get("role") in {"user", "assistant", "tool"}
            ]})
            return
        if path == "/":
            asset = WEB_ROOT / "index.html"
        elif path.startswith("/assets/"):
            asset = (WEB_ROOT / path.lstrip("/")).resolve()
            if not asset.is_relative_to(WEB_ROOT.resolve()):
                self._json(404, {"error": "Page not found."})
                return
        else:
            self._json(404, {"error": "Page not found."})
            return
        if not asset.is_file():
            self._json(503 if path == "/" else 404, {"error": "Build the React UI with `cd web && npm ci && npm run build`."})
            return
        body = asset.read_bytes()
        content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "image/svg+xml"}:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self._valid_host() or not self._valid_mutation():
            return
        data = self._body()
        if data is None:
            return
        if self.path == "/api/sessions":
            requested_root = data.get("project_root", str(self.server.project_root))
            if not isinstance(requested_root, str) or requested_root not in self.server.projects():
                self._json(400, {"error": "Choose a known project folder."})
                return
            session_id = self.server.store.create_session(Path(requested_root))
            self._json(201, {"id": session_id})
        elif self.path == "/api/turns/cancel":
            session_id = data.get("session_id")
            if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
                self._json(400, {"error": "Give a valid session ID."})
                return
            with self.server.state_lock:
                cancel_event = self.server.active_turns.get(session_id)
                if cancel_event:
                    cancel_event.set()
                    for approval in self.server.approvals.values():
                        if approval.session_id == session_id:
                            approval.decision = False
                            approval.ready.set()
            self._json(200, {"cancelled": cancel_event is not None})
        elif self.path == "/api/approvals":
            approval_id, allow = data.get("id"), data.get("allow")
            if not isinstance(approval_id, str) or not isinstance(allow, bool):
                self._json(400, {"error": "Give an approval ID and a true or false decision."})
                return
            with self.server.state_lock:
                approval = self.server.approvals.get(approval_id)
                if approval is None or approval.ready.is_set():
                    self._json(404, {"error": "That approval has expired."})
                    return
                approval.decision = allow
                approval.ready.set()
            self._json(200, {"allowed": allow})
        elif self.path == "/api/mcp":
            self._update_mcp(data)
        elif self.path == "/api/turns":
            self._turn(data)
        else:
            self._json(404, {"error": "Endpoint not found."})

    def do_PATCH(self) -> None:
        if not self._valid_host() or not self._valid_mutation():
            return
        path = urlsplit(self.path).path
        if not path.startswith("/api/sessions/"):
            self._json(404, {"error": "Endpoint not found."})
            return
        session_id = path.removeprefix("/api/sessions/")
        if self.server.session_root(session_id) is None:
            self._json(404, {"error": "Session not found."})
            return
        data = self._body()
        if data is None:
            return
        title = data.get("title")
        if not isinstance(title, str):
            self._json(400, {"error": "Give a chat title."})
            return
        try:
            renamed = self.server.store.rename_session(session_id, title)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        if not renamed:
            self._json(404, {"error": "Session not found."})
            return
        self._json(200, {"id": session_id, "title": " ".join(title.split())})

    def do_DELETE(self) -> None:
        if not self._valid_host() or not self._valid_mutation():
            return
        path = urlsplit(self.path).path
        if not path.startswith("/api/sessions/"):
            self._json(404, {"error": "Endpoint not found."})
            return
        session_id = path.removeprefix("/api/sessions/")
        with self.server.state_lock:
            if self.server.session_root(session_id) is None:
                result = "missing"
            elif session_id in self.server.active_turns:
                result = "active"
            elif not self.server.store.delete_session(session_id):
                result = "missing"
            else:
                self.server.file_change_history.pop(session_id, None)
                terminal_jobs = self.server.terminal_jobs.pop(session_id, None)
                if terminal_jobs:
                    terminal_jobs.close()
                result = "deleted"
        if result == "missing":
            self._json(404, {"error": "Session not found."})
        elif result == "active":
            self._json(409, {"error": "Stop the active turn before deleting this chat."})
        else:
            self._json(200, {"deleted": True})

    def _event(self, kind: str, **data: Any) -> None:
        line = json.dumps({"type": kind, **data}, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write((line + "\n").encode("utf-8"))
        self.wfile.flush()

    def _update_mcp(self, data: dict[str, Any]) -> None:
        enabled = data.get("enabled")
        if (not isinstance(enabled, dict) or set(enabled) != set(SERVER_NAMES)
                or any(not isinstance(value, bool) for value in enabled.values())):
            self._json(400, {"error": "Choose enabled or disabled for each known MCP server."})
            return
        with self.server.state_lock:
            if self.server.active_turns:
                self._json(409, {"error": "Wait for the active turn to finish before changing MCP servers."})
                return
            if self.server.mcp_reconfiguring:
                self._json(409, {"error": "MCP servers are already being updated."})
                return
            client = self.server.mcp_client
            if client is None:
                self._json(503, {"error": "MCP servers are unavailable in this dashboard."})
                return
            self.server.mcp_reconfiguring = True
            previous = {config.name for config in client.configs if config.enabled}

        try:
            statuses = client.set_enabled({name for name, value in enabled.items() if value})
            save_enabled_servers(
                {name for name, value in enabled.items() if value},
                self.server.mcp_settings_file or mcp_settings_path(),
            )
        except Exception as exc:
            try:
                client.set_enabled(previous)
            except Exception:
                pass
            self._json(500, {"error": f"Could not update MCP servers: {exc}"})
        else:
            self._json(200, {"mcp_servers": statuses})
        finally:
            with self.server.state_lock:
                self.server.mcp_reconfiguring = False

    def _ask(self, session_id: str, cancel_event: threading.Event, action: str, target: str, content: str) -> bool:
        approval_id = secrets.token_urlsafe(18)
        approval = Approval(session_id)
        with self.server.state_lock:
            self.server.approvals[approval_id] = approval
            if cancel_event.is_set():
                approval.decision = False
                approval.ready.set()
        try:
            self._event("approval", id=approval_id, action=action, target=target, content=content)
            deadline = time.monotonic() + 300
            while not approval.ready.wait(0.1):
                if cancel_event.is_set() or time.monotonic() >= deadline:
                    return False
            return not cancel_event.is_set() and approval.decision is True
        finally:
            with self.server.state_lock:
                self.server.approvals.pop(approval_id, None)

    def _turn(self, data: dict[str, Any]) -> None:
        session_id, prompt = data.get("session_id"), data.get("content")
        if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
            self._json(404, {"error": "Session not found."})
            return
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 10_000:
            self._json(400, {"error": "Write a message of 1 to 10,000 characters."})
            return
        with self.server.state_lock:
            root = self.server.session_root(session_id)
            if root is None:
                self._json(404, {"error": "Session not found."})
                return
            if session_id in self.server.active_turns:
                self._json(409, {"error": "This session is already answering. Wait for it to finish."})
                return
            if self.server.mcp_reconfiguring:
                self._json(409, {"error": "MCP servers are restarting. Try again in a moment."})
                return
            cancel_event = threading.Event()
            self.server.active_turns[session_id] = cancel_event
            if session_id not in self.server.file_change_history:
                self.server.file_change_history[session_id] = self.server.store.load_file_change_history(
                    session_id, root,
                )
            undo_history = self.server.file_change_history[session_id]
            if session_id not in self.server.terminal_jobs:
                self.server.terminal_jobs[session_id] = TerminalJobManager(root)
            terminal_jobs = self.server.terminal_jobs[session_id]
        streaming = False
        partial_text: list[str] = []
        try:
            saved = self.server.store.load_messages(session_id)
            instructions = load_project_instructions(root)
            history: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
            if root != APP_ROOT:
                history.append({
                    "role": "developer",
                    "content": f"Active project folder: {str(root)!r}. Tool paths are relative to it.",
                })
            if instructions:
                history.append({
                    "role": "developer",
                    "content": "Project guidance from the root AGENTS.md follows. Apply it to work in "
                               "this project unless it conflicts with the system prompt or user's request.\n\n"
                               f"{instructions}",
                })
            history.extend(saved)
            start = len(history)
            history.append({"role": "user", "content": prompt})
            provider = self.server.provider_factory(self.server.model)
            mcp_tools = self.server.mcp_client.tool_schemas() if self.server.mcp_client else []
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            streaming = True

            def show_tool(phase: str, call: ToolCall, result: str | None) -> None:
                if phase == "start":
                    detail = next((str(call.arguments[key]) for key in
                                   ("path", "ref", "url", "query", "command", "key")
                                   if key in call.arguments), "")
                    self._event("tool_start", id=call.id, name=call.name, detail=detail[:160])
                else:
                    partial_text.clear()  # The completed call already retains this response text.
                    self._event("tool_result", id=call.id, name=call.name,
                                result=(result or "")[:2000])

            def show_text(delta: str) -> None:
                partial_text.append(delta)
                self._event("delta", text=delta)

            def mark_interrupted_reply(turn_status: str) -> None:
                for message in history[start:]:
                    if message.get("role") == "assistant" and (message.get("content") or message.get("tool_calls")):
                        message["turn_status"] = turn_status
                if partial_text:
                    history.append({
                        "role": "assistant",
                        "content": "".join(partial_text),
                        "turn_status": turn_status,
                    })

            try:
                answer = run_turn(
                    history, provider.complete, tool_schemas(mcp_tools), root,
                    lambda command: self._ask(session_id, cancel_event, "terminal", "Project terminal", command),
                    lambda path, content, exists: self._ask(
                        session_id,
                        cancel_event,
                        "replace" if exists else "create", path, content
                    ),
                    on_text_delta=show_text,
                    on_tool_event=show_tool,
                    cancel_event=cancel_event,
                    confirm_edit=lambda path, diff: self._ask(
                        session_id, cancel_event, "edit", path, diff
                    ),
                    confirm_undo=lambda path, diff, removes_created_file: self._ask(
                        session_id,
                        cancel_event,
                        "undo_created" if removes_created_file else "undo",
                        path,
                        diff,
                    ),
                    undo_history=undo_history,
                    file_change_journal=lambda phase, change: self.server.store.record_file_change(
                        session_id, root, phase, change,
                    ),
                    load_context_summary=lambda: self.server.store.load_context_summary(session_id),
                    save_context_summary=lambda summary, count, digest: self.server.store.save_context_summary(
                        session_id, summary, count, digest,
                    ),
                    terminal_jobs=terminal_jobs,
                    on_diagnostic=lambda turn_id, event: self.server.store.append_diagnostic(
                        session_id, turn_id, event,
                    ),
                    mcp_client=self.server.mcp_client,
                    confirm_mcp=lambda name, preview: self._ask(
                        session_id, cancel_event, "mcp", name, preview
                    ),
                    limits=self.server.turn_limits,
                    on_status=lambda text: self._event("progress", message=text),
                )
                if cancel_event.is_set():
                    raise TurnCancelled
                history.append({"role": "assistant", "content": answer})
                outcome = {"type": "done", "answer": answer}
            except TurnCancelled:
                mark_interrupted_reply("cancelled")
                outcome = {"type": "cancelled"}
            except TurnLimitReached as exc:
                mark_interrupted_reply("paused")
                outcome = {"type": "paused", "message": str(exc)}
            except Exception as exc:
                mark_interrupted_reply("failed")
                outcome = {"type": "error", "message": f"Agent turn failed: {exc}"}
            try:
                self.server.store.append_messages(history[start:], session_id)
            except Exception as exc:
                outcome = {"type": "error", "message": f"Could not save this turn: {exc}"}
            kind = outcome.pop("type")
            self._event(kind, **outcome)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            if not self.wfile.closed:
                try:
                    if streaming:
                        self._event("error", message=f"Dashboard error: {exc}")
                    else:
                        self._json(500, {"error": str(exc)})
                except (BrokenPipeError, ConnectionResetError):
                    pass
        finally:
            with self.server.state_lock:
                self.server.active_turns.pop(session_id, None)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local Oryn browser dashboard.")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Project folder (default: current folder)")
    parser.add_argument("--port", type=int, default=9119)
    parser.add_argument("--model", default=MODEL, help="Model ID, such as gpt-5.6-luna or gemini-3.8-flash")
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser tab")
    add_turn_arguments(parser)
    args = parser.parse_args(argv)
    try:
        limits = turn_limits_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    server = DashboardServer(
        args.project, args.port, mcp_client=MCPClient(), turn_limits=limits, model=args.model,
    )
    if warning := provider_setup_warning(provider_for_model(server.model)):
        print(f"config> {warning}")
    for status in server.mcp_client.start():
        print(f"mcp> {status}")
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Oryn dashboard: {url}")
    print(f"Project: {server.project_root}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
