#!/usr/bin/env python3
"""Reject legacy or unapproved containers that can access restored databases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Any


RESTORE_COMPOSE_PROJECT = "apptolast-restore"
SWARM_SERVICE_LABEL = "com.docker.swarm.service.name"
SWARM_TASK_ID_LABEL = "com.docker.swarm.task.id"
SWARM_TASK_NAME_LABEL = "com.docker.swarm.task.name"
SWARM_STACK_LABEL = "com.docker.stack.namespace"
READ_ONLY_OBSERVER_TARGETS = {
    "observability_cadvisor": "/rootfs",
    "observability_node-exporter": "/host",
}


class ContainerGateError(RuntimeError):
    """A container can conflict with the reviewed Swarm database writers."""


def parse_mount_contract(raw_contract: str) -> dict[str, str]:
    try:
        document = json.loads(raw_contract)
    except json.JSONDecodeError as exc:
        raise ContainerGateError("mount contract is not valid JSON") from exc
    if not isinstance(document, dict) or len(document) != 3:
        raise ContainerGateError("mount contract must contain three databases")
    result: dict[str, str] = {}
    for source, service in document.items():
        if (
            not isinstance(source, str)
            or not source.startswith("/")
            or PurePosixPath(source) == PurePosixPath("/")
            or not isinstance(service, str)
            or not service.startswith("workloads_")
        ):
            raise ContainerGateError("mount contract contains an invalid entry")
        normalized = str(PurePosixPath(source))
        if normalized != source:
            raise ContainerGateError("mount contract paths must be normalized")
        result[source] = service
    return result


def paths_overlap(first: str, second: str) -> bool:
    first_path = PurePosixPath(first)
    second_path = PurePosixPath(second)
    return (
        first_path == second_path
        or first_path in second_path.parents
        or second_path in first_path.parents
    )


def validate_containers(
    containers: list[dict[str, Any]],
    mount_contract: dict[str, str],
) -> None:
    for container in containers:
        if not isinstance(container, dict):
            raise ContainerGateError("Docker inspect contains a non-object")
        container_id = container.get("Id")
        if not isinstance(container_id, str) or not container_id:
            raise ContainerGateError("Docker inspect entry has no container ID")
        labels = container.get("Config", {}).get("Labels") or {}
        if not isinstance(labels, dict):
            raise ContainerGateError(f"container {container_id} has invalid labels")
        if labels.get("com.docker.compose.project") == RESTORE_COMPOSE_PROJECT:
            raise ContainerGateError(
                f"restore Compose container still exists: {container_id}"
            )

        mounts = container.get("Mounts") or []
        if not isinstance(mounts, list):
            raise ContainerGateError(f"container {container_id} has invalid mounts")
        for mount in mounts:
            if not isinstance(mount, dict):
                raise ContainerGateError(
                    f"container {container_id} has an invalid mount"
                )
            source = mount.get("Source")
            if not isinstance(source, str) or not source.startswith("/"):
                continue
            protected = [
                (database_path, expected_service)
                for database_path, expected_service in mount_contract.items()
                if paths_overlap(source, database_path)
            ]
            if not protected:
                continue
            service_name = labels.get(SWARM_SERVICE_LABEL)
            task_id = labels.get(SWARM_TASK_ID_LABEL)
            task_name = labels.get(SWARM_TASK_NAME_LABEL)
            observer_target = READ_ONLY_OBSERVER_TARGETS.get(service_name)
            if observer_target is not None:
                if (
                    source == "/"
                    and mount.get("Destination") == observer_target
                    and mount.get("Type") == "bind"
                    and mount.get("RW") is False
                    and labels.get(SWARM_STACK_LABEL) == "observability"
                    and isinstance(task_id, str)
                    and task_id
                    and isinstance(task_name, str)
                    and task_name.startswith(f"{service_name}.")
                ):
                    continue
                raise ContainerGateError(
                    f"observer container {container_id} has an unsafe "
                    "database-overlapping mount"
                )
            if len(protected) != 1:
                raise ContainerGateError(
                    f"container {container_id} mount overlaps multiple databases"
                )
            database_path, expected_service = protected[0]
            if (
                source != database_path
                or mount.get("Destination") != "/var/lib/postgresql/data"
                or mount.get("Type") != "bind"
                or mount.get("RW") is not True
                or service_name != expected_service
                or not isinstance(task_id, str)
                or not task_id
                or not isinstance(task_name, str)
                or not task_name.startswith(f"{expected_service}.")
            ):
                raise ContainerGateError(
                    f"unapproved container {container_id} overlaps "
                    f"database path {database_path}"
                )


def load_inspect(path: Path | None) -> list[dict[str, Any]]:
    try:
        raw = sys.stdin.read() if path is None else path.read_text(encoding="utf-8")
        document = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContainerGateError("cannot read Docker inspect JSON") from exc
    if not isinstance(document, list):
        raise ContainerGateError("Docker inspect JSON must be a list")
    return document


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--inspect-json",
        type=Path,
        help="Docker container inspect JSON; stdin is used when omitted",
    )
    parser.add_argument("--mount-contract", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        validate_containers(
            load_inspect(args.inspect_json),
            parse_mount_contract(args.mount_contract),
        )
    except ContainerGateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("No restore or unapproved PostgreSQL writer containers remain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
