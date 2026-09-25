"""Read API credentials from the environment or Oryn's user config."""

import os
from pathlib import Path


def local_secret(name: str, filename: str) -> str:
    """Read one secret without printing or storing it in the project."""
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()

    env_file = Path.home() / ".mini-hermes" / filename
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return ""
