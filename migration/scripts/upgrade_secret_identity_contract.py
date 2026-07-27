#!/usr/bin/env python3
"""Atomically upgrade a restored runtime to keyed Secret source identities.

The migration never touches application datasets or existing Secret source
files. It archives the byte-exact v3 manifest and v1 restore marker before
adding one random HMAC key, replacing only the runtime manifest, and creating
the v2 deployment gate last. The archived state supports a byte-exact,
non-destructive rollback.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
from typing import Any

import yaml

HOST_LOCK_DIRECTORY = Path(__file__).resolve().parents[2] / "scripts"
if str(HOST_LOCK_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(HOST_LOCK_DIRECTORY))
from host_global_operation_lock import ensure_mutation_lock  # noqa: E402

DEFAULT_SERVICES_ROOT = Path("/srv/dockerswarm/services")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SERVICE_CATALOG = REPOSITORY_ROOT / "config/services.yml"
DEFAULT_SECRET_CATALOG = REPOSITORY_ROOT / "stacks/workloads/secrets.yml"

IDENTITY_KEY_FILE = "workloads_secret_identity_hmac_key"
IDENTITY_KEY_SIZE = 32
IDENTITY_DIGEST_KIND = "sha256-of-random-hmac-key"
RECOVERY_DIGEST_KIND = "sha256-of-verified-recovery-manifest"
UPGRADE_ID = "workload-secret-source-hmac-v1"

LEGACY_MANIFEST_KEYS = {
    "edgeRecoveryArtifacts",
    "openClawLegacyImported",
    "openClawState",
    "preparedAt",
    "runtimeRootContract",
    "schemaVersion",
    "secretDelivery",
    "secretFiles",
    "sourceBackup",
}
LEGACY_MARKER_KEYS = {
    "schemaVersion",
    "completedAt",
    "catalogVersion",
    "catalogSha256",
    "runtimeManifestSha256",
    "sourceBackup",
    "sourceBackupSha256",
    "sourceBackupDigestKind",
    "approvedServices",
    "datasets",
    "databaseRestores",
    "openClawLegacyImported",
}
CURRENT_MARKER_KEYS = LEGACY_MARKER_KEYS | {
    "runtimeManifestSchemaVersion",
    "secretIdentityKeySha256",
    "secretIdentityKeyDigestKind",
}


class UpgradeError(RuntimeError):
    """The runtime cannot be safely upgraded or rolled back."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_private_file(
    path: Path,
    description: str,
    *,
    canonical: bool,
) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise UpgradeError(f"{description} is absent or unsafe: {path}")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise UpgradeError(f"{description} must be mode 0600: {path}")
    if canonical and metadata.st_uid != 0:
        raise UpgradeError(f"{description} must be owned by root: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise UpgradeError(f"cannot read {description}: {path}") from exc


def parse_json(data: bytes, description: str) -> dict[str, Any]:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpgradeError(f"{description} is not valid JSON") from exc
    if not isinstance(document, dict):
        raise UpgradeError(f"{description} is not a JSON object")
    return document


def load_yaml(path: Path, description: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise UpgradeError(f"{description} is absent or unsafe: {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise UpgradeError(f"cannot read {description}: {path}") from exc
    if not isinstance(document, dict):
        raise UpgradeError(f"{description} is not a mapping")
    return document


def validate_utc_timestamp(value: Any, description: str) -> None:
    try:
        timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise UpgradeError(f"{description} has an invalid timestamp") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() != timezone.utc.utcoffset(
        None
    ):
        raise UpgradeError(f"{description} timestamp is not UTC")


def expected_scope(
    service_catalog: dict[str, Any],
) -> tuple[set[str], dict[str, str]]:
    approved = service_catalog.get("approved_services")
    datasets = service_catalog.get("datasets")
    if not isinstance(approved, list) or not isinstance(datasets, list):
        raise UpgradeError("service catalog scope is invalid")
    services = {item["id"] for item in approved if item.get("id") != "traefik-edge"}
    states = {
        item["id"]: (
            "initialized-empty"
            if item.get("migration") == "initialize-empty"
            else "restored-and-verified"
        )
        for item in datasets
        if item.get("owner") != "traefik-edge"
    }
    return services, states


def expected_secret_files(secret_catalog: dict[str, Any]) -> set[str]:
    entries = secret_catalog.get("workloads_secrets")
    if not isinstance(entries, list) or not entries:
        raise UpgradeError("workload Secret catalog is invalid")
    sources = [item.get("source_file") for item in entries]
    if any(
        not isinstance(item, str) or not item or "/" in item or "\\" in item
        for item in sources
    ) or len(sources) != len(set(sources)):
        raise UpgradeError("workload Secret source catalog is invalid")
    return set(sources)


def validate_legacy_contract(
    manifest_data: bytes,
    marker_data: bytes,
    *,
    service_catalog: dict[str, Any],
    secret_catalog: dict[str, Any],
    service_catalog_sha256: str,
    recovery_manifest_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = parse_json(manifest_data, "legacy runtime manifest")
    marker = parse_json(marker_data, "legacy restore marker")
    if set(manifest) != LEGACY_MANIFEST_KEYS or manifest.get("schemaVersion") != 3:
        raise UpgradeError("legacy runtime manifest schema differs from v3")
    if (
        manifest.get("openClawLegacyImported") is not False
        or manifest.get("runtimeRootContract") != os.fspath(DEFAULT_SERVICES_ROOT)
        or manifest.get("secretDelivery") != "individual-docker-secret-source-files"
        or not isinstance(manifest.get("sourceBackup"), str)
        or not manifest["sourceBackup"].startswith("apptolast-data-")
    ):
        raise UpgradeError("legacy runtime manifest invariants are invalid")
    secret_files = manifest.get("secretFiles")
    expected_sources = expected_secret_files(secret_catalog)
    if (
        not isinstance(secret_files, list)
        or len(secret_files) != len(set(secret_files))
        or set(secret_files) != expected_sources
    ):
        raise UpgradeError("legacy runtime Secret sources differ from catalog")

    if set(marker) != LEGACY_MARKER_KEYS or marker.get("schemaVersion") != 1:
        raise UpgradeError("legacy restore marker schema differs from v1")
    validate_utc_timestamp(marker.get("completedAt"), "legacy restore marker")
    services, datasets = expected_scope(service_catalog)
    if (
        marker.get("catalogVersion") != service_catalog.get("catalog_version")
        or marker.get("catalogSha256") != service_catalog_sha256
        or marker.get("runtimeManifestSha256") != sha256_bytes(manifest_data)
        or marker.get("sourceBackup") != manifest["sourceBackup"]
        or marker.get("sourceBackupSha256") != recovery_manifest_sha256
        or marker.get("sourceBackupDigestKind") != RECOVERY_DIGEST_KIND
        or marker.get("approvedServices") != sorted(services)
        or marker.get("datasets") != datasets
        or marker.get("databaseRestores")
        != {"n8n": "verified", "passbolt": "verified", "shlink": "verified"}
        or marker.get("openClawLegacyImported") is not False
    ):
        raise UpgradeError("legacy restore evidence is stale or incomplete")
    return manifest, marker


def current_manifest_bytes(legacy_manifest: dict[str, Any]) -> bytes:
    current = dict(legacy_manifest)
    current["schemaVersion"] = 4
    current["secretIdentityKeyFile"] = IDENTITY_KEY_FILE
    return (json.dumps(current, indent=2, sort_keys=True) + "\n").encode("utf-8")


def current_marker(
    legacy_marker: dict[str, Any],
    *,
    runtime_manifest_sha256: str,
    identity_key_sha256: str,
    completed_at: str,
) -> dict[str, Any]:
    marker = dict(legacy_marker)
    marker.update(
        {
            "schemaVersion": 2,
            "completedAt": completed_at,
            "runtimeManifestSha256": runtime_manifest_sha256,
            "runtimeManifestSchemaVersion": 4,
            "secretIdentityKeySha256": identity_key_sha256,
            "secretIdentityKeyDigestKind": IDENTITY_DIGEST_KIND,
        }
    )
    return marker


def encode_json(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_exclusive(
    path: Path,
    data: bytes,
    *,
    canonical: bool,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = read_private_file(path, path.name, canonical=canonical)
        if existing != data:
            raise UpgradeError(f"existing archive differs: {path}")
        return
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)


def atomic_replace(path: Path, data: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def ensure_private_directory(path: Path, *, canonical: bool) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise UpgradeError(f"unsafe directory: {path}")
    else:
        path.mkdir(mode=0o700)
        fsync_directory(path.parent)
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise UpgradeError(f"directory must be mode 0700: {path}")
    if canonical and metadata.st_uid != 0:
        raise UpgradeError(f"directory must be owned by root: {path}")


def require_private_directory(path: Path, *, canonical: bool) -> None:
    if path.is_symlink() or not path.is_dir():
        raise UpgradeError(f"required private directory is absent or unsafe: {path}")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise UpgradeError(f"directory must be mode 0700: {path}")
    if canonical and metadata.st_uid != 0:
        raise UpgradeError(f"directory must be owned by root: {path}")


def read_identity_key(path: Path, *, canonical: bool) -> bytes:
    data = read_private_file(path, "Secret identity HMAC key", canonical=canonical)
    if len(data) != IDENTITY_KEY_SIZE:
        raise UpgradeError("Secret identity HMAC key must be exactly 32 bytes")
    return data


def validate_current_marker(
    marker: dict[str, Any],
    legacy_marker: dict[str, Any],
    *,
    runtime_sha256: str,
    identity_sha256: str,
) -> None:
    if set(marker) != CURRENT_MARKER_KEYS or marker.get("schemaVersion") != 2:
        raise UpgradeError("current restore marker schema differs from v2")
    validate_utc_timestamp(marker.get("completedAt"), "current restore marker")
    expected = current_marker(
        legacy_marker,
        runtime_manifest_sha256=runtime_sha256,
        identity_key_sha256=identity_sha256,
        completed_at=marker["completedAt"],
    )
    if marker != expected:
        raise UpgradeError("current restore marker is stale or inconsistent")


def paths(services_root: Path) -> dict[str, Path]:
    state = services_root / "restore-state"
    compatibility = state / "compat-v3"
    return {
        "manifest": services_root / "runtime-manifest.json",
        "recovery": services_root / "recovery/SHA256SUMS",
        "legacy_marker": state / "workloads-ready-v1.json",
        "current_marker": state / "workloads-ready-v2.json",
        "identity_key": services_root / "secrets/files" / IDENTITY_KEY_FILE,
        "compatibility": compatibility,
        "manifest_v3": compatibility / "runtime-manifest-v3.json",
        "marker_v1": compatibility / "workloads-ready-v1.json",
        "manifest_v4": compatibility / "runtime-manifest-v4.json",
        "evidence": compatibility / "secret-identity-upgrade-v1.json",
        "lock": state / ".secret-identity-upgrade.lock",
    }


def catalogs(
    service_catalog_path: Path,
    secret_catalog_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    service_data = service_catalog_path.read_bytes()
    return (
        load_yaml(service_catalog_path, "service catalog"),
        load_yaml(secret_catalog_path, "Secret catalog"),
        sha256_bytes(service_data),
    )


def apply_upgrade(
    services_root: Path,
    service_catalog_path: Path,
    secret_catalog_path: Path,
) -> dict[str, Any]:
    selected = paths(services_root)
    canonical = services_root == DEFAULT_SERVICES_ROOT
    service_catalog, secret_catalog, catalog_sha = catalogs(
        service_catalog_path,
        secret_catalog_path,
    )
    recovery_data = read_private_file(
        selected["recovery"],
        "recovery manifest",
        canonical=canonical,
    )
    marker_v1_data = read_private_file(
        selected["legacy_marker"],
        "legacy restore marker",
        canonical=canonical,
    )

    manifest_data = read_private_file(
        selected["manifest"],
        "runtime manifest",
        canonical=canonical,
    )
    manifest_document = parse_json(manifest_data, "runtime manifest")
    if manifest_document.get("schemaVersion") == 3:
        legacy_manifest_data = manifest_data
    elif manifest_document.get("schemaVersion") == 4:
        require_private_directory(
            selected["compatibility"],
            canonical=canonical,
        )
        legacy_manifest_data = read_private_file(
            selected["manifest_v3"],
            "archived v3 runtime manifest",
            canonical=canonical,
        )
    else:
        raise UpgradeError("runtime manifest is neither schema v3 nor v4")

    legacy_manifest, legacy_marker = validate_legacy_contract(
        legacy_manifest_data,
        marker_v1_data,
        service_catalog=service_catalog,
        secret_catalog=secret_catalog,
        service_catalog_sha256=catalog_sha,
        recovery_manifest_sha256=sha256_bytes(recovery_data),
    )
    ensure_private_directory(selected["compatibility"], canonical=canonical)
    write_exclusive(
        selected["manifest_v3"],
        legacy_manifest_data,
        canonical=canonical,
    )
    write_exclusive(
        selected["marker_v1"],
        marker_v1_data,
        canonical=canonical,
    )

    if selected["identity_key"].exists():
        identity_key = read_identity_key(
            selected["identity_key"],
            canonical=canonical,
        )
    else:
        write_exclusive(
            selected["identity_key"],
            secrets.token_bytes(IDENTITY_KEY_SIZE),
            canonical=canonical,
        )
        identity_key = read_identity_key(
            selected["identity_key"],
            canonical=canonical,
        )

    manifest_v4_data = current_manifest_bytes(legacy_manifest)
    if manifest_document.get("schemaVersion") == 3:
        atomic_replace(selected["manifest"], manifest_v4_data)
    elif manifest_data != manifest_v4_data:
        raise UpgradeError("current v4 runtime manifest differs from migration")

    runtime_sha = sha256_bytes(manifest_v4_data)
    identity_sha = sha256_bytes(identity_key)
    if selected["current_marker"].exists():
        marker_v2_data = read_private_file(
            selected["current_marker"],
            "current restore marker",
            canonical=canonical,
        )
        marker_v2 = parse_json(marker_v2_data, "current restore marker")
        validate_current_marker(
            marker_v2,
            legacy_marker,
            runtime_sha256=runtime_sha,
            identity_sha256=identity_sha,
        )
    else:
        marker_v2 = current_marker(
            legacy_marker,
            runtime_manifest_sha256=runtime_sha,
            identity_key_sha256=identity_sha,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        marker_v2_data = encode_json(marker_v2)
        write_exclusive(
            selected["current_marker"],
            marker_v2_data,
            canonical=canonical,
        )

    evidence = {
        "schemaVersion": 1,
        "migration": UPGRADE_ID,
        "legacyRuntimeManifestSha256": sha256_bytes(legacy_manifest_data),
        "legacyRestoreMarkerSha256": sha256_bytes(marker_v1_data),
        "currentRuntimeManifestSha256": runtime_sha,
        "currentRestoreMarkerSha256": sha256_bytes(marker_v2_data),
        "secretIdentityKeySha256": identity_sha,
    }
    write_exclusive(
        selected["evidence"],
        encode_json(evidence),
        canonical=canonical,
    )
    return {
        "schemaVersion": 1,
        "action": "applied",
        "runtimeManifestSchemaVersion": 4,
        "restoreMarkerSchemaVersion": 2,
        "rollbackArchive": os.fspath(selected["compatibility"]),
    }


def rollback_upgrade(
    services_root: Path,
    service_catalog_path: Path,
    secret_catalog_path: Path,
) -> dict[str, Any]:
    selected = paths(services_root)
    canonical = services_root == DEFAULT_SERVICES_ROOT
    service_catalog, secret_catalog, catalog_sha = catalogs(
        service_catalog_path,
        secret_catalog_path,
    )
    legacy_manifest_data = read_private_file(
        selected["manifest_v3"],
        "archived v3 runtime manifest",
        canonical=canonical,
    )
    marker_v1_data = read_private_file(
        selected["marker_v1"],
        "archived v1 restore marker",
        canonical=canonical,
    )
    recovery_data = read_private_file(
        selected["recovery"],
        "recovery manifest",
        canonical=canonical,
    )
    legacy_manifest, legacy_marker = validate_legacy_contract(
        legacy_manifest_data,
        marker_v1_data,
        service_catalog=service_catalog,
        secret_catalog=secret_catalog,
        service_catalog_sha256=catalog_sha,
        recovery_manifest_sha256=sha256_bytes(recovery_data),
    )
    current_data = read_private_file(
        selected["manifest"],
        "current runtime manifest",
        canonical=canonical,
    )
    expected_current = current_manifest_bytes(legacy_manifest)
    if current_data != expected_current:
        raise UpgradeError("current runtime manifest differs; rollback refused")
    identity_key = read_identity_key(
        selected["identity_key"],
        canonical=canonical,
    )
    marker_v2_data = read_private_file(
        selected["current_marker"],
        "current restore marker",
        canonical=canonical,
    )
    validate_current_marker(
        parse_json(marker_v2_data, "current restore marker"),
        legacy_marker,
        runtime_sha256=sha256_bytes(expected_current),
        identity_sha256=sha256_bytes(identity_key),
    )
    write_exclusive(
        selected["manifest_v4"],
        current_data,
        canonical=canonical,
    )
    atomic_replace(selected["manifest"], legacy_manifest_data)
    return {
        "schemaVersion": 1,
        "action": "rolled-back",
        "runtimeManifestSchemaVersion": 3,
        "restoreMarkerSchemaVersion": 1,
        "preservedUpgradeEvidence": os.fspath(selected["compatibility"]),
    }


def status(services_root: Path) -> dict[str, Any]:
    selected = paths(services_root)
    manifest = parse_json(
        selected["manifest"].read_bytes(),
        "runtime manifest",
    )
    return {
        "schemaVersion": 1,
        "action": "status",
        "runtimeManifestSchemaVersion": manifest.get("schemaVersion"),
        "legacyMarkerPresent": selected["legacy_marker"].is_file(),
        "currentMarkerPresent": selected["current_marker"].is_file(),
        "identityKeyPresent": selected["identity_key"].is_file(),
        "rollbackArchivePresent": (
            selected["manifest_v3"].is_file() and selected["marker_v1"].is_file()
        ),
    }


def run_locked(
    action: str,
    services_root: Path,
    service_catalog: Path,
    secret_catalog: Path,
) -> dict[str, Any]:
    selected = paths(services_root)
    canonical = services_root == DEFAULT_SERVICES_ROOT
    require_private_directory(services_root, canonical=canonical)
    require_private_directory(
        services_root / "restore-state",
        canonical=canonical,
    )
    require_private_directory(
        services_root / "secrets/files",
        canonical=canonical,
    )
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(selected["lock"], flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        lock_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(lock_metadata.st_mode):
            raise UpgradeError("migration lock is not a regular file")
        if canonical and lock_metadata.st_uid != 0:
            raise UpgradeError("migration lock must be owned by root")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if action == "apply":
            return apply_upgrade(
                services_root,
                service_catalog,
                secret_catalog,
            )
        return rollback_upgrade(
            services_root,
            service_catalog,
            secret_catalog,
        )
    except BlockingIOError as exc:
        raise UpgradeError("another Secret identity migration is active") from exc
    finally:
        os.close(descriptor)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "apply", "rollback"))
    parser.add_argument(
        "--services-root",
        type=Path,
        default=DEFAULT_SERVICES_ROOT,
    )
    parser.add_argument(
        "--service-catalog",
        type=Path,
        default=DEFAULT_SERVICE_CATALOG,
    )
    parser.add_argument(
        "--secret-catalog",
        type=Path,
        default=DEFAULT_SECRET_CATALOG,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    services_root = Path(os.path.abspath(args.services_root))
    service_catalog = Path(os.path.abspath(args.service_catalog))
    secret_catalog = Path(os.path.abspath(args.secret_catalog))
    try:
        if (
            services_root == DEFAULT_SERVICES_ROOT
            and os.geteuid() != 0
            and args.action != "status"
        ):
            raise UpgradeError("the canonical runtime migration requires root")
        if services_root == Path("/") or not services_root.is_absolute():
            raise UpgradeError("services root must be absolute and not /")
        if services_root.is_symlink() or not services_root.is_dir():
            raise UpgradeError("services root is absent or unsafe")
        if services_root == DEFAULT_SERVICES_ROOT and args.action in {
            "apply",
            "rollback",
        }:
            ensure_mutation_lock(
                f"migration-secret-identity-{args.action}",
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    *sys.argv[1:],
                ],
                caller=Path(__file__),
            )
        os.umask(0o077)
        if args.action == "status":
            result = status(services_root)
        else:
            result = run_locked(
                args.action,
                services_root,
                service_catalog,
                secret_catalog,
            )
    except (OSError, UpgradeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
