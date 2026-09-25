import difflib
import hashlib
import os
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


_PRIVATE_PARTS = (".git", ".venv", "node_modules", "__pycache__", ".codex", ".mini-hermes", ".ssh", "firecrawl.env", "tavily.env")
MAX_UNDO_HISTORY = 20
MAX_UNDO_SNAPSHOT_BYTES = 1_000_000


@dataclass(frozen=True)
class FileChange:
    path: str
    previous_content: bytes | None
    previous_mode: int | None
    result_digest: bytes
    result_mode: int


def _remember_file_change(
    history: list[FileChange] | None,
    path: str,
    previous_content: bytes | None,
    previous_mode: int | None,
    result_content: bytes,
    target: Path,
) -> None:
    if history is None or previous_content == result_content:
        return
    history.append(FileChange(
        path, previous_content, previous_mode,
        hashlib.sha256(result_content).digest(), stat.S_IMODE(target.stat().st_mode),
    ))
    # ponytail: keep 20 in-memory snapshots per session; persist only if restart recovery is needed.
    del history[:-MAX_UNDO_HISTORY]


def _is_private_path(target: Path, root: Path) -> bool:
    return any(
        part in _PRIVATE_PARTS or part == ".env" or part.startswith(".env.")
        for part in target.relative_to(root).parts
    )


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
    if _is_private_path(target, root):
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
    for ignored in (*_PRIVATE_PARTS, ".env", ".env.*"):
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
    if _is_private_path(target, root):
        raise ValueError("That project path is excluded from reading.")
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
    undo_history: list[FileChange] | None = None,
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
    if undo_history is not None and exists and target.stat().st_size > MAX_UNDO_SNAPSHOT_BYTES:
        raise ValueError("The existing file is too large for a safe undo snapshot (1 MB maximum).")
    previous_content = target.read_bytes() if exists else None
    relative_path = target.relative_to(root).as_posix()
    if not confirm(relative_path, content, exists):
        return f"Write cancelled; '{relative_path}' was not changed."

    if exists:
        if target.is_symlink() or not target.is_file() or target.read_bytes() != previous_content:
            raise RuntimeError(f"'{relative_path}' changed while approval was pending. Read it again before writing.")
        if stat.S_IMODE(target.stat().st_mode) != mode:
            raise RuntimeError(f"Permissions for '{relative_path}' changed while approval was pending. Read it again before writing.")
    elif target.exists():
        raise RuntimeError(f"'{relative_path}' appeared while approval was pending. Read it again before writing.")

    _atomic_write_text(target, content, mode)
    _remember_file_change(
        undo_history, relative_path, previous_content, mode, content.encode("utf-8"), target,
    )
    return f"Wrote {len(content)} characters to '{relative_path}'."


def edit_file(
    path: str,
    old_text: str,
    new_text: str,
    project_root: Path,
    confirm: Callable[[str, str], bool],
    undo_history: list[FileChange] | None = None,
) -> str:
    """Replace one exact, unique text match after the user approves its diff."""
    if not isinstance(path, str) or not path.strip() or "\0" in path or Path(path).is_absolute():
        raise ValueError("Give a non-empty project-relative file path, such as 'src/app.py'.")
    if not isinstance(old_text, str) or not old_text:
        raise ValueError("Give the exact non-empty text to replace.")
    if not isinstance(new_text, str):
        raise ValueError("Replacement text must be text.")

    root = project_root.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError("File path must stay inside the project folder.")
    if _is_private_path(target, root):
        raise ValueError("That project path is excluded from editing.")
    if not target.is_file():
        raise ValueError(f"No file found at '{path}'. Give a path to an existing project file.")
    if undo_history is not None and target.stat().st_size > MAX_UNDO_SNAPSHOT_BYTES:
        raise ValueError("The existing file is too large for a safe undo snapshot (1 MB maximum).")
    try:
        original_bytes = target.read_bytes()
        raw_original = original_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"'{path}' is not a UTF-8 text file.") from exc
    original_mode = stat.S_IMODE(target.stat().st_mode)
    crlf_count = raw_original.count("\r\n")
    other_endings = raw_original.replace("\r\n", "")
    lf_count, cr_count = other_endings.count("\n"), other_endings.count("\r")
    if sum(count > 0 for count in (crlf_count, lf_count, cr_count)) > 1:
        raise ValueError(f"'{path}' has mixed line endings. Normalize them before using edit_file.")
    line_ending = "\r\n" if crlf_count else "\r" if cr_count else "\n"
    original = raw_original.replace("\r\n", "\n").replace("\r", "\n")
    old_text = old_text.replace("\r\n", "\n").replace("\r", "\n")
    new_text = new_text.replace("\r\n", "\n").replace("\r", "\n")

    first_match = original.find(old_text)
    if first_match < 0:
        raise ValueError("The exact text was not found. Read the file again and retry with current text.")
    if original.find(old_text, first_match + 1) >= 0:
        raise ValueError("The exact text occurs more than once. Include more surrounding text to identify one match.")

    updated = original[:first_match] + new_text + original[first_match + len(old_text):]
    if len(updated) > 100_000:
        raise ValueError("Edited file would exceed 100,000 characters. Split it into smaller edits.")
    if updated == original:
        return f"No change needed in '{target.relative_to(root).as_posix()}'."

    relative_path = target.relative_to(root).as_posix()
    preview = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), updated.splitlines(keepends=True),
        fromfile=f"a/{relative_path}", tofile=f"b/{relative_path}",
    ))
    if len(preview) > 20_000:
        raise ValueError("This edit is too large to review safely. Split it into smaller edits.")
    if not confirm(relative_path, preview):
        return f"Edit cancelled; '{relative_path}' was not changed."

    # Recheck after approval so an intervening change cannot be silently overwritten.
    if target.read_bytes() != original_bytes:
        raise RuntimeError(f"'{relative_path}' changed after approval. Read it again before editing.")
    if stat.S_IMODE(target.stat().st_mode) != original_mode:
        raise RuntimeError(f"Permissions for '{relative_path}' changed after approval. Read it again before editing.")
    stored_text = updated.replace("\n", line_ending)
    result_content = stored_text.encode("utf-8")
    _atomic_write_bytes(target, result_content, original_mode)
    _remember_file_change(
        undo_history, relative_path, original_bytes, original_mode, result_content, target,
    )
    return f"Edited '{relative_path}' by replacing one exact match."


def undo_file_change(
    project_root: Path,
    history: list[FileChange],
    confirm: Callable[[str, str, bool], bool],
) -> str:
    """Restore Oryn's latest recorded file change if the file is still untouched."""
    if not history:
        return "There is no recent Oryn file change to undo in this chat session."

    change = history[-1]
    root = project_root.resolve()
    candidate = root / change.path
    target = candidate.resolve()
    if not target.is_relative_to(root) or target != candidate:
        raise RuntimeError("The changed file path no longer resolves safely inside the project.")
    if candidate.is_symlink() or not target.is_file():
        raise RuntimeError(f"'{change.path}' is missing or no longer a regular file; nothing was undone.")

    current_content = target.read_bytes()
    current_mode = stat.S_IMODE(target.stat().st_mode)
    if hashlib.sha256(current_content).digest() != change.result_digest or current_mode != change.result_mode:
        raise RuntimeError(f"'{change.path}' changed after Oryn edited it; leaving it untouched.")

    current_text = current_content.decode("utf-8", errors="replace")
    previous_text = (change.previous_content or b"").decode("utf-8", errors="replace")
    preview = "".join(difflib.unified_diff(
        current_text.splitlines(keepends=True), previous_text.splitlines(keepends=True),
        fromfile=f"a/{change.path}", tofile=f"b/{change.path}",
    ))
    if len(preview) > 20_000:
        preview = preview[:19_800] + "\n[Undo preview truncated; the full file will be restored.]\n"
    removes_created_file = change.previous_content is None
    if not confirm(change.path, preview, removes_created_file):
        return f"Undo cancelled; '{change.path}' was not changed."

    # Recheck after approval so an intervening edit is never overwritten.
    if target.is_symlink() or target.read_bytes() != current_content:
        raise RuntimeError(f"'{change.path}' changed while undo approval was pending; leaving it untouched.")
    if stat.S_IMODE(target.stat().st_mode) != current_mode:
        raise RuntimeError(f"Permissions for '{change.path}' changed while approval was pending; leaving it untouched.")
    if change.previous_content is None:
        target.unlink()
    else:
        _atomic_write_bytes(target, change.previous_content, change.previous_mode)
    history.pop()
    return f"Undid Oryn's most recent file change to '{change.path}'."


def _atomic_write_text(target: Path, content: str, mode: int | None) -> None:
    _atomic_write_bytes(target, content.encode("utf-8"), mode)


def _atomic_write_bytes(target: Path, content: bytes, mode: int | None) -> None:
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent,
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
