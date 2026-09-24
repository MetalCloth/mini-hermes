import subprocess
from pathlib import Path
import shutil
from typing import Callable


def validate_project_root(project_root: Path) -> Path:
    root = project_root.resolve()
    if root == Path("/") or any(
        root == system_path or root.is_relative_to(system_path)
        for system_path in (Path("/proc"), Path("/dev"), Path("/sys"))
    ):
        raise ValueError("Choose a project folder outside system mounts and /.")
    return root


def run_terminal(command: str, project_root: Path, confirm: Callable[[str], bool]) -> str:
    if not command.strip():
        raise ValueError("Command is empty. Provide a shell command to run.")
    root = validate_project_root(project_root)
    if not confirm(command):
        return "Command cancelled by the user."
    bubblewrap = shutil.which("bwrap")
    if not bubblewrap:
        raise RuntimeError("Terminal tool needs bubblewrap (bwrap) for project isolation; install it and retry.")
    project_dirs = []
    # Restore the selected path under the private /home or /tmp mount.
    for hidden_parent in (Path("/home"), Path("/tmp")):
        if root.is_relative_to(hidden_parent):
            destination = hidden_parent
            for part in root.relative_to(hidden_parent).parts:
                destination /= part
                project_dirs.extend(["--dir", str(destination)])
            break
    try:
        result = subprocess.run(
            [bubblewrap, "--ro-bind", "/", "/", "--tmpfs", "/home", "--tmpfs", "/tmp",
             *project_dirs, "--bind", str(root), str(root),
             "--proc", "/proc", "--dev", "/dev",
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
