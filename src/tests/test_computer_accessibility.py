"""Target binding and bounded AT-SPI fallback checks."""

import json
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from src.tools.computer_accessibility import _map_rect, _probe, observe_active_window


class Node:
    def __init__(self, role="panel", name="", children=(), *, pid=0, size=(0, 0),
                 at=(0, 0), showing=True):
        self.role, self.name, self.children = role, name, list(children)
        self.pid, self.size, self.at, self.showing = pid, size, at, showing

    def get_process_id(self):
        return self.pid

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_component(self):
        return self

    def get_extents(self, _coordinate_type):
        return SimpleNamespace(x=self.at[0], y=self.at[1],
                               width=self.size[0], height=self.size[1])

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_state_set(self):
        return self

    def contains(self, _state):
        return self.showing


def fake_gi(apps):
    desktop = Node(children=apps)
    atspi = SimpleNamespace(
        init=lambda: None, get_desktop=lambda _index: desktop,
        CoordType=SimpleNamespace(SCREEN=0, WINDOW=1),
        StateType=SimpleNamespace(SHOWING=1, VISIBLE=2),
    )
    gi = ModuleType("gi")
    gi.require_version = lambda *_args: None
    repository = ModuleType("gi.repository")
    repository.Atspi = atspi
    gi.repository = repository
    return {"gi": gi, "gi.repository": repository}


class ComputerAccessibilityTests(unittest.TestCase):
    def test_probe_uses_exact_pid_and_frame_and_prioritizes_page(self):
        page = Node("document web", children=[
            Node("heading", "A\npage"), Node("link", "Open result"),
            Node("link", "Hidden result", showing=False),
        ])
        frame = Node("frame", children=[Node("button", "Back"), page], size=(1808, 1018))
        wrong_app = Node("application", children=[frame], pid=99)
        app = Node("application", children=[frame], pid=42)
        with patch.dict(sys.modules, fake_gi([wrong_app, app])):
            result = _probe(42, 1808, 1018)
        self.assertEqual(result["status"], "available")
        self.assertEqual(
            [(row["role"], row["name"]) for row in result["rows"]],
            [("heading", "A page"), ("link", "Open result"), ("button", "Back")],
        )
        self.assertNotIn("Hidden", json.dumps(result))
        self.assertEqual(result["frame"]["screen"], [0, 0, 1808, 1018])
        self.assertIsNone(result["rows"][0]["screen"])

    def test_probe_refuses_ambiguous_or_missing_frame(self):
        frame = Node("frame", size=(800, 600))
        with patch.dict(sys.modules, fake_gi([Node("application", children=[frame, frame], pid=42)])):
            self.assertEqual(_probe(42, 800, 600)["status"], "ambiguous")
        with patch.dict(sys.modules, fake_gi([Node("application", children=[frame], pid=42)])):
            self.assertEqual(_probe(42, 1920, 1080)["status"], "sparse")

    def test_parent_rejects_focus_change_and_times_out_safely(self):
        active = {"address": "0xother", "pid": 42, "size": [800, 600]}
        with patch("src.tools.computer_accessibility.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, json.dumps(active).encode(), b"")
            self.assertEqual(observe_active_window("0xtarget", (800, 600))["status"], "focus_changed")
            run.assert_called_once()
            active["address"] = "0xtarget"
            run.side_effect = [
                subprocess.CompletedProcess([], 0, json.dumps(active).encode(), b""),
                subprocess.TimeoutExpired(["python3"], 0.9),
            ]
            self.assertEqual(observe_active_window("0xtarget", (800, 600))["status"], "timed_out")

    def test_parent_rejects_bad_probe_output_and_caps_utf8_bytes(self):
        active = subprocess.CompletedProcess(
            [], 0, b'{"address":"0xtarget","pid":42,"size":[800,600]}', b"",
        )
        bad = subprocess.CompletedProcess([], 0, b'{"status":"available","rows":[123]}', b"")
        with patch("src.tools.computer_accessibility.subprocess.run", side_effect=[active, bad]):
            self.assertEqual(observe_active_window("0xtarget", (800, 600))["status"], "unavailable")

        rows = [{"role": "entry", "name": "\u00e9" * 100,
                 "screen": None, "window": None}] * 40
        good = subprocess.CompletedProcess(
            [], 0, json.dumps({"status": "available", "rows": rows}).encode(), b"",
        )
        with patch("src.tools.computer_accessibility.subprocess.run", side_effect=[active, good]):
            result = observe_active_window("0xtarget", (800, 600))
        self.assertEqual(result["status"], "available")
        self.assertLessEqual(len(result["labels"].encode("utf-8")), 4000)

    def test_maps_consistent_window_relative_or_screen_rectangles(self):
        window = {"at": [81, 31], "size": [620, 360], "monitor": 0}
        monitor = {"id": 0, "x": 0, "y": 0, "width": 1920,
                   "height": 1080, "scale": 1}
        frame = {"screen": [0, 0, 620, 360], "window": [0, 0, 620, 360]}
        row = {"screen": [50, 55, 150, 90], "window": [50, 55, 150, 90]}
        self.assertEqual(_map_rect(row, frame, window, monitor, (1920, 1080)),
                         (131, 86, 150, 90))
        frame["screen"][:2] = [81, 31]
        row["screen"][:2] = [131, 86]
        self.assertEqual(_map_rect(row, frame, window, monitor, (1920, 1080)),
                         (131, 86, 150, 90))
        row["screen"][0] = 400
        self.assertIsNone(_map_rect(row, frame, window, monitor, (1920, 1080)))

        scaled_window = {"at": [2020, 30], "size": [620, 360], "monitor": 2}
        scaled_monitor = {"id": 2, "x": 1920, "y": 0, "width": 3840,
                          "height": 2160, "scale": 2}
        relative_row = {"screen": [50, 55, 150, 90], "window": [50, 55, 150, 90]}
        relative_frame = {"screen": [0, 0, 620, 360], "window": [0, 0, 620, 360]}
        self.assertEqual(
            _map_rect(relative_row, relative_frame, scaled_window, scaled_monitor, (3840, 2160)),
            (300, 170, 300, 180),
        )

    def test_observer_emits_only_validated_screenshot_boxes(self):
        active = {"address": "0xtarget", "pid": 42, "monitor": 0,
                  "at": [81, 31], "size": [620, 360]}
        data = {
            "status": "available",
            "frame": {"screen": [0, 0, 620, 360], "window": [0, 0, 620, 360]},
            "rows": [
                {"role": "button", "name": "Open", "screen": [50, 55, 150, 90],
                 "window": [50, 55, 150, 90]},
                {"role": "button", "name": "Uncertain", "screen": [300, 55, 150, 90],
                 "window": [310, 55, 150, 90]},
            ],
        }
        monitor = {"id": 0, "x": 0, "y": 0, "width": 800, "height": 600, "scale": 1}
        replies = [active, data, [monitor]]
        with patch("src.tools.computer_accessibility.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps(item).encode(), b"") for item in replies
        ]):
            result = observe_active_window("0xtarget", (800, 600))
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["coordinates"], 1)
        self.assertIn('button: "Open" [x=131, y=86, w=150, h=90]', result["labels"])
        self.assertIn('button: "Uncertain"', result["labels"])
        self.assertNotIn('Uncertain" [', result["labels"])


if __name__ == "__main__":
    unittest.main()
