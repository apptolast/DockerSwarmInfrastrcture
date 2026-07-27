#!/usr/bin/env python3
"""Validate restored workload data and atomically write the deployment gate.

This module deliberately has no command-line entry point.  The companion
``finalize_restore.sh`` performs live PostgreSQL checks first and is the only
supported caller.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any

import yaml

from backup_safety import SafetyError

EXPECTED_SERVICES = {
    "kropia",
    "minecraft",
    "minecraft-stats",
    "n8n",
    "openclaw-clean",
    "passbolt",
    "personal-website-alberto",
    "personal-website-pablo",
    "shlink",
}
DATASET_STATUSES = {
    "minecraft-mods": "restored-and-verified",
    "minecraft-worlds": "restored-and-verified",
    "n8n-home": "restored-and-verified",
    "n8n-postgres": "restored-and-verified",
    "openclaw-clean-home": "initialized-empty",
    "passbolt-gpg": "restored-and-verified",
    "passbolt-jwt": "restored-and-verified",
    "passbolt-postgres": "restored-and-verified",
    "shlink-postgres": "restored-and-verified",
}
EXPECTED_RELATIVE_PATHS = {
    "minecraft-mods": "minecraft/mods",
    "minecraft-worlds": "minecraft/data",
    "n8n-home": "n8n/home",
    "n8n-postgres": "n8n/postgres",
    "openclaw-clean-home": "openclaw-clean/home",
    "passbolt-gpg": "passbolt/gpg",
    "passbolt-jwt": "passbolt/jwt",
    "passbolt-postgres": "passbolt/postgres",
    "shlink-postgres": "shlink/postgres",
}
EXPECTED_OWNERS = {
    "minecraft-mods": (1000, 1000),
    "minecraft-worlds": (1000, 1000),
    "n8n-home": (1000, 1000),
    "n8n-postgres": (999, 999),
    "openclaw-clean-home": (1000, 1000),
    "passbolt-gpg": (33, 33),
    "passbolt-jwt": (33, 33),
    "passbolt-postgres": (70, 70),
    "shlink-postgres": (70, 70),
}
CRITICAL_FILES = {
    "minecraft-worlds": (
        "server_chavalda/level.dat",
        "world/level.dat",
        "world_berenejena/level.dat",
    ),
    "n8n-home": ("config",),
    "n8n-postgres": ("pgdata/PG_VERSION",),
    "passbolt-gpg": ("serverkey_private.asc",),
    "passbolt-jwt": ("jwt.key",),
    "passbolt-postgres": ("pgdata/PG_VERSION",),
    "shlink-postgres": ("pgdata/PG_VERSION",),
}
SECRET_IDENTITY_KEY_FILE = "workloads_secret_identity_hmac_key"
SECRET_IDENTITY_KEY_SIZE = 32
VECTOR_RESTORE_MARKERS = {
    "database-vectors-081": ("vectors", "0.8.1"),
    "database-rag-082": ("rag", "0.8.2"),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    require_regular(path, label)
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SafetyError(f"{label} no es YAML UTF-8 válido") from exc
    if not isinstance(document, dict):
        raise SafetyError(f"{label} debe contener un mapping")
    return document


def load_json_mapping(path: Path, label: str) -> dict[str, Any]:
    require_regular(path, label)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafetyError(f"{label} no es JSON UTF-8 válido") from exc
    if not isinstance(document, dict):
        raise SafetyError(f"{label} debe contener un objeto")
    return document


def require_regular(path: Path, label: str, *, nonempty: bool = True) -> None:
    try:
        status = path.lstat()
    except FileNotFoundError as exc:
        raise SafetyError(f"Falta {label}: {path}") from exc
    if not stat.S_ISREG(status.st_mode):
        raise SafetyError(f"{label} no es un fichero regular: {path}")
    if nonempty and status.st_size == 0:
        raise SafetyError(f"{label} está vacío: {path}")


def require_real_directory(path: Path, label: str) -> os.stat_result:
    try:
        status = path.lstat()
    except FileNotFoundError as exc:
        raise SafetyError(f"Falta {label}: {path}") from exc
    if not stat.S_ISDIR(status.st_mode):
        raise SafetyError(f"{label} no es un directorio real: {path}")
    return status


def validate_tree_types(root: Path, label: str) -> None:
    files = 0
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise SafetyError(f"Tipo o enlace no permitido en {label}: {path}")
        files += 1
    if label != "openclaw-clean-home" and files == 0:
        raise SafetyError(f"Dataset restaurado vacío: {label}")


def validate_catalog(
    catalog_path: Path,
    canonical_services_root: Path,
) -> tuple[str, set[str]]:
    catalog = load_yaml_mapping(catalog_path, "catálogo de servicios")
    if catalog.get("schema_version") != 1:
        raise SafetyError("schema_version del catálogo debe ser 1")
    catalog_version = catalog.get("catalog_version")
    if not isinstance(catalog_version, str):
        raise SafetyError("catalog_version ausente")
    target = catalog.get("target")
    if not isinstance(target, dict) or target.get("services_root") != str(
        canonical_services_root
    ):
        raise SafetyError("services_root del catálogo no coincide")

    approved = catalog.get("approved_services")
    if not isinstance(approved, list):
        raise SafetyError("approved_services inválido")
    approved_ids = {item.get("id") for item in approved if isinstance(item, dict)} - {
        "traefik-edge"
    }
    if approved_ids != EXPECTED_SERVICES:
        raise SafetyError("Servicios aprobados de workloads fuera de contrato")

    datasets = catalog.get("datasets")
    if not isinstance(datasets, list):
        raise SafetyError("datasets inválido")
    observed: dict[str, str] = {}
    for item in datasets:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise SafetyError("Entrada dataset inválida")
        identifier = item["id"]
        if identifier not in EXPECTED_RELATIVE_PATHS:
            continue
        target_path = item.get("target_path")
        expected = canonical_services_root / EXPECTED_RELATIVE_PATHS[identifier]
        if target_path != str(expected):
            raise SafetyError(f"Ruta de dataset fuera de contrato: {identifier}")
        observed[identifier] = str(target_path)
    if set(observed) != set(EXPECTED_RELATIVE_PATHS):
        raise SafetyError("El catálogo no cubre exactamente los datasets runtime")
    return catalog_version, approved_ids


def validate_secret_catalog(
    secret_catalog_path: Path,
    services_root: Path,
    runtime_manifest: dict[str, Any],
) -> None:
    catalog = load_yaml_mapping(secret_catalog_path, "catálogo de secrets")
    entries = catalog.get("workloads_secrets")
    if not isinstance(entries, list) or not entries:
        raise SafetyError("workloads_secrets inválido")
    source_files: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise SafetyError("Entrada de secret inválida")
        name = item.get("source_file")
        required = item.get("required_nonempty")
        if (
            not isinstance(name, str)
            or not name
            or "/" in name
            or "\\" in name
            or not isinstance(required, bool)
            or name in source_files
        ):
            raise SafetyError("Contrato source_file inválido o duplicado")
        source_files.add(name)
        path = services_root / "secrets/files" / name
        require_regular(
            path,
            f"source_file {name}",
            nonempty=bool(required),
        )
        if path.stat().st_mode & 0o077:
            raise SafetyError(f"Permisos demasiado amplios en source_file {name}")
    manifest_secret_files = runtime_manifest.get("secretFiles")
    if (
        not isinstance(manifest_secret_files, list)
        or any(not isinstance(item, str) for item in manifest_secret_files)
        or len(manifest_secret_files) != len(set(manifest_secret_files))
        or set(manifest_secret_files) != source_files
    ):
        raise SafetyError("runtime-manifest y catálogo de secrets difieren")
    if runtime_manifest.get("secretIdentityKeyFile") != SECRET_IDENTITY_KEY_FILE:
        raise SafetyError("runtime-manifest no identifica la clave HMAC de identidad")
    identity_key_path = services_root / "secrets/files" / SECRET_IDENTITY_KEY_FILE
    require_regular(
        identity_key_path,
        "clave HMAC de identidad de secrets",
        nonempty=True,
    )
    if identity_key_path.stat().st_mode & 0o077:
        raise SafetyError(
            "Permisos demasiado amplios en clave HMAC de identidad de secrets"
        )
    if identity_key_path.stat().st_size != SECRET_IDENTITY_KEY_SIZE:
        raise SafetyError("Tamaño inválido de clave HMAC de identidad de secrets")


def validate_restore_phase_markers(services_root: Path) -> None:
    state_dir = services_root / "restore-state"
    core = load_json_mapping(
        state_dir / "databases-core.json",
        "marker databases-core",
    )
    if (
        set(core) != {"schemaVersion", "phase", "completedAt"}
        or core.get("schemaVersion") != 1
        or core.get("phase") != "databases-core"
        or not isinstance(core.get("completedAt"), str)
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:" r"[0-9]{2}:[0-9]{2}Z",
            core["completedAt"],
        )
        is None
    ):
        raise SafetyError("Marker databases-core inválido")

    expected_keys = {
        "schemaVersion",
        "phase",
        "database",
        "dumpSha256",
        "tableCount",
        "schemaSha256",
        "vectorExtensionVersion",
        "completedAt",
    }
    for phase, (database, extension_version) in VECTOR_RESTORE_MARKERS.items():
        marker = load_json_mapping(
            state_dir / f"{phase}.json",
            f"marker {phase}",
        )
        dump = services_root / f"recovery/databases/{database}.dump"
        require_regular(dump, f"dump {database}", nonempty=True)
        if (
            set(marker) != expected_keys
            or marker.get("schemaVersion") != 1
            or marker.get("phase") != phase
            or marker.get("database") != database
            or marker.get("dumpSha256") != sha256_file(dump)
            or not isinstance(marker.get("tableCount"), int)
            or isinstance(marker.get("tableCount"), bool)
            or marker["tableCount"] <= 0
            or not isinstance(marker.get("schemaSha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", marker["schemaSha256"]) is None
            or marker.get("vectorExtensionVersion") != extension_version
            or not isinstance(marker.get("completedAt"), str)
            or re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:" r"[0-9]{2}:[0-9]{2}Z",
                marker["completedAt"],
            )
            is None
        ):
            raise SafetyError(f"Marker {phase} inválido u obsoleto")


def validate_datasets(
    services_root: Path,
    *,
    enforce_owners: bool,
) -> None:
    for identifier, relative in EXPECTED_RELATIVE_PATHS.items():
        path = services_root / relative
        status = require_real_directory(path, identifier)
        if status.st_mode & 0o022:
            raise SafetyError(f"Dataset escribible por grupo/otros: {identifier}")
        if (
            enforce_owners
            and (status.st_uid, status.st_gid) != EXPECTED_OWNERS[identifier]
        ):
            raise SafetyError(f"Ownership numérico incorrecto: {identifier}")
        validate_tree_types(path, identifier)
        for relative_file in CRITICAL_FILES.get(identifier, ()):
            require_regular(
                path / relative_file,
                f"{identifier}/{relative_file}",
            )

    n8n_binary_data = services_root / EXPECTED_RELATIVE_PATHS["n8n-home"] / "binaryData"
    require_real_directory(n8n_binary_data, "n8n-home/binaryData")

    openclaw_home = services_root / EXPECTED_RELATIVE_PATHS["openclaw-clean-home"]
    if any(openclaw_home.iterdir()):
        raise SafetyError("OpenClaw limpio contiene estado antes del despliegue")


def write_ready_marker(
    *,
    services_root: Path,
    canonical_services_root: Path,
    catalog_path: Path,
    secret_catalog_path: Path,
    enforce_owners: bool = True,
) -> Path:
    if not services_root.is_absolute() or services_root == Path("/"):
        raise SafetyError("services_root debe ser absoluto y distinto de /")
    require_real_directory(services_root, "services_root")
    catalog_version, approved_ids = validate_catalog(
        catalog_path,
        canonical_services_root,
    )
    runtime_manifest_path = services_root / "runtime-manifest.json"
    runtime_manifest = load_json_mapping(
        runtime_manifest_path,
        "runtime-manifest.json",
    )
    if runtime_manifest.get("schemaVersion") != 4:
        raise SafetyError("runtime-manifest schemaVersion no soportado")
    if runtime_manifest.get("openClawLegacyImported") is not False:
        raise SafetyError("runtime-manifest no excluye OpenClaw legado")
    source_backup = runtime_manifest.get("sourceBackup")
    if not isinstance(source_backup, str) or not source_backup.startswith(
        "apptolast-data-"
    ):
        raise SafetyError("runtime-manifest no identifica el backup origen")
    validate_secret_catalog(
        secret_catalog_path,
        services_root,
        runtime_manifest,
    )
    validate_restore_phase_markers(services_root)
    validate_datasets(services_root, enforce_owners=enforce_owners)

    recovery_manifest = services_root / "recovery/SHA256SUMS"
    require_regular(recovery_manifest, "manifiesto recovery")
    state_dir = services_root / "restore-state"
    require_real_directory(state_dir, "restore-state")
    marker_path = state_dir / "workloads-ready-v2.json"
    if marker_path.exists() or marker_path.is_symlink():
        raise SafetyError("Se rechaza sobrescribir workloads-ready-v2.json")
    identity_key_path = services_root / "secrets/files" / SECRET_IDENTITY_KEY_FILE
    marker = {
        "schemaVersion": 2,
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "catalogVersion": catalog_version,
        "catalogSha256": sha256_file(catalog_path),
        "runtimeManifestSha256": sha256_file(runtime_manifest_path),
        "sourceBackup": source_backup,
        "sourceBackupSha256": sha256_file(recovery_manifest),
        "sourceBackupDigestKind": "sha256-of-verified-recovery-manifest",
        "approvedServices": sorted(approved_ids),
        "datasets": DATASET_STATUSES,
        "databaseRestores": {
            "n8n": "verified",
            "passbolt": "verified",
            "shlink": "verified",
        },
        "openClawLegacyImported": False,
        "runtimeManifestSchemaVersion": 4,
        "secretIdentityKeySha256": sha256_file(identity_key_path),
        "secretIdentityKeyDigestKind": "sha256-of-random-hmac-key",
    }

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=state_dir,
            prefix=".workloads-ready-v2.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(marker, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, marker_path)
        temporary.unlink()
        temporary = None
        directory_descriptor = os.open(state_dir, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return marker_path
