#!/usr/bin/env python3
"""Verify live Swarm resource references against a rendered stack."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import yaml


class DeploymentContractError(RuntimeError):
    """The live Swarm deployment differs from the reviewed stack."""


def require_mapping(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DeploymentContractError(f"{description} must be a mapping")
    return value


def require_list(value: Any, description: str) -> list[Any]:
    if not isinstance(value, list):
        raise DeploymentContractError(f"{description} must be a list")
    return value


def require_string(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise DeploymentContractError(f"{description} must be a non-empty string")
    return value


def load_stack(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DeploymentContractError(f"cannot load rendered stack: {path}") from exc
    return require_mapping(document, "rendered stack")


def load_verified_references(raw: str, description: str) -> dict[str, dict[str, str]]:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeploymentContractError(f"{description} is not valid JSON") from exc
    mapping = require_mapping(document, description)
    result: dict[str, dict[str, str]] = {}
    for alias, raw_reference in mapping.items():
        key = require_string(alias, f"{description} alias")
        reference = require_mapping(
            raw_reference,
            f"{description} reference {key}",
        )
        if set(reference) != {"id", "name"}:
            raise DeploymentContractError(
                f"{description} reference {key} has unexpected fields"
            )
        result[key] = {
            "id": require_string(reference["id"], f"{description} {key} ID"),
            "name": require_string(reference["name"], f"{description} {key} name"),
        }
    return result


def load_verified_external_networks(
    raw: str,
) -> dict[str, dict[str, Any]]:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeploymentContractError(
            "verified external networks is not valid JSON"
        ) from exc
    mapping = require_mapping(document, "verified external networks")
    result: dict[str, dict[str, Any]] = {}
    for alias, raw_reference in mapping.items():
        key = require_string(alias, "verified external network alias")
        reference = require_mapping(
            raw_reference,
            f"verified external network {key}",
        )
        if (
            set(reference) != {"id", "name", "internal"}
            or type(reference.get("internal")) is not bool
        ):
            raise DeploymentContractError(
                f"verified external network {key} has unexpected fields"
            )
        result[key] = {
            "id": require_string(
                reference["id"],
                f"verified external network {key} ID",
            ),
            "name": require_string(
                reference["name"],
                f"verified external network {key} name",
            ),
            "internal": reference["internal"],
        }
    return result


def physical_network_name(
    stack_name: str,
    logical_name: str,
    declaration: dict[str, Any],
) -> str:
    explicit_name = declaration.get("name")
    if explicit_name is not None:
        return require_string(explicit_name, f"network {logical_name} name")
    return f"{stack_name}_{logical_name}"


def normalize_file_reference(
    item: Any,
    *,
    kind: str,
    declarations: dict[str, Any],
    verified: dict[str, dict[str, str]],
) -> tuple[str, str, str, str, str, int]:
    if isinstance(item, str):
        alias = item
        reference: dict[str, Any] = {}
    else:
        reference = require_mapping(item, f"{kind} service reference")
        alias = require_string(reference.get("source"), f"{kind} source")
    declaration = require_mapping(
        declarations.get(alias),
        f"{kind} declaration {alias}",
    )
    expected_name = require_string(
        declaration.get("name"),
        f"{kind} declaration {alias} name",
    )
    identity = require_mapping(
        verified.get(alias),
        f"verified {kind} {alias}",
    )
    if identity.get("name") != expected_name:
        raise DeploymentContractError(
            f"verified {kind} name differs from rendered declaration: {alias}"
        )
    target = reference.get("target", alias)
    uid = reference.get("uid", "0")
    gid = reference.get("gid", "0")
    mode = reference.get("mode", 0o444)
    if (
        not isinstance(target, str)
        or not target
        or isinstance(mode, bool)
        or not isinstance(mode, int)
        or mode < 0
        or mode > 0o7777
    ):
        raise DeploymentContractError(f"invalid rendered {kind} file reference")
    return (
        require_string(identity.get("id"), f"verified {kind} {alias} ID"),
        expected_name,
        target,
        str(uid),
        str(gid),
        mode,
    )


def observed_file_references(
    items: Any,
    *,
    kind: str,
) -> list[tuple[str, str, str, str, str, int]]:
    if items is None:
        return []
    id_key = "ConfigID" if kind == "config" else "SecretID"
    name_key = "ConfigName" if kind == "config" else "SecretName"
    result: list[tuple[str, str, str, str, str, int]] = []
    for raw_item in require_list(items, f"live {kind} references"):
        item = require_mapping(raw_item, f"live {kind} reference")
        file_reference = require_mapping(
            item.get("File"),
            f"live {kind} file reference",
        )
        mode = file_reference.get("Mode")
        if isinstance(mode, bool) or not isinstance(mode, int):
            raise DeploymentContractError(f"live {kind} mode is invalid")
        result.append(
            (
                require_string(item.get(id_key), f"live {kind} ID"),
                require_string(item.get(name_key), f"live {kind} name"),
                require_string(file_reference.get("Name"), f"live {kind} target"),
                str(file_reference.get("UID")),
                str(file_reference.get("GID")),
                mode,
            )
        )
    return sorted(result)


def index_networks(
    stack_name: str,
    declarations: dict[str, Any],
    inspected: list[Any],
    verified_external_networks: dict[str, dict[str, Any]],
) -> dict[str, str]:
    by_name: dict[str, dict[str, Any]] = {}
    for raw_network in inspected:
        network = require_mapping(raw_network, "inspected Docker network")
        name = require_string(network.get("Name"), "inspected network name")
        if name in by_name:
            raise DeploymentContractError(f"duplicate inspected network: {name}")
        by_name[name] = network

    expected_names = {
        physical_network_name(
            stack_name,
            logical_name,
            require_mapping(declaration, f"network {logical_name}"),
        )
        for logical_name, declaration in declarations.items()
    }
    if set(by_name) != expected_names:
        raise DeploymentContractError(
            "inspected Docker network set differs from rendered stack"
        )

    expected_external_aliases = {
        logical_name
        for logical_name, raw_declaration in declarations.items()
        if require_mapping(
            raw_declaration,
            f"network {logical_name}",
        ).get("external")
        is True
    }
    if set(verified_external_networks) != expected_external_aliases:
        raise DeploymentContractError(
            "verified external network set differs from rendered stack"
        )

    ids: dict[str, str] = {}
    observed_ids: set[str] = set()
    for logical_name, raw_declaration in declarations.items():
        declaration = require_mapping(raw_declaration, f"network {logical_name}")
        name = physical_network_name(stack_name, logical_name, declaration)
        network = by_name[name]
        network_id = require_string(network.get("Id"), f"network {name} ID")
        if network_id in observed_ids:
            raise DeploymentContractError("Docker network IDs are not unique")
        observed_ids.add(network_id)
        external = declaration.get("external", False)
        if type(external) is not bool:
            raise DeploymentContractError(
                f"rendered network {logical_name} external flag is not boolean"
            )
        if external:
            identity = require_mapping(
                verified_external_networks.get(logical_name),
                f"verified external network {logical_name}",
            )
            if identity.get("id") != network_id or identity.get("name") != name:
                raise DeploymentContractError(
                    "live external Docker network differs from its verified "
                    f"identity: {name}"
                )
            expected_internal = identity.get("internal")
        else:
            expected_internal = declaration.get("internal", False)
        if type(expected_internal) is not bool:
            raise DeploymentContractError(
                f"rendered network {logical_name} internal flag is not boolean"
            )
        if (
            network.get("Driver") != "overlay"
            or network.get("Scope") != "swarm"
            or network.get("Attachable") is not False
            or network.get("Internal") is not expected_internal
            or require_mapping(
                network.get("Options"),
                f"network {name} options",
            ).get("encrypted")
            != ""
        ):
            raise DeploymentContractError(
                f"live Docker network differs from rendered contract: {name}"
            )
        ids[logical_name] = network_id
    return ids


def validate_deployment(
    stack: dict[str, Any],
    stack_name: str,
    inspected_services: list[Any],
    inspected_networks: list[Any],
    verified_configs: dict[str, dict[str, str]],
    verified_secrets: dict[str, dict[str, str]],
    verified_external_networks: dict[str, dict[str, Any]],
) -> None:
    services = require_mapping(stack.get("services"), "rendered services")
    config_declarations = require_mapping(
        stack.get("configs"),
        "rendered configs",
    )
    secret_declarations = require_mapping(
        stack.get("secrets"),
        "rendered secrets",
    )
    network_declarations = require_mapping(
        stack.get("networks"),
        "rendered networks",
    )
    if set(verified_configs) != set(config_declarations):
        raise DeploymentContractError(
            "verified Docker Config set differs from rendered stack"
        )
    if set(verified_secrets) != set(secret_declarations):
        raise DeploymentContractError(
            "verified Docker Secret set differs from rendered stack"
        )

    network_ids = index_networks(
        stack_name,
        network_declarations,
        inspected_networks,
        verified_external_networks,
    )
    live_services: dict[str, dict[str, Any]] = {}
    for raw_service in inspected_services:
        service = require_mapping(raw_service, "inspected Docker service")
        physical_name = require_string(
            require_mapping(service.get("Spec"), "service Spec").get("Name"),
            "service name",
        )
        if physical_name in live_services:
            raise DeploymentContractError(
                f"duplicate inspected service: {physical_name}"
            )
        live_services[physical_name] = service
    expected_service_names = {
        f"{stack_name}_{logical_name}" for logical_name in services
    }
    if set(live_services) != expected_service_names:
        raise DeploymentContractError(
            "inspected Docker service set differs from rendered stack"
        )

    for logical_name, raw_service_contract in services.items():
        service_contract = require_mapping(
            raw_service_contract,
            f"rendered service {logical_name}",
        )
        physical_name = f"{stack_name}_{logical_name}"
        live_service = live_services[physical_name]
        task_template = require_mapping(
            require_mapping(live_service["Spec"], f"{physical_name} Spec").get(
                "TaskTemplate"
            ),
            f"{physical_name} TaskTemplate",
        )
        container_spec = require_mapping(
            task_template.get("ContainerSpec"),
            f"{physical_name} ContainerSpec",
        )

        expected_configs = sorted(
            normalize_file_reference(
                item,
                kind="config",
                declarations=config_declarations,
                verified=verified_configs,
            )
            for item in service_contract.get("configs", [])
        )
        observed_configs = observed_file_references(
            container_spec.get("Configs"),
            kind="config",
        )
        if observed_configs != expected_configs:
            raise DeploymentContractError(
                f"live Docker Config references differ for {physical_name}"
            )

        expected_secrets = sorted(
            normalize_file_reference(
                item,
                kind="secret",
                declarations=secret_declarations,
                verified=verified_secrets,
            )
            for item in service_contract.get("secrets", [])
        )
        observed_secrets = observed_file_references(
            container_spec.get("Secrets"),
            kind="secret",
        )
        if observed_secrets != expected_secrets:
            raise DeploymentContractError(
                f"live Docker Secret references differ for {physical_name}"
            )

        logical_networks = require_list(
            service_contract.get("networks", []),
            f"rendered networks for {logical_name}",
        )
        if any(
            not isinstance(item, str) or item not in network_ids
            for item in logical_networks
        ) or len(logical_networks) != len(set(logical_networks)):
            raise DeploymentContractError(
                f"rendered service network references are invalid: {logical_name}"
            )
        live_networks = task_template.get("Networks")
        if live_networks is None:
            observed_network_ids: list[str] = []
        else:
            observed_network_ids = sorted(
                require_string(
                    require_mapping(item, "live service network").get("Target"),
                    "live service network target",
                )
                for item in require_list(live_networks, "live service networks")
            )
        expected_network_ids = sorted(network_ids[item] for item in logical_networks)
        if observed_network_ids != expected_network_ids:
            raise DeploymentContractError(
                f"live Docker network references differ for {physical_name}"
            )


def docker_inspect(
    docker_bin: str,
    object_type: str,
    names: list[str],
) -> list[Any]:
    if not names:
        return []
    completed = subprocess.run(
        [docker_bin, object_type, "inspect", *names],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise DeploymentContractError(
            f"Docker {object_type} inspection failed without mutating the stack"
        )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DeploymentContractError(
            f"Docker {object_type} inspection returned invalid JSON"
        ) from exc
    return require_list(document, f"Docker {object_type} inspection")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", type=Path, required=True)
    parser.add_argument("--stack-name", required=True)
    parser.add_argument("--docker-bin", default="/usr/bin/docker")
    parser.add_argument("--verified-configs", required=True)
    parser.add_argument("--verified-secrets", required=True)
    parser.add_argument("--verified-external-networks", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if not args.stack_name or not args.stack_name.replace("-", "").isalnum():
            raise DeploymentContractError("stack name is invalid")
        stack = load_stack(args.stack)
        verified_configs = load_verified_references(
            args.verified_configs,
            "verified Docker Configs",
        )
        verified_secrets = load_verified_references(
            args.verified_secrets,
            "verified Docker Secrets",
        )
        verified_external_networks = load_verified_external_networks(
            args.verified_external_networks
        )
        services = require_mapping(stack.get("services"), "rendered services")
        networks = require_mapping(stack.get("networks"), "rendered networks")
        service_names = [f"{args.stack_name}_{name}" for name in services]
        network_names = [
            physical_network_name(
                args.stack_name,
                name,
                require_mapping(declaration, f"network {name}"),
            )
            for name, declaration in networks.items()
        ]
        validate_deployment(
            stack,
            args.stack_name,
            docker_inspect(args.docker_bin, "service", service_names),
            docker_inspect(args.docker_bin, "network", network_names),
            verified_configs,
            verified_secrets,
            verified_external_networks,
        )
    except DeploymentContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("Live Swarm Config, Secret and network references match the reviewed stack.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
