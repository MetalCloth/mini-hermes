import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path


def read_file(path: str, project_root: Path) -> str:
    root = project_root.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError("File path must stay inside the project folder.")
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
