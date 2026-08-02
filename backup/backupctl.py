#!/usr/bin/env python3
"""Fail-closed backup and recovery controller for the mononode Swarm.

The controller never embeds credentials, never removes live application data,
and serializes every repository operation through a single host lock.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

SCHEMA_VERSION = 1
RESTIC_VERSION = "0.19.1"
SNAPSHOT_PATTERN = re.compile(r"^[a-f0-9]{8,64}$")
SERVICE_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")
DATASET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]+$")
R2_REPOSITORY_PATTERN = re.compile(
    r"^s3:https://[a-f0-9]{32}\.r2\.cloudflarestorage\.com/"
    r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9](?:/[a-zA-Z0-9._/-]+)?$"
)
SENSITIVE_ENVIRONMENT_KEYS = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SHARED_CREDENTIALS_FILE",
        "RESTIC_PASSWORD",
        "RESTIC_PASSWORD_COMMAND",
        "RESTIC_PASSWORD_FILE",
        "RESTIC_REPOSITORY",
    }
)


def host_lock_helper() -> Any:
    """Load the same helper from a source checkout or installed libexec path."""

    current = Path(__file__).resolve()
    candidates = (
        current.parent / "host_global_operation_lock.py",
        current.parents[1] / "scripts" / "host_global_operation_lock.py",
    )
    for candidate in candidates:
        if not candidate.is_file() or candidate.is_symlink():
            continue
        spec = importlib.util.spec_from_file_location(
            "dockerswarm_host_global_operation_lock",
            candidate,
        )
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    fail("host-global operation-lock helper is unavailable")


class BackupError(RuntimeError):
    """An expected safety, backup, verification, or restore failure."""


@dataclasses.dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str


def fail(message: str) -> NoReturn:
    raise BackupError(message)


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, document: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run(
    argv: Sequence[str],
    *,
    environment: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 900,
    cwd: Path | None = None,
) -> CommandResult:
    if not argv or any(not isinstance(value, str) or not value for value in argv):
        fail("refusing to execute an empty or malformed command")
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            input=input_text,
            timeout=timeout,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError) as error:
        fail(f"command could not run: {argv[0]}: {error}")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        if len(detail) > 1200:
            detail = f"{detail[:1200]}..."
        fail(f"command failed ({argv[0]}, rc={completed.returncode}): {detail}")
    return CommandResult(completed.stdout, completed.stderr)


def run_stdout_to_file(
    argv: Sequence[str],
    destination: Path,
    *,
    environment: dict[str, str] | None = None,
    timeout: int = 3600,
) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            completed = subprocess.run(
                list(argv),
                check=False,
                stdout=stream,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=timeout,
            )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            fail(
                f"streaming command failed ({argv[0]}, "
                f"rc={completed.returncode}): {detail[:1200]}"
            )
        if destination.stat().st_size == 0:
            fail(f"streaming command produced an empty file: {destination.name}")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def run_file_to_stdin(
    argv: Sequence[str],
    source: Path,
    *,
    environment: dict[str, str] | None = None,
    timeout: int = 3600,
) -> CommandResult:
    with source.open("rb") as stream:
        try:
            completed = subprocess.run(
                list(argv),
                check=False,
                stdin=stream,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as error:
            fail(f"command could not read {source.name}: {error}")
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        fail(
            f"verification command failed ({argv[0]}, "
            f"rc={completed.returncode}): {(stderr or stdout)[:1200]}"
        )
    return CommandResult(stdout, stderr)


def absolute_safe_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        fail(f"{label} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute() or path == Path("/"):
        fail(f"{label} must be absolute and must not be /")
    if ".." in path.parts:
        fail(f"{label} must not contain parent traversal")
    return path


def normalize_archive_path(
    value: str,
    *,
    base: PurePosixPath = PurePosixPath(),
    label: str,
) -> PurePosixPath:
    candidate = PurePosixPath(value)
    if candidate.is_absolute():
        fail(f"absolute {label} is forbidden: {value!r}")
    parts = [*base.parts]
    for part in candidate.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                fail(f"{label} escapes the archive root: {value!r}")
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        fail(f"{label} resolves to the archive root: {value!r}")
    return PurePosixPath(*parts)


def validate_tar_members(archive: Path) -> int:
    members_seen = 0
    member_types: dict[PurePosixPath, str] = {}
    links: list[tuple[PurePosixPath, PurePosixPath, bool]] = []
    try:
        with tarfile.open(archive, "r:") as tar_stream:
            for member in tar_stream:
                member_path = normalize_archive_path(
                    member.name,
                    label=f"member in {archive.name}",
                )
                if member_path in member_types:
                    fail(f"duplicate member in {archive.name}: {member.name!r}")
                if member.isdir():
                    member_type = "directory"
                elif member.isfile():
                    member_type = "file"
                elif member.issym():
                    member_type = "symlink"
                elif member.islnk():
                    member_type = "hardlink"
                else:
                    fail(
                        f"special or unsupported member is forbidden in "
                        f"{archive.name}: {member.name!r}"
                    )
                member_types[member_path] = member_type
                if member.issym() or member.islnk():
                    link_value = PurePosixPath(member.linkname)
                    if link_value.is_absolute():
                        fail(
                            f"absolute link target is forbidden in "
                            f"{archive.name}: {member.linkname!r}"
                        )
                    link_base = (
                        member_path.parent if member.issym() else PurePosixPath()
                    )
                    target = normalize_archive_path(
                        member.linkname,
                        base=link_base,
                        label=f"link target in {archive.name}",
                    )
                    links.append((member_path, target, member.islnk()))
                members_seen += 1
    except (tarfile.TarError, OSError) as error:
        fail(f"invalid tar archive {archive.name}: {error}")
    if members_seen == 0:
        fail(f"tar archive is empty: {archive.name}")
    for member_path, target, is_hardlink in links:
        if is_hardlink and member_types.get(target) != "file":
            fail(
                f"hardlink target is not a regular archived file in "
                f"{archive.name}: {member_path} -> {target}"
            )
    return members_seen


def validate_manifest_tree(root: Path) -> tuple[Path, dict[str, Any]]:
    manifests = list(root.rglob("manifest.json"))
    if len(manifests) != 1:
        fail(f"restore must contain exactly one manifest, found {len(manifests)}")
    manifest_path = manifests[0]
    if manifest_path.is_symlink() or not manifest_path.is_file():
        fail("restored manifest must be a regular non-symlink file")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"invalid restored manifest: {error}")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        fail("restored manifest schema is not supported")
    kind = manifest.get("kind")
    allowed_artifact_types = {
        "application": {"filesystem-tar", "postgres-custom-dump"},
        "swarm-state": {"secret-file", "swarm-state-tar"},
    }
    if kind not in allowed_artifact_types:
        fail("restored manifest kind is not supported")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        fail("restored manifest has no artifacts")
    base = manifest_path.parent.resolve()
    artifact_ids: set[str] = set()
    artifact_files: set[str] = set()
    artifact_paths: set[Path] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            fail("restored manifest contains a malformed artifact")
        identifier = artifact.get("id")
        artifact_type = artifact.get("type")
        relative = artifact.get("file")
        expected = artifact.get("sha256")
        if (
            not isinstance(identifier, str)
            or not DATASET_PATTERN.fullmatch(identifier)
            or identifier in artifact_ids
            or artifact_type not in allowed_artifact_types[kind]
            or not isinstance(relative, str)
            or relative in artifact_files
            or not isinstance(expected, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected)
        ):
            fail("restored manifest contains invalid artifact metadata")
        artifact_ids.add(identifier)
        artifact_files.add(relative)
        candidate = (base / relative).resolve()
        if (
            not candidate.is_relative_to(base)
            or candidate in artifact_paths
            or candidate.is_symlink()
            or not candidate.is_file()
        ):
            fail(f"manifest artifact is absent or escapes the restore: {relative}")
        artifact_paths.add(candidate)
        if sha256_file(candidate) != expected:
            fail(f"manifest checksum mismatch: {relative}")
        size = artifact.get("size")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
            or size != candidate.stat().st_size
        ):
            fail(f"manifest size mismatch: {relative}")
        if artifact.get("type") in {"filesystem-tar", "swarm-state-tar"}:
            members = validate_tar_members(candidate)
            if artifact.get("members") != members:
                fail(f"manifest member count mismatch: {relative}")
        if artifact.get("type") == "postgres-custom-dump":
            extensions = artifact.get("extensions")
            rehearsal_image = artifact.get("rehearsal_image")
            if (
                not isinstance(extensions, dict)
                or not isinstance(rehearsal_image, str)
                or not re.fullmatch(
                    r"[a-z0-9./_-]+@sha256:[a-f0-9]{64}",
                    rehearsal_image,
                )
            ):
                fail("database artifact has no safe rehearsal/extension contract")
            for extension, version in extensions.items():
                if (
                    not isinstance(extension, str)
                    or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", extension)
                    or not isinstance(version, str)
                    or not re.fullmatch(
                        r"[0-9]+(?:\.[0-9]+){1,3}",
                        version,
                    )
                ):
                    fail("database artifact has an unsafe extension contract")
            schema_inventory = artifact.get("schema_inventory")
            if (
                not isinstance(schema_inventory, list)
                or not schema_inventory
                or len(schema_inventory) != len(set(schema_inventory))
                or any(
                    not isinstance(value, str) or not value
                    for value in schema_inventory
                )
                or not re.fullmatch(
                    r"[a-f0-9]{64}",
                    str(artifact.get("dump_catalog_sha256", "")),
                )
                or not isinstance(artifact.get("dump_catalog_entries"), int)
                or isinstance(artifact.get("dump_catalog_entries"), bool)
                or artifact["dump_catalog_entries"] < 1
            ):
                fail("database artifact has invalid structural invariants")
    return manifest_path, manifest


def validate_restored_contract(
    config: BackupConfig, manifest: dict[str, Any]
) -> None:
    artifacts = manifest["artifacts"]
    observed = {artifact["id"]: artifact for artifact in artifacts}
    if manifest["kind"] == "swarm-state":
        if {
            identifier: artifact["type"] for identifier, artifact in observed.items()
        } != {
            "swarm-state": "swarm-state-tar",
            "swarm-unlock-key": "secret-file",
        }:
            fail("Swarm-state snapshot artifact allowlist differs")
        return

    databases = {database["id"]: database for database in config.document["databases"]}
    datasets = {dataset["id"]: dataset for dataset in config.document["datasets"]}
    expected_types = {
        **dict.fromkeys(databases, "postgres-custom-dump"),
        **dict.fromkeys(datasets, "filesystem-tar"),
    }
    if {
        identifier: artifact["type"] for identifier, artifact in observed.items()
    } != expected_types:
        fail("application snapshot artifact allowlist differs")

    expected_groups: dict[str, str] = {}
    for group in config.document["consistency_groups"]:
        for identifier in [*group["databases"], *group["datasets"]]:
            expected_groups[identifier] = group["id"]
    for identifier, dataset in datasets.items():
        if dataset["consistency"] == "minecraft-save":
            expected_groups[identifier] = "minecraft-rcon"
        artifact = observed[identifier]
        if (
            artifact.get("source") != dataset["source"]
            or artifact.get("consistency_group") != expected_groups[identifier]
        ):
            fail(f"filesystem artifact contract differs: {identifier}")
    for identifier, database in databases.items():
        artifact = observed[identifier]
        if (
            artifact.get("service") != database["service"]
            or artifact.get("database") != database["database"]
            or artifact.get("rehearsal_image") != database["rehearsal_image"]
            or artifact.get("extensions") != database["extensions"]
            or artifact.get("consistency_group") != expected_groups[identifier]
            or not isinstance(artifact.get("image"), str)
            or not re.fullmatch(
                r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}",
                artifact["image"],
            )
        ):
            fail(f"database artifact contract differs: {identifier}")

    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict):
        fail("application snapshot metadata is missing")
    observed_groups = metadata.get("consistency_groups")
    if not isinstance(observed_groups, list):
        fail("application snapshot consistency metadata is missing")
    expected_group_metadata = {
        group["id"]: {
            "services": group["services"],
            "artifacts": [*group["databases"], *group["datasets"]],
        }
        for group in config.document["consistency_groups"]
    }
    normalized_groups: dict[str, dict[str, list[str]]] = {}
    for group in observed_groups:
        if (
            not isinstance(group, dict)
            or set(group)
            != {"id", "services", "artifacts", "started_at", "finished_at"}
            or not isinstance(group["id"], str)
            or group["id"] in normalized_groups
            or not all(
                isinstance(group[key], str)
                and re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T" r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
                    group[key],
                )
                for key in ("started_at", "finished_at")
            )
        ):
            fail("application snapshot has malformed consistency metadata")
        normalized_groups[group["id"]] = {
            "services": group["services"],
            "artifacts": group["artifacts"],
        }
    if normalized_groups != expected_group_metadata:
        fail("application snapshot consistency metadata differs")
    minecraft_metadata = metadata.get("minecraft_consistency")
    minecraft_ids = [
        identifier
        for identifier, dataset in datasets.items()
        if dataset["consistency"] == "minecraft-save"
    ]
    if minecraft_metadata != {
        "service": config.section("services")["minecraft"],
        "artifacts": minecraft_ids,
        "protocol": "rcon-save-off-save-all-flush-save-on",
    }:
        fail("application snapshot Minecraft consistency metadata differs")

    service_metadata = metadata.get("services")
    expected_services = {
        service
        for group in config.document["consistency_groups"]
        for service in group["services"]
    }
    expected_services.add(config.section("services")["minecraft"])
    expected_services.update(
        database["service"] for database in config.document["databases"]
    )
    if not isinstance(service_metadata, dict) or set(service_metadata) != (
        expected_services
    ):
        fail("application snapshot service metadata differs")
    for service, value in service_metadata.items():
        if (
            not isinstance(value, dict)
            or set(value) != {"desired_replicas", "running_replicas", "image"}
            or not isinstance(value["desired_replicas"], int)
            or isinstance(value["desired_replicas"], bool)
            or value["desired_replicas"] < 1
            or not isinstance(value["running_replicas"], int)
            or isinstance(value["running_replicas"], bool)
            or value["running_replicas"] != value["desired_replicas"]
            or not isinstance(value["image"], str)
            or "@sha256:" not in value["image"]
        ):
            fail(f"application snapshot service metadata is invalid: {service}")


@dataclasses.dataclass(frozen=True)
class BackupConfig:
    path: Path
    document: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> BackupConfig:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            fail(f"cannot read backup configuration: {error}")
        if not isinstance(document, dict):
            fail("backup configuration must be a JSON object")
        config = cls(path=path, document=document)
        config.validate()
        return config

    def section(self, name: str) -> dict[str, Any]:
        section = self.document.get(name)
        if not isinstance(section, dict):
            fail(f"configuration section is missing or malformed: {name}")
        return section

    def validate(self) -> None:
        if self.document.get("schema_version") != SCHEMA_VERSION:
            fail("unsupported backup configuration schema")

        restic = self.section("restic")
        repository = restic.get("repository")
        if not isinstance(repository, str) or not R2_REPOSITORY_PATTERN.fullmatch(
            repository
        ):
            fail("restic.repository must be a credential-free Cloudflare R2 URL")
        if "@" in repository or "?" in repository or "#" in repository:
            fail("restic.repository must not embed credentials or query values")
        if restic.get("version") != RESTIC_VERSION:
            fail(f"restic.version must be exactly {RESTIC_VERSION}")
        if not re.fullmatch(r"[a-f0-9]{64}", str(restic.get("binary_sha256", ""))):
            fail("restic.binary_sha256 is invalid")
        for key in (
            "binary",
            "password_file",
            "access_key_id_file",
            "secret_access_key_file",
            "cache_directory",
        ):
            absolute_safe_path(restic.get(key), f"restic.{key}")

        runtime = self.section("runtime")
        for key in (
            "lock_file",
            "staging_directory",
            "status_directory",
            "metrics_directory",
        ):
            absolute_safe_path(runtime.get(key), f"runtime.{key}")
        hostname = runtime.get("hostname")
        if not isinstance(hostname, str) or not SERVICE_PATTERN.fullmatch(hostname):
            fail("runtime.hostname is invalid")

        services = self.section("services")
        if set(services) != {"minecraft"}:
            fail("services must contain only the Minecraft service")
        minecraft_service = services.get("minecraft")
        if not isinstance(minecraft_service, str) or not SERVICE_PATTERN.fullmatch(
            minecraft_service
        ):
            fail(f"invalid Swarm service name: {minecraft_service!r}")

        databases = self.document.get("databases")
        if not isinstance(databases, list) or len(databases) != 5:
            fail("exactly five logical PostgreSQL databases must be configured")
        database_ids: set[str] = set()
        reviewed_databases = {
            "n8n-postgres": {
                "service": "workloads_n8n-db",
                "database": "n8n",
                "username": "n8n",
                "rehearsal_image": (
                    "docker.io/pgvector/pgvector@sha256:"
                    "7d400e340efb42f4d8c9c12c6427adb253f726881a9985d2"
                    "a471bf0eed824dff"
                ),
                "extensions": {},
            },
            "n8n-vectors": {
                "service": "workloads_n8n-db",
                "database": "vectors",
                "username": "n8n",
                "rehearsal_image": (
                    "docker.io/pgvector/pgvector@sha256:"
                    "33198da2828a14c30348d2ccb4750833d5ed9a44c88d840a"
                    "0e523d7417120337"
                ),
                "extensions": {"vector": "0.8.1"},
            },
            "n8n-rag": {
                "service": "workloads_n8n-db",
                "database": "rag",
                "username": "n8n",
                "rehearsal_image": (
                    "docker.io/pgvector/pgvector@sha256:"
                    "7d400e340efb42f4d8c9c12c6427adb253f726881a9985d2"
                    "a471bf0eed824dff"
                ),
                "extensions": {"vector": "0.8.2"},
            },
            "passbolt-postgres": {
                "service": "workloads_passbolt-db",
                "database": "passbolt",
                "username": "passbolt",
                "rehearsal_image": (
                    "docker.io/library/postgres@sha256:"
                    "fceb6f86328c36f2438fae3b851b0cc57c4a7e69a58c866d"
                    "9ce24281f2cf0c9c"
                ),
                "extensions": {},
            },
            "shlink-postgres": {
                "service": "workloads_shlink-db",
                "database": "shlink",
                "username": "shlink",
                "rehearsal_image": (
                    "docker.io/library/postgres@sha256:"
                    "66266770619a23ab310c7fa60043b6d1fa041038cb232ced5"
                    "9d2c509fecd297b"
                ),
                "extensions": {},
            },
        }
        observed_databases: dict[str, dict[str, Any]] = {}
        for database in databases:
            if not isinstance(database, dict) or set(database) != {
                "id",
                "service",
                "database",
                "username",
                "rehearsal_image",
                "extensions",
            }:
                fail("database entry is malformed")
            identifier = database.get("id")
            service = database.get("service")
            if (
                not isinstance(identifier, str)
                or not DATASET_PATTERN.fullmatch(identifier)
                or identifier in database_ids
            ):
                fail("database IDs must be unique safe names")
            if not isinstance(service, str) or not SERVICE_PATTERN.fullmatch(service):
                fail("database services must be safe names")
            for key in ("database", "username"):
                if not isinstance(database.get(key), str) or not re.fullmatch(
                    r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*", database[key]
                ):
                    fail(f"database {identifier} has invalid {key}")
            rehearsal_image = database.get("rehearsal_image")
            if not isinstance(rehearsal_image, str) or not re.fullmatch(
                r"[a-z0-9./_-]+@sha256:[a-f0-9]{64}",
                rehearsal_image,
            ):
                fail(f"database {identifier} has an unsafe rehearsal image")
            extensions = database.get("extensions")
            if not isinstance(extensions, dict):
                fail(f"database {identifier} extensions must be a mapping")
            for extension, version in extensions.items():
                if (
                    not isinstance(extension, str)
                    or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", extension)
                    or not isinstance(version, str)
                    or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version)
                ):
                    fail(f"database {identifier} has an invalid extension lock")
            database_ids.add(identifier)
            observed_databases[identifier] = {
                key: database[key]
                for key in (
                    "service",
                    "database",
                    "username",
                    "rehearsal_image",
                    "extensions",
                )
            }
        if observed_databases != reviewed_databases:
            fail("database allowlist differs from the reviewed recovery contract")

        datasets = self.document.get("datasets")
        if not isinstance(datasets, list) or not datasets:
            fail("datasets must be a non-empty list")
        dataset_ids: set[str] = set()
        minecraft_datasets = 0
        reviewed_dataset_consistency = {
            "n8n-home": "quiesced",
            "passbolt-gpg": "quiesced",
            "passbolt-jwt": "quiesced",
            "minecraft-data": "minecraft-save",
            "minecraft-mods": "minecraft-save",
            "openclaw-clean-home": "quiesced",
            "traefik-acme": "quiesced",
            "runtime-secret-source": "immutable",
            "observability-secret-source": "immutable",
            "observability-prometheus": "quiesced",
            "observability-alertmanager": "quiesced",
            "observability-loki": "quiesced",
            "observability-grafana": "quiesced",
        }
        observed_dataset_consistency: dict[str, str] = {}
        for dataset in datasets:
            if not isinstance(dataset, dict):
                fail("dataset entry is malformed")
            identifier = dataset.get("id")
            if (
                not isinstance(identifier, str)
                or not DATASET_PATTERN.fullmatch(identifier)
                or identifier in dataset_ids
            ):
                fail("dataset IDs must be unique safe names")
            absolute_safe_path(dataset.get("source"), f"dataset {identifier}")
            consistency = dataset.get("consistency")
            if consistency not in {"immutable", "quiesced", "minecraft-save"}:
                fail(f"dataset {identifier} has invalid consistency mode")
            if consistency == "minecraft-save":
                minecraft_datasets += 1
            dataset_ids.add(identifier)
            observed_dataset_consistency[identifier] = consistency
        if observed_dataset_consistency != reviewed_dataset_consistency:
            fail("dataset allowlist differs from the reviewed recovery contract")
        if minecraft_datasets != 2:
            fail("both and only Minecraft datasets must use minecraft-save")

        reviewed_groups = {
            "n8n": {
                "services": ["workloads_n8n", "workloads_n8n-runners"],
                "databases": ["n8n-postgres", "n8n-vectors", "n8n-rag"],
                "datasets": ["n8n-home"],
            },
            "passbolt": {
                "services": ["workloads_passbolt"],
                "databases": ["passbolt-postgres"],
                "datasets": ["passbolt-gpg", "passbolt-jwt"],
            },
            "shlink": {
                "services": ["workloads_shlink"],
                "databases": ["shlink-postgres"],
                "datasets": [],
            },
            "openclaw": {
                "services": ["workloads_openclaw"],
                "databases": [],
                "datasets": ["openclaw-clean-home"],
            },
            "traefik": {
                "services": ["edge_traefik"],
                "databases": [],
                "datasets": ["traefik-acme"],
            },
            "observability-prometheus": {
                "services": ["observability_prometheus"],
                "databases": [],
                "datasets": ["observability-prometheus"],
            },
            "observability-alertmanager": {
                "services": ["observability_alertmanager"],
                "databases": [],
                "datasets": ["observability-alertmanager"],
            },
            "observability-loki": {
                "services": ["observability_loki"],
                "databases": [],
                "datasets": ["observability-loki"],
            },
            "observability-grafana": {
                "services": ["observability_grafana"],
                "databases": [],
                "datasets": ["observability-grafana"],
            },
            "immutable-secrets": {
                "services": [],
                "databases": [],
                "datasets": [
                    "runtime-secret-source",
                    "observability-secret-source",
                ],
            },
        }
        groups = self.document.get("consistency_groups")
        if not isinstance(groups, list) or len(groups) != len(reviewed_groups):
            fail("consistency group allowlist is missing or malformed")
        observed_groups: dict[str, dict[str, list[str]]] = {}
        grouped_services: list[str] = []
        grouped_databases: list[str] = []
        grouped_datasets: list[str] = []
        for group in groups:
            if not isinstance(group, dict) or set(group) != {
                "id",
                "services",
                "databases",
                "datasets",
            }:
                fail("consistency group entry is malformed")
            identifier = group.get("id")
            if (
                not isinstance(identifier, str)
                or not DATASET_PATTERN.fullmatch(identifier)
                or identifier in observed_groups
            ):
                fail("consistency group IDs must be unique safe names")
            for key in ("services", "databases", "datasets"):
                values = group.get(key)
                if (
                    not isinstance(values, list)
                    or len(values) != len(set(values))
                    or any(not isinstance(value, str) for value in values)
                ):
                    fail(f"consistency group {identifier} has invalid {key}")
            for service in group["services"]:
                if not SERVICE_PATTERN.fullmatch(service):
                    fail(f"invalid grouped Swarm service name: {service!r}")
            observed_groups[identifier] = {
                key: group[key] for key in ("services", "databases", "datasets")
            }
            grouped_services.extend(group["services"])
            grouped_databases.extend(group["databases"])
            grouped_datasets.extend(group["datasets"])
        if observed_groups != reviewed_groups:
            fail("consistency groups differ from the reviewed recovery contract")
        if len(grouped_services) != len(set(grouped_services)):
            fail("writer services cannot appear in multiple consistency groups")
        if set(grouped_databases) != database_ids or len(grouped_databases) != len(
            database_ids
        ):
            fail("each database must appear in exactly one consistency group")
        quiesced_dataset_ids = {
            identifier
            for identifier, consistency in reviewed_dataset_consistency.items()
            if consistency != "minecraft-save"
        }
        if set(grouped_datasets) != quiesced_dataset_ids or len(
            grouped_datasets
        ) != len(quiesced_dataset_ids):
            fail("each non-Minecraft dataset needs one consistency group")

        retention = self.section("retention")
        for key in ("hourly", "daily", "weekly", "monthly", "yearly"):
            value = retention.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                fail(f"retention.{key} must be a positive integer")

        swarm = self.section("swarm_state")
        if swarm.get("require_autolock") is not True:
            fail("swarm_state.require_autolock must be true")
        for key in ("source", "unlock_key_file"):
            absolute_safe_path(swarm.get(key), f"swarm_state.{key}")
        if Path(swarm["unlock_key_file"]) != Path(
            "/etc/dockerswarm/backup/swarm-unlock-key"
        ):
            fail("swarm_state.unlock_key_file differs from the platform lifecycle")
        size = swarm.get("rehearsal_tmpfs_size")
        if not isinstance(size, str) or not re.fullmatch(r"[1-9][0-9]*[mMgG]", size):
            fail("swarm_state.rehearsal_tmpfs_size is invalid")

    def ensure_runtime_security(self) -> None:
        if os.geteuid() != 0:
            fail("backup controller must run as root")
        config_stat = self.path.stat()
        if not stat.S_ISREG(config_stat.st_mode) or self.path.is_symlink():
            fail("backup configuration must be a regular non-symlink file")
        if config_stat.st_uid != 0 or config_stat.st_mode & 0o022:
            fail("backup configuration must be root-owned and not writable by group")
        for key in (
            "password_file",
            "access_key_id_file",
            "secret_access_key_file",
        ):
            self.ensure_secret_file(
                absolute_safe_path(self.section("restic")[key], f"restic.{key}")
            )
        repository_password = (
            Path(self.section("restic")["password_file"]).read_bytes().rstrip(b"\r\n")
        )
        if len(repository_password) < 32:
            fail("restic repository password must contain at least 32 bytes")

    @staticmethod
    def ensure_secret_file(path: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError as error:
            fail(f"required credential file is unavailable: {path}: {error}")
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or path.is_symlink()
        ):
            fail(f"credential file must be root:root 0600 and regular: {path}")
        value = path.read_bytes()
        if not value.strip() or b"\x00" in value or len(value) > 4096:
            fail(f"credential file is empty or malformed: {path}")


class Repository:
    def __init__(self, config: BackupConfig):
        self.config = config
        self.settings = config.section("restic")
        self.binary = absolute_safe_path(self.settings["binary"], "restic.binary")

    def verify_binary(self) -> None:
        if (
            not self.binary.is_file()
            or self.binary.is_symlink()
            or not os.access(self.binary, os.X_OK)
        ):
            fail(f"locked restic binary is unavailable: {self.binary}")
        if sha256_file(self.binary) != self.settings["binary_sha256"]:
            fail("installed restic binary checksum differs from the lock")
        result = run([str(self.binary), "version"], timeout=30)
        if not result.stdout.startswith(f"restic {RESTIC_VERSION} "):
            fail(f"installed restic version is not {RESTIC_VERSION}")

    def environment(self) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in SENSITIVE_ENVIRONMENT_KEYS
            and not key.startswith("AWS_")
            and not key.startswith("RESTIC_")
        }
        access_key = (
            Path(self.settings["access_key_id_file"])
            .read_text(encoding="utf-8")
            .strip()
        )
        secret_key = (
            Path(self.settings["secret_access_key_file"])
            .read_text(encoding="utf-8")
            .strip()
        )
        if not re.fullmatch(r"[A-Za-z0-9]{16,128}", access_key):
            fail("R2 access-key ID file has an invalid format")
        if not re.fullmatch(r"[A-Za-z0-9/+=_-]{16,256}", secret_key):
            fail("R2 secret-access-key file has an invalid format")
        environment.update(
            {
                "AWS_ACCESS_KEY_ID": access_key,
                "AWS_SECRET_ACCESS_KEY": secret_key,
                "AWS_DEFAULT_REGION": "auto",
                "RESTIC_REPOSITORY": self.settings["repository"],
                "RESTIC_PASSWORD_FILE": self.settings["password_file"],
                "RESTIC_CACHE_DIR": self.settings["cache_directory"],
            }
        )
        return environment

    def call(
        self,
        arguments: Sequence[str],
        *,
        timeout: int = 3600,
        cwd: Path | None = None,
    ) -> CommandResult:
        return run(
            (
                [str(self.binary), "--no-cache", *arguments]
                if arguments and arguments[0] == "init"
                else [str(self.binary), *arguments]
            ),
            environment=self.environment(),
            timeout=timeout,
            cwd=cwd,
        )

    def check_exists(self) -> None:
        self.call(["snapshots", "--json", "--latest", "1"], timeout=120)

    def initialize(self) -> bool:
        try:
            self.check_exists()
            return False
        except BackupError:
            pass
        self.call(["init"], timeout=120)
        self.check_exists()
        return True

    def backup(self, source: Path, tag: str, hostname: str) -> str:
        result = self.call(
            [
                "backup",
                ".",
                "--host",
                hostname,
                "--tag",
                tag,
                "--tag",
                f"schema-v{SCHEMA_VERSION}",
                "--json",
            ],
            timeout=21600,
            cwd=source,
        )
        snapshot_id = ""
        for line in result.stdout.splitlines():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("message_type") == "summary" and isinstance(
                message.get("snapshot_id"), str
            ):
                snapshot_id = message["snapshot_id"]
        if not SNAPSHOT_PATTERN.fullmatch(snapshot_id):
            fail("restic did not report a valid snapshot ID")
        self.call(["snapshots", "--json", snapshot_id], timeout=120)
        return snapshot_id

    def apply_retention(self, tag: str, hostname: str) -> None:
        policy = self.config.section("retention")
        self.call(
            [
                "forget",
                "--host",
                hostname,
                "--tag",
                tag,
                "--group-by",
                "host,tags",
                "--keep-hourly",
                str(policy["hourly"]),
                "--keep-daily",
                str(policy["daily"]),
                "--keep-weekly",
                str(policy["weekly"]),
                "--keep-monthly",
                str(policy["monthly"]),
                "--keep-yearly",
                str(policy["yearly"]),
                "--prune",
            ],
            timeout=21600,
        )

    def check(self, read_all_data: bool) -> None:
        arguments = ["check"]
        if read_all_data:
            arguments.append("--read-data")
        self.call(arguments, timeout=43200)

    def restore(self, snapshot: str, target: Path) -> None:
        self.call(
            ["restore", snapshot, "--target", str(target), "--verify"],
            timeout=43200,
        )


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    production = path.parent == Path("/run/lock")
    path.parent.mkdir(
        mode=0o755 if production else 0o700,
        parents=True,
        exist_ok=True,
    )
    parent_state = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent_state.st_mode)
        or (
            production
            and (
                parent_state.st_uid != 0
                or parent_state.st_gid != 0
                or stat.S_IMODE(parent_state.st_mode) != 0o1777
            )
        )
        or (
            not production
            and (
                parent_state.st_uid != os.geteuid()
                or parent_state.st_gid != os.getegid()
                or stat.S_IMODE(parent_state.st_mode) != 0o700
            )
        )
    ):
        fail("backup lock directory is unsafe")
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    created = False
    try:
        descriptor = os.open(
            path,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            fail(f"cannot safely open backup lock: {error}")
    try:
        if created:
            os.fchmod(descriptor, 0o600)
        expected_uid = 0 if production else os.geteuid()
        expected_gid = 0 if production else os.getegid()
        lock_state = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_state.st_mode)
            or stat.S_IMODE(lock_state.st_mode) != 0o600
            or lock_state.st_uid != expected_uid
            or lock_state.st_gid != expected_gid
            or lock_state.st_nlink != 1
        ):
            fail("backup lock inode is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("another backup, verification, or restore operation is active")
        path_state = path.lstat()
        if (
            path_state.st_dev != lock_state.st_dev
            or path_state.st_ino != lock_state.st_ino
            or path_state.st_nlink != 1
        ):
            fail("backup lock path changed during acquisition")
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode())
        yield
        final_descriptor_state = os.fstat(descriptor)
        final_path_state = path.lstat()
        if (
            final_descriptor_state.st_dev != lock_state.st_dev
            or final_descriptor_state.st_ino != lock_state.st_ino
            or final_descriptor_state.st_nlink != 1
            or final_path_state.st_dev != lock_state.st_dev
            or final_path_state.st_ino != lock_state.st_ino
            or final_path_state.st_nlink != 1
        ):
            fail("backup lock inode was replaced while held")
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


class RuntimeStatus:
    def __init__(self, config: BackupConfig, kind: str):
        runtime = config.section("runtime")
        self.kind = kind
        self.status_path = Path(runtime["status_directory"]) / f"{kind}.json"
        self.metrics_path = (
            Path(runtime["metrics_directory"]) / "dockerswarm_backup.prom"
        )
        self.status_directory = Path(runtime["status_directory"])
        self.started = int(time.time())

    def write(self, success: bool, snapshot: str = "", detail: str = "") -> None:
        finished = int(time.time())
        document = {
            "schema_version": SCHEMA_VERSION,
            "kind": self.kind,
            "success": success,
            "started_timestamp": self.started,
            "finished_timestamp": finished,
            "duration_seconds": max(0, finished - self.started),
            "snapshot_id": snapshot,
            "detail": detail[:500],
            "updated_at": utc_timestamp(),
        }
        atomic_json(self.status_path, document)
        statuses: list[dict[str, Any]] = []
        if self.status_directory.exists():
            for status_path in sorted(self.status_directory.glob("*.json")):
                try:
                    value = json.loads(status_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict) and isinstance(value.get("kind"), str):
                    statuses.append(value)
        lines = [
            "# HELP dockerswarm_backup_last_run_success Whether the last run succeeded.",
            "# TYPE dockerswarm_backup_last_run_success gauge",
            "# HELP dockerswarm_backup_last_finished_timestamp_seconds "
            "Unix timestamp of the last finished run.",
            "# TYPE dockerswarm_backup_last_finished_timestamp_seconds gauge",
            "# HELP dockerswarm_backup_last_duration_seconds Duration of the last run.",
            "# TYPE dockerswarm_backup_last_duration_seconds gauge",
        ]
        for item in statuses:
            label = item["kind"].replace("\\", "\\\\").replace('"', '\\"')
            value = 1 if item.get("success") is True else 0
            lines.append(
                f'dockerswarm_backup_last_run_success{{kind="{label}"}} {value}'
            )
            lines.append(
                "dockerswarm_backup_last_finished_timestamp_seconds"
                f'{{kind="{label}"}} {int(item.get("finished_timestamp", 0))}'
            )
            lines.append(
                f'dockerswarm_backup_last_duration_seconds{{kind="{label}"}} '
                f'{int(item.get("duration_seconds", 0))}'
            )
        atomic_text(self.metrics_path, "\n".join(lines) + "\n")


class Swarm:
    def __init__(self) -> None:
        self.docker = "/usr/bin/docker"

    def inspect_service(self, name: str) -> dict[str, Any]:
        result = run([self.docker, "service", "inspect", name])
        try:
            documents = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            fail(f"Docker returned invalid service metadata for {name}: {error}")
        if not isinstance(documents, list) or len(documents) != 1:
            fail(f"Swarm service is absent or ambiguous: {name}")
        return documents[0]

    def replicas(self, name: str) -> tuple[int, int]:
        document = self.inspect_service(name)
        replicated = document.get("Spec", {}).get("Mode", {}).get("Replicated")
        if not isinstance(replicated, dict):
            fail(f"service must use replicated mode: {name}")
        desired = replicated.get("Replicas")
        running = document.get("ServiceStatus", {}).get("RunningTasks")
        if (
            not isinstance(desired, int)
            or isinstance(desired, bool)
            or not isinstance(running, int)
            or isinstance(running, bool)
        ):
            fail(f"service replica status is unavailable: {name}")
        return desired, running

    def service_image(self, name: str) -> str:
        document = self.inspect_service(name)
        image = (
            document.get("Spec", {})
            .get("TaskTemplate", {})
            .get("ContainerSpec", {})
            .get("Image")
        )
        if not isinstance(image, str) or "@sha256:" not in image:
            fail(f"service image is not immutable: {name}")
        return image

    def wait_running(self, name: str, expected: int, timeout: int = 300) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            desired, running = self.replicas(name)
            if desired == expected and running == expected:
                return
            time.sleep(2)
        fail(f"service did not converge to {expected} replicas: {name}")

    def scale(self, name: str, replicas: int) -> None:
        run(
            [
                self.docker,
                "service",
                "scale",
                "--detach=false",
                f"{name}={replicas}",
            ],
            timeout=600,
        )
        self.wait_running(name, replicas)

    def one_container(self, service: str) -> str:
        result = run(
            [
                self.docker,
                "ps",
                "--filter",
                f"label=com.docker.swarm.service.name={service}",
                "--filter",
                "status=running",
                "--format",
                "{{.ID}}",
            ]
        )
        containers = [line for line in result.stdout.splitlines() if line]
        if len(containers) != 1:
            fail(
                f"expected exactly one running container for {service}, "
                f"found {len(containers)}"
            )
        return containers[0]

    def assert_manager(self) -> None:
        result = run(
            [
                self.docker,
                "info",
                "--format",
                "{{json .Swarm.LocalNodeState}} " "{{json .Swarm.ControlAvailable}}",
            ]
        )
        if result.stdout.strip() != '"active" true':
            fail("local Docker Engine is not an active Swarm manager")


@contextlib.contextmanager
def quiesced_services(swarm: Swarm, service_names: Sequence[str]) -> Iterator[None]:
    original: list[tuple[str, int]] = []
    primary_error: BaseException | None = None
    try:
        for service in service_names:
            desired, running = swarm.replicas(service)
            if desired < 1 or running != desired:
                fail(f"writer service is not fully available before backup: {service}")
            original.append((service, desired))
        for service, _ in original:
            swarm.scale(service, 0)
        yield
    except BaseException as error:
        primary_error = error
    recovery_errors: list[str] = []
    for service, replicas in reversed(original):
        try:
            swarm.scale(service, replicas)
        except BackupError as error:
            recovery_errors.append(str(error))
    if recovery_errors:
        detail = "; ".join(recovery_errors)
        if primary_error is not None:
            fail(f"{primary_error}; writer recovery also failed: {detail}")
        fail(f"writer recovery failed: {detail}")
    if primary_error is not None:
        raise primary_error


@contextlib.contextmanager
def suspended_minecraft_saves(swarm: Swarm, service: str) -> Iterator[str]:
    desired, running = swarm.replicas(service)
    if desired != 1 or running != 1:
        fail("Minecraft must have exactly one available replica for backup")
    container = swarm.one_container(service)
    save_off_attempted = False
    primary_error: BaseException | None = None
    try:
        # Treat dispatch as ambiguous: a timeout or signal may arrive after
        # rcon accepted save-off but before the Docker CLI reports success.
        save_off_attempted = True
        run(
            [swarm.docker, "exec", container, "rcon-cli", "save-off"],
            timeout=60,
        )
        run(
            [
                swarm.docker,
                "exec",
                container,
                "rcon-cli",
                "save-all",
                "flush",
            ],
            timeout=300,
        )
        yield container
    except BaseException as error:
        primary_error = error
    try:
        if save_off_attempted:
            run(
                [swarm.docker, "exec", container, "rcon-cli", "save-on"],
                timeout=60,
            )
    except BackupError as error:
        if primary_error is not None:
            fail(f"{primary_error}; Minecraft save-on also failed: {error}")
        raise
    if primary_error is not None:
        raise primary_error


def archive_source(identifier: str, source: Path, destination: Path) -> dict[str, Any]:
    if not source.exists() or source.is_symlink():
        fail(f"required backup source is absent or a root symlink: {source}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    run(
        [
            "/usr/bin/tar",
            "--create",
            "--file",
            str(destination),
            "--acls",
            "--xattrs",
            "--selinux",
            "--sparse",
            "--numeric-owner",
            "--one-file-system",
            "--directory",
            str(source.parent),
            "--",
            source.name,
        ],
        timeout=7200,
    )
    os.chmod(destination, 0o600)
    members = validate_tar_members(destination)
    return {
        "id": identifier,
        "type": "filesystem-tar",
        "source": str(source),
        "file": str(destination.relative_to(destination.parents[1])),
        "sha256": sha256_file(destination),
        "size": destination.stat().st_size,
        "members": members,
    }


def database_schema_inventory(
    docker: str,
    container: str,
    username: str,
    database: str,
) -> list[str]:
    result = run(
        [
            docker,
            "exec",
            container,
            "psql",
            "--username",
            username,
            "--dbname",
            database,
            "--tuples-only",
            "--no-align",
            "--command",
            (
                "SELECT n.nspname || '.' || c.relname || '|' || c.relkind "
                "FROM pg_catalog.pg_class AS c "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                "AND n.nspname NOT LIKE 'pg_toast%' "
                "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') "
                "ORDER BY n.nspname, c.relname, c.relkind"
            ),
        ]
    )
    inventory = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not inventory or len(inventory) != len(set(inventory)):
        fail(f"database schema inventory is empty or duplicated: {database}")
    return inventory


def dump_database(
    swarm: Swarm,
    database: dict[str, Any],
    destination: Path,
) -> dict[str, Any]:
    service = database["service"]
    container = swarm.one_container(service)
    expected_extensions = database["extensions"]
    observed_extensions: dict[str, str] = {}
    if expected_extensions:
        extension_names = ", ".join(f"'{name}'" for name in sorted(expected_extensions))
        extension_result = run(
            [
                swarm.docker,
                "exec",
                container,
                "psql",
                "--username",
                database["username"],
                "--dbname",
                database["database"],
                "--tuples-only",
                "--no-align",
                "--field-separator=|",
                "--command",
                (
                    "SELECT extname, extversion FROM pg_extension "
                    f"WHERE extname IN ({extension_names}) ORDER BY extname"
                ),
            ]
        )
        for line in extension_result.stdout.splitlines():
            name, separator, version = line.strip().partition("|")
            if not separator or not name or not version:
                fail(f"invalid extension inventory for database {database['id']}")
            observed_extensions[name] = version
    if observed_extensions != expected_extensions:
        fail(
            f"extension contract differs for database {database['id']}: "
            f"{observed_extensions!r}"
        )
    schema_inventory = database_schema_inventory(
        swarm.docker,
        container,
        database["username"],
        database["database"],
    )
    run_stdout_to_file(
        [
            swarm.docker,
            "exec",
            container,
            "pg_dump",
            "--format=custom",
            "--compress=6",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            f"--username={database['username']}",
            f"--dbname={database['database']}",
        ],
        destination,
    )
    listing = run_file_to_stdin(
        [swarm.docker, "exec", "-i", container, "pg_restore", "--list"],
        destination,
    )
    if not listing.stdout.strip():
        fail(f"pg_restore returned an empty catalog for {database['id']}")
    catalog_entries = [
        line
        for line in listing.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith(";")
    ]
    if not catalog_entries:
        fail(f"pg_restore returned no entries for {database['id']}")
    return {
        "id": database["id"],
        "type": "postgres-custom-dump",
        "service": service,
        "database": database["database"],
        "image": swarm.service_image(service),
        "rehearsal_image": database["rehearsal_image"],
        "extensions": observed_extensions,
        "schema_inventory": schema_inventory,
        "dump_catalog_sha256": hashlib.sha256(
            listing.stdout.encode("utf-8")
        ).hexdigest(),
        "dump_catalog_entries": len(catalog_entries),
        "file": str(destination.relative_to(destination.parents[1])),
        "sha256": sha256_file(destination),
        "size": destination.stat().st_size,
    }


def make_stage(config: BackupConfig, prefix: str) -> Path:
    root = Path(config.section("runtime")["staging_directory"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=root))
    os.chmod(stage, 0o700)
    return stage


def cleanup_stage(config: BackupConfig, stage: Path) -> None:
    root = Path(config.section("runtime")["staging_directory"]).resolve()
    resolved = stage.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        fail("refusing to clean a staging path outside the configured root")
    shutil.rmtree(resolved)


def write_manifest(
    stage: Path,
    *,
    kind: str,
    artifacts: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> Path:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "created_at": utc_timestamp(),
        "hostname": socket.gethostname(),
        "artifacts": artifacts,
        "metadata": metadata,
    }
    path = stage / "manifest.json"
    atomic_json(path, manifest)
    return path


def command_application(config: BackupConfig, repository: Repository) -> str:
    swarm = Swarm()
    swarm.assert_manager()
    repository.check_exists()
    services = config.section("services")
    datasets = config.document["datasets"]
    databases_by_id = {
        database["id"]: database for database in config.document["databases"]
    }
    datasets_by_id = {dataset["id"]: dataset for dataset in datasets}
    consistency_groups = config.document["consistency_groups"]
    minecraft = [
        dataset for dataset in datasets if dataset["consistency"] == "minecraft-save"
    ]
    for dataset in datasets:
        source = Path(dataset["source"])
        if not source.exists() or source.is_symlink():
            fail(f"required application dataset is unavailable: {source}")

    stage = make_stage(config, "application")
    artifacts: list[dict[str, Any]] = []
    service_metadata: dict[str, Any] = {}
    try:
        (stage / "databases").mkdir(mode=0o700)
        (stage / "filesystems").mkdir(mode=0o700)
        group_metadata: list[dict[str, Any]] = []
        for group in consistency_groups:
            started_at = utc_timestamp()
            group_artifact_ids: list[str] = []
            with quiesced_services(swarm, group["services"]):
                for database_id in group["databases"]:
                    database = databases_by_id[database_id]
                    artifact = dump_database(
                        swarm,
                        database,
                        stage / "databases" / f"{database_id}.dump",
                    )
                    artifact["consistency_group"] = group["id"]
                    artifacts.append(artifact)
                    group_artifact_ids.append(database_id)
                for dataset_id in group["datasets"]:
                    dataset = datasets_by_id[dataset_id]
                    artifact = archive_source(
                        dataset_id,
                        Path(dataset["source"]),
                        stage / "filesystems" / f"{dataset_id}.tar",
                    )
                    artifact["consistency_group"] = group["id"]
                    artifacts.append(artifact)
                    group_artifact_ids.append(dataset_id)
            group_metadata.append(
                {
                    "id": group["id"],
                    "services": group["services"],
                    "artifacts": group_artifact_ids,
                    "started_at": started_at,
                    "finished_at": utc_timestamp(),
                }
            )
        with suspended_minecraft_saves(swarm, services["minecraft"]):
            for dataset in minecraft:
                artifact = archive_source(
                    dataset["id"],
                    Path(dataset["source"]),
                    stage / "filesystems" / f"{dataset['id']}.tar",
                )
                artifact["consistency_group"] = "minecraft-rcon"
                artifacts.append(artifact)
        metadata_services = [
            service for group in consistency_groups for service in group["services"]
        ]
        metadata_services.extend(
            [
                services["minecraft"],
                *(database["service"] for database in config.document["databases"]),
            ]
        )
        for service in dict.fromkeys(metadata_services):
            desired, running = swarm.replicas(service)
            service_metadata[service] = {
                "desired_replicas": desired,
                "running_replicas": running,
                "image": swarm.service_image(service),
            }
        write_manifest(
            stage,
            kind="application",
            artifacts=artifacts,
            metadata={
                "consistency_groups": group_metadata,
                "minecraft_consistency": {
                    "service": services["minecraft"],
                    "artifacts": [dataset["id"] for dataset in minecraft],
                    "protocol": "rcon-save-off-save-all-flush-save-on",
                },
                "services": service_metadata,
            },
        )
        snapshot = repository.backup(
            stage,
            "application",
            config.section("runtime")["hostname"],
        )
        repository.apply_retention("application", config.section("runtime")["hostname"])
        return snapshot
    finally:
        cleanup_stage(config, stage)


def systemd_is_active(unit: str) -> bool:
    completed = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "--quiet", unit],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def wait_systemd(unit: str, active: bool, timeout: int = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if systemd_is_active(unit) is active:
            return
        time.sleep(1)
    state = "active" if active else "inactive"
    fail(f"{unit} did not become {state}")


def swarm_identity() -> dict[str, Any]:
    result = run(
        [
            "/usr/bin/docker",
            "info",
            "--format",
            (
                "{{json .Swarm.Cluster.ID}}|"
                "{{json .Swarm.Cluster.Spec.EncryptionConfig.AutoLockManagers}}|"
                "{{json .Swarm.NodeID}}|"
                "{{json .ServerVersion}}"
            ),
        ]
    )
    encoded_fields = result.stdout.strip().split("|")
    if len(encoded_fields) != 4:
        fail("Docker returned malformed Swarm identity fields")
    try:
        swarm_id, autolock, node_id, engine_version = (
            json.loads(value) for value in encoded_fields
        )
    except json.JSONDecodeError as error:
        fail(f"Docker returned invalid Swarm identity JSON: {error}")
    if (
        not isinstance(swarm_id, str)
        or not swarm_id
        or not isinstance(autolock, bool)
        or not isinstance(node_id, str)
        or not node_id
        or not isinstance(engine_version, str)
        or not engine_version
    ):
        fail("Swarm ID or autolock state is unavailable")
    return {
        "id": swarm_id,
        "autolock": autolock,
        "node_id": node_id,
        "engine_version": engine_version,
    }


def start_and_unlock_docker(
    *,
    socket_was_active: bool,
    expected_swarm_id: str,
) -> None:
    run(["/usr/bin/systemctl", "start", "docker.service"], timeout=180)
    wait_systemd("docker.service", True)
    run(
        [
            "/usr/bin/systemctl",
            "start",
            "dockerswarm-swarm-unlock.service",
        ],
        timeout=180,
    )
    if socket_was_active:
        run(["/usr/bin/systemctl", "start", "docker.socket"], timeout=60)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            identity = swarm_identity()
        except BackupError:
            time.sleep(2)
            continue
        if identity["id"] == expected_swarm_id:
            return
        fail("Docker restarted with an unexpected Swarm identity")
    fail("Docker restarted but Swarm did not become available")


def command_swarm_state(config: BackupConfig, repository: Repository) -> str:
    Swarm().assert_manager()
    repository.check_exists()
    settings = config.section("swarm_state")
    source = Path(settings["source"])
    if source != Path("/var/lib/docker/swarm"):
        fail("Swarm state source must be exactly /var/lib/docker/swarm")
    if not source.is_dir() or source.is_symlink():
        fail("Swarm state source is absent or unsafe")
    identity = swarm_identity()
    if settings["require_autolock"] and identity["autolock"] is not True:
        fail("Swarm autolock is required before state backup")
    unlock_path = Path(settings["unlock_key_file"])
    config.ensure_secret_file(unlock_path)
    unlock_key = unlock_path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"SWMKEY-1-[A-Za-z0-9/+=_-]{32,}", unlock_key):
        fail("escrowed Swarm unlock key has an invalid format")
    current_key = run(["/usr/bin/docker", "swarm", "unlock-key", "-q"]).stdout.strip()
    if not hmac.compare_digest(unlock_key, current_key):
        fail("escrowed Swarm unlock key differs from the active key")

    if not systemd_is_active("docker.service"):
        fail("docker.service must be active before Swarm state backup")
    socket_was_active = systemd_is_active("docker.socket")
    stage = make_stage(config, "swarm-state")
    docker_was_stopped = False
    primary_error: BaseException | None = None
    try:
        archive = stage / "swarm-state.tar"
        key_escrow = stage / "swarm-unlock-key"
        shutil.copyfile(unlock_path, key_escrow)
        os.chmod(key_escrow, 0o600)
        try:
            if socket_was_active:
                run(["/usr/bin/systemctl", "stop", "docker.socket"], timeout=60)
            docker_was_stopped = True
            run(["/usr/bin/systemctl", "stop", "docker.service"], timeout=180)
            wait_systemd("docker.service", False)
            run(
                [
                    "/usr/bin/tar",
                    "--create",
                    "--file",
                    str(archive),
                    "--acls",
                    "--xattrs",
                    "--selinux",
                    "--sparse",
                    "--numeric-owner",
                    "--one-file-system",
                    "--directory",
                    "/var/lib/docker",
                    "--",
                    "swarm",
                ],
                timeout=7200,
            )
            os.chmod(archive, 0o600)
            validate_tar_members(archive)
        except BaseException as error:
            primary_error = error
        if docker_was_stopped:
            try:
                start_and_unlock_docker(
                    socket_was_active=socket_was_active,
                    expected_swarm_id=identity["id"],
                )
                docker_was_stopped = False
            except BaseException as error:
                if primary_error is None:
                    primary_error = error
                else:
                    primary_error = BackupError(
                        f"{primary_error}; Docker recovery also failed: {error}"
                    )
        if primary_error is not None:
            raise primary_error
        artifacts = [
            {
                "id": "swarm-state",
                "type": "swarm-state-tar",
                "source": str(source),
                "file": archive.name,
                "sha256": sha256_file(archive),
                "size": archive.stat().st_size,
                "members": validate_tar_members(archive),
            },
            {
                "id": "swarm-unlock-key",
                "type": "secret-file",
                "source": str(unlock_path),
                "file": key_escrow.name,
                "sha256": sha256_file(key_escrow),
                "size": key_escrow.stat().st_size,
            },
        ]
        write_manifest(
            stage,
            kind="swarm-state",
            artifacts=artifacts,
            metadata={"swarm": identity},
        )
        snapshot = repository.backup(
            stage,
            "swarm-state",
            config.section("runtime")["hostname"],
        )
        repository.apply_retention("swarm-state", config.section("runtime")["hostname"])
        return snapshot
    finally:
        final_recovery_error: BaseException | None = None
        if docker_was_stopped:
            try:
                start_and_unlock_docker(
                    socket_was_active=socket_was_active,
                    expected_swarm_id=identity["id"],
                )
            except BaseException as error:
                final_recovery_error = error
        try:
            cleanup_stage(config, stage)
        except BaseException as error:
            if final_recovery_error is None:
                final_recovery_error = error
            else:
                final_recovery_error = BackupError(
                    f"{final_recovery_error}; staging cleanup also failed: {error}"
                )
        if final_recovery_error is not None:
            raise final_recovery_error


def ensure_empty_target(target: Path) -> None:
    if target.exists():
        if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
            fail("restore target must be absent or an empty non-symlink directory")
    else:
        target.mkdir(mode=0o700, parents=True)
    os.chmod(target, 0o700)


def restore_snapshot(
    config: BackupConfig,
    repository: Repository,
    snapshot: str,
    target: Path,
    extract_to: Path | None,
) -> dict[str, Any]:
    if not SNAPSHOT_PATTERN.fullmatch(snapshot):
        fail("snapshot must be an explicit restic hexadecimal ID")
    ensure_empty_target(target)
    repository.restore(snapshot, target)
    manifest_path, manifest = validate_manifest_tree(target)
    validate_restored_contract(config, manifest)
    if extract_to is not None:
        ensure_empty_target(extract_to)
        for artifact in manifest["artifacts"]:
            if artifact["type"] != "filesystem-tar":
                continue
            destination = extract_to / artifact["id"]
            resolved_destination = destination.resolve()
            resolved_extract_root = extract_to.resolve()
            if (
                not resolved_destination.is_relative_to(resolved_extract_root)
                or resolved_destination == resolved_extract_root
            ):
                fail("filesystem artifact destination escapes the restore root")
            destination.mkdir(mode=0o700)
            archive = manifest_path.parent / artifact["file"]
            run(
                [
                    "/usr/bin/tar",
                    "--extract",
                    "--file",
                    str(archive),
                    "--directory",
                    str(destination),
                    "--acls",
                    "--xattrs",
                    "--selinux",
                    "--numeric-owner",
                    "--same-permissions",
                ],
                timeout=7200,
            )
    return manifest


def wait_postgres(container: str, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        completed = subprocess.run(
            [
                "/usr/bin/docker",
                "exec",
                container,
                "pg_isready",
                "--username=postgres",
                "--dbname=rehearsal",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if completed.returncode == 0:
            return
        time.sleep(2)
    fail(f"temporary PostgreSQL did not become ready: {container}")


def rehearse_database(
    artifact: dict[str, Any],
    dump: Path,
    tmpfs_size: str,
) -> None:
    image = artifact.get("rehearsal_image")
    if not isinstance(image, str) or "@sha256:" not in image:
        fail(f"database rehearsal image is not immutable: {artifact.get('id')}")
    safe_id = re.sub(r"[^a-z0-9-]", "-", str(artifact["id"]).lower())
    container = f"dockerswarm-rehearsal-{safe_id}-{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(32)
    extensions = artifact.get("extensions")
    if not isinstance(extensions, dict):
        fail(f"database rehearsal extensions are invalid: {artifact.get('id')}")
    for extension, version in extensions.items():
        if (
            not isinstance(extension, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", extension)
            or not isinstance(version, str)
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version)
        ):
            fail(f"database rehearsal extension lock is unsafe: {artifact['id']}")
    started = False
    try:
        run(
            [
                "/usr/bin/docker",
                "run",
                "--detach",
                "--name",
                container,
                "--network",
                "none",
                "--tmpfs",
                ("/var/lib/postgresql/data:" f"rw,nosuid,nodev,size={tmpfs_size}"),
                "--env",
                f"POSTGRES_PASSWORD={password}",
                "--env",
                "POSTGRES_USER=postgres",
                "--env",
                "POSTGRES_DB=rehearsal",
                image,
            ],
            timeout=600,
        )
        started = True
        wait_postgres(container)
        listing = run_file_to_stdin(
            ["/usr/bin/docker", "exec", "-i", container, "pg_restore", "--list"],
            dump,
        )
        catalog_entries = [
            line
            for line in listing.stdout.splitlines()
            if line.strip() and not line.lstrip().startswith(";")
        ]
        if (
            hashlib.sha256(listing.stdout.encode("utf-8")).hexdigest()
            != artifact["dump_catalog_sha256"]
            or len(catalog_entries) != artifact["dump_catalog_entries"]
        ):
            fail(f"database dump catalog changed: {artifact['id']}")
        for extension, version in sorted(extensions.items()):
            run(
                [
                    "/usr/bin/docker",
                    "exec",
                    container,
                    "psql",
                    "--username=postgres",
                    "--dbname=rehearsal",
                    "--set=ON_ERROR_STOP=1",
                    "--command",
                    (
                        f"CREATE EXTENSION IF NOT EXISTS {extension} "
                        f"WITH SCHEMA public VERSION '{version}'"
                    ),
                ]
            )
        run_file_to_stdin(
            [
                "/usr/bin/docker",
                "exec",
                "-i",
                container,
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--username=postgres",
                "--dbname=rehearsal",
            ],
            dump,
            timeout=7200,
        )
        result = run(
            [
                "/usr/bin/docker",
                "exec",
                container,
                "psql",
                "--username=postgres",
                "--dbname=rehearsal",
                "--tuples-only",
                "--no-align",
                "--command=SELECT 1",
            ]
        )
        if result.stdout.strip() != "1":
            fail(f"database rehearsal query failed: {artifact['id']}")
        schema_inventory = database_schema_inventory(
            "/usr/bin/docker",
            container,
            "postgres",
            "rehearsal",
        )
        if schema_inventory != artifact["schema_inventory"]:
            fail(f"database rehearsal schema differs: {artifact['id']}")
        for extension, version in sorted(extensions.items()):
            result = run(
                [
                    "/usr/bin/docker",
                    "exec",
                    container,
                    "psql",
                    "--username=postgres",
                    "--dbname=rehearsal",
                    "--tuples-only",
                    "--no-align",
                    "--command",
                    (
                        "SELECT extversion FROM pg_extension "
                        f"WHERE extname = '{extension}'"
                    ),
                ]
            )
            if result.stdout.strip() != version:
                fail(
                    f"database rehearsal extension mismatch: "
                    f"{artifact['id']}/{extension}"
                )
    finally:
        if started:
            completed = subprocess.run(
                ["/usr/bin/docker", "rm", "--force", container],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            if completed.returncode != 0 and sys.exc_info()[0] is None:
                fail(f"could not remove temporary rehearsal container: {container}")


def command_rehearse(config: BackupConfig, repository: Repository) -> str:
    Swarm().assert_manager()
    repository.check_exists()
    snapshots = repository.call(
        [
            "snapshots",
            "--json",
            "--latest",
            "1",
            "--host",
            config.section("runtime")["hostname"],
            "--tag",
            "application",
        ],
        timeout=120,
    )
    try:
        snapshot_documents = json.loads(snapshots.stdout)
    except json.JSONDecodeError as error:
        fail(f"restic returned invalid snapshot JSON: {error}")
    if not isinstance(snapshot_documents, list) or len(snapshot_documents) != 1:
        fail("exactly one latest application snapshot was not found")
    snapshot = snapshot_documents[0].get("id")
    if not isinstance(snapshot, str) or not SNAPSHOT_PATTERN.fullmatch(snapshot):
        fail("latest application snapshot has an invalid ID")
    target = make_stage(config, "rehearsal")
    materialized = target.parent / f"{target.name}-materialized"
    try:
        manifest = restore_snapshot(config, repository, snapshot, target, materialized)
        manifest_path = next(target.rglob("manifest.json"))
        database_artifacts = {
            artifact["id"]: artifact
            for artifact in manifest["artifacts"]
            if artifact["type"] == "postgres-custom-dump"
        }
        expected_databases = {
            database["id"]: database for database in config.document["databases"]
        }
        if set(database_artifacts) != set(expected_databases):
            fail("snapshot does not contain the five reviewed database dumps")
        for identifier, database in expected_databases.items():
            artifact = database_artifacts[identifier]
            if (
                artifact.get("service") != database["service"]
                or artifact.get("database") != database["database"]
                or artifact.get("rehearsal_image") != database["rehearsal_image"]
                or artifact.get("extensions") != database["extensions"]
            ):
                fail(f"database artifact contract differs: {identifier}")
            rehearse_database(
                artifact,
                manifest_path.parent / artifact["file"],
                config.section("swarm_state")["rehearsal_tmpfs_size"],
            )
        return snapshot
    finally:
        if materialized.exists():
            cleanup_stage(config, materialized)
        cleanup_stage(config, target)


def install_signal_handlers() -> None:
    def handle_signal(signum: int, _frame: Any) -> NoReturn:
        fail(f"received signal {signum}; entering guarded recovery")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGHUP, handle_signal)
    signal.signal(signal.SIGQUIT, handle_signal)


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Encrypted off-host backup and safe restore controller"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/dockerswarm/backup/config.json"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-config")
    init_parser = subparsers.add_parser("init")
    init_parser.add_argument(
        "--confirm-create-repository",
        action="store_true",
        help="required acknowledgement for the external repository mutation",
    )
    subparsers.add_parser("application")
    swarm_parser = subparsers.add_parser("swarm-state")
    swarm_parser.add_argument(
        "--confirm-docker-stop",
        action="store_true",
        help="required acknowledgement for the Docker maintenance outage",
    )
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--read-all-data", action="store_true")
    snapshots_parser = subparsers.add_parser("snapshots")
    snapshots_parser.add_argument(
        "--tag",
        choices=("application", "swarm-state"),
        required=True,
    )
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--snapshot", required=True)
    restore_parser.add_argument("--target", type=Path, required=True)
    restore_parser.add_argument("--extract-filesystems-to", type=Path)
    subparsers.add_parser("rehearse")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    arguments = parse_arguments(raw_arguments)
    if arguments.command not in {"validate-config", "snapshots"}:
        lock_helper = host_lock_helper()
        lock_helper.ensure_mutation_lock(
            f"backupctl-{arguments.command}",
            [
                sys.executable,
                str(Path(__file__).resolve()),
                *raw_arguments,
            ],
            caller=Path(__file__),
        )
    main_locals: dict[str, Any] = {}

    def record_failure(detail: str) -> None:
        if "config" not in main_locals:
            return
        if arguments.command in {"validate-config", "snapshots"}:
            return
        try:
            RuntimeStatus(main_locals["config"], arguments.command).write(
                False,
                detail=detail,
            )
        except Exception as status_error:
            # La captura sigue siendo ancha a proposito: este handler no
            # debe enmascarar el error primario, que ya viaja por los
            # except de :2477 y :2482. Pero silenciarlo por completo deja
            # ciega a la monitorizacion: si esta escritura falla, el gauge
            # dockerswarm_backup_last_run_success conserva el 1 del run
            # anterior y la alerta critica BackupLastRunFailed no dispara.
            print(
                "WARNING: cannot record backup failure status: "
                f"{type(status_error).__name__}",
                file=sys.stderr,
            )

    try:
        config = BackupConfig.load(arguments.config)
        main_locals["config"] = config
        if arguments.command == "validate-config":
            print("Backup configuration is valid.")
            return 0

        install_signal_handlers()
        config.ensure_runtime_security()
        repository = Repository(config)
        repository.verify_binary()
        lock_path = Path(config.section("runtime")["lock_file"])
        status = RuntimeStatus(config, arguments.command)
        with exclusive_lock(lock_path):
            if arguments.command == "init":
                if not arguments.confirm_create_repository:
                    fail("init requires --confirm-create-repository")
                repository_created = repository.initialize()
                status.write(
                    True,
                    detail=(
                        "repository initialized and opened"
                        if repository_created
                        else "existing repository opened"
                    ),
                )
                if repository_created:
                    print("REPOSITORY_CREATED")
                else:
                    print("REPOSITORY_ALREADY_EXISTS")
                return 0
            if arguments.command == "application":
                snapshot = command_application(config, repository)
                status.write(True, snapshot=snapshot)
                print(f"Application backup completed: snapshot {snapshot}")
                return 0
            if arguments.command == "swarm-state":
                if not arguments.confirm_docker_stop:
                    fail("swarm-state requires --confirm-docker-stop")
                snapshot = command_swarm_state(config, repository)
                status.write(True, snapshot=snapshot)
                print(f"Swarm state backup completed: snapshot {snapshot}")
                return 0
            if arguments.command == "verify":
                repository.check_exists()
                repository.check(arguments.read_all_data)
                status.write(
                    True,
                    detail=(
                        "full repository data verified"
                        if arguments.read_all_data
                        else "repository metadata verified"
                    ),
                )
                print("Encrypted repository verification completed.")
                return 0
            if arguments.command == "snapshots":
                repository.check_exists()
                result = repository.call(
                    [
                        "snapshots",
                        "--host",
                        config.section("runtime")["hostname"],
                        "--tag",
                        arguments.tag,
                    ],
                    timeout=120,
                )
                print(result.stdout, end="")
                return 0
            if arguments.command == "restore":
                target = absolute_safe_path(arguments.target, "restore target")
                extract_to = (
                    absolute_safe_path(
                        arguments.extract_filesystems_to,
                        "filesystem extraction target",
                    )
                    if arguments.extract_filesystems_to
                    else None
                )
                repository.check_exists()
                manifest = restore_snapshot(
                    config,
                    repository,
                    arguments.snapshot,
                    target,
                    extract_to,
                )
                status.write(
                    True,
                    snapshot=arguments.snapshot,
                    detail=f"staged {manifest['kind']} restore without activation",
                )
                print(
                    "Snapshot restored, checksummed, and staged without "
                    "overwriting live data."
                )
                return 0
            if arguments.command == "rehearse":
                snapshot = command_rehearse(config, repository)
                status.write(True, snapshot=snapshot)
                print(f"Application restore rehearsal completed: {snapshot}")
                return 0
            fail(f"unsupported command: {arguments.command}")
    except BackupError as error:
        record_failure(str(error))
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        detail = f"unexpected internal failure ({type(error).__name__})"
        record_failure(detail)
        print(f"ERROR: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
