"""CUA desktop observation and clicks, with dotool for Hyprland input."""

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from src.tools.computer_driver import HyprlandDriver


class CuaDriver(HyprlandDriver):
    def __init__(self, trace=None) -> None:
        super().__init__(trace)
        # CUA's desktop coordinates refer to the whole display. Until multi-monitor
        # mapping is verified, refuse input that might land on another monitor.
        if self.monitor_rect != (0.0, 0.0, float(self.width), float(self.height)) or self.desktop_bounds != (0.0, 0.0, float(self.width), float(self.height)):
            raise RuntimeError("CUA computer mode currently needs one unscaled monitor at (0, 0). Set ORYN_COMPUTER_DRIVER=dotool for this layout.")
        local = Path(__file__).resolve().parents[2] / ".venv/bin/cua-driver"
        self.binary = shutil.which("cua-driver") or (str(local) if local.is_file() else None)
        if not self.binary:
            raise RuntimeError("CUA Driver is missing. Install cua-driver in .venv/bin, or set ORYN_COMPUTER_DRIVER=dotool.")
        self._temporary = tempfile.TemporaryDirectory(prefix="oryn-cua-")
        self.socket = Path(self._temporary.name) / "driver.sock"
        self.session = f"oryn-{os.getpid()}-{id(self)}"
        self.env = os.environ.copy()
        self.env["CUA_DRIVER_RS_ENABLE_WAYLAND"] = "1"
        self._server = None
        self._session_started = False
        self._counter = 0
        self._capture_id = None
        self._snapshot_stamp = None
        self._window = None
        self._rows = []
        self._tree = ""
        try:
            log = open(Path(self._temporary.name) / "server.log", "wb")
            try:
                self._server = subprocess.Popen(
                    [self.binary, "serve", "--no-overlay", "--socket", str(self.socket)], env=self.env,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                )
            finally:
                log.close()
            for _ in range(50):
                if self.socket.exists():
                    break
                if self._server.poll() is not None:
                    raise RuntimeError("CUA Driver exited during startup.")
                time.sleep(0.1)
            else:
                raise RuntimeError("CUA Driver did not start within five seconds.")
            self._call("start_session", {"session": self.session})
            self._session_started = True
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._session_started:
            try:
                self._call("end_session", {"session": self.session}, timeout=3)
            except RuntimeError:
                pass
            self._session_started = False
        if self._server is not None:
            self._server.terminate()
            try:
                self._server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._server.kill()
                self._server.wait(timeout=3)
            self._server = None
        self._temporary.cleanup()

    def _call(self, name: str, args: dict, *, timeout: int = 12) -> dict:
        started = time.monotonic()
        if self.trace:
            self.trace.write("cua_call_start", tool=name)
        try:
            result = subprocess.run(
                [self.binary, "call", name, json.dumps(args), "--socket", str(self.socket)],
                capture_output=True, text=True, timeout=timeout, env=self.env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            if self.trace:
                self.trace.write("cua_call_result", tool=name, success=False, error=str(exc), elapsed_ms=round((time.monotonic() - started) * 1000))
            raise RuntimeError(f"CUA {name} failed: {exc}") from exc
        try:
            response = json.loads(result.stdout) if result.returncode == 0 else None
        except json.JSONDecodeError:
            response = None
        success = result.returncode == 0 and isinstance(response, dict) and response.get("effect") != "refused"
        if self.trace:
            self.trace.write(
                "cua_call_result", tool=name, success=success, returncode=result.returncode,
                effect=response.get("effect") if isinstance(response, dict) else None,
                route=response.get("route") if isinstance(response, dict) else None,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                error=(result.stderr or result.stdout)[:500] if not success else "",
            )
        if not success:
            detail = (result.stderr or result.stdout).strip()[:500]
            raise RuntimeError(f"CUA {name} failed: {detail or 'invalid response'}")
        return response

    def _active_cua_window(self) -> dict | None:
        active = self._active_window_data()
        pid = active.get("pid")
        if type(pid) is not int or pid <= 0:
            return None
        windows = self._call("list_windows", {
            "pid": pid, "on_screen_only": True, "session": self.session,
        }).get("windows", [])
        if not isinstance(windows, list):
            return None
        candidates = [item for item in windows if isinstance(item, dict) and item.get("pid") == pid]
        if len(candidates) > 1:
            candidates = [item for item in candidates if item.get("title") == active.get("title")]
        return candidates[0] if len(candidates) == 1 else None

    def screenshot(self) -> tuple[bytes, tuple[int, int]]:
        self._counter += 1
        path = Path(self._temporary.name) / f"screen-{self._counter}.png"
        state = self._call("get_desktop_state", {
            "session": self.session, "max_image_dimension": 0,
            "screenshot_out_file": str(path),
        })
        size = (state.get("screenshot_width"), state.get("screenshot_height"))
        if size != (self.width, self.height) or not path.is_file():
            raise RuntimeError("CUA screenshot size does not match the selected monitor.")
        screenshot = path.read_bytes()
        path.unlink(missing_ok=True)
        self._capture_id = state.get("capture_id")
        self._snapshot_stamp = self.active_window_stamp()
        self._window = None
        self._rows = []
        self._tree = ""
        try:
            window = self._active_cua_window()
            if window and type(window.get("window_id")) is int:
                self._window = window
                if not self._window_geometry_matches():
                    self._window = None
                    return screenshot, size
                state = self._call("get_window_state", {
                    "pid": window["pid"], "window_id": window["window_id"],
                    "session": self.session, "include_screenshot": False,
                    "max_elements": 80, "max_depth": 8, "timeout_ms": 1500,
                }, timeout=5)
                self._rows = state.get("elements", []) if isinstance(state.get("elements"), list) else []
                self._tree = state.get("tree_markdown", "") if isinstance(state.get("tree_markdown"), str) else ""
        except RuntimeError as exc:
            if self.trace:
                self.trace.write("cua_accessibility_unavailable", error=str(exc)[:300])
        return screenshot, size

    def _window_geometry_matches(self) -> bool:
        if not self._window or not self._snapshot_stamp or not self._snapshot_stamp[2]:
            return False
        bounds = self._window.get("bounds")
        if not isinstance(bounds, dict):
            return False
        expected = (*self._snapshot_stamp[2], *self._snapshot_stamp[3])
        actual = tuple(bounds.get(key) for key in ("x", "y", "width", "height"))
        return all(type(value) in (int, float) for value in actual) and all(abs(a - b) <= 4 for a, b in zip(actual, expected))

    def _box(self, row: dict) -> tuple[int, int, int, int] | None:
        if not self._window_geometry_matches():
            return None
        frame = row.get("frame")
        if not isinstance(frame, dict) or any(type(frame.get(key)) not in (int, float) for key in ("x", "y", "w", "h")):
            return None
        x, y = self._window["bounds"]["x"] + frame["x"], self._window["bounds"]["y"] + frame["y"]
        w, h = frame["w"], frame["h"]
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > self.width or y + h > self.height:
            return None
        return int(x), int(y), int(w), int(h)

    def accessibility_observation(self, _window=None, _size=None) -> dict:
        lines = []
        coordinates = 0
        for row in self._rows:
            if not isinstance(row, dict):
                continue
            label = row.get("label")
            value = row.get("value")
            if not isinstance(label, str) or not label.strip():
                continue
            line = f"{row.get('role', 'element')}: {json.dumps(label[:120], ensure_ascii=False)}"
            box = self._box(row)
            if box:
                line += f" [x={box[0]}, y={box[1]}, w={box[2]}, h={box[3]}]"
                coordinates += 1
            if isinstance(value, str) and value:
                line += f" value={json.dumps(value[:120], ensure_ascii=False)}"
            if len("\n".join(lines + [line])) > 1600:
                break
            lines.append(line)
            if len(lines) >= 40:
                break
        labels = self._tree[:2300]
        if lines:
            labels += "\nMapped elements:\n" + "\n".join(lines)
        return {"status": "available" if labels else "sparse", "labels": labels[:4000], "count": len(lines), "coordinates": coordinates}

    def execute(self, name: str, args: dict) -> None:
        if self.active_window_stamp() != self._snapshot_stamp:
            raise RuntimeError("The active window changed since CUA captured the screen.")
        if name in {"click", "left_double"}:
            x, y = args["point"]
            payload = {"scope": "desktop", "x": x, "y": y, "session": self.session, "delivery_mode": "foreground"}
            if self._capture_id:
                payload["capture_id"] = self._capture_id
            if name == "left_double":
                payload["count"] = 2
            self._call("click", payload)
            return
        if self.trace:
            self.trace.write("cua_dotool_fallback", action=name)
        super().execute(name, args)
