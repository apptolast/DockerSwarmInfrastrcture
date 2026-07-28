#!/usr/bin/env python3

from __future__ import annotations

import ipaddress
import re
import sys
from pathlib import Path
from typing import Any

import yaml


PROJECT_DIR = Path(__file__).resolve().parent.parent


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def load_yaml(relative_path: str) -> Any:
    path = PROJECT_DIR / relative_path
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        fail(f"cannot read {relative_path}: {error}")


contract = load_yaml("config/platform.yml")
if not isinstance(contract, dict):
    fail("config/platform.yml must contain a mapping")

required_keys = {
    "platform_environment",
    "platform_release_version",
    "platform_node_id",
    "platform_admin_uid",
    "platform_admin_gid",
    "platform_public_ipv4",
    "platform_dns_legacy_ipv4",
    "platform_dns_cutover",
    "platform_public_ipv6",
    "platform_public_ipv6_prefix",
    "platform_public_interface",
    "platform_netcup_server_id",
    "platform_netcup_server_mac",
    "platform_swarm_data_path_port",
    "platform_swarm_default_addr_pool",
    "platform_swarm_default_addr_pool_mask_length",
    "platform_install_root",
    "platform_state_root",
    "platform_edge_monitoring_network",
    "platform_edge_networks",
    "platform_public_tcp_ports",
    "platform_minecraft_public_enabled",
    "platform_public_ipv6_tcp_ports",
    "edge_cloudflare_zone",
    "edge_traefik_hostname",
}
if set(contract) != required_keys:
    fail("the platform contract has missing or unexpected keys")

release_version = contract["platform_release_version"]
if not isinstance(release_version, str) or not re.fullmatch(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?",
    release_version,
):
    fail("platform_release_version is not a supported semantic version")

if contract["platform_node_id"] != "netcup-manager-01":
    fail("platform_node_id must remain the canonical backup/runtime identity")

try:
    public_ipv4 = ipaddress.IPv4Address(contract["platform_public_ipv4"])
except ipaddress.AddressValueError as error:
    fail(f"platform_public_ipv4 is invalid: {error}")

if str(public_ipv4) != contract["platform_public_ipv4"]:
    fail("platform_public_ipv4 is not in canonical form")

try:
    legacy_ipv4 = ipaddress.IPv4Address(contract["platform_dns_legacy_ipv4"])
except ipaddress.AddressValueError as error:
    fail(f"platform_dns_legacy_ipv4 is invalid: {error}")
if str(legacy_ipv4) != contract["platform_dns_legacy_ipv4"]:
    fail("platform_dns_legacy_ipv4 is not in canonical form")
if legacy_ipv4 == public_ipv4:
    fail("legacy and target public IPv4 addresses must differ")

reviewed_dns_keys = {
    "edge",
    "kropia",
    "minecraft-stats",
    "minecraft",
    "n8n",
    "openclaw",
    "passbolt",
    "generadorcodigosqr",
    "pablohurtadohg",
    "albertohidalgo",
}
dns_cutover = contract["platform_dns_cutover"]
if (
    not isinstance(dns_cutover, dict)
    or set(dns_cutover) != reviewed_dns_keys
    or any(not isinstance(enabled, bool) for enabled in dns_cutover.values())
):
    fail("platform_dns_cutover must be the exact reviewed boolean allowlist")
if dns_cutover["edge"] is not True:
    fail("the new edge health record must target the managed server")

try:
    public_ipv6 = ipaddress.IPv6Address(contract["platform_public_ipv6"])
    public_ipv6_prefix = ipaddress.IPv6Network(
        contract["platform_public_ipv6_prefix"],
        strict=True,
    )
except (ipaddress.AddressValueError, ipaddress.NetmaskValueError) as error:
    fail(f"the reviewed public IPv6 contract is invalid: {error}")

if public_ipv6 not in public_ipv6_prefix:
    fail("platform_public_ipv6 is outside platform_public_ipv6_prefix")
if str(public_ipv6) != contract["platform_public_ipv6"]:
    fail("platform_public_ipv6 is not in canonical form")
if str(public_ipv6_prefix) != contract["platform_public_ipv6_prefix"]:
    fail("platform_public_ipv6_prefix is not in canonical form")

server_id = contract["platform_netcup_server_id"]
if not isinstance(server_id, int) or server_id <= 0:
    fail("platform_netcup_server_id must be a positive integer")

if (
    contract["platform_admin_uid"] != 1001
    or contract["platform_admin_gid"] != 1001
):
    fail("the reviewed admin UID/GID contract must remain 1001:1001")

server_mac = contract["platform_netcup_server_mac"]
if not isinstance(server_mac, str) or not re.fullmatch(
    r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}",
    server_mac,
):
    fail("platform_netcup_server_mac is not a canonical lowercase MAC")

hostname = contract["edge_traefik_hostname"]
if not isinstance(hostname, str) or not re.fullmatch(
    r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", hostname
):
    fail("edge_traefik_hostname is invalid")

cloudflare_zone = contract["edge_cloudflare_zone"]
if not isinstance(cloudflare_zone, str) or not re.fullmatch(
    r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", cloudflare_zone
):
    fail("edge_cloudflare_zone is invalid")
if hostname != cloudflare_zone and not hostname.endswith(f".{cloudflare_zone}"):
    fail("edge_traefik_hostname is outside edge_cloudflare_zone")

public_ports = contract["platform_public_tcp_ports"]
if public_ports != [80, 443, 25565]:
    fail(
        "the reviewed public port contract must remain exactly "
        "[80, 443, 25565]"
    )

if not isinstance(contract["platform_minecraft_public_enabled"], bool):
    fail("platform_minecraft_public_enabled must be boolean")
if (
    dns_cutover["minecraft"]
    != contract["platform_minecraft_public_enabled"]
):
    fail("Minecraft DNS cutover and public-ingress gates must change together")
minecraft_document = load_yaml("config/minecraft.yml")
if (
    not isinstance(minecraft_document, dict)
    or not isinstance(minecraft_document.get("minecraft_contract"), dict)
):
    fail("config/minecraft.yml has no minecraft_contract mapping")
if (
    dns_cutover["minecraft"]
    and minecraft_document["minecraft_contract"].get("online_mode") is not True
):
    fail("Minecraft cannot cut over publicly while online_mode is false")
if contract["platform_public_ipv6_tcp_ports"] != []:
    fail("public application TCP over IPv6 is outside the reviewed contract")

if contract["platform_swarm_data_path_port"] in public_ports:
    fail("the Swarm data-path port cannot be public")

address_pools = contract["platform_swarm_default_addr_pool"]
if not isinstance(address_pools, list) or len(address_pools) != 1:
    fail("exactly one reviewed Swarm default address pool is required")

try:
    swarm_pool = ipaddress.IPv4Network(address_pools[0], strict=True)
except (ipaddress.AddressValueError, ipaddress.NetmaskValueError) as error:
    fail(f"the Swarm address pool is invalid: {error}")

pool_mask = contract["platform_swarm_default_addr_pool_mask_length"]
if not isinstance(pool_mask, int) or not swarm_pool.prefixlen < pool_mask <= 30:
    fail("the Swarm subnet mask length is invalid")

inventory = load_yaml("ansible/inventory/production/hosts.yml")
inventory_ip = inventory["all"]["children"]["swarm_managers"]["hosts"][
    "netcup-manager-01"
]["ansible_host"]
if inventory_ip != str(public_ipv4):
    fail("the production inventory IP differs from the platform contract")

group_vars = load_yaml("ansible/group_vars/all.yml")
duplicated_contract_keys = required_keys.intersection(group_vars)
if duplicated_contract_keys:
    fail(
        "group_vars duplicates contract keys: "
        + ", ".join(sorted(duplicated_contract_keys))
    )

render_dir = PROJECT_DIR / ".build/edge"
for rendered_name in ("stack.yml", "static.yml", "dynamic.yml"):
    if not (render_dir / rendered_name).is_file():
        fail(f"missing rendered edge artifact: {rendered_name}")

stack = load_yaml(".build/edge/stack.yml")
traefik_service = stack["services"]["traefik"]
published = {
    (item["published"], item["target"], item["mode"], item["protocol"])
    for item in traefik_service["ports"]
}
expected_published = {
    (80, 8000, "host", "tcp"),
    (443, 8443, "host", "tcp"),
}
if published != expected_published:
    fail(f"rendered edge ports differ from the contract: {published}")

expected_edge_networks = {
    "kropia": "apptolast-edge-kropia",
    "minecraft-stats": "apptolast-edge-minecraft-stats",
    "n8n": "apptolast-edge-n8n",
    "openclaw": "apptolast-edge-openclaw",
    "passbolt": "apptolast-edge-passbolt",
    "portfolio-alberto": "apptolast-edge-portfolio-alberto",
    "portfolio-pablo": "apptolast-edge-portfolio-pablo",
    "shlink": "apptolast-edge-shlink",
}
if contract["platform_edge_networks"] != expected_edge_networks:
    fail("the isolated edge network map differs from the reviewed contract")
if (
    contract["platform_edge_monitoring_network"]
    != "apptolast-edge-monitoring"
):
    fail("the dedicated edge monitoring network differs from the contract")

expected_stack_networks = {
    f"edge-{key}": {"external": True, "name": value}
    for key, value in expected_edge_networks.items()
}
expected_stack_networks["edge-monitoring"] = {
    "external": True,
    "name": contract["platform_edge_monitoring_network"],
}
if stack["networks"] != expected_stack_networks:
    fail("the rendered edge networks differ from the isolation contract")
if set(traefik_service["networks"]) != set(expected_stack_networks):
    fail("Traefik is not attached to every and only reviewed edge network")

if not re.fullmatch(
    r"traefik@sha256:[a-f0-9]{64}", traefik_service["image"]
):
    fail("the rendered Traefik image is not pinned by digest")

dynamic = load_yaml(".build/edge/dynamic.yml")
health_router = dynamic["http"]["routers"]["edge-health"]
expected_rule = f"Host(`{hostname}`) && Path(`/ping`)"
if health_router["rule"] != expected_rule:
    fail("the rendered health hostname differs from the platform contract")

service_catalog = load_yaml("config/services.yml")
approved_by_id = {
    service["id"]: service for service in service_catalog["approved_services"]
}
edge_routes = {
    "kropia": ("kropia", "http://workloads_kropia:80"),
    "minecraft-stats": (
        "minecraft-stats",
        "http://workloads_minecraft-stats:8080",
    ),
    "n8n": ("n8n", "http://workloads_n8n:5678"),
    "openclaw": ("openclaw-clean", "http://workloads_openclaw:18789"),
    "passbolt": ("passbolt", "http://workloads_passbolt:80"),
    "portfolio-pablo": (
        "personal-website-pablo",
        "http://workloads_portfolio-pablo:3000",
    ),
    "portfolio-alberto": (
        "personal-website-alberto",
        "http://workloads_portfolio-alberto:3000",
    ),
    "shlink": ("shlink", "http://workloads_shlink:8080"),
}
if set(dynamic["http"]["routers"]) != {
    "edge-health",
    "edge-ping-internal",
    *edge_routes,
}:
    fail("the rendered edge router allowlist differs from the service catalog")
if set(dynamic["http"]["services"]) != set(edge_routes):
    fail("the rendered edge backend allowlist differs from the service catalog")
for route, (service_id, upstream) in edge_routes.items():
    expected_hostnames = approved_by_id[service_id]["hostnames"]
    if len(expected_hostnames) != 1:
        fail(f"{service_id} must have exactly one edge hostname")
    if (
        dynamic["http"]["routers"][route]["rule"]
        != f"Host(`{expected_hostnames[0]}`)"
    ):
        fail(f"the rendered {route} hostname differs from the service catalog")
    servers = dynamic["http"]["services"][route]["loadBalancer"]["servers"]
    if servers != [{"url": upstream}]:
        fail(f"the rendered {route} upstream differs from the Swarm contract")

static = load_yaml(".build/edge/static.yml")
if static.get("ping") != {
    "entryPoint": "traefik",
    "manualRouting": True,
}:
    fail("Traefik ping must use the explicitly routed internal service")
for entrypoint in ("web", "websecure", "traefik", "metrics"):
    encoded = static["entryPoints"][entrypoint]["http"]["encodedCharacters"]
    if set(encoded.values()) != {False}:
        fail(f"{entrypoint} does not reject every reviewed encoded character")
    if (
        static["entryPoints"][entrypoint]["http"]["underscoreHeadersStrategy"]
        != "reject"
    ):
        fail(f"{entrypoint} does not reject underscore-form headers")

cloudflare_dns = (
    PROJECT_DIR
    / "infra/terraform/cloudflare/apptolast-dns/dns.tf"
).read_text(encoding="utf-8")
if "local.platform_contract.edge_traefik_hostname" not in cloudflare_dns:
    fail("Cloudflare DNS does not consume the shared hostname contract")
if "local.platform_contract.platform_public_ipv4" not in cloudflare_dns:
    fail("Cloudflare DNS does not consume the shared IPv4 contract")

print("Cross-layer platform contract validation passed.")
