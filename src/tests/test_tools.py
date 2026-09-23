import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.tools.file_tools import read_file, write_file
from src.tools.registry import execute_tool, tool_schemas
from src.tools.terminal_tool import run_terminal
from src.tools.web_tools import web_search


class ToolTests(unittest.TestCase):
    def test_read_file_rejects_paths_outside_project(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "inside the project"):
                read_file("../outside.txt", Path(folder))

    def test_write_file_creates_utf8_text_after_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            confirm = Mock(return_value=True)
            result = write_file("notes.txt", "hello, नमस्ते\n", root, confirm)
            confirm.assert_called_once_with("notes.txt", "hello, नमस्ते\n", False)
            self.assertEqual((root / "notes.txt").read_text(encoding="utf-8"), "hello, नमस्ते\n")
            self.assertEqual(result, "Wrote 14 characters to 'notes.txt'.")
            self.assertEqual(list(root.iterdir()), [root / "notes.txt"])

    def test_write_file_overwrites_only_after_approval_and_keeps_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "notes.txt"
            target.write_text("old", encoding="utf-8")
            target.chmod(0o640)
            confirm = Mock(return_value=True)
            write_file("notes.txt", "new", Path(folder), confirm)
            confirm.assert_called_once_with("notes.txt", "new", True)
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_write_file_cancel_leaves_existing_content_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "notes.txt"
            target.write_text("keep me", encoding="utf-8")
            result = write_file("notes.txt", "replace me", Path(folder), Mock(return_value=False))
            self.assertIn("cancelled", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "keep me")

    def test_failed_atomic_replace_preserves_original_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "notes.txt"
            target.write_text("keep me", encoding="utf-8")
            with patch("src.tools.file_tools.os.replace", side_effect=OSError("disk error")):
                with self.assertRaisesRegex(OSError, "disk error"):
                    write_file("notes.txt", "new contents", root, Mock(return_value=True))
            self.assertEqual(target.read_text(encoding="utf-8"), "keep me")
            self.assertEqual(list(root.iterdir()), [target])

    def test_write_file_rejects_traversal_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            root = Path(folder)
            (root / "outside-link").symlink_to(Path(outside) / "target.txt")
            confirm = Mock(return_value=True)
            for path in ("../outside.txt", "outside-link"):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "inside the project"):
                    write_file(path, "nope", root, confirm)
            confirm.assert_not_called()

    def test_write_file_requires_existing_parent_and_caps_content(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            confirm = Mock(return_value=True)
            with self.assertRaisesRegex(ValueError, "Parent folder"):
                write_file("missing/file.txt", "text", root, confirm)
            with self.assertRaisesRegex(ValueError, "100,000"):
                write_file("large.txt", "x" * 100_001, root, confirm)
            confirm.assert_not_called()

    def test_terminal_requires_confirmation(self):
        confirm = Mock(return_value=False)
        result = run_terminal("echo should-not-run", Path.cwd(), confirm)
        self.assertEqual(result, "Command cancelled by the user.")
        confirm.assert_called_once_with("echo should-not-run")

    def test_terminal_uses_project_write_sandbox_and_pid_isolation(self):
        root = Path("/home/puneet/Documents/mini-hermes")
        with patch("src.tools.terminal_tool.shutil.which", return_value="/usr/bin/bwrap"):
            with patch("src.tools.terminal_tool.subprocess.run") as run:
                run.return_value = Mock(returncode=0, stdout="ok", stderr="")
                result = run_terminal("echo ok", root, Mock(return_value=True))
        command = run.call_args.args[0]
        self.assertEqual(result, "Exit code: 0\nok")
        self.assertIn("--unshare-pid", command)
        self.assertIn("--bind", command)
        self.assertEqual(command[-3:], ["/bin/bash", "-lc", "echo ok"])

    def test_web_search_parses_result_title_url_and_snippet(self):
        page = b'''<a class="result__a" href="https://example.com/">Example</a>
                   <a class="result__snippet">Useful result</a>'''
        with patch("src.tools.web_tools.urlopen") as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = page
            result = web_search("example", 1)
        self.assertIn("Example", result)
        self.assertIn("https://example.com/", result)
        self.assertIn("Useful result", result)

    def test_catalog_exposes_four_function_tools(self):
        self.assertEqual([tool["name"] for tool in tool_schemas()], [
            "terminal", "read_file", "write_file", "web_search"
        ])

    def test_registry_dispatches_write_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            confirm = Mock(return_value=True)
            result = execute_tool(
                "write_file", {"path": "created.txt", "content": "saved"},
                root, Mock(), confirm,
            )
            self.assertEqual(result, "Wrote 5 characters to 'created.txt'.")
            self.assertEqual((root / "created.txt").read_text(encoding="utf-8"), "saved")
            confirm.assert_called_once_with("created.txt", "saved", False)


if __name__ == "__main__":
    unittest.main()
