import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from src.tools.browser_tools import BrowserSession, FirecrawlHTTPError, _request
from src.tools.file_tools import read_file, search_files, write_file
from src.tools.git_tools import git_diff, git_status
from src.tools.registry import execute_tool, tool_schemas
from src.tools.terminal_tool import TerminalJobManager, run_terminal
from src.tools.web_tools import _firecrawl_extract, _tavily_api_key, web_extract, web_search


class ToolTests(unittest.TestCase):
    def test_read_file_rejects_paths_outside_project(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "inside the project"):
                read_file("../outside.txt", Path(folder))

    def test_read_file_rejects_firecrawl_key(self):
        with self.assertRaisesRegex(ValueError, "Firecrawl key file"):
            read_file(".mini-hermes/firecrawl.env", Path.home())

    def test_read_file_rejects_private_paths_and_aliases(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env").write_text("secret")
            (root / "tavily.env").write_text("TAVILY_API_KEY=fake-secret")
            (root / "alias").symlink_to(root / ".env")
            for path in (".env", "alias", "tavily.env"):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "excluded from reading"):
                    read_file(path, root)

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
            (root / "tavily.env").write_text("search_marker\n")
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
            with self.assertRaisesRegex(ValueError, "excluded from search"):
                search_files("search_marker", root, path="tavily.env")
            with self.assertRaisesRegex(ValueError, "Search failed"):
                search_files("(", root)

    def test_git_status_and_diff_are_read_only_and_scoped_to_project(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            project = repo / "project"
            project.mkdir()
            (repo / "outside.txt").write_text("outside baseline\n")
            (project / "tracked.txt").write_text("before\n")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
            (project / "tracked.txt").write_text("unstaged change\n")
            (project / "staged.txt").write_text("staged change\n")
            subprocess.run(["git", "-C", str(repo), "add", "project/staged.txt"], check=True)
            (project / "new.txt").write_text("untracked\n")

            before = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                check=True, capture_output=True,
            ).stdout
            status = git_status(project)
            preview = git_diff(project)
            after = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                check=True, capture_output=True,
            ).stdout

            self.assertIn("Staged changes", status)
            self.assertIn("Unstaged changes", status)
            self.assertIn("Untracked paths", status)
            self.assertNotIn("outside.txt", status)
            self.assertIn("index compared with HEAD", preview)
            self.assertIn("worktree compared with index", preview)
            self.assertIn('"project/new.txt"', preview)
            self.assertNotIn("outside.txt", preview)
            self.assertEqual(before, after)

    def test_git_tools_reject_non_repository_and_bound_large_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaisesRegex(ValueError, "not inside a Git repository"):
                git_status(root)

            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "large.txt").write_text("old\n")
            subprocess.run(["git", "-C", str(root), "add", "large.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "commit", "-qm", "baseline"], check=True)
            (root / "large.txt").write_text("changed\n" * 5_000)
            preview = git_diff(root)
            self.assertIn("Git output truncated at 20,000 characters", preview)

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

    def test_terminal_timeout_preserves_captured_output(self):
        root = Path("/home/puneet/Documents/mini-hermes")
        timeout = subprocess.TimeoutExpired("command", 30, output=b"still working", stderr=b"warning")
        with patch("src.tools.terminal_tool.shutil.which", return_value="/usr/bin/bwrap"):
            with patch("src.tools.terminal_tool.subprocess.run", side_effect=timeout):
                result = run_terminal("slow-command", root, Mock(return_value=True))
        self.assertIn("timed out after 30 seconds", result)
        self.assertIn("stdout:\nstill working", result)
        self.assertIn("stderr:\nwarning", result)

        timeout = subprocess.TimeoutExpired("command", 30, output=b"x" * 25_000)
        with patch("src.tools.terminal_tool.shutil.which", return_value="/usr/bin/bwrap"):
            with patch("src.tools.terminal_tool.subprocess.run", side_effect=timeout):
                result = run_terminal("slow-command", root, Mock(return_value=True))
        self.assertIn("[output truncated at 20,000 characters]", result)
        self.assertLess(len(result), 20_200)

    def test_terminal_jobs_support_incremental_output_input_stop_and_session_isolation(self):
        import sys

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = TerminalJobManager(root)
            other_session = TerminalJobManager(root)
            command_builder = lambda code, _root, _bwrap: [sys.executable, "-c", code]
            with patch("src.tools.terminal_tool.shutil.which", return_value="/fake/bwrap"), \
                 patch("src.tools.terminal_tool._sandbox_command", side_effect=command_builder):
                job_id = manager.start(
                    "print('READY', flush=True); value = input(); print('GOT=' + value, flush=True)",
                    Mock(return_value=True),
                )
                initial = manager.read(job_id, wait_seconds=2)
                self.assertIn("READY", initial)
                manager.send_input(job_id, "hello", Mock(return_value=True))
                final = manager.read(job_id, wait_seconds=2)
                self.assertIn("GOT=hello", final)
                with self.assertRaisesRegex(ValueError, "No terminal job"):
                    other_session.read(job_id, 0)

                long_job = manager.start("import time; time.sleep(30)", Mock(return_value=True))
                self.assertIn("Stopped terminal job", manager.stop(long_job))
            manager.close()
            other_session.close()

    def test_terminal_job_denial_and_input_bounds_prevent_process_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = TerminalJobManager(Path(folder))
            with patch("src.tools.terminal_tool.shutil.which", return_value="/fake/bwrap"):
                with patch("src.tools.terminal_tool.subprocess.Popen") as popen:
                    self.assertEqual(manager.start("do not run", Mock(return_value=False)), "Command cancelled by the user.")
                    popen.assert_not_called()
            with self.assertRaisesRegex(ValueError, "4,096"):
                manager.send_input("a" * 12, "x" * 5000, Mock(return_value=True))
            manager.close()

    def test_cancelling_a_terminal_output_wait_stops_that_job(self):
        import sys
        from threading import Event

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = TerminalJobManager(root)
            with patch("src.tools.terminal_tool.shutil.which", return_value="/fake/bwrap"), \
                 patch("src.tools.terminal_tool._sandbox_command",
                       side_effect=lambda code, _root, _bwrap: [sys.executable, "-c", code]):
                job_id = manager.start("import time; time.sleep(30)", Mock(return_value=True))
                cancel = Event()
                cancel.set()
                with self.assertRaises(InterruptedError):
                    execute_tool(
                        "terminal_read", {"job_id": job_id, "wait_seconds": 2},
                        root, Mock(), Mock(), terminal_jobs=manager, cancel_event=cancel,
                    )
                self.assertIn("already exited", manager.stop(job_id))
            manager.close()

    def test_web_search_uses_tavily_and_formats_sources(self):
        payload = {"results": [
            {"title": " Example  Page ", "url": "https://example.com/", "content": "Useful\n result"},
            {"title": "Other", "url": "https://other.example/", "content": "More text"},
        ]}
        with patch("src.tools.web_tools._tavily_api_key", return_value="fake-key"):
            with patch("src.tools.web_tools.build_opener") as opener:
                opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
                result = execute_tool(
                    "web_search", {"query": " example ", "max_results": 1},
                    Path.cwd(), Mock(), Mock(),
                )
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.tavily.com/search")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-key")
        self.assertEqual(json.loads(request.data), {
            "query": "example", "max_results": 1, "search_depth": "basic",
            "include_answer": False, "include_raw_content": False,
        })
        self.assertEqual(result, "1. Example Page\nhttps://example.com/\nUseful result")

    def test_web_search_reports_configuration_and_api_failures(self):
        with patch("src.tools.web_tools._tavily_api_key", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "Tavily API key missing"):
                web_search("example")
        with patch("src.tools.web_tools._tavily_api_key", return_value="fake-key"):
            with patch("src.tools.web_tools.build_opener") as opener:
                unauthorized = HTTPError("https://api.tavily.com/search", 401, "Unauthorized", None, None)
                rate_limited = HTTPError("https://api.tavily.com/search", 429, "Rate Limited", None, None)
                opener.return_value.open.side_effect = unauthorized
                with self.assertRaisesRegex(RuntimeError, "rejected the API key"):
                    web_search("example")
                opener.return_value.open.side_effect = rate_limited
                with self.assertRaisesRegex(RuntimeError, "rate limited"):
                    web_search("example")
                unauthorized.close()
                rate_limited.close()

    def test_web_search_distinguishes_empty_results_from_invalid_data(self):
        with patch("src.tools.web_tools._tavily_api_key", return_value="fake-key"):
            with patch("src.tools.web_tools.build_opener") as opener:
                response = opener.return_value.open.return_value.__enter__.return_value
                response.read.return_value = b'{"results": []}'
                self.assertIn("No web results found", web_search("example"))
                response.read.return_value = b'{"results": [{"title": "Bad", "url": "javascript:alert(1)", "content": "x"}]}'
                with self.assertRaisesRegex(RuntimeError, "invalid result URL"):
                    web_search("example")

    def test_tavily_key_reads_private_file_and_environment_override(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / ".mini-hermes"
            config.mkdir()
            (config / "tavily.env").write_text("TAVILY_API_KEY='file-key'\n")
            with patch("src.security.secrets.Path.home", return_value=Path(folder)):
                with patch.dict(os.environ, {"TAVILY_API_KEY": ""}):
                    self.assertEqual(_tavily_api_key(), "file-key")
                with patch.dict(os.environ, {"TAVILY_API_KEY": "env-key"}):
                    self.assertEqual(_tavily_api_key(), "env-key")

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

    def test_catalog_exposes_browser_navigation_tools(self):
        self.assertEqual([tool["name"] for tool in tool_schemas()], [
            "terminal", "terminal_read", "terminal_input", "terminal_stop",
            "read_file", "search_files", "git_status", "git_diff", "delegate_read_only",
            "write_file", "edit_file", "undo_file_change",
            "web_search", "web_extract",
            "browser_open", "browser_snapshot", "browser_click", "browser_fill",
            "browser_press", "browser_scroll", "browser_wait", "browser_back",
        ])

    def test_browser_keeps_one_session_across_open_and_click_then_closes(self):
        scrape_id = "550e8400-e29b-41d4-a716-446655440000"
        replies = [
            {"success": True, "data": {"metadata": {"scrapeId": scrape_id}}},
            {"success": True, "stdout": "https://example.com/\n- link [ref=e2]"},
            {"success": True, "stdout": "https://www.iana.org/\n- heading IANA"},
            {"success": True},
        ]
        with patch("src.tools.browser_tools._check_public_url"):
            with patch("src.tools.browser_tools._firecrawl_api_key", return_value="fake-key"):
                with patch("src.tools.browser_tools._request", side_effect=replies) as request:
                    browser = BrowserSession()
                    self.assertIn("ref=e2", browser.open("https://example.com"))
                    self.assertIn("IANA", browser.click("@e2"))
                    browser.close()
        self.assertEqual(request.call_count, 4)
        self.assertEqual(request.call_args_list[2].kwargs["body"]["code"],
                         "agent-browser click @e2 && agent-browser get url && agent-browser snapshot")
        self.assertEqual(request.call_args_list[3].kwargs["method"], "DELETE")
        self.assertTrue(request.call_args_list[3].kwargs["retry_safe"])

    def test_browser_navigation_commands_validate_inputs(self):
        browser = BrowserSession()
        with patch.object(browser, "_run", return_value="snapshot") as run:
            browser.press("Enter")
            browser.scroll("down", 500)
            browser.wait("text", "Ready; echo nope")
            browser.back()
            self.assertEqual([item.args[0] for item in run.call_args_list], [
                "agent-browser press Enter",
                "agent-browser scroll down 500",
                "agent-browser wait --text 'Ready; echo nope' --timeout 10000",
                "agent-browser back",
            ])
            with self.assertRaises(ValueError):
                browser.press("Control+O")
            with self.assertRaises(ValueError):
                browser.scroll("down", 0)
            with self.assertRaises(ValueError):
                browser.wait("ref", "not-a-ref")

    def test_browser_retries_snapshots_but_not_actions(self):
        failure = HTTPError("https://api.firecrawl.dev", 502, "Bad Gateway", None, None)
        self.addCleanup(failure.close)
        with patch("src.tools.browser_tools.time.sleep"):
            with patch("src.tools.browser_tools.build_opener") as opener:
                opener.return_value.open.side_effect = [
                    failure, io.BytesIO(b'{"success": true, "stdout": "snapshot"}'),
                ]
                self.assertEqual(_request("/scrape/id/interact", "fake", retry_safe=True)["stdout"],
                                 "snapshot")
                self.assertEqual(opener.return_value.open.call_count, 2)
            with patch("src.tools.browser_tools.build_opener") as opener:
                opener.return_value.open.side_effect = failure
                with self.assertRaisesRegex(RuntimeError, "may have run"):
                    _request("/scrape/id/interact", "fake")
                self.assertEqual(opener.return_value.open.call_count, 1)

    def test_firecrawl_http_error_keeps_only_safe_request_metadata(self):
        secret = "fc-1234567890abcdef"
        error = HTTPError(
            "https://api.firecrawl.dev/v2/interact", 502, "Bad Gateway",
            {"X-Request-ID": "req-123abc"},
            io.BytesIO(json.dumps({"error": f"upstream failed Bearer hidden {secret}"}).encode()),
        )
        self.addCleanup(error.close)
        with patch("src.tools.browser_tools.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(FirecrawlHTTPError) as raised:
                _request("/scrape/id/interact", secret)
        failure = raised.exception
        self.assertEqual(failure.status_code, 502)
        self.assertEqual(failure.request_id, "req-123abc")
        self.assertEqual(failure.detail, "upstream failed [redacted] [redacted]")
        self.assertIn("Request ID: req-123abc", str(failure))
        self.assertNotIn(secret, str(failure))
        self.assertNotIn("hidden", str(failure))

    def test_browser_close_retries_transient_error_and_preserves_failed_session_id(self):
        scrape_id = "550e8400-e29b-41d4-a716-446655440000"
        browser = BrowserSession()
        browser.scrape_id, browser.key = scrape_id, "fake-key"
        with patch("src.tools.browser_tools._request", side_effect=RuntimeError("HTTP 502")):
            with self.assertRaisesRegex(RuntimeError, scrape_id):
                browser.close()
        self.assertEqual(browser.scrape_id, scrape_id)
        failure = HTTPError("https://api.firecrawl.dev", 502, "Bad Gateway", None, None)
        self.addCleanup(failure.close)
        with patch("src.tools.browser_tools.time.sleep"):
            with patch("src.tools.browser_tools.build_opener") as opener:
                opener.return_value.open.side_effect = [
                    failure, io.BytesIO(b'{"success": true}'),
                ]
                browser.close()
                self.assertEqual(opener.return_value.open.call_count, 2)
        self.assertIsNone(browser.scrape_id)

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
