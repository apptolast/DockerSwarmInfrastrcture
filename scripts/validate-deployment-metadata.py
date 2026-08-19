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
DEFAULT_COMPONENT_DIRECTORY = Path("/opt/dockerswarm/deployments")
PLAYBOOK_COMPONENTS = {
    "platform": ["host-security", "platform"],
    "host-baseline": ["host-security", "host-baseline"],
    "preflight-images": ["image-preflight", "runner-image"],
    "edge": ["image-preflight", "runner-image", "edge"],
    "workloads": [
        "minecraft-preflight",
        "image-preflight",
        "runner-image",
        "workloads",
    ],
    "observability": [
        "image-preflight",
        "runner-image",
        "observability",
    ],
    "backup": ["backup"],
    "site": [
        "host-security",
        "platform",
        "host-baseline",
        "minecraft-preflight",
        "image-preflight",
        "runner-image",
        "edge",
        "workloads",
        "observability",
    ],
}
ALLOWED_COMPONENTS = {
    component for components in PLAYBOOK_COMPONENTS.values() for component in components
}
REQUIRED_SCOPES = {
    "invocation": None,
    "site": PLAYBOOK_COMPONENTS["site"],
    "site-with-backup": [*PLAYBOOK_COMPONENTS["site"], "backup"],
}


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
    "--component-directory",
    type=Path,
    default=DEFAULT_COMPONENT_DIRECTORY,
    help=(
        "component metadata directory "
        f"(default: {DEFAULT_COMPONENT_DIRECTORY})"
    ),
)
parser.add_argument(
    "--allow-absent",
    action="store_true",
    help="allow missing metadata only during the initial platform bootstrap",
)
parser.add_argument(
    "--required-scope",
    choices=sorted(REQUIRED_SCOPES),
    default="invocation",
    help=(
        "components which must all match this source revision "
        "(default: invocation)"
    ),
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
playbook = metadata.get("playbook")
if playbook not in PLAYBOOK_COMPONENTS:
    fail("deployment metadata contains an invalid playbook identity")

contract_paths = [
    PROJECT_DIR / "config/capacity.yml",
    PROJECT_DIR / "config/host-security.yml",
    PROJECT_DIR / "config/minecraft.yml",
    PROJECT_DIR / "config/platform.yml",
    PROJECT_DIR / "config/services.yml",
    PROJECT_DIR / "config/workload-image-updates.yml",
    PROJECT_DIR / "stacks/observability/secrets.yml",
    PROJECT_DIR / "stacks/workloads/secrets.yml",
]
contract = load_yaml(PROJECT_DIR / "config/platform.yml")
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

contract_hashes = "\n".join(
    hashlib.sha256(path.read_bytes()).hexdigest() for path in contract_paths
)
contract_sha256 = hashlib.sha256(
    f"{contract_hashes}\n".encode("ascii")
).hexdigest()
components = PLAYBOOK_COMPONENTS[playbook]
expected = {
    "schema_version": 2,
    "semantics": "components-updated-by-last-invocation",
    "repository": group_vars["deployment_metadata_repository_slug"],
    "release": contract["platform_release_version"],
    "revision": revision,
    "profile": "production",
    "playbook": playbook,
    "contract_sha256": contract_sha256,
    "components": components,
}
if metadata != expected:
    fail("installed deployment metadata differs from this production source")

component_directory = args.component_directory
if (
    component_directory.is_symlink()
    or not component_directory.is_dir()
    or stat.S_IMODE(component_directory.stat().st_mode) != 0o755
    or component_directory.stat().st_uid != 0
    or component_directory.stat().st_gid != 0
):
    fail("component metadata directory must be root:root 0755")
component_files = sorted(component_directory.glob("*.yml"))
if not component_files:
    fail("component deployment metadata is absent")
observed_components: dict[str, dict[str, Any]] = {}
for path in component_files:
    if (
        path.is_symlink()
        or not path.is_file()
        or stat.S_IMODE(path.stat().st_mode) != 0o644
        or path.stat().st_uid != 0
        or path.stat().st_gid != 0
        or path.stem not in ALLOWED_COMPONENTS
        or path.stem in observed_components
    ):
        fail(f"component metadata file is unsafe: {path}")
    value = load_yaml(path)
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "component",
            "repository",
            "release",
            "revision",
            "profile",
            "applied_by_playbook",
            "contract_sha256",
        }
        or value["schema_version"] != 1
        or value["component"] != path.stem
        or value["repository"] != expected["repository"]
        or value["profile"] not in {"production", "acme-staging"}
        or value["applied_by_playbook"] not in PLAYBOOK_COMPONENTS
        or path.stem
        not in PLAYBOOK_COMPONENTS[value["applied_by_playbook"]]
        or not isinstance(value["revision"], str)
        or len(value["revision"]) != 40
        or any(character not in "0123456789abcdef" for character in value["revision"])
        or not isinstance(value["contract_sha256"], str)
        or len(value["contract_sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in value["contract_sha256"]
        )
    ):
        fail(f"component metadata is malformed: {path}")
    observed_components[path.stem] = value

required_components = REQUIRED_SCOPES[args.required_scope] or components
for component in required_components:
    value = observed_components.get(component)
    if value is None:
        fail(f"required component metadata is absent: {component}")
    expected_component = {
        "schema_version": 1,
        "component": component,
        "repository": expected["repository"],
        "release": expected["release"],
        "revision": expected["revision"],
        "profile": expected["profile"],
        "applied_by_playbook": (
            playbook
            if args.required_scope == "invocation"
            else value["applied_by_playbook"]
        ),
        "contract_sha256": expected["contract_sha256"],
    }
    if value != expected_component:
        fail(f"required component metadata differs: {component}")

print(
    "Deployment metadata matches release "
    f"{expected['release']} at commit {expected['revision']} "
    f"for {', '.join(required_components)} "
    f"(scope {args.required_scope}) after playbook {playbook}."
)
