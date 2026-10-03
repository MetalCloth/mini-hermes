"""Bounded, read-only AT-SPI hints for the focused Hyprland window."""

from collections import deque
import json
from math import isfinite
from pathlib import Path
import subprocess
import sys
from time import monotonic


SYSTEM_PYTHON = "/usr/bin/python3"  # This interpreter has the distro's GI bindings.
PROBE_TIMEOUT = 0.9
MAX_ROWS = 40
MAX_TEXT = 4000
ROLES = {
    "button", "toggle button", "link", "entry", "text", "check box",
    "radio button", "menu item", "page tab", "combo box", "slider", "heading",
}


def observe_active_window(expected_address: str | None, screenshot_size: tuple[int, int]) -> dict:
    """Return safe-to-display labels, or a reason to use the screenshot alone."""
    started = monotonic()

    def result(status: str, labels: str = "", count: int = 0, coordinates: int = 0) -> dict:
        return {
            "status": status, "labels": labels, "count": count,
            "coordinates": coordinates,
            "elapsed_ms": round((monotonic() - started) * 1000, 1),
        }

    if not expected_address:
        return result("no_window")
    try:
        active = subprocess.run(
            ["hyprctl", "activewindow", "-j"], capture_output=True, check=True, timeout=0.5,
        )
        window = json.loads(active.stdout)
        if window.get("address") != expected_address:
            return result("focus_changed")
        pid, size = window.get("pid"), window.get("size")
        if type(pid) is not int or pid <= 0 or not isinstance(size, list) or len(size) != 2:
            return result("invalid_window")
        width, height = size
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            return result("invalid_window")
        probe = subprocess.run(
            [SYSTEM_PYTHON, str(Path(__file__).resolve()), str(pid), str(width), str(height)],
            capture_output=True, timeout=PROBE_TIMEOUT,
        )
        if probe.returncode != 0:
            return result("unavailable")
        data = json.loads(probe.stdout)
        if not isinstance(data, dict) or data.get("status") not in {"available", "sparse", "ambiguous"}:
            return result("unavailable")
        if data["status"] != "available":
            return result(data["status"])
        rows = data.get("rows")
        if not isinstance(rows, list) or not all(
            isinstance(row, dict) and row.get("role") in ROLES
            and isinstance(row.get("name"), str) and 0 < len(row["name"]) <= 100
            for row in rows
        ):
            return result("unavailable")
        monitor = None
        if any(row.get("screen") and row.get("window") for row in rows[:MAX_ROWS]):
            try:
                monitors = json.loads(subprocess.run(
                    ["hyprctl", "monitors", "-j"], capture_output=True, check=True, timeout=0.5,
                ).stdout)
                monitor = next(item for item in monitors if item.get("id") == window.get("monitor"))
            except (OSError, ValueError, TypeError, StopIteration, subprocess.SubprocessError):
                pass
        kept = []
        length = 0
        coordinates = 0
        for row in rows[:MAX_ROWS]:
            line = f'{row["role"]}: {json.dumps(row["name"], ensure_ascii=False)}'
            box = _map_rect(row, data.get("frame"), window, monitor, screenshot_size)
            if box:
                line += f" [x={box[0]}, y={box[1]}, w={box[2]}, h={box[3]}]"
            added = len(line.encode("utf-8")) + bool(kept)
            if length + added > MAX_TEXT:
                break
            kept.append(line)
            length += added
            coordinates += bool(box)
        labels = "\n".join(kept)
        return result("available", labels, len(kept), coordinates) if labels else result("sparse")
    except subprocess.TimeoutExpired:
        return result("timed_out")
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        return result("unavailable")


def _rect(value) -> bool:
    return (isinstance(value, list) and len(value) == 4
            and all(type(part) is int for part in value)
            and value[2] > 0 and value[3] > 0)


def _map_rect(row: dict, frame: dict | None, window: dict, monitor: dict | None,
              screenshot_size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Map a self-consistent AT-SPI rectangle into native screenshot pixels."""
    if not isinstance(frame, dict) or not isinstance(monitor, dict):
        return None
    screen, local = row.get("screen"), row.get("window")
    frame_screen, frame_local = frame.get("screen"), frame.get("window")
    at, size = window.get("at"), window.get("size")
    if not all(_rect(item) for item in (screen, local, frame_screen, frame_local)):
        return None
    if not (isinstance(at, list) and len(at) == 2 and all(type(v) is int for v in at)
            and isinstance(size, list) and len(size) == 2
            and all(type(v) is int and v > 0 for v in size)):
        return None
    if any(abs(frame_screen[index + 2] - size[index]) > 4 for index in (0, 1)):
        return None
    if any(abs(frame_local[index + 2] - frame_screen[index + 2]) > 2 for index in (0, 1)):
        return None
    if any(abs(screen[index + 2] - local[index + 2]) > 2 for index in (0, 1)):
        return None
    offset = [screen[index] - frame_screen[index] for index in (0, 1)]
    if any(abs(offset[index] - (local[index] - frame_local[index])) > 2 for index in (0, 1)):
        return None
    if any(offset[index] < 0 or offset[index] + screen[index + 2] > frame_screen[index + 2]
           for index in (0, 1)):
        return None
    scale = monitor.get("scale")
    if (type(scale) not in (int, float) or not isfinite(scale) or scale <= 0
            or monitor.get("id") != window.get("monitor")
            or (monitor.get("width"), monitor.get("height")) != screenshot_size
            or type(monitor.get("x")) is not int or type(monitor.get("y")) is not int):
        return None
    left = round((at[0] + offset[0] - monitor["x"]) * scale)
    top = round((at[1] + offset[1] - monitor["y"]) * scale)
    right = round((at[0] + offset[0] + screen[2] - monitor["x"]) * scale)
    bottom = round((at[1] + offset[1] + screen[3] - monitor["y"]) * scale)
    if not (0 <= left < right <= screenshot_size[0] and 0 <= top < bottom <= screenshot_size[1]):
        return None
    return left, top, right - left, bottom - top


def _walk(root, limit: int):
    queue = deque([root])
    for _ in range(limit):
        if not queue:
            break
        node = queue.popleft()
        yield node
        try:
            count = min(node.get_child_count(), 40)
            queue.extend(node.get_child_at_index(index) for index in range(count))
        except Exception:
            continue


def _row(node, Atspi) -> dict | None:
    try:
        role = node.get_role_name()
        if role not in ROLES:
            return None
        states = node.get_state_set()
        if not (states.contains(Atspi.StateType.SHOWING)
                and states.contains(Atspi.StateType.VISIBLE)):
            return None
        name = "".join(char if char.isprintable() else " " for char in (node.get_name() or "")[:200]).strip()
        name = " ".join(name.split())[:100]
        return {"role": role, "name": name} if name else None
    except Exception:
        return None


def _node_rect(node, Atspi, coordinates) -> list[int] | None:
    try:
        bounds = node.get_component().get_extents(coordinates)
        result = [bounds.x, bounds.y, bounds.width, bounds.height]
        return result if _rect(result) else None
    except Exception:
        return None


def _probe(pid: int, width: int, height: int) -> dict:
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    Atspi.init()
    desktop = Atspi.get_desktop(0)
    apps = []
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        try:
            if app.get_process_id() == pid:
                apps.append(app)
        except Exception:
            continue
    if len(apps) != 1:
        return {"status": "ambiguous" if apps else "sparse"}
    frames = []
    app = apps[0]
    for index in range(app.get_child_count()):
        child = app.get_child_at_index(index)
        try:
            bounds = child.get_component().get_extents(Atspi.CoordType.SCREEN)
            if abs(bounds.width - width) <= 4 and abs(bounds.height - height) <= 4:
                frames.append(child)
        except Exception:
            continue
    if len(frames) != 1:
        return {"status": "ambiguous" if frames else "sparse"}

    frame = frames[0]
    chrome_rows = []
    document = None
    for node in _walk(frame, 500):
        try:
            if node.get_role_name() == "document web":
                document = node
                break
        except Exception:
            continue
        row = _row(node, Atspi)
        if row:
            chrome_rows.append((row, node))
    if document is None:
        rows = chrome_rows
    else:
        page_rows = []
        for node in _walk(document, 450):
            row = _row(node, Atspi)
            if row:
                page_rows.append((row, node))
        rows = page_rows[:32] + chrome_rows[:8]
    if not rows:
        return {"status": "sparse"}
    result_rows = []
    for row, node in rows[:MAX_ROWS]:
        result_rows.append({
            **row,
            "screen": _node_rect(node, Atspi, Atspi.CoordType.SCREEN),
            "window": _node_rect(node, Atspi, Atspi.CoordType.WINDOW),
        })
    return {
        "status": "available",
        "frame": {
            "screen": _node_rect(frame, Atspi, Atspi.CoordType.SCREEN),
            "window": _node_rect(frame, Atspi, Atspi.CoordType.WINDOW),
        },
        "rows": result_rows,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(_probe(*(int(value) for value in sys.argv[1:4])), ensure_ascii=False))
    except Exception:
        print('{"status":"sparse"}')
