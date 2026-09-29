"""Discover and load local SKILL.md instructions without executing their contents."""

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


MAX_SKILL_BYTES = 16_000
MAX_SKILLS = 32
_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    path: Path
    root: Path


def _parse_skill(path: Path, root: Path) -> Skill:
    try:
        resolved = path.resolve(strict=True)
        base = root.resolve(strict=True)
        if not resolved.is_relative_to(base) or path.is_symlink() or not resolved.is_file():
            raise ValueError("skill file must be a regular file inside its skills folder")
        raw = resolved.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError("skill file is missing") from exc
    except OSError as exc:
        raise ValueError("skill file cannot be read") from exc
    if len(raw) > MAX_SKILL_BYTES:
        raise ValueError(f"skill file exceeds the {MAX_SKILL_BYTES:,}-byte limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("skill file must be UTF-8 text") from exc
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("skill file needs a frontmatter block with name and description")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("skill frontmatter is missing its closing '---'") from exc
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            raise ValueError("skill frontmatter must use simple 'key: value' fields")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip().strip("\"'")
        if key not in {"name", "description"} or key in fields or not value:
            raise ValueError("skill frontmatter may contain one non-empty name and description")
        fields[key] = value
    name, description = fields.get("name", ""), fields.get("description", "")
    instructions = "\n".join(lines[end + 1:]).strip()
    if not _SKILL_NAME.fullmatch(name):
        raise ValueError("skill name must be lowercase letters, numbers, and hyphens")
    if len(description) > 300:
        raise ValueError("skill description exceeds the 300-character limit")
    if not instructions:
        raise ValueError("skill instructions are empty")
    return Skill(name, description, instructions, resolved, base)


def discover_skills(
    project_root: Path, user_skill_root: Path | None = None,
) -> tuple[dict[str, Skill], list[tuple[str, str]]]:
    roots = [
        (Path(project_root).resolve() / ".agents" / "skills"),
        (user_skill_root or (Path.home() / ".agents" / "skills")),
    ]
    skills: dict[str, Skill] = {}
    problems: list[tuple[str, str]] = []
    seen = 0
    for base in roots:
        try:
            base_real = base.resolve(strict=True)
            if not base_real.is_dir():
                continue
            folders = sorted(base_real.iterdir(), key=lambda path: path.name)
        except FileNotFoundError:
            continue
        except OSError:
            problems.append((base.name, "skills folder cannot be read"))
            continue
        for folder in folders:
            if folder.is_symlink() or not folder.is_dir():
                continue
            seen += 1
            if seen > MAX_SKILLS:
                problems.append(("catalog", f"only the first {MAX_SKILLS} skill folders are considered"))
                return skills, problems
            name = folder.name[:64]
            try:
                skill = _parse_skill(folder / "SKILL.md", base_real)
                if skill.name in skills:
                    raise ValueError("duplicate skill name; project skills are checked first")
                skills[skill.name] = skill
            except ValueError as exc:
                problems.append((name, str(exc)))
    return skills, problems


def load_skill(
    project_root: Path, name: Any, user_skill_root: Path | None = None,
) -> Skill:
    if not isinstance(name, str) or not _SKILL_NAME.fullmatch(name):
        raise ValueError("Choose a skill by its exact name from the skill catalog.")
    skills, problems = discover_skills(project_root, user_skill_root)
    skill = skills.get(name)
    if skill is not None:
        return skill
    detail = next((message for problem_name, message in problems if problem_name == name), "skill not found")
    raise ValueError(f"Skill '{name}' is unavailable: {detail}.")


def skill_loader_tool(skills: dict[str, Skill]) -> dict[str, Any]:
    directory = [{"name": skill.name, "description": skill.description} for skill in skills.values()]
    description = (
        "Load one relevant local SKILL.md as task instructions. Choose by exact name only when "
        "its description matches the user's request. Loading returns the instruction text; it "
        "does not execute code or register tools. Apply only for this user turn. Catalog: "
        + json.dumps(directory, ensure_ascii=False, separators=(",", ":"))
    )
    return {
        "type": "function", "name": "load_skill", "description": description,
        "parameters": {
            "type": "object", "properties": {
                "name": {"type": "string", "enum": list(skills)},
            }, "required": ["name"], "additionalProperties": False,
        },
        "strict": True,
    }
