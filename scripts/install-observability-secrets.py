#!/usr/bin/env python3
"""Generate and install versioned observability secrets without logging values."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
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
DEFAULT_CATALOG = REPOSITORY_ROOT / "stacks/observability/secrets.yml"
DEFAULT_SOURCE_DIR = Path("/srv/dockerswarm/observability/secrets/files")
SECRET_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}[a-z0-9]")
SECRET_KEY_RE = re.compile(r"[a-z][a-z0-9_]{1,62}[a-z0-9]")
VERSION_RE = re.compile(r"v[1-9][0-9]*")
SHA256_RE = re.compile(r"[a-f0-9]{64}")
MAX_SECRET_SIZE = 500 * 1024


class SecretInstallError(RuntimeError):
    """A source or Docker Secret violates the immutable contract."""


def load_mapping(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SecretInstallError(f"cannot read YAML contract: {path}") from exc
    if not isinstance(document, dict):
        raise SecretInstallError(f"YAML contract is not a mapping: {path}")
    return document


def validate_contract(
    catalog: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    if set(catalog) != {
        "observability_secret_catalog_version",
        "observability_secret_version",
        "observability_secrets",
    }:
        raise SecretInstallError("unexpected observability secret schema")
    if catalog["observability_secret_catalog_version"] != 1:
        raise SecretInstallError("unsupported observability secret catalog")
    version = catalog["observability_secret_version"]
    entries = catalog["observability_secrets"]
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise SecretInstallError("invalid observability_secret_version")
    if not isinstance(entries, list) or len(entries) != 5:
        raise SecretInstallError("the secret catalog must contain five entries")

    keys: list[str] = []
    external_names: list[str] = []
    source_files: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "key",
            "external_name",
            "source_file",
            "generated_bytes",
            "consumers",
        }:
            raise SecretInstallError("invalid secret catalog entry schema")
        key = entry["key"]
        name = entry["external_name"]
        source = entry["source_file"]
        generated_bytes = entry["generated_bytes"]
        consumers = entry["consumers"]
        if not isinstance(key, str) or not SECRET_KEY_RE.fullmatch(key):
            raise SecretInstallError("invalid observability secret key")
        if (
            not isinstance(name, str)
            or not SECRET_NAME_RE.fullmatch(name)
            or not name.endswith(f"-{version}")
        ):
            raise SecretInstallError(f"invalid versioned secret name: {key}")
        if (
            not isinstance(source, str)
            or source != key
            or "/" in source
            or source in {".", ".."}
        ):
            raise SecretInstallError(f"invalid source file for {key}")
        if type(generated_bytes) is not int or not 32 <= generated_bytes <= 64:
            raise SecretInstallError(f"unsafe entropy size for {key}")
        if (
            not isinstance(consumers, list)
            or not consumers
            or any(not isinstance(item, str) or not item for item in consumers)
            or len(consumers) != len(set(consumers))
        ):
            raise SecretInstallError(f"invalid consumers for {key}")
        keys.append(key)
        external_names.append(name)
        source_files.append(source)

    if any(
        len(values) != len(set(values))
        for values in (keys, external_names, source_files)
    ):
        raise SecretInstallError("duplicate observability secret identity")
    return version, entries


def ensure_private_directory(path: Path, create: bool) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise SecretInstallError(f"secret source directory is unsafe: {path}")
    elif create:
        try:
            path.mkdir(mode=0o700, parents=True)
        except OSError as exc:
            raise SecretInstallError(
                f"cannot create secret source directory: {path}"
            ) from exc
    else:
        raise SecretInstallError(f"secret source directory is absent: {path}")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise SecretInstallError(f"secret source directory must be mode 0700: {path}")


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
        raise SecretInstallError("Docker returned invalid metadata") from exc
    if not isinstance(document, list) or len(document) != 1:
        raise SecretInstallError("Docker returned ambiguous secret metadata")
    secret = document[0]
    if not isinstance(secret, dict):
        raise SecretInstallError("Docker returned invalid secret metadata")
    return secret


def atomic_generate_source(path: Path, entropy_bytes: int) -> bytes:
    value = (secrets.token_urlsafe(entropy_bytes) + "\n").encode("ascii")
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        raise SecretInstallError(
            f"cannot create secret source atomically: {path.name}"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return value


def read_source(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SecretInstallError(f"secret source is unsafe: {path.name}")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise SecretInstallError(f"secret source must be mode 0600: {path.name}")
    data = path.read_bytes()
    if not data.rstrip(b"\n"):
        raise SecretInstallError(f"secret source is empty: {path.name}")
    if len(data) >= MAX_SECRET_SIZE:
        raise SecretInstallError(f"secret source is too large: {path.name}")
    return data


def validate_secret_metadata(
    secret: dict[str, Any],
    entry: dict[str, Any],
    version: str,
    source_sha256: str,
) -> None:
    spec = secret.get("Spec")
    labels = spec.get("Labels") if isinstance(spec, dict) else None
    if not isinstance(spec, dict) or not isinstance(labels, dict):
        raise SecretInstallError("Docker Secret has no contract labels")
    expected = {
        "com.apptolast.managed-by": "observability-secret-installer",
        "com.apptolast.secret-key": entry["key"],
        "com.apptolast.secret-version": version,
        "com.apptolast.source-sha256": source_sha256,
    }
    if spec.get("Name") != entry["external_name"] or any(
        labels.get(key) != value for key, value in expected.items()
    ):
        raise SecretInstallError(f"Docker Secret metadata differs for {entry['key']}")


def create_secret(
    entry: dict[str, Any],
    version: str,
    source_sha256: str,
    data: bytes,
) -> None:
    if not SHA256_RE.fullmatch(source_sha256):
        raise SecretInstallError("invalid source fingerprint")
    command = [
        "/usr/bin/docker",
        "secret",
        "create",
        "--label",
        "com.apptolast.managed-by=observability-secret-installer",
        "--label",
        f"com.apptolast.secret-key={entry['key']}",
        "--label",
        f"com.apptolast.secret-version={version}",
        "--label",
        f"com.apptolast.source-sha256={source_sha256}",
        entry["external_name"],
        "-",
    ]
    completed = subprocess.run(
        command,
        check=False,
        input=data,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise SecretInstallError(f"Docker Secret creation failed for {entry['key']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="validate sources and existing Docker Secrets without creating",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.verify_only:
        ensure_mutation_lock(
            "observability-secret-install",
            [
                sys.executable,
                str(Path(__file__).resolve()),
                *sys.argv[1:],
            ],
            caller=Path(__file__),
        )
    os.umask(0o077)
    try:
        catalog = load_mapping(args.catalog)
        version, entries = validate_contract(catalog)
        ensure_private_directory(args.source_dir, create=not args.verify_only)
        created_sources: list[str] = []
        created_secrets: list[str] = []
        for entry in entries:
            existing = inspect_secret(entry["external_name"])
            source_path = args.source_dir / entry["source_file"]
            if not source_path.exists():
                if existing is not None:
                    raise SecretInstallError(
                        f"source is absent for existing secret: {entry['key']}"
                    )
                if args.verify_only:
                    raise SecretInstallError(f"secret source is absent: {entry['key']}")
                atomic_generate_source(source_path, entry["generated_bytes"])
                created_sources.append(entry["key"])
            data = read_source(source_path)
            source_sha256 = hashlib.sha256(data).hexdigest()
            if existing is None:
                if args.verify_only:
                    raise SecretInstallError(f"Docker Secret is absent: {entry['key']}")
                create_secret(entry, version, source_sha256, data)
                created_secrets.append(entry["key"])
                existing = inspect_secret(entry["external_name"])
                if existing is None:
                    raise SecretInstallError(
                        f"Docker Secret disappeared after create: {entry['key']}"
                    )
            validate_secret_metadata(
                existing,
                entry,
                version,
                source_sha256,
            )
    except (OSError, SecretInstallError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    action = "verified" if args.verify_only else "reconciled"
    print(
        f"Observability secrets {action}: {len(entries)} total, "
        f"{len(created_sources)} sources and "
        f"{len(created_secrets)} Docker Secrets created."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
