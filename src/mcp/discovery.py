"""Oryn's small, known-good MCP server presets."""

from dataclasses import dataclass

from src.security.secrets import local_secret


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: str
    args: tuple[str, ...]
    env: dict[str, str]
    disabled_reason: str = ""


def server_configs() -> list[MCPServerConfig]:
    """Build fixed server commands; Oryn does not run arbitrary MCP commands."""
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
    ) if github_token else MCPServerConfig(
        "github", "docker", (), {},
        "Add GITHUB_PERSONAL_ACCESS_TOKEN to ~/.mini-hermes/mcp.env.",
    )
    return [
        MCPServerConfig(
            "context7", "npx", ("-y", "@upstash/context7-mcp"),
            {"CONTEXT7_API_KEY": context7_key} if context7_key else {},
        ),
        github,
        MCPServerConfig(
            "playwright", "npx",
            (
                "-y", "@playwright/mcp@latest", "--headless", "--isolated",
                "--idle-timeout=300000",
            ),
            {},
        ),
    ]
