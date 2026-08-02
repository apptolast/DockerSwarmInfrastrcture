#!/usr/bin/env python3
"""Promote, recover, and roll back immutable workload runtime generations.

The production CLI has no path overrides.  It operates only on the reviewed
``/srv/dockerswarm`` layout and delegates every mutation to the host-global
operation lock.  Importable classes accept an isolated layout solely so the
transaction protocol can be tested without touching production.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import signal
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn, Protocol

import yaml

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIRECTORY.parents[1]
HOST_HELPER_DIRECTORY = REPOSITORY_ROOT / "scripts"
if str(HOST_HELPER_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(HOST_HELPER_DIRECTORY))
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

import backup_safety  # noqa: E402
import finalize_restore  # noqa: E402
from host_global_operation_lock import (  # noqa: E402
    HostGlobalLockError,
    ensure_mutation_lock,
    prove_or_none,
)

PRODUCTION_STATE_ROOT = Path("/srv/dockerswarm")
PRODUCTION_GENERATIONS_ROOT = PRODUCTION_STATE_ROOT / "runtime-generations"
PRODUCTION_EVIDENCE_ROOT = Path("/var/lib/dockerswarm-migration/final-freeze-evidence")
PRODUCTION_ALLOWED_SIGNERS = Path(
    "/etc/dockerswarm/migration/runtime-promotion.allowed-signers"
)
PRODUCTION_CATALOG = REPOSITORY_ROOT / "config/services.yml"
PRODUCTION_SECRET_CATALOG = REPOSITORY_ROOT / "stacks/workloads/secrets.yml"
PRODUCTION_PLATFORM = REPOSITORY_ROOT / "config/platform.yml"
RESTORE_COMPOSE_FILES = (
    REPOSITORY_ROOT / "migration/compose/restore.yml",
    REPOSITORY_ROOT / "migration/compose/restore-vectors-081.yml",
)
HOST_LOCK_OPERATION = "migration-runtime-promotion"
ATTESTATION_IDENTITY = "apptolast-runtime-promotion"
ATTESTATION_NAMESPACE = "runtime-promotion@apptolast.com"
BASELINE_SOURCE_BACKUP = "apptolast-data-20260723T225340Z"
MAX_ATTESTATION_LIFETIME = timedelta(hours=6)
MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_JSON_BYTES = 1024 * 1024
MAX_SIGNATURE_BYTES = 64 * 1024
MAX_COMMAND_OUTPUT = 1024 * 1024
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2

SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
COMMIT_RE = re.compile(r"^[a-f0-9]{40,64}$")
SOURCE_BACKUP_RE = re.compile(r"^apptolast-data-(?P<stamp>[0-9]{8}T[0-9]{6}Z)$")
GENERATION_RE = SOURCE_BACKUP_RE
TRANSACTION_RE = re.compile(r"^[a-f0-9]{64}$")
EVENT_FILE_RE = re.compile(r"^(?P<sequence>[0-9]{6})\.json$")
PENDING_EVENT_RE = re.compile(
    r"^\.pending-(?P<sequence>[0-9]{6})-(?P<nonce>[a-f0-9]{32})\.json$"
)
SAFE_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?\+00:00$"
)
TERMINAL_PHASES = {
    "promotion-completed",
    "rollback-completed",
    "aborted-before-exchange",
    "candidate-import-completed",
    "candidate-import-aborted",
}
ATTESTATION_KEYS = {
    "schemaVersion",
    "signerIdentity",
    "legacyIPv4",
    "targetIPv4",
    "sourceBackup",
    "catalogSha256",
    "platformSha256",
    "runtimeManifestSha256",
    "recoveryManifestSha256",
    "readyMarkerSha256",
    "secretCatalogSha256",
    "secretSourcesSha256",
    "runtimeTreeSha256",
    "legacyWritersStopped",
    "finalBackupVerified",
    "legacyStoppedAt",
    "backupStartedAt",
    "backupCompletedAt",
    "issuedAt",
    "expiresAt",
    "sourceCommit",
}


class PromotionError(RuntimeError):
    """A promotion precondition or transaction invariant failed."""


class PromotionInterrupted(BaseException):
    """A handled signal interrupted a transaction."""

    def __init__(self, signal_number: int):
        super().__init__(f"interrupted by signal {signal_number}")
        self.signal_number = signal_number


class CrashInjection(BaseException):
    """Test-only abrupt-crash injection that deliberately skips cleanup."""


def fail(message: str) -> NoReturn:
    raise PromotionError(message)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def strict_json(raw: bytes, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                fail(f"{label} contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PromotionError(f"{label} is not strict ASCII JSON") from error
    if not isinstance(value, dict):
        fail(f"{label} is not a JSON object")
    return value


def parse_utc(value: object, label: str) -> datetime:
    if not isinstance(value, str) or SAFE_TIMESTAMP_RE.fullmatch(value) is None:
        fail(f"{label} is not a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise PromotionError(f"{label} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        fail(f"{label} must use UTC")
    return parsed


def source_backup_time(value: object, label: str) -> datetime:
    match = SOURCE_BACKUP_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        fail(f"{label} is not a canonical sourceBackup")
    try:
        return datetime.strptime(
            match.group("stamp"),
            "%Y%m%dT%H%M%SZ",
        ).replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise PromotionError(f"{label} contains an invalid timestamp") from error


def require_generation(value: str) -> str:
    source_backup_time(value, "generation")
    return value


def file_identity(status: os.stat_result) -> dict[str, int]:
    return {
        "device": status.st_dev,
        "inode": status.st_ino,
    }


def same_identity(
    status: os.stat_result,
    expected: dict[str, Any],
) -> bool:
    return status.st_dev == expected.get("device") and status.st_ino == expected.get(
        "inode"
    )


def open_directory(
    path: Path,
    label: str,
    *,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
    exact_mode: int | None = None,
) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise PromotionError(f"cannot safely open {label}: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_nlink < 2
            or (expected_uid is not None and metadata.st_uid != expected_uid)
            or (expected_gid is not None and metadata.st_gid != expected_gid)
            or (exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode)
        ):
            fail(f"{label} directory inode is unsafe")
        current = path.lstat()
        if (
            current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
            or not stat.S_ISDIR(current.st_mode)
        ):
            fail(f"{label} path changed while opening")
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def validate_path_chain(
    path: Path,
    label: str,
    *,
    production: bool,
    leaf_mode: int | None = None,
    leaf_uid: int | None = None,
    leaf_gid: int | None = None,
) -> os.stat_result:
    if not path.is_absolute() or path == Path("/"):
        fail(f"{label} path must be absolute and non-root")
    parts = path.parts
    current = Path(parts[0])
    for index, part in enumerate(parts[1:], start=1):
        current /= part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise PromotionError(f"cannot inspect {label} path chain") from error
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_nlink < 2:
            fail(f"{label} path chain contains a non-directory")
        is_leaf = index == len(parts) - 1
        if production:
            if current == Path("/srv") or current == Path("/var"):
                if metadata.st_uid != 0 or metadata.st_gid != 0:
                    fail(f"{label} trusted parent is not root-owned")
                if metadata.st_mode & 0o022:
                    fail(f"{label} trusted parent is writable by group/others")
            elif current.parts[:2] in {
                ("/", "srv"),
                ("/", "var"),
                ("/", "etc"),
            }:
                if metadata.st_uid != 0 or metadata.st_gid != 0:
                    fail(f"{label} production chain is not root-owned")
                if not is_leaf and metadata.st_mode & 0o022:
                    fail(f"{label} production parent is writable by group/others")
        if is_leaf:
            if leaf_uid is not None and metadata.st_uid != leaf_uid:
                fail(f"{label} leaf UID is invalid")
            if leaf_gid is not None and metadata.st_gid != leaf_gid:
                fail(f"{label} leaf GID is invalid")
            if leaf_mode is not None and stat.S_IMODE(metadata.st_mode) != leaf_mode:
                fail(f"{label} leaf mode is invalid")
    return path.lstat()


def read_regular(
    path: Path,
    label: str,
    *,
    max_bytes: int,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
    exact_mode: int | None = None,
) -> tuple[bytes, os.stat_result]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise PromotionError(f"cannot safely open {label}: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size < 0
            or metadata.st_size > max_bytes
            or (expected_uid is not None and metadata.st_uid != expected_uid)
            or (expected_gid is not None and metadata.st_gid != expected_gid)
            or (exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode)
        ):
            fail(f"{label} inode is unsafe")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                fail(f"{label} changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            fail(f"{label} grew while reading")
        current = path.lstat()
        after = os.fstat(descriptor)
        if (
            current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
            or after.st_dev != metadata.st_dev
            or after.st_ino != metadata.st_ino
            or after.st_size != metadata.st_size
            or after.st_mtime_ns != metadata.st_mtime_ns
            or after.st_ctime_ns != metadata.st_ctime_ns
            or after.st_nlink != 1
        ):
            fail(f"{label} changed while reading")
        return b"".join(chunks), metadata
    finally:
        os.close(descriptor)


def sha256_regular(path: Path, label: str, *, max_bytes: int = 1024**4) -> str:
    raw, _metadata = read_regular(path, label, max_bytes=max_bytes)
    return sha256_bytes(raw)


def fsync_directory(path: Path) -> None:
    descriptor, _metadata = open_directory(path, f"fsync directory {path}")
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_restore_validator() -> Any:
    path = REPOSITORY_ROOT / "scripts/validate-workloads-restore-marker.py"
    specification = importlib.util.spec_from_file_location(
        "dockerswarm_validate_workloads_restore_marker",
        path,
    )
    if specification is None or specification.loader is None:
        fail("restore-marker validator cannot be loaded")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Layout:
    state_root: Path
    evidence_root: Path
    allowed_signers: Path
    production: bool
    owner_uid: int
    owner_gid: int

    @property
    def canonical(self) -> Path:
        return self.state_root / "services"

    @property
    def generations(self) -> Path:
        return self.state_root / "runtime-generations"

    @property
    def candidates(self) -> Path:
        return self.generations / "candidates"

    @property
    def history(self) -> Path:
        return self.generations / "history"

    @property
    def incoming(self) -> Path:
        return self.generations / "incoming"

    @property
    def imports(self) -> Path:
        return self.generations / "import-transactions"

    @property
    def transactions(self) -> Path:
        return self.generations / "transactions"

    def candidate(self, generation: str) -> Path:
        require_generation(generation)
        return self.candidates / generation / "services"

    def historical(self, generation: str) -> Path:
        require_generation(generation)
        return self.history / generation / "services"

    def incoming_generation(self, generation: str) -> Path:
        require_generation(generation)
        return self.incoming / generation / "services"

    def attestation(self, generation: str) -> Path:
        require_generation(generation)
        return self.evidence_root / f"{generation}.json"

    def signature(self, generation: str) -> Path:
        require_generation(generation)
        return self.evidence_root / f"{generation}.json.sig"

    @classmethod
    def production_layout(cls) -> Layout:
        return cls(
            state_root=PRODUCTION_STATE_ROOT,
            evidence_root=PRODUCTION_EVIDENCE_ROOT,
            allowed_signers=PRODUCTION_ALLOWED_SIGNERS,
            production=True,
            owner_uid=0,
            owner_gid=0,
        )


@dataclass(frozen=True)
class GenerationEvidence:
    source_backup: str
    root_device: int
    root_inode: int
    catalog_sha256: str
    platform_sha256: str
    runtime_manifest_sha256: str
    recovery_manifest_sha256: str
    ready_marker_sha256: str
    secret_catalog_sha256: str
    secret_sources_sha256: str
    runtime_tree_sha256: str


@dataclass(frozen=True)
class AttestationEvidence:
    document_sha256: str
    signature_sha256: str
    source_commit: str
    expires_at: str


@dataclass(frozen=True)
class QuiescenceEvidence:
    swarm_state: str
    stacks: tuple[str, ...]
    services: tuple[str, ...]
    containers: tuple[str, ...]
    restore_compose_containers: tuple[str, ...]
    process_references: tuple[str, ...]

    def digest(self) -> str:
        return sha256_bytes(canonical_json(asdict(self)))


@dataclass(frozen=True)
class PromotionPlan:
    operation: str
    canonical: GenerationEvidence
    source: GenerationEvidence
    source_slot: str
    preserve_slot: str
    attestation: AttestationEvidence | None
    source_commit: str
    quiescence_sha256: str

    def document(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "operation": self.operation,
            "canonical": asdict(self.canonical),
            "source": asdict(self.source),
            "sourceSlot": self.source_slot,
            "preserveSlot": self.preserve_slot,
            "attestation": (
                asdict(self.attestation) if self.attestation is not None else None
            ),
            "sourceCommit": self.source_commit,
            "quiescenceSha256": self.quiescence_sha256,
        }

    def digest(self) -> str:
        return sha256_bytes(canonical_json(self.document()))

    def confirmation(self) -> str:
        action = (
            "PROMOTE_RUNTIME_GENERATION"
            if self.operation == "promotion"
            else "ROLLBACK_RUNTIME_GENERATION"
        )
        return (
            f"{action}:{self.source.source_backup}:"
            f"{self.digest()}:NO_WRITERS_CONFIRMED"
        )


@dataclass(frozen=True)
class CandidateImportPlan:
    canonical: GenerationEvidence
    incoming: GenerationEvidence
    source_slot: str
    target_slot: str
    source_commit: str
    quiescence_sha256: str

    def document(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "operation": "candidate-import",
            "canonical": asdict(self.canonical),
            "incoming": asdict(self.incoming),
            "sourceSlot": self.source_slot,
            "targetSlot": self.target_slot,
            "sourceCommit": self.source_commit,
            "quiescenceSha256": self.quiescence_sha256,
        }

    def digest(self) -> str:
        return sha256_bytes(canonical_json(self.document()))

    def confirmation(self) -> str:
        return (
            f"IMPORT_RUNTIME_CANDIDATE:{self.incoming.source_backup}:"
            f"{self.digest()}:NO_WRITERS_CONFIRMED"
        )


class RuntimeProbe(Protocol):
    def inspect(self, protected_roots: Sequence[Path]) -> QuiescenceEvidence:
        """Return fail-closed runtime and process evidence."""


class SignatureVerifier(Protocol):
    def verify(
        self,
        generation: GenerationEvidence,
        source_commit: str,
        *,
        now: datetime,
    ) -> AttestationEvidence:
        """Verify and bind the signed external freeze attestation."""


def hash_tree(root: Path) -> tuple[str, os.stat_result]:
    """Hash a same-filesystem tree through dirfds without following links."""

    root_descriptor, root_state = open_directory(root, "runtime generation")
    digest = hashlib.sha256()
    digest.update(b"dockerswarm-runtime-tree-v1\0")

    def update_record(
        entry_type: bytes,
        relative: str,
        metadata: os.stat_result,
        content_digest: bytes = b"",
    ) -> None:
        encoded = relative.encode("utf-8", errors="strict")
        fields = (
            entry_type
            + b"\0"
            + encoded
            + b"\0"
            + str(stat.S_IMODE(metadata.st_mode)).encode("ascii")
            + b"\0"
            + str(metadata.st_uid).encode("ascii")
            + b"\0"
            + str(metadata.st_gid).encode("ascii")
            + b"\0"
            + str(metadata.st_size).encode("ascii")
            + b"\0"
            + content_digest
            + b"\n"
        )
        digest.update(fields)

    def walk(descriptor: int, relative: str) -> None:
        try:
            names = sorted(os.listdir(descriptor))
        except OSError as error:
            raise PromotionError("cannot enumerate runtime generation") from error
        if len(names) != len(set(names)):
            fail("runtime generation contains duplicate directory entries")
        for name in names:
            if (
                not isinstance(name, str)
                or name in {"", ".", ".."}
                or "/" in name
                or "\x00" in name
            ):
                fail("runtime generation contains an unsafe name")
            child_relative = f"{relative}/{name}" if relative else name
            try:
                before = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise PromotionError(
                    f"cannot inspect runtime entry: {child_relative}"
                ) from error
            if before.st_dev != root_state.st_dev:
                fail("runtime generation crosses a filesystem boundary")
            if stat.S_ISDIR(before.st_mode):
                if before.st_nlink < 2:
                    fail(f"runtime directory link count is unsafe: {child_relative}")
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                try:
                    opened = os.fstat(child)
                    if (
                        opened.st_dev != before.st_dev
                        or opened.st_ino != before.st_ino
                        or not stat.S_ISDIR(opened.st_mode)
                    ):
                        fail(f"runtime directory was replaced: {child_relative}")
                    update_record(b"d", child_relative, opened)
                    walk(child, child_relative)
                    after = os.stat(
                        name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        after.st_dev != opened.st_dev
                        or after.st_ino != opened.st_ino
                        or after.st_ctime_ns != opened.st_ctime_ns
                        or after.st_mtime_ns != opened.st_mtime_ns
                    ):
                        fail(
                            f"runtime directory changed while hashing: "
                            f"{child_relative}"
                        )
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                fail(f"runtime entry type/link count is unsafe: {child_relative}")
            child = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            try:
                opened = os.fstat(child)
                if (
                    opened.st_dev != before.st_dev
                    or opened.st_ino != before.st_ino
                    or opened.st_nlink != 1
                    or not stat.S_ISREG(opened.st_mode)
                ):
                    fail(f"runtime file was replaced: {child_relative}")
                content = hashlib.sha256()
                while True:
                    chunk = os.read(child, 1024 * 1024)
                    if not chunk:
                        break
                    content.update(chunk)
                after_fd = os.fstat(child)
                after_path = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (
                    after_fd.st_dev != opened.st_dev
                    or after_fd.st_ino != opened.st_ino
                    or after_fd.st_size != opened.st_size
                    or after_fd.st_ctime_ns != opened.st_ctime_ns
                    or after_fd.st_mtime_ns != opened.st_mtime_ns
                    or after_fd.st_nlink != 1
                    or after_path.st_dev != opened.st_dev
                    or after_path.st_ino != opened.st_ino
                ):
                    fail(f"runtime file changed while hashing: {child_relative}")
                update_record(
                    b"f",
                    child_relative,
                    opened,
                    content.hexdigest().encode("ascii"),
                )
            finally:
                os.close(child)

    try:
        update_record(b"d", ".", root_state)
        walk(root_descriptor, "")
        current = root.lstat()
        after_root = os.fstat(root_descriptor)
        if (
            current.st_dev != root_state.st_dev
            or current.st_ino != root_state.st_ino
            or after_root.st_dev != root_state.st_dev
            or after_root.st_ino != root_state.st_ino
            or after_root.st_ctime_ns != root_state.st_ctime_ns
            or after_root.st_mtime_ns != root_state.st_mtime_ns
        ):
            fail("runtime root changed while hashing")
        return digest.hexdigest(), root_state
    finally:
        os.close(root_descriptor)


def secret_sources_digest(
    services_root: Path,
    secret_catalog: dict[str, Any],
) -> str:
    entries = secret_catalog.get("workloads_secrets")
    if not isinstance(entries, list):
        fail("workload secret catalog is invalid")
    records: list[dict[str, str]] = []
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("source_file"), str):
            fail("workload secret catalog entry is invalid")
        name = entry["source_file"]
        if name in names or name in {"", ".", ".."} or "/" in name or "\\" in name:
            fail("workload secret source name is unsafe")
        names.add(name)
        path = services_root / "secrets/files" / name
        raw, metadata = read_regular(
            path,
            f"secret source {name}",
            max_bytes=16 * 1024 * 1024,
            exact_mode=0o600,
        )
        records.append(
            {
                "name": name,
                "sha256": sha256_bytes(raw),
                "uid": str(metadata.st_uid),
                "gid": str(metadata.st_gid),
            }
        )
    identity_name = finalize_restore.SECRET_IDENTITY_KEY_FILE
    identity_raw, identity_state = read_regular(
        services_root / "secrets/files" / identity_name,
        "secret identity key",
        max_bytes=finalize_restore.SECRET_IDENTITY_KEY_SIZE,
        exact_mode=0o600,
    )
    records.append(
        {
            "name": identity_name,
            "sha256": sha256_bytes(identity_raw),
            "uid": str(identity_state.st_uid),
            "gid": str(identity_state.st_gid),
        }
    )
    return sha256_bytes(
        canonical_json(
            {
                "schemaVersion": 1,
                "sources": sorted(records, key=lambda item: item["name"]),
            }
        )
    )


class GenerationValidator:
    def __init__(
        self,
        *,
        catalog_path: Path,
        secret_catalog_path: Path,
        platform_path: Path,
        canonical_services_root: Path,
        enforce_owners: bool,
    ):
        self.catalog_path = catalog_path
        self.secret_catalog_path = secret_catalog_path
        self.platform_path = platform_path
        self.canonical_services_root = canonical_services_root
        self.enforce_owners = enforce_owners
        self.restore_validator = load_restore_validator()

    def validate(self, services_root: Path) -> GenerationEvidence:
        root_digest, root_state = hash_tree(services_root)
        catalog_raw, _catalog_state = read_regular(
            self.catalog_path,
            "service catalog",
            max_bytes=MAX_JSON_BYTES,
        )
        secret_catalog_raw, _secret_catalog_state = read_regular(
            self.secret_catalog_path,
            "workload secret catalog",
            max_bytes=MAX_JSON_BYTES,
        )
        platform_raw, _platform_state = read_regular(
            self.platform_path,
            "platform contract",
            max_bytes=MAX_JSON_BYTES,
        )
        try:
            catalog = yaml.safe_load(catalog_raw.decode("utf-8"))
            secret_catalog = yaml.safe_load(secret_catalog_raw.decode("utf-8"))
            platform = yaml.safe_load(platform_raw.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as error:
            raise PromotionError("versioned runtime contracts are invalid") from error
        if not all(
            isinstance(value, dict) for value in (catalog, secret_catalog, platform)
        ):
            fail("versioned runtime contracts must be mappings")

        manifest_path = services_root / "runtime-manifest.json"
        marker_path = services_root / "restore-state/workloads-ready-v2.json"
        recovery_path = services_root / "recovery/SHA256SUMS"
        identity_path = (
            services_root / "secrets/files" / finalize_restore.SECRET_IDENTITY_KEY_FILE
        )
        manifest_raw, _manifest_state = read_regular(
            manifest_path,
            "runtime manifest",
            max_bytes=MAX_JSON_BYTES,
            exact_mode=0o600,
        )
        marker_raw, _marker_state = read_regular(
            marker_path,
            "workloads-ready-v2 marker",
            max_bytes=MAX_JSON_BYTES,
            exact_mode=0o600,
        )
        recovery_raw, _recovery_state = read_regular(
            recovery_path,
            "recovery checksum manifest",
            max_bytes=128 * 1024 * 1024,
        )
        identity_raw, _identity_state = read_regular(
            identity_path,
            "secret identity key",
            max_bytes=finalize_restore.SECRET_IDENTITY_KEY_SIZE,
            exact_mode=0o600,
        )
        manifest = strict_json(manifest_raw, "runtime manifest")
        marker = strict_json(marker_raw, "workloads-ready-v2 marker")

        try:
            finalize_restore.validate_catalog(
                self.catalog_path,
                self.canonical_services_root,
            )
            finalize_restore.validate_secret_catalog(
                self.secret_catalog_path,
                services_root,
                manifest,
            )
            finalize_restore.validate_restore_phase_markers(services_root)
            finalize_restore.validate_datasets(
                services_root,
                enforce_owners=self.enforce_owners,
            )
            backup_safety.verify_checksum_tree(
                services_root / "recovery",
                recovery_path,
            )
            self.restore_validator.validate_marker(
                marker,
                catalog,
                catalog_sha256=sha256_bytes(catalog_raw),
                runtime_manifest=manifest,
                runtime_manifest_sha256=sha256_bytes(manifest_raw),
                recovery_manifest_sha256=sha256_bytes(recovery_raw),
                secret_identity_key_sha256=sha256_bytes(identity_raw),
            )
        except (
            backup_safety.SafetyError,
            self.restore_validator.MarkerError,
        ) as error:
            raise PromotionError(
                f"runtime generation validation failed: {error}"
            ) from error

        source_backup = manifest.get("sourceBackup")
        source_backup_time(source_backup, "runtime sourceBackup")
        secret_digest = secret_sources_digest(services_root, secret_catalog)

        # Re-hash the complete tree after all validators.  Equality closes the
        # validation-to-plan window and catches changes made by a concurrent
        # process even when an individual contract file still looks valid.
        final_tree_digest, final_root_state = hash_tree(services_root)
        if (
            final_tree_digest != root_digest
            or final_root_state.st_dev != root_state.st_dev
            or final_root_state.st_ino != root_state.st_ino
        ):
            fail("runtime generation changed during validation")
        return GenerationEvidence(
            source_backup=str(source_backup),
            root_device=root_state.st_dev,
            root_inode=root_state.st_ino,
            catalog_sha256=sha256_bytes(catalog_raw),
            platform_sha256=sha256_bytes(platform_raw),
            runtime_manifest_sha256=sha256_bytes(manifest_raw),
            recovery_manifest_sha256=sha256_bytes(recovery_raw),
            ready_marker_sha256=sha256_bytes(marker_raw),
            secret_catalog_sha256=sha256_bytes(secret_catalog_raw),
            secret_sources_sha256=secret_digest,
            runtime_tree_sha256=root_digest,
        )


class OpenSSHAttestationVerifier:
    def __init__(
        self,
        *,
        layout: Layout,
        platform_path: Path,
        ssh_keygen: Path = Path("/usr/bin/ssh-keygen"),
    ):
        self.layout = layout
        self.platform_path = platform_path
        self.ssh_keygen = ssh_keygen

    def verify(
        self,
        generation: GenerationEvidence,
        source_commit: str,
        *,
        now: datetime,
    ) -> AttestationEvidence:
        raw, _attestation_state = read_regular(
            self.layout.attestation(generation.source_backup),
            "runtime promotion attestation",
            max_bytes=MAX_JSON_BYTES,
            expected_uid=self.layout.owner_uid,
            expected_gid=self.layout.owner_gid,
            exact_mode=0o600,
        )
        document = strict_json(raw, "runtime promotion attestation")
        if raw != canonical_json(document):
            fail("runtime promotion attestation is not canonical JSON")
        if set(document) != ATTESTATION_KEYS or document.get("schemaVersion") != 1:
            fail("runtime promotion attestation schema is invalid")
        if document.get("signerIdentity") != ATTESTATION_IDENTITY:
            fail("runtime promotion attestation signer identity is invalid")
        for field in (
            "legacyWritersStopped",
            "finalBackupVerified",
        ):
            if document.get(field) is not True:
                fail(f"runtime promotion attestation does not assert {field}")

        platform_raw, _platform_state = read_regular(
            self.platform_path,
            "platform contract",
            max_bytes=MAX_JSON_BYTES,
        )
        try:
            platform = yaml.safe_load(platform_raw.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as error:
            raise PromotionError("platform contract is invalid") from error
        if not isinstance(platform, dict):
            fail("platform contract is not a mapping")
        expected_legacy = platform.get("platform_dns_legacy_ipv4")
        expected_target = platform.get("platform_public_ipv4")
        try:
            ipaddress.ip_address(document.get("legacyIPv4"))
            ipaddress.ip_address(document.get("targetIPv4"))
        except ValueError as error:
            raise PromotionError("attestation IP address is invalid") from error
        if (
            document.get("legacyIPv4") != expected_legacy
            or document.get("targetIPv4") != expected_target
            or expected_legacy == expected_target
        ):
            fail("attestation host identities differ from platform contract")

        expected_hashes = {
            "catalogSha256": generation.catalog_sha256,
            "platformSha256": generation.platform_sha256,
            "runtimeManifestSha256": generation.runtime_manifest_sha256,
            "recoveryManifestSha256": generation.recovery_manifest_sha256,
            "readyMarkerSha256": generation.ready_marker_sha256,
            "secretCatalogSha256": generation.secret_catalog_sha256,
            "secretSourcesSha256": generation.secret_sources_sha256,
            "runtimeTreeSha256": generation.runtime_tree_sha256,
        }
        for field, expected in expected_hashes.items():
            if (
                document.get(field) != expected
                or SHA256_RE.fullmatch(str(document.get(field))) is None
            ):
                fail(f"attestation {field} is stale or invalid")
        if (
            document.get("sourceBackup") != generation.source_backup
            or document.get("sourceCommit") != source_commit
            or COMMIT_RE.fullmatch(str(document.get("sourceCommit"))) is None
        ):
            fail("attestation source identity is stale")

        stopped_at = parse_utc(document.get("legacyStoppedAt"), "legacyStoppedAt")
        backup_started = parse_utc(
            document.get("backupStartedAt"),
            "backupStartedAt",
        )
        backup_completed = parse_utc(
            document.get("backupCompletedAt"),
            "backupCompletedAt",
        )
        issued_at = parse_utc(document.get("issuedAt"), "issuedAt")
        expires_at = parse_utc(document.get("expiresAt"), "expiresAt")
        backup_stamp = source_backup_time(
            generation.source_backup,
            "attested sourceBackup",
        )
        if not (
            stopped_at
            <= backup_started
            <= backup_stamp
            <= backup_completed
            <= issued_at
            < expires_at
        ):
            fail("attestation stop/final-backup time ordering is invalid")
        if expires_at - issued_at > MAX_ATTESTATION_LIFETIME:
            fail("attestation validity interval is too long")
        if issued_at > now + MAX_CLOCK_SKEW or now >= expires_at:
            fail("attestation is not currently valid")

        allowed_descriptor, allowed_state = self._open_verification_file(
            self.layout.allowed_signers,
            "runtime promotion allowed-signers",
            MAX_JSON_BYTES,
            exact_mode=None,
        )
        signature_descriptor, signature_state = self._open_verification_file(
            self.layout.signature(generation.source_backup),
            "runtime promotion signature",
            MAX_SIGNATURE_BYTES,
            exact_mode=0o600,
        )
        try:
            executable = self.ssh_keygen.lstat()
            if (
                not stat.S_ISREG(executable.st_mode)
                or executable.st_uid != 0
                or executable.st_mode & 0o022
            ):
                fail("ssh-keygen executable is unsafe")
            command = [
                str(self.ssh_keygen),
                "-Y",
                "verify",
                "-f",
                f"/proc/self/fd/{allowed_descriptor}",
                "-I",
                ATTESTATION_IDENTITY,
                "-n",
                ATTESTATION_NAMESPACE,
                "-s",
                f"/proc/self/fd/{signature_descriptor}",
            ]
            result = subprocess.run(
                command,
                input=raw,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
                close_fds=True,
                pass_fds=(allowed_descriptor, signature_descriptor),
            )
            if (
                len(result.stdout) > MAX_COMMAND_OUTPUT
                or len(result.stderr) > MAX_COMMAND_OUTPUT
            ):
                fail("ssh-keygen verification output exceeded its limit")
            if result.returncode != 0:
                fail("runtime promotion attestation signature is invalid")
            self._revalidate_verification_file(
                self.layout.allowed_signers,
                allowed_descriptor,
                allowed_state,
                "runtime promotion allowed-signers",
            )
            self._revalidate_verification_file(
                self.layout.signature(generation.source_backup),
                signature_descriptor,
                signature_state,
                "runtime promotion signature",
            )
            os.lseek(signature_descriptor, 0, os.SEEK_SET)
            signature_raw = os.read(signature_descriptor, MAX_SIGNATURE_BYTES + 1)
        finally:
            os.close(allowed_descriptor)
            os.close(signature_descriptor)
        return AttestationEvidence(
            document_sha256=sha256_bytes(raw),
            signature_sha256=sha256_bytes(signature_raw),
            source_commit=source_commit,
            expires_at=str(document["expiresAt"]),
        )

    def _open_verification_file(
        self,
        path: Path,
        label: str,
        limit: int,
        *,
        exact_mode: int | None,
    ) -> tuple[int, os.stat_result]:
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
        except OSError as error:
            raise PromotionError(f"cannot safely open {label}") from error
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > limit
            or metadata.st_uid != self.layout.owner_uid
            or metadata.st_gid != self.layout.owner_gid
            or metadata.st_mode & 0o022
            or (exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode)
        ):
            os.close(descriptor)
            fail(f"{label} inode is unsafe")
        current = path.lstat()
        if current.st_dev != metadata.st_dev or current.st_ino != metadata.st_ino:
            os.close(descriptor)
            fail(f"{label} path changed while opening")
        return descriptor, metadata

    @staticmethod
    def _revalidate_verification_file(
        path: Path,
        descriptor: int,
        expected: os.stat_result,
        label: str,
    ) -> None:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
            or opened.st_size != expected.st_size
            or opened.st_ctime_ns != expected.st_ctime_ns
            or opened.st_mtime_ns != expected.st_mtime_ns
            or current.st_dev != expected.st_dev
            or current.st_ino != expected.st_ino
        ):
            fail(f"{label} changed during signature verification")


class ProductionRuntimeProbe:
    def __init__(self, docker: Path = Path("/usr/bin/docker")):
        self.docker = docker

    def _run(self, arguments: Sequence[str], label: str) -> tuple[str, ...]:
        executable = self.docker.lstat()
        if (
            not stat.S_ISREG(executable.st_mode)
            or executable.st_uid != 0
            or executable.st_mode & 0o022
        ):
            fail("Docker CLI executable is unsafe")
        try:
            result = subprocess.run(
                [str(self.docker), *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
                env={
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PromotionError(f"cannot inspect {label}") from error
        if (
            len(result.stdout) > MAX_COMMAND_OUTPUT
            or len(result.stderr) > MAX_COMMAND_OUTPUT
        ):
            fail(f"{label} output exceeded its limit")
        if result.returncode != 0:
            fail(f"Docker failed while inspecting {label}")
        try:
            lines = tuple(
                sorted(
                    line.strip()
                    for line in result.stdout.decode("utf-8").splitlines()
                    if line.strip()
                )
            )
        except UnicodeDecodeError as error:
            raise PromotionError(f"{label} output is not UTF-8") from error
        return lines

    def inspect(self, protected_roots: Sequence[Path]) -> QuiescenceEvidence:
        swarm = self._run(
            [
                "info",
                "--format",
                "{{.Swarm.LocalNodeState}}|" "{{.Swarm.ControlAvailable}}",
            ],
            "Swarm manager state",
        )
        if swarm != ("active|true",):
            fail("target is not the active Swarm manager")
        stacks = self._run(["stack", "ls", "--format", "{{.Name}}"], "stacks")
        services = self._run(["service", "ls", "--quiet"], "services")
        containers = self._run(
            ["container", "ls", "--all", "--quiet"],
            "containers",
        )
        restore_containers: list[str] = []
        for compose_path in RESTORE_COMPOSE_FILES:
            restore_containers.extend(
                self._run(
                    [
                        "compose",
                        "-f",
                        str(compose_path),
                        "ps",
                        "--all",
                        "--quiet",
                    ],
                    f"restore Compose {compose_path.name}",
                )
            )
        process_references = inspect_process_references(protected_roots)
        evidence = QuiescenceEvidence(
            swarm_state=swarm[0],
            stacks=stacks,
            services=services,
            containers=containers,
            restore_compose_containers=tuple(sorted(restore_containers)),
            process_references=process_references,
        )
        if (
            evidence.stacks
            or evidence.services
            or evidence.containers
            or evidence.restore_compose_containers
            or evidence.process_references
        ):
            fail("runtime is not quiescent; writers or workload objects remain")
        return evidence


def path_is_within(target: str, roots: Sequence[Path]) -> bool:
    target = target.removesuffix(" (deleted)")
    if not target.startswith("/"):
        return False
    candidate = Path(target)
    for root in roots:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def inspect_process_references(roots: Sequence[Path]) -> tuple[str, ...]:
    """Fail closed if any process can currently reference a protected tree."""

    normalized = tuple(Path(os.path.abspath(root)) for root in roots)
    references: list[str] = []
    own_pid = os.getpid()
    try:
        process_entries = tuple(Path("/proc").iterdir())
    except OSError as error:
        raise PromotionError("cannot enumerate /proc") from error
    for process in process_entries:
        if not process.name.isdigit() or int(process.name) == own_pid:
            continue
        pid = int(process.name)
        try:
            for field in ("cwd", "root", "exe"):
                try:
                    target = os.readlink(process / field)
                except FileNotFoundError:
                    break
                except PermissionError:
                    # El proceso existe pero no podemos leer a que apunta, de
                    # modo que no es posible demostrar que no referencia el
                    # arbol protegido. Se registra como referencia bloqueante
                    # en lugar de abortar: el llamante ya falla en cerrado
                    # ante cualquier referencia, y asi el motivo queda
                    # enumerado en la evidencia. Un /proc sin `hidepid` deja
                    # visibles procesos ajenos ilegibles, que antes hacian
                    # estallar el recorrido entero.
                    references.append(f"{pid}:{field}:unreadable")
                    break
                except OSError as error:
                    if not process.exists():
                        break
                    raise PromotionError(
                        f"cannot inspect process {pid} {field}"
                    ) from error
                if path_is_within(target, normalized):
                    references.append(f"{pid}:{field}")
            else:
                fd_directory = process / "fd"
                try:
                    descriptors = tuple(fd_directory.iterdir())
                except FileNotFoundError:
                    continue
                except PermissionError:
                    references.append(f"{pid}:fd:unreadable")
                    continue
                except OSError as error:
                    if not process.exists():
                        continue
                    raise PromotionError(
                        f"cannot inspect process {pid} descriptors"
                    ) from error
                for descriptor in descriptors:
                    try:
                        target = os.readlink(descriptor)
                    except FileNotFoundError:
                        continue
                    except PermissionError:
                        references.append(f"{pid}:fd:unreadable")
                        break
                    except OSError as error:
                        if not process.exists():
                            break
                        raise PromotionError(
                            f"cannot inspect process {pid} descriptor"
                        ) from error
                    if path_is_within(target, normalized):
                        references.append(f"{pid}:fd:{descriptor.name}")
                        break
                try:
                    command = (process / "cmdline").read_bytes()
                except FileNotFoundError:
                    continue
                except PermissionError:
                    references.append(f"{pid}:cmdline:unreadable")
                    continue
                except OSError as error:
                    if not process.exists():
                        continue
                    raise PromotionError(
                        f"cannot inspect process {pid} command"
                    ) from error
                restore_names = (
                    b"restore.yml",
                    b"restore-vectors-081.yml",
                )
                if (
                    b"docker" in command
                    and b"compose" in command
                    and any(name in command for name in restore_names)
                ):
                    references.append(f"{pid}:restore-compose-command")
        except FileNotFoundError:
            continue
    return tuple(sorted(set(references)))


def repository_state() -> tuple[str, bool]:
    command_base = ["/usr/bin/git", "-C", str(REPOSITORY_ROOT)]
    try:
        head = subprocess.run(
            [*command_base, "rev-parse", "--verify", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        status_result = subprocess.run(
            [
                *command_base,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PromotionError("cannot inspect repository state") from error
    if (
        head.returncode != 0
        or status_result.returncode != 0
        or len(head.stdout) > 256
        or len(status_result.stdout) > MAX_COMMAND_OUTPUT
    ):
        fail("repository state cannot be trusted")
    try:
        revision = head.stdout.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise PromotionError("repository revision is not ASCII") from error
    if COMMIT_RE.fullmatch(revision) is None:
        fail("repository revision is invalid")
    return revision, status_result.stdout == b""


def renameat2(
    old_parent_fd: int,
    old_name: str,
    new_parent_fd: int,
    new_name: str,
    flags: int,
) -> None:
    for name in (old_name, new_name):
        if not name or name in {".", ".."} or "/" in name or "\x00" in name:
            fail("renameat2 basename is unsafe")
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    if function is None:
        fail("libc renameat2 is unavailable; atomic exchange is mandatory")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        old_parent_fd,
        old_name.encode("utf-8"),
        new_parent_fd,
        new_name.encode("utf-8"),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            f"{old_name}<->{new_name}",
        )


def atomic_write_noreplace(
    directory: Path,
    final_name: str,
    raw: bytes,
    *,
    mode: int = 0o600,
) -> Path:
    directory_descriptor, _directory_state = open_directory(
        directory,
        "transaction journal directory",
    )
    nonce = os.urandom(16).hex()
    pending_name = f".pending-{final_name.removesuffix('.json')}-{nonce}.json"
    try:
        descriptor = os.open(
            pending_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            mode,
            dir_fd=directory_descriptor,
        )
        try:
            os.fchmod(descriptor, mode)
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if written <= 0:
                    fail("short write while creating transaction journal event")
                offset += written
            os.fsync(descriptor)
            state = os.fstat(descriptor)
            if (
                not stat.S_ISREG(state.st_mode)
                or state.st_nlink != 1
                or state.st_size != len(raw)
            ):
                fail("new transaction journal event inode is unsafe")
        finally:
            os.close(descriptor)
        renameat2(
            directory_descriptor,
            pending_name,
            directory_descriptor,
            final_name,
            RENAME_NOREPLACE,
        )
        os.fsync(directory_descriptor)
        return directory / final_name
    finally:
        os.close(directory_descriptor)


@dataclass(frozen=True)
class JournalState:
    transaction_path: Path
    identity: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    tip_hash: str
    pending: tuple[tuple[Path, str], ...]

    @property
    def terminal(self) -> bool:
        return bool(self.events) and self.events[-1].get("phase") in TERMINAL_PHASES


class TransactionJournal:
    def __init__(
        self,
        transactions_root: Path,
        *,
        owner_uid: int,
        owner_gid: int,
        production: bool,
    ):
        self.transactions_root = transactions_root
        self.owner_uid = owner_uid
        self.owner_gid = owner_gid
        self.production = production

    def create(self, identity: dict[str, Any]) -> JournalState:
        raw_identity = canonical_json(identity)
        transaction_id = identity.get("transactionId")
        if (
            not isinstance(transaction_id, str)
            or TRANSACTION_RE.fullmatch(transaction_id) is None
        ):
            fail("transaction identity is invalid")
        root_descriptor, _root_state = open_directory(
            self.transactions_root,
            "transactions root",
        )
        try:
            os.mkdir(
                transaction_id,
                mode=0o700,
                dir_fd=root_descriptor,
            )
            os.fsync(root_descriptor)
        except OSError as error:
            raise PromotionError("cannot create transaction marker") from error
        finally:
            os.close(root_descriptor)
        transaction_path = self.transactions_root / transaction_id
        events = transaction_path / "events"
        os.mkdir(events, mode=0o700)
        fsync_directory(transaction_path)
        atomic_write_noreplace(
            transaction_path,
            "identity.json",
            raw_identity,
        )
        fsync_directory(transaction_path)
        journal = self.load(transaction_path)
        self.append(journal, "prepared", {"planSha256": identity["planSha256"]})
        return self.load(transaction_path)

    def append(
        self,
        journal: JournalState,
        phase: str,
        details: dict[str, Any],
    ) -> JournalState:
        if journal.terminal:
            fail("cannot append to a terminal transaction journal")
        if not re.fullmatch(r"^[a-z][a-z0-9-]{1,63}$", phase):
            fail("journal phase is invalid")
        if journal.pending:
            fail("journal contains a pending event; explicit recovery is required")
        sequence = len(journal.events) + 1
        base = {
            "schemaVersion": 1,
            "transactionId": journal.identity["transactionId"],
            "sequence": sequence,
            "phase": phase,
            "recordedAt": utc_now().isoformat(),
            "previousHash": journal.tip_hash,
            "details": details,
        }
        record_hash = sha256_bytes(canonical_json(base))
        record = {**base, "recordHash": record_hash}
        atomic_write_noreplace(
            journal.transaction_path / "events",
            f"{sequence:06d}.json",
            canonical_json(record),
        )
        return self.load(journal.transaction_path)

    def resolve_pending(self, journal: JournalState) -> JournalState:
        """Adopt a complete next event or archive partial evidence, never delete."""

        if not journal.pending:
            return journal
        if len(journal.pending) != 1:
            fail("multiple pending journal events require manual forensics")
        pending_path, pending_digest = journal.pending[0]
        raw, _pending_state = read_regular(
            pending_path,
            "pending transaction event",
            max_bytes=MAX_JSON_BYTES,
            expected_uid=self.owner_uid,
            expected_gid=self.owner_gid,
            exact_mode=0o600,
        )
        sequence = len(journal.events) + 1
        valid_next = False
        try:
            event = strict_json(raw, "pending transaction event")
            base = {key: value for key, value in event.items() if key != "recordHash"}
            valid_next = (
                set(event)
                == {
                    "schemaVersion",
                    "transactionId",
                    "sequence",
                    "phase",
                    "recordedAt",
                    "previousHash",
                    "details",
                    "recordHash",
                }
                and event.get("schemaVersion") == 1
                and event.get("transactionId") == journal.identity["transactionId"]
                and event.get("sequence") == sequence
                and event.get("previousHash") == journal.tip_hash
                and event.get("recordHash") == sha256_bytes(canonical_json(base))
                and isinstance(event.get("details"), dict)
                and isinstance(event.get("phase"), str)
                and isinstance(event.get("recordedAt"), str)
            )
            if valid_next:
                parse_utc(event["recordedAt"], "pending journal recordedAt")
        except PromotionError:
            valid_next = False

        events_path = journal.transaction_path / "events"
        events_descriptor, _events_state = open_directory(
            events_path,
            "transaction events",
        )
        try:
            if valid_next:
                renameat2(
                    events_descriptor,
                    pending_path.name,
                    events_descriptor,
                    f"{sequence:06d}.json",
                    RENAME_NOREPLACE,
                )
                os.fsync(events_descriptor)
                return self.load(journal.transaction_path)
        finally:
            os.close(events_descriptor)

        orphans = journal.transaction_path / "orphaned-events"
        try:
            os.mkdir(orphans, mode=0o700)
            fsync_directory(journal.transaction_path)
        except FileExistsError:
            pass
        validate_path_chain(
            orphans,
            "orphaned transaction events",
            production=self.production,
            leaf_mode=0o700,
            leaf_uid=self.owner_uid,
            leaf_gid=self.owner_gid,
        )
        orphan_descriptor, _orphan_state = open_directory(
            orphans,
            "orphaned transaction events",
        )
        events_descriptor, _events_state = open_directory(
            events_path,
            "transaction events",
        )
        try:
            renameat2(
                events_descriptor,
                pending_path.name,
                orphan_descriptor,
                f"{pending_path.name}.{pending_digest}",
                RENAME_NOREPLACE,
            )
            os.fsync(events_descriptor)
            os.fsync(orphan_descriptor)
        finally:
            os.close(events_descriptor)
            os.close(orphan_descriptor)
        return self.load(journal.transaction_path)

    def load(self, transaction_path: Path) -> JournalState:
        validate_path_chain(
            transaction_path,
            "runtime transaction",
            production=self.production,
            leaf_mode=0o700,
            leaf_uid=self.owner_uid,
            leaf_gid=self.owner_gid,
        )
        identity_raw, _identity_state = read_regular(
            transaction_path / "identity.json",
            "transaction identity",
            max_bytes=MAX_JSON_BYTES,
            expected_uid=self.owner_uid,
            expected_gid=self.owner_gid,
            exact_mode=0o600,
        )
        identity = strict_json(identity_raw, "transaction identity")
        transaction_id = identity.get("transactionId")
        if (
            not isinstance(transaction_id, str)
            or TRANSACTION_RE.fullmatch(transaction_id) is None
            or transaction_path.name != transaction_id
        ):
            fail("transaction identity/path mismatch")
        events_path = transaction_path / "events"
        validate_path_chain(
            events_path,
            "runtime transaction events",
            production=self.production,
            leaf_mode=0o700,
            leaf_uid=self.owner_uid,
            leaf_gid=self.owner_gid,
        )
        open_descriptor, _events_state = open_directory(
            events_path,
            "transaction events",
        )
        os.close(open_descriptor)
        events: list[dict[str, Any]] = []
        pending: list[tuple[Path, str]] = []
        try:
            entries = sorted(events_path.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise PromotionError("cannot enumerate transaction events") from error
        previous_hash = sha256_bytes(identity_raw)
        expected_sequence = 1
        for path in entries:
            match = EVENT_FILE_RE.fullmatch(path.name)
            pending_match = PENDING_EVENT_RE.fullmatch(path.name)
            if pending_match is not None:
                raw, _state = read_regular(
                    path,
                    "pending transaction event",
                    max_bytes=MAX_JSON_BYTES,
                    expected_uid=self.owner_uid,
                    expected_gid=self.owner_gid,
                    exact_mode=0o600,
                )
                pending.append((path, sha256_bytes(raw)))
                continue
            if match is None:
                fail("transaction events directory contains an unknown entry")
            sequence = int(match.group("sequence"))
            if sequence != expected_sequence:
                fail("transaction event sequence is not contiguous")
            raw, _state = read_regular(
                path,
                "transaction event",
                max_bytes=MAX_JSON_BYTES,
                expected_uid=self.owner_uid,
                expected_gid=self.owner_gid,
                exact_mode=0o600,
            )
            event = strict_json(raw, "transaction event")
            required = {
                "schemaVersion",
                "transactionId",
                "sequence",
                "phase",
                "recordedAt",
                "previousHash",
                "details",
                "recordHash",
            }
            base = {key: value for key, value in event.items() if key != "recordHash"}
            if (
                set(event) != required
                or event.get("schemaVersion") != 1
                or event.get("transactionId") != transaction_id
                or event.get("sequence") != sequence
                or event.get("previousHash") != previous_hash
                or event.get("recordHash") != sha256_bytes(canonical_json(base))
                or not isinstance(event.get("details"), dict)
                or not isinstance(event.get("phase"), str)
                or not isinstance(event.get("recordedAt"), str)
            ):
                fail("transaction event hash chain is invalid")
            parse_utc(event["recordedAt"], "journal recordedAt")
            events.append(event)
            previous_hash = event["recordHash"]
            expected_sequence += 1
        return JournalState(
            transaction_path=transaction_path,
            identity=identity,
            events=tuple(events),
            tip_hash=previous_hash,
            pending=tuple(pending),
        )

    def all(self) -> tuple[JournalState, ...]:
        open_descriptor, _root_state = open_directory(
            self.transactions_root,
            "transactions root",
        )
        os.close(open_descriptor)
        journals: list[JournalState] = []
        for path in sorted(
            self.transactions_root.iterdir(),
            key=lambda item: item.name,
        ):
            if not TRANSACTION_RE.fullmatch(path.name):
                fail("transactions root contains an unknown entry")
            journals.append(self.load(path))
        return tuple(journals)


def mkdir_safe(path: Path, owner_uid: int, owner_gid: int) -> os.stat_result:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_gid != owner_gid
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_nlink < 2
    ):
        fail(f"managed runtime-generation directory is unsafe: {path}")
    return metadata


class PromotionController:
    def __init__(
        self,
        *,
        layout: Layout,
        validator: GenerationValidator,
        runtime_probe: RuntimeProbe,
        signature_verifier: SignatureVerifier,
        repository_state_provider: Callable[[], tuple[str, bool]],
        now_provider: Callable[[], datetime] = utc_now,
        crash_hook: Callable[[str], None] | None = None,
        fsync_function: Callable[[Path], None] = fsync_directory,
    ):
        self.layout = layout
        self.validator = validator
        self.runtime_probe = runtime_probe
        self.signature_verifier = signature_verifier
        self.repository_state_provider = repository_state_provider
        self.now_provider = now_provider
        self.crash_hook = crash_hook or (lambda _phase: None)
        self.fsync_function = fsync_function

    def journal_store(self) -> TransactionJournal:
        return TransactionJournal(
            self.layout.transactions,
            owner_uid=self.layout.owner_uid,
            owner_gid=self.layout.owner_gid,
            production=self.layout.production,
        )

    def import_journal_store(self) -> TransactionJournal:
        return TransactionJournal(
            self.layout.imports,
            owner_uid=self.layout.owner_uid,
            owner_gid=self.layout.owner_gid,
            production=self.layout.production,
        )

    def require_root_and_lock(self) -> None:
        if self.layout.production:
            if os.geteuid() != 0:
                fail("production runtime generation mutations require root")
            if not prove_or_none(HOST_LOCK_OPERATION):
                fail("host-global mutation lock is not proven")

    def validate_layout(self, *, need_transactions: bool = True) -> None:
        if self.layout.production:
            expected = {
                "state": PRODUCTION_STATE_ROOT,
                "generations": PRODUCTION_GENERATIONS_ROOT,
                "evidence": PRODUCTION_EVIDENCE_ROOT,
                "allowed": PRODUCTION_ALLOWED_SIGNERS,
            }
            observed = {
                "state": self.layout.state_root,
                "generations": self.layout.generations,
                "evidence": self.layout.evidence_root,
                "allowed": self.layout.allowed_signers,
            }
            if observed != expected:
                fail("production runtime-generation paths are immutable")
        validate_path_chain(
            self.layout.state_root,
            "state root",
            production=self.layout.production,
            leaf_mode=0o700,
            leaf_uid=self.layout.owner_uid,
            leaf_gid=self.layout.owner_gid,
        )
        for label, path in (
            ("canonical services", self.layout.canonical),
            ("runtime generations", self.layout.generations),
            ("runtime candidates", self.layout.candidates),
            ("runtime history", self.layout.history),
            ("incoming runtime generations", self.layout.incoming),
            ("candidate import transactions", self.layout.imports),
        ):
            validate_path_chain(
                path,
                label,
                production=self.layout.production,
                leaf_mode=0o700,
                leaf_uid=self.layout.owner_uid,
                leaf_gid=self.layout.owner_gid,
            )
        validate_path_chain(
            self.layout.evidence_root,
            "final-freeze evidence root",
            production=self.layout.production,
            leaf_mode=0o700,
            leaf_uid=self.layout.owner_uid,
            leaf_gid=self.layout.owner_gid,
        )
        validate_path_chain(
            self.layout.allowed_signers.parent,
            "runtime-promotion allowed-signers parent",
            production=self.layout.production,
            leaf_uid=self.layout.owner_uid,
            leaf_gid=self.layout.owner_gid,
        )
        if need_transactions:
            validate_path_chain(
                self.layout.transactions,
                "runtime transactions",
                production=self.layout.production,
                leaf_mode=0o700,
                leaf_uid=self.layout.owner_uid,
                leaf_gid=self.layout.owner_gid,
            )

    def repository_identity(self) -> str:
        revision, clean = self.repository_state_provider()
        if COMMIT_RE.fullmatch(revision) is None or not clean:
            fail("runtime promotion requires an exact clean Git commit")
        return revision

    def plan_promotion(self, generation: str) -> PromotionPlan:
        require_generation(generation)
        self.validate_layout()
        self.refuse_nonterminal_transactions()
        self.refuse_nonterminal_imports()
        revision = self.repository_identity()
        canonical = self.validator.validate(self.layout.canonical)
        source_path = self.layout.candidate(generation)
        validate_path_chain(
            source_path,
            "candidate runtime generation",
            production=self.layout.production,
            leaf_mode=0o700,
            leaf_uid=self.layout.owner_uid,
            leaf_gid=self.layout.owner_gid,
        )
        source = self.validator.validate(source_path)
        if source.source_backup != generation:
            fail("candidate directory does not match its sourceBackup")
        baseline = max(
            source_backup_time(BASELINE_SOURCE_BACKUP, "baseline sourceBackup"),
            source_backup_time(canonical.source_backup, "canonical sourceBackup"),
        )
        candidate_time = source_backup_time(
            source.source_backup,
            "candidate sourceBackup",
        )
        if candidate_time <= baseline:
            fail(
                "candidate sourceBackup is not strictly newer than both the "
                "canonical generation and the reviewed baseline"
            )
        self.require_same_filesystem(self.layout.canonical, source_path)
        preserve = self.layout.historical(canonical.source_backup)
        self.require_empty_preserve_slot(preserve)
        attestation = self.signature_verifier.verify(
            source,
            revision,
            now=self.now_provider(),
        )
        quiescence = self.runtime_probe.inspect(
            (self.layout.canonical, source_path, preserve)
        )
        return PromotionPlan(
            operation="promotion",
            canonical=canonical,
            source=source,
            source_slot=str(source_path),
            preserve_slot=str(preserve),
            attestation=attestation,
            source_commit=revision,
            quiescence_sha256=quiescence.digest(),
        )

    def attestation_bindings(self, generation: str) -> dict[str, Any]:
        """Render hash bindings without asserting external stop/backup facts."""

        require_generation(generation)
        self.validate_layout()
        self.refuse_nonterminal_transactions()
        self.refuse_nonterminal_imports()
        revision = self.repository_identity()
        canonical = self.validator.validate(self.layout.canonical)
        source_path = self.layout.candidate(generation)
        validate_path_chain(
            source_path,
            "candidate runtime generation",
            production=self.layout.production,
            leaf_mode=0o700,
            leaf_uid=self.layout.owner_uid,
            leaf_gid=self.layout.owner_gid,
        )
        source = self.validator.validate(source_path)
        if source.source_backup != generation:
            fail("candidate directory does not match its sourceBackup")
        baseline = max(
            source_backup_time(BASELINE_SOURCE_BACKUP, "baseline sourceBackup"),
            source_backup_time(canonical.source_backup, "canonical sourceBackup"),
        )
        if (
            source_backup_time(source.source_backup, "candidate sourceBackup")
            <= baseline
        ):
            fail("candidate sourceBackup is not strictly newer")
        preserve = self.layout.historical(canonical.source_backup)
        self.require_same_filesystem(self.layout.canonical, source_path)
        self.require_empty_preserve_slot(preserve)
        quiescence = self.runtime_probe.inspect(
            (self.layout.canonical, source_path, preserve)
        )
        platform_raw, _platform_state = read_regular(
            self.validator.platform_path,
            "platform contract",
            max_bytes=MAX_JSON_BYTES,
        )
        try:
            platform = yaml.safe_load(platform_raw.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as error:
            raise PromotionError("platform contract is invalid") from error
        if not isinstance(platform, dict):
            fail("platform contract is not a mapping")
        return {
            "schemaVersion": 1,
            "signerIdentity": ATTESTATION_IDENTITY,
            "signatureNamespace": ATTESTATION_NAMESPACE,
            "attestationPath": str(self.layout.attestation(generation)),
            "signaturePath": str(self.layout.signature(generation)),
            "legacyIPv4": platform.get("platform_dns_legacy_ipv4"),
            "targetIPv4": platform.get("platform_public_ipv4"),
            "sourceBackup": source.source_backup,
            "catalogSha256": source.catalog_sha256,
            "platformSha256": source.platform_sha256,
            "runtimeManifestSha256": source.runtime_manifest_sha256,
            "recoveryManifestSha256": source.recovery_manifest_sha256,
            "readyMarkerSha256": source.ready_marker_sha256,
            "secretCatalogSha256": source.secret_catalog_sha256,
            "secretSourcesSha256": source.secret_sources_sha256,
            "runtimeTreeSha256": source.runtime_tree_sha256,
            "sourceCommit": revision,
            "canonicalSourceBackup": canonical.source_backup,
            "quiescenceSha256": quiescence.digest(),
            "externalFactsNotAsserted": [
                "legacyWritersStopped",
                "finalBackupVerified",
                "legacyStoppedAt",
                "backupStartedAt",
                "backupCompletedAt",
                "issuedAt",
                "expiresAt",
            ],
        }

    def plan_candidate_import(self, generation: str) -> CandidateImportPlan:
        """Validate a finalized incoming tree before atomic candidate import."""

        require_generation(generation)
        self.validate_layout()
        self.refuse_nonterminal_transactions()
        self.refuse_nonterminal_imports()
        revision = self.repository_identity()
        canonical = self.validator.validate(self.layout.canonical)
        source_path = self.layout.incoming_generation(generation)
        validate_path_chain(
            source_path,
            "incoming runtime generation",
            production=self.layout.production,
            leaf_mode=0o700,
            leaf_uid=self.layout.owner_uid,
            leaf_gid=self.layout.owner_gid,
        )
        incoming = self.validator.validate(source_path)
        if incoming.source_backup != generation:
            fail("incoming directory does not match its sourceBackup")
        baseline = max(
            source_backup_time(BASELINE_SOURCE_BACKUP, "baseline sourceBackup"),
            source_backup_time(canonical.source_backup, "canonical sourceBackup"),
        )
        if source_backup_time(incoming.source_backup, "incoming sourceBackup") <= (
            baseline
        ):
            fail(
                "incoming sourceBackup is not strictly newer than both the "
                "canonical generation and reviewed baseline"
            )
        target_path = self.layout.candidate(generation)
        self.require_absent_candidate_target(target_path)
        self.require_same_filesystem(self.layout.canonical, source_path)
        quiescence = self.runtime_probe.inspect(
            (self.layout.canonical, source_path, target_path)
        )
        return CandidateImportPlan(
            canonical=canonical,
            incoming=incoming,
            source_slot=str(source_path),
            target_slot=str(target_path),
            source_commit=revision,
            quiescence_sha256=quiescence.digest(),
        )

    def apply_candidate_import(
        self,
        generation: str,
        confirmation: str,
    ) -> JournalState:
        self.require_root_and_lock()
        plan = self.plan_candidate_import(generation)
        if confirmation != plan.confirmation():
            fail(f"confirmation mismatch; expected: {plan.confirmation()}")
        return self._execute_candidate_import(plan)

    def _execute_candidate_import(
        self,
        plan: CandidateImportPlan,
    ) -> JournalState:
        source = Path(plan.source_slot)
        target = Path(plan.target_slot)
        transaction_id = sha256_bytes(
            canonical_json(
                {
                    "planSha256": plan.digest(),
                    "nonce": os.urandom(32).hex(),
                }
            )
        )
        identity = {
            "schemaVersion": 1,
            "transactionId": transaction_id,
            "operation": "candidate-import",
            "planSha256": plan.digest(),
            "plan": plan.document(),
            "sourceSlot": str(source),
            "targetSlot": str(target),
            "sourceBefore": {
                "sourceBackup": plan.incoming.source_backup,
                "device": plan.incoming.root_device,
                "inode": plan.incoming.root_inode,
                "treeSha256": plan.incoming.runtime_tree_sha256,
            },
            "createdAt": self.now_provider().isoformat(),
        }
        store = self.import_journal_store()
        journal = store.create(identity)
        signal_handlers: dict[int, signal.Handlers] = {}

        def interrupt(signal_number: int, _frame: object) -> None:
            raise PromotionInterrupted(signal_number)

        try:
            signal_handlers = {
                signal_number: signal.signal(signal_number, interrupt)
                for signal_number in (
                    signal.SIGINT,
                    signal.SIGTERM,
                    signal.SIGHUP,
                )
            }
            self._prepare_candidate_parent(target)
            journal = store.append(
                journal,
                "candidate-target-prepared",
                {
                    "targetSlot": str(target),
                    "servicesEntryAbsent": True,
                },
            )
            self._revalidate_candidate_import(plan)
            journal = store.append(
                journal,
                "candidate-import-intent",
                {
                    "sourceBefore": identity["sourceBefore"],
                },
            )
            self.crash_hook("import-before-rename")
            self._move_noreplace(
                source,
                target,
                expected={
                    "device": plan.incoming.root_device,
                    "inode": plan.incoming.root_inode,
                },
            )
            self.crash_hook("import-after-rename")
            inferred = self.infer_candidate_import_state(journal)
            if inferred != "imported":
                fail("candidate import inode result is not exact")
            journal = store.append(
                journal,
                "candidate-import-observed",
                {"inferredState": inferred},
            )
            self.fsync_function(source.parent)
            self.fsync_function(target.parent)
            self.crash_hook("import-after-fsync")
            imported = self.validator.validate(target)
            if imported != plan.incoming:
                fail("imported candidate differs from validated incoming runtime")
            journal = store.append(
                journal,
                "candidate-import-completed",
                {
                    "candidateSourceBackup": plan.incoming.source_backup,
                    "candidateTreeSha256": plan.incoming.runtime_tree_sha256,
                    "incomingAttemptPreserved": True,
                    "noGenerationDeleted": True,
                },
            )
            return journal
        except CrashInjection:
            raise
        except BaseException as error:
            try:
                current = store.load(journal.transaction_path)
                if not current.pending:
                    store.append(
                        current,
                        "candidate-import-failed",
                        {
                            "errorType": type(error).__name__,
                            "inferredState": (
                                self.infer_candidate_import_state(current)
                            ),
                            "manualRecoveryRequired": True,
                        },
                    )
            except BaseException:
                pass
            raise
        finally:
            for signal_number, handler in signal_handlers.items():
                signal.signal(signal_number, handler)

    def recover_candidate_import(
        self,
        *,
        mode: str,
        apply: bool,
        confirmation: str | None,
    ) -> str:
        self.validate_layout()
        journal = self.single_nonterminal_import()
        inferred = self.infer_candidate_import_state(journal)
        if inferred not in {"pre-import", "imported"}:
            fail(
                "candidate-import inode state is unknown; no recovery "
                "confirmation can be issued"
            )
        allowed_mode = "abort" if inferred == "pre-import" else "complete"
        if mode != allowed_mode:
            fail(
                f"candidate-import recovery mode {mode!r} is unsafe for "
                f"{inferred!r}; required mode is {allowed_mode!r}"
            )
        pending_digest = sha256_bytes(
            canonical_json(
                {
                    "pending": [
                        {"name": path.name, "sha256": digest}
                        for path, digest in journal.pending
                    ]
                }
            )
        )
        expected = (
            f"RECOVER_RUNTIME_CANDIDATE_IMPORT:"
            f"{journal.identity['transactionId']}:{mode}:{journal.tip_hash}:"
            f"{pending_digest}:{inferred}:NO_WRITERS_CONFIRMED"
        )
        if not apply:
            return expected
        self.require_root_and_lock()
        if confirmation != expected:
            fail(f"candidate-import confirmation mismatch; expected: {expected}")
        store = self.import_journal_store()
        if journal.pending:
            journal = store.resolve_pending(journal)
            if journal.terminal:
                if self.infer_candidate_import_state(journal) != "imported":
                    fail(
                        "adopted candidate-import terminal event conflicts "
                        "with filesystem inodes"
                    )
                return journal.tip_hash
        plan = self.import_plan_from_identity(journal.identity)
        self.runtime_probe.inspect(
            (
                self.layout.canonical,
                Path(plan.source_slot),
                Path(plan.target_slot),
            )
        )
        if inferred == "pre-import":
            journal = store.append(
                journal,
                "candidate-import-aborted",
                {
                    "incomingGenerationPreserved": True,
                    "noGenerationDeleted": True,
                },
            )
            return journal.tip_hash
        self.fsync_function(Path(plan.source_slot).parent)
        self.fsync_function(Path(plan.target_slot).parent)
        imported = self.validator.validate(Path(plan.target_slot))
        if imported != plan.incoming:
            fail("recovered candidate differs from import transaction")
        journal = store.append(
            journal,
            "candidate-import-completed",
            {
                "candidateSourceBackup": plan.incoming.source_backup,
                "candidateTreeSha256": plan.incoming.runtime_tree_sha256,
                "completedByExplicitRecovery": True,
                "noGenerationDeleted": True,
            },
        )
        return journal.tip_hash

    def plan_rollback(self, generation: str) -> PromotionPlan:
        require_generation(generation)
        self.validate_layout()
        self.refuse_nonterminal_transactions()
        self.refuse_nonterminal_imports()
        revision = self.repository_identity()
        canonical = self.validator.validate(self.layout.canonical)
        source_path = self.layout.historical(generation)
        validate_path_chain(
            source_path,
            "historical runtime generation",
            production=self.layout.production,
            leaf_mode=0o700,
            leaf_uid=self.layout.owner_uid,
            leaf_gid=self.layout.owner_gid,
        )
        source = self.validator.validate(source_path)
        if source.source_backup != generation:
            fail("history directory does not match its sourceBackup")
        if source_backup_time(source.source_backup, "rollback sourceBackup") >= (
            source_backup_time(canonical.source_backup, "canonical sourceBackup")
        ):
            fail("rollback source is not older than the canonical generation")
        if not self.was_preserved_by_completed_transaction(
            generation,
            canonical.source_backup,
        ):
            fail("rollback generation lacks completed preservation evidence")
        self.require_same_filesystem(self.layout.canonical, source_path)
        preserve = self.layout.historical(canonical.source_backup)
        self.require_empty_preserve_slot(preserve)
        quiescence = self.runtime_probe.inspect(
            (self.layout.canonical, source_path, preserve)
        )
        return PromotionPlan(
            operation="rollback",
            canonical=canonical,
            source=source,
            source_slot=str(source_path),
            preserve_slot=str(preserve),
            attestation=None,
            source_commit=revision,
            quiescence_sha256=quiescence.digest(),
        )

    def apply_plan(self, plan: PromotionPlan, confirmation: str) -> JournalState:
        self.require_root_and_lock()
        if confirmation != plan.confirmation():
            fail(f"confirmation mismatch; expected: {plan.confirmation()}")
        # Rebuild the exact plan under the proven lock.  The confirmation binds
        # every content hash, inode, commit, attestation, and quiescence result.
        rebuilt = (
            self.plan_promotion(plan.source.source_backup)
            if plan.operation == "promotion"
            else self.plan_rollback(plan.source.source_backup)
        )
        if rebuilt.document() != plan.document():
            fail("runtime promotion plan changed before apply")
        return self._execute(rebuilt)

    def apply_promotion(
        self,
        generation: str,
        confirmation: str,
    ) -> JournalState:
        self.require_root_and_lock()
        plan = self.plan_promotion(generation)
        if confirmation != plan.confirmation():
            fail(f"confirmation mismatch; expected: {plan.confirmation()}")
        return self._execute(plan)

    def apply_rollback(
        self,
        generation: str,
        confirmation: str,
    ) -> JournalState:
        self.require_root_and_lock()
        plan = self.plan_rollback(generation)
        if confirmation != plan.confirmation():
            fail(f"confirmation mismatch; expected: {plan.confirmation()}")
        return self._execute(plan)

    def _execute(self, plan: PromotionPlan) -> JournalState:
        self.refuse_nonterminal_transactions()
        source = Path(plan.source_slot)
        preserve = Path(plan.preserve_slot)
        self._verify_plan_inodes(plan)
        transaction_id = sha256_bytes(
            canonical_json(
                {
                    "planSha256": plan.digest(),
                    "nonce": os.urandom(32).hex(),
                }
            )
        )
        identity = {
            "schemaVersion": 1,
            "transactionId": transaction_id,
            "operation": plan.operation,
            "planSha256": plan.digest(),
            "plan": plan.document(),
            "canonicalPath": str(self.layout.canonical),
            "sourceSlot": str(source),
            "preserveSlot": str(preserve),
            "canonicalBefore": {
                "sourceBackup": plan.canonical.source_backup,
                "device": plan.canonical.root_device,
                "inode": plan.canonical.root_inode,
                "treeSha256": plan.canonical.runtime_tree_sha256,
            },
            "sourceBefore": {
                "sourceBackup": plan.source.source_backup,
                "device": plan.source.root_device,
                "inode": plan.source.root_inode,
                "treeSha256": plan.source.runtime_tree_sha256,
            },
            "createdAt": self.now_provider().isoformat(),
        }
        journal_store = self.journal_store()
        journal = journal_store.create(identity)
        signal_handlers: dict[int, signal.Handlers] = {}

        def interrupt(signal_number: int, _frame: object) -> None:
            raise PromotionInterrupted(signal_number)

        try:
            signal_handlers = {
                signal_number: signal.signal(signal_number, interrupt)
                for signal_number in (
                    signal.SIGINT,
                    signal.SIGTERM,
                    signal.SIGHUP,
                )
            }
            self.crash_hook("after-journal")
            self._prepare_preserve_parent(preserve)
            journal = journal_store.append(
                journal,
                "preserve-slot-prepared",
                {
                    "preserveSlot": str(preserve),
                    "servicesEntryAbsent": True,
                },
            )
            # Revalidate contracts, content hashes, inodes, and all live
            # writer probes immediately before writing exchange intent.
            self._revalidate_plan(plan)
            journal = journal_store.append(
                journal,
                "exchange-intent",
                {
                    "canonicalBefore": identity["canonicalBefore"],
                    "sourceBefore": identity["sourceBefore"],
                },
            )
            self.crash_hook("before-exchange")
            self._exchange(
                self.layout.canonical,
                source,
                canonical_expected={
                    "device": plan.canonical.root_device,
                    "inode": plan.canonical.root_inode,
                },
                source_expected={
                    "device": plan.source.root_device,
                    "inode": plan.source.root_inode,
                },
            )
            self.crash_hook("after-exchange")
            inferred = self.infer_state(journal)
            if inferred != "exchanged":
                fail("post-exchange inode state is not exact")
            journal = journal_store.append(
                journal,
                "exchange-observed",
                {"inferredState": inferred},
            )
            self.fsync_function(self.layout.canonical.parent)
            self.fsync_function(source.parent)
            journal = journal_store.append(
                journal,
                "exchange-durable",
                {"parentsFsynced": True},
            )
            self.crash_hook("after-exchange-fsync")
            self._preserve_old(
                source,
                preserve,
                expected={
                    "device": plan.canonical.root_device,
                    "inode": plan.canonical.root_inode,
                },
            )
            self.crash_hook("after-preserve")
            inferred = self.infer_state(journal)
            if inferred != "preserved":
                fail("rollback generation was not preserved exactly")
            journal = journal_store.append(
                journal,
                "rollback-generation-preserved",
                {
                    "inferredState": inferred,
                    "preservedSourceBackup": plan.canonical.source_backup,
                },
            )
            final_phase = (
                "promotion-completed"
                if plan.operation == "promotion"
                else "rollback-completed"
            )
            journal = journal_store.append(
                journal,
                final_phase,
                {
                    "canonicalSourceBackup": plan.source.source_backup,
                    "rollbackSourceBackup": plan.canonical.source_backup,
                    "noGenerationDeleted": True,
                },
            )
            return journal
        except CrashInjection:
            raise
        except BaseException as error:
            try:
                current = journal_store.load(journal.transaction_path)
                if not current.pending:
                    journal_store.append(
                        current,
                        "operation-failed",
                        {
                            "errorType": type(error).__name__,
                            "inferredState": self.infer_state(current),
                            "manualRecoveryRequired": True,
                        },
                    )
            except BaseException:
                pass
            raise
        finally:
            for signal_number, handler in signal_handlers.items():
                signal.signal(signal_number, handler)

    def recover(
        self,
        *,
        mode: str,
        apply: bool,
        confirmation: str | None,
    ) -> str:
        self.validate_layout()
        journal = self.single_nonterminal_transaction()
        inferred = self.infer_state(journal)
        if inferred not in {"pre-exchange", "exchanged", "preserved"}:
            fail(
                "transaction inode state is unknown; no recovery "
                "confirmation can be issued"
            )
        allowed_mode = "abort" if inferred == "pre-exchange" else "complete"
        if mode != allowed_mode:
            fail(
                f"recovery mode {mode!r} is unsafe for inferred state "
                f"{inferred!r}; required mode is {allowed_mode!r}"
            )
        pending_digest = sha256_bytes(
            canonical_json(
                {
                    "pending": [
                        {"name": path.name, "sha256": digest}
                        for path, digest in journal.pending
                    ]
                }
            )
        )
        expected = (
            f"RECOVER_RUNTIME_GENERATION:{journal.identity['transactionId']}:"
            f"{mode}:{journal.tip_hash}:{pending_digest}:{inferred}:"
            "NO_WRITERS_CONFIRMED"
        )
        if not apply:
            return expected
        self.require_root_and_lock()
        if confirmation != expected:
            fail(f"recovery confirmation mismatch; expected: {expected}")
        store = self.journal_store()
        if journal.pending:
            journal = store.resolve_pending(journal)
            if journal.terminal:
                if self.infer_state(journal) != "preserved":
                    fail("adopted terminal event conflicts with filesystem inodes")
                return journal.tip_hash
        plan = self.plan_from_identity(journal.identity)
        self.runtime_probe.inspect(
            (
                self.layout.canonical,
                Path(plan.source_slot),
                Path(plan.preserve_slot),
            )
        )
        if inferred == "pre-exchange":
            journal = store.append(
                journal,
                "aborted-before-exchange",
                {
                    "candidatePreserved": True,
                    "noGenerationDeleted": True,
                },
            )
            return journal.tip_hash
        if inferred == "exchanged":
            self.fsync_function(self.layout.canonical.parent)
            self.fsync_function(Path(plan.source_slot).parent)
            self._preserve_old(
                Path(plan.source_slot),
                Path(plan.preserve_slot),
                expected={
                    "device": plan.canonical.root_device,
                    "inode": plan.canonical.root_inode,
                },
            )
            journal = store.append(
                journal,
                "recovery-preserved-rollback-generation",
                {
                    "inferredState": self.infer_state(journal),
                    "noGenerationDeleted": True,
                },
            )
        elif inferred == "preserved":
            self.fsync_function(self.layout.canonical.parent)
            self.fsync_function(Path(plan.preserve_slot).parent)
        else:
            fail("transaction inode state cannot be recovered safely")
        inferred = self.infer_state(journal)
        if inferred != "preserved":
            fail("recovery did not preserve the prior generation")
        final_phase = (
            "promotion-completed"
            if plan.operation == "promotion"
            else "rollback-completed"
        )
        journal = store.append(
            journal,
            final_phase,
            {
                "canonicalSourceBackup": plan.source.source_backup,
                "rollbackSourceBackup": plan.canonical.source_backup,
                "completedByExplicitRecovery": True,
                "noGenerationDeleted": True,
            },
        )
        return journal.tip_hash

    def status(self) -> dict[str, Any]:
        self.validate_layout()
        canonical = self.validator.validate(self.layout.canonical)
        journals = self.journal_store().all()
        import_journals = self.import_journal_store().all()
        transactions = []
        for journal in journals:
            transactions.append(
                {
                    "transactionId": journal.identity["transactionId"],
                    "operation": journal.identity["operation"],
                    "terminal": journal.terminal,
                    "lastPhase": (
                        journal.events[-1]["phase"] if journal.events else None
                    ),
                    "tipHash": journal.tip_hash,
                    "pendingEvents": [
                        {"name": path.name, "sha256": digest}
                        for path, digest in journal.pending
                    ],
                    "inferredState": self.infer_state(journal),
                }
            )
        candidate_imports = []
        for journal in import_journals:
            candidate_imports.append(
                {
                    "transactionId": journal.identity["transactionId"],
                    "terminal": journal.terminal,
                    "lastPhase": (
                        journal.events[-1]["phase"] if journal.events else None
                    ),
                    "tipHash": journal.tip_hash,
                    "pendingEvents": [
                        {"name": path.name, "sha256": digest}
                        for path, digest in journal.pending
                    ],
                    "inferredState": self.infer_candidate_import_state(journal),
                }
            )
        return {
            "schemaVersion": 1,
            "canonical": asdict(canonical),
            "transactions": transactions,
            "candidateImports": candidate_imports,
            "blocked": any(
                not item["terminal"] or item["pendingEvents"] for item in transactions
            )
            or any(
                not item["terminal"] or item["pendingEvents"]
                for item in candidate_imports
            ),
        }

    def infer_state(self, journal: JournalState) -> str:
        identity = journal.identity
        canonical = self._safe_slot_state(Path(identity["canonicalPath"]))
        source = self._safe_slot_state(Path(identity["sourceSlot"]))
        preserve = self._safe_slot_state(Path(identity["preserveSlot"]))
        old = identity["canonicalBefore"]
        new = identity["sourceBefore"]
        if (
            canonical is not None
            and source is not None
            and preserve is None
            and same_identity(canonical, old)
            and same_identity(source, new)
        ):
            return "pre-exchange"
        if (
            canonical is not None
            and source is not None
            and preserve is None
            and same_identity(canonical, new)
            and same_identity(source, old)
        ):
            return "exchanged"
        if (
            canonical is not None
            and source is None
            and preserve is not None
            and same_identity(canonical, new)
            and same_identity(preserve, old)
        ):
            return "preserved"
        return "unknown"

    def infer_candidate_import_state(self, journal: JournalState) -> str:
        identity = journal.identity
        source = self._safe_slot_state(Path(identity["sourceSlot"]))
        target = self._safe_slot_state(Path(identity["targetSlot"]))
        expected = identity["sourceBefore"]
        if source is not None and target is None and same_identity(source, expected):
            return "pre-import"
        if source is None and target is not None and same_identity(target, expected):
            return "imported"
        return "unknown"

    def refuse_nonterminal_transactions(self) -> None:
        for journal in self.journal_store().all():
            if not journal.terminal or journal.pending:
                fail(
                    "an incomplete runtime-generation transaction blocks "
                    "all new promotion/rollback operations"
                )

    def refuse_nonterminal_imports(self) -> None:
        for journal in self.import_journal_store().all():
            if not journal.terminal or journal.pending:
                fail(
                    "an incomplete candidate-import transaction blocks "
                    "all new imports"
                )

    def single_nonterminal_transaction(self) -> JournalState:
        nonterminal = tuple(
            journal
            for journal in self.journal_store().all()
            if not journal.terminal or journal.pending
        )
        if len(nonterminal) != 1:
            fail("recovery requires exactly one incomplete transaction")
        return nonterminal[0]

    def single_nonterminal_import(self) -> JournalState:
        nonterminal = tuple(
            journal
            for journal in self.import_journal_store().all()
            if not journal.terminal or journal.pending
        )
        if len(nonterminal) != 1:
            fail(
                "candidate-import recovery requires exactly one incomplete "
                "transaction"
            )
        return nonterminal[0]

    def plan_from_identity(self, identity: dict[str, Any]) -> PromotionPlan:
        document = identity.get("plan")
        if not isinstance(document, dict):
            fail("transaction plan is absent")
        try:
            canonical = GenerationEvidence(**document["canonical"])
            source = GenerationEvidence(**document["source"])
            attestation_value = document.get("attestation")
            attestation = (
                AttestationEvidence(**attestation_value)
                if isinstance(attestation_value, dict)
                else None
            )
            plan = PromotionPlan(
                operation=document["operation"],
                canonical=canonical,
                source=source,
                source_slot=document["sourceSlot"],
                preserve_slot=document["preserveSlot"],
                attestation=attestation,
                source_commit=document["sourceCommit"],
                quiescence_sha256=document["quiescenceSha256"],
            )
        except (KeyError, TypeError) as error:
            raise PromotionError("transaction plan schema is invalid") from error
        if (
            plan.digest() != identity.get("planSha256")
            or str(self.layout.canonical) != identity.get("canonicalPath")
            or plan.source_slot != identity.get("sourceSlot")
            or plan.preserve_slot != identity.get("preserveSlot")
        ):
            fail("transaction identity does not bind its plan")
        return plan

    def import_plan_from_identity(
        self,
        identity: dict[str, Any],
    ) -> CandidateImportPlan:
        document = identity.get("plan")
        if not isinstance(document, dict):
            fail("candidate-import plan is absent")
        try:
            plan = CandidateImportPlan(
                canonical=GenerationEvidence(**document["canonical"]),
                incoming=GenerationEvidence(**document["incoming"]),
                source_slot=document["sourceSlot"],
                target_slot=document["targetSlot"],
                source_commit=document["sourceCommit"],
                quiescence_sha256=document["quiescenceSha256"],
            )
        except (KeyError, TypeError) as error:
            raise PromotionError("candidate-import plan schema is invalid") from error
        if (
            document.get("schemaVersion") != 1
            or document.get("operation") != "candidate-import"
            or plan.digest() != identity.get("planSha256")
            or plan.source_slot != identity.get("sourceSlot")
            or plan.target_slot != identity.get("targetSlot")
        ):
            fail("candidate-import identity does not bind its plan")
        return plan

    def was_preserved_by_completed_transaction(
        self,
        generation: str,
        current_generation: str,
    ) -> bool:
        for journal in self.journal_store().all():
            if not journal.terminal or journal.pending:
                continue
            plan = self.plan_from_identity(journal.identity)
            if (
                plan.canonical.source_backup == generation
                and plan.source.source_backup == current_generation
                and journal.events[-1]["phase"]
                in {"promotion-completed", "rollback-completed"}
            ):
                return True
        return False

    def _revalidate_candidate_import(
        self,
        plan: CandidateImportPlan,
    ) -> None:
        canonical = self.validator.validate(self.layout.canonical)
        incoming = self.validator.validate(Path(plan.source_slot))
        if canonical != plan.canonical or incoming != plan.incoming:
            fail("incoming runtime or canonical generation changed before import")
        if self.repository_identity() != plan.source_commit:
            fail("source commit changed before candidate import")
        self.require_absent_candidate_target(Path(plan.target_slot))
        self.require_same_filesystem(
            self.layout.canonical,
            Path(plan.source_slot),
        )
        self.runtime_probe.inspect(
            (
                self.layout.canonical,
                Path(plan.source_slot),
                Path(plan.target_slot),
            )
        )

    def require_absent_candidate_target(self, target: Path) -> None:
        parent = target.parent
        if parent.exists() or parent.is_symlink():
            validate_path_chain(
                parent,
                "candidate generation parent",
                production=self.layout.production,
                leaf_mode=0o700,
                leaf_uid=self.layout.owner_uid,
                leaf_gid=self.layout.owner_gid,
            )
        if target.exists() or target.is_symlink():
            fail("candidate target already exists")

    def _prepare_candidate_parent(self, target: Path) -> None:
        parent = target.parent
        if parent.exists() or parent.is_symlink():
            validate_path_chain(
                parent,
                "candidate generation parent",
                production=self.layout.production,
                leaf_mode=0o700,
                leaf_uid=self.layout.owner_uid,
                leaf_gid=self.layout.owner_gid,
            )
        else:
            candidates_descriptor, _candidates_state = open_directory(
                self.layout.candidates,
                "runtime candidates",
            )
            try:
                os.mkdir(parent.name, mode=0o700, dir_fd=candidates_descriptor)
                os.fsync(candidates_descriptor)
            finally:
                os.close(candidates_descriptor)
        self.require_absent_candidate_target(target)

    def _revalidate_plan(self, plan: PromotionPlan) -> None:
        current = self.validator.validate(self.layout.canonical)
        source = self.validator.validate(Path(plan.source_slot))
        if current != plan.canonical or source != plan.source:
            fail("runtime generation content or inode changed before exchange")
        revision = self.repository_identity()
        if revision != plan.source_commit:
            fail("source commit changed before exchange")
        if plan.operation == "promotion":
            attestation = self.signature_verifier.verify(
                source,
                revision,
                now=self.now_provider(),
            )
            if attestation != plan.attestation:
                fail("signed freeze attestation changed before exchange")
        self.require_empty_preserve_slot(Path(plan.preserve_slot))
        self.runtime_probe.inspect(
            (
                self.layout.canonical,
                Path(plan.source_slot),
                Path(plan.preserve_slot),
            )
        )
        self._verify_plan_inodes(plan)

    def _verify_plan_inodes(self, plan: PromotionPlan) -> None:
        canonical = self.layout.canonical.lstat()
        source = Path(plan.source_slot).lstat()
        if (
            canonical.st_dev != plan.canonical.root_device
            or canonical.st_ino != plan.canonical.root_inode
            or source.st_dev != plan.source.root_device
            or source.st_ino != plan.source.root_inode
            or not stat.S_ISDIR(canonical.st_mode)
            or not stat.S_ISDIR(source.st_mode)
        ):
            fail("runtime generation inode identity changed")
        self.require_same_filesystem(self.layout.canonical, Path(plan.source_slot))

    @staticmethod
    def require_same_filesystem(first: Path, second: Path) -> None:
        first_state = first.lstat()
        second_state = second.lstat()
        if first_state.st_dev != second_state.st_dev:
            fail("atomic runtime exchange requires one filesystem")

    def require_empty_preserve_slot(self, preserve: Path) -> None:
        parent = preserve.parent
        if parent.exists() or parent.is_symlink():
            validate_path_chain(
                parent,
                "history generation parent",
                production=self.layout.production,
                leaf_mode=0o700,
                leaf_uid=self.layout.owner_uid,
                leaf_gid=self.layout.owner_gid,
            )
        if preserve.exists() or preserve.is_symlink():
            fail("rollback preservation slot already exists")

    def _prepare_preserve_parent(self, preserve: Path) -> None:
        parent = preserve.parent
        if parent.exists() or parent.is_symlink():
            validate_path_chain(
                parent,
                "history generation parent",
                production=self.layout.production,
                leaf_mode=0o700,
                leaf_uid=self.layout.owner_uid,
                leaf_gid=self.layout.owner_gid,
            )
        else:
            history_descriptor, _history_state = open_directory(
                self.layout.history,
                "runtime history",
            )
            try:
                os.mkdir(parent.name, mode=0o700, dir_fd=history_descriptor)
                os.fsync(history_descriptor)
            finally:
                os.close(history_descriptor)
        self.require_empty_preserve_slot(preserve)

    @staticmethod
    def _exchange(
        canonical: Path,
        source: Path,
        *,
        canonical_expected: dict[str, int],
        source_expected: dict[str, int],
    ) -> None:
        canonical_parent, _canonical_parent_state = open_directory(
            canonical.parent,
            "canonical parent",
        )
        source_parent, _source_parent_state = open_directory(
            source.parent,
            "source-slot parent",
        )
        try:
            canonical_before = os.stat(
                canonical.name,
                dir_fd=canonical_parent,
                follow_symlinks=False,
            )
            source_before = os.stat(
                source.name,
                dir_fd=source_parent,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(canonical_before.st_mode)
                or not stat.S_ISDIR(source_before.st_mode)
                or not same_identity(canonical_before, canonical_expected)
                or not same_identity(source_before, source_expected)
            ):
                fail("runtime exchange inode changed before renameat2")
            renameat2(
                canonical_parent,
                canonical.name,
                source_parent,
                source.name,
                RENAME_EXCHANGE,
            )
            canonical_after = os.stat(
                canonical.name,
                dir_fd=canonical_parent,
                follow_symlinks=False,
            )
            source_after = os.stat(
                source.name,
                dir_fd=source_parent,
                follow_symlinks=False,
            )
            if not same_identity(canonical_after, source_expected) or not same_identity(
                source_after, canonical_expected
            ):
                fail("runtime exchange inode result is not exact")
        finally:
            os.close(canonical_parent)
            os.close(source_parent)

    @staticmethod
    def _move_noreplace(
        source: Path,
        target: Path,
        *,
        expected: dict[str, int],
    ) -> None:
        source_parent, _source_parent_state = open_directory(
            source.parent,
            "incoming generation parent",
        )
        target_parent, _target_parent_state = open_directory(
            target.parent,
            "candidate generation parent",
        )
        try:
            source_before = os.stat(
                source.name,
                dir_fd=source_parent,
                follow_symlinks=False,
            )
            try:
                os.stat(
                    target.name,
                    dir_fd=target_parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                fail("candidate target appeared before renameat2")
            if not stat.S_ISDIR(source_before.st_mode) or not same_identity(
                source_before, expected
            ):
                fail("incoming generation inode changed before renameat2")
            renameat2(
                source_parent,
                source.name,
                target_parent,
                target.name,
                RENAME_NOREPLACE,
            )
            imported = os.stat(
                target.name,
                dir_fd=target_parent,
                follow_symlinks=False,
            )
            if not same_identity(imported, expected):
                fail("candidate inode differs after renameat2")
            try:
                os.stat(
                    source.name,
                    dir_fd=source_parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                fail("incoming source still exists after candidate import")
            os.fsync(source_parent)
            os.fsync(target_parent)
        finally:
            os.close(source_parent)
            os.close(target_parent)

    def _preserve_old(
        self,
        source: Path,
        preserve: Path,
        *,
        expected: dict[str, int],
    ) -> None:
        source_parent, _source_parent_state = open_directory(
            source.parent,
            "source-slot parent",
        )
        preserve_parent, _preserve_parent_state = open_directory(
            preserve.parent,
            "preserve-slot parent",
        )
        try:
            source_before = os.stat(
                source.name,
                dir_fd=source_parent,
                follow_symlinks=False,
            )
            try:
                os.stat(
                    preserve.name,
                    dir_fd=preserve_parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                fail("rollback preservation slot appeared before renameat2")
            if not stat.S_ISDIR(source_before.st_mode) or not same_identity(
                source_before, expected
            ):
                fail("rollback generation inode changed before preservation")
            renameat2(
                source_parent,
                source.name,
                preserve_parent,
                preserve.name,
                RENAME_NOREPLACE,
            )
            preserved = os.stat(
                preserve.name,
                dir_fd=preserve_parent,
                follow_symlinks=False,
            )
            if not same_identity(preserved, expected):
                fail("preserved rollback inode differs after renameat2")
            os.fsync(source_parent)
            os.fsync(preserve_parent)
        finally:
            os.close(source_parent)
            os.close(preserve_parent)

    @staticmethod
    def _safe_slot_state(path: Path) -> os.stat_result | None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_nlink < 2:
            fail(f"runtime transaction slot is unsafe: {path}")
        return metadata


def build_production_controller() -> PromotionController:
    layout = Layout.production_layout()
    return PromotionController(
        layout=layout,
        validator=GenerationValidator(
            catalog_path=PRODUCTION_CATALOG,
            secret_catalog_path=PRODUCTION_SECRET_CATALOG,
            platform_path=PRODUCTION_PLATFORM,
            canonical_services_root=layout.canonical,
            enforce_owners=True,
        ),
        runtime_probe=ProductionRuntimeProbe(),
        signature_verifier=OpenSSHAttestationVerifier(
            layout=layout,
            platform_path=PRODUCTION_PLATFORM,
        ),
        repository_state_provider=repository_state,
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="action", required=True)
    commands.add_parser("status")
    import_plan = commands.add_parser("import-plan")
    import_plan.add_argument("--generation", required=True)
    import_candidate = commands.add_parser("import-candidate")
    import_candidate.add_argument("--generation", required=True)
    import_candidate.add_argument("--confirm", required=True)
    recover_import = commands.add_parser("recover-import")
    recover_import.add_argument(
        "--mode",
        choices=("abort", "complete"),
        required=True,
    )
    recover_import.add_argument("--apply", action="store_true")
    recover_import.add_argument("--confirm")
    bindings = commands.add_parser("attestation-bindings")
    bindings.add_argument("--candidate", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--candidate", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--candidate", required=True)
    apply.add_argument("--confirm", required=True)
    recover = commands.add_parser("recover")
    recover.add_argument("--mode", choices=("abort", "complete"), required=True)
    recover.add_argument("--apply", action="store_true")
    recover.add_argument("--confirm")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--generation", required=True)
    rollback.add_argument("--apply", action="store_true")
    rollback.add_argument("--confirm")
    return root


def is_mutating(args: argparse.Namespace) -> bool:
    return (
        args.action in {"apply", "import-candidate"}
        or (args.action == "recover" and args.apply)
        or (args.action == "recover-import" and args.apply)
        or (args.action == "rollback" and args.apply)
    )


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(argv if argv is not None else sys.argv[1:])
    args = parser().parse_args(raw_arguments)
    if os.geteuid() != 0:
        print("ERROR: production runtime-generation CLI requires root", file=sys.stderr)
        return 126
    if is_mutating(args):
        try:
            ensure_mutation_lock(
                HOST_LOCK_OPERATION,
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    *raw_arguments,
                ],
                caller=Path(__file__),
            )
        except HostGlobalLockError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 126
    try:
        controller = build_production_controller()
        if args.action == "status":
            print(
                json.dumps(
                    controller.status(),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.action == "import-plan":
            plan = controller.plan_candidate_import(require_generation(args.generation))
            print(json.dumps(plan.document(), indent=2, sort_keys=True))
            print(f"confirmation={plan.confirmation()}")
            return 0
        if args.action == "import-candidate":
            journal = controller.apply_candidate_import(
                require_generation(args.generation),
                args.confirm,
            )
            print(
                f"candidate import completed: transaction="
                f"{journal.identity['transactionId']} tip={journal.tip_hash}"
            )
            return 0
        if args.action == "recover-import":
            result = controller.recover_candidate_import(
                mode=args.mode,
                apply=args.apply,
                confirmation=args.confirm,
            )
            print(result)
            return 0
        if args.action == "attestation-bindings":
            print(
                json.dumps(
                    controller.attestation_bindings(require_generation(args.candidate)),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.action == "plan":
            plan = controller.plan_promotion(require_generation(args.candidate))
            print(json.dumps(plan.document(), indent=2, sort_keys=True))
            print(f"confirmation={plan.confirmation()}")
            return 0
        if args.action == "apply":
            journal = controller.apply_promotion(
                require_generation(args.candidate),
                args.confirm,
            )
            print(
                f"promotion completed: transaction="
                f"{journal.identity['transactionId']} tip={journal.tip_hash}"
            )
            return 0
        if args.action == "recover":
            result = controller.recover(
                mode=args.mode,
                apply=args.apply,
                confirmation=args.confirm,
            )
            print(result)
            return 0
        plan = controller.plan_rollback(require_generation(args.generation))
        if not args.apply:
            print(json.dumps(plan.document(), indent=2, sort_keys=True))
            print(f"confirmation={plan.confirmation()}")
            return 0
        journal = controller.apply_rollback(
            require_generation(args.generation),
            args.confirm or "",
        )
        print(
            f"rollback completed: transaction="
            f"{journal.identity['transactionId']} tip={journal.tip_hash}"
        )
        return 0
    except PromotionInterrupted as error:
        print(
            f"ERROR: transaction interrupted by signal {error.signal_number}",
            file=sys.stderr,
        )
        return 128 + error.signal_number
    except (
        PromotionError,
        HostGlobalLockError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
