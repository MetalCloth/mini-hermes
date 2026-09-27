"""Bubblewrap-isolated shell commands and session-owned interactive terminal jobs."""

from dataclasses import dataclass, field
import os
import pty
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable


MAX_COMMAND_OUTPUT_CHARS = 20_000
MAX_JOB_BUFFER_BYTES = 200_000
MAX_RUNNING_JOBS = 8
MAX_JOBS_PER_SESSION = 20
MAX_TERMINAL_WAIT_SECONDS = 10
MAX_TERMINAL_JOB_SECONDS = 2 * 60 * 60
MAX_JOB_READ_BYTES = 4_800


def _timeout_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def validate_project_root(project_root: Path) -> Path:
    root = project_root.resolve()
    if root == Path("/") or any(
        root == system_path or root.is_relative_to(system_path)
        for system_path in (Path("/proc"), Path("/dev"), Path("/sys"))
    ):
        raise ValueError("Choose a project folder outside system mounts and /.")
    return root


def _sandbox_command(command: str, root: Path, bubblewrap: str) -> list[str]:
    project_dirs = []
    for hidden_parent in (Path("/home"), Path("/tmp")):
        if root.is_relative_to(hidden_parent):
            destination = hidden_parent
            for part in root.relative_to(hidden_parent).parts:
                destination /= part
                project_dirs.extend(["--dir", str(destination)])
            break
    return [
        bubblewrap, "--ro-bind", "/", "/", "--tmpfs", "/home", "--tmpfs", "/tmp",
        *project_dirs, "--bind", str(root), str(root), "--proc", "/proc", "--dev", "/dev",
        "--unshare-pid", "--chdir", str(root), "--setenv", "HOME", str(Path.home()),
        "--die-with-parent", "--new-session", "/bin/bash", "-lc", command,
    ]


def _safe_output(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return "".join(
        char if char in "\n\r\t" or ord(char) >= 32 and ord(char) != 127
        else f"\\x{ord(char):02x}"
        for char in text
    )


def run_terminal(command: str, project_root: Path, confirm: Callable[[str], bool]) -> str:
    """Run a bounded one-shot command for compatibility with non-chat callers."""
    if not isinstance(command, str) or not command.strip():
        raise ValueError("Command is empty. Provide a shell command to run.")
    root = validate_project_root(project_root)
    if not confirm(command):
        return "Command cancelled by the user."
    bubblewrap = shutil.which("bwrap")
    if not bubblewrap:
        raise RuntimeError("Terminal tool needs bubblewrap (bwrap) for project isolation; install it and retry.")
    try:
        result = subprocess.run(
            _sandbox_command(command, root, bubblewrap), cwd=root, text=True,
            capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        partial = []
        for label, value in (("stdout", exc.stdout), ("stderr", exc.stderr)):
            text = _timeout_output(value)
            if text:
                partial.append(f"{label}:\n{text}")
        output = "\n".join(partial)
        if len(output) > MAX_COMMAND_OUTPUT_CHARS:
            output = output[:MAX_COMMAND_OUTPUT_CHARS] + "\n[output truncated at 20,000 characters]"
        detail = f"\nPartial output:\n{output}" if output else ""
        return f"Command timed out after 30 seconds.{detail} Use terminal jobs for longer commands."
    output = result.stdout + result.stderr
    if len(output) > MAX_COMMAND_OUTPUT_CHARS:
        output = output[:MAX_COMMAND_OUTPUT_CHARS] + "\n[output truncated at 20,000 characters]"
    return f"Exit code: {result.returncode}\n{output}" if output else f"Exit code: {result.returncode}"


@dataclass
class TerminalJob:
    id: str
    process: subprocess.Popen
    master_fd: int
    ready: threading.Event = field(default_factory=threading.Event)
    output: bytearray = field(default_factory=bytearray)
    output_start: int = 0
    read_offset: int = 0
    reader_finished: bool = False
    total_bytes: int = 0
    started_at: float = field(default_factory=time.monotonic)
    timeout_timer: threading.Timer | None = None
    timed_out: bool = False


class TerminalJobManager:
    """Own shell processes for one chat session; jobs never survive app exit."""

    def __init__(self, project_root: Path):
        self.project_root = validate_project_root(project_root)
        self._jobs: dict[str, TerminalJob] = {}
        self._lock = threading.RLock()
        self._closed = False

    def start(self, command: str, confirm: Callable[[str], bool]) -> str:
        if not isinstance(command, str) or not command.strip() or len(command) > 20_000 or "\0" in command:
            raise ValueError("Give a non-empty shell command of at most 20,000 characters.")
        if self._closed:
            raise RuntimeError("This chat's terminal job manager is closed.")
        if not confirm(command):
            return "Command cancelled by the user."
        bubblewrap = shutil.which("bwrap")
        if not bubblewrap:
            raise RuntimeError("Terminal tool needs bubblewrap (bwrap) for project isolation; install it and retry.")

        with self._lock:
            self._discard_finished_overflow()
            if len(self._jobs) >= MAX_JOBS_PER_SESSION:
                raise RuntimeError(f"This chat already has {MAX_JOBS_PER_SESSION} terminal jobs; stop or close one first.")
            if sum(job.process.poll() is None for job in self._jobs.values()) >= MAX_RUNNING_JOBS:
                raise RuntimeError(f"This chat can run at most {MAX_RUNNING_JOBS} terminal jobs at once.")
            master_fd, slave_fd = pty.openpty()
            try:
                process = subprocess.Popen(
                    _sandbox_command(command, self.project_root, bubblewrap),
                    cwd=self.project_root, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                    close_fds=True, start_new_session=True,
                )
            except BaseException:
                os.close(master_fd)
                raise
            finally:
                os.close(slave_fd)
            job_id = uuid.uuid4().hex[:12]
            while job_id in self._jobs:
                job_id = uuid.uuid4().hex[:12]
            job = TerminalJob(job_id, process, master_fd)
            self._jobs[job_id] = job
            job.timeout_timer = threading.Timer(
                MAX_TERMINAL_JOB_SECONDS, self._expire, args=(job,),
            )
            job.timeout_timer.daemon = True
            job.timeout_timer.start()
            reader = threading.Thread(target=self._read_output, args=(job,), daemon=True)
            reader.start()
            return job_id

    def read(self, job_id: str, wait_seconds: float = 2, cancel_event: threading.Event | None = None) -> str:
        job = self._get(job_id)
        if type(wait_seconds) not in {int, float} or not 0 <= wait_seconds <= MAX_TERMINAL_WAIT_SECONDS:
            raise ValueError(f"wait_seconds must be between 0 and {MAX_TERMINAL_WAIT_SECONDS}.")
        deadline = time.monotonic() + wait_seconds
        while not job.ready.is_set() and time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("Terminal output wait cancelled.")
            job.ready.wait(min(0.1, deadline - time.monotonic()))
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Terminal output wait cancelled.")
        with self._lock:
            status = self._status(job)
            available_start = max(job.read_offset, job.output_start)
            dropped = max(0, job.output_start - job.read_offset)
            start_index = available_start - job.output_start
            chunk = bytes(job.output[start_index:start_index + MAX_JOB_READ_BYTES])
            job.read_offset = available_start + len(chunk)
            job.ready.clear()
            overflow = "[Earlier terminal output was discarded after the 200,000-byte session buffer filled.]\n" if dropped else ""
            output = _safe_output(chunk)
            if not output:
                output = "No new output."
            if status == "running":
                next_step = f"Use terminal_read with job_id '{job.id}' to check again, terminal_input to send input, or terminal_stop to stop it."
            else:
                next_step = "The process has exited."
            if job.total_bytes > MAX_JOB_BUFFER_BYTES:
                overflow += f"[Captured {job.total_bytes:,} output bytes; older data may have been discarded.]\n"
            return f"Terminal job {job.id} · {status}\n{overflow}{output}\n{next_step}"

    def send_input(
        self, job_id: str, text: str, confirm: Callable[[str], bool],
    ) -> str:
        if not isinstance(text, str) or not text or len(text.encode("utf-8")) > 4096 or "\0" in text:
            raise ValueError("Terminal input must be 1 to 4,096 UTF-8 bytes and cannot contain NUL.")
        job = self._get(job_id)
        if job.process.poll() is not None:
            raise RuntimeError(f"Terminal job {job.id} has already exited.")
        if not confirm(f"Terminal input to job {job.id}:\n{text}"):
            return "Terminal input cancelled by the user; nothing was sent."
        if job.process.poll() is not None:
            raise RuntimeError(f"Terminal job {job.id} exited while approval was pending.")
        data = text.encode("utf-8")
        if not data.endswith(b"\n"):
            data += b"\n"
        written = os.write(job.master_fd, data)
        if written != len(data):
            raise RuntimeError("Only part of the terminal input was sent; inspect the job before retrying.")
        return f"Sent input to terminal job {job.id}. Use terminal_read to inspect its response."

    def stop(self, job_id: str) -> str:
        job = self._get(job_id)
        if job.process.poll() is not None:
            return f"Terminal job {job.id} has already exited with code {job.process.returncode}."
        try:
            os.killpg(job.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            job.process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(job.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            job.process.wait(timeout=1)
        job.ready.set()
        return f"Stopped terminal job {job.id}; exit code {job.process.returncode}."

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.timeout_timer:
                job.timeout_timer.cancel()
            if job.process.poll() is None:
                try:
                    os.killpg(job.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    job.process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(job.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    job.process.wait(timeout=1)
            job.ready.set()
            try:
                os.close(job.master_fd)
            except OSError:
                pass

    def _get(self, job_id: str) -> TerminalJob:
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{12}", job_id):
            raise ValueError("Give the 12-character job ID returned by terminal.")
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise ValueError("No terminal job with that ID exists in this chat. Job IDs are session-local and expire when Oryn exits.")
        return job

    def _status(self, job: TerminalJob) -> str:
        result = job.process.poll()
        if job.timed_out:
            return f"timed out after {MAX_TERMINAL_JOB_SECONDS // 3600} hours (exit code {result})"
        return "running" if result is None else f"exited with code {result}"

    def _expire(self, job: TerminalJob) -> None:
        if job.process.poll() is None:
            job.timed_out = True
            try:
                os.killpg(job.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                job.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(job.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                job.process.wait(timeout=1)
            job.ready.set()

    def _read_output(self, job: TerminalJob) -> None:
        try:
            while True:
                try:
                    chunk = os.read(job.master_fd, 8192)
                except OSError:
                    # PTYs report EIO at EOF and EBADF when the owning app closes the job.
                    break
                if not chunk:
                    break
                with self._lock:
                    job.output.extend(chunk)
                    job.total_bytes += len(chunk)
                    overflow = len(job.output) - MAX_JOB_BUFFER_BYTES
                    if overflow > 0:
                        del job.output[:overflow]
                        job.output_start += overflow
                    job.ready.set()
        finally:
            if job.timeout_timer:
                job.timeout_timer.cancel()
            with self._lock:
                job.reader_finished = True
                job.ready.set()

    def _discard_finished_overflow(self) -> None:
        finished = [job for job in self._jobs.values() if job.process.poll() is not None]
        while len(self._jobs) >= MAX_JOBS_PER_SESSION and finished:
            self._jobs.pop(finished.pop(0).id, None)
