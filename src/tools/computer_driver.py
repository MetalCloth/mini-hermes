"""Screenshot and input commands for the local Hyprland desktop."""

import io
import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image


class HyprlandDriver:
    def __init__(self) -> None:
        local_wdotool = Path(__file__).resolve().parents[2] / ".venv/bin/wdotool"
        self.wdotool = shutil.which("wdotool") or (str(local_wdotool) if local_wdotool.is_file() else None)
        if not self.wdotool or not shutil.which("grim") or not shutil.which("hyprctl"):
            raise RuntimeError("Computer mode needs Hyprland, grim, and wdotool. See README.md.")
        try:
            result = subprocess.run(
                ["hyprctl", "monitors", "-j"], capture_output=True, check=True, timeout=5,
            )
            monitors = json.loads(result.stdout)
            monitor = next((item for item in monitors if item.get("focused") and not item.get("disabled")), None)
            if not monitor:
                raise ValueError("no focused monitor")
            self.output = monitor["name"]
            self.monitor_id = monitor["id"]
            self.width, self.height = int(monitor["width"]), int(monitor["height"])
            if self.width <= 0 or self.height <= 0:
                raise ValueError("invalid monitor size")
        except (subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("Could not find a focused Hyprland monitor.") from exc

    def active_window(self) -> str | None:
        """Identify the window receiving input on the selected monitor."""
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
                return address
            result = subprocess.run(
                ["hyprctl", "monitors", "-j"], capture_output=True, check=True, timeout=5,
            )
            monitors = json.loads(result.stdout)
            focused = next(item for item in monitors if item.get("focused") and not item.get("disabled"))
            if focused.get("id") != self.monitor_id:
                raise ValueError("selected monitor is no longer focused")
            return None
        except (subprocess.SubprocessError, ValueError, TypeError, AttributeError, StopIteration) as exc:
            raise RuntimeError("Could not verify the active window on the selected monitor.") from exc

    def screenshot(self) -> tuple[bytes, tuple[int, int]]:
        try:
            result = subprocess.run(
                ["grim", "-o", self.output, "-"], capture_output=True, check=True, timeout=10,
            )
            with Image.open(io.BytesIO(result.stdout)) as image:
                if image.size != (self.width, self.height):
                    raise ValueError("monitor resolution changed")
                image = image.convert("RGB")
                image.thumbnail((800, 800), Image.Resampling.LANCZOS)
                size = image.size
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=80)
                return output.getvalue(), size
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            raise RuntimeError("Could not capture the selected monitor.") from exc

    def _input(self, *args: str, input_data: bytes | None = None) -> None:
        try:
            subprocess.run(
                [self.wdotool, "--backend", "wlr-protocols", *args],
                input=input_data, capture_output=True, check=True, timeout=10,
            )
        except subprocess.SubprocessError as exc:
            raise RuntimeError(f"Desktop input failed during {args[0]}.") from exc

    def _move(self, point: tuple[int, int], image_size: tuple[int, int]) -> None:
        x = min(self.width - 1, round(point[0] * self.width / image_size[0]))
        y = min(self.height - 1, round(point[1] * self.height / image_size[1]))
        self._input("mousemove", "--output", self.output, str(x), str(y))

    def execute(self, name: str, args: dict, image_size: tuple[int, int]) -> None:
        if name in {"click", "left_double", "right_single", "scroll"}:
            self._move(args["point"], image_size)
        if name in {"click", "left_double", "right_single"}:
            button = "3" if name == "right_single" else "1"
            self._input("click", button)
            if name == "left_double":
                self._input("click", button)
        elif name == "scroll":
            delta = {"up": (0, -3), "down": (0, 3), "left": (-3, 0), "right": (3, 0)}[args["direction"]]
            self._input("scroll", str(delta[0]), str(delta[1]))
        elif name == "hotkey":
            self._input("key", args["key"])
        elif name == "type":
            content = args["content"]
            if content.endswith("\n"):
                if content[:-1]:
                    self._input("type", "--file", "-", input_data=content[:-1].encode("utf-8"))
                self._input("key", "Return")
            else:
                self._input("type", "--file", "-", input_data=content.encode("utf-8"))
