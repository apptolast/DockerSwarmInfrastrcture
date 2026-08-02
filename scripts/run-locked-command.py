#!/usr/bin/env python3
"""Run an interactive command in a killable PTY while watching a lock holder."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import os
import pty
import select
import selectors
import signal
import sys
import termios
import time
import tty
from pathlib import Path

PR_SET_PDEATHSIG = 1
PR_SET_CHILD_SUBREAPER = 36
HOLDER_LOST_EXIT = 125
TERMINATION_GRACE_SECONDS = 2.0
MIN_SIGNAL_GRACE_SECONDS = 2.0
MAX_SIGNAL_GRACE_SECONDS = 3600.0


class LockedCommandError(RuntimeError):
    """The guarded command cannot be launched safely."""


def set_parent_death_signal(parent_pid: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)


def become_child_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def copy_terminal_size(source: int, destination: int) -> None:
    if not os.isatty(source):
        return
    try:
        size = fcntl.ioctl(source, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(destination, termios.TIOCSWINSZ, size)
    except OSError:
        return


def holder_is_alive(pid: int, pid_descriptor: int | None) -> bool:
    if pid_descriptor is not None:
        readable, _writable, _exceptional = select.select(
            [pid_descriptor],
            [],
            [],
            0,
        )
        return not readable
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(child_pid: int, signal_number: int) -> None:
    try:
        os.killpg(child_pid, signal_number)
    except ProcessLookupError:
        pass


def terminate_group_and_wait(child_pid: int) -> int:
    signal_group(child_pid, signal.SIGTERM)
    signal_group(child_pid, signal.SIGCONT)
    deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
        if waited_pid == child_pid:
            return status
        time.sleep(0.05)
    signal_group(child_pid, signal.SIGKILL)
    _waited_pid, status = os.waitpid(child_pid, 0)
    return status


def descendant_pids(parent_pid: int) -> set[int]:
    # A subreaper routinely has short-lived grandchildren reparented to it
    # in their last instant of life: they exit, become a zombie under their
    # original parent, and only land here if that parent also exits before
    # reaping them. A zombie holds no resources and cannot execute anything;
    # it is a wait()-able exit status, not a live escaped process, so it is
    # excluded here rather than treated as a residual/leaked descendant.
    # reap_children() (called by every caller of this function) still
    # collects it via waitpid() regardless of this exclusion.
    parent_by_pid: dict[int, int] = {}
    live_pids: set[int] = set()
    for candidate in Path("/proc").iterdir():
        if not candidate.name.isdigit():
            continue
        try:
            status = (candidate / "status").read_text(encoding="ascii")
        except OSError:
            continue
        parent_lines = [
            line for line in status.splitlines() if line.startswith("PPid:")
        ]
        state_lines = [
            line for line in status.splitlines() if line.startswith("State:")
        ]
        if len(parent_lines) == 1:
            pid = int(candidate.name)
            parent_by_pid[pid] = int(parent_lines[0].split()[1])
            is_zombie = len(state_lines) == 1 and state_lines[0].split()[1] == "Z"
            if not is_zombie:
                live_pids.add(pid)

    descendants: set[int] = set()
    changed = True
    while changed:
        changed = False
        for pid, observed_parent in parent_by_pid.items():
            if pid in descendants:
                continue
            if observed_parent == parent_pid or observed_parent in descendants:
                descendants.add(pid)
                changed = True
    return descendants & live_pids


def reap_children() -> None:
    while True:
        try:
            waited_pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if waited_pid == 0:
            return


def terminate_residual_descendants(parent_pid: int) -> bool:
    residual_detected = bool(descendant_pids(parent_pid))
    if not residual_detected:
        reap_children()
        return False
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
        while time.monotonic() < deadline:
            descendants = descendant_pids(parent_pid)
            if not descendants:
                reap_children()
                return True
            for pid in descendants:
                try:
                    os.kill(pid, signal_number)
                except ProcessLookupError:
                    pass
            reap_children()
            time.sleep(0.05)
    if descendant_pids(parent_pid):
        raise LockedCommandError("guarded descendants survived TERM and KILL")
    reap_children()
    return True


def wait_status_to_exit_code(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    raise LockedCommandError("guarded command returned an unknown wait status")


def run(
    command: list[str],
    watch_pid: int,
    *,
    signal_grace_seconds: float = TERMINATION_GRACE_SECONDS,
) -> int:
    if not command or watch_pid <= 1:
        raise LockedCommandError("command or lock-holder PID is invalid")
    if not (
        MIN_SIGNAL_GRACE_SECONDS <= signal_grace_seconds <= MAX_SIGNAL_GRACE_SECONDS
    ):
        raise LockedCommandError("signal grace is outside the reviewed bounds")
    try:
        pid_descriptor = os.pidfd_open(watch_pid)
    except (AttributeError, OSError):
        pid_descriptor = None
    if not holder_is_alive(watch_pid, pid_descriptor):
        if pid_descriptor is not None:
            os.close(pid_descriptor)
        raise LockedCommandError("operation-lock holder is already absent")

    runner_parent = os.getppid()
    set_parent_death_signal(runner_parent)
    become_child_subreaper()
    runner_pid = os.getpid()
    child_pid, terminal_master = pty.fork()
    if child_pid == 0:
        try:
            set_parent_death_signal(runner_pid)
            os.execvp(command[0], command)
        except BaseException as error:
            print(f"ERROR: cannot exec guarded command: {error}", file=sys.stderr)
            os._exit(126)

    stdin_descriptor = sys.stdin.fileno()
    stdout_descriptor = sys.stdout.fileno()
    original_terminal = None
    selector: selectors.BaseSelector | None = None
    child_status: int | None = None
    holder_lost = False
    termination_started: float | None = None
    termination_grace = TERMINATION_GRACE_SECONDS
    interrupted_signal: int | None = None
    old_handlers: dict[int, signal.Handlers] = {}
    old_winch: signal.Handlers | None = None
    stdin_unpollable = False
    stdin_finished = False

    def request_termination(signal_number: int, _frame: object) -> None:
        nonlocal interrupted_signal, termination_grace, termination_started
        interrupted_signal = signal_number
        if termination_started is None:
            termination_started = time.monotonic()
            termination_grace = signal_grace_seconds
            delivered_signal = (
                signal_number
                if signal_number in {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
                else signal.SIGTERM
            )
            signal_group(child_pid, delivered_signal)

    def resize(_signal_number: int, _frame: object) -> None:
        copy_terminal_size(stdin_descriptor, terminal_master)

    try:
        if os.isatty(stdin_descriptor):
            original_terminal = termios.tcgetattr(stdin_descriptor)
            tty.setraw(stdin_descriptor)
        copy_terminal_size(stdin_descriptor, terminal_master)

        selector = selectors.DefaultSelector()
        selector.register(terminal_master, selectors.EVENT_READ, "terminal")
        try:
            selector.register(stdin_descriptor, selectors.EVENT_READ, "stdin")
        except OSError as error:
            if error.errno != errno.EPERM:
                raise
            stdin_unpollable = True

        old_handlers = {
            signal_number: signal.signal(signal_number, request_termination)
            for signal_number in (
                signal.SIGINT,
                signal.SIGTERM,
                signal.SIGHUP,
                signal.SIGQUIT,
            )
        }
        old_winch = signal.signal(signal.SIGWINCH, resize)

        while child_status is None:
            if not holder_lost and not holder_is_alive(watch_pid, pid_descriptor):
                holder_lost = True
                termination_started = time.monotonic()
                termination_grace = TERMINATION_GRACE_SECONDS
                signal_group(child_pid, signal.SIGTERM)
            if (
                termination_started is not None
                and time.monotonic() - termination_started >= termination_grace
            ):
                signal_group(child_pid, signal.SIGKILL)

            if stdin_unpollable and not stdin_finished:
                chunk = os.read(stdin_descriptor, 65536)
                if chunk:
                    os.write(terminal_master, chunk)
                else:
                    stdin_finished = True
                    try:
                        os.write(terminal_master, b"\x04")
                    except OSError:
                        pass

            for key, _events in selector.select(timeout=0.1):
                if key.data == "terminal":
                    try:
                        chunk = os.read(terminal_master, 65536)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            selector.unregister(terminal_master)
                            continue
                        raise
                    if not chunk:
                        selector.unregister(terminal_master)
                        continue
                    view = memoryview(chunk)
                    while view:
                        written = os.write(stdout_descriptor, view)
                        view = view[written:]
                else:
                    try:
                        chunk = os.read(stdin_descriptor, 65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        selector.unregister(stdin_descriptor)
                        stdin_finished = True
                        try:
                            os.write(terminal_master, b"\x04")
                        except OSError:
                            pass
                        continue
                    try:
                        os.write(terminal_master, chunk)
                    except OSError:
                        pass

            waited_pid, status = os.waitpid(
                child_pid,
                os.WNOHANG | os.WUNTRACED | os.WCONTINUED,
            )
            if waited_pid == child_pid:
                if os.WIFSTOPPED(status):
                    signal_group(child_pid, signal.SIGCONT)
                elif os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    child_status = status
    except BaseException:
        if child_status is None:
            child_status = terminate_group_and_wait(child_pid)
        terminate_residual_descendants(os.getpid())
        raise
    finally:
        for signal_number, old_handler in old_handlers.items():
            signal.signal(signal_number, old_handler)
        if old_winch is not None:
            signal.signal(signal.SIGWINCH, old_winch)
        if original_terminal is not None:
            termios.tcsetattr(
                stdin_descriptor,
                termios.TCSADRAIN,
                original_terminal,
            )
        if selector is not None:
            selector.close()
        os.close(terminal_master)
        if pid_descriptor is not None:
            os.close(pid_descriptor)

    residual_detected = terminate_residual_descendants(os.getpid())
    if holder_lost:
        print(
            "\nERROR: operation-lock holder died; guarded process group stopped.",
            file=sys.stderr,
        )
        return HOLDER_LOST_EXIT
    if interrupted_signal is not None:
        return 128 + interrupted_signal
    assert child_status is not None
    exit_code = wait_status_to_exit_code(child_status)
    if residual_detected and exit_code == 0:
        raise LockedCommandError(
            "guarded root exited successfully but left live descendants"
        )
    return exit_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch-pid", type=int, required=True)
    parser.add_argument(
        "--signal-grace-seconds",
        type=float,
        default=TERMINATION_GRACE_SECONDS,
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    return args


def main() -> int:
    args = parse_args()
    try:
        return run(
            args.command,
            args.watch_pid,
            signal_grace_seconds=args.signal_grace_seconds,
        )
    except (OSError, LockedCommandError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
