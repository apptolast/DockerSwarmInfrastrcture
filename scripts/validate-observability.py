#!/usr/bin/env python3
"""Validate the rendered internal observability stack and all cross-contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml


EXPECTED_COMPONENTS = {
    "prometheus",
    "alertmanager",
    "blackbox-exporter",
    "loki",
    "alloy",
    "grafana",
    "node-exporter",
    "cadvisor",
    "postgres-exporter",
    "redis-exporter",
}
EXPECTED_SERVICES = {
    "prometheus",
    "alertmanager",
    "blackbox-exporter",
    "loki",
    "alloy",
    "grafana",
    "node-exporter",
    "cadvisor",
    "postgres-exporter-n8n",
    "postgres-exporter-passbolt",
    "postgres-exporter-shlink",
    "redis-exporter",
}
GLOBAL_SERVICES = {"alloy", "node-exporter", "cadvisor"}
IMAGE_COMPONENT = {
    "prometheus": "prometheus",
    "alertmanager": "alertmanager",
    "blackbox-exporter": "blackbox-exporter",
    "loki": "loki",
    "alloy": "alloy",
    "grafana": "grafana",
    "node-exporter": "node-exporter",
    "cadvisor": "cadvisor",
    "postgres-exporter-n8n": "postgres-exporter",
    "postgres-exporter-passbolt": "postgres-exporter",
    "postgres-exporter-shlink": "postgres-exporter",
    "redis-exporter": "redis-exporter",
}
EXPECTED_NETWORKS = {
    "monitoring",
    "edge-monitoring",
    "n8n-monitoring",
    "workloads-n8n-backend",
    "workloads-n8n-coordination",
    "workloads-passbolt-backend",
    "workloads-shlink-backend",
    "minecraft-monitoring",
}
EXPECTED_SERVICE_NETWORKS = {
    "prometheus": {
        "monitoring",
        "edge-monitoring",
        "n8n-monitoring",
    },
    "alertmanager": {"monitoring"},
    "blackbox-exporter": {
        "monitoring",
        "edge-monitoring",
        "minecraft-monitoring",
    },
    "loki": {"monitoring"},
    "alloy": {"monitoring"},
    "grafana": {"monitoring"},
    "node-exporter": {"monitoring"},
    "cadvisor": {"monitoring"},
    "postgres-exporter-n8n": {
        "monitoring",
        "workloads-n8n-backend",
    },
    "postgres-exporter-passbolt": {
        "monitoring",
        "workloads-passbolt-backend",
    },
    "postgres-exporter-shlink": {
        "monitoring",
        "workloads-shlink-backend",
    },
    "redis-exporter": {
        "monitoring",
        "workloads-n8n-coordination",
    },
}
EXPECTED_CONFIG_DESTINATIONS = {
    "prometheus_config": "prometheus.yml",
    "prometheus_alerts": "prometheus-alerts.yml",
    "alertmanager_config": "alertmanager.yml",
    "blackbox_config": "blackbox.yml",
    "loki_config": "loki.yml",
    "alloy_config": "alloy.alloy",
    "grafana_datasources": "grafana-datasources.yml",
    "grafana_dashboards": "grafana-dashboards.yml",
    "grafana_dashboard_overview": "dashboards/platform-overview.json",
    "grafana_dashboard_logs": "dashboards/platform-logs.json",
}
EXPECTED_SECRET_CONSUMERS = {
    "grafana_admin_password": {"grafana"},
    "grafana_secret_key": {"grafana"},
    "postgres_exporter_n8n_password": {"postgres-exporter-n8n"},
    "postgres_exporter_passbolt_password": {
        "postgres-exporter-passbolt"
    },
    "postgres_exporter_shlink_password": {"postgres-exporter-shlink"},
}
EXPECTED_JOBS = {
    "prometheus",
    "alertmanager",
    "blackbox-exporter",
    "loki",
    "alloy",
    "grafana",
    "node-exporter",
    "cadvisor",
    "postgres-exporter-n8n",
    "postgres-exporter-passbolt",
    "postgres-exporter-shlink",
    "redis-exporter",
    "traefik",
    "n8n",
    "blackbox-http-public",
    "blackbox-minecraft-tcp",
}
EXPECTED_STATIC_TARGETS = {
    "prometheus": {"prometheus:9090"},
    "alertmanager": {"alertmanager:9093"},
    "blackbox-exporter": {"blackbox-exporter:9115"},
    "loki": {"loki:3100"},
    "alloy": {"tasks.alloy:12345"},
    "grafana": {"grafana:3000"},
    "postgres-exporter-n8n": {"postgres-exporter-n8n:9187"},
    "postgres-exporter-passbolt": {"postgres-exporter-passbolt:9187"},
    "postgres-exporter-shlink": {"postgres-exporter-shlink:9187"},
    "redis-exporter": {"redis-exporter:9121"},
    "traefik": {"edge_traefik:8082"},
    "n8n": {"workloads_n8n:5678"},
}
EXPECTED_PUBLIC_PROBE_PATHS = {
    "kropia": "/health",
    "minecraft-stats": "/actuator/health",
    "n8n": "/healthz",
    "openclaw-clean": "/healthz",
    "passbolt": "/healthcheck/status.json",
    "personal-website-alberto": "/robots.txt",
    "personal-website-pablo": "/placeholder-logo.svg",
    "shlink": "/rest/health",
}
EXPECTED_HEALTH_MARKERS = {
    "prometheus": "http://127.0.0.1:9090/-/ready",
    "alertmanager": "http://127.0.0.1:9093/-/ready",
    "blackbox-exporter": "http://127.0.0.1:9115/-/healthy",
    "loki": "-verify-config=true",
    "alloy": "GET /-/ready HTTP/1.1",
    "grafana": "http://127.0.0.1:3000/api/health",
    "node-exporter": "http://127.0.0.1:9100/metrics",
    "cadvisor": "http://127.0.0.1:8080/healthz",
    "postgres-exporter-n8n": "http://127.0.0.1:9187/metrics",
    "postgres-exporter-passbolt": "http://127.0.0.1:9187/metrics",
    "postgres-exporter-shlink": "http://127.0.0.1:9187/metrics",
    "redis-exporter": "--version",
}
EXPECTED_ALERTS = {
    "PrometheusTargetDown",
    "PublicEndpointDown",
    "MinecraftEndpointDown",
    "HostMemoryPressure",
    "HostFilesystemSpaceLow",
    "HostFilesystemWillFillIn24Hours",
    "ContainerAggregateMemoryPressure",
    "BackupMetricsMissing",
    "BackupLastRunFailed",
    "ApplicationBackupStale",
    "PrometheusRuleEvaluationFailures",
    "LokiDiscardingLogs",
    "ExternalAlertDeliveryDisabled",
}
RESTRICTED_DOCKER_SOCKET = (
    "/run/dockerswarm-observability/docker-readonly.sock"
)
IMAGE_RE = re.compile(r".+@sha256:[a-f0-9]{64}")


class ContractError(RuntimeError):
    """The rendered observability platform differs from its reviewed contract."""


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ContractError(f"cannot load YAML: {path}") from exc
    if not isinstance(document, dict):
        raise ContractError(f"YAML document is not a mapping: {path}")
    return document


def load_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load JSON: {path}") from exc
    if not isinstance(document, dict):
        raise ContractError(f"JSON document is not a mapping: {path}")
    return document


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


def network_names(service: dict[str, Any]) -> set[str]:
    networks = service.get("networks", [])
    if isinstance(networks, list) and all(
        isinstance(item, str) for item in networks
    ):
        return set(networks)
    if isinstance(networks, dict):
        return set(networks)
    raise ContractError("invalid service network reference")


def bind_sources(service: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for item in service.get("volumes", []):
        if not isinstance(item, dict) or item.get("type") != "bind":
            raise ContractError("only long bind mounts are permitted")
        source = item.get("source")
        if not isinstance(source, str):
            raise ContractError("bind mount has no source")
        result.add(source)
    return result


def bind_mounts(service: dict[str, Any]) -> list[dict[str, Any]]:
    """Return validated long-form bind mounts for one service."""
    mounts = service.get("volumes", [])
    if not isinstance(mounts, list):
        raise ContractError("service volumes must be a list")
    if any(
        not isinstance(item, dict) or item.get("type") != "bind"
        for item in mounts
    ):
        raise ContractError("only long bind mounts are permitted")
    return mounts


def static_targets(job: dict[str, Any]) -> set[str]:
    """Flatten static Prometheus target groups."""
    return {
        target
        for item in job.get("static_configs", [])
        for target in item.get("targets", [])
    }


def validate_stack(
    stack: dict[str, Any],
    service_catalog: dict[str, Any],
    secret_catalog: dict[str, Any],
    platform: dict[str, Any],
) -> None:
    observability = (
        service_catalog.get("internal_platform", {}).get("observability")
    )
    if not isinstance(observability, dict):
        raise ContractError("observability catalog is absent")
    if (
        observability.get("classification") != "internal-platform"
        or observability.get("legacy_migration") is not False
        or observability.get("hostnames") != []
    ):
        raise ContractError("observability is not a clean internal platform")
    components = observability.get("components")
    ports = observability.get("ports")
    if not isinstance(components, list) or not isinstance(ports, list):
        raise ContractError("observability component catalog is incomplete")
    components_by_id = {item.get("id"): item for item in components}
    if set(components_by_id) != EXPECTED_COMPONENTS:
        raise ContractError("observability component scope changed")
    if any(
        item.get("published") is not None
        or item.get("exposure") != "internal"
        for item in ports
    ):
        raise ContractError("observability catalog publishes a port")
    for component, item in components_by_id.items():
        image = item.get("image")
        if not isinstance(image, str) or not IMAGE_RE.fullmatch(image):
            raise ContractError(f"unpinned image for {component}")

    services = stack.get("services")
    if not isinstance(services, dict) or set(services) != EXPECTED_SERVICES:
        raise ContractError("rendered observability service set changed")
    for service_name, service in services.items():
        expected_image = components_by_id[
            IMAGE_COMPONENT[service_name]
        ]["image"]
        if service.get("image") != expected_image:
            raise ContractError(f"image drift for {service_name}")
        if service.get("ports", []) != []:
            raise ContractError(f"published port found in {service_name}")
        if service.get("network_mode") == "host":
            raise ContractError(f"host networking found in {service_name}")
        if service.get("privileged") is True:
            raise ContractError(f"privileged mode found in {service_name}")
        if service.get("read_only") is not True:
            raise ContractError(f"read-only root missing for {service_name}")
        if "no-new-privileges:true" not in service.get("security_opt", []):
            raise ContractError(
                f"no-new-privileges missing for {service_name}"
            )
        healthcheck = service.get("healthcheck")
        if not isinstance(healthcheck, dict):
            raise ContractError(f"healthcheck missing for {service_name}")
        health_test = healthcheck.get("test")
        if (
            not isinstance(health_test, list)
            or EXPECTED_HEALTH_MARKERS[service_name]
            not in " ".join(str(item) for item in health_test)
            or healthcheck.get("disable") is True
        ):
            raise ContractError(
                f"specific healthcheck drift for {service_name}"
            )
        if network_names(service) != EXPECTED_SERVICE_NETWORKS[service_name]:
            raise ContractError(f"network drift for {service_name}")
        deploy = service.get("deploy", {})
        if (
            "node.labels.platform.observability == true"
            not in deploy.get("placement", {}).get("constraints", [])
        ):
            raise ContractError(f"placement drift for {service_name}")
        mode = deploy.get("mode")
        if service_name in GLOBAL_SERVICES:
            if mode != "global" or "replicas" in deploy:
                raise ContractError(f"global mode drift for {service_name}")
        elif mode != "replicated" or deploy.get("replicas") != 1:
            raise ContractError(f"replica mode drift for {service_name}")
        if deploy.get("update_config", {}).get("failure_action") != "rollback":
            raise ContractError(f"rollback-on-update missing for {service_name}")
        if deploy.get("rollback_config", {}).get("order") != "stop-first":
            raise ContractError(f"rollback order drift for {service_name}")
        resources = deploy.get("resources", {})
        if not resources.get("limits") or not resources.get("reservations"):
            raise ContractError(f"resource budget missing for {service_name}")
        labels = deploy.get("labels", {})
        if (
            labels.get("com.apptolast.managed-by") != "ansible"
            or labels.get("com.apptolast.platform-class")
            != "internal-platform"
            or labels.get("com.apptolast.component")
            != IMAGE_COMPONENT[service_name]
        ):
            raise ContractError(f"platform labels drift for {service_name}")
        environment = service.get("environment", {})
        if any(
            "PASSWORD" in key
            and not key.endswith("_FILE")
            and not key.endswith("__FILE")
            for key in environment
        ):
            raise ContractError(
                f"plaintext password environment key in {service_name}"
            )

    direct_socket_consumers = {
        name
        for name, service in services.items()
        if "/var/run/docker.sock" in bind_sources(service)
    }
    if direct_socket_consumers:
        raise ContractError("a service consumes the Docker daemon socket")
    if any(
        mount.get("target") == "/var/run/docker.sock"
        for service in services.values()
        for mount in bind_mounts(service)
    ):
        raise ContractError("a service exposes the Docker daemon socket path")
    restricted_socket_consumers = {
        name
        for name, service in services.items()
        if RESTRICTED_DOCKER_SOCKET in bind_sources(service)
    }
    if restricted_socket_consumers != {"alloy"}:
        raise ContractError("restricted Docker proxy consumer drift")
    alloy_proxy_mounts = [
        item
        for item in bind_mounts(services["alloy"])
        if item.get("source") == RESTRICTED_DOCKER_SOCKET
    ]
    if (
        len(alloy_proxy_mounts) != 1
        or alloy_proxy_mounts[0].get("target")
        != "/run/docker-readonly/docker.sock"
        or alloy_proxy_mounts[0].get("read_only") is not True
    ):
        raise ContractError("Alloy Docker proxy mount is not read-only")

    data_paths = {
        item["component"]: item["path"]
        for item in observability.get("data_paths", [])
    }
    expected_binds = {
        "prometheus": {data_paths["prometheus"]},
        "alertmanager": {data_paths["alertmanager"]},
        "blackbox-exporter": set(),
        "loki": {data_paths["loki"]},
        "alloy": {RESTRICTED_DOCKER_SOCKET, data_paths["alloy"]},
        "grafana": {data_paths["grafana"]},
        "node-exporter": {
            "/",
            "/var/lib/node_exporter/textfile_collector",
        },
        "cadvisor": {
            "/",
            "/sys",
            "/var/lib/docker",
        },
        "postgres-exporter-n8n": set(),
        "postgres-exporter-passbolt": set(),
        "postgres-exporter-shlink": set(),
        "redis-exporter": set(),
    }
    for name, expected in expected_binds.items():
        if bind_sources(services[name]) != expected:
            raise ContractError(f"bind mount drift for {name}")
    cadvisor_command = services["cadvisor"].get("command", [])
    cadvisor_command_text = " ".join(str(item) for item in cadvisor_command)
    if (
        "--store_container_labels=false" not in cadvisor_command
        or "--disable_metrics=" not in cadvisor_command_text
        or "--docker_only" in cadvisor_command_text
        or "--whitelisted_container_labels" in cadvisor_command_text
    ):
        raise ContractError("socket-free cAdvisor command drift")
    prometheus_command = services["prometheus"].get("command", [])
    if any(
        item in {"--web.enable-admin-api", "--web.enable-lifecycle"}
        or str(item).startswith("--web.enable-admin-api=")
        or str(item).startswith("--web.enable-lifecycle=")
        for item in prometheus_command
    ):
        raise ContractError("Prometheus mutation endpoint enabled")
    grafana_environment = services["grafana"].get("environment", {})
    if (
        grafana_environment.get("GF_PLUGINS_PREINSTALL_DISABLED") != "true"
        or grafana_environment.get("GF_PLUGINS_PREINSTALL_AUTO_UPDATE")
        != "false"
    ):
        raise ContractError("Grafana immutable plugin policy drift")

    networks = stack.get("networks")
    if not isinstance(networks, dict) or set(networks) != EXPECTED_NETWORKS:
        raise ContractError("observability network set changed")
    monitoring = networks["monitoring"]
    if (
        monitoring.get("name") != "apptolast-observability"
        or monitoring.get("driver") != "overlay"
        or monitoring.get("internal") is not True
        or monitoring.get("attachable") is True
        or monitoring.get("driver_opts", {}).get("encrypted") != ""
    ):
        raise ContractError("internal encrypted monitoring network drift")
    expected_external_networks = {
        "edge-monitoring": platform.get(
            "platform_edge_monitoring_network"
        ),
        "n8n-monitoring": "apptolast-n8n-monitoring",
        "workloads-n8n-backend": "workloads_n8n-backend",
        "workloads-n8n-coordination": "workloads_n8n-coordination",
        "workloads-passbolt-backend": "workloads_passbolt-backend",
        "workloads-shlink-backend": "workloads_shlink-backend",
        "minecraft-monitoring": "apptolast-minecraft-monitoring",
    }
    for alias, external_name in expected_external_networks.items():
        network = networks[alias]
        if (
            network.get("external") is not True
            or network.get("name") != external_name
        ):
            raise ContractError(f"external network drift: {alias}")

    entries = secret_catalog.get("observability_secrets")
    version = secret_catalog.get("observability_secret_version")
    if (
        not isinstance(entries, list)
        or version != "v1"
        or len(entries) != 5
    ):
        raise ContractError("observability secret catalog is incomplete")
    by_key = {item.get("key"): item for item in entries}
    if set(by_key) != set(EXPECTED_SECRET_CONSUMERS):
        raise ContractError("observability secret identities changed")
    declared_secrets = stack.get("secrets")
    if not isinstance(declared_secrets, dict) or set(declared_secrets) != set(
        by_key
    ):
        raise ContractError("stack secret aliases differ from secret catalog")
    for key, expected_consumers in EXPECTED_SECRET_CONSUMERS.items():
        entry = by_key[key]
        declaration = declared_secrets[key]
        if (
            entry.get("consumers") != sorted(expected_consumers)
            or entry.get("source_file") != key
            or entry.get("generated_bytes") not in {32, 48}
            or not entry.get("external_name", "").endswith("-v1")
            or declaration.get("external") is not True
            or declaration.get("name") != entry.get("external_name")
        ):
            raise ContractError(f"secret contract drift: {key}")
        actual_consumers = {
            name
            for name, service in services.items()
            if key in secret_sources(service)
        }
        if actual_consumers != expected_consumers:
            raise ContractError(f"secret consumer drift: {key}")

    raw_stack = str(stack).lower()
    for forbidden in (
        "monitoring-dozzle",
        "kube-system",
        "longhorn-system",
        "openclaw-legacy",
    ):
        if forbidden in raw_stack:
            raise ContractError(f"legacy component leaked into stack: {forbidden}")


def validate_configs(
    stack: dict[str, Any],
    service_catalog: dict[str, Any],
    config_dir: Path,
) -> None:
    declarations = stack.get("configs")
    if not isinstance(declarations, dict) or set(declarations) != set(
        EXPECTED_CONFIG_DESTINATIONS
    ):
        raise ContractError("immutable Docker Config set changed")
    for key, relative_name in EXPECTED_CONFIG_DESTINATIONS.items():
        source = config_dir / relative_name
        try:
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
        except OSError as exc:
            raise ContractError(f"cannot read config source: {source}") from exc
        expected_name = f"observability-{key.replace('_', '-')}-{digest[:16]}"
        declaration = declarations[key]
        if (
            declaration.get("external") is not True
            or declaration.get("name") != expected_name
        ):
            raise ContractError(f"immutable Docker Config drift: {key}")

    prometheus = load_yaml(config_dir / "prometheus.yml")
    scrape_configs = prometheus.get("scrape_configs")
    if not isinstance(scrape_configs, list):
        raise ContractError("Prometheus scrape_configs is absent")
    jobs = {item.get("job_name"): item for item in scrape_configs}
    if set(jobs) != EXPECTED_JOBS:
        raise ContractError("Prometheus scrape job set changed")
    for job_name, expected_targets in EXPECTED_STATIC_TARGETS.items():
        if static_targets(jobs[job_name]) != expected_targets:
            raise ContractError(
                f"Prometheus scrape target drift: {job_name}"
            )
    if (
        jobs["node-exporter"].get("dns_sd_configs")
        != [
            {
                "names": ["tasks.node-exporter"],
                "type": "A",
                "port": 9100,
                "refresh_interval": "30s",
            }
        ]
        or jobs["cadvisor"].get("dns_sd_configs")
        != [
            {
                "names": ["tasks.cadvisor"],
                "type": "A",
                "port": 8080,
                "refresh_interval": "30s",
            }
        ]
        or prometheus.get("rule_files")
        != ["/etc/prometheus/rules/platform.yml"]
        or prometheus.get("alerting", {}).get("alertmanagers")
        != [{"static_configs": [{"targets": ["alertmanager:9093"]}]}]
    ):
        raise ContractError("Prometheus discovery, rules or alerting drift")
    public_configs = jobs["blackbox-http-public"].get("static_configs", [])
    public_targets = {
        target
        for item in public_configs
        for target in item.get("targets", [])
    }
    catalog_probe_services = {
        service["id"]: service
        for service in service_catalog["approved_services"]
        if service["id"] in EXPECTED_PUBLIC_PROBE_PATHS
    }
    if set(catalog_probe_services) != set(EXPECTED_PUBLIC_PROBE_PATHS):
        raise ContractError("public health-probe service scope changed")
    expected_public_targets = {
        f"https://{hostname}{EXPECTED_PUBLIC_PROBE_PATHS[service_id]}"
        for service_id, service in catalog_probe_services.items()
        for hostname in service["hostnames"]
    }
    if public_targets != expected_public_targets:
        raise ContractError("public blackbox probes differ from service catalog")
    minecraft_targets = (
        jobs["blackbox-minecraft-tcp"]
        .get("static_configs", [{}])[0]
        .get("targets")
    )
    if minecraft_targets != ["workloads_minecraft:25565"]:
        raise ContractError("Minecraft TCP probe drift")

    rules = load_yaml(config_dir / "prometheus-alerts.yml")
    alert_names = {
        rule.get("alert")
        for group in rules.get("groups", [])
        for rule in group.get("rules", [])
    }
    if alert_names != EXPECTED_ALERTS:
        raise ContractError("Prometheus alert rule set changed")
    alert_rules = {
        rule.get("alert"): rule
        for group in rules.get("groups", [])
        for rule in group.get("rules", [])
    }
    disabled_delivery = alert_rules["ExternalAlertDeliveryDisabled"]
    if (
        disabled_delivery.get("expr") != "vector(1)"
        or disabled_delivery.get("labels", {}).get("severity") != "info"
        or "receiver" not in (
            disabled_delivery.get("annotations", {}).get("description", "")
        )
    ):
        raise ContractError("disabled external alert delivery marker drift")

    alertmanager = load_yaml(config_dir / "alertmanager.yml")
    if (
        alertmanager.get("receivers")
        != [{"name": "notifications-disabled"}]
        or alertmanager.get("route", {}).get("receiver")
        != "notifications-disabled"
    ):
        raise ContractError("Alertmanager internal-only receiver drift")
    forbidden_receiver_keys = {
        "email_configs",
        "webhook_configs",
        "slack_configs",
        "pagerduty_configs",
        "opsgenie_configs",
        "telegram_configs",
    }
    if any(
        forbidden_receiver_keys.intersection(receiver)
        for receiver in alertmanager.get("receivers", [])
    ):
        raise ContractError("unreviewed external Alertmanager receiver found")

    blackbox = load_yaml(config_dir / "blackbox.yml")
    if set(blackbox.get("modules", {})) != {"http_success", "tcp_connect"}:
        raise ContractError("Blackbox module set changed")
    blackbox_http = blackbox["modules"]["http_success"].get("http", {})
    if (
        blackbox_http.get("tls_config", {}).get("insecure_skip_verify")
        is not False
        or blackbox_http.get("valid_status_codes") != [200]
        or blackbox_http.get("follow_redirects") is not False
    ):
        raise ContractError("Blackbox exact HTTP health contract drift")

    loki = load_yaml(config_dir / "loki.yml")
    schema = loki.get("schema_config", {}).get("configs", [])
    if (
        len(schema) != 1
        or schema[0].get("store") != "tsdb"
        or schema[0].get("schema") != "v13"
        or schema[0].get("object_store") != "filesystem"
        or schema[0].get("index", {}).get("period") != "24h"
        or loki.get("limits_config", {}).get("retention_period") != "336h"
        or loki.get("compactor", {}).get("retention_enabled") is not True
    ):
        raise ContractError("Loki TSDB or retention contract drift")

    alloy_text = (config_dir / "alloy.alloy").read_text(encoding="utf-8")
    for required in (
        'host             = "unix:///run/docker-readonly/docker.sock"',
        'regex  = "(edge|workloads|observability)_.+"',
        'url = "http://loki:3100/loki/api/v1/push"',
    ):
        if required not in alloy_text:
            raise ContractError("Alloy managed log pipeline drift")
    for forbidden in ("dozzle", "kubernetes", "openclaw-legacy"):
        if forbidden in alloy_text.lower():
            raise ContractError(f"legacy Alloy pipeline found: {forbidden}")

    datasources = load_yaml(config_dir / "grafana-datasources.yml")
    if datasources.get("apiVersion") != 1 or datasources.get("prune") is not True:
        raise ContractError("Grafana datasource lifecycle drift")
    datasource_identities = {
        (item.get("name"), item.get("uid"), item.get("url"))
        for item in datasources.get("datasources", [])
    }
    if datasource_identities != {
        ("Prometheus", "prometheus", "http://prometheus:9090"),
        ("Loki", "loki", "http://loki:3100"),
    }:
        raise ContractError("Grafana datasource provisioning drift")
    dashboard_provider = load_yaml(config_dir / "grafana-dashboards.yml")
    providers = dashboard_provider.get("providers", [])
    if (
        len(providers) != 1
        or providers[0].get("editable") is not False
        or providers[0].get("options", {}).get("path")
        != "/var/lib/grafana/dashboards"
    ):
        raise ContractError("Grafana dashboard provider drift")
    dashboard_uids = {
        load_json(config_dir / relative_name).get("uid")
        for relative_name in (
            "dashboards/platform-overview.json",
            "dashboards/platform-logs.json",
        )
    }
    if dashboard_uids != {
        "apptolast-platform-overview",
        "apptolast-platform-logs",
    }:
        raise ContractError("Grafana dashboard identity drift")
    overview_text = (
        config_dir / "dashboards/platform-overview.json"
    ).read_text(encoding="utf-8")
    if (
        "container_label_com_docker" in overview_text
        or "/system.slice/docker-[0-9a-f]{64}" not in overview_text
    ):
        raise ContractError("Grafana socket-free cAdvisor dashboard drift")


def validate_operational_helpers(repository_root: Path) -> None:
    """Enforce stdio-only access and the restricted host socket boundary."""
    remote = (
        repository_root / "scripts/observability-forward-remote.py"
    ).read_text(encoding="utf-8")
    local = (
        repository_root / "scripts/observability-tunnel.py"
    ).read_text(encoding="utf-8")
    wrapper = (
        repository_root / "scripts/observability-tunnel.sh"
    ).read_text(encoding="utf-8")
    proxy = (
        repository_root / "scripts/docker-readonly-proxy.py"
    ).read_text(encoding="utf-8")
    sudoers = (
        repository_root
        / "ansible/roles/observability/templates/"
        "observability-forward.sudoers.j2"
    ).read_text(encoding="utf-8")
    proxy_unit = (
        repository_root
        / "ansible/roles/observability/templates/"
        "docker-readonly-proxy.service.j2"
    ).read_text(encoding="utf-8")
    deploy_tasks = (
        repository_root
        / "ansible/roles/observability/tasks/deploy.yml"
    ).read_text(encoding="utf-8")

    for forbidden in (
        'listener.bind(("0.0.0.0"',
        "listener.listen(",
        "os.fork(",
    ):
        if forbidden in remote:
            raise ContractError("remote observability helper opens a listener")
    for required in (
        '"--stdio"',
        "os.setns(",
        '("127.0.0.1", SERVICE_PORTS[service])',
    ):
        if required not in remote:
            raise ContractError("remote stdio namespace helper drift")

    for forbidden in (
        '"-L"',
        "ExitOnForwardFailure",
        'listener.bind(("0.0.0.0"',
    ):
        if forbidden in local or forbidden in wrapper:
            raise ContractError("SSH forwarding or non-loopback access found")
    for required in (
        'listener.bind(("127.0.0.1", args.local_port))',
        '"-T"',
        '"sudo"',
        '"-n"',
        '"--stdio"',
    ):
        if required not in local:
            raise ContractError("local SSH stdio transport drift")

    if (
        proxy.count('method not in {"GET", "HEAD"}') != 1
        or '"/containers/json"' not in proxy
        or '"/networks"' not in proxy
        or 'normalized.endswith("/logs")' not in proxy
        or "HOP_BY_HOP_HEADERS" not in proxy
        or "request bodies are denied" not in proxy
    ):
        raise ContractError("read-only Docker API proxy allowlist drift")
    if (
        sudoers.count(" --stdio ") != 5
        or "NOPASSWD:" not in sudoers
        or " ALL=(ALL)" in sudoers
    ):
        raise ContractError("observability sudo command allowlist drift")
    for required in (
        "RestrictAddressFamilies=AF_UNIX",
        "NoNewPrivileges=true",
        "CapabilityBoundingSet=",
        "ProtectSystem=strict",
        "ReadOnlyPaths=/var/run/docker.sock",
    ):
        if required not in proxy_unit:
            raise ContractError("Docker API proxy systemd hardening drift")
    for required in (
        "observability_secret_installer",
        "--verify-only",
        "observability_installed_secret_catalog",
        "no_log: true",
    ):
        if required not in deploy_tasks:
            raise ContractError("secret source-to-Swarm verification drift")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", type=Path, required=True)
    parser.add_argument("--services", type=Path, required=True)
    parser.add_argument("--secrets", type=Path, required=True)
    parser.add_argument("--platform", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        stack = load_yaml(args.stack)
        services = load_yaml(args.services)
        secrets = load_yaml(args.secrets)
        platform = load_yaml(args.platform)
        validate_stack(stack, services, secrets, platform)
        validate_configs(stack, services, args.config_dir)
        validate_operational_helpers(Path(__file__).resolve().parents[1])
    except (OSError, UnicodeDecodeError, ContractError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        "Rendered internal observability contract passed: "
        "10 pinned components, 12 services and zero published ports."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
