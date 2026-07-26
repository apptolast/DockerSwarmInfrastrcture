#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import yaml


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_METADATA = Path("/opt/dockerswarm/DEPLOYED_VERSION.yml")


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        fail(f"cannot read {path}: {error}")


parser = argparse.ArgumentParser(
    description="Validate the source identity recorded by Ansible."
)
parser.add_argument(
    "--metadata",
    type=Path,
    default=DEFAULT_METADATA,
    help=f"installed metadata path (default: {DEFAULT_METADATA})",
)
parser.add_argument(
    "--allow-absent",
    action="store_true",
    help="allow missing metadata only during the initial platform bootstrap",
)
args = parser.parse_args()

if not args.metadata.exists():
    if args.allow_absent:
        print("Deployment metadata is absent and explicitly allowed for bootstrap.")
        raise SystemExit(0)
    fail(f"deployment metadata is absent: {args.metadata}")
if args.metadata.is_symlink() or not args.metadata.is_file():
    fail("deployment metadata must be a regular, non-symlink file")

metadata_stat = args.metadata.stat()
if stat.S_IMODE(metadata_stat.st_mode) != 0o644:
    fail("deployment metadata mode must be 0644")
if metadata_stat.st_uid != 0 or metadata_stat.st_gid != 0:
    fail("deployment metadata must be owned by root:root")

metadata = load_yaml(args.metadata)
if not isinstance(metadata, dict):
    fail("deployment metadata must contain a mapping")
playbook = metadata.pop("playbook", None)
if playbook not in {"platform", "host-baseline", "edge", "site"}:
    fail("deployment metadata contains an invalid playbook identity")

contract_path = PROJECT_DIR / "config/platform.yml"
contract = load_yaml(contract_path)
group_vars = load_yaml(PROJECT_DIR / "ansible/group_vars/all.yml")
if not isinstance(contract, dict) or not isinstance(group_vars, dict):
    fail("the source contract or Ansible group vars are invalid")

try:
    revision = subprocess.run(
        ["git", "-C", os.fspath(PROJECT_DIR), "rev-parse", "--verify", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
except (OSError, subprocess.CalledProcessError) as error:
    fail(f"cannot resolve the source commit: {error}")

contract_sha256 = hashlib.sha256(contract_path.read_bytes()).hexdigest()
expected = {
    "schema_version": 1,
    "repository": group_vars["deployment_metadata_repository_slug"],
    "release": contract["platform_release_version"],
    "revision": revision,
    "profile": "production",
    "contract_sha256": contract_sha256,
}
if metadata != expected:
    fail("installed deployment metadata differs from this production source")

print(
    "Deployment metadata matches release "
    f"{expected['release']} at commit {expected['revision']} "
    f"after playbook {playbook}."
)
