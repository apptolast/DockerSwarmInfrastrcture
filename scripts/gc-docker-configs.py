#!/usr/bin/env python3
"""Plan or remove old, unreferenced, immutable Docker Swarm Configs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Sequence

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))
from host_global_operation_lock import ensure_mutation_lock  # noqa: E402

MANAGED_BY_LABEL = "com.apptolast.managed-by"
STACK_LABEL = "com.apptolast.stack"
KIND_LABEL = "com.apptolast.kind"
SHA256_LABEL = "com.apptolast.sha256"
MANAGED_BY_VALUE = "ansible"
SUPPORTED_STACK_KINDS = {
    "edge": {"dynamic", "static"},
    "workloads": {"application-config", "entrypoint"},
    "observability": {
        "alert-rules",
        "application-config",
        "grafana-dashboard",
        "grafana-provisioning",
    },
}
DEFAULT_RETENTION = 2
MAX_RETENTION = 100
CONFIRMATION_PREFIX = "DELETE_DOCKER_CONFIGS"
CONFIG_NAME_RE = re.compile(r"(?P<series>[a-z][a-z0-9-]*)-(?P<digest>[a-f0-9]{16})")
SHA256_RE = re.compile(r"[a-f0-9]{64}")
RFC3339_UTC_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T" r"[0-9]{2}:[0-9]{2}:[0-9]{2}" r"(?:\.[0-9]{1,9})?Z"
)


class ConfigGcError(RuntimeError):
    """The immutable Config inventory is unsafe or cannot be reconciled."""


def require_mapping(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigGcError(f"{description} must be a mapping")
    return value


def require_list(value: Any, description: str) -> list[Any]:
    if not isinstance(value, list):
        raise ConfigGcError(f"{description} must be a list")
    return value


def require_string(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigGcError(f"{description} must be a non-empty string")
    return value


def canonical_json(document: Any) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def normalize_created_at(value: Any) -> str:
    created_at = require_string(value, "Docker Config creation time")
    if RFC3339_UTC_RE.fullmatch(created_at) is None:
        raise ConfigGcError("Docker Config creation time is not canonical UTC")
    timestamp_and_fraction = created_at[:-1]
    if "." in timestamp_and_fraction:
        timestamp, suffix = timestamp_and_fraction.split(".", 1)
    else:
        timestamp, suffix = timestamp_and_fraction, ""
    return f"{timestamp}.{suffix.ljust(9, '0')}Z"


def normalize_config(
    raw_config: Any,
    selected_stacks: set[str],
) -> dict[str, Any] | None:
    config = require_mapping(raw_config, "Docker Config")
    config_id = require_string(config.get("ID"), "Docker Config ID")
    created_at = normalize_created_at(config.get("CreatedAt"))
    spec = require_mapping(config.get("Spec"), f"Docker Config {config_id} Spec")
    name = require_string(spec.get("Name"), f"Docker Config {config_id} name")
    labels = require_mapping(
        spec.get("Labels"),
        f"Docker Config {name} labels",
    )
    if labels.get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE:
        raise ConfigGcError(
            f"Docker Config {name} escaped the managed-by inventory filter"
        )
    stack = labels.get(STACK_LABEL)
    if stack not in selected_stacks:
        return None
    if stack not in SUPPORTED_STACK_KINDS:
        raise ConfigGcError(f"Docker Config {name} has an unsupported owner")
    kind = labels.get(KIND_LABEL)
    if kind not in SUPPORTED_STACK_KINDS[stack]:
        raise ConfigGcError(
            f"Docker Config {name} has an unsupported kind for owner {stack}"
        )
    source_sha256 = labels.get(SHA256_LABEL)
    if not isinstance(source_sha256, str) or SHA256_RE.fullmatch(source_sha256) is None:
        raise ConfigGcError(f"Docker Config {name} has an invalid source digest")
    name_match = CONFIG_NAME_RE.fullmatch(name)
    if (
        name_match is None
        or not name.startswith(f"{stack}-")
        or source_sha256[:16] != name_match.group("digest")
    ):
        raise ConfigGcError(
            f"Docker Config {name} name, owner and digest are inconsistent"
        )
    return {
        "id": config_id,
        "name": name,
        "stack": stack,
        "kind": kind,
        "series": name_match.group("series"),
        "sourceSha256": source_sha256,
        "createdAt": created_at,
    }


def referenced_config_ids(raw_services: Any) -> set[str]:
    references: set[str] = set()
    for raw_service in require_list(raw_services, "Docker service inventory"):
        service = require_mapping(raw_service, "Docker service")
        service_spec = require_mapping(service.get("Spec"), "Docker service Spec")
        service_name = require_string(service_spec.get("Name"), "Docker service name")
        specs: list[tuple[str, Any]] = [("current", service_spec)]
        previous_spec = service.get("PreviousSpec")
        if previous_spec is not None:
            specs.append(
                (
                    "previous",
                    require_mapping(
                        previous_spec,
                        f"Docker service {service_name} PreviousSpec",
                    ),
                )
            )
        for generation, raw_spec in specs:
            task_template = require_mapping(
                require_mapping(
                    raw_spec,
                    f"Docker service {service_name} {generation} Spec",
                ).get("TaskTemplate"),
                f"Docker service {service_name} {generation} TaskTemplate",
            )
            container_spec = require_mapping(
                task_template.get("ContainerSpec"),
                f"Docker service {service_name} {generation} ContainerSpec",
            )
            raw_references = container_spec.get("Configs")
            if raw_references is None:
                continue
            for raw_reference in require_list(
                raw_references,
                f"Docker service {service_name} {generation} Configs",
            ):
                reference = require_mapping(
                    raw_reference,
                    f"Docker service {service_name} Config reference",
                )
                references.add(
                    require_string(
                        reference.get("ConfigID"),
                        f"Docker service {service_name} ConfigID",
                    )
                )
    return references


def build_plan(
    raw_configs: Any,
    raw_services: Any,
    *,
    selected_stacks: set[str],
    retain: int,
) -> dict[str, Any]:
    if not selected_stacks or not selected_stacks.issubset(SUPPORTED_STACK_KINDS):
        raise ConfigGcError("selected Config owners are invalid")
    if isinstance(retain, bool) or not 1 <= retain <= MAX_RETENTION:
        raise ConfigGcError(
            f"retention must be between 1 and {MAX_RETENTION} generations"
        )
    configs: list[dict[str, Any]] = []
    observed_ids: set[str] = set()
    observed_names: set[str] = set()
    for raw_config in require_list(raw_configs, "Docker Config inventory"):
        config = normalize_config(raw_config, selected_stacks)
        if config is None:
            continue
        if config["id"] in observed_ids or config["name"] in observed_names:
            raise ConfigGcError("managed Docker Config IDs and names must be unique")
        observed_ids.add(config["id"])
        observed_names.add(config["name"])
        configs.append(config)

    references = referenced_config_ids(raw_services)
    by_series: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for config in configs:
        by_series.setdefault(
            (config["stack"], config["kind"], config["series"]),
            [],
        ).append(config)

    retained_by_policy: set[str] = set()
    for series_configs in by_series.values():
        newest_first = sorted(
            series_configs,
            key=lambda item: (item["createdAt"], item["id"]),
            reverse=True,
        )
        retained_by_policy.update(item["id"] for item in newest_first[:retain])
    candidates = sorted(
        (
            config
            for config in configs
            if config["id"] not in references and config["id"] not in retained_by_policy
        ),
        key=lambda item: (
            item["stack"],
            item["series"],
            item["createdAt"],
            item["id"],
        ),
    )
    protected = sorted(
        (
            {
                **config,
                "reasons": sorted(
                    [
                        *(["service-reference"] if config["id"] in references else []),
                        *(["retention"] if config["id"] in retained_by_policy else []),
                    ]
                ),
            }
            for config in configs
            if config["id"] in references or config["id"] in retained_by_policy
        ),
        key=lambda item: (item["stack"], item["series"], item["name"], item["id"]),
    )
    plan_identity = {
        "schemaVersion": 1,
        "selectedStacks": sorted(selected_stacks),
        "retainPerSeries": retain,
        "managedConfigIds": sorted(observed_ids),
        "referencedConfigIds": sorted(references),
        "candidates": candidates,
        "protected": protected,
    }
    plan_sha256 = hashlib.sha256(canonical_json(plan_identity)).hexdigest()
    return {
        **plan_identity,
        "planSha256": plan_sha256,
        "confirmation": f"{CONFIRMATION_PREFIX}:{plan_sha256}",
    }


class DockerClient:
    """Minimal Docker CLI adapter; it never requests Config payload data."""

    def __init__(self, docker_bin: str = "/usr/bin/docker") -> None:
        self.docker_bin = docker_bin

    def run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                [self.docker_bin, *argv],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise ConfigGcError("cannot execute the Docker CLI") from exc
        if completed.returncode != 0:
            raise ConfigGcError(
                "Docker command failed; no force or service mutation was attempted"
            )
        return completed

    def inspect_managed_configs(self) -> list[Any]:
        listed = self.run(
            [
                "config",
                "ls",
                "--filter",
                f"label={MANAGED_BY_LABEL}={MANAGED_BY_VALUE}",
                "--format",
                "{{.ID}}",
            ]
        )
        config_ids = sorted(
            line.strip() for line in listed.stdout.splitlines() if line.strip()
        )
        if len(config_ids) != len(set(config_ids)):
            raise ConfigGcError("Docker returned duplicate Config IDs")
        if not config_ids:
            return []
        inspected = self.run(["config", "inspect", *config_ids])
        try:
            return require_list(
                json.loads(inspected.stdout),
                "Docker Config inspection",
            )
        except json.JSONDecodeError as exc:
            raise ConfigGcError(
                "Docker Config inspection returned invalid JSON"
            ) from exc

    def inspect_services(self) -> list[Any]:
        listed = self.run(["service", "ls", "--quiet"])
        service_ids = sorted(
            line.strip() for line in listed.stdout.splitlines() if line.strip()
        )
        if len(service_ids) != len(set(service_ids)):
            raise ConfigGcError("Docker returned duplicate service IDs")
        if not service_ids:
            return []
        inspected = self.run(["service", "inspect", *service_ids])
        try:
            return require_list(
                json.loads(inspected.stdout),
                "Docker service inspection",
            )
        except json.JSONDecodeError as exc:
            raise ConfigGcError(
                "Docker service inspection returned invalid JSON"
            ) from exc

    def remove_config(self, config_id: str) -> None:
        self.run(["config", "rm", config_id])


def plan_with_client(
    client: DockerClient,
    *,
    selected_stacks: set[str],
    retain: int,
) -> dict[str, Any]:
    return build_plan(
        client.inspect_managed_configs(),
        client.inspect_services(),
        selected_stacks=selected_stacks,
        retain=retain,
    )


def reconcile(
    client: DockerClient,
    *,
    selected_stacks: set[str],
    retain: int,
    apply: bool,
    confirmation: str | None,
) -> dict[str, Any]:
    plan = plan_with_client(
        client,
        selected_stacks=selected_stacks,
        retain=retain,
    )
    if not apply:
        if confirmation is not None:
            raise ConfigGcError("--confirm is valid only with --apply")
        return {**plan, "mode": "dry-run", "removed": []}
    if confirmation != plan["confirmation"]:
        raise ConfigGcError(
            "confirmation does not bind the current Config inventory and references"
        )

    removed: list[dict[str, str]] = []
    for candidate in plan["candidates"]:
        live_references = referenced_config_ids(client.inspect_services())
        if candidate["id"] in live_references:
            raise ConfigGcError(
                f"Docker Config became referenced before removal: {candidate['name']}"
            )
        client.remove_config(candidate["id"])
        removed.append(
            {
                "id": candidate["id"],
                "name": candidate["name"],
            }
        )
    return {**plan, "mode": "apply", "removed": removed}


def retention_value(raw: str) -> int:
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("retention must be an integer") from exc
    if not 1 <= value <= MAX_RETENTION:
        raise argparse.ArgumentTypeError(
            f"retention must be between 1 and {MAX_RETENTION}"
        )
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run immutable Config GC. Apply requires the exact confirmation "
            "printed by an unchanged dry-run."
        )
    )
    parser.add_argument(
        "--stack",
        action="append",
        choices=sorted(SUPPORTED_STACK_KINDS),
        dest="stacks",
        help="Config owner to include; repeat to select multiple (default: all)",
    )
    parser.add_argument(
        "--retain",
        type=retention_value,
        default=DEFAULT_RETENTION,
        metavar="GENERATIONS",
        help=f"newest generations retained per Config series (default: {DEFAULT_RETENTION})",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--docker-bin", default="/usr/bin/docker")
    args = parser.parse_args(argv)
    args.stacks = sorted(set(args.stacks or SUPPORTED_STACK_KINDS))
    if args.confirm is not None and not args.apply:
        parser.error("--confirm is valid only with --apply")
    if args.apply and args.confirm is None:
        parser.error("--apply requires --confirm from an unchanged dry-run")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(raw_arguments)
    if args.apply:
        ensure_mutation_lock(
            "docker-config-gc",
            [
                sys.executable,
                str(Path(__file__).resolve()),
                *raw_arguments,
            ],
            caller=Path(__file__),
        )
    try:
        report = reconcile(
            DockerClient(args.docker_bin),
            selected_stacks=set(args.stacks),
            retain=args.retain,
            apply=args.apply,
            confirmation=args.confirm,
        )
    except ConfigGcError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
