"""Screenshot and input commands for the local Hyprland desktop."""

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

from PIL import Image

from src.computer_logging import ComputerTrace


def computer_driver_name() -> str:
    name = os.environ.get("ORYN_COMPUTER_DRIVER", "cua").strip().lower()
    if name not in {"cua", "dotool"}:
        raise ValueError("ORYN_COMPUTER_DRIVER must be 'cua' or 'dotool'.")
    return name


class HyprlandDriver:
    def __init__(self, trace: ComputerTrace | None = None) -> None:
        self.trace = trace
        local_dotool = Path(__file__).resolve().parents[2] / ".venv/bin/dotool"
        self.dotool = shutil.which("dotool") or (str(local_dotool) if local_dotool.is_file() else None)
        if not self.dotool or not shutil.which("grim") or not shutil.which("hyprctl"):
            raise RuntimeError("Computer mode needs Hyprland, grim, and dotool. See README.md.")
        try:
            result = subprocess.run(
                ["hyprctl", "monitors", "-j"], capture_output=True, check=True, timeout=5,
            )
            monitors = json.loads(result.stdout)
            active_monitors = [item for item in monitors if not item.get("disabled")]
            monitor = next((item for item in active_monitors if item.get("focused")), None)
            if not monitor:
                raise ValueError("no focused monitor")
            self.output = monitor["name"]
            self.monitor_id = monitor["id"]
            self.width, self.height = int(monitor["width"]), int(monitor["height"])
            if self.width <= 0 or self.height <= 0:
                raise ValueError("invalid monitor size")
            layouts = []
            for item in active_monitors:
                scale = float(item.get("scale", 1))
                if scale <= 0:
                    raise ValueError("invalid monitor scale")
                x, y = float(item["x"]), float(item["y"])
                width = float(item["width"]) / scale
                height = float(item["height"]) / scale
                if width <= 0 or height <= 0:
                    raise ValueError("invalid monitor geometry")
                layouts.append((x, y, width, height))
            selected = next(item for item in active_monitors if item["id"] == self.monitor_id)
            scale = float(selected.get("scale", 1))
            self.monitor_rect = (
                float(selected["x"]), float(selected["y"]),
                float(selected["width"]) / scale, float(selected["height"]) / scale,
            )
            self.desktop_bounds = (
                min(rect[0] for rect in layouts),
                min(rect[1] for rect in layouts),
                max(rect[0] + rect[2] for rect in layouts),
                max(rect[1] + rect[3] for rect in layouts),
            )
        except (subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("Could not read the focused Hyprland monitor layout.") from exc

    def _active_window_data(self) -> dict:
        """Read the window receiving input on the selected monitor."""
        try:
            result = subprocess.run(
                ["hyprctl", "activewindow", "-j"], capture_output=True, check=True, timeout=5,
            )
            window = json.loads(result.stdout)
            if not isinstance(window, dict):
                raise ValueError("invalid active window")
            address = window.get("address")
            if address and address != "0x0":
                if not isinstance(address, str) or window.get("monitor") != self.monitor_id:
                    raise ValueError("active window is on another monitor")
                return window
            result = subprocess.run(
                ["hyprctl", "monitors", "-j"], capture_output=True, check=True, timeout=5,
            )
            monitors = json.loads(result.stdout)
            focused = next(item for item in monitors if item.get("focused") and not item.get("disabled"))
            if focused.get("id") != self.monitor_id:
                raise ValueError("selected monitor is no longer focused")
            return {"address": None, "monitor": self.monitor_id}
        except (subprocess.SubprocessError, ValueError, TypeError, AttributeError, StopIteration) as exc:
            raise RuntimeError("Could not verify the active window on the selected monitor.") from exc

    def active_window(self) -> str | None:
        return self._active_window_data()["address"]

    def active_window_stamp(self) -> tuple:
        """Bind a screenshot or plan to both window identity and geometry."""
        window = self._active_window_data()
        address = window["address"]
        if address is None:
            return None, self.monitor_id, None, None
        at, size = window.get("at"), window.get("size")
        if not (isinstance(at, list) and isinstance(size, list)
                and len(at) == len(size) == 2
                and all(type(value) is int for value in at + size)
                and all(value > 0 for value in size)):
            raise RuntimeError("Could not verify the active window geometry.")
        return address, self.monitor_id, tuple(at), tuple(size)

    def screenshot(self) -> tuple[bytes, tuple[int, int]]:
        try:
            result = subprocess.run(
                ["grim", "-o", self.output, "-"], capture_output=True, check=True, timeout=10,
            )
            with Image.open(io.BytesIO(result.stdout)) as image:
                if image.size != (self.width, self.height):
                    raise ValueError("monitor resolution changed")
                image = image.convert("RGB")
                size = image.size
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=95)
                return output.getvalue(), size
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            raise RuntimeError("Could not capture the selected monitor.") from exc

    def _input(self, actions: list[str]) -> None:
        """Send dotool's stdin action stream in one process."""
        command = [self.dotool]
        stdin = "\n".join(actions) + "\n"
        input_data = stdin.encode("utf-8")
        action = actions[0].split(maxsplit=1)[0]
        if self.trace:
            self.trace.write("dotool_start", argv=command, stdin=stdin)
        try:
            result = subprocess.run(
                command, input=input_data, capture_output=True, timeout=10,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = _output_text(exc.stderr)
            if self.trace:
                self.trace.write(
                    "dotool_result", returncode=None, success=False, timed_out=True,
                    stdout=_output_text(exc.stdout)[:4000], stderr=stderr[:4000],
                )
            raise RuntimeError(f"Desktop input timed out during {action}.") from exc
        except OSError as exc:
            if self.trace:
                self.trace.write("dotool_result", returncode=None, success=False, error=str(exc))
            raise RuntimeError(f"Desktop input failed during {action}: {exc}") from exc
        stdout, stderr = _output_text(result.stdout), _output_text(result.stderr)
        success = result.returncode == 0 and not stderr
        if self.trace:
            self.trace.write(
                "dotool_result", returncode=result.returncode, success=success,
                stdout=stdout[:4000], stderr=stderr[:4000],
            )
        # dotool reports some rejected keys as stderr warnings with exit code 0.
        if not success:
            detail = f": {stderr[:400]}" if stderr else ""
            raise RuntimeError(
                f"Desktop input failed during {action} (exit {result.returncode}){detail}"
            )

    def _move(self, point: tuple[int, int]) -> str:
        """Convert screenshot pixels to dotool's normalized desktop coordinates."""
        x = min(self.width - 1, max(0, point[0]))
        y = min(self.height - 1, max(0, point[1]))
        left, top, width, height = self.monitor_rect
        desktop_left, desktop_top, desktop_right, desktop_bottom = self.desktop_bounds
        global_x = left + x / self.width * width
        global_y = top + y / self.height * height
        screen_x = (global_x - desktop_left) / (desktop_right - desktop_left)
        screen_y = (global_y - desktop_top) / (desktop_bottom - desktop_top)
        return f"mouseto {screen_x:.6f} {screen_y:.6f}"

    def execute(self, name: str, args: dict) -> None:
        actions = []
        if name in {"click", "left_double", "right_single", "scroll"}:
            actions.append(self._move(args["point"]))
        if name in {"click", "left_double", "right_single"}:
            button = "right" if name == "right_single" else "left"
            actions.append(f"click {button}")
            if name == "left_double":
                actions.append(f"click {button}")
        elif name == "scroll":
            scroll = {"up": "wheel 3", "down": "wheel -3", "left": "hwheel 3", "right": "hwheel -3"}
            actions.append(scroll[args["direction"]])
        elif name == "hotkey":
            actions.append(f"key {args['key']}")
        elif name == "type":
            lines = args["content"].split("\n")
            for index, line in enumerate(lines):
                if line:
                    actions.append(f"type {line}")
                if index < len(lines) - 1:
                    actions.append("key enter")
        else:
            raise ValueError(f"Unsupported desktop action: {name}")
        self._input(actions)


def _output_text(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""
