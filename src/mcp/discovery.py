"""Oryn's small, known-good MCP server presets."""

import json
from dataclasses import dataclass
from pathlib import Path

from src.security.secrets import local_secret


SERVER_NAMES = ("context7", "github", "playwright")
_DEFAULT_ENABLED = set(SERVER_NAMES)


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: str
    args: tuple[str, ...]
    env: dict[str, str]
    disabled_reason: str = ""
    enabled: bool = True
    access: str = ""


def mcp_settings_path() -> Path:
    return Path.home() / ".mini-hermes" / "mcp-settings.json"


def load_enabled_servers(path: Path | None = None) -> set[str]:
    """Load user MCP switches, defaulting to the servers Oryn already starts."""
    try:
        data = json.loads((path or mcp_settings_path()).read_text(encoding="utf-8"))
        enabled = data["enabled_servers"]
        if not isinstance(enabled, list) or any(name not in SERVER_NAMES for name in enabled):
            return set(_DEFAULT_ENABLED)
        return set(enabled)
    except (OSError, ValueError, KeyError, TypeError):
        return set(_DEFAULT_ENABLED)


def save_enabled_servers(enabled: set[str], path: Path | None = None) -> None:
    """Atomically save non-secret server preferences with owner-only file access."""
    if not enabled <= set(SERVER_NAMES):
        raise ValueError("Unknown MCP server.")
    target = path or mcp_settings_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as settings:
            settings.write(json.dumps({"enabled_servers": sorted(enabled)}) + "\n")
            settings.flush()
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def server_configs(enabled_servers: set[str] | None = None) -> list[MCPServerConfig]:
    """Build fixed server commands; Oryn does not run arbitrary MCP commands."""
    enabled = load_enabled_servers() if enabled_servers is None else enabled_servers
    if not enabled <= set(SERVER_NAMES):
        raise ValueError("Unknown MCP server.")
    context7_key = local_secret("CONTEXT7_API_KEY", "mcp.env")
    github_token = local_secret("GITHUB_PERSONAL_ACCESS_TOKEN", "mcp.env")
    github = MCPServerConfig(
        "github", "docker",
        (
            "run", "-i", "--rm",
            "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
            "-e", "GITHUB_READ_ONLY",
            "-e", "GITHUB_TOOLSETS",
            "ghcr.io/github/github-mcp-server",
        ),
        {
            "GITHUB_PERSONAL_ACCESS_TOKEN": github_token,
            "GITHUB_READ_ONLY": "true",
            "GITHUB_TOOLSETS": "repos,issues,pull_requests",
        },
        enabled="github" in enabled,
        access="Read-only GitHub access to repositories, issues, and pull requests.",
    ) if github_token else MCPServerConfig(
        "github", "docker", (), {},
        "Add GITHUB_PERSONAL_ACCESS_TOKEN to ~/.mini-hermes/mcp.env.",
        enabled="github" in enabled,
        access="Read-only GitHub access to repositories, issues, and pull requests.",
    )
    return [
        MCPServerConfig(
            "context7", "npx", ("-y", "@upstash/context7-mcp"),
            {"CONTEXT7_API_KEY": context7_key} if context7_key else {},
            enabled="context7" in enabled,
            access="Searches Context7's documentation library and retrieves library docs.",
        ),
        github,
        MCPServerConfig(
            "playwright", "npx",
            (
                "-y", "@playwright/mcp@latest", "--headless", "--isolated",
                "--idle-timeout=300000",
            ),
            {},
            enabled="playwright" in enabled,
            access="Opens pages and reads browser content; sensitive browser actions need approval.",
        ),
    ]
