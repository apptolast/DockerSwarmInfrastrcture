#!/usr/bin/env python3
"""Serialize direct host mutations with the reviewed Ansible/bootstrap lock.

Direct commands never trust a boolean bypass.  A child launched by ``run`` must
prove the direct marker, the live flock holder, and its supervisor ancestry.
Commands launched from an Ansible operation must instead prove every field of
the reviewed Ansible marker and the actively held flock.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import functools
import hashlib
import importlib.util
import json
import os
import re
import secrets
import signal
import stat
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, NoReturn

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
OPERATION_LOCK_HELPER = SCRIPT_DIRECTORY / "ansible-operation-lock.py"
LOCKED_COMMAND_RUNNER = SCRIPT_DIRECTORY / "run-locked-command.py"
DEFAULT_LOCK_PATH = Path("/run/lock/dockerswarm-iac.lock")
DEFAULT_DIRECT_MARKER_PATH = Path("/run/lock/dockerswarm-direct.marker")
DEFAULT_ANSIBLE_MARKER_PATH = Path("/run/lock/dockerswarm-ansible.marker")
DEFAULT_BOOTSTRAP_MARKER_PATH = Path("/run/lock/dockerswarm-bootstrap.marker")
DEFAULT_RECOVERY_ARCHIVE = Path("/var/backups/dockerswarm/direct-lock-recovery")
PRODUCTION_LOCK_UID = 1001
PRODUCTION_LOCK_GID = 1001
PROOF_SCOPE_ENV = "DOCKERSWARM_IAC_LOCK_SCOPE"
DIRECT_PROOF_FIELDS = {
    "operation_id": "DOCKERSWARM_IAC_OPERATION_ID",
    "operation": "DOCKERSWARM_IAC_OPERATION",
    "holder_pid": "DOCKERSWARM_IAC_HOLDER_PID",
    "supervisor_pid": "DOCKERSWARM_IAC_SUPERVISOR_PID",
}
ANSIBLE_PROOF_FIELDS = {
    "operation_id": "DOCKERSWARM_IAC_OPERATION_ID",
    "source_revision": "DOCKERSWARM_IAC_SOURCE_REVISION",
    "contract_sha256": "DOCKERSWARM_IAC_CONTRACT_SHA256",
    "playbook": "DOCKERSWARM_IAC_PLAYBOOK",
    "profile": "DOCKERSWARM_IAC_PROFILE",
    "mode": "DOCKERSWARM_IAC_MODE",
    "controller": "DOCKERSWARM_IAC_CONTROLLER",
}
DIRECT_MARKER_KEYS = {
    "schema_version",
    "operation_id",
    "operation",
    "started_at",
    "holder_pid",
    "holder_uid",
    "supervisor_pid",
    "supervisor_uid",
}
SAFE_OPERATION_ID = re.compile(r"^[a-f0-9]{64}$")
SAFE_OPERATION = re.compile(r"^[a-z][-a-z0-9_.:]{1,127}$")
SAFE_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T" r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
RECOVERY_PREFIX = "RECOVER_DIRECT_OPERATION"
MARKER_RETAINED_EXIT = 124
DEFAULT_SIGNAL_GRACE_SECONDS = 120.0
BACKUP_SIGNAL_GRACE_SECONDS = 2100.0
OPERATION_SIGNAL_GRACE_SECONDS = {
    "backupctl-application": BACKUP_SIGNAL_GRACE_SECONDS,
    "backupctl-rehearse": BACKUP_SIGNAL_GRACE_SECONDS,
    "backupctl-restore": BACKUP_SIGNAL_GRACE_SECONDS,
    "backupctl-swarm-state": BACKUP_SIGNAL_GRACE_SECONDS,
    "backupctl-verify": BACKUP_SIGNAL_GRACE_SECONDS,
    "n8n-workflows-publish": 1900.0,
    "n8n-workflows-rollback": 1900.0,
}


class HostGlobalLockError(RuntimeError):
    """The shared host mutation lock cannot be used safely."""


def fail(message: str) -> NoReturn:
    raise HostGlobalLockError(message)


def load_sibling_module(name: str, path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        fail(f"required lock component is absent or a symlink: {path.name}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        fail(f"cannot load required lock component: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@functools.cache
def operation_lock_module() -> Any:
    return load_sibling_module(
        "dockerswarm_ansible_operation_lock",
        OPERATION_LOCK_HELPER,
    )


@functools.cache
def locked_command_module() -> Any:
    return load_sibling_module(
        "dockerswarm_locked_command_runner",
        LOCKED_COMMAND_RUNNER,
    )


def canonical_json(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def strict_json(raw: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                fail("direct-operation marker contains duplicate keys")
            result[key] = value
        return result

    try:
        document = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HostGlobalLockError(
            "direct-operation marker is not strict JSON"
        ) from error
    if not isinstance(document, dict):
        fail("direct-operation marker must be a JSON object")
    return document


def require_production_paths(lock_path: Path, marker_path: Path) -> None:
    if lock_path == DEFAULT_LOCK_PATH and marker_path == DEFAULT_DIRECT_MARKER_PATH:
        return
    if lock_path.parent == Path("/run/lock") or marker_path.parent == Path("/run/lock"):
        fail("production direct-operation paths are immutable")


def expected_marker_owner(marker_path: Path) -> tuple[int, int]:
    if marker_path == DEFAULT_DIRECT_MARKER_PATH:
        return (0, 0)
    return (os.geteuid(), os.getegid())


def require_direct_metadata(
    document: dict[str, Any],
    *,
    operation_id: str | None = None,
    operation: str | None = None,
    holder_pid: int | None = None,
    supervisor_pid: int | None = None,
) -> None:
    if set(document) != DIRECT_MARKER_KEYS or document.get("schema_version") != 1:
        fail("direct-operation marker schema is invalid")
    if not isinstance(
        document.get("operation_id"), str
    ) or not SAFE_OPERATION_ID.fullmatch(document["operation_id"]):
        fail("direct-operation ID is invalid")
    if not isinstance(document.get("operation"), str) or not SAFE_OPERATION.fullmatch(
        document["operation"]
    ):
        fail("direct-operation name is invalid")
    if not isinstance(document.get("started_at"), str) or not SAFE_TIMESTAMP.fullmatch(
        document["started_at"]
    ):
        fail("direct-operation timestamp is invalid")
    for field in (
        "holder_pid",
        "holder_uid",
        "supervisor_pid",
        "supervisor_uid",
    ):
        value = document.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            fail(f"direct-operation {field} is invalid")
    if document["holder_pid"] <= 1 or document["supervisor_pid"] <= 1:
        fail("direct-operation process identity is invalid")
    expected = {
        "operation_id": operation_id,
        "operation": operation,
        "holder_pid": holder_pid,
        "supervisor_pid": supervisor_pid,
    }
    for key, value in expected.items():
        if value is not None and document[key] != value:
            fail(f"direct-operation marker {key} differs")


def open_direct_marker(
    path: Path,
) -> tuple[int, bytes, dict[str, Any], os.stat_result]:
    owner_uid, owner_gid = expected_marker_owner(path)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise HostGlobalLockError(
            "cannot safely open direct-operation marker"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != owner_uid
            or metadata.st_gid != owner_gid
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > 4096
        ):
            fail("direct-operation marker inode is unsafe")
        raw = os.read(descriptor, 4097)
        if len(raw) > 4096:
            fail("direct-operation marker is too large")
        document = strict_json(raw)
        require_direct_metadata(document)
        path_state = path.lstat()
        if path_state.st_dev != metadata.st_dev or path_state.st_ino != metadata.st_ino:
            fail("direct-operation marker path changed during inspection")
        return descriptor, raw, document, metadata
    except BaseException:
        os.close(descriptor)
        raise


def create_direct_marker(path: Path, document: dict[str, Any]) -> bytes:
    lock_helper = operation_lock_module()
    owner_uid, owner_gid = expected_marker_owner(path)
    lock_helper.require_lock_directory(path, owner_uid, owner_gid)
    raw = canonical_json(document)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError as error:
        raise HostGlobalLockError(
            "a stale direct-operation marker exists; recovery is required"
        ) from error
    except OSError as error:
        raise HostGlobalLockError("cannot create direct-operation marker") from error
    try:
        os.fchmod(descriptor, 0o600)
        if (os.geteuid(), os.getegid()) != (owner_uid, owner_gid):
            os.fchown(descriptor, owner_uid, owner_gid)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != owner_uid
            or metadata.st_gid != owner_gid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            fail("new direct-operation marker inode is unsafe")
        os.write(descriptor, raw)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)
    lock_helper.fsync_directory(path.parent)
    return raw


def production_peer_markers(marker_path: Path) -> tuple[Path, ...]:
    if marker_path.parent != Path("/run/lock"):
        return ()
    return (
        DEFAULT_ANSIBLE_MARKER_PATH,
        DEFAULT_BOOTSTRAP_MARKER_PATH,
        DEFAULT_DIRECT_MARKER_PATH,
    )


def refuse_existing_markers(marker_path: Path) -> None:
    candidates = (
        production_peer_markers(marker_path)
        if marker_path.parent == Path("/run/lock")
        else (marker_path,)
    )
    for candidate in candidates:
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise HostGlobalLockError(
                "cannot inspect host-global operation marker"
            ) from error
        fail(f"an active or stale operation marker exists: {candidate.name}")


def open_and_acquire_global_lock(
    lock_path: Path,
    marker_path: Path,
    *,
    create: bool,
) -> tuple[int, os.stat_result]:
    require_production_paths(lock_path, marker_path)
    lock_helper = operation_lock_module()
    owner_uid = PRODUCTION_LOCK_UID if lock_path == DEFAULT_LOCK_PATH else os.geteuid()
    owner_gid = PRODUCTION_LOCK_GID if lock_path == DEFAULT_LOCK_PATH else os.getegid()
    try:
        descriptor, metadata = lock_helper.open_lock_file(
            lock_path,
            owner_uid,
            owner_gid,
            create=create,
        )
    except lock_helper.OperationLockError as error:
        raise HostGlobalLockError(str(error)) from error
    try:
        try:
            lock_helper.acquire_lock(descriptor)
        except lock_helper.OperationLockError as error:
            raise HostGlobalLockError(str(error)) from error
        path_state = lock_path.lstat()
        if (
            path_state.st_dev != metadata.st_dev
            or path_state.st_ino != metadata.st_ino
            or path_state.st_nlink != 1
        ):
            fail("host-global lock path changed during acquisition")
        refuse_existing_markers(marker_path)
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def verify_lock_still_named(
    descriptor: int,
    lock_path: Path,
    expected: os.stat_result,
) -> None:
    descriptor_state = os.fstat(descriptor)
    try:
        path_state = lock_path.lstat()
    except OSError as error:
        raise HostGlobalLockError(
            "host-global lock path disappeared while held"
        ) from error
    if (
        descriptor_state.st_dev != expected.st_dev
        or descriptor_state.st_ino != expected.st_ino
        or descriptor_state.st_nlink != 1
        or path_state.st_dev != expected.st_dev
        or path_state.st_ino != expected.st_ino
        or path_state.st_nlink != 1
    ):
        fail("host-global lock inode was replaced while held")


def direct_marker_document(
    operation: str,
    operation_id: str,
    supervisor_pid: int,
) -> dict[str, Any]:
    if not SAFE_OPERATION.fullmatch(operation):
        fail("direct-operation name is invalid")
    if not SAFE_OPERATION_ID.fullmatch(operation_id):
        fail("direct-operation ID is invalid")
    return {
        "schema_version": 1,
        "operation_id": operation_id,
        "operation": operation,
        "started_at": __import__("time").strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            __import__("time").gmtime(),
        ),
        "holder_pid": os.getpid(),
        "holder_uid": os.geteuid(),
        "supervisor_pid": supervisor_pid,
        "supervisor_uid": os.getuid(),
    }


def hold_direct_lock(
    *,
    operation: str,
    operation_id: str,
    supervisor_pid: int,
    control_descriptor: int,
    status_descriptor: int,
    lock_path: Path = DEFAULT_LOCK_PATH,
    marker_path: Path = DEFAULT_DIRECT_MARKER_PATH,
) -> int:
    if lock_path == DEFAULT_LOCK_PATH and os.geteuid() != 0:
        fail("direct production mutations must run as root")
    lock_descriptor, lock_state = open_and_acquire_global_lock(
        lock_path,
        marker_path,
        create=True,
    )
    marker_created = False
    try:
        document = direct_marker_document(
            operation,
            operation_id,
            supervisor_pid,
        )
        expected_raw = create_direct_marker(marker_path, document)
        marker_created = True
        os.write(status_descriptor, f"LOCKED:{operation_id}\n".encode("ascii"))
        release = os.read(control_descriptor, 4097)
        if release != f"RELEASE:{operation_id}\n".encode("ascii"):
            return MARKER_RETAINED_EXIT
        verify_lock_still_named(lock_descriptor, lock_path, lock_state)
        marker_descriptor, raw, observed, marker_state = open_direct_marker(marker_path)
        os.close(marker_descriptor)
        require_direct_metadata(
            observed,
            operation_id=operation_id,
            operation=operation,
            holder_pid=os.getpid(),
            supervisor_pid=supervisor_pid,
        )
        if raw != expected_raw:
            fail("direct-operation marker changed before release")
        current_marker = marker_path.lstat()
        if (
            current_marker.st_dev != marker_state.st_dev
            or current_marker.st_ino != marker_state.st_ino
        ):
            fail("direct-operation marker inode changed before release")
        marker_path.unlink()
        marker_created = False
        operation_lock_module().fsync_directory(marker_path.parent)
        os.write(
            status_descriptor,
            f"RELEASED:{operation_id}\n".encode("ascii"),
        )
        return 0
    finally:
        os.close(lock_descriptor)
        if marker_created:
            # A missing or malformed exact release is fail-closed.  Recovery
            # archives and removes this evidence explicitly.
            pass


def read_exact_environment(fields: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, environment_name in fields.items():
        value = os.environ.get(environment_name)
        if value is None or not value:
            fail(f"required lock-proof field is absent: {environment_name}")
        values[key] = value
    return values


def pid_value(raw: str, label: str) -> int:
    if not raw.isdigit() or int(raw) <= 1:
        fail(f"{label} is invalid")
    return int(raw)


def parent_pid(pid: int) -> int:
    try:
        status = (Path("/proc") / str(pid) / "status").read_text(encoding="ascii")
    except OSError as error:
        raise HostGlobalLockError("cannot inspect proof process") from error
    lines = [line for line in status.splitlines() if line.startswith("PPid:")]
    if len(lines) != 1:
        fail("proof process parent is ambiguous")
    return int(lines[0].split()[1])


def process_arguments(pid: int) -> list[str]:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError as error:
        raise HostGlobalLockError("cannot inspect proof process command") from error
    return [
        item.decode("utf-8", errors="replace")
        for item in raw.rstrip(b"\x00").split(b"\x00")
        if item
    ]


def is_ansible_execution_process(arguments: Sequence[str]) -> bool:
    if not arguments:
        return False
    basenames = {Path(argument).name for argument in arguments[:4]}
    if "ansible-playbook" in basenames or any(
        name.startswith("AnsiballZ_") for name in basenames
    ):
        return True
    return any(
        "/.ansible/tmp/ansible-tmp-" in argument or "/.ansible/cp/" in argument
        for argument in arguments
    )


def require_ansible_execution_ancestry(pid: int) -> None:
    inspected = operation_lock_module().ancestor_pids(pid)
    for candidate in inspected:
        try:
            arguments = process_arguments(candidate)
        except HostGlobalLockError:
            continue
        if is_ansible_execution_process(arguments):
            return
    fail("nested lock proof is outside a live Ansible execution tree")


def prove_direct_environment(
    operation: str,
    *,
    lock_path: Path = DEFAULT_LOCK_PATH,
    marker_path: Path = DEFAULT_DIRECT_MARKER_PATH,
) -> None:
    if not SAFE_OPERATION.fullmatch(operation):
        fail("direct-operation name is invalid")
    fields = read_exact_environment(DIRECT_PROOF_FIELDS)
    guarded_operation = fields["operation"]
    if not SAFE_OPERATION.fullmatch(guarded_operation):
        fail("direct lock-proof operation is invalid")
    holder_pid = pid_value(fields["holder_pid"], "direct holder PID")
    supervisor_pid = pid_value(
        fields["supervisor_pid"],
        "direct supervisor PID",
    )
    marker_descriptor, _raw, document, _marker_state = open_direct_marker(marker_path)
    os.close(marker_descriptor)
    require_direct_metadata(
        document,
        operation_id=fields["operation_id"],
        operation=guarded_operation,
        holder_pid=holder_pid,
        supervisor_pid=supervisor_pid,
    )
    lock_helper = operation_lock_module()
    descriptor, lock_state = lock_helper.open_lock_file(
        lock_path,
        PRODUCTION_LOCK_UID if lock_path == DEFAULT_LOCK_PATH else os.geteuid(),
        PRODUCTION_LOCK_GID if lock_path == DEFAULT_LOCK_PATH else os.getegid(),
        create=False,
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            fail("direct host-global lock is not actively held")
        if not lock_helper.process_holds_inode(holder_pid, lock_state):
            fail("direct marker holder does not own the host-global lock")
        if supervisor_pid not in lock_helper.ancestor_pids(os.getpid()):
            fail("direct lock proof is outside the guarded supervisor tree")
        if parent_pid(holder_pid) != supervisor_pid:
            fail("direct lock holder is detached from its supervisor")
        verify_lock_still_named(descriptor, lock_path, lock_state)
    finally:
        os.close(descriptor)


def prove_ansible_environment(
    scope: str,
    *,
    lock_path: Path = DEFAULT_LOCK_PATH,
    marker_path: Path | None = None,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> None:
    if scope not in {"ansible", "bootstrap"}:
        fail("nested operation-lock scope is invalid")
    fields = read_exact_environment(ANSIBLE_PROOF_FIELDS)
    expected_marker_path = (
        DEFAULT_ANSIBLE_MARKER_PATH
        if scope == "ansible"
        else DEFAULT_BOOTSTRAP_MARKER_PATH
    )
    marker_path = marker_path or expected_marker_path
    expected_owner = (1001, 1001) if scope == "ansible" else (0, 0)
    if owner_uid is None:
        owner_uid = expected_owner[0]
    if owner_gid is None:
        owner_gid = expected_owner[1]
    lock_helper = operation_lock_module()
    _raw, document, _marker_state = lock_helper.read_marker(
        marker_path,
        owner_uid,
        owner_gid,
    )
    lock_helper.validate_marker(
        document,
        operation_id=fields["operation_id"],
        source_revision=fields["source_revision"],
        contract_sha256=fields["contract_sha256"],
        playbook=fields["playbook"],
        profile=fields["profile"],
        mode=fields["mode"],
    )
    if document["controller"] != fields["controller"]:
        fail("nested operation marker controller differs")
    descriptor, lock_state = lock_helper.open_lock_file(
        lock_path,
        owner_uid,
        owner_gid,
        create=False,
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            fail("nested host-global lock is not actively held")
        if not lock_helper.process_holds_inode(
            document["holder_pid"],
            lock_state,
        ):
            fail("nested marker holder does not own the host-global lock")
        require_ansible_execution_ancestry(os.getpid())
        verify_lock_still_named(descriptor, lock_path, lock_state)
    finally:
        os.close(descriptor)


def prove_or_none(operation: str) -> bool:
    scope = os.environ.get(PROOF_SCOPE_ENV)
    if scope is None:
        proof_names = set(DIRECT_PROOF_FIELDS.values()) | set(
            ANSIBLE_PROOF_FIELDS.values()
        )
        if any(name in os.environ for name in proof_names):
            fail("partial lock proof exists without an explicit scope")
        return False
    if scope == "direct":
        prove_direct_environment(operation)
        return True
    if scope in {"ansible", "bootstrap"}:
        prove_ansible_environment(scope)
        return True
    fail("operation-lock proof scope is invalid")


def locate_helper(caller: Path) -> Path:
    candidates = (
        caller.resolve().parent / "host_global_operation_lock.py",
        SCRIPT_DIRECTORY / "host_global_operation_lock.py",
        caller.resolve().parents[1] / "scripts" / "host_global_operation_lock.py",
        caller.resolve().parents[2] / "scripts" / "host_global_operation_lock.py",
    )
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    fail("host-global operation-lock helper is unavailable")


def ensure_mutation_lock(
    operation: str,
    command: Sequence[str],
    *,
    caller: Path,
) -> None:
    if prove_or_none(operation):
        return
    helper = locate_helper(caller)
    os.execv(
        sys.executable,
        [
            sys.executable,
            str(helper),
            "run",
            "--operation",
            operation,
            "--",
            *command,
        ],
    )


@contextlib.contextmanager
def patched_environment(values: dict[str, str]) -> Iterator[None]:
    proof_names = (
        {PROOF_SCOPE_ENV}
        | set(DIRECT_PROOF_FIELDS.values())
        | set(ANSIBLE_PROOF_FIELDS.values())
    )
    original = {key: os.environ.get(key) for key in proof_names}
    for key in proof_names:
        os.environ.pop(key, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for key in proof_names:
            os.environ.pop(key, None)
        for key, value in original.items():
            if value is None:
                continue
            else:
                os.environ[key] = value


def wait_status(descriptor: int) -> str:
    chunks: list[bytes] = []
    while sum(len(chunk) for chunk in chunks) <= 4096:
        chunk = os.read(descriptor, 4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks).decode("ascii", errors="replace").strip()


def run_direct(
    operation: str,
    command: Sequence[str],
    *,
    lock_path: Path = DEFAULT_LOCK_PATH,
    marker_path: Path = DEFAULT_DIRECT_MARKER_PATH,
) -> int:
    if lock_path == DEFAULT_LOCK_PATH and os.geteuid() != 0:
        fail("direct production mutations must run as root")
    if not command:
        fail("a guarded command is required")
    if not SAFE_OPERATION.fullmatch(operation):
        fail("direct-operation name is invalid")
    operation_id = secrets.token_hex(32)
    supervisor_pid = os.getpid()
    control_read, control_write = os.pipe2(os.O_CLOEXEC)
    status_read, status_write = os.pipe2(os.O_CLOEXEC)
    holder_pid = os.fork()
    if holder_pid == 0:
        try:
            os.close(control_write)
            os.close(status_read)
            locked_runner = locked_command_module()
            locked_runner.set_parent_death_signal(supervisor_pid)
            result = hold_direct_lock(
                operation=operation,
                operation_id=operation_id,
                supervisor_pid=supervisor_pid,
                control_descriptor=control_read,
                status_descriptor=status_write,
                lock_path=lock_path,
                marker_path=marker_path,
            )
        except BaseException as error:
            print(f"ERROR: {error}", file=sys.stderr)
            result = 1
        finally:
            os.close(control_read)
            os.close(status_write)
        os._exit(result)

    os.close(control_read)
    os.close(status_write)
    released = False
    holder_reaped = False
    control_open = True
    status_open = True
    try:
        ready = wait_status(status_read)
        if ready != f"LOCKED:{operation_id}":
            fail("direct lock holder failed before acquisition")
        proof_environment = {
            PROOF_SCOPE_ENV: "direct",
            DIRECT_PROOF_FIELDS["operation_id"]: operation_id,
            DIRECT_PROOF_FIELDS["operation"]: operation,
            DIRECT_PROOF_FIELDS["holder_pid"]: str(holder_pid),
            DIRECT_PROOF_FIELDS["supervisor_pid"]: str(supervisor_pid),
        }
        runner_pid = os.fork()
        if runner_pid == 0:
            try:
                runner = locked_command_module()
                with patched_environment(proof_environment):
                    result = runner.run(
                        list(command),
                        holder_pid,
                        signal_grace_seconds=OPERATION_SIGNAL_GRACE_SECONDS.get(
                            operation,
                            DEFAULT_SIGNAL_GRACE_SECONDS,
                        ),
                    )
            except BaseException as error:
                print(f"ERROR: {error}", file=sys.stderr)
                result = 126
            os._exit(result)

        interrupted_signal: int | None = None
        old_handlers: dict[int, signal.Handlers] = {}

        def forward_signal(signal_number: int, _frame: object) -> None:
            nonlocal interrupted_signal
            interrupted_signal = signal_number
            try:
                os.kill(runner_pid, signal_number)
            except ProcessLookupError:
                pass

        try:
            old_handlers = {
                signal_number: signal.signal(signal_number, forward_signal)
                for signal_number in (
                    signal.SIGINT,
                    signal.SIGTERM,
                    signal.SIGHUP,
                    signal.SIGQUIT,
                )
            }
            _waited_runner, runner_status = os.waitpid(runner_pid, 0)
        finally:
            for signal_number, old_handler in old_handlers.items():
                signal.signal(signal_number, old_handler)
        if os.WIFEXITED(runner_status):
            result = os.WEXITSTATUS(runner_status)
        elif os.WIFSIGNALED(runner_status):
            result = 128 + os.WTERMSIG(runner_status)
        else:
            fail("guarded command supervisor returned an unknown status")
        if interrupted_signal is not None and result == 0:
            result = 128 + interrupted_signal
        runner = locked_command_module()
        if result == runner.HOLDER_LOST_EXIT:
            fail("direct lock holder died during the guarded mutation")
        if result != 0:
            # A non-zero result may follow a partial write or a delivered
            # signal.  Closing without RELEASE intentionally retains the
            # marker until explicit, evidenced recovery.
            os.close(control_write)
            control_open = False
            wait_status(status_read)
            os.close(status_read)
            status_open = False
            os.waitpid(holder_pid, 0)
            holder_reaped = True
            return result
        os.write(
            control_write,
            f"RELEASE:{operation_id}\n".encode("ascii"),
        )
        released_status = wait_status(status_read)
        waited_pid, holder_status = os.waitpid(holder_pid, 0)
        holder_reaped = True
        if (
            waited_pid != holder_pid
            or holder_status != 0
            or released_status != f"RELEASED:{operation_id}"
        ):
            fail("direct lock holder did not release cleanly")
        released = True
        return result
    finally:
        if control_open:
            os.close(control_write)
        if status_open:
            os.close(status_read)
        if not holder_reaped:
            try:
                os.waitpid(holder_pid, 0)
            except ChildProcessError:
                pass


def recovery_confirmation(raw: bytes, document: dict[str, Any]) -> str:
    return (
        f"{RECOVERY_PREFIX}:{document['operation_id']}:"
        f"{hashlib.sha256(raw).hexdigest()}:CONTROLLER_STOPPED"
    )


def recover_direct_marker(
    *,
    apply: bool,
    confirmation: str | None,
    lock_path: Path = DEFAULT_LOCK_PATH,
    marker_path: Path = DEFAULT_DIRECT_MARKER_PATH,
    archive_directory: Path = DEFAULT_RECOVERY_ARCHIVE,
) -> str:
    if lock_path == DEFAULT_LOCK_PATH and os.geteuid() != 0:
        fail("production direct-marker recovery requires root")
    marker_descriptor, raw, document, marker_state = open_direct_marker(marker_path)
    os.close(marker_descriptor)
    # Recovery intentionally opens the shared inode without the normal
    # peer-marker refusal: this exact marker is the evidence being recovered.
    helper = operation_lock_module()
    owner_uid = PRODUCTION_LOCK_UID if lock_path == DEFAULT_LOCK_PATH else os.geteuid()
    owner_gid = PRODUCTION_LOCK_GID if lock_path == DEFAULT_LOCK_PATH else os.getegid()
    descriptor, lock_state = helper.open_lock_file(
        lock_path,
        owner_uid,
        owner_gid,
        create=False,
    )
    try:
        helper.acquire_lock(descriptor)
        verify_lock_still_named(descriptor, lock_path, lock_state)
        supervisor_proc = Path("/proc") / str(document["supervisor_pid"])
        if supervisor_proc.exists():
            fail("direct-operation supervisor is still present")
        holder_proc = Path("/proc") / str(document["holder_pid"])
        if holder_proc.exists():
            try:
                holder_active = helper.process_holds_inode(
                    document["holder_pid"],
                    lock_state,
                )
            except helper.OperationLockError:
                if holder_proc.exists():
                    raise
                holder_active = False
            if holder_active:
                fail("direct-operation holder still owns the host-global lock")
        expected = recovery_confirmation(raw, document)
        if not apply:
            return expected
        if confirmation != expected:
            fail("direct-marker recovery confirmation differs")
        archive_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        archive_state = archive_directory.lstat()
        if (
            not stat.S_ISDIR(archive_state.st_mode)
            or stat.S_IMODE(archive_state.st_mode) != 0o700
            or archive_state.st_uid != os.geteuid()
        ):
            fail("direct-marker recovery archive is unsafe")
        archive = archive_directory / (
            f"direct-{document['operation_id']}-"
            f"{hashlib.sha256(raw).hexdigest()}.json"
        )
        archive_descriptor = os.open(
            archive,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(archive_descriptor, raw)
            os.fsync(archive_descriptor)
        finally:
            os.close(archive_descriptor)
        current = marker_path.lstat()
        if (
            current.st_dev != marker_state.st_dev
            or current.st_ino != marker_state.st_ino
        ):
            fail("direct-operation marker changed during recovery")
        marker_path.unlink()
        helper.fsync_directory(marker_path.parent)
        helper.fsync_directory(archive_directory)
        return str(archive)
    finally:
        os.close(descriptor)


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--operation", required=True)
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    prove_parser = subparsers.add_parser("prove")
    prove_parser.add_argument("--operation", required=True)
    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--apply", action="store_true")
    recover_parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    if args.action == "run" and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_arguments(argv or sys.argv[1:])
    try:
        if args.action == "run":
            return run_direct(args.operation, args.command)
        if args.action == "prove":
            if not prove_or_none(args.operation):
                fail("no operation-lock proof environment is present")
            print("PROVEN")
            return 0
        result = recover_direct_marker(
            apply=args.apply,
            confirmation=args.confirm,
        )
        print(result)
        return 0
    except (HostGlobalLockError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
