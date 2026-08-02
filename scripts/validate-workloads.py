#!/usr/bin/env python3
"""Validate the rendered Swarm workloads against both declarative catalogs."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

EXPECTED_APPROVED_IDS = {
    "kropia",
    "minecraft",
    "minecraft-stats",
    "n8n",
    "openclaw-clean",
    "passbolt",
    "personal-website-alberto",
    "personal-website-pablo",
    "shlink",
    "traefik-edge",
}
EXPECTED_STACK_SERVICES = {
    "kropia",
    "minecraft",
    "minecraft-stats",
    "n8n",
    "n8n-db",
    "n8n-runners",
    "openclaw",
    "passbolt",
    "passbolt-db",
    "portfolio-alberto",
    "portfolio-pablo",
    "redis-coordinator",
    "selenium",
    "shlink",
    "shlink-db",
}
EXPECTED_INTERNAL_NETWORKS = {
    "minecraft-monitoring",
    "n8n-backend",
    "n8n-browser",
    "n8n-coordination",
    "n8n-monitoring",
    "n8n-runner-broker",
    "passbolt-backend",
    "shlink-backend",
}
EXPECTED_DEDICATED_EGRESS_NETWORKS = {
    "minecraft-egress": {"minecraft"},
    "selenium-egress": {"selenium"},
}
EXPECTED_EDGE_NETWORKS = {
    "edge-kropia": ("apptolast-edge-kropia", {"kropia"}),
    "edge-minecraft-stats": (
        "apptolast-edge-minecraft-stats",
        {"minecraft-stats"},
    ),
    "edge-n8n": ("apptolast-edge-n8n", {"n8n"}),
    "edge-openclaw": ("apptolast-edge-openclaw", {"openclaw"}),
    "edge-passbolt": ("apptolast-edge-passbolt", {"passbolt"}),
    "edge-portfolio-alberto": (
        "apptolast-edge-portfolio-alberto",
        {"portfolio-alberto"},
    ),
    "edge-portfolio-pablo": (
        "apptolast-edge-portfolio-pablo",
        {"portfolio-pablo"},
    ),
    "edge-shlink": ("apptolast-edge-shlink", {"shlink"}),
}
EXPECTED_SERVICE_NETWORKS = {
    "kropia": {"edge-kropia"},
    "minecraft": {"minecraft-egress", "minecraft-monitoring"},
    "minecraft-stats": {"edge-minecraft-stats"},
    "n8n-db": {"n8n-backend"},
    "n8n": {
        "edge-n8n",
        "n8n-backend",
        "n8n-browser",
        "n8n-coordination",
        "n8n-monitoring",
        "n8n-runner-broker",
    },
    "n8n-runners": {"n8n-runner-broker"},
    "redis-coordinator": {"n8n-coordination"},
    "selenium": {"n8n-browser", "selenium-egress"},
    "openclaw": {"edge-openclaw"},
    "passbolt-db": {"passbolt-backend"},
    "passbolt": {"edge-passbolt", "passbolt-backend"},
    "portfolio-alberto": {"edge-portfolio-alberto"},
    "portfolio-pablo": {"edge-portfolio-pablo"},
    "shlink-db": {"shlink-backend"},
    "shlink": {"edge-shlink", "shlink-backend"},
}
EXPECTED_EDGE_CONSUMERS = {
    "kropia",
    "minecraft-stats",
    "n8n",
    "openclaw",
    "passbolt",
    "portfolio-alberto",
    "portfolio-pablo",
    "shlink",
}
IMAGE_CONTRACT = {
    "kropia": ("kropia", "app"),
    "minecraft": ("minecraft", "server"),
    "minecraft-stats": ("minecraft-stats", "app"),
    "n8n": ("n8n", "app"),
    "n8n-db": ("n8n", "database"),
    "n8n-runners": ("n8n", "runner"),
    "redis-coordinator": ("n8n", "workflow-cache"),
    "selenium": ("n8n", "browser"),
    "openclaw": ("openclaw-clean", "app"),
    "passbolt": ("passbolt", "app"),
    "passbolt-db": ("passbolt", "database"),
    "portfolio-alberto": ("personal-website-alberto", "app"),
    "portfolio-pablo": ("personal-website-pablo", "app"),
    "shlink": ("shlink", "app"),
    "shlink-db": ("shlink", "database"),
}
CONFIG_SOURCE_BY_KEY = {
    "n8n_entrypoint": "n8n-entrypoint.sh",
    "n8n_runners_entrypoint": "n8n-runners-entrypoint.sh",
    "n8n_task_runners": "n8n-task-runners.json",
    "openclaw_entrypoint": "openclaw-entrypoint.sh",
    "passbolt_entrypoint": "passbolt-entrypoint.sh",
    "portfolio_pablo_entrypoint": "portfolio-pablo-entrypoint.sh",
    "redis_config": "redis.conf",
    "shlink_entrypoint": "shlink-entrypoint.sh",
}


class ContractError(RuntimeError):
    """The rendered workloads stack differs from its catalogs."""


def load_runner_manager() -> Any:
    path = Path(__file__).with_name("manage-n8n-runner-image.py")
    spec = importlib.util.spec_from_file_location("manage_n8n_runner_image", path)
    if spec is None or spec.loader is None:
        raise ContractError("cannot load n8n runner image manager")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ContractError(f"cannot load YAML: {path}") from exc
    if not isinstance(document, dict):
        raise ContractError(f"YAML document is not a mapping: {path}")
    return document


def find_image(
    services_by_id: dict[str, dict[str, Any]],
    service_id: str,
    component: str,
) -> str:
    matches = [
        item.get("reference")
        for item in services_by_id[service_id].get("images", [])
        if item.get("component") == component
    ]
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise ContractError(f"ambiguous image {service_id}/{component}")
    return matches[0]


def secret_sources(service: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for item in service.get("secrets", []):
        if isinstance(item, str):
            result.add(item)
        elif isinstance(item, dict) and isinstance(item.get("source"), str):
            result.add(item["source"])
        else:
            raise ContractError("invalid service secret reference")
    return result


def bind_sources(service: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for item in service.get("volumes", []):
        if isinstance(item, dict) and item.get("type") == "bind":
            source = item.get("source")
            if not isinstance(source, str):
                raise ContractError("bind mount has no source")
            result.append(source)
        elif isinstance(item, str):
            raise ContractError("short volume syntax is forbidden")
    return result


def validate_stack(
    stack: dict[str, Any],
    service_catalog: dict[str, Any],
    secret_catalog: dict[str, Any],
    runner_metadata: dict[str, Any],
    platform: dict[str, Any],
) -> None:
    approved = service_catalog.get("approved_services")
    datasets = service_catalog.get("datasets")
    if not isinstance(approved, list) or not isinstance(datasets, list):
        raise ContractError("service catalog is incomplete")
    services_by_id = {item["id"]: item for item in approved}
    if set(services_by_id) != EXPECTED_APPROVED_IDS:
        raise ContractError("approved service scope changed")
    services = stack.get("services")
    if not isinstance(services, dict) or set(services) != EXPECTED_STACK_SERVICES:
        raise ContractError("rendered Swarm service set changed")

    for stack_service, (catalog_id, component) in IMAGE_CONTRACT.items():
        catalog_reference = find_image(services_by_id, catalog_id, component)
        expected = catalog_reference
        if stack_service == "n8n-runners":
            if runner_metadata.get("source_reference") != catalog_reference:
                raise ContractError("n8n runner audited source reference drift")
            expected = runner_metadata.get("image_reference")
            if (
                not isinstance(expected, str)
                or re.fullmatch(
                    r"apptolast/n8n-runners:src-[a-f0-9]{64}",
                    expected,
                )
                is None
            ):
                raise ContractError("n8n runner local image identity is invalid")
        service = services[stack_service]
        if service.get("image") != expected:
            raise ContractError(f"image drift for {stack_service}")
        if stack_service != "n8n-runners" and not re.fullmatch(
            r".+@sha256:[a-f0-9]{64}",
            expected,
        ):
            raise ContractError(f"unpinned image for {stack_service}")
        if "env_file" in service:
            raise ContractError(f"env_file is forbidden for {stack_service}")
        if service.get("privileged") is True:
            raise ContractError(f"privileged mode is forbidden for {stack_service}")
        if "healthcheck" not in service:
            raise ContractError(f"healthcheck missing for {stack_service}")
        deploy = service.get("deploy", {})
        constraints = deploy.get("placement", {}).get("constraints", [])
        if "node.labels.platform.workloads == true" not in constraints:
            raise ContractError(f"placement constraint missing for {stack_service}")
        if deploy.get("replicas") != 1:
            raise ContractError(f"replica count drift for {stack_service}")
        if deploy.get("update_config", {}).get("failure_action") != "rollback":
            raise ContractError(f"rollback-on-update missing for {stack_service}")
        if deploy.get("rollback_config", {}).get("order") != "stop-first":
            raise ContractError(f"rollback order drift for {stack_service}")
        resources = deploy.get("resources", {})
        if not resources.get("limits") or not resources.get("reservations"):
            raise ContractError(f"resource budget missing for {stack_service}")

    minecraft_public_enabled = platform.get("platform_minecraft_public_enabled")
    dns_cutover = platform.get("platform_dns_cutover")
    if (
        type(minecraft_public_enabled) is not bool
        or not isinstance(dns_cutover, dict)
        or dns_cutover.get("minecraft") is not minecraft_public_enabled
        or platform.get("platform_public_tcp_ports") != [80, 443, 25565]
    ):
        raise ContractError("Minecraft public port, firewall and DNS gates differ")

    observed_ports: list[tuple[str, dict[str, Any]]] = []
    for name, service in services.items():
        for port in service.get("ports", []):
            if not isinstance(port, dict):
                raise ContractError(f"short port syntax is forbidden for {name}")
            observed_ports.append((name, port))
    expected_ports = (
        [
            (
                "minecraft",
                {
                    "target": 25565,
                    "published": 25565,
                    "protocol": "tcp",
                    "mode": "host",
                },
            )
        ]
        if minecraft_public_enabled
        else []
    )
    if observed_ports != expected_ports:
        raise ContractError("Minecraft published port differs from its public gate")

    expected_bind_paths = {
        item["target_path"] for item in datasets if item.get("owner") != "traefik-edge"
    }
    observed_bind_paths = {
        path for service in services.values() for path in bind_sources(service)
    }
    if observed_bind_paths != expected_bind_paths:
        raise ContractError("bind mounts differ from the dataset catalog")

    networks = stack.get("networks", {})
    expected_networks = (
        set(EXPECTED_EDGE_NETWORKS)
        | EXPECTED_INTERNAL_NETWORKS
        | set(EXPECTED_DEDICATED_EGRESS_NETWORKS)
    )
    if not isinstance(networks, dict) or set(networks) != expected_networks:
        raise ContractError("rendered Swarm network set changed")
    for logical_name, (
        external_name,
        expected_consumers,
    ) in EXPECTED_EDGE_NETWORKS.items():
        network = networks.get(logical_name, {})
        if set(network) != {"external", "name"} or network != {
            "external": True,
            "name": external_name,
        }:
            raise ContractError(
                f"dedicated external edge network drift: {logical_name}"
            )
        actual_consumers = {
            service_name
            for service_name, service in services.items()
            if logical_name in service.get("networks", [])
        }
        if actual_consumers != expected_consumers:
            raise ContractError(f"dedicated edge consumers drift: {logical_name}")
    for name in EXPECTED_INTERNAL_NETWORKS:
        network = networks.get(name, {})
        if (
            network.get("driver") != "overlay"
            or network.get("internal") is not True
            or network.get("driver_opts", {}).get("encrypted") != ""
        ):
            raise ContractError(f"internal encrypted network drift: {name}")
    if networks["minecraft-monitoring"].get("name") != (
        "apptolast-minecraft-monitoring"
    ):
        raise ContractError("Minecraft monitoring network name drift")
    if networks["n8n-monitoring"].get("name") != "apptolast-n8n-monitoring":
        raise ContractError("n8n monitoring network name drift")
    if networks["n8n-browser"].get("name") != "workloads_n8n-browser":
        raise ContractError("n8n browser network name drift")
    if networks["n8n-runner-broker"].get("name") != ("workloads_n8n-runner-broker"):
        raise ContractError("n8n runner broker network name drift")
    for name, expected_consumers in EXPECTED_DEDICATED_EGRESS_NETWORKS.items():
        network = networks.get(name, {})
        if (
            network.get("driver") != "overlay"
            or network.get("internal") is True
            or network.get("driver_opts", {}).get("encrypted") != ""
        ):
            raise ContractError(f"dedicated encrypted egress drift: {name}")
        actual_consumers = {
            service_name
            for service_name, service in services.items()
            if name in service.get("networks", [])
        }
        if actual_consumers != expected_consumers:
            raise ContractError(f"dedicated egress consumers drift: {name}")
    for service_name, expected_service_networks in EXPECTED_SERVICE_NETWORKS.items():
        actual_service_networks = services[service_name].get("networks", [])
        if (
            not isinstance(actual_service_networks, list)
            or len(actual_service_networks) != len(set(actual_service_networks))
            or set(actual_service_networks) != expected_service_networks
        ):
            raise ContractError(f"service network drift: {service_name}")
    actual_edge_consumers = set().union(
        *(
            {
                service_name
                for service_name, service in services.items()
                if logical_name in service.get("networks", [])
            }
            for logical_name in EXPECTED_EDGE_NETWORKS
        )
    )
    if actual_edge_consumers != EXPECTED_EDGE_CONSUMERS:
        raise ContractError("dedicated edge consumer matrix drift")

    n8n_environment = services["n8n"].get("environment", {})
    if (
        n8n_environment.get("N8N_BLOCK_ENV_ACCESS_IN_NODE") != "true"
        or n8n_environment.get("N8N_UNVERIFIED_PACKAGES_ENABLED") != "false"
    ):
        raise ContractError("n8n workflow security gates are not fail-closed")
    if "EXECUTIONS_MODE" in n8n_environment or any(
        key.startswith("QUEUE_BULL_REDIS_") for key in n8n_environment
    ):
        raise ContractError(
            "n8n queue mode is forbidden without an explicit worker topology"
        )

    secret_entries = secret_catalog.get("workloads_secrets")
    if not isinstance(secret_entries, list):
        raise ContractError("secret catalog is incomplete")
    secret_entries_by_key = {item["key"]: item for item in secret_entries}
    declared_secrets = stack.get("secrets", {})
    if set(declared_secrets) != set(secret_entries_by_key):
        raise ContractError("stack secret aliases differ from secret catalog")
    for key, entry in secret_entries_by_key.items():
        declaration = declared_secrets[key]
        if (
            declaration.get("external") is not True
            or declaration.get("name") != entry["external_name"]
        ):
            raise ContractError(f"external secret drift: {key}")
        actual_consumers = {
            service_name
            for service_name, service in services.items()
            if key in secret_sources(service)
        }
        if actual_consumers != set(entry["consumers"]):
            raise ContractError(f"secret consumer drift: {key}")

    forbidden_secret_environment_keys = {
        "DATASOURCES_DEFAULT_PASSWORD",
        "DB_PASSWORD",
        "DB_POSTGRESDB_PASSWORD",
        "N8N_ENCRYPTION_KEY",
        "N8N_RUNNERS_AUTH_TOKEN",
        "OPENCLAW_GATEWAY_TOKEN",
        "SECURITY_SALT",
    }
    for name, service in services.items():
        environment = service.get("environment", {})
        if forbidden_secret_environment_keys & set(environment):
            raise ContractError(f"secret value environment key present in {name}")

    openclaw = services["openclaw"]
    openclaw_mounts = bind_sources(openclaw)
    expected_openclaw = next(
        item["target_path"] for item in datasets if item["id"] == "openclaw-clean-home"
    )
    if openclaw_mounts != [expected_openclaw]:
        raise ContractError("OpenClaw does not use only clean state")
    health_text = str(openclaw.get("healthcheck", {}).get("test", []))
    if "/healthz" not in health_text or "legacy" in str(openclaw).lower():
        raise ContractError("OpenClaw clean health/state contract changed")

    raw_stack = str(stack)
    if "docker.sock" in raw_stack or "observability" in services:
        raise ContractError("forbidden socket or observability workload found")


def validate_configs(stack: dict[str, Any], config_dir: Path) -> None:
    configs = stack.get("configs")
    if not isinstance(configs, dict) or set(configs) != set(CONFIG_SOURCE_BY_KEY):
        raise ContractError("immutable Docker Config set changed")
    import hashlib

    for key, source_name in CONFIG_SOURCE_BY_KEY.items():
        source = config_dir / source_name
        try:
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
        except OSError as exc:
            raise ContractError(f"cannot read config source: {source}") from exc
        expected_name = f"workloads-{key.replace('_', '-')}-{digest[:16]}"
        declaration = configs[key]
        if (
            declaration.get("external") is not True
            or declaration.get("name") != expected_name
        ):
            raise ContractError(f"immutable Docker Config drift: {key}")
    try:
        runner_config = json.loads(
            (config_dir / "n8n-task-runners.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("cannot load n8n task runner configuration") from exc
    configured_runners = (
        runner_config.get("task-runners") if isinstance(runner_config, dict) else None
    )
    if not isinstance(configured_runners, list):
        raise ContractError("n8n task runner list is invalid")
    python_runners = [
        item
        for item in configured_runners
        if isinstance(item, dict) and item.get("runner-type") == "python"
    ]
    runner_manager = load_runner_manager()
    if (
        len(python_runners) != 1
        or python_runners[0].get("command") != runner_manager.PYTHON_COMMAND
        or python_runners[0].get("args") != list(runner_manager.PYTHON_HARDENING_ARGS)
    ):
        raise ContractError("n8n Python runner hardening args drift")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", type=Path, required=True)
    parser.add_argument("--services", type=Path, required=True)
    parser.add_argument("--secrets", type=Path, required=True)
    parser.add_argument("--platform", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--runner-context", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        stack = load_yaml(args.stack)
        services = load_yaml(args.services)
        secrets = load_yaml(args.secrets)
        platform = load_yaml(args.platform)
        approved = {item["id"]: item for item in services.get("approved_services", [])}
        runner_source = find_image(approved, "n8n", "runner")
        runner_manager = load_runner_manager()
        try:
            runner_metadata = runner_manager.describe(
                args.runner_context,
                runner_source,
            )
        except runner_manager.RunnerImageError as exc:
            raise ContractError(str(exc)) from exc
        validate_stack(stack, services, secrets, runner_metadata, platform)
        validate_configs(stack, args.config_dir)
    except ContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("Rendered Docker Swarm workloads contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
