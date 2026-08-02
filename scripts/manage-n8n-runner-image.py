#!/usr/bin/env python3
"""Describe, build and verify the repository-native n8n runner image."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))
from host_global_operation_lock import ensure_mutation_lock  # noqa: E402

CONTEXT_SCHEMA = b"apptolast-n8n-runner-context-v1\0"
CONTEXT_FILES = (
    ".dockerignore",
    "Dockerfile",
    "package.json",
    "package-lock.json",
)
LOCAL_REPOSITORY = "apptolast/n8n-runners"
LABELS = {
    "com.apptolast.managed-by": "ansible",
    "com.apptolast.image-purpose": "n8n-task-runners",
}
MODULE_ROOT = Path("/opt/runners/task-runner-javascript/node_modules")
PYTHON_COMMAND = "/opt/runners/task-runner-python/.venv/bin/python"
PYTHON_HARDENING_ARGS = (
    "-I",
    "-B",
    "-X",
    "disable_remote_debug",
    "-m",
    "src.main",
)


class RunnerImageError(RuntimeError):
    """The local runner image cannot be reproduced or verified."""


class Docker:
    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(argv),
                check=check,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise RunnerImageError(f"cannot execute {argv[0]}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            raise RunnerImageError(
                f"Docker command failed ({' '.join(argv[:3])}): {detail}"
            ) from exc


def require_context(context: Path) -> None:
    if context.is_symlink() or not context.is_dir():
        raise RunnerImageError(f"runner context is absent or unsafe: {context}")
    observed = {entry.name for entry in context.iterdir()}
    if observed != set(CONTEXT_FILES):
        raise RunnerImageError("runner context file set differs from the contract")
    for name in CONTEXT_FILES:
        path = context / name
        if path.is_symlink() or not path.is_file():
            raise RunnerImageError(f"runner context entry is unsafe: {name}")


def context_sha256(context: Path) -> str:
    require_context(context)
    digest = hashlib.sha256(CONTEXT_SCHEMA)
    for name in CONTEXT_FILES:
        content = (context / name).read_bytes()
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def validate_source_reference(reference: str) -> None:
    if (
        re.fullmatch(
            r"ghcr\.io/apptolast/[a-z0-9._/-]+@sha256:[a-f0-9]{64}",
            reference,
        )
        is None
    ):
        raise RunnerImageError("catalog runner source reference is invalid")


def image_reference(context_digest: str) -> str:
    if re.fullmatch(r"[a-f0-9]{64}", context_digest) is None:
        raise RunnerImageError("runner context SHA-256 is invalid")
    return f"{LOCAL_REPOSITORY}:src-{context_digest}"


def load_dependencies(context: Path) -> dict[str, str]:
    try:
        package = json.loads((context / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerImageError("cannot load runner package.json") from exc
    dependencies = package.get("dependencies") if isinstance(package, dict) else None
    if (
        not isinstance(dependencies, dict)
        or not dependencies
        or any(
            not isinstance(name, str)
            or re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+", name) is None
            or not isinstance(version, str)
            or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None
            for name, version in dependencies.items()
        )
    ):
        raise RunnerImageError("runner dependencies must use exact versions")
    return dict(sorted(dependencies.items()))


def describe(context: Path, source_reference: str) -> dict[str, Any]:
    validate_source_reference(source_reference)
    digest = context_sha256(context)
    dependencies = load_dependencies(context)
    return {
        "schema_version": 1,
        "context_sha256": digest,
        "image_reference": image_reference(digest),
        "source_reference": source_reference,
        "dependencies": dependencies,
    }


def inspect_image(docker: Docker, reference: str) -> dict[str, Any] | None:
    result = docker.run(
        ["/usr/bin/docker", "image", "inspect", reference],
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RunnerImageError("Docker returned invalid image inspect JSON") from exc
    if not isinstance(document, list) or len(document) != 1:
        raise RunnerImageError("Docker image inspect result is ambiguous")
    image = document[0]
    if not isinstance(image, dict):
        raise RunnerImageError("Docker image inspect entry is invalid")
    return image


def module_probe_script(dependencies: dict[str, str]) -> str:
    expected = json.dumps(dependencies, sort_keys=True, separators=(",", ":"))
    root = os.fspath(MODULE_ROOT)
    return (
        "const fs=require('fs');"
        "const path=require('path');"
        "const {createRequire}=require('module');"
        f"const expected={expected};"
        f"const root={json.dumps(root)};"
        "const req=createRequire(path.join(root,'__apptolast_probe__.js'));"
        "const observed={};"
        "for(const [name,version] of Object.entries(expected)){"
        "const manifest=JSON.parse(fs.readFileSync("
        "path.join(root,name,'package.json'),'utf8'));"
        "if(manifest.version!==version)throw new Error("
        "`version mismatch for ${name}`);"
        "req.resolve(name);observed[name]=manifest.version;}"
        "process.stdout.write(JSON.stringify(observed));"
    )


def verify_modules(
    docker: Docker,
    reference: str,
    dependencies: dict[str, str],
) -> None:
    result = docker.run(
        [
            "/usr/bin/docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--user=runner",
            "--entrypoint=node",
            reference,
            "-e",
            module_probe_script(dependencies),
        ]
    )
    try:
        observed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RunnerImageError("runner module probe returned invalid JSON") from exc
    if observed != dependencies:
        raise RunnerImageError("runner module content differs from package.json")


def verify_python_hardening(docker: Docker, reference: str) -> None:
    probe = (
        "import json,sys;"
        "print(json.dumps({"
        "'isolated':sys.flags.isolated,"
        "'dont_write_bytecode':sys.dont_write_bytecode,"
        "'disable_remote_debug':sys._xoptions.get('disable_remote_debug')"
        "},sort_keys=True))"
    )
    result = docker.run(
        [
            "/usr/bin/docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--user=runner",
            f"--entrypoint={PYTHON_COMMAND}",
            reference,
            *PYTHON_HARDENING_ARGS[:-2],
            "-c",
            probe,
        ]
    )
    try:
        observed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RunnerImageError(
            "runner Python hardening probe returned invalid JSON"
        ) from exc
    if observed != {
        "isolated": 1,
        "dont_write_bytecode": True,
        "disable_remote_debug": True,
    }:
        raise RunnerImageError("runner Python isolation flags are not effective")


def verify_image(
    docker: Docker,
    image: dict[str, Any],
    metadata: dict[str, Any],
) -> str:
    image_id = image.get("Id")
    if (
        not isinstance(image_id, str)
        or re.fullmatch(r"sha256:[a-f0-9]{64}", image_id) is None
    ):
        raise RunnerImageError("runner image ID is invalid")
    if image.get("Architecture") != "amd64" or image.get("Os") != "linux":
        raise RunnerImageError("runner image must be linux/amd64")
    config = image.get("Config")
    if not isinstance(config, dict) or config.get("User") != "runner":
        raise RunnerImageError("runner image must retain the runner user")
    labels = config.get("Labels") or {}
    expected_labels = {
        **LABELS,
        "com.apptolast.context-sha256": metadata["context_sha256"],
        "com.apptolast.catalog-source-reference": metadata["source_reference"],
    }
    if not isinstance(labels, dict) or any(
        labels.get(key) != value for key, value in expected_labels.items()
    ):
        raise RunnerImageError("runner image labels differ from the source contract")
    verify_modules(
        docker,
        metadata["image_reference"],
        metadata["dependencies"],
    )
    verify_python_hardening(docker, metadata["image_reference"])
    return image_id


def reconcile(
    context: Path,
    source_reference: str,
    expected_context_sha256: str,
    docker: Docker | None = None,
) -> dict[str, Any]:
    metadata = describe(context, source_reference)
    if metadata["context_sha256"] != expected_context_sha256:
        raise RunnerImageError("installed runner context differs from controller")
    docker = docker or Docker()
    image = inspect_image(docker, metadata["image_reference"])
    changed = False
    if image is None:
        build_argv = [
            "/usr/bin/docker",
            "buildx",
            "build",
            "--load",
            "--pull",
            "--platform=linux/amd64",
            "--provenance=false",
            "--sbom=false",
            "--build-arg=SOURCE_DATE_EPOCH=0",
        ]
        build_labels = {
            **LABELS,
            "com.apptolast.context-sha256": metadata["context_sha256"],
            "com.apptolast.catalog-source-reference": metadata["source_reference"],
        }
        for key, value in sorted(build_labels.items()):
            build_argv.extend(["--label", f"{key}={value}"])
        build_argv.extend(
            [
                "--tag",
                metadata["image_reference"],
                os.fspath(context),
            ]
        )
        docker.run(build_argv)
        changed = True
        image = inspect_image(docker, metadata["image_reference"])
        if image is None:
            raise RunnerImageError("runner image is absent after a successful build")
    image_id = verify_image(docker, image, metadata)
    return {
        **metadata,
        "changed": changed,
        "image_id": image_id,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("describe", "reconcile"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--context", type=Path, required=True)
        subparser.add_argument("--source-reference", required=True)
        if command == "reconcile":
            subparser.add_argument("--expected-context-sha256", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "reconcile":
        ensure_mutation_lock(
            "n8n-runner-image-reconcile",
            [
                sys.executable,
                str(Path(__file__).resolve()),
                *sys.argv[1:],
            ],
            caller=Path(__file__),
        )
    try:
        if args.command == "describe":
            result = describe(args.context, args.source_reference)
        else:
            result = reconcile(
                args.context,
                args.source_reference,
                args.expected_context_sha256,
            )
    except RunnerImageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
