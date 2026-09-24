import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.tools.file_tools import read_file, search_files, write_file
from src.tools.registry import execute_tool, tool_schemas
from src.tools.terminal_tool import run_terminal
from src.tools.web_tools import _firecrawl_extract, web_extract, web_search


class ToolTests(unittest.TestCase):
    def test_read_file_rejects_paths_outside_project(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "inside the project"):
                read_file("../outside.txt", Path(folder))

    def test_read_file_rejects_firecrawl_key(self):
        with self.assertRaisesRegex(ValueError, "Firecrawl key file"):
            read_file(".mini-hermes/firecrawl.env", Path.home())

    def test_search_files_regex_filters_and_case_insensitive_literal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("def append_messages():\n    pass\n")
            (root / "src" / "other.py").write_text("def append_messages():\n")
            (root / "src" / "other.txt").write_text("def append_messages():\n")
            args = {
                "pattern": r"def append_\w+\(", "path": "src", "include": "*.py",
                "exclude": "other.py", "literal": False, "case_sensitive": True,
                "max_results": 10,
            }
            result = execute_tool("search_files", args, root, Mock(), Mock())
            self.assertIn("src/main.py:1:def append_messages():", result)
            self.assertNotIn("other.py", result)
            self.assertNotIn("other.txt", result)

            args.update(pattern="DEF APPEND_MESSAGES", literal=True, case_sensitive=False)
            self.assertIn("src/main.py:1:def append_messages():", execute_tool(
                "search_files", args, root, Mock(), Mock(),
            ))

    def test_search_files_excludes_private_paths_and_reports_bad_patterns(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "visible.txt").write_text("search_marker\n")
            (root / ".env").write_text("search_marker\n")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "hidden.txt").write_text("search_marker\n")
            result = search_files("search_marker", root, include="*", max_results=1)
            self.assertIn("visible.txt:1:search_marker", result)
            self.assertIn("Search stopped at 1 matches", result)
            self.assertNotIn(".env", result)
            self.assertNotIn("node_modules", result)
            with self.assertRaisesRegex(ValueError, "inside the project"):
                search_files("search_marker", root, path="../")
            with self.assertRaisesRegex(ValueError, "excluded from search"):
                search_files("search_marker", root, path=".env")
            with self.assertRaisesRegex(ValueError, "Search failed"):
                search_files("(", root)

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

    def test_web_extract_uses_firecrawl_only_when_key_is_configured(self):
        with patch("src.tools.web_tools._check_public_url"):
            with patch("src.tools.web_tools._firecrawl_api_key", return_value="fake-key"):
                with patch("src.tools.web_tools._firecrawl_extract", return_value="remote") as remote:
                    self.assertEqual(web_extract("https://example.com"), "remote")
                    remote.assert_called_once_with("https://example.com", "fake-key")
            with patch("src.tools.web_tools._firecrawl_api_key", return_value=""):
                with patch("src.tools.web_tools._local_extract", return_value="local") as local:
                    self.assertEqual(web_extract("https://example.com"), "local")
                    local.assert_called_once_with("https://example.com")

    def test_firecrawl_extract_reads_markdown_response(self):
        payload = {"success": True, "data": {
            "markdown": "# Example Domain\n\nUseful text",
            "metadata": {"title": "Example Domain"},
        }}
        with patch("src.tools.web_tools.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = (
                json.dumps(payload).encode()
            )
            result = _firecrawl_extract("https://example.com", "fake-key")
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.firecrawl.dev/v2/scrape")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-key")
        self.assertEqual(json.loads(request.data)["formats"], ["markdown"])
        self.assertIn("Title: Example Domain\nContent:\n# Example Domain", result)

    def test_catalog_exposes_six_function_tools(self):
        self.assertEqual([tool["name"] for tool in tool_schemas()], [
            "terminal", "read_file", "search_files", "write_file", "web_search", "web_extract"
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
