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
    "platform_public_ipv4",
    "platform_public_interface",
    "platform_swarm_data_path_port",
    "platform_swarm_default_addr_pool",
    "platform_swarm_default_addr_pool_mask_length",
    "platform_install_root",
    "platform_state_root",
    "platform_edge_network",
    "platform_public_tcp_ports",
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

try:
    public_ipv4 = ipaddress.IPv4Address(contract["platform_public_ipv4"])
except ipaddress.AddressValueError as error:
    fail(f"platform_public_ipv4 is invalid: {error}")

if str(public_ipv4) != contract["platform_public_ipv4"]:
    fail("platform_public_ipv4 is not in canonical form")

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
if public_ports != [80, 443]:
    fail("the reviewed public port contract must remain exactly [80, 443]")

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

if stack["networks"]["edge"]["name"] != contract["platform_edge_network"]:
    fail("the rendered edge network differs from the platform contract")

if not re.fullmatch(
    r"traefik@sha256:[a-f0-9]{64}", traefik_service["image"]
):
    fail("the rendered Traefik image is not pinned by digest")

dynamic = load_yaml(".build/edge/dynamic.yml")
health_router = dynamic["http"]["routers"]["edge-health"]
expected_rule = f"Host(`{hostname}`) && Path(`/ping`)"
if health_router["rule"] != expected_rule:
    fail("the rendered health hostname differs from the platform contract")

static = load_yaml(".build/edge/static.yml")
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
