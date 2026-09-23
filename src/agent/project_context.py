"""Load the project's root-level AGENTS.md instructions."""

from pathlib import Path


MAX_PROJECT_INSTRUCTIONS_CHARS = 20_000


def load_project_instructions(project_root: Path) -> str | None:
    root = Path(project_root).resolve()
    try:
        path = (root / "AGENTS.md").resolve(strict=True)
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Could not resolve project instructions under {root}: {exc}") from exc
    if not path.is_relative_to(root):
        raise ValueError("AGENTS.md must resolve inside the project folder.")
    if not path.is_file():
        return None

    try:
        with path.open(encoding="utf-8") as file:
            content = file.read(MAX_PROJECT_INSTRUCTIONS_CHARS + 1)
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"Could not read project instructions from {path}: {exc}") from exc

    if len(content) > MAX_PROJECT_INSTRUCTIONS_CHARS:
        marker = "\n[AGENTS.md truncated at 20,000 characters.]"
        content = content[:MAX_PROJECT_INSTRUCTIONS_CHARS - len(marker)] + marker
    return content if content.strip() else None
