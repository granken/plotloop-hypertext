"""Local daemon lifecycle, worker supervision, and bounded logging."""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import os
import selectors
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from .config import AgentConfig
from .logging_utils import redact_log_text
from .private_files import open_private_file, protect_existing_file

STOP_REQUEST = "daemon.stop"
SUPERVISED_STATE_ENV = "_HYPERTEXT_SUPERVISED_STATE_DIR"
SUPERVISOR_PID_ENV = "_HYPERTEXT_SUPERVISOR_PID"
WORKER_LOCK_BUSY = 75
MAX_LOG_BYTES = 20 * 1024 * 1024
MAX_LOG_LINE_BYTES = 65536
POLL_INTERVAL_SEC = 0.1
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACTIONS = {"start", "stop", "restart", "status", "logs", "fg"}


@contextlib.contextmanager
def file_lock(path: Path, *, blocking: bool = False) -> Iterator[int | None]:
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError as exc:
            if blocking or exc.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            yield None
        else:
            yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def lease_lock(path: Path) -> Iterator[int | None]:
    """Keep the lease after releasing the short guard shared with status probes."""
    with contextlib.ExitStack() as stack:
        with file_lock(path.with_suffix(".guard"), blocking=True):
            fd = stack.enter_context(file_lock(path))
        yield fd


def lock_held(path: Path) -> bool:
    with file_lock(path.with_suffix(".guard"), blocking=True):
        with file_lock(path) as fd:
            return fd is None


def stop_request_id(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii")
    except FileNotFoundError:
        return None


class DaemonLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def protect_existing(self) -> None:
        for path in (self.path, self.path.with_name(f"{self.path.name}.1")):
            protect_existing_file(path)

    def write(self, data: bytes) -> None:
        data = redact_log_text(data.decode("utf-8", errors="replace")).encode()
        fd = open_private_file(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        size = os.fstat(fd).st_size
        if size and size + len(data) > MAX_LOG_BYTES:
            os.close(fd)
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
            fd = open_private_file(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        with os.fdopen(fd, "ab") as log:
            log.write(data)

    def note(self, message: str) -> None:
        prefix = time.strftime("%Y-%m-%d %H:%M:%S")
        self.write(f"{prefix} [Supervisor] {message}\n".encode())

    def redacted_tail(self) -> list[str]:
        try:
            with os.fdopen(open_private_file(self.path, os.O_RDONLY), "rb") as log:
                size = log.seek(0, os.SEEK_END)
                log.seek(max(0, size - 65536))
                data = log.read(65536)
        except FileNotFoundError:
            return []
        if size > 65536:
            data = data.partition(b"\n")[2]
        lines = data.decode("utf-8", errors="replace").splitlines()[-8:]
        return [redact_log_text(line) for line in lines]


def process_output(args: list[str]) -> str:
    with contextlib.suppress(OSError):
        return subprocess.run(args, capture_output=True, text=True, check=False).stdout.strip()
    return ""


def worker_command(args: list[str]) -> list[str]:
    script = os.getenv("HYPERTEXT_AGENT_SCRIPT") or str(PROJECT_ROOT / "hypertext_agent.py")
    return [sys.executable, "-u", script, *args]


class Supervisor:
    def __init__(self, state: Path, args: list[str]) -> None:
        self.state = state
        self.args = args
        self.stop_path = state / STOP_REQUEST
        self.log = DaemonLog(state / "daemon.log")
        self.signalled = False

    def stopping(self) -> bool:
        return self.signalled or self.stop_path.exists()

    def stop_on_signal(self, *_: object) -> None:
        self.signalled = True

    def run_worker(self) -> tuple[int, int]:
        with subprocess.Popen(
            worker_command(self.args),
            env=dict(os.environ, **{
                SUPERVISED_STATE_ENV: str(self.state),
                SUPERVISOR_PID_ENV: str(os.getpid()),
            }),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as worker, selectors.DefaultSelector() as selector:
            assert worker.stdout is not None
            selector.register(worker.stdout, selectors.EVENT_READ)

            line = bytearray()
            truncated = False

            def write_line() -> None:
                nonlocal truncated
                suffix = b" [truncated]\n" if truncated else b"\n"
                self.log.write(bytes(line) + suffix)
                line.clear()
                truncated = False

            def consume(data: bytes) -> None:
                nonlocal truncated
                parts = data.split(b"\n")
                for i, part in enumerate(parts):
                    remaining = MAX_LOG_LINE_BYTES - len(line)
                    line.extend(part[:remaining])
                    truncated |= len(part) > remaining
                    if i < len(parts) - 1:
                        write_line()

            def pump(timeout: float) -> bool:
                if not selector.select(timeout):
                    return False
                data = os.read(worker.stdout.fileno(), 65536)
                if not data:
                    selector.unregister(worker.stdout)
                    return False
                consume(data)
                return True

            terminating = False
            try:
                while worker.poll() is None:
                    if self.stopping() and not terminating:
                        worker.terminate()
                        terminating = True
                    pump(POLL_INTERVAL_SEC)
                while pump(0):
                    pass
                if line:
                    write_line()
                return worker.pid, worker.wait()
            finally:
                if worker.poll() is None:
                    worker.stdout.close()
                    worker.terminate()

    def run(self, lock_fd: int) -> int:
        os.set_inheritable(lock_fd, False)
        try:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, self.stop_on_signal)
            signal.signal(signal.SIGINT, self.stop_on_signal)
            pid_path = self.state / "supervisor.pid"
            pid_path.write_text(f"{os.getpid()}\n", encoding="ascii")
            caffeinate = None
            try:
                if (
                    Path("/usr/bin/caffeinate").is_file()
                    and os.getenv("HYPERTEXT_DISABLE_CAFFEINATE") != "1"
                ):
                    caffeinate = subprocess.Popen(
                        ["/usr/bin/caffeinate", "-is", "-w", str(os.getpid())],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                delay = max(0.05, float(os.getenv("HYPERTEXT_RESPAWN_DELAY_SEC", "2")))
                while not self.stopping():
                    self.log.note(f"Starting HyperText worker (supervisor PID={os.getpid()})")
                    pid, code = self.run_worker()
                    if self.stopping():
                        break
                    if code == WORKER_LOCK_BUSY:
                        self.log.note("Another worker owns daemon.lock; ending supervision.")
                        return 1
                    self.log.note(f"Worker PID {pid} exited (code={code}); respawning in {delay:g}s")
                    deadline = time.monotonic() + delay
                    while not self.stopping() and time.monotonic() < deadline:
                        time.sleep(min(POLL_INTERVAL_SEC, max(0, deadline - time.monotonic())))
            except (OSError, ValueError) as exc:
                self.log.note(f"Supervisor failed: {exc}")
                return 1
            finally:
                if caffeinate is not None:
                    caffeinate.terminate()
                    caffeinate.wait()
                pid_path.unlink(missing_ok=True)
            return 0
        finally:
            os.close(lock_fd)


class DaemonControl:
    def __init__(self, state: Path) -> None:
        self.state = state
        self.stop_path = state / STOP_REQUEST
        self.log = DaemonLog(state / "daemon.log")

    def active(self, role: str) -> bool:
        return lock_held(self.state / f"{role}.lock")

    def live_pid(self, role: str) -> int | None:
        if not self.active(role):
            return None
        try:
            pid = int((self.state / f"{role}.pid").read_text(encoding="ascii"))
        except (FileNotFoundError, ValueError):
            return None
        return pid if pid > 1 else None

    def start(self, args: list[str]) -> int:
        if self.active("supervisor") or self.active("daemon"):
            if self.stop_path.exists():
                print("HyperText is still stopping.", file=sys.stderr)
                return 1
            print("HyperText daemon or supervisor already running.")
            return 0

        self.state.chmod(0o700)
        self.log.protect_existing()
        self.stop_path.unlink(missing_ok=True)
        (self.state / "supervisor.pid").unlink(missing_ok=True)
        with lease_lock(self.state / "supervisor.lock") as lock_fd:
            if lock_fd is None:
                print("HyperText supervisor already running.")
                return 0
            supervisor = subprocess.Popen(
                [
                    sys.executable, "-m", "hypertext.daemon_control", "_supervise",
                    str(lock_fd), str(self.state), *args,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                pass_fds=(lock_fd,),
            )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and supervisor.poll() is None:
            worker_pid = self.live_pid("daemon")
            if self.live_pid("supervisor") and worker_pid:
                print(f"HyperText supervisor started (PID={supervisor.pid}, worker PID={worker_pid}).")
                return 0
            time.sleep(POLL_INTERVAL_SEC)
        if supervisor.poll() is None:
            print("HyperText supervisor is running, but the worker has not started. Check logs.", file=sys.stderr)
        else:
            print("HyperText supervisor exited during startup. Check logs.", file=sys.stderr)
        return 1

    def stop(self) -> int:
        timeout = max(0, float(os.getenv("HYPERTEXT_SHUTDOWN_TIMEOUT_SEC", "30")))
        if self.active("supervisor") or self.active("daemon"):
            request_id = uuid.uuid4().hex
            temp = self.stop_path.with_name(f".{STOP_REQUEST}.{request_id}")
            try:
                temp.write_text(request_id, encoding="ascii")
                temp.replace(self.stop_path)
            finally:
                temp.unlink(missing_ok=True)
            deadline = time.monotonic() + timeout
            while self.active("supervisor") or self.active("daemon"):
                if time.monotonic() >= deadline:
                    print(
                        "HyperText is still stopping. In-flight work has not been killed.",
                        file=sys.stderr,
                    )
                    return 1
                time.sleep(POLL_INTERVAL_SEC)
        self.stop_path.unlink(missing_ok=True)
        print("HyperText daemon stopped.")
        return 0

    def status(self) -> int:
        active_supervisor = self.active("supervisor")
        active_worker = self.active("daemon")
        sup_pid = self.live_pid("supervisor")
        worker_pid = self.live_pid("daemon")
        print(f"  Supervisor : {self.describe(active_supervisor, sup_pid)}")
        caffeinate = None
        if sup_pid is not None:
            output = process_output(["pgrep", "-P", str(sup_pid), "-x", "caffeinate"])
            caffeinate = next(iter(output.split()), None)
        print(f"  Caffeinate : {'active (PID=' + caffeinate + ')' if caffeinate else 'inactive'}")
        uptime = ""
        if worker_pid is not None:
            output = process_output(["ps", "-p", str(worker_pid), "-o", "etime="])
            if output:
                uptime = f"uptime={output}"
        print(f"  Worker     : {self.describe(active_worker, worker_pid, uptime)}")
        if self.stop_path.exists() and (active_worker or active_supervisor):
            print("  Shutdown   : requested")
        print(f"  State Dir  : {self.state}")
        print(f"  Log File   : {self.log.path}")
        lines = self.log.redacted_tail()
        if lines:
            print("Recent Log Tail (redacted):")
            for line in lines:
                print(f"  {line}")
        return 0 if active_worker else 1

    @staticmethod
    def describe(active: bool, pid: int | None, detail: str = "") -> str:
        if not active:
            return "stopped"
        if pid is None:
            return "starting"
        suffix = f", {detail}" if detail else ""
        return f"running (PID={pid}{suffix})"

    def logs(self) -> int:
        self.log.protect_existing()
        os.close(open_private_file(self.log.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND))
        with subprocess.Popen(
            ["tail", "-n", "50", "-F", str(self.log.path)],
            stdout=subprocess.PIPE, text=True, errors="replace",
        ) as tail:
            assert tail.stdout is not None
            try:
                for line in tail.stdout:
                    print(redact_log_text(line), end="", flush=True)
            except KeyboardInterrupt:
                return 0
            finally:
                if tail.poll() is None:
                    tail.terminate()
            return tail.wait()


def main(argv: list[str] | None = None) -> int:
    from .cli import build_parser

    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "_supervise":
        return Supervisor(Path(args[2]), args[3:]).run(int(args[1]))

    usage = argparse.ArgumentParser(
        prog="start_daemon.sh",
        usage="%(prog)s [start|stop|restart|status|logs|fg] [--env-file PATH] [agent options]",
        description="Manage the HyperText daemon. With agent one-shot flags, run the CLI directly.",
    )
    usage.add_argument("--env-file", help="Select the same configuration file for each command.")
    if args and args[0] in ("-h", "--help", "help"):
        usage.print_help()
        return 0
    action = args.pop(0) if args and args[0] in ACTIONS else "start"
    if action in {"stop", "status", "logs"}:
        opts = usage.parse_args(args)
    else:
        opts = build_parser().parse_args(args)
        one_shot = any(
            (opts.cli, opts.self_test, opts.doctor, opts.create_invite, opts.list_users,
             opts.allow_user, opts.revoke_user)
        )
        if action == "restart" and one_shot:
            usage.error("restart requires daemon options")
        if action == "fg" or one_shot:
            os.execv(sys.executable, worker_command(args))

    try:
        config = AgentConfig.from_env(env_file=opts.env_file, export_dotenv=False)
        state = config.state_dir.resolve()
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        daemon = DaemonControl(state)
        if action == "status":
            return daemon.status()
        if action == "logs":
            return daemon.logs()
        with file_lock(state / "lifecycle.lock", blocking=True):
            if action in {"stop", "restart"}:
                result = daemon.stop()
                if action == "stop" or result:
                    return result
            return daemon.start(args)
    except (OSError, ValueError) as exc:
        print(f"HyperText daemon: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
