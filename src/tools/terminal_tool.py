import subprocess
from pathlib import Path
import shutil
from typing import Callable


def run_terminal(command: str, project_root: Path, confirm: Callable[[str], bool]) -> str:
    if not command.strip():
        raise ValueError("Command is empty. Provide a shell command to run.")
    if not confirm(command):
        return "Command cancelled by the user."
    bubblewrap = shutil.which("bwrap")
    if not bubblewrap:
        raise RuntimeError("Terminal tool needs bubblewrap (bwrap) for project isolation; install it and retry.")
    root = project_root.resolve()
    try:
        path_parts = root.relative_to(Path("/home")).parts
    except ValueError as exc:
        raise RuntimeError("The terminal sandbox currently supports projects under /home.") from exc
    project_dirs = []
    destination = Path("/home")
    for part in path_parts:
        destination /= part
        project_dirs.extend(["--dir", str(destination)])
    try:
        result = subprocess.run(
            [bubblewrap, "--ro-bind", "/", "/", "--tmpfs", "/home",
             *project_dirs, "--bind", str(root), str(root),
             "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
             "--unshare-pid", "--chdir", str(root), "--setenv", "HOME", str(Path.home()),
             "--die-with-parent", "--new-session", "/bin/bash", "-lc", command],
            cwd=root, text=True,
            capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "Command timed out after 30 seconds. Try a narrower or faster command."
    output = result.stdout + result.stderr
    if len(output) > 20_000:
        output = output[:20_000] + "\n[output truncated at 20,000 characters]"
    return f"Exit code: {result.returncode}\n{output}" if output else f"Exit code: {result.returncode}"
