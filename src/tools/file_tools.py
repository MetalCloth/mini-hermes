import os
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path


def search_files(
    pattern: str,
    project_root: Path,
    path: str = ".",
    include: str = "",
    exclude: str = "",
    literal: bool = False,
    case_sensitive: bool = True,
    max_results: int = 30,
) -> str:
    """Find matching lines in project text files with ripgrep."""
    if not isinstance(pattern, str) or not pattern or len(pattern) > 500 or "\0" in pattern:
        raise ValueError("Give a non-empty search pattern of at most 500 characters.")
    if any(not isinstance(value, str) or "\0" in value for value in (path, include, exclude)):
        raise ValueError("Search path and filename filters must be text without NUL characters.")
    if include.startswith("!") or exclude.startswith("!"):
        raise ValueError("Give filename globs without a leading '!'.")
    if type(literal) is not bool or type(case_sensitive) is not bool:
        raise ValueError("literal and case_sensitive must be true or false.")
    if type(max_results) is not int or not 1 <= max_results <= 50:
        raise ValueError("max_results must be between 1 and 50.")

    root = project_root.resolve()
    if Path(path or ".").is_absolute():
        raise ValueError("Search path must be relative to the project folder.")
    target = (root / (path or ".")).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Search path must stay inside the project folder.")
    if target == (Path.home() / ".mini-hermes" / "firecrawl.env").resolve():
        raise ValueError("The Firecrawl key file cannot be searched through the agent.")
    excluded = (".git", ".venv", "node_modules", "__pycache__", ".codex", ".mini-hermes", ".ssh", "firecrawl.env")
    if any(
        part in excluded or part == ".env" or part.startswith(".env.")
        for part in target.relative_to(root).parts
    ):
        raise ValueError("That project path is excluded from search.")
    if not target.is_file() and not target.is_dir():
        raise ValueError(f"No project file or folder found at '{path}'.")
    ripgrep = shutil.which("rg")
    if not ripgrep:
        raise RuntimeError("search_files needs ripgrep (rg) installed and on PATH.")

    command = [
        ripgrep, "--line-number", "--with-filename", "--no-heading", "--color", "never",
        "--max-columns", "300", "--max-columns-preview", "--max-filesize", "2M",
    ]
    if literal:
        command.append("--fixed-strings")
    if not case_sensitive:
        command.append("--ignore-case")
    if include:
        command.extend(["--glob", include])
    if exclude:
        command.extend(["--glob", f"!{exclude}"])
    # Keep these paths out even if an include glob overrides ignore rules.
    for ignored in (*excluded, ".env", ".env.*"):
        command.extend(["--glob", f"!{ignored}"])
    command.extend(["--", pattern, target.relative_to(root).as_posix() or "."])

    matches: list[str] = []
    pending = b""
    output_chars = 0
    stopped = ""
    deadline = time.monotonic() + 10
    process = subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    try:
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        while not stopped:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError
            chunk = os.read(process.stdout.fileno(), 8192)
            at_end = not chunk
            pending += chunk if chunk else (b"\n" if pending else b"")
            while b"\n" in pending:
                raw_line, pending = pending.split(b"\n", 1)
                line = raw_line.decode("utf-8", errors="replace").removeprefix("./")
                if output_chars + len(line) + 1 > 12_000:
                    stopped = "12,000 output characters"
                    break
                matches.append(line)
                output_chars += len(line) + 1
                if len(matches) >= max_results:
                    stopped = f"{max_results} matches"
                    break
            if at_end:
                break

        if not stopped:
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if process.returncode not in (0, 1):
                detail = process.stderr.read(1000).decode("utf-8", errors="replace").strip()
                raise ValueError(f"Search failed: {detail or f'ripgrep exited {process.returncode}'}")
    except (TimeoutError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Search timed out after 10 seconds. Narrow the path or filename filters.") from exc
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()

    if not matches:
        return f"No matches shown; search stopped at {stopped}." if stopped else "No matches found."
    result = "\n".join(matches)
    if stopped:
        result += f"\n[Search stopped at {stopped}; narrow the pattern or filters for more.]"
    return result


def read_file(path: str, project_root: Path) -> str:
    root = project_root.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError("File path must stay inside the project folder.")
    if target == (Path.home() / ".mini-hermes" / "firecrawl.env").resolve():
        raise ValueError("The Firecrawl key file cannot be read through the agent.")
    if not target.is_file():
        raise ValueError(f"No file found at '{path}'. Give a path to a project file.")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"'{path}' is not a UTF-8 text file.") from exc
    if len(content) > 100_000:
        return content[:100_000] + "\n[truncated at 100,000 characters]"
    return content


def write_file(
    path: str,
    content: str,
    project_root: Path,
    confirm: Callable[[str, str, bool], bool],
) -> str:
    if not path.strip() or Path(path).is_absolute():
        raise ValueError("Give a non-empty project-relative file path, such as 'notes.txt'.")
    if not isinstance(content, str):
        raise ValueError("File content must be text.")
    if len(content) > 100_000:
        raise ValueError("Content exceeds 100,000 characters. Split it into smaller files or edits.")

    root = project_root.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError("File path must stay inside the project folder.")
    if not target.parent.is_dir():
        raise ValueError(f"Parent folder for '{path}' does not exist; choose an existing project folder.")
    exists = target.exists()
    if exists and not target.is_file():
        raise ValueError(f"'{path}' is not a regular file.")
    mode = stat.S_IMODE(target.stat().st_mode) if exists else None
    relative_path = target.relative_to(root).as_posix()
    if not confirm(relative_path, content, exists):
        return f"Write cancelled; '{relative_path}' was not changed."

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=target.parent,
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        if mode is not None:
            temp_path.chmod(mode)
        os.replace(temp_path, target)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
    return f"Wrote {len(content)} characters to '{relative_path}'."
