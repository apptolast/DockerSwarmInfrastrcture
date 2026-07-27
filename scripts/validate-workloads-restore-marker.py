#!/usr/bin/env python3
"""Validate the fail-closed marker required before workload activation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml

EXPECTED_DATABASES = {"n8n", "passbolt", "shlink"}
EXPECTED_DIGEST_KIND = "sha256-of-verified-recovery-manifest"
EXPECTED_SECRET_IDENTITY_KEY_FILE = "workloads_secret_identity_hmac_key"
EXPECTED_SECRET_IDENTITY_DIGEST_KIND = "sha256-of-random-hmac-key"
N8N_QUARANTINE_FILENAME = "n8n-workflows-quarantine.json"
EXPECTED_MARKER_KEYS = {
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
    "runtimeManifestSchemaVersion",
    "secretIdentityKeySha256",
    "secretIdentityKeyDigestKind",
}


class MarkerError(RuntimeError):
    """Restore evidence is absent, stale, or incomplete."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarkerError(f"cannot read restore marker: {path}") from exc
    if not isinstance(document, dict):
        raise MarkerError("restore marker is not a JSON object")
    return document


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise MarkerError(f"cannot read service catalog: {path}") from exc
    if not isinstance(document, dict):
        raise MarkerError("service catalog is not a mapping")
    return document


def sha256_file(path: Path, description: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise MarkerError(f"{description} is absent or unsafe: {path}")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise MarkerError(f"cannot hash {description}: {path}") from exc


def reject_workflow_quarantine(marker_path: Path) -> None:
    quarantine = marker_path.parent / N8N_QUARANTINE_FILENAME
    if quarantine.exists() or quarantine.is_symlink():
        raise MarkerError(
            "n8n workflow quarantine blocks workload deployment until "
            "reviewed recovery"
        )


def validate_marker(
    marker: dict[str, Any],
    service_catalog: dict[str, Any],
    *,
    catalog_sha256: str,
    runtime_manifest: dict[str, Any],
    runtime_manifest_sha256: str,
    recovery_manifest_sha256: str,
    secret_identity_key_sha256: str,
) -> None:
    if (
        runtime_manifest.get("schemaVersion") != 4
        or runtime_manifest.get("secretIdentityKeyFile")
        != EXPECTED_SECRET_IDENTITY_KEY_FILE
    ):
        raise MarkerError("runtime-manifest schemaVersion is stale")
    if set(marker) != EXPECTED_MARKER_KEYS:
        raise MarkerError("restore marker fields differ from schemaVersion 2")
    if marker.get("schemaVersion") != 2:
        raise MarkerError("unsupported restore marker schemaVersion")
    if marker.get("runtimeManifestSchemaVersion") != 4:
        raise MarkerError("restore marker runtime manifest schema is stale")
    completed_at = marker.get("completedAt")
    try:
        completed_timestamp = datetime.fromisoformat(completed_at)
    except (TypeError, ValueError) as exc:
        raise MarkerError("restore marker completedAt is invalid") from exc
    if (
        completed_timestamp.tzinfo is None
        or completed_timestamp.utcoffset() != timezone.utc.utcoffset(None)
    ):
        raise MarkerError("restore marker completedAt must be UTC")
    if marker.get("catalogVersion") != service_catalog.get("catalog_version"):
        raise MarkerError("restore marker catalogVersion is stale")
    expected_digests = {
        "catalogSha256": catalog_sha256,
        "runtimeManifestSha256": runtime_manifest_sha256,
        "sourceBackupSha256": recovery_manifest_sha256,
        "secretIdentityKeySha256": secret_identity_key_sha256,
    }
    for field, expected_digest in expected_digests.items():
        observed_digest = marker.get(field)
        if (
            not isinstance(observed_digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", observed_digest) is None
            or observed_digest != expected_digest
        ):
            raise MarkerError(f"{field} is stale or invalid")
    if marker.get("sourceBackupDigestKind") != EXPECTED_DIGEST_KIND:
        raise MarkerError("sourceBackupDigestKind is invalid")
    if (
        marker.get("secretIdentityKeyDigestKind")
        != EXPECTED_SECRET_IDENTITY_DIGEST_KIND
    ):
        raise MarkerError("secretIdentityKeyDigestKind is invalid")
    source_backup = runtime_manifest.get("sourceBackup")
    if (
        not isinstance(source_backup, str)
        or not source_backup.startswith("apptolast-data-")
        or marker.get("sourceBackup") != source_backup
    ):
        raise MarkerError("sourceBackup differs from runtime-manifest.json")

    approved_services = service_catalog.get("approved_services")
    if not isinstance(approved_services, list):
        raise MarkerError("service catalog approved_services is invalid")
    expected_services = {
        item["id"] for item in approved_services if item["id"] != "traefik-edge"
    }
    observed_services = marker.get("approvedServices")
    if (
        not isinstance(observed_services, list)
        or len(observed_services) != len(expected_services)
        or set(observed_services) != expected_services
    ):
        raise MarkerError("approvedServices differs from migration scope")

    if marker.get("openClawLegacyImported") is not False:
        raise MarkerError("legacy OpenClaw state is forbidden")

    catalog_datasets = service_catalog.get("datasets")
    if not isinstance(catalog_datasets, list):
        raise MarkerError("service catalog datasets is invalid")
    expected_dataset_states = {
        item["id"]: (
            "initialized-empty"
            if item["migration"] == "initialize-empty"
            else "restored-and-verified"
        )
        for item in catalog_datasets
        if item["owner"] != "traefik-edge"
    }
    if marker.get("datasets") != expected_dataset_states:
        raise MarkerError("dataset verification evidence is incomplete")

    expected_database_states = {name: "verified" for name in sorted(EXPECTED_DATABASES)}
    if marker.get("databaseRestores") != expected_database_states:
        raise MarkerError("database restore evidence is incomplete")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--marker", type=Path, required=True)
    parser.add_argument("--services", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--recovery-manifest", type=Path, required=True)
    parser.add_argument("--secret-identity-key", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        reject_workflow_quarantine(args.marker)
        service_catalog = load_yaml(args.services)
        runtime_manifest = load_json(args.runtime_manifest)
        validate_marker(
            load_json(args.marker),
            service_catalog,
            catalog_sha256=sha256_file(args.services, "service catalog"),
            runtime_manifest=runtime_manifest,
            runtime_manifest_sha256=sha256_file(
                args.runtime_manifest,
                "runtime manifest",
            ),
            recovery_manifest_sha256=sha256_file(
                args.recovery_manifest,
                "recovery manifest",
            ),
            secret_identity_key_sha256=sha256_file(
                args.secret_identity_key,
                "secret identity key",
            ),
        )
    except MarkerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("Workloads restore completion marker passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
