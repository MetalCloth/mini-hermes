"""Read-only, project-scoped Git status and diff previews."""

import json
import os
import subprocess
from pathlib import Path


MAX_GIT_OUTPUT_CHARS = 20_000


def _git(project_root: Path, *args: str) -> str:
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ValueError("The active project folder is unavailable.")
    try:
        result = subprocess.run(
            ["git", "--no-pager", "--no-optional-locks", "-C", str(root), *args],
            cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=False, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git is not installed or is unavailable on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Git inspection timed out after 5 seconds.") from exc
    if result.returncode:
        detail = " ".join(result.stderr.split())[:500]
        if "not a git repository" in detail.casefold():
            raise ValueError("The active project folder is not inside a Git repository.")
        raise RuntimeError(f"Git inspection failed: {detail or 'unknown Git error'}")
    return result.stdout


def _truncate(text: str) -> str:
    if len(text) <= MAX_GIT_OUTPUT_CHARS:
        return text
    return text[:MAX_GIT_OUTPUT_CHARS] + f"\n[Git output truncated at {MAX_GIT_OUTPUT_CHARS:,} characters.]"


def _status(project_root: Path) -> tuple[list[str], list[str], list[str]]:
    raw = _git(
        project_root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".",
    )
    records = raw.split("\0")
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    skip_next_path = False
    for record in records:
        if not record:
            continue
        if skip_next_path:
            skip_next_path = False
            continue
        code, path = record[:2], record[3:]
        if "R" in code or "C" in code:
            skip_next_path = True
        display = f"{code} {json.dumps(path, ensure_ascii=False)}"
        if code == "??":
            untracked.append(path)
        else:
            if code[0] != " ":
                staged.append(display)
            if code[1] != " ":
                unstaged.append(display)
    return staged, unstaged, untracked


def git_status(project_root: Path) -> str:
    """Summarize staged, unstaged, and untracked paths under the selected project root."""
    staged, unstaged, untracked = _status(project_root)
    if not staged and not unstaged and not untracked:
        return "Working tree clean."
    sections = []
    for title, entries, quote_paths in (
        ("Staged changes (index vs HEAD)", staged, False),
        ("Unstaged changes (worktree vs index)", unstaged, False),
        ("Untracked paths (not included in Git diff)", untracked, True),
    ):
        if entries:
            lines = [json.dumps(entry, ensure_ascii=False) for entry in entries] if quote_paths else entries
            sections.append(f"{title}:\n" + "\n".join(lines))
    return _truncate("\n\n".join(sections))


def git_diff(project_root: Path) -> str:
    """Show bounded staged and unstaged diffs plus untracked paths under the project root."""
    staged, unstaged, untracked = _status(project_root)
    sections = []
    if staged:
        diff = _git(project_root, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--", ".")
        sections.append("Staged diff (index compared with HEAD):\n" + (diff or "No patch available."))
    if unstaged:
        diff = _git(project_root, "diff", "--no-ext-diff", "--no-textconv", "--", ".")
        sections.append("Unstaged diff (worktree compared with index):\n" + (diff or "No patch available."))
    if untracked:
        sections.append(
            "Untracked paths (Git does not include these in diff output):\n"
            + "\n".join(json.dumps(path, ensure_ascii=False) for path in untracked)
        )
    return _truncate("\n\n".join(sections) if sections else "Working tree clean.")
