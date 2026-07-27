#!/usr/bin/env python3
"""Fail-closed attestations for Terraform state, backends, and saved plans."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import stat
import sys
from typing import Any, BinaryIO, Sequence

TERRAFORM_VERSION = "1.15.8"
CONTRACT_VERSION = 1
REPOSITORY_CONTRACT = "apptolast-dockerswarm-iac"
CLOUDFLARE_ACCOUNT_ID = "becc1c00820a0f6779e9b278ab150abf"
CLOUDFLARE_ZONE_ID = "b69e21ff397bd00c35b989fe10068d0e"
NETCUP_SERVER_ID = 900782
NETCUP_SERVER_MAC = "16:2e:0f:f4:c4:da"
LOCK_PROOF_MAX_SECONDS = 24 * 60 * 60
PLAN_MAX_SECONDS = 60 * 60
MAX_STATE_BYTES = 64 * 1024 * 1024
MAX_TERRAFORM_UI_BYTES = 64 * 1024 * 1024
LOCK_PROOF_SIGNER = "apptolast-terraform-lock-proof"
LOCK_PROOF_NAMESPACE = "terraform-r2-lock-proof"
LOCK_PROOF_ALLOWED_SIGNERS = Path("infra/terraform/lock-proof.allowed-signers")
PLAN_SIGNER = "apptolast-terraform-plan"
PLAN_NAMESPACE = "terraform-saved-plan"
PLAN_ALLOWED_SIGNERS = Path("infra/terraform/plan.allowed-signers")
BACKEND_IDENTITIES_PATH = Path("infra/terraform/backend-identities.json")
SNAPSHOT_RECIPIENTS_PATH = Path("infra/terraform/snapshot-recipients.json")
LOCKING_CONTRACT_PATHS = (
    Path("scripts/r2-operation-lease.py"),
    Path("scripts/r2-cross-credential-probe.py"),
    Path("scripts/terraform-safety.py"),
    Path("scripts/test-terraform-r2-locking.sh"),
    Path("infra/terraform/testing/r2-lock/main.tf"),
    Path("infra/terraform/testing/r2-lock/versions.tf"),
)

ROOT_IDENTITIES: dict[str, dict[str, Any]] = {
    "cloudflare/state-bootstrap": {
        "contract_version": CONTRACT_VERSION,
        "repository_contract": REPOSITORY_CONTRACT,
        "root": "cloudflare/state-bootstrap",
        "backend_type": "local",
        "backend_key": "",
        "workspace": "default",
        "cloudflare_account_id": CLOUDFLARE_ACCOUNT_ID,
    },
    "cloudflare/apptolast-dns": {
        "contract_version": CONTRACT_VERSION,
        "repository_contract": REPOSITORY_CONTRACT,
        "root": "cloudflare/apptolast-dns",
        "backend_type": "s3",
        "backend_key": "cloudflare/apptolast-dns/terraform.tfstate",
        "workspace": "default",
        "cloudflare_account_id": CLOUDFLARE_ACCOUNT_ID,
        "cloudflare_zone_id": CLOUDFLARE_ZONE_ID,
    },
    "netcup/perimeter": {
        "contract_version": CONTRACT_VERSION,
        "repository_contract": REPOSITORY_CONTRACT,
        "root": "netcup/perimeter",
        "backend_type": "s3",
        "backend_key": "netcup/perimeter/terraform.tfstate",
        "workspace": "default",
        "netcup_server_id": NETCUP_SERVER_ID,
        "netcup_server_mac": NETCUP_SERVER_MAC,
    },
}

DNS_KEYS = (
    "edge",
    "kropia",
    "minecraft-stats",
    "minecraft",
    "n8n",
    "openclaw",
    "passbolt",
    "generadorcodigosqr",
    "pablohurtadohg",
    "albertohidalgo",
)
DNS_LEGACY_IPV4 = "138.199.157.58"
DNS_PLATFORM_IPV4 = "159.195.156.57"
MANAGED_CONTRACT_FIELDS = {
    "cloudflare_dns_record": (
        "zone_id",
        "name",
        "content",
        "type",
        "proxied",
        "ttl",
    ),
    "cloudflare_r2_bucket": (
        "account_id",
        "name",
        "location",
        "storage_class",
    ),
    "cloudflare_r2_managed_domain": (
        "account_id",
        "bucket_name",
        "enabled",
    ),
    "netcup_firewall_policy": (
        "id",
        "name",
        "description",
        "rule",
    ),
    "netcup_server_firewall": (
        "server_id",
        "mac",
        "policy_ids",
        "active",
    ),
    "netcup_ssh_key": (
        "name",
        "key",
    ),
}
UNKNOWN_VALUE = {"__terraform_unknown__": True}
EXPECTED_CHECKS: dict[str, dict[str, str]] = {
    "cloudflare/state-bootstrap": {
        "terraform_data.root_identity": "resource",
        "var.cloudflare_account_id": "var",
        "var.dockerswarm_backup_bucket": "var",
        "var.state_buckets": "var",
    },
    "cloudflare/apptolast-dns": {
        "check.platform_contract": "check",
    },
    "netcup/perimeter": {
        "check.platform_contract": "check",
        "netcup_firewall_policy.host_ingress": "resource",
        "netcup_server_firewall.host": "resource",
        "var.admin_cidrs": "var",
        "var.environment": "var",
        "var.preserved_policy_ids": "var",
        "var.scp_ssh_public_keys": "var",
        "var.ssh_port": "var",
    },
}


class TerraformSafetyError(RuntimeError):
    """A Terraform artifact violates the reviewed production contract."""


def canonical_json(document: Any) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json_bytes(data: bytes, context: str) -> dict[str, Any]:
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerraformSafetyError(f"{context} is not valid JSON") from exc
    if not isinstance(document, dict):
        raise TerraformSafetyError(f"{context} must be a JSON object")
    return document


def load_json_file(path: Path, context: str) -> tuple[dict[str, Any], bytes]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TerraformSafetyError(f"cannot read {context}: {path}") from exc
    return load_json_bytes(data, context), data


def load_json_without_duplicate_keys(data: bytes, context: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise TerraformSafetyError(
                    f"{context} contains a duplicate JSON key: {key}"
                )
            document[key] = value
        return document

    try:
        document = json.loads(data, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerraformSafetyError(f"{context} is not valid JSON") from exc
    if not isinstance(document, dict):
        raise TerraformSafetyError(f"{context} must be a JSON object")
    return document


def require_bounded_regular_file(
    path: Path,
    context: str,
    maximum_bytes: int,
) -> bytes:
    data = require_regular_file(path, context)
    if len(data) > maximum_bytes:
        raise TerraformSafetyError(f"{context} exceeds its safety limit")
    return data


def validate_empty_stderr(path: Path, context: str) -> str:
    data = require_bounded_regular_file(
        path,
        f"{context} standard error",
        MAX_TERRAFORM_UI_BYTES,
    )
    if data:
        raise TerraformSafetyError(
            f"{context} wrote unexpected standard error; output withheld"
        )
    return sha256_bytes(data)


def fsync_artifact(path: Path, directory: Path) -> dict[str, Any]:
    if not path.is_absolute() or not directory.is_absolute():
        raise TerraformSafetyError("fsync artifact path and directory must be absolute")
    if path.parent != directory:
        raise TerraformSafetyError(
            "fsync artifact must be an immediate child of its directory"
        )
    if directory.is_symlink() or not directory.is_dir():
        raise TerraformSafetyError("fsync artifact directory must be a real directory")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        directory_fd = os.open(directory, directory_flags)
    except OSError as exc:
        raise TerraformSafetyError(
            "cannot open fsync artifact directory safely"
        ) from exc
    file_fd = -1
    try:
        directory_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise TerraformSafetyError(
                "fsync artifact directory descriptor is not a directory"
            )
        try:
            file_fd = os.open(path.name, file_flags, dir_fd=directory_fd)
        except OSError as exc:
            raise TerraformSafetyError("cannot open fsync artifact safely") from exc
        file_stat = os.fstat(file_fd)
        path_stat = os.stat(
            path.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
            or (file_stat.st_dev, file_stat.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise TerraformSafetyError("fsync artifact is not a unique regular file")
        digest = hashlib.sha256()
        os.lseek(file_fd, 0, os.SEEK_SET)
        while chunk := os.read(file_fd, 1024 * 1024):
            digest.update(chunk)
        os.fsync(file_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        raise TerraformSafetyError("cannot durably fsync Terraform artifact") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(directory_fd)
    return {
        "schema": CONTRACT_VERSION,
        "path": str(path),
        "device": file_stat.st_dev,
        "inode": file_stat.st_ino,
        "size": file_stat.st_size,
        "sha256": digest.hexdigest(),
        "durable": True,
    }


def attest_terraform_ui_stream(
    operation: str,
    events_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    if operation not in {"init", "plan", "apply"}:
        raise TerraformSafetyError("unsupported Terraform UI operation")
    events = require_bounded_regular_file(
        events_path,
        f"Terraform {operation} UI stream",
        MAX_TERRAFORM_UI_BYTES,
    )
    stderr_sha256 = validate_empty_stderr(stderr_path, f"Terraform {operation}")
    if not events or not events.endswith(b"\n"):
        raise TerraformSafetyError(
            f"Terraform {operation} UI stream is empty or truncated"
        )
    lines = events.splitlines()
    documents: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if not line:
            raise TerraformSafetyError(
                f"Terraform {operation} UI stream contains a blank line"
            )
        document = load_json_without_duplicate_keys(
            line,
            f"Terraform {operation} UI event {index}",
        )
        if (
            document.get("@module") != "terraform.ui"
            or not isinstance(document.get("type"), str)
            or not document.get("type")
        ):
            raise TerraformSafetyError(
                f"Terraform {operation} UI event {index} is not canonical"
            )
        level = document.get("@level")
        if level != "info":
            raise TerraformSafetyError(
                f"Terraform {operation} emitted a non-info UI event; "
                "diagnostic content withheld"
            )
        diagnostic = document.get("diagnostic")
        if document.get("type") == "diagnostic" or diagnostic is not None:
            raise TerraformSafetyError(
                f"Terraform {operation} emitted a diagnostic UI event; "
                "diagnostic content withheld"
            )
        documents.append(document)
    first = documents[0]
    ui_version = first.get("ui")
    if (
        first.get("type") != "version"
        or first.get("terraform") != TERRAFORM_VERSION
        or not isinstance(ui_version, str)
        or ui_version.split(".", maxsplit=1)[0] != "1"
    ):
        raise TerraformSafetyError(
            f"Terraform {operation} UI stream has an unsupported version"
        )
    version_documents = [
        document for document in documents if document.get("type") == "version"
    ]
    if (
        not version_documents
        or any(
            document.get("terraform") != TERRAFORM_VERSION
            or document.get("ui") != ui_version
            for document in version_documents
        )
        or (operation != "init" and len(version_documents) != 1)
    ):
        raise TerraformSafetyError(
            f"Terraform {operation} UI stream has an invalid version inventory"
        )

    change_summary: dict[str, int] | None = None
    if operation == "init":
        successes = [
            document
            for document in documents
            if document.get("type") == "init_output"
            and document.get("message_code") == "output_init_success_message"
        ]
        if len(successes) != 1:
            raise TerraformSafetyError(
                "Terraform init UI stream has no unique success event"
            )
    else:
        summaries = [
            document.get("changes")
            for document in documents
            if document.get("type") == "change_summary"
            and isinstance(document.get("changes"), dict)
            and document["changes"].get("operation") == operation
        ]
        if len(summaries) != 1:
            raise TerraformSafetyError(
                f"Terraform {operation} UI stream has no unique change summary"
            )
        raw_summary = summaries[0]
        expected_summary_fields = {
            "action_invocation",
            "add",
            "change",
            "import",
            "operation",
            "remove",
        }
        if set(raw_summary) != expected_summary_fields:
            raise TerraformSafetyError(
                f"Terraform {operation} change summary is not canonical"
            )
        for field in expected_summary_fields - {"operation"}:
            value = raw_summary.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise TerraformSafetyError(
                    f"Terraform {operation} change summary is invalid"
                )
        if raw_summary.get("remove") != 0 or raw_summary.get("action_invocation") != 0:
            raise TerraformSafetyError(
                f"Terraform {operation} reported a forbidden destructive "
                "or provider action"
            )
        change_summary = {
            field: raw_summary[field]
            for field in sorted(expected_summary_fields - {"operation"})
        }
    return {
        "schema": CONTRACT_VERSION,
        "operation": operation,
        "terraform_version": TERRAFORM_VERSION,
        "ui_version": ui_version,
        "event_count": len(documents),
        "diagnostic_count": 0,
        "events_sha256": sha256_bytes(events),
        "stderr_sha256": stderr_sha256,
        "change_summary": change_summary,
    }


def validate_terraform_validate_document(
    document_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    document_bytes = require_bounded_regular_file(
        document_path,
        "Terraform validate JSON",
        MAX_TERRAFORM_UI_BYTES,
    )
    validate_empty_stderr(stderr_path, "Terraform validate")
    document = load_json_without_duplicate_keys(
        document_bytes,
        "Terraform validate JSON",
    )
    if (
        set(document)
        != {
            "diagnostics",
            "error_count",
            "format_version",
            "valid",
            "warning_count",
        }
        or document.get("format_version") != "1.0"
        or document.get("valid") is not True
        or document.get("error_count") != 0
        or document.get("warning_count") != 0
        or document.get("diagnostics") != []
    ):
        raise TerraformSafetyError(
            "Terraform validate reported errors, warnings, or unknown output"
        )
    return {
        "schema": CONTRACT_VERSION,
        "terraform_version": TERRAFORM_VERSION,
        "diagnostic_count": 0,
        "document_sha256": sha256_bytes(document_bytes),
    }


def validate_empty_state_probe(stderr_path: Path) -> dict[str, Any]:
    stderr = require_bounded_regular_file(
        stderr_path,
        "Terraform empty-state probe standard error",
        MAX_TERRAFORM_UI_BYTES,
    )
    expected = (
        b"No state file was found!\n"
        b"\n"
        b"State management commands require a state file. Run this command\n"
        b"in a directory where Terraform has been run or use the -state flag\n"
        b"to point the command to a specific state location.\n"
    )
    if stderr != expected:
        raise TerraformSafetyError(
            "Terraform empty-state probe emitted non-canonical output"
        )
    return {
        "schema": CONTRACT_VERSION,
        "terraform_version": TERRAFORM_VERSION,
        "stderr_sha256": sha256_bytes(stderr),
    }


def require_root(root: str) -> dict[str, Any]:
    try:
        return ROOT_IDENTITIES[root]
    except KeyError as exc:
        raise TerraformSafetyError(f"unsupported Terraform root: {root}") from exc


def parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise TerraformSafetyError(f"{field} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise TerraformSafetyError(f"{field} must be an RFC3339 UTC timestamp") from exc
    return parsed


def require_regular_file(path: Path, context: str) -> bytes:
    # Opened with O_NOFOLLOW and checked via the held descriptor (fstat, not
    # a second path-based stat) so a symlink swap between a check and a
    # later read cannot substitute a different file, matching the
    # atomic-open idiom used elsewhere in this codebase (e.g.
    # ansible-operation-lock.py's read_marker).
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise TerraformSafetyError(f"cannot read {context}: {path}") from exc
    try:
        file_state = os.fstat(descriptor)
        if not stat.S_ISREG(file_state.st_mode):
            raise TerraformSafetyError(
                f"{context} must be a regular, non-symlink file"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def locking_contract(project_dir: Path) -> dict[str, Any]:
    repository = project_dir.resolve(strict=True)
    files: list[dict[str, str]] = []
    for relative_path in LOCKING_CONTRACT_PATHS:
        absolute_path = repository / relative_path
        content = require_regular_file(
            absolute_path,
            f"locking contract file {relative_path}",
        )
        files.append(
            {
                "path": relative_path.as_posix(),
                "sha256": sha256_bytes(content),
            }
        )
    return {
        "schema": CONTRACT_VERSION,
        "files": files,
        "sha256": sha256_bytes(canonical_json(files)),
    }


def normalize_registered_backend(
    value: Any,
    *,
    expected_type: str,
    project_dir: Path,
    context: str,
) -> dict[str, str]:
    if not isinstance(value, dict) or value.get("backend_type") != expected_type:
        raise TerraformSafetyError(f"{context} has an invalid backend type")
    if expected_type == "local":
        if set(value) != {"backend_type", "path"}:
            raise TerraformSafetyError(
                f"{context} must contain only backend_type and path"
            )
        raw_path = value.get("path")
        if (
            not isinstance(raw_path, str)
            or "replace" in raw_path.lower()
            or not Path(raw_path).is_absolute()
        ):
            raise TerraformSafetyError(f"{context} path must be absolute")
        state_path = Path(raw_path).resolve(strict=False)
        repository = project_dir.resolve(strict=True)
        if state_path == repository or repository in state_path.parents:
            raise TerraformSafetyError(
                f"{context} path must stay outside the Git repository"
            )
        return {
            "backend_type": "local",
            "path": str(state_path),
        }
    if expected_type != "s3":
        raise TerraformSafetyError(f"{context} backend type is unsupported")
    if set(value) != {
        "backend_type",
        "bucket",
        "key",
        "access_key_id_sha256",
    }:
        raise TerraformSafetyError(
            f"{context} must contain backend_type, bucket, key, "
            "and access_key_id_sha256"
        )
    bucket = value.get("bucket")
    key = value.get("key")
    access_key_id_sha256 = value.get("access_key_id_sha256")
    if (
        not isinstance(bucket, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket) is None
        or "replace" in bucket.lower()
        or not isinstance(key, str)
        or not key
        or key.startswith("/")
        or any(part in {"", ".", ".."} for part in key.split("/"))
        or not isinstance(access_key_id_sha256, str)
        or re.fullmatch(r"[a-f0-9]{64}", access_key_id_sha256) is None
    ):
        raise TerraformSafetyError(f"{context} S3 identity is invalid")
    return {
        "backend_type": "s3",
        "bucket": bucket,
        "key": key,
        "access_key_id_sha256": access_key_id_sha256,
    }


def load_backend_identities(
    project_dir: Path,
) -> tuple[dict[str, dict[str, Any]], str]:
    repository = project_dir.resolve(strict=True)
    registry_path = repository / BACKEND_IDENTITIES_PATH
    registry_bytes = require_regular_file(
        registry_path,
        "approved backend identity registry",
    )
    registry = load_json_bytes(
        registry_bytes,
        "approved backend identity registry",
    )
    if set(registry) != {"schema", "roots"} or registry.get("schema") != 1:
        raise TerraformSafetyError(
            "approved backend identity registry schema is invalid"
        )
    roots = registry.get("roots")
    if not isinstance(roots, dict) or set(roots) != set(ROOT_IDENTITIES):
        raise TerraformSafetyError(
            "approved backend identity registry has an incomplete root set"
        )
    normalized: dict[str, dict[str, Any]] = {}
    for root, expected in ROOT_IDENTITIES.items():
        root_entry = roots.get(root)
        if not isinstance(root_entry, dict) or set(root_entry) != {
            "production",
            "pending_destination",
        }:
            raise TerraformSafetyError(
                f"approved backend identities are invalid for {root}"
            )
        production_raw = root_entry.get("production")
        production = None
        if production_raw is not None:
            production = normalize_registered_backend(
                production_raw,
                expected_type=expected["backend_type"],
                project_dir=repository,
                context=f"production backend for {root}",
            )
            if (
                production["backend_type"] == "s3"
                and production["key"] != expected["backend_key"]
            ):
                raise TerraformSafetyError(
                    f"registered production key belongs to another root: {root}"
                )
        pending_raw = root_entry["pending_destination"]
        pending = None
        if pending_raw is not None:
            if production is None:
                raise TerraformSafetyError(
                    f"pending destination has no production source for {root}"
                )
            pending = normalize_registered_backend(
                pending_raw,
                expected_type=expected["backend_type"],
                project_dir=repository,
                context=f"pending migration destination for {root}",
            )
            if (
                pending["backend_type"] == "s3"
                and pending["key"] != expected["backend_key"]
            ):
                raise TerraformSafetyError(
                    f"pending destination key belongs to another root: {root}"
                )
        if (
            production is not None
            and pending is not None
            and (
                (
                    pending["backend_type"] == "local"
                    and pending["path"] == production["path"]
                )
                or (
                    pending["backend_type"] == "s3"
                    and pending["bucket"] == production["bucket"]
                    and pending["key"] == production["key"]
                )
            )
        ):
            raise TerraformSafetyError(
                f"pending destination shares production storage for {root}"
            )
        normalized[root] = {
            "production": production,
            "pending_destination": pending,
        }
    return normalized, sha256_bytes(registry_bytes)


def registered_backend_projection(identity: dict[str, Any]) -> dict[str, str]:
    backend_type = identity.get("backend_type")
    if backend_type == "local":
        return {
            "backend_type": "local",
            "path": identity["path"],
        }
    if backend_type == "s3":
        return {
            "backend_type": "s3",
            "bucket": identity["bucket"],
            "key": identity["key"],
            "access_key_id_sha256": identity["access_key_id_sha256"],
        }
    raise TerraformSafetyError("backend identity type is unsupported")


def require_registered_backend(
    *,
    root: str,
    identity: dict[str, Any],
    project_dir: Path,
    role: str,
) -> str:
    registry, registry_sha256 = load_backend_identities(project_dir)
    root_registry = registry[root]
    actual = registered_backend_projection(identity)
    if role not in {"production", "pending_destination"}:
        raise TerraformSafetyError("backend registry role is invalid")
    authorized = root_registry[role]
    if authorized is None:
        raise TerraformSafetyError(f"backend registry has no {role.replace('_', ' ')}")
    if actual != authorized:
        raise TerraformSafetyError(
            f"backend is not the versioned {role.replace('_', ' ')} "
            "identity for this root"
        )
    return registry_sha256


def attest_snapshot_recipient(
    recipient_file: Path,
    project_dir: Path,
) -> dict[str, Any]:
    recipient_bytes = require_regular_file(
        recipient_file,
        "age recipient file",
    )
    if not recipient_bytes.strip():
        raise TerraformSafetyError("age recipient file is empty")
    repository = project_dir.resolve(strict=True)
    registry_bytes = require_regular_file(
        repository / SNAPSHOT_RECIPIENTS_PATH,
        "approved snapshot recipient registry",
    )
    registry = load_json_bytes(
        registry_bytes,
        "approved snapshot recipient registry",
    )
    expected_hash = registry.get("recipient_file_sha256")
    if (
        set(registry) != {"schema", "recipient_file_sha256"}
        or registry.get("schema") != 1
        or not isinstance(expected_hash, str)
        or re.fullmatch(r"[a-f0-9]{64}", expected_hash) is None
        or expected_hash == "0" * 64
    ):
        raise TerraformSafetyError("snapshot recipient registry is invalid")
    actual_hash = sha256_bytes(recipient_bytes)
    if actual_hash != expected_hash:
        raise TerraformSafetyError("age recipients are not the versioned approved set")
    return {
        "schema": CONTRACT_VERSION,
        "recipient_file_sha256": actual_hash,
        "recipient_registry_sha256": sha256_bytes(registry_bytes),
    }


def verify_lock_proof_signature(
    proof_bytes: bytes,
    signature_path: Path,
    project_dir: Path,
) -> dict[str, str]:
    signature_bytes = require_regular_file(
        signature_path,
        "locking proof signature",
    )
    allowed_signers_path = project_dir.resolve(strict=True) / LOCK_PROOF_ALLOWED_SIGNERS
    allowed_signers_bytes = require_regular_file(
        allowed_signers_path,
        "approved locking-proof signer registry",
    )
    try:
        result = subprocess.run(
            (
                "ssh-keygen",
                "-Y",
                "verify",
                "-f",
                str(allowed_signers_path),
                "-I",
                LOCK_PROOF_SIGNER,
                "-n",
                LOCK_PROOF_NAMESPACE,
                "-s",
                str(signature_path),
            ),
            input=proof_bytes,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise TerraformSafetyError(
            "cannot execute ssh-keygen to verify the locking proof"
        ) from exc
    if result.returncode != 0:
        raise TerraformSafetyError(
            "locking proof has no valid signature from an approved signer"
        )
    return {
        "signature_sha256": sha256_bytes(signature_bytes),
        "allowed_signers_sha256": sha256_bytes(allowed_signers_bytes),
        "signer_identity": LOCK_PROOF_SIGNER,
    }


def verify_plan_signature(
    sidecar_bytes: bytes,
    signature_path: Path,
    project_dir: Path,
) -> dict[str, str]:
    signature_bytes = require_regular_file(
        signature_path,
        "saved plan signature",
    )
    allowed_signers_path = project_dir.resolve(strict=True) / PLAN_ALLOWED_SIGNERS
    allowed_signers_bytes = require_regular_file(
        allowed_signers_path,
        "approved plan signer registry",
    )
    try:
        result = subprocess.run(
            (
                "ssh-keygen",
                "-Y",
                "verify",
                "-f",
                str(allowed_signers_path),
                "-I",
                PLAN_SIGNER,
                "-n",
                PLAN_NAMESPACE,
                "-s",
                str(signature_path),
            ),
            input=sidecar_bytes,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise TerraformSafetyError("cannot verify the saved plan signature") from exc
    if result.returncode != 0:
        raise TerraformSafetyError(
            "saved plan has no valid signature from an approved signer"
        )
    return {
        "signature_sha256": sha256_bytes(signature_bytes),
        "allowed_signers_sha256": sha256_bytes(allowed_signers_bytes),
        "signer_identity": PLAN_SIGNER,
    }


def s3_endpoint(config: dict[str, Any]) -> str:
    endpoints = config.get("endpoints")
    if isinstance(endpoints, list):
        if len(endpoints) != 1 or not isinstance(endpoints[0], dict):
            raise TerraformSafetyError("S3 backend endpoints are ambiguous")
        endpoints = endpoints[0]
    if not isinstance(endpoints, dict):
        raise TerraformSafetyError("S3 backend has no explicit endpoints map")
    if any(
        key != "s3" and value not in (None, "", [], {})
        for key, value in endpoints.items()
    ):
        raise TerraformSafetyError("S3 backend contains an alternate service endpoint")
    endpoint = endpoints.get("s3")
    if not isinstance(endpoint, str):
        raise TerraformSafetyError("S3 backend has no explicit S3 endpoint")
    return endpoint.rstrip("/")


def reject_alternative_backend_credentials(config: dict[str, Any]) -> None:
    alternative_fields = (
        "profile",
        "shared_credentials_file",
        "shared_credentials_files",
        "shared_config_files",
        "assume_role",
        "assume_role_with_web_identity",
        "web_identity_token",
        "web_identity_token_file",
        "sts_endpoint",
        "iam_endpoint",
    )
    if any(config.get(field) not in (None, "", [], {}) for field in alternative_fields):
        raise TerraformSafetyError(
            "alternate backend credential selectors are forbidden"
        )


def reject_unsafe_s3_transport(config: dict[str, Any]) -> None:
    if config.get("insecure") not in (None, False):
        raise TerraformSafetyError("S3 backend TLS verification must be enabled")
    unsafe_transport_fields = (
        "custom_ca_bundle",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    )
    if any(
        config.get(field) not in (None, "", [], {}) for field in unsafe_transport_fields
    ):
        raise TerraformSafetyError(
            "custom S3 backend CA or proxy configuration is forbidden"
        )


def primary_credential_identity() -> str:
    alternative_environment = (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_SESSION_NAME",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    )
    if any(os.environ.get(name) for name in alternative_environment):
        raise TerraformSafetyError(
            "alternate AWS credential environment selectors are forbidden"
        )
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if (
        not isinstance(access_key, str)
        or not access_key.strip()
        or any(character.isspace() for character in access_key)
        or not isinstance(secret_key, str)
        or not secret_key
    ):
        raise TerraformSafetyError(
            "remote backend requires explicit access and secret keys"
        )
    return sha256_bytes(access_key.encode("utf-8"))


def validate_lock_proof(
    proof_path: Path,
    root: str,
    backend_registry_role: str,
    locking_scope_sha256: str,
    project_dir: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    proof_bytes = require_regular_file(proof_path, "locking proof")
    signature = verify_lock_proof_signature(
        proof_bytes,
        Path(f"{proof_path}.sig"),
        project_dir,
    )
    proof = load_json_bytes(proof_bytes, "locking proof")
    if proof.get("schema") != CONTRACT_VERSION:
        raise TerraformSafetyError("locking proof schema is not supported")
    if proof.get("terraform_version") != TERRAFORM_VERSION:
        raise TerraformSafetyError("locking proof uses another Terraform version")
    if proof.get("locking_scope_sha256") != locking_scope_sha256:
        raise TerraformSafetyError("locking proof belongs to another backend")
    if proof.get("root") != root:
        raise TerraformSafetyError("locking proof belongs to another root")
    if proof.get("backend_registry_role") != backend_registry_role:
        raise TerraformSafetyError(
            "locking proof belongs to another backend registry role"
        )
    cross_root = proof.get("cross_root")
    remote_roots = {
        name
        for name, identity in ROOT_IDENTITIES.items()
        if identity["backend_type"] == "s3"
    }
    if not isinstance(cross_root, str) or {root, cross_root} != remote_roots:
        raise TerraformSafetyError(
            "locking proof does not cross-test the other remote root"
        )
    primary_identity = primary_credential_identity()
    if proof.get("primary_access_key_id_sha256") != primary_identity:
        raise TerraformSafetyError(
            "locking proof belongs to another primary R2 credential"
        )
    cross_identity = proof.get("cross_access_key_id_sha256")
    if (
        not isinstance(cross_identity, str)
        or re.fullmatch(r"[a-f0-9]{64}", cross_identity) is None
        or cross_identity == primary_identity
    ):
        raise TerraformSafetyError(
            "locking proof has no distinct cross-credential identity"
        )
    registry, registry_sha256 = load_backend_identities(project_dir)
    cross_production = registry[cross_root]["production"]
    if (
        not isinstance(cross_production, dict)
        or cross_production["access_key_id_sha256"] != cross_identity
    ):
        raise TerraformSafetyError(
            "locking proof did not test the registered other-root credential"
        )
    positive_scope = proof.get("cross_credential_positive_scope_sha256")
    if (
        not isinstance(positive_scope, str)
        or re.fullmatch(r"[a-f0-9]{64}", positive_scope) is None
        or positive_scope == locking_scope_sha256
    ):
        raise TerraformSafetyError(
            "locking proof has no distinct cross-credential positive scope"
        )
    expected_positive_scope = {
        "backend_type": "s3",
        "bucket": cross_production["bucket"],
        "endpoint": (f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"),
        "root": cross_root,
        "backend_registry_role": "production",
        "backend_registry_sha256": registry_sha256,
        "primary_access_key_id_sha256": cross_identity,
        "terraform_version": TERRAFORM_VERSION,
    }
    if positive_scope != sha256_bytes(canonical_json(expected_positive_scope)):
        raise TerraformSafetyError(
            "locking proof positive scope differs from the other root registry"
        )
    contract = locking_contract(project_dir)
    if proof.get("locking_contract_sha256") != contract["sha256"]:
        raise TerraformSafetyError(
            "locking proof was produced by another harness contract"
        )
    source_commit = proof.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or re.fullmatch(r"[a-f0-9]{40}", source_commit) is None
    ):
        raise TerraformSafetyError("locking proof source commit is invalid")
    if proof.get("signer_identity") != LOCK_PROOF_SIGNER:
        raise TerraformSafetyError("locking proof signer identity is invalid")
    required_results = (
        "exclusive_create",
        "second_client_blocked",
        "normal_release",
        "interrupted_client_recovered",
        "distributed_operation_lease",
        "terraform_backend_access_denied",
        "cross_credential_own_bucket_list_succeeded",
        "cross_credential_own_bucket_read_succeeded",
        "cross_credential_own_bucket_write_succeeded",
        "cross_credential_own_bucket_delete_succeeded",
        "cross_credential_list_denied",
        "cross_credential_read_denied",
        "cross_credential_write_denied",
        "cross_credential_delete_denied",
    )
    results = proof.get("results")
    if not isinstance(results, dict) or any(
        results.get(key) is not True for key in required_results
    ):
        raise TerraformSafetyError("locking proof does not pass every required test")
    tested_at = parse_utc(proof.get("tested_at"), "tested_at")
    valid_until = parse_utc(proof.get("valid_until"), "valid_until")
    if valid_until <= tested_at:
        raise TerraformSafetyError("locking proof validity interval is empty")
    if (valid_until - tested_at).total_seconds() > LOCK_PROOF_MAX_SECONDS:
        raise TerraformSafetyError("locking proof validity exceeds 90 days")
    current = now or datetime.now(timezone.utc)
    if tested_at > current or current >= valid_until:
        raise TerraformSafetyError("locking proof is not currently valid")
    operator = proof.get("operator")
    if not isinstance(operator, str) or not operator.strip():
        raise TerraformSafetyError("locking proof has no accountable operator")
    return {
        "sha256": sha256_bytes(proof_bytes),
        "tested_at": proof["tested_at"],
        "valid_until": proof["valid_until"],
        "operator": operator,
        "primary_access_key_id_sha256": primary_identity,
        "cross_access_key_id_sha256": cross_identity,
        "cross_root": cross_root,
        "cross_credential_positive_scope_sha256": positive_scope,
        "source_commit": source_commit,
        "locking_contract_sha256": contract["sha256"],
        **signature,
    }


def describe_locking_scope(
    metadata: dict[str, Any],
    root: str,
    project_dir: Path,
    backend_role: str,
) -> dict[str, Any]:
    expected = require_root(root)
    if expected["backend_type"] != "s3":
        raise TerraformSafetyError("locking scope is supported only for remote roots")
    if backend_role not in {"production", "pending_destination"}:
        raise TerraformSafetyError("locking scope backend registry role is invalid")
    backend = metadata.get("backend")
    if not isinstance(backend, dict) or backend.get("type") != "s3":
        raise TerraformSafetyError("locking scope requires an initialized S3 backend")
    config = backend.get("config")
    if not isinstance(config, dict):
        raise TerraformSafetyError("S3 backend configuration is missing")
    bucket = config.get("bucket")
    if (
        not isinstance(bucket, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket) is None
        or "replace" in bucket.lower()
    ):
        raise TerraformSafetyError("S3 backend bucket is absent or a placeholder")
    if config.get("use_lockfile") is not True:
        raise TerraformSafetyError("S3 lockfile support is not enabled")
    key = config.get("key")
    if (
        not isinstance(key, str)
        or re.fullmatch(r"lock-tests/[a-zA-Z0-9._/-]+\.tfstate", key) is None
        or any(part in {"", ".", ".."} for part in key.split("/"))
    ):
        raise TerraformSafetyError(
            "locking scope requires a disposable key below lock-tests/"
        )
    reject_alternative_backend_credentials(config)
    reject_unsafe_s3_transport(config)
    endpoint = s3_endpoint(config)
    expected_endpoint = f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
    if endpoint != expected_endpoint:
        raise TerraformSafetyError("R2 endpoint belongs to another account")
    primary_identity = primary_credential_identity()
    registry, registry_sha256 = load_backend_identities(project_dir)
    registered_backend = registry[root][backend_role]
    if (
        not isinstance(registered_backend, dict)
        or registered_backend["bucket"] != bucket
        or registered_backend["access_key_id_sha256"] != primary_identity
    ):
        raise TerraformSafetyError(
            "locking scope does not use this root's registered "
            f"{backend_role.replace('_', ' ')} bucket and credential"
        )
    scope = {
        "backend_type": "s3",
        "bucket": bucket,
        "endpoint": endpoint,
        "root": root,
        "backend_registry_role": backend_role,
        "backend_registry_sha256": registry_sha256,
        "primary_access_key_id_sha256": primary_identity,
        "terraform_version": TERRAFORM_VERSION,
    }
    return {
        "locking_scope": scope,
        "locking_scope_sha256": sha256_bytes(canonical_json(scope)),
    }


def attest_backend(
    root: str,
    metadata: dict[str, Any],
    workspace: str,
    project_dir: Path,
    lock_proof_path: Path | None,
    now: datetime | None = None,
    require_lock_proof: bool = True,
    registry_role: str = "production",
) -> dict[str, Any]:
    expected = require_root(root)
    if workspace != "default":
        raise TerraformSafetyError("production Terraform requires default workspace")
    backend = metadata.get("backend")
    if not isinstance(backend, dict):
        raise TerraformSafetyError("Terraform backend metadata is missing")
    backend_type = backend.get("type")
    if backend_type != expected["backend_type"]:
        raise TerraformSafetyError("Terraform backend type belongs to another root")
    config = backend.get("config")
    if not isinstance(config, dict):
        raise TerraformSafetyError("Terraform backend configuration is missing")

    if backend_type == "local":
        if lock_proof_path is not None:
            raise TerraformSafetyError(
                "local backend must not receive an S3 lock proof"
            )
        raw_path = config.get("path")
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            raise TerraformSafetyError("bootstrap state path must be absolute")
        state_path = Path(raw_path).resolve(strict=False)
        repository = project_dir.resolve(strict=True)
        if state_path == repository or repository in state_path.parents:
            raise TerraformSafetyError(
                "bootstrap state must be outside the Git repository"
            )
        identity = {
            "backend_type": "local",
            "path": str(state_path),
            "workspace": workspace,
        }
        registry_sha256 = require_registered_backend(
            root=root,
            identity=identity,
            project_dir=project_dir,
            role=registry_role,
        )
        return {
            "schema": CONTRACT_VERSION,
            "root": root,
            "terraform_version": TERRAFORM_VERSION,
            "identity": identity,
            "identity_sha256": sha256_bytes(canonical_json(identity)),
            "backend_registry_sha256": registry_sha256,
            "locking": {
                "mechanism": "local-filesystem",
                "proof_required": False,
                "proof_sha256": None,
            },
        }

    expected_key = expected["backend_key"]
    expected_endpoint = f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
    bucket = config.get("bucket")
    if (
        not isinstance(bucket, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket) is None
        or "replace" in bucket.lower()
    ):
        raise TerraformSafetyError("S3 backend bucket is absent or a placeholder")
    if config.get("key") != expected_key:
        raise TerraformSafetyError("S3 backend key belongs to another root")
    if config.get("region") != "auto":
        raise TerraformSafetyError("R2 backend region must be auto")
    endpoint = s3_endpoint(config)
    if endpoint != expected_endpoint:
        raise TerraformSafetyError("R2 endpoint belongs to another account")
    required_true = (
        "skip_credentials_validation",
        "skip_metadata_api_check",
        "skip_region_validation",
        "skip_requesting_account_id",
        "skip_s3_checksum",
        "use_lockfile",
        "use_path_style",
    )
    if any(config.get(key) is not True for key in required_true):
        raise TerraformSafetyError(
            "R2 backend compatibility or locking flags are incomplete"
        )
    sensitive_backend_fields = (
        "access_key",
        "secret_key",
        "token",
        "session_token",
        "sse_customer_key",
    )
    if any(config.get(key) not in (None, "") for key in sensitive_backend_fields):
        raise TerraformSafetyError(
            "backend credentials must come from the environment, not configuration"
        )
    reject_alternative_backend_credentials(config)
    reject_unsafe_s3_transport(config)
    primary_identity = primary_credential_identity()
    identity = {
        "backend_type": "s3",
        "bucket": bucket,
        "endpoint": endpoint,
        "key": expected_key,
        "region": "auto",
        "workspace": workspace,
        "access_key_id_sha256": primary_identity,
    }
    registry_sha256 = require_registered_backend(
        root=root,
        identity=identity,
        project_dir=project_dir,
        role=registry_role,
    )
    locking_scope = {
        "backend_type": "s3",
        "bucket": bucket,
        "endpoint": endpoint,
        "root": root,
        "backend_registry_role": registry_role,
        "backend_registry_sha256": registry_sha256,
        "primary_access_key_id_sha256": primary_identity,
        "terraform_version": TERRAFORM_VERSION,
    }
    locking_scope_sha256 = sha256_bytes(canonical_json(locking_scope))
    if not require_lock_proof:
        if lock_proof_path is not None:
            raise TerraformSafetyError(
                "read-only backend attestation must not receive a lock proof"
            )
        return {
            "schema": CONTRACT_VERSION,
            "root": root,
            "terraform_version": TERRAFORM_VERSION,
            "identity": identity,
            "identity_sha256": sha256_bytes(canonical_json(identity)),
            "backend_registry_sha256": registry_sha256,
            "locking": {
                "mechanism": "s3-lockfile",
                "proof_required_for_writes": True,
                "proof_verified": False,
                "scope_sha256": locking_scope_sha256,
                "backend_registry_role": registry_role,
                "primary_access_key_id_sha256": primary_identity,
            },
        }
    if lock_proof_path is None:
        raise TerraformSafetyError(
            "remote registered backend requires a current two-client lock proof"
        )
    proof = validate_lock_proof(
        lock_proof_path,
        root,
        registry_role,
        locking_scope_sha256,
        project_dir,
        now,
    )
    return {
        "schema": CONTRACT_VERSION,
        "root": root,
        "terraform_version": TERRAFORM_VERSION,
        "identity": identity,
        "identity_sha256": sha256_bytes(canonical_json(identity)),
        "backend_registry_sha256": registry_sha256,
        "locking": {
            "mechanism": "s3-lockfile",
            "proof_required": True,
            "scope_sha256": locking_scope_sha256,
            "backend_registry_role": registry_role,
            "proof_sha256": proof["sha256"],
            "tested_at": proof["tested_at"],
            "valid_until": proof["valid_until"],
            "operator": proof["operator"],
            "primary_access_key_id_sha256": proof["primary_access_key_id_sha256"],
            "cross_access_key_id_sha256": proof["cross_access_key_id_sha256"],
            "cross_root": proof["cross_root"],
            "cross_credential_positive_scope_sha256": proof[
                "cross_credential_positive_scope_sha256"
            ],
            "source_commit": proof["source_commit"],
            "locking_contract_sha256": proof["locking_contract_sha256"],
            "signature_sha256": proof["signature_sha256"],
            "allowed_signers_sha256": proof["allowed_signers_sha256"],
            "signer_identity": proof["signer_identity"],
        },
    }


def attest_migration_source_backend(
    root: str,
    metadata: dict[str, Any],
    workspace: str,
    project_dir: Path,
    lock_proof_path: Path | None,
    now: datetime | None = None,
    require_lock_proof: bool = True,
) -> dict[str, Any]:
    expected = require_root(root)
    if workspace != "default":
        raise TerraformSafetyError("state migration requires default workspace")
    backend = metadata.get("backend")
    if not isinstance(backend, dict):
        raise TerraformSafetyError("source backend metadata is missing")
    backend_type = backend.get("type")
    if backend_type != expected["backend_type"]:
        raise TerraformSafetyError(
            "source backend type differs from the backend declared by this root"
        )
    config = backend.get("config")
    if not isinstance(config, dict):
        raise TerraformSafetyError("source backend configuration is missing")
    if backend_type == "local":
        if lock_proof_path is not None:
            raise TerraformSafetyError(
                "local source backend must not receive an S3 lock proof"
            )
        raw_path = config.get("path")
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            raise TerraformSafetyError("source local state path must be absolute")
        state_path = Path(raw_path).resolve(strict=False)
        repository = project_dir.resolve(strict=True)
        if state_path == repository or repository in state_path.parents:
            raise TerraformSafetyError(
                "source local state must be outside the Git repository"
            )
        identity = {
            "backend_type": "local",
            "path": str(state_path),
            "workspace": workspace,
        }
        registry_sha256 = require_registered_backend(
            root=root,
            identity=identity,
            project_dir=project_dir,
            role="production",
        )
        return {
            "schema": CONTRACT_VERSION,
            "root": root,
            "terraform_version": TERRAFORM_VERSION,
            "identity": identity,
            "identity_sha256": sha256_bytes(canonical_json(identity)),
            "backend_registry_sha256": registry_sha256,
            "locking": {
                "mechanism": "local-filesystem",
                "proof_required": False,
                "proof_sha256": None,
            },
        }
    if backend_type != "s3":
        raise TerraformSafetyError("source backend type is not supported")

    bucket = config.get("bucket")
    key = config.get("key")
    if (
        not isinstance(bucket, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket) is None
        or "replace" in bucket.lower()
        or not isinstance(key, str)
        or not key
    ):
        raise TerraformSafetyError("source S3 backend identity is incomplete")
    endpoint = s3_endpoint(config)
    expected_endpoint = f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
    if endpoint != expected_endpoint or config.get("region") != "auto":
        raise TerraformSafetyError("source R2 account or region is unexpected")
    required_true = (
        "skip_credentials_validation",
        "skip_metadata_api_check",
        "skip_region_validation",
        "skip_requesting_account_id",
        "skip_s3_checksum",
        "use_lockfile",
        "use_path_style",
    )
    if any(config.get(field) is not True for field in required_true):
        raise TerraformSafetyError("source R2 backend is not safely locked")
    sensitive_backend_fields = (
        "access_key",
        "secret_key",
        "token",
        "session_token",
        "sse_customer_key",
    )
    if any(config.get(field) not in (None, "") for field in sensitive_backend_fields):
        raise TerraformSafetyError(
            "source backend credentials must come from the environment, "
            "not configuration"
        )
    reject_alternative_backend_credentials(config)
    reject_unsafe_s3_transport(config)
    primary_identity = primary_credential_identity()
    identity = {
        "backend_type": "s3",
        "bucket": bucket,
        "endpoint": endpoint,
        "key": key,
        "region": "auto",
        "workspace": workspace,
        "access_key_id_sha256": primary_identity,
    }
    registry_sha256 = require_registered_backend(
        root=root,
        identity=identity,
        project_dir=project_dir,
        role="production",
    )
    locking_scope = {
        "backend_type": "s3",
        "bucket": bucket,
        "endpoint": endpoint,
        "root": root,
        "backend_registry_role": "production",
        "backend_registry_sha256": registry_sha256,
        "primary_access_key_id_sha256": primary_identity,
        "terraform_version": TERRAFORM_VERSION,
    }
    locking_scope_sha256 = sha256_bytes(canonical_json(locking_scope))
    if not require_lock_proof:
        if lock_proof_path is not None:
            raise TerraformSafetyError(
                "read-only source attestation must not receive a lock proof"
            )
        return {
            "schema": CONTRACT_VERSION,
            "root": root,
            "terraform_version": TERRAFORM_VERSION,
            "identity": identity,
            "identity_sha256": sha256_bytes(canonical_json(identity)),
            "backend_registry_sha256": registry_sha256,
            "locking": {
                "mechanism": "s3-lockfile",
                "proof_required_for_writes": True,
                "proof_verified": False,
                "scope_sha256": locking_scope_sha256,
                "backend_registry_role": "production",
                "primary_access_key_id_sha256": primary_identity,
            },
        }
    if lock_proof_path is None:
        raise TerraformSafetyError(
            "remote source backend requires a current two-client lock proof"
        )
    proof = validate_lock_proof(
        lock_proof_path,
        root,
        "production",
        locking_scope_sha256,
        project_dir,
        now,
    )
    return {
        "schema": CONTRACT_VERSION,
        "root": root,
        "terraform_version": TERRAFORM_VERSION,
        "identity": identity,
        "identity_sha256": sha256_bytes(canonical_json(identity)),
        "backend_registry_sha256": registry_sha256,
        "locking": {
            "mechanism": "s3-lockfile",
            "proof_required": True,
            "scope_sha256": locking_scope_sha256,
            "backend_registry_role": "production",
            "proof_sha256": proof["sha256"],
            "tested_at": proof["tested_at"],
            "valid_until": proof["valid_until"],
            "operator": proof["operator"],
            "primary_access_key_id_sha256": proof["primary_access_key_id_sha256"],
            "cross_access_key_id_sha256": proof["cross_access_key_id_sha256"],
            "cross_root": proof["cross_root"],
            "cross_credential_positive_scope_sha256": proof[
                "cross_credential_positive_scope_sha256"
            ],
            "source_commit": proof["source_commit"],
            "locking_contract_sha256": proof["locking_contract_sha256"],
            "signature_sha256": proof["signature_sha256"],
            "allowed_signers_sha256": proof["allowed_signers_sha256"],
            "signer_identity": proof["signer_identity"],
        },
    }


def attest_migration_destination_backend(
    root: str,
    metadata: dict[str, Any],
    workspace: str,
    project_dir: Path,
    lock_proof_path: Path | None,
    now: datetime | None = None,
    require_lock_proof: bool = True,
) -> dict[str, Any]:
    return attest_backend(
        root,
        metadata,
        workspace,
        project_dir,
        lock_proof_path,
        now,
        require_lock_proof,
        registry_role="pending_destination",
    )


def instance_address(resource: dict[str, Any], instance: dict[str, Any]) -> str:
    module = resource.get("module")
    prefix = f"{module}." if isinstance(module, str) and module else ""
    base = f"{prefix}{resource.get('type')}.{resource.get('name')}"
    index = instance.get("index_key")
    if isinstance(index, str):
        return f"{base}[{json.dumps(index)}]"
    if isinstance(index, int):
        return f"{base}[{index}]"
    return base


def state_inventory(
    state: dict[str, Any],
) -> tuple[set[str], dict[str, Any], dict[str, dict[str, Any]]]:
    inventory: set[str] = set()
    identities: list[dict[str, Any]] = []
    instance_attributes: dict[str, dict[str, Any]] = {}
    resources = state.get("resources")
    if not isinstance(resources, list):
        raise TerraformSafetyError("Terraform state has no resource inventory")
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("mode") != "managed":
            raise TerraformSafetyError("Terraform state contains an unsupported entry")
        instances = resource.get("instances")
        if not isinstance(instances, list) or not instances:
            raise TerraformSafetyError("Terraform state contains an empty resource")
        for instance in instances:
            if not isinstance(instance, dict):
                raise TerraformSafetyError("Terraform state instance is invalid")
            address = instance_address(resource, instance)
            if address in inventory:
                raise TerraformSafetyError("Terraform state has duplicate addresses")
            inventory.add(address)
            attributes = instance.get("attributes")
            if not isinstance(attributes, dict):
                raise TerraformSafetyError(
                    f"Terraform state attributes are missing for {address}"
                )
            instance_attributes[address] = attributes
            if address == "terraform_data.root_identity":
                value = attributes.get("output")
                if value is None:
                    value = attributes.get("input")
                if not isinstance(value, dict):
                    raise TerraformSafetyError("root identity value is missing")
                identities.append(value)
    if len(identities) != 1:
        raise TerraformSafetyError("state must contain exactly one root identity")
    return inventory, identities[0], instance_attributes


def selected_managed_contract(
    resources: dict[str, tuple[str, dict[str, Any]]],
    *,
    planned: bool,
    unknowns: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    def mask_unknown(value: Any, unknown: Any) -> Any:
        if unknown is True:
            return UNKNOWN_VALUE
        if isinstance(unknown, dict):
            source = value if isinstance(value, dict) else {}
            return {
                key: mask_unknown(source.get(key), nested_unknown)
                for key, nested_unknown in unknown.items()
                if nested_unknown not in (False, None, {}, [])
            } | {
                key: nested_value
                for key, nested_value in source.items()
                if key not in unknown or unknown.get(key) in (False, None, {}, [])
            }
        if isinstance(unknown, list):
            source = value if isinstance(value, list) else [None] * len(unknown)
            if len(source) != len(unknown):
                raise TerraformSafetyError(
                    "planned unknown-value shape differs from planned values"
                )
            return [
                mask_unknown(nested_value, nested_unknown)
                for nested_value, nested_unknown in zip(source, unknown)
            ]
        return value

    contract: dict[str, dict[str, Any]] = {}
    for address, (resource_type, values) in resources.items():
        if resource_type == "terraform_data":
            continue
        fields = MANAGED_CONTRACT_FIELDS.get(resource_type)
        if fields is None:
            raise TerraformSafetyError(
                f"no managed-value contract exists for {resource_type}"
            )
        resource_unknowns = (
            unknowns.get(address, {}) if planned and isinstance(unknowns, dict) else {}
        )
        if not isinstance(resource_unknowns, dict):
            raise TerraformSafetyError(
                f"planned unknown-value contract is invalid for {address}"
            )
        selected: dict[str, Any] = {}
        for field in fields:
            field_unknown = resource_unknowns.get(field, False)
            if field in values or field_unknown not in (False, None, {}, []):
                selected[field] = mask_unknown(
                    values.get(field),
                    field_unknown,
                )
        if not selected:
            raise TerraformSafetyError(f"managed values are missing for {address}")
        contract[address] = selected
    return contract


def expected_fixed_inventory(root: str) -> set[str] | None:
    if root == "cloudflare/state-bootstrap":
        return {
            "terraform_data.root_identity",
            'cloudflare_r2_bucket.terraform_state["cloudflare_dns"]',
            'cloudflare_r2_bucket.terraform_state["netcup_perimeter"]',
            'cloudflare_r2_managed_domain.terraform_state["cloudflare_dns"]',
            'cloudflare_r2_managed_domain.terraform_state["netcup_perimeter"]',
            "cloudflare_r2_bucket.dockerswarm_backups",
            "cloudflare_r2_managed_domain.dockerswarm_backups",
        }
    if root == "cloudflare/apptolast-dns":
        return {
            "terraform_data.root_identity",
            *{f"cloudflare_dns_record.managed[{json.dumps(key)}]" for key in DNS_KEYS},
        }
    return None


def validate_inventory(
    root: str,
    inventory: set[str],
    *,
    exact: bool,
) -> None:
    fixed = expected_fixed_inventory(root)
    if fixed is not None:
        if (
            "terraform_data.root_identity" not in inventory
            or not inventory.issubset(fixed)
            or (exact and inventory != fixed)
        ):
            raise TerraformSafetyError(
                f"{root} inventory differs from its allowed resource contract"
            )
        return
    allowed = re.compile(
        r"^(?:"
        r"terraform_data\.root_identity|"
        r"netcup_firewall_policy\.host_ingress\[0\]|"
        r"netcup_server_firewall\.host\[0\]|"
        r"netcup_ssh_key\.image_installation\[\"[a-z0-9_-]+\"\]"
        r")$"
    )
    if any(allowed.fullmatch(address) is None for address in inventory):
        raise TerraformSafetyError("Netcup state contains an unmodeled address")
    if "terraform_data.root_identity" not in inventory:
        raise TerraformSafetyError("Netcup state has no root identity")
    policy = "netcup_firewall_policy.host_ingress[0]" in inventory
    assignment = "netcup_server_firewall.host[0]" in inventory
    if exact and policy != assignment:
        raise TerraformSafetyError(
            "Netcup firewall policy and assignment must converge together"
        )


def validate_raw_check_results(
    root: str,
    check_results: Any,
    managed_inventory: set[str],
) -> dict[str, Any]:
    expected = EXPECTED_CHECKS.get(root)
    if expected is None or not expected:
        raise TerraformSafetyError(
            f"{root} has no reviewed raw Terraform check inventory"
        )
    if not isinstance(check_results, list) or not check_results:
        raise TerraformSafetyError(
            "raw Terraform state check results are absent or empty"
        )
    projected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in check_results:
        if (
            not isinstance(result, dict)
            or set(result) != {"config_addr", "object_kind", "objects", "status"}
            or result.get("status") != "pass"
        ):
            raise TerraformSafetyError(
                "raw Terraform state check result is malformed or did not pass"
            )
        address = result.get("config_addr")
        kind = result.get("object_kind")
        if (
            not isinstance(address, str)
            or address not in expected
            or address in seen
            or kind != expected[address]
        ):
            raise TerraformSafetyError(
                "raw Terraform state check inventory is unexpected or duplicated"
            )
        seen.add(address)
        if kind in {"check", "var"}:
            expected_instances = {address}
        else:
            expected_instances = {
                managed_address
                for managed_address in managed_inventory
                if managed_address == address
                or managed_address.startswith(f"{address}[")
            }
        objects = result.get("objects")
        if objects is None and kind == "resource" and not expected_instances:
            objects = []
        if not isinstance(objects, list):
            raise TerraformSafetyError(
                f"raw Terraform check objects are malformed: {address}"
            )
        object_addresses: list[str] = []
        for checked_object in objects:
            if (
                not isinstance(checked_object, dict)
                or set(checked_object) != {"object_addr", "status"}
                or checked_object.get("status") != "pass"
                or not isinstance(checked_object.get("object_addr"), str)
            ):
                raise TerraformSafetyError(
                    f"raw Terraform check object did not pass: {address}"
                )
            object_addresses.append(checked_object["object_addr"])
        if (
            len(object_addresses) != len(set(object_addresses))
            or set(object_addresses) != expected_instances
        ):
            raise TerraformSafetyError(
                f"raw Terraform check objects differ from state inventory: {address}"
            )
        projected.append(
            {
                "address": address,
                "kind": kind,
                "status": "pass",
                "instances": sorted(object_addresses),
            }
        )
    if seen != set(expected):
        raise TerraformSafetyError("raw Terraform state check inventory is incomplete")
    projected.sort(key=lambda item: item["address"])
    return {
        "checks": projected,
        "checks_sha256": sha256_bytes(canonical_json(projected)),
    }


def attest_state(root: str, state: dict[str, Any]) -> dict[str, Any]:
    expected = require_root(root)
    validate_structural_state(state)
    if state.get("terraform_version") != TERRAFORM_VERSION:
        raise TerraformSafetyError(
            "Terraform state was written by an unreviewed Terraform version"
        )
    lineage = state.get("lineage")
    serial = state.get("serial")
    if not isinstance(lineage, str) or not lineage:
        raise TerraformSafetyError("Terraform state lineage is missing")
    if not isinstance(serial, int) or isinstance(serial, bool) or serial < 0:
        raise TerraformSafetyError("Terraform state serial is invalid")
    inventory, identity, instance_attributes = state_inventory(state)
    if identity != expected:
        raise TerraformSafetyError("Terraform state belongs to another root")
    validate_inventory(root, inventory, exact=False)
    check_contract = validate_raw_check_results(
        root,
        state.get("check_results"),
        inventory,
    )
    ordered_inventory = sorted(inventory)
    state_resources = {
        address: (
            address.split(".", maxsplit=1)[0],
            attributes,
        )
        for address, attributes in instance_attributes.items()
    }
    managed_contract = selected_managed_contract(
        state_resources,
        planned=False,
    )
    return {
        "schema": CONTRACT_VERSION,
        "root": root,
        "lineage": lineage,
        "serial": serial,
        "root_identity": identity,
        "inventory": ordered_inventory,
        "inventory_sha256": sha256_bytes(canonical_json(ordered_inventory)),
        "checks": check_contract["checks"],
        "checks_sha256": check_contract["checks_sha256"],
        "managed_contract": managed_contract,
        "managed_contract_sha256": sha256_bytes(canonical_json(managed_contract)),
        "state_sha256": sha256_bytes(canonical_json(state)),
    }


def attest_empty_state(root: str) -> dict[str, Any]:
    require_root(root)
    return {
        "schema": CONTRACT_VERSION,
        "root": root,
        "empty": True,
        "lineage": None,
        "serial": None,
        "root_identity": None,
        "inventory": [],
        "inventory_sha256": sha256_bytes(canonical_json([])),
        "managed_contract": {},
        "managed_contract_sha256": sha256_bytes(canonical_json({})),
    }


def validate_structural_state(state: dict[str, Any]) -> None:
    if state.get("version") != 4:
        raise TerraformSafetyError("Terraform state format version is invalid")
    lineage = state.get("lineage")
    serial = state.get("serial")
    if (
        not isinstance(lineage, str)
        or not lineage
        or not isinstance(serial, int)
        or isinstance(serial, bool)
        or serial < 0
        or not isinstance(state.get("resources"), list)
        or not isinstance(state.get("outputs"), dict)
    ):
        raise TerraformSafetyError(
            "Terraform state lacks its structural recovery fields"
        )


def verify_attested_state_bytes(
    root: str,
    state_bytes: bytes,
    expected_attestation: dict[str, Any],
    stderr_path: Path,
) -> bytes:
    if len(state_bytes) > MAX_STATE_BYTES:
        raise TerraformSafetyError("Terraform state exceeds the migration safety limit")
    state = load_json_bytes(state_bytes, "Terraform migration state")
    actual_attestation = attest_state(root, state)
    if actual_attestation != expected_attestation:
        raise TerraformSafetyError(
            "Terraform migration source changed after typed confirmation"
        )
    validate_empty_stderr(stderr_path, "Terraform migration source state pull")
    return state_bytes


def flatten_planned_resources(module: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    resources = module.get("resources", [])
    if not isinstance(resources, list):
        raise TerraformSafetyError("planned resources are invalid")
    for resource in resources:
        if not isinstance(resource, dict):
            raise TerraformSafetyError("planned resource is invalid")
        result.append(resource)
    children = module.get("child_modules", [])
    if not isinstance(children, list):
        raise TerraformSafetyError("planned child modules are invalid")
    for child in children:
        if not isinstance(child, dict):
            raise TerraformSafetyError("planned child module is invalid")
        result.extend(flatten_planned_resources(child))
    return result


def validate_check_results(
    root: str,
    checks: Any,
    managed_inventory: set[str],
) -> dict[str, Any]:
    expected = EXPECTED_CHECKS.get(root)
    if expected is None or not expected:
        raise TerraformSafetyError(f"{root} has no reviewed Terraform check inventory")
    if not isinstance(checks, list) or not checks:
        raise TerraformSafetyError("Terraform check results are absent or empty")

    projected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in checks:
        if (
            not isinstance(result, dict)
            or set(result)
            not in (
                {"address", "status"},
                {"address", "instances", "status"},
            )
            or result.get("status") != "pass"
        ):
            raise TerraformSafetyError(
                "Terraform check result is malformed or did not pass"
            )
        address = result.get("address")
        if not isinstance(address, dict):
            raise TerraformSafetyError("Terraform check address is malformed")
        to_display = address.get("to_display")
        if (
            not isinstance(to_display, str)
            or to_display not in expected
            or to_display in seen
        ):
            raise TerraformSafetyError(
                "Terraform check inventory is unexpected or duplicated"
            )
        seen.add(to_display)
        expected_kind = expected[to_display]
        if expected_kind == "check":
            expected_address = {
                "kind": "check",
                "name": to_display.removeprefix("check."),
                "to_display": to_display,
            }
            expected_instances = {to_display}
        elif expected_kind == "var":
            expected_address = {
                "kind": "var",
                "name": to_display.removeprefix("var."),
                "to_display": to_display,
            }
            expected_instances = {to_display}
        else:
            resource_type, resource_name = to_display.split(".", maxsplit=1)
            expected_address = {
                "kind": "resource",
                "mode": "managed",
                "name": resource_name,
                "to_display": to_display,
                "type": resource_type,
            }
            expected_instances = {
                managed_address
                for managed_address in managed_inventory
                if managed_address == to_display
                or managed_address.startswith(f"{to_display}[")
            }
        if address != expected_address:
            raise TerraformSafetyError(
                f"Terraform check address is not canonical: {to_display}"
            )
        instances = result.get("instances", [])
        if not isinstance(instances, list):
            raise TerraformSafetyError(
                f"Terraform check instances are malformed: {to_display}"
            )
        instance_addresses: list[str] = []
        for instance in instances:
            if (
                not isinstance(instance, dict)
                or set(instance) != {"address", "status"}
                or instance.get("status") != "pass"
                or not isinstance(instance.get("address"), dict)
                or set(instance["address"]) != {"to_display"}
                or not isinstance(instance["address"].get("to_display"), str)
            ):
                raise TerraformSafetyError(
                    f"Terraform check instance did not pass: {to_display}"
                )
            instance_addresses.append(instance["address"]["to_display"])
        if (
            len(instance_addresses) != len(set(instance_addresses))
            or set(instance_addresses) != expected_instances
        ):
            raise TerraformSafetyError(
                f"Terraform check instances differ from managed inventory: "
                f"{to_display}"
            )
        projected.append(
            {
                "address": to_display,
                "kind": expected_kind,
                "status": "pass",
                "instances": sorted(instance_addresses),
            }
        )
    if seen != set(expected):
        raise TerraformSafetyError("Terraform check inventory is incomplete")
    projected.sort(key=lambda item: item["address"])
    return {
        "checks": projected,
        "checks_sha256": sha256_bytes(canonical_json(projected)),
    }


def validate_state_check_document(
    root: str,
    document: dict[str, Any],
) -> dict[str, Any]:
    if (
        document.get("format_version") != "1.0"
        or document.get("terraform_version") != TERRAFORM_VERSION
        or set(document)
        - {
            "checks",
            "format_version",
            "terraform_version",
            "values",
        }
    ):
        raise TerraformSafetyError(
            "post-apply Terraform state JSON format is not reviewed"
        )
    values = document.get("values")
    if not isinstance(values, dict):
        raise TerraformSafetyError("post-apply Terraform state JSON has no values")
    root_module = values.get("root_module")
    if not isinstance(root_module, dict):
        raise TerraformSafetyError("post-apply Terraform state JSON has no root module")
    resources = flatten_planned_resources(root_module)
    managed_inventory = {
        resource.get("address")
        for resource in resources
        if resource.get("mode") == "managed"
        and isinstance(resource.get("address"), str)
    }
    if len(managed_inventory) != len(
        [
            resource
            for resource in resources
            if resource.get("mode") == "managed"
            and isinstance(resource.get("address"), str)
        ]
    ):
        raise TerraformSafetyError(
            "post-apply Terraform state JSON has duplicate managed resources"
        )
    validate_inventory(root, managed_inventory, exact=True)
    return validate_check_results(
        root,
        document.get("checks"),
        managed_inventory,
    )


def validate_dns_planned_values(
    resources: list[dict[str, Any]],
    operation_mode: str,
    plan_variables: Any,
) -> None:
    if operation_mode not in {"established", "initialize"}:
        raise TerraformSafetyError("Terraform operation mode is invalid")
    records = {
        resource.get("index"): resource.get("values")
        for resource in resources
        if resource.get("type") == "cloudflare_dns_record"
        and resource.get("name") == "managed"
    }
    if set(records) != set(DNS_KEYS):
        raise TerraformSafetyError("DNS plan does not contain the exact record keys")
    adoption_only = (
        plan_variables.get("adoption_only", {}).get("value")
        if isinstance(plan_variables, dict)
        and isinstance(plan_variables.get("adoption_only"), dict)
        else None
    )
    if adoption_only is not (operation_mode == "initialize"):
        raise TerraformSafetyError(
            f"adoption_only does not match {operation_mode} mode"
        )
    for key in DNS_KEYS:
        values = records[key]
        if not isinstance(values, dict):
            raise TerraformSafetyError(f"DNS planned values are missing for {key}")
        expected_contents = {DNS_PLATFORM_IPV4}
        if operation_mode == "initialize" and key != "edge":
            expected_contents = {DNS_LEGACY_IPV4}
        elif operation_mode == "established" and key != "edge":
            expected_contents = {DNS_LEGACY_IPV4, DNS_PLATFORM_IPV4}
        expected_values = {
            "zone_id": CLOUDFLARE_ZONE_ID,
            "name": f"{key}.apptolast.com",
            "type": "A",
            "proxied": False,
            "ttl": 300,
        }
        if values.get("content") not in expected_contents or any(
            values.get(field) != value for field, value in expected_values.items()
        ):
            raise TerraformSafetyError(
                f"DNS planned values violate the {operation_mode} contract: {key}"
            )


def validate_plan_document(
    root: str,
    plan: dict[str, Any],
    operation_mode: str,
) -> dict[str, Any]:
    if plan.get("format_version") != "1.2":
        raise TerraformSafetyError(
            "saved plan JSON format is not the reviewed Terraform 1.2 format"
        )
    allowed_plan_fields = {
        "action_invocations",
        "applyable",
        "checks",
        "complete",
        "configuration",
        "deferred_changes",
        "errored",
        "format_version",
        "output_changes",
        "planned_values",
        "prior_state",
        "proposed_unknown",
        "relevant_attributes",
        "resource_changes",
        "resource_drift",
        "terraform_version",
        "timestamp",
        "variables",
    }
    unexpected_fields = set(plan) - allowed_plan_fields
    if unexpected_fields:
        raise TerraformSafetyError(
            "saved plan contains unreviewed top-level fields: "
            + ", ".join(sorted(unexpected_fields))
        )
    action_invocations = plan.get("action_invocations")
    if action_invocations not in (None, [], {}):
        raise TerraformSafetyError(
            "saved plans may not invoke Terraform provider actions"
        )
    deferred_changes = plan.get("deferred_changes")
    if deferred_changes not in (None, [], {}):
        raise TerraformSafetyError("saved plans may not contain deferred changes")
    configuration = plan.get("configuration")

    def reject_action_configuration(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in {
                    "action",
                    "actions",
                    "action_trigger",
                    "action_triggers",
                    "action_invocation",
                    "action_invocations",
                } and nested not in (None, [], {}):
                    raise TerraformSafetyError(
                        "Terraform action configuration is forbidden"
                    )
                reject_action_configuration(nested)
        elif isinstance(value, list):
            for nested in value:
                reject_action_configuration(nested)

    if configuration is not None:
        if not isinstance(configuration, dict):
            raise TerraformSafetyError(
                "saved plan configuration representation is invalid"
            )
        reject_action_configuration(configuration)
    expected = require_root(root)
    if plan.get("terraform_version") != TERRAFORM_VERSION:
        raise TerraformSafetyError("saved plan uses another Terraform version")
    if plan.get("errored") is not False or plan.get("complete") is not True:
        raise TerraformSafetyError("saved plan is errored or incomplete")
    if plan.get("applyable") is not True:
        raise TerraformSafetyError("saved plan has no applyable changes")
    planned_values = plan.get("planned_values")
    if not isinstance(planned_values, dict):
        raise TerraformSafetyError("saved plan has no planned values")
    root_module = planned_values.get("root_module")
    if not isinstance(root_module, dict):
        raise TerraformSafetyError("saved plan has no root module")
    resources = flatten_planned_resources(root_module)
    resource_addresses: list[str] = []
    for resource in resources:
        address = resource.get("address")
        if (
            resource.get("mode") != "managed"
            or not isinstance(address, str)
            or not address
            or not isinstance(resource.get("type"), str)
            or not isinstance(resource.get("name"), str)
            or not isinstance(resource.get("values"), dict)
        ):
            raise TerraformSafetyError(
                "saved plan contains an unsupported or malformed resource"
            )
        resource_addresses.append(address)
    if len(resource_addresses) != len(set(resource_addresses)):
        raise TerraformSafetyError("saved plan contains duplicate resource addresses")
    check_contract = validate_check_results(
        root,
        plan.get("checks"),
        set(resource_addresses),
    )
    identities = [
        resource.get("values", {}).get("input")
        for resource in resources
        if resource.get("address") == "terraform_data.root_identity"
        and resource.get("type") == "terraform_data"
        and resource.get("name") == "root_identity"
        and isinstance(resource.get("values"), dict)
    ]
    if identities != [expected]:
        raise TerraformSafetyError("saved plan has another root identity")
    planned_inventory = {resource["address"] for resource in resources}
    validate_inventory(root, planned_inventory, exact=True)
    if root == "cloudflare/apptolast-dns":
        validate_dns_planned_values(
            resources,
            operation_mode,
            plan.get("variables"),
        )
    changes = plan.get("resource_changes")
    if not isinstance(changes, list):
        raise TerraformSafetyError("saved plan has no resource change inventory")
    unknowns_by_address = {
        change["address"]: change.get("change", {}).get("after_unknown", {})
        for change in changes
        if isinstance(change, dict)
        and isinstance(change.get("address"), str)
        and isinstance(change.get("change"), dict)
    }
    planned_resources = {
        resource["address"]: (
            resource.get("type", ""),
            resource.get("values", {}),
        )
        for resource in resources
        if resource.get("mode", "managed") == "managed"
        and isinstance(resource.get("address"), str)
        and isinstance(resource.get("values"), dict)
    }
    managed_contract = selected_managed_contract(
        planned_resources,
        planned=True,
        unknowns=unknowns_by_address,
    )

    counts = {"create": 0, "read": 0, "update": 0, "no-op": 0}
    allowed = set(counts)
    changed_addresses: set[str] = set()
    for resource_change in changes:
        if not isinstance(resource_change, dict):
            raise TerraformSafetyError("saved plan contains an invalid change")
        address = resource_change.get("address")
        if (
            not isinstance(address, str)
            or address not in planned_inventory
            or address in changed_addresses
        ):
            raise TerraformSafetyError(
                "saved plan contains an unmodeled or duplicate resource change"
            )
        changed_addresses.add(address)
        change = resource_change.get("change")
        actions = change.get("actions") if isinstance(change, dict) else None
        if (
            not isinstance(actions, list)
            or len(actions) != 1
            or any(not isinstance(action, str) for action in actions)
        ):
            raise TerraformSafetyError("saved plan contains invalid actions")
        if "delete" in actions or any(action not in allowed for action in actions):
            address = resource_change.get("address", "<unknown>")
            raise TerraformSafetyError(
                f"destructive or unsupported Terraform action is forbidden: {address}"
            )
        if (
            root == "cloudflare/apptolast-dns"
            and address.startswith("cloudflare_dns_record.managed[")
            and actions != ["no-op"]
        ):
            before = change.get("before")
            after = change.get("after")
            if not isinstance(after, dict) or not isinstance(after.get("content"), str):
                raise TerraformSafetyError(
                    "DNS changes require explicit resulting content"
                )
            before_content = before.get("content") if isinstance(before, dict) else None
            if (
                before_content != DNS_PLATFORM_IPV4
                and after["content"] == DNS_PLATFORM_IPV4
            ):
                raise TerraformSafetyError(
                    "DNS creation/cutover to the platform is disabled until a "
                    "signed host-readiness coordinator is implemented"
                )
        for action in actions:
            counts[action] += 1
    ordered_inventory = sorted(planned_inventory)
    return {
        "action_counts": counts,
        "checks": check_contract["checks"],
        "checks_sha256": check_contract["checks_sha256"],
        "planned_inventory": ordered_inventory,
        "planned_inventory_sha256": sha256_bytes(canonical_json(ordered_inventory)),
        "planned_managed_contract": managed_contract,
        "planned_managed_contract_sha256": sha256_bytes(
            canonical_json(managed_contract)
        ),
    }


def validate_plan_ui_attestation(attestation: Any) -> dict[str, Any]:
    expected_fields = {
        "change_summary",
        "diagnostic_count",
        "event_count",
        "events_sha256",
        "operation",
        "schema",
        "stderr_sha256",
        "terraform_version",
        "ui_version",
    }
    if not isinstance(attestation, dict) or set(attestation) != expected_fields:
        raise TerraformSafetyError("Terraform plan UI attestation is malformed")
    ui_version = attestation.get("ui_version")
    if (
        attestation.get("schema") != CONTRACT_VERSION
        or attestation.get("operation") != "plan"
        or attestation.get("terraform_version") != TERRAFORM_VERSION
        or not isinstance(ui_version, str)
        or ui_version.split(".", maxsplit=1)[0] != "1"
        or not isinstance(attestation.get("event_count"), int)
        or isinstance(attestation.get("event_count"), bool)
        or attestation["event_count"] < 2
        or attestation.get("diagnostic_count") != 0
        or re.fullmatch(r"[a-f0-9]{64}", attestation.get("events_sha256", "")) is None
        or attestation.get("stderr_sha256") != sha256_bytes(b"")
    ):
        raise TerraformSafetyError(
            "Terraform plan UI attestation did not prove a clean run"
        )
    summary = attestation.get("change_summary")
    expected_summary_fields = {
        "action_invocation",
        "add",
        "change",
        "import",
        "remove",
    }
    if not isinstance(summary, dict) or set(summary) != expected_summary_fields:
        raise TerraformSafetyError("Terraform plan UI change summary is malformed")
    for field in expected_summary_fields:
        value = summary.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise TerraformSafetyError("Terraform plan UI change summary is invalid")
    if summary.get("remove") != 0 or summary.get("action_invocation") != 0:
        raise TerraformSafetyError(
            "Terraform plan UI attestation contains a forbidden action"
        )
    return attestation


def build_sidecar(
    root: str,
    plan_bytes: bytes,
    plan_sha256: str,
    source_commit: str,
    backend_attestation: dict[str, Any],
    state_attestation: dict[str, Any],
    variable_file_sha256: str | None,
    operation_mode: str,
    plan_ui_attestation: dict[str, Any],
) -> dict[str, Any]:
    if re.fullmatch(r"[a-f0-9]{64}", plan_sha256) is None:
        raise TerraformSafetyError("saved plan SHA-256 is invalid")
    if re.fullmatch(r"[a-f0-9]{40}", source_commit) is None:
        raise TerraformSafetyError("source commit is invalid")
    if backend_attestation.get("root") != root:
        raise TerraformSafetyError("backend attestation belongs to another root")
    if state_attestation.get("root") != root:
        raise TerraformSafetyError("state attestation belongs to another root")
    if operation_mode not in {"established", "initialize"}:
        raise TerraformSafetyError("Terraform operation mode is invalid")
    if operation_mode == "initialize":
        if state_attestation != attest_empty_state(root):
            raise TerraformSafetyError(
                "initialize plan requires an attested empty backend"
            )
    elif state_attestation.get("empty") is True:
        raise TerraformSafetyError(
            "established plan requires an attested root identity"
        )
    if (
        variable_file_sha256 is not None
        and re.fullmatch(r"[a-f0-9]{64}", variable_file_sha256) is None
    ):
        raise TerraformSafetyError("variable file SHA-256 is invalid")
    plan = load_json_bytes(plan_bytes, "saved plan JSON")
    plan_contract = validate_plan_document(root, plan, operation_mode)
    validated_plan_ui = validate_plan_ui_attestation(plan_ui_attestation)
    timestamp = plan.get("timestamp")
    parse_utc(timestamp, "plan timestamp")
    return {
        "schema": CONTRACT_VERSION,
        "root": root,
        "source_commit": source_commit,
        "terraform_version": TERRAFORM_VERSION,
        "workspace": "default",
        "operation_mode": operation_mode,
        "plan_sha256": plan_sha256,
        "plan_json_sha256": sha256_bytes(plan_bytes),
        "variable_file_sha256": variable_file_sha256,
        "created_at": timestamp,
        "destructive_actions_allowed": False,
        "action_counts": plan_contract["action_counts"],
        "checks": plan_contract["checks"],
        "checks_sha256": plan_contract["checks_sha256"],
        "plan_ui": validated_plan_ui,
        "planned_inventory": plan_contract["planned_inventory"],
        "planned_inventory_sha256": plan_contract["planned_inventory_sha256"],
        "planned_managed_contract": plan_contract["planned_managed_contract"],
        "planned_managed_contract_sha256": plan_contract[
            "planned_managed_contract_sha256"
        ],
        "backend": backend_attestation,
        "prior_state": state_attestation,
    }


def verify_sidecar(
    root: str,
    sidecar: dict[str, Any],
    plan_bytes: bytes,
    plan_sha256: str,
    source_commit: str,
    backend_attestation: dict[str, Any],
    state_attestation: dict[str, Any],
    sidecar_bytes: bytes,
    sidecar_signature: Path,
    project_dir: Path,
    now: datetime | None = None,
) -> None:
    verify_plan_signature(
        sidecar_bytes,
        sidecar_signature,
        project_dir,
    )
    if sidecar.get("schema") != CONTRACT_VERSION:
        raise TerraformSafetyError("plan sidecar schema is not supported")
    expected = build_sidecar(
        root,
        plan_bytes,
        plan_sha256,
        source_commit,
        backend_attestation,
        state_attestation,
        sidecar.get("variable_file_sha256"),
        sidecar.get("operation_mode"),
        sidecar.get("plan_ui"),
    )
    if sidecar != expected:
        raise TerraformSafetyError(
            "saved plan sidecar differs from current source, backend, state, or plan"
        )
    created_at = parse_utc(sidecar.get("created_at"), "plan created_at")
    current = now or datetime.now(timezone.utc)
    if created_at > current:
        raise TerraformSafetyError("saved plan timestamp is in the future")
    if (current - created_at).total_seconds() > PLAN_MAX_SECONDS:
        raise TerraformSafetyError("saved plan is older than one hour")


def verify_post_state(
    before: dict[str, Any],
    after: dict[str, Any],
    sidecar: dict[str, Any],
) -> None:
    def matches_expected(expected: Any, actual: Any) -> bool:
        if expected == UNKNOWN_VALUE:
            return True
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(
                key in actual and matches_expected(expected_value, actual[key])
                for key, expected_value in expected.items()
            )
        if isinstance(expected, list):
            return (
                isinstance(actual, list)
                and len(expected) == len(actual)
                and all(
                    matches_expected(expected_item, actual_item)
                    for expected_item, actual_item in zip(expected, actual)
                )
            )
        return expected == actual

    if before.get("root") != after.get("root"):
        raise TerraformSafetyError("post-apply state belongs to another root")
    expected_inventory = sidecar.get("planned_inventory")
    if (
        not isinstance(expected_inventory, list)
        or after.get("inventory") != expected_inventory
        or after.get("inventory_sha256") != sidecar.get("planned_inventory_sha256")
    ):
        raise TerraformSafetyError(
            "post-apply state inventory differs from the saved plan"
        )
    expected_contract = sidecar.get("planned_managed_contract")
    actual_contract = after.get("managed_contract")
    if not isinstance(expected_contract, dict) or not isinstance(
        actual_contract,
        dict,
    ):
        raise TerraformSafetyError("post-apply managed-value contract is missing")
    if sha256_bytes(canonical_json(expected_contract)) != sidecar.get(
        "planned_managed_contract_sha256"
    ) or sha256_bytes(canonical_json(actual_contract)) != after.get(
        "managed_contract_sha256"
    ):
        raise TerraformSafetyError("post-apply managed-value contract hash is invalid")
    for address, expected_values in expected_contract.items():
        actual_values = actual_contract.get(address)
        if (
            not isinstance(expected_values, dict)
            or not isinstance(actual_values, dict)
            or not matches_expected(expected_values, actual_values)
        ):
            raise TerraformSafetyError(
                f"post-apply managed values differ from the saved plan: {address}"
            )
    if after.get("root") == "netcup/perimeter":
        policy = actual_contract.get("netcup_firewall_policy.host_ingress[0]")
        assignment = actual_contract.get("netcup_server_firewall.host[0]")
        if policy is not None or assignment is not None:
            policy_id = policy.get("id") if isinstance(policy, dict) else None
            policy_ids = (
                assignment.get("policy_ids") if isinstance(assignment, dict) else None
            )
            if policy_id in (None, "") or policy_ids != [policy_id]:
                raise TerraformSafetyError(
                    "Netcup firewall assignment does not reference its "
                    "managed policy"
                )
    if before.get("empty") is True:
        if after.get("root_identity") != require_root(after.get("root")):
            raise TerraformSafetyError("initialized state has no exact root identity")
        if (
            not isinstance(after.get("serial"), int)
            or isinstance(after.get("serial"), bool)
            or after.get("serial") < 1
        ):
            raise TerraformSafetyError("initialized state serial did not advance")
        return
    if before.get("lineage") != after.get("lineage"):
        raise TerraformSafetyError("post-apply state lineage changed")
    before_serial = before.get("serial")
    after_serial = after.get("serial")
    if not isinstance(before_serial, int) or not isinstance(after_serial, int):
        raise TerraformSafetyError("state serial attestation is invalid")
    if after_serial <= before_serial:
        raise TerraformSafetyError("post-apply state serial did not advance")
    if before.get("root_identity") != after.get("root_identity"):
        raise TerraformSafetyError("post-apply root identity changed")


def verify_post_apply_checks(
    root: str,
    state_document: dict[str, Any],
    sidecar: dict[str, Any],
) -> None:
    if sidecar.get("root") != root:
        raise TerraformSafetyError(
            "saved plan sidecar belongs to another Terraform root"
        )
    result = validate_state_check_document(root, state_document)
    if result.get("checks") != sidecar.get("checks") or result.get(
        "checks_sha256"
    ) != sidecar.get("checks_sha256"):
        raise TerraformSafetyError(
            "post-apply Terraform checks differ from the signed saved plan"
        )


def write_document(document: dict[str, Any], stream: BinaryIO) -> None:
    stream.write(canonical_json(document))


def parser() -> argparse.ArgumentParser:
    root_choices = sorted(ROOT_IDENTITIES)
    argument_parser = argparse.ArgumentParser(description=__doc__)
    subparsers = argument_parser.add_subparsers(dest="command", required=True)

    backend = subparsers.add_parser("attest-backend")
    backend.add_argument("--root", choices=root_choices, required=True)
    backend.add_argument("--metadata", type=Path, required=True)
    backend.add_argument("--workspace", required=True)
    backend.add_argument("--project-dir", type=Path, required=True)
    backend.add_argument("--lock-proof", type=Path)

    source_backend = subparsers.add_parser("attest-migration-source")
    source_backend.add_argument("--root", choices=root_choices, required=True)
    source_backend.add_argument("--metadata", type=Path, required=True)
    source_backend.add_argument("--workspace", required=True)
    source_backend.add_argument("--project-dir", type=Path, required=True)
    source_backend.add_argument("--lock-proof", type=Path)

    destination_backend = subparsers.add_parser("attest-migration-destination")
    destination_backend.add_argument(
        "--root",
        choices=root_choices,
        required=True,
    )
    destination_backend.add_argument("--metadata", type=Path, required=True)
    destination_backend.add_argument("--workspace", required=True)
    destination_backend.add_argument(
        "--project-dir",
        type=Path,
        required=True,
    )
    destination_backend.add_argument("--lock-proof", type=Path)

    snapshot_backend = subparsers.add_parser("attest-snapshot-backend")
    snapshot_backend.add_argument("--root", choices=root_choices, required=True)
    snapshot_backend.add_argument("--metadata", type=Path, required=True)
    snapshot_backend.add_argument("--workspace", required=True)
    snapshot_backend.add_argument("--project-dir", type=Path, required=True)
    snapshot_backend.add_argument("--migration-source", action="store_true")
    snapshot_backend.add_argument(
        "--migration-destination",
        action="store_true",
    )

    locking_scope = subparsers.add_parser("locking-scope")
    locking_scope.add_argument("--root", choices=root_choices, required=True)
    locking_scope.add_argument("--metadata", type=Path, required=True)
    locking_scope.add_argument("--project-dir", type=Path, required=True)
    locking_scope.add_argument(
        "--backend-role",
        choices=("production", "pending_destination"),
        required=True,
    )

    locking_contract_parser = subparsers.add_parser("locking-contract")
    locking_contract_parser.add_argument(
        "--project-dir",
        type=Path,
        required=True,
    )

    state = subparsers.add_parser("attest-state")
    state.add_argument("--root", choices=root_choices, required=True)

    empty_state = subparsers.add_parser("attest-empty-state")
    empty_state.add_argument("--root", choices=root_choices, required=True)

    sidecar = subparsers.add_parser("build-sidecar")
    sidecar.add_argument("--root", choices=root_choices, required=True)
    sidecar.add_argument("--plan-sha256", required=True)
    sidecar.add_argument("--source-commit", required=True)
    sidecar.add_argument("--backend-attestation", type=Path, required=True)
    sidecar.add_argument("--state-attestation", type=Path, required=True)
    sidecar.add_argument("--ui-attestation", type=Path, required=True)
    sidecar.add_argument("--variable-file-sha256")
    sidecar.add_argument(
        "--operation-mode",
        choices=("established", "initialize"),
        required=True,
    )

    verify = subparsers.add_parser("verify-sidecar")
    verify.add_argument("--root", choices=root_choices, required=True)
    verify.add_argument("--sidecar", type=Path, required=True)
    verify.add_argument("--plan-sha256", required=True)
    verify.add_argument("--source-commit", required=True)
    verify.add_argument("--backend-attestation", type=Path, required=True)
    verify.add_argument("--state-attestation", type=Path, required=True)
    verify.add_argument("--project-dir", type=Path, required=True)

    plan_signature = subparsers.add_parser("verify-plan-signature")
    plan_signature.add_argument("--sidecar", type=Path, required=True)
    plan_signature.add_argument("--project-dir", type=Path, required=True)

    post = subparsers.add_parser("verify-post-state")
    post.add_argument("--before", type=Path, required=True)
    post.add_argument("--after", type=Path, required=True)
    post.add_argument("--sidecar", type=Path, required=True)

    terraform_ui = subparsers.add_parser("attest-terraform-ui")
    terraform_ui.add_argument(
        "--operation",
        choices=("init", "plan", "apply"),
        required=True,
    )
    terraform_ui.add_argument("--events", type=Path, required=True)
    terraform_ui.add_argument("--stderr", type=Path, required=True)

    terraform_validate = subparsers.add_parser("verify-terraform-validate")
    terraform_validate.add_argument("--document", type=Path, required=True)
    terraform_validate.add_argument("--stderr", type=Path, required=True)

    empty_state_probe = subparsers.add_parser("verify-empty-state-probe")
    empty_state_probe.add_argument("--stderr", type=Path, required=True)

    durable_artifact = subparsers.add_parser("fsync-artifact")
    durable_artifact.add_argument("--path", type=Path, required=True)
    durable_artifact.add_argument("--directory", type=Path, required=True)

    state_checks = subparsers.add_parser("attest-state-checks")
    state_checks.add_argument("--root", choices=root_choices, required=True)
    state_checks.add_argument("--document", type=Path, required=True)

    post_checks = subparsers.add_parser("verify-post-apply-checks")
    post_checks.add_argument("--root", choices=root_choices, required=True)
    post_checks.add_argument("--document", type=Path, required=True)
    post_checks.add_argument("--sidecar", type=Path, required=True)

    validated_state = subparsers.add_parser("pass-validated-state")
    validated_state.add_argument(
        "--root",
        choices=root_choices,
        required=True,
    )
    validated_state.add_argument(
        "--validation",
        choices=("contract", "structural"),
        required=True,
    )
    attested_state = subparsers.add_parser("pass-attested-state")
    attested_state.add_argument(
        "--root",
        choices=root_choices,
        required=True,
    )
    attested_state.add_argument(
        "--attestation",
        type=Path,
        required=True,
    )
    attested_state.add_argument(
        "--stderr",
        type=Path,
        required=True,
    )
    recipient = subparsers.add_parser("attest-snapshot-recipient")
    recipient.add_argument("--recipient-file", type=Path, required=True)
    recipient.add_argument("--project-dir", type=Path, required=True)
    return argument_parser


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "attest-backend":
            metadata, _ = load_json_file(args.metadata, "backend metadata")
            document = attest_backend(
                args.root,
                metadata,
                args.workspace,
                args.project_dir,
                args.lock_proof,
            )
        elif args.command == "attest-migration-source":
            metadata, _ = load_json_file(args.metadata, "source backend metadata")
            document = attest_migration_source_backend(
                args.root,
                metadata,
                args.workspace,
                args.project_dir,
                args.lock_proof,
            )
        elif args.command == "attest-migration-destination":
            metadata, _ = load_json_file(
                args.metadata,
                "destination backend metadata",
            )
            document = attest_migration_destination_backend(
                args.root,
                metadata,
                args.workspace,
                args.project_dir,
                args.lock_proof,
            )
        elif args.command == "attest-snapshot-backend":
            metadata, _ = load_json_file(args.metadata, "snapshot backend metadata")
            if args.migration_source and args.migration_destination:
                raise TerraformSafetyError(
                    "snapshot backend cannot be both migration endpoints"
                )
            if args.migration_source:
                document = attest_migration_source_backend(
                    args.root,
                    metadata,
                    args.workspace,
                    args.project_dir,
                    None,
                    require_lock_proof=False,
                )
            elif args.migration_destination:
                document = attest_migration_destination_backend(
                    args.root,
                    metadata,
                    args.workspace,
                    args.project_dir,
                    None,
                    require_lock_proof=False,
                )
            else:
                document = attest_backend(
                    args.root,
                    metadata,
                    args.workspace,
                    args.project_dir,
                    None,
                    require_lock_proof=False,
                )
        elif args.command == "locking-scope":
            metadata, _ = load_json_file(args.metadata, "backend metadata")
            document = describe_locking_scope(
                metadata,
                args.root,
                args.project_dir,
                args.backend_role,
            )
        elif args.command == "locking-contract":
            document = locking_contract(args.project_dir)
        elif args.command == "attest-state":
            document = attest_state(
                args.root,
                load_json_bytes(sys.stdin.buffer.read(), "Terraform state"),
            )
        elif args.command == "attest-empty-state":
            document = attest_empty_state(args.root)
        elif args.command in {"build-sidecar", "verify-sidecar"}:
            backend, _ = load_json_file(
                args.backend_attestation,
                "backend attestation",
            )
            state, _ = load_json_file(
                args.state_attestation,
                "state attestation",
            )
            plan_bytes = sys.stdin.buffer.read()
            if args.command == "build-sidecar":
                ui_attestation, _ = load_json_file(
                    args.ui_attestation,
                    "Terraform plan UI attestation",
                )
                variable_sha = args.variable_file_sha256
                if variable_sha == "none":
                    variable_sha = None
                document = build_sidecar(
                    args.root,
                    plan_bytes,
                    args.plan_sha256,
                    args.source_commit,
                    backend,
                    state,
                    variable_sha,
                    args.operation_mode,
                    ui_attestation,
                )
            else:
                sidecar, sidecar_bytes = load_json_file(
                    args.sidecar,
                    "plan sidecar",
                )
                verify_sidecar(
                    args.root,
                    sidecar,
                    plan_bytes,
                    args.plan_sha256,
                    args.source_commit,
                    backend,
                    state,
                    sidecar_bytes,
                    Path(f"{args.sidecar}.sig"),
                    args.project_dir,
                )
                document = {"verified": True}
        elif args.command == "verify-plan-signature":
            _, sidecar_bytes = load_json_file(
                args.sidecar,
                "plan sidecar",
            )
            document = verify_plan_signature(
                sidecar_bytes,
                Path(f"{args.sidecar}.sig"),
                args.project_dir,
            )
        elif args.command == "verify-post-state":
            before, _ = load_json_file(args.before, "pre-apply state attestation")
            after, _ = load_json_file(args.after, "post-apply state attestation")
            sidecar, _ = load_json_file(args.sidecar, "plan sidecar")
            verify_post_state(before, after, sidecar)
            document = {"verified": True}
        elif args.command == "attest-terraform-ui":
            document = attest_terraform_ui_stream(
                args.operation,
                args.events,
                args.stderr,
            )
        elif args.command == "verify-terraform-validate":
            document = validate_terraform_validate_document(
                args.document,
                args.stderr,
            )
        elif args.command == "verify-empty-state-probe":
            document = validate_empty_state_probe(args.stderr)
        elif args.command == "fsync-artifact":
            document = fsync_artifact(args.path, args.directory)
        elif args.command == "attest-state-checks":
            state_document, _ = load_json_file(
                args.document,
                "Terraform state JSON",
            )
            document = validate_state_check_document(
                args.root,
                state_document,
            )
        elif args.command == "verify-post-apply-checks":
            state_document, _ = load_json_file(
                args.document,
                "post-apply Terraform state JSON",
            )
            sidecar, _ = load_json_file(args.sidecar, "plan sidecar")
            verify_post_apply_checks(
                args.root,
                state_document,
                sidecar,
            )
            document = {"verified": True}
        elif args.command == "pass-validated-state":
            state_bytes = sys.stdin.buffer.read(MAX_STATE_BYTES + 1)
            if len(state_bytes) > MAX_STATE_BYTES:
                raise TerraformSafetyError(
                    "Terraform state exceeds the snapshot safety limit"
                )
            state = load_json_bytes(state_bytes, "Terraform state")
            if args.validation == "contract":
                attest_state(args.root, state)
            else:
                validate_structural_state(state)
            sys.stdout.buffer.write(state_bytes)
            return 0
        elif args.command == "pass-attested-state":
            expected_attestation, _ = load_json_file(
                args.attestation,
                "confirmed Terraform state attestation",
            )
            state_bytes = sys.stdin.buffer.read(MAX_STATE_BYTES + 1)
            verified_state = verify_attested_state_bytes(
                args.root,
                state_bytes,
                expected_attestation,
                args.stderr,
            )
            sys.stdout.buffer.write(verified_state)
            return 0
        elif args.command == "attest-snapshot-recipient":
            document = attest_snapshot_recipient(
                args.recipient_file,
                args.project_dir,
            )
        else:  # pragma: no cover - argparse guarantees a known command.
            raise TerraformSafetyError("unknown command")
    except TerraformSafetyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    write_document(document, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
