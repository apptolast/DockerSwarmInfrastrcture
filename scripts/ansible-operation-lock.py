#!/usr/bin/env python3
"""Hold, prove and recover the host-global Ansible operation lock."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

DEFAULT_LOCK_PATH = Path("/run/lock/dockerswarm-iac.lock")
DEFAULT_MARKER_PATH = Path("/run/lock/dockerswarm-ansible.marker")
BOOTSTRAP_LOCK_PATH = DEFAULT_LOCK_PATH
BOOTSTRAP_MARKER_PATH = Path("/run/lock/dockerswarm-bootstrap.marker")
DIRECT_MARKER_PATH = Path("/run/lock/dockerswarm-direct.marker")
PRODUCTION_LOCK_UID = 1001
PRODUCTION_LOCK_GID = 1001
DEFAULT_ARCHIVE_DIRECTORY = Path("/var/backups/dockerswarm/ansible-lock-recovery")
SAFE_OPERATION_ID = re.compile(r"^[a-f0-9]{64}$")
SAFE_REVISION = re.compile(r"^[a-f0-9]{40}$")
SAFE_DIGEST = re.compile(r"^[a-f0-9]{64}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")
SAFE_PLAYBOOKS = {
    "platform",
    "host-baseline",
    "preflight-images",
    "edge",
    "workloads",
    "observability",
    "backup",
    "bootstrap-host",
    "site",
}
SAFE_PROFILES = {"production", "acme-staging", "fresh-host"}
SAFE_MODES = {"check", "apply"}
MARKER_KEYS = {
    "schema_version",
    "operation_id",
    "source_revision",
    "contract_sha256",
    "playbook",
    "profile",
    "mode",
    "controller",
    "started_at",
    "holder_pid",
    "holder_uid",
}


class OperationLockError(RuntimeError):
    """The global deployment lock cannot be safely used."""


def require_identifier(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not pattern.fullmatch(value):
        raise OperationLockError(f"{label} has an invalid format")
    return value


def require_metadata(
    operation_id: str,
    source_revision: str,
    contract_sha256: str,
    playbook: str,
    profile: str,
    mode: str,
    controller: str,
) -> None:
    require_identifier(operation_id, SAFE_OPERATION_ID, "operation ID")
    require_identifier(source_revision, SAFE_REVISION, "source revision")
    require_identifier(contract_sha256, SAFE_DIGEST, "contract digest")
    require_identifier(controller, SAFE_NAME, "controller identity")
    if playbook not in SAFE_PLAYBOOKS:
        raise OperationLockError("playbook identity is invalid")
    if profile not in SAFE_PROFILES:
        raise OperationLockError("deployment profile is invalid")
    if profile == "acme-staging" and playbook != "edge":
        raise OperationLockError("ACME staging is valid only for edge")
    if (profile == "fresh-host") != (playbook == "bootstrap-host"):
        raise OperationLockError(
            "fresh-host profile and bootstrap-host operation must be paired"
        )
    if mode not in SAFE_MODES:
        raise OperationLockError("deployment mode is invalid")


def require_lock_directory(path: Path, owner_uid: int, owner_gid: int) -> None:
    if not path.is_absolute() or path.parent == path:
        raise OperationLockError("operation-lock path must be absolute")
    try:
        directory_state = path.parent.lstat()
    except OSError as error:
        raise OperationLockError("cannot inspect operation-lock directory") from error
    if not stat.S_ISDIR(directory_state.st_mode):
        raise OperationLockError("operation-lock parent is not a directory")
    mode = stat.S_IMODE(directory_state.st_mode)
    production_directory = (
        path.parent == Path("/run/lock")
        and directory_state.st_uid == 0
        and directory_state.st_gid == 0
        and mode == 0o1777
    )
    private_test_directory = (
        path.parent != Path("/run/lock")
        and directory_state.st_uid == owner_uid
        and directory_state.st_gid == owner_gid
        and mode == 0o700
    )
    if not (production_directory or private_test_directory):
        raise OperationLockError("operation-lock directory is not trusted")


def require_paths(
    lock_path: Path,
    marker_path: Path,
    owner_uid: int,
    owner_gid: int,
) -> None:
    if lock_path == marker_path or lock_path.parent != marker_path.parent:
        raise OperationLockError("lock and marker paths must be distinct siblings")
    require_lock_directory(lock_path, owner_uid, owner_gid)
    require_lock_directory(marker_path, owner_uid, owner_gid)
    if lock_path.parent == Path("/run/lock"):
        production_contracts = {
            (DEFAULT_LOCK_PATH, DEFAULT_MARKER_PATH): (1001, 1001),
            (BOOTSTRAP_LOCK_PATH, BOOTSTRAP_MARKER_PATH): (0, 0),
        }
        expected_owner = production_contracts.get((lock_path, marker_path))
        if expected_owner is None:
            raise OperationLockError("production operation-lock paths are immutable")
        if (owner_uid, owner_gid) != expected_owner:
            raise OperationLockError("production operation-lock owner is immutable")


def lock_inode_owner(
    path: Path,
    owner_uid: int,
    owner_gid: int,
) -> tuple[int, int]:
    if path == DEFAULT_LOCK_PATH:
        return PRODUCTION_LOCK_UID, PRODUCTION_LOCK_GID
    return owner_uid, owner_gid


def open_lock_file(
    path: Path,
    owner_uid: int,
    owner_gid: int,
    *,
    create: bool,
) -> tuple[int, os.stat_result]:
    require_lock_directory(path, owner_uid, owner_gid)
    expected_uid, expected_gid = lock_inode_owner(
        path,
        owner_uid,
        owner_gid,
    )
    common_flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    created = False
    if create:
        try:
            descriptor = os.open(
                path,
                common_flags | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            created = True
        except FileExistsError:
            try:
                descriptor = os.open(path, common_flags)
            except OSError as error:
                raise OperationLockError("cannot safely open operation lock") from error
        except OSError as error:
            raise OperationLockError("cannot create operation lock") from error
    else:
        try:
            descriptor = os.open(path, common_flags)
        except OSError as error:
            raise OperationLockError(
                "existing operation lock is absent or unsafe"
            ) from error
    try:
        if created:
            if (
                path == DEFAULT_LOCK_PATH
                and os.geteuid() == 0
                and (expected_uid, expected_gid) != (0, 0)
            ):
                os.fchown(descriptor, expected_uid, expected_gid)
            os.fchmod(descriptor, 0o600)
        lock_state = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_state.st_mode)
            or lock_state.st_uid != expected_uid
            or lock_state.st_gid != expected_gid
            or stat.S_IMODE(lock_state.st_mode) != 0o600
            or lock_state.st_nlink != 1
        ):
            raise OperationLockError("operation-lock inode is unsafe")
        return descriptor, lock_state
    except BaseException:
        os.close(descriptor)
        raise


def acquire_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise OperationLockError(
            "another host-global Ansible operation is active"
        ) from error


def refuse_peer_production_marker(
    marker_path: Path,
) -> None:
    production_markers = {
        DEFAULT_MARKER_PATH,
        BOOTSTRAP_MARKER_PATH,
        DIRECT_MARKER_PATH,
    }
    if marker_path not in production_markers:
        return
    for peer in sorted(production_markers - {marker_path}):
        try:
            peer_state = peer.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise OperationLockError(
                "cannot inspect the peer operation marker"
            ) from error
        if not stat.S_ISREG(peer_state.st_mode):
            raise OperationLockError("peer operation marker is unsafe")
        raise OperationLockError(
            "a stale or active peer operation marker exists; recovery is required"
        )


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def marker_bytes(metadata: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            metadata,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


def create_marker(
    path: Path,
    content: bytes,
    owner_uid: int,
    owner_gid: int,
) -> None:
    require_lock_directory(path, owner_uid, owner_gid)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError as error:
        raise OperationLockError(
            "a stale operation marker exists; recovery is required"
        ) from error
    except OSError as error:
        raise OperationLockError("cannot create operation marker") from error
    try:
        os.fchmod(descriptor, 0o600)
        marker_state = os.fstat(descriptor)
        if (
            not stat.S_ISREG(marker_state.st_mode)
            or marker_state.st_uid != owner_uid
            or marker_state.st_gid != owner_gid
            or marker_state.st_nlink != 1
        ):
            raise OperationLockError("new operation marker is unsafe")
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)
    fsync_directory(path.parent)


def decode_json_strict(raw: bytes) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise OperationLockError("operation marker has duplicate keys")
            document[key] = value
        return document

    try:
        decoded = raw.decode("ascii")
        document = json.loads(
            decoded,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OperationLockError("operation marker is not strict JSON") from error
    if not isinstance(document, dict) or set(document) != MARKER_KEYS:
        raise OperationLockError("operation marker schema is invalid")
    return document


def read_marker(
    path: Path,
    owner_uid: int,
    owner_gid: int,
) -> tuple[bytes, dict[str, Any], os.stat_result]:
    require_lock_directory(path, owner_uid, owner_gid)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise OperationLockError("cannot safely open operation marker") from error
    try:
        marker_state = os.fstat(descriptor)
        if (
            not stat.S_ISREG(marker_state.st_mode)
            or marker_state.st_uid != owner_uid
            or marker_state.st_gid != owner_gid
            or stat.S_IMODE(marker_state.st_mode) != 0o600
            or marker_state.st_nlink != 1
            or marker_state.st_size <= 0
            or marker_state.st_size > 4096
        ):
            raise OperationLockError("operation-marker inode is unsafe")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(4097)
    finally:
        os.close(descriptor)
    if len(raw) > 4096:
        raise OperationLockError("operation marker is too large")
    document = decode_json_strict(raw)
    return raw, document, marker_state


def validate_marker(
    document: dict[str, Any],
    *,
    operation_id: str,
    source_revision: str | None = None,
    contract_sha256: str | None = None,
    playbook: str | None = None,
    profile: str | None = None,
    mode: str | None = None,
) -> None:
    if document.get("schema_version") != 1:
        raise OperationLockError("operation marker version is invalid")
    if document.get("operation_id") != operation_id:
        raise OperationLockError("operation marker nonce differs")
    require_identifier(document["operation_id"], SAFE_OPERATION_ID, "operation ID")
    require_identifier(
        document["source_revision"],
        SAFE_REVISION,
        "source revision",
    )
    require_identifier(
        document["contract_sha256"],
        SAFE_DIGEST,
        "contract digest",
    )
    require_identifier(document["controller"], SAFE_NAME, "controller identity")
    if document["playbook"] not in SAFE_PLAYBOOKS:
        raise OperationLockError("marker playbook is invalid")
    if document["profile"] not in SAFE_PROFILES:
        raise OperationLockError("marker profile is invalid")
    if document["mode"] not in SAFE_MODES:
        raise OperationLockError("marker mode is invalid")
    if (
        not isinstance(document["holder_pid"], int)
        or isinstance(document["holder_pid"], bool)
        or document["holder_pid"] <= 1
        or not isinstance(document["holder_uid"], int)
        or isinstance(document["holder_uid"], bool)
        or document["holder_uid"] < 0
    ):
        raise OperationLockError("operation marker holder identity is invalid")
    try:
        started_at = datetime.strptime(
            document["started_at"],
            "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as error:
        raise OperationLockError("operation marker timestamp is invalid") from error
    if started_at > datetime.now(timezone.utc):
        raise OperationLockError("operation marker timestamp is in the future")
    expected_values = {
        "source_revision": source_revision,
        "contract_sha256": contract_sha256,
        "playbook": playbook,
        "profile": profile,
        "mode": mode,
    }
    for key, expected in expected_values.items():
        if expected is not None and document[key] != expected:
            raise OperationLockError(f"operation marker {key} differs")


def process_holds_inode(pid: int, inode_state: os.stat_result) -> bool:
    descriptor_directory = Path("/proc") / str(pid) / "fd"
    try:
        descriptor_paths = list(descriptor_directory.iterdir())
    except OSError as error:
        raise OperationLockError("cannot inspect lock-holder descriptors") from error
    for descriptor_path in descriptor_paths:
        try:
            descriptor_state = descriptor_path.stat()
        except OSError:
            continue
        if (
            descriptor_state.st_dev == inode_state.st_dev
            and descriptor_state.st_ino == inode_state.st_ino
        ):
            return True
    return False


def hold_lock(args: argparse.Namespace, input_stream: BinaryIO) -> None:
    if os.geteuid() != args.owner_uid or os.getegid() != args.owner_gid:
        raise OperationLockError("lock holder must run as the reviewed owner")
    require_metadata(
        args.operation_id,
        args.source_revision,
        args.contract_sha256,
        args.playbook,
        args.profile,
        args.mode,
        args.controller,
    )
    require_paths(
        args.lock_path,
        args.marker_path,
        args.owner_uid,
        args.owner_gid,
    )
    lock_descriptor, _lock_state = open_lock_file(
        args.lock_path,
        args.owner_uid,
        args.owner_gid,
        create=True,
    )
    try:
        acquire_lock(lock_descriptor)
        refuse_peer_production_marker(args.marker_path)
        metadata = {
            "schema_version": 1,
            "operation_id": args.operation_id,
            "source_revision": args.source_revision,
            "contract_sha256": args.contract_sha256,
            "playbook": args.playbook,
            "profile": args.profile,
            "mode": args.mode,
            "controller": args.controller,
            "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "holder_pid": os.getpid(),
            "holder_uid": os.geteuid(),
        }
        expected_raw = marker_bytes(metadata)
        create_marker(
            args.marker_path,
            expected_raw,
            args.owner_uid,
            args.owner_gid,
        )
        print(f"LOCKED:{args.operation_id}", flush=True)
        release = input_stream.readline(4097)
        if release != f"RELEASE:{args.operation_id}\n".encode("ascii"):
            raise OperationLockError(
                "lock-holder control channel ended without exact release"
            )
        observed_raw, document, _marker_state = read_marker(
            args.marker_path,
            args.owner_uid,
            args.owner_gid,
        )
        validate_marker(
            document,
            operation_id=args.operation_id,
            source_revision=args.source_revision,
            contract_sha256=args.contract_sha256,
            playbook=args.playbook,
            profile=args.profile,
            mode=args.mode,
        )
        if observed_raw != expected_raw:
            raise OperationLockError("operation marker changed before release")
        args.marker_path.unlink()
        fsync_directory(args.marker_path.parent)
        print(f"RELEASED:{args.operation_id}", flush=True)
    finally:
        os.close(lock_descriptor)


def prove_lock(args: argparse.Namespace) -> None:
    require_metadata(
        args.operation_id,
        args.source_revision,
        args.contract_sha256,
        args.playbook,
        args.profile,
        args.mode,
        args.controller,
    )
    require_paths(
        args.lock_path,
        args.marker_path,
        args.owner_uid,
        args.owner_gid,
    )
    _raw, document, _marker_state = read_marker(
        args.marker_path,
        args.owner_uid,
        args.owner_gid,
    )
    validate_marker(
        document,
        operation_id=args.operation_id,
        source_revision=args.source_revision,
        contract_sha256=args.contract_sha256,
        playbook=args.playbook,
        profile=args.profile,
        mode=args.mode,
    )
    if document["controller"] != args.controller:
        raise OperationLockError("operation marker controller differs")
    if document["holder_uid"] != args.owner_uid:
        raise OperationLockError("operation marker holder UID differs")

    lock_descriptor, lock_state = open_lock_file(
        args.lock_path,
        args.owner_uid,
        args.owner_gid,
        create=False,
    )
    try:
        try:
            fcntl.flock(
                lock_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            pass
        else:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            raise OperationLockError("operation lock is not actively held")
        if not process_holds_inode(document["holder_pid"], lock_state):
            raise OperationLockError(
                "marker holder PID does not own the operation lock"
            )
    finally:
        os.close(lock_descriptor)
    print(f"PROVEN:{args.operation_id}")


def ancestor_pids(pid: int) -> set[int]:
    ancestors = {pid}
    current = pid
    while current > 1:
        try:
            status_text = (Path("/proc") / str(current) / "status").read_text(
                encoding="ascii"
            )
        except OSError:
            break
        parent_lines = [
            line for line in status_text.splitlines() if line.startswith("PPid:")
        ]
        if len(parent_lines) != 1:
            break
        current = int(parent_lines[0].split()[1])
        if current in ancestors:
            break
        ancestors.add(current)
    return ancestors


def is_mutating_process(arguments: list[str]) -> bool:
    basenames = [Path(argument).name for argument in arguments[:3]]
    if any(
        name
        in {
            "ansible-playbook",
            "apt",
            "apt-get",
            "dpkg",
            "unattended-upgrade",
            "apt.systemd.daily",
        }
        for name in basenames
    ):
        return True
    if any(name.startswith("AnsiballZ_") for name in basenames):
        return True
    if basenames and basenames[0] == "docker" and len(arguments) >= 2:
        return arguments[1] in {"stack", "service", "swarm", "node"}
    return False


def mutating_processes() -> list[tuple[int, str]]:
    excluded = ancestor_pids(os.getpid())
    matches: list[tuple[int, str]] = []
    for candidate in Path("/proc").iterdir():
        if not candidate.name.isdigit():
            continue
        pid = int(candidate.name)
        if pid in excluded:
            continue
        try:
            raw = (candidate / "cmdline").read_bytes()
        except OSError:
            continue
        arguments = [
            item.decode("utf-8", errors="replace")
            for item in raw.rstrip(b"\x00").split(b"\x00")
            if item
        ]
        if arguments and is_mutating_process(arguments):
            matches.append((pid, Path(arguments[0]).name))
    return sorted(matches)


def ensure_archive_directory(path: Path) -> None:
    if path != DEFAULT_ARCHIVE_DIRECTORY:
        try:
            state = path.lstat()
        except FileNotFoundError:
            path.mkdir(mode=0o700)
            state = path.lstat()
        if (
            not stat.S_ISDIR(state.st_mode)
            or state.st_uid != os.geteuid()
            or state.st_gid != os.getegid()
            or stat.S_IMODE(state.st_mode) != 0o700
        ):
            raise OperationLockError("test recovery archive is unsafe")
        return

    parent = path.parent
    try:
        parent_state = parent.lstat()
    except OSError as error:
        raise OperationLockError("backup root is absent or unsafe") from error
    if (
        not stat.S_ISDIR(parent_state.st_mode)
        or parent_state.st_uid != 0
        or parent_state.st_gid != 0
        or stat.S_IMODE(parent_state.st_mode) != 0o700
    ):
        raise OperationLockError("backup root must be root:root 0700")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    state = path.lstat()
    if (
        not stat.S_ISDIR(state.st_mode)
        or state.st_uid != 0
        or state.st_gid != 0
        or stat.S_IMODE(state.st_mode) != 0o700
    ):
        raise OperationLockError("recovery archive must be root:root 0700")


def recover_lock(args: argparse.Namespace, *, require_root: bool = True) -> None:
    require_identifier(args.operation_id, SAFE_OPERATION_ID, "operation ID")
    require_paths(
        args.lock_path,
        args.marker_path,
        args.owner_uid,
        args.owner_gid,
    )
    if args.apply and require_root and os.geteuid() != 0:
        raise OperationLockError("lock recovery apply requires root")
    lock_descriptor, _lock_state = open_lock_file(
        args.lock_path,
        args.owner_uid,
        args.owner_gid,
        create=False,
    )
    try:
        acquire_lock(lock_descriptor)
        raw, document, _marker_state = read_marker(
            args.marker_path,
            args.owner_uid,
            args.owner_gid,
        )
        validate_marker(document, operation_id=args.operation_id)
        active = mutating_processes()
        if active:
            identities = ", ".join(f"{pid}:{name}" for pid, name in active)
            raise OperationLockError(
                f"mutating host processes are still active: {identities}"
            )
        digest = hashlib.sha256(raw).hexdigest()
        confirmation = (
            f"RECOVER_ANSIBLE_LOCK:{args.operation_id}:{digest}:" "CONTROLLER_STOPPED"
        )
        print(
            "Operator assertion required: the original controller is stopped "
            "and a maintenance window is active."
        )
        print(f"Recovery confirmation: {confirmation}")
        if not args.apply:
            print("Dry run only; the stale marker remains.")
            return
        if args.confirm != confirmation:
            raise OperationLockError(
                "recovery confirmation differs from the inspected marker"
            )
        ensure_archive_directory(args.archive_directory)
        archive = args.archive_directory / (f"{args.operation_id}.{digest}.json")
        try:
            archive_descriptor = os.open(
                archive,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            existing = archive.read_bytes()
            if existing != raw:
                raise OperationLockError("existing recovery evidence differs")
        else:
            try:
                os.fchmod(archive_descriptor, 0o600)
                with os.fdopen(
                    archive_descriptor,
                    "wb",
                    closefd=False,
                ) as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(archive_descriptor)
            fsync_directory(args.archive_directory)
        args.marker_path.unlink()
        fsync_directory(args.marker_path.parent)
        print(f"Recovered stale marker; evidence: {archive}")
    finally:
        os.close(lock_descriptor)


def add_common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--marker-path", type=Path, default=DEFAULT_MARKER_PATH)
    parser.add_argument("--owner-uid", type=int, default=1001)
    parser.add_argument("--owner-gid", type=int, default=1001)


def add_operation_metadata(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--playbook", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--mode", choices=sorted(SAFE_MODES), required=True)
    parser.add_argument("--controller", required=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    hold = subparsers.add_parser("hold")
    add_common_paths(hold)
    add_operation_metadata(hold)

    prove = subparsers.add_parser("prove")
    add_common_paths(prove)
    add_operation_metadata(prove)

    recover = subparsers.add_parser("recover")
    add_common_paths(recover)
    recover.add_argument("--operation-id", required=True)
    recover.add_argument(
        "--archive-directory",
        type=Path,
        default=DEFAULT_ARCHIVE_DIRECTORY,
    )
    recover.add_argument("--apply", action="store_true")
    recover.add_argument("--confirm", default="")
    return parser.parse_args()


def main() -> int:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    args = parse_args()
    try:
        if args.command == "hold":
            hold_lock(args, sys.stdin.buffer)
        elif args.command == "prove":
            prove_lock(args)
        else:
            recover_lock(args)
    except (OSError, OperationLockError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
