#!/usr/bin/env python3
"""Install versioned external Docker Secrets without exposing their values."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any

import yaml

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))
from host_global_operation_lock import ensure_mutation_lock  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = REPOSITORY_ROOT / "stacks/workloads/secrets.yml"
DEFAULT_SOURCE_DIR = Path("/srv/dockerswarm/services/secrets/files")
DEFAULT_RUNTIME_MANIFEST = Path("/srv/dockerswarm/services/runtime-manifest.json")
OPTIONAL_UNSET_SENTINEL = b"__APPTOLAST_OPTIONAL_UNSET_V1__\n"
MAX_SECRET_SIZE = 500 * 1024
SECRET_IDENTITY_KEY_FILE = "workloads_secret_identity_hmac_key"
SECRET_IDENTITY_LABEL = "com.apptolast.secret-source-hmac-sha256"
SECRET_IDENTITY_DOMAIN = b"apptolast-workload-secret-source-v1\x00"
MIN_IDENTITY_KEY_SIZE = 32
MAX_IDENTITY_KEY_SIZE = 1024


class SecretInstallError(RuntimeError):
    """A secret source or existing Docker Secret violates the contract."""


def load_mapping(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SecretInstallError(f"cannot read YAML contract: {path}") from exc
    if not isinstance(document, dict):
        raise SecretInstallError(f"YAML contract is not a mapping: {path}")
    return document


def load_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecretInstallError(f"cannot read JSON manifest: {path}") from exc
    if not isinstance(document, dict):
        raise SecretInstallError(f"JSON manifest is not a mapping: {path}")
    return document


def validate_private_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise SecretInstallError(f"secret source directory is unsafe: {path}")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SecretInstallError(
            f"secret source directory must deny group/other access: {path}"
        )


def read_secret_source(source_dir: Path, entry: dict[str, Any]) -> bytes:
    source_file = entry.get("source_file")
    if (
        not isinstance(source_file, str)
        or not source_file
        or "/" in source_file
        or source_file in {".", ".."}
    ):
        raise SecretInstallError("invalid source_file in secret catalog")
    source_path = source_dir / source_file
    if source_path.is_symlink() or not source_path.is_file():
        raise SecretInstallError(f"secret source is absent or unsafe: {source_file}")
    metadata = source_path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise SecretInstallError(f"secret source must be mode 0600: {source_file}")
    data = source_path.read_bytes()
    if len(data) >= MAX_SECRET_SIZE:
        raise SecretInstallError(f"secret source is too large: {source_file}")
    required_nonempty = entry.get("required_nonempty")
    if not isinstance(required_nonempty, bool):
        raise SecretInstallError(f"required_nonempty must be boolean: {source_file}")
    if data == OPTIONAL_UNSET_SENTINEL:
        raise SecretInstallError(
            f"secret source uses the reserved optional sentinel: {source_file}"
        )
    if required_nonempty and not data.strip():
        raise SecretInstallError(f"required secret source is empty: {source_file}")
    if not data:
        if required_nonempty:
            raise SecretInstallError(f"required secret source is empty: {source_file}")
        return OPTIONAL_UNSET_SENTINEL
    return data


def read_identity_key(source_dir: Path, runtime_manifest: dict[str, Any]) -> bytes:
    if runtime_manifest.get("secretIdentityKeyFile") != SECRET_IDENTITY_KEY_FILE:
        raise SecretInstallError(
            "runtime-manifest.json secretIdentityKeyFile differs from contract"
        )
    path = source_dir / SECRET_IDENTITY_KEY_FILE
    if path.is_symlink() or not path.is_file():
        raise SecretInstallError("secret identity HMAC key is absent or unsafe")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise SecretInstallError("secret identity HMAC key must be mode 0600")
    data = path.read_bytes()
    if not MIN_IDENTITY_KEY_SIZE <= len(data) <= MAX_IDENTITY_KEY_SIZE:
        raise SecretInstallError("secret identity HMAC key size is invalid")
    return data


def source_identity(
    identity_key: bytes,
    entry: dict[str, Any],
    version: str,
    secret_data: bytes,
) -> str:
    """Return a keyed identity safe to expose in Docker metadata.

    A raw hash would permit offline guessing of low-entropy configuration
    values. The retained random HMAC key makes the public label useful for
    exact reconciliation without disclosing a reusable verifier.
    """

    payload = (
        SECRET_IDENTITY_DOMAIN
        + entry["key"].encode("utf-8")
        + b"\x00"
        + version.encode("ascii")
        + b"\x00"
        + secret_data
    )
    return hmac.new(identity_key, payload, hashlib.sha256).hexdigest()


def inspect_secret(name: str) -> dict[str, Any] | None:
    completed = subprocess.run(
        ["/usr/bin/docker", "secret", "inspect", name],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if completed.returncode != 0:
        return None
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SecretInstallError("Docker returned invalid secret metadata") from exc
    if not isinstance(document, list) or len(document) != 1:
        raise SecretInstallError("Docker returned ambiguous secret metadata")
    secret = document[0]
    if not isinstance(secret, dict):
        raise SecretInstallError("Docker returned invalid secret metadata")
    return secret


def validate_secret_metadata(
    secret: dict[str, Any],
    entry: dict[str, Any],
    version: str,
    expected_source_identity: str,
) -> None:
    spec = secret.get("Spec")
    if not isinstance(spec, dict):
        raise SecretInstallError("Docker Secret has no Spec")
    labels = spec.get("Labels")
    if not isinstance(labels, dict):
        raise SecretInstallError("Docker Secret has no contract labels")
    expected = {
        "com.apptolast.managed-by": "workload-secret-installer",
        "com.apptolast.secret-key": entry["key"],
        "com.apptolast.secret-version": version,
        SECRET_IDENTITY_LABEL: expected_source_identity,
    }
    if spec.get("Name") != entry["external_name"] or any(
        labels.get(key) != value for key, value in expected.items()
    ):
        raise SecretInstallError(f"Docker Secret metadata differs for {entry['key']}")


def create_secret(
    entry: dict[str, Any],
    version: str,
    secret_data: bytes,
    expected_source_identity: str,
) -> None:
    command = [
        "/usr/bin/docker",
        "secret",
        "create",
        "--label",
        "com.apptolast.managed-by=workload-secret-installer",
        "--label",
        f"com.apptolast.secret-key={entry['key']}",
        "--label",
        f"com.apptolast.secret-version={version}",
        "--label",
        f"{SECRET_IDENTITY_LABEL}={expected_source_identity}",
        entry["external_name"],
        "-",
    ]
    completed = subprocess.run(
        command,
        check=False,
        input=secret_data,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise SecretInstallError(f"Docker Secret creation failed for {entry['key']}")


def validate_contract(
    catalog: dict[str, Any], runtime_manifest: dict[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    if runtime_manifest.get("schemaVersion") != 4:
        raise SecretInstallError("unsupported runtime-manifest.json schemaVersion")
    version = catalog.get("workloads_secret_version")
    entries = catalog.get("workloads_secrets")
    if not isinstance(version, str) or not version.startswith("v"):
        raise SecretInstallError("invalid workloads_secret_version")
    if not isinstance(entries, list) or not entries:
        raise SecretInstallError("workloads_secrets is empty or invalid")
    for entry in entries:
        if not isinstance(entry, dict):
            raise SecretInstallError("invalid secret catalog entry")
        for key in ("key", "external_name", "source_file", "consumers"):
            if key not in entry:
                raise SecretInstallError(f"secret catalog entry lacks {key}")
        external_name = entry["external_name"]
        if not isinstance(external_name, str) or len(external_name) > 64:
            raise SecretInstallError("invalid or overlong Docker Secret name")
        if not external_name.endswith(f"-{version}"):
            raise SecretInstallError(f"Docker Secret is not versioned: {entry['key']}")
    source_files = [entry["source_file"] for entry in entries]
    secret_keys = [entry["key"] for entry in entries]
    external_names = [entry["external_name"] for entry in entries]
    if any(
        len(values) != len(set(values))
        for values in (source_files, secret_keys, external_names)
    ):
        raise SecretInstallError("secret catalog contains duplicate identities")
    manifest_files = runtime_manifest.get("secretFiles")
    if (
        not isinstance(manifest_files, list)
        or set(manifest_files) != set(source_files)
        or len(manifest_files) != len(source_files)
    ):
        raise SecretInstallError(
            "runtime-manifest.json secretFiles differs from secret catalog"
        )
    if runtime_manifest.get("openClawLegacyImported") is not False:
        raise SecretInstallError("runtime manifest does not exclude legacy OpenClaw")
    if runtime_manifest.get("secretIdentityKeyFile") != SECRET_IDENTITY_KEY_FILE:
        raise SecretInstallError(
            "runtime-manifest.json secretIdentityKeyFile differs from contract"
        )
    return version, entries


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument(
        "--runtime-manifest", type=Path, default=DEFAULT_RUNTIME_MANIFEST
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="validate source files and existing Docker Secrets without creating",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.verify_only:
        ensure_mutation_lock(
            "workload-secret-install",
            [
                sys.executable,
                str(Path(__file__).resolve()),
                *sys.argv[1:],
            ],
            caller=Path(__file__),
        )
    try:
        if os.geteuid() == 0:
            os.umask(0o077)
        catalog = load_mapping(args.catalog)
        runtime_manifest = load_json(args.runtime_manifest)
        version, entries = validate_contract(catalog, runtime_manifest)
        validate_private_directory(args.source_dir)
        identity_key = read_identity_key(args.source_dir, runtime_manifest)
        sources = {
            entry["key"]: read_secret_source(args.source_dir, entry)
            for entry in entries
        }
        source_identities = {
            entry["key"]: source_identity(
                identity_key,
                entry,
                version,
                sources[entry["key"]],
            )
            for entry in entries
        }
        created: list[str] = []
        for entry in entries:
            existing = inspect_secret(entry["external_name"])
            if existing is None:
                if args.verify_only:
                    raise SecretInstallError(f"Docker Secret is absent: {entry['key']}")
                create_secret(
                    entry,
                    version,
                    sources[entry["key"]],
                    source_identities[entry["key"]],
                )
                created.append(entry["key"])
                existing = inspect_secret(entry["external_name"])
                if existing is None:
                    raise SecretInstallError(
                        f"Docker Secret disappeared after create: {entry['key']}"
                    )
            validate_secret_metadata(
                existing,
                entry,
                version,
                source_identities[entry["key"]],
            )
    except SecretInstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    action = "verified" if args.verify_only else "reconciled"
    print(
        f"Workload Docker Secrets {action}: "
        f"{len(entries)} total, {len(created)} created."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
