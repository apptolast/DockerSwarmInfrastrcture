#!/usr/bin/env python3

from __future__ import annotations

import ipaddress
import re
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
    "platform_minecraft_offline_public_accepted",
    "platform_parked_workloads",
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
if not isinstance(
    contract["platform_minecraft_offline_public_accepted"], bool
):
    fail("platform_minecraft_offline_public_accepted must be boolean")
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
# Publicar con `online_mode: false` deja el 25565 sin autenticacion de
# Mojang/Microsoft. La compuerta sigue cerrada por defecto y solo la abre una
# aceptacion explicita del propietario registrada en el contrato, de modo que
# la decision queda auditable en el repositorio en lugar de desaparecer.
if (
    dns_cutover["minecraft"]
    and minecraft_document["minecraft_contract"].get("online_mode") is not True
    and contract["platform_minecraft_offline_public_accepted"] is not True
):
    fail(
        "Minecraft cannot cut over publicly while online_mode is false "
        "unless platform_minecraft_offline_public_accepted is true"
    )
# Aparcar deja un servicio en `replicas: 0`. Solo se admiten servicios sin
# dependientes: aparcar una base de datos romperia a sus consumidores.
parked_workloads = contract["platform_parked_workloads"]
if (
    not isinstance(parked_workloads, list)
    or any(not isinstance(name, str) for name in parked_workloads)
    or parked_workloads != sorted(set(parked_workloads))
    or not set(parked_workloads) <= {"minecraft", "openclaw"}
):
    fail(
        "platform_parked_workloads must be a sorted, duplicate-free subset "
        "of the reviewed parkable services [minecraft, openclaw]"
    )
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
expected_stack_networks["edge-organizationweb"] = {
    "external": True,
    "name": "apptolast-edge-organizationweb",
}
expected_stack_networks["edge-racinggame"] = {
    "external": True,
    "name": "apptolast-edge-racinggame",
}
# The monitoring dashboard lives outside this repository; only its
# ingress is reviewed here.
expected_stack_networks["edge-observatorio"] = {
    "external": True,
    "name": "apptolast-edge-observatorio",
}
# Satisfactory also lives outside this repository; only its ingress is
# reviewed here.
expected_stack_networks["edge-satisfactory"] = {
    "external": True,
    "name": "apptolast-edge-satisfactory",
}
# The AX web panel runs in the kind lab; its TCP forwarder, a plain
# container, is the only other member of this network.
expected_stack_networks["edge-ax"] = {
    "external": True,
    "name": "apptolast-edge-ax",
}
if stack["networks"] != expected_stack_networks:
    fail("the rendered edge networks differ from the isolation contract")
if set(traefik_service["networks"]) != set(expected_stack_networks):
    fail("Traefik is not attached to every and only reviewed edge network")
if group_vars.get("edge_adopted_attachable_networks") != [
    "apptolast-edge-observatorio",
    "apptolast-edge-satisfactory",
    "apptolast-edge-ax",
]:
    fail("the adopted attachable edge networks differ from the contract")
# The AX panel's forwarder admits only this subnet (docs/EDGE.md, «Ruta de
# AX»); Swarm allocates every other edge subnet.
if group_vars.get("edge_network_subnets") != {"apptolast-edge-ax": "10.0.250.0/24"}:
    fail("the fixed edge network subnets differ from the contract")

# The ACME token, one users file per basicAuth middleware and the mTLS
# material of the AX upstream. Only the versioned name of each Docker
# Secret is in Git; the users files, and so every password hash, and the
# certificates and key exist only on the host.
basicauth_secrets = {
    "basicauth_satisfactory_logs": "edge-basicauth-satisfactory-logs-v1",
    "basicauth_ax": "edge-basicauth-ax-v1",
}
if group_vars.get("edge_traefik_basicauth_secrets") != basicauth_secrets:
    fail("the basicAuth users file secrets differ from the reviewed map")
upstream_mtls_secrets = {
    "ax_upstream_ca": "edge-ax-upstream-ca-v1",
    "ax_upstream_client": "edge-ax-upstream-client-v1",
}
if group_vars.get("edge_traefik_upstream_mtls_secrets") != upstream_mtls_secrets:
    fail("the upstream mTLS secrets differ from the reviewed map")
expected_stack_secrets = {
    "cloudflare_dns_api_token": {
        "external": True,
        "name": group_vars["edge_traefik_cloudflare_secret_name"],
    },
    **{
        target: {"external": True, "name": name}
        for target, name in {**basicauth_secrets, **upstream_mtls_secrets}.items()
    },
}
if stack.get("secrets") != expected_stack_secrets:
    fail("the rendered edge secrets differ from the reviewed contract")
if traefik_service.get("secrets") != [
    {
        "source": target,
        "target": target,
        "uid": "65532",
        "gid": "65532",
        "mode": 0o400,
    }
    for target in [
        "cloudflare_dns_api_token",
        *sorted(basicauth_secrets),
        *sorted(upstream_mtls_secrets),
    ]
]:
    fail("Traefik does not mount exactly the reviewed read-only secrets")

def load_image_channels() -> dict[str, Any]:
    import importlib.util

    path = PROJECT_DIR / "scripts/validate-image-channels.py"
    spec = importlib.util.spec_from_file_location("validate_image_channels", path)
    if spec is None or spec.loader is None:
        fail("cannot load the image channel validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.load_channel_map(PROJECT_DIR)
    except module.ChannelError as error:
        fail(f"image channel map: {error}")
    raise AssertionError("unreachable")


traefik_channel = load_image_channels()["services"].get("edge", {}).get("traefik")
if not isinstance(traefik_channel, dict):
    fail("the Traefik image channel entry is missing")
if traefik_service["image"] != traefik_channel["reference"]:
    fail("the rendered Traefik image differs from its image channel entry")
if traefik_channel["reference"] != group_vars["edge_traefik_image"] and not (
    re.fullmatch(
        r"(docker\.io/library/)?traefik:v3(@sha256:[a-f0-9]{64})?",
        traefik_channel["reference"],
    )
):
    fail("the Traefik image channel is neither the pin nor traefik:v3")
if not re.fullmatch(
    r"traefik@sha256:[a-f0-9]{64}", group_vars["edge_traefik_image"]
):
    fail("the reviewed Traefik baseline is not pinned by digest")
traefik_labels = traefik_service.get("deploy", {}).get("labels", {})
if traefik_labels.get("apptolast.autoupdate") != traefik_channel["label"]:
    fail("the rendered Traefik autoupdate label differs from its channel entry")

dynamic = load_yaml(".build/edge/dynamic.yml")
# Only HTTP routing and the TLS options: no TCP/UDP router can bypass the
# reviewed HTTP routers and middlewares.
if set(dynamic) != {"http", "tls"}:
    fail("the rendered dynamic configuration has unreviewed top-level sections")
if set(dynamic["http"]) != {
    "routers",
    "middlewares",
    "services",
    "serversTransports",
}:
    fail("the rendered dynamic HTTP configuration has unreviewed sections")
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
# Satisfactory runs outside this repository. Its four routes are pinned
# exactly as they ran in the hand-made Docker Config of 2026-09-22, except
# that the logs login reads its users from a Docker Secret.
satisfactory_host = "Host(`satisfactory.apptolast.com`)"
satisfactory_routers = {
    "satisfactory-web": {
        "rule": satisfactory_host,
        "entryPoints": ["websecure"],
        "middlewares": ["edge-security"],
        "service": "satisfactory-web",
        "tls": {"certResolver": "letsencrypt"},
    },
    "satisfactory-ws": {
        "rule": satisfactory_host + " && PathPrefix(`/app/`)",
        "priority": 100,
        "entryPoints": ["websecure"],
        "middlewares": ["edge-security"],
        "service": "satisfactory-reverb",
        "tls": {"certResolver": "letsencrypt"},
    },
    "satisfactory-companions": {
        "rule": satisfactory_host
        + " && (Path(`/companions`) || PathPrefix(`/companions/`))",
        "priority": 120,
        "entryPoints": ["websecure"],
        "middlewares": ["edge-security"],
        "service": "satisfactory-companions",
        "tls": {"certResolver": "letsencrypt"},
    },
    "satisfactory-logs": {
        "rule": "Host(`logs-satisfactory.apptolast.com`)",
        "entryPoints": ["websecure"],
        "middlewares": ["edge-security", "satisfactory-log-auth"],
        "service": "satisfactory-logs",
        "tls": {"certResolver": "letsencrypt"},
    },
}
satisfactory_services = {
    name: {"loadBalancer": {"servers": [{"url": url}]}}
    for name, url in {
        "satisfactory-web": "http://satisfactory-web:80",
        "satisfactory-reverb": "http://satisfactory-reverb:8080",
        "satisfactory-companions": "http://satisfactory-companions_web:8080",
        "satisfactory-logs": "http://satisfactory-logs:8080",
    }.items()
}
# The AX web panel (docs/EDGE.md, «Ruta de AX»). The Host is made canonical
# first: rateLimit and inFlightReq group `requestHost` by the raw Host, which
# the router matches case-insensitively and without its port, so every
# variant would get fresh counters. Its own limits run before the bcrypt of
# basicAuth, and both rate limits before inFlightReq: a rate limiter holds a
# delayed request for up to 0.5 s, which must not occupy an in-flight slot.
# No compress middleware: it holds back the first bytes of the panel's live
# output (SSE). Only `GET /healthz` skips the login, and it never carries
# the browser's Authorization header to the panel.
ax_hostname = "ax.apptolast.com"
ax_host = f"Host(`{ax_hostname}`)"
ax_limits = [
    "edge-security",
    "ax-canonical-host",
    "ax-rl-ip",
    "ax-rl-host",
    "ax-inflight",
]
ax_routers = {
    "ax": {
        "rule": ax_host,
        "entryPoints": ["websecure"],
        "middlewares": [*ax_limits, "ax-auth"],
        "service": "ax",
        "tls": {"certResolver": "letsencrypt"},
    },
    "ax-health": {
        "rule": ax_host + " && Path(`/healthz`) && Method(`GET`)",
        "entryPoints": ["websecure"],
        "middlewares": [*ax_limits, "ax-strip-authorization"],
        "service": "ax",
        "tls": {"certResolver": "letsencrypt"},
    },
}
# The bursts are what one page load of the panel needs. Traefik recreates a
# source's bucket full after 3 s (per IP) or 2 s (host) without requests,
# so the real caps are about 30 requests every 2-3 s per IP and 10 every
# 1-2 s for the host; docs/EDGE.md states them and the bcrypt budget.
ax_middlewares = {
    "ax-canonical-host": {
        "headers": {"customRequestHeaders": {"Host": ax_hostname}},
    },
    "ax-rl-ip": {
        "rateLimit": {
            "average": 30,
            "period": "1m",
            "burst": 30,
            "sourceCriterion": {"ipStrategy": {"ipv6Subnet": 64}},
        },
    },
    "ax-rl-host": {
        "rateLimit": {
            "average": 2,
            "period": "1s",
            "burst": 10,
            "sourceCriterion": {"requestHost": True},
        },
    },
    "ax-inflight": {"inFlightReq": {"amount": 8}},
    "ax-auth": {
        "basicAuth": {
            "usersFile": "/run/secrets/basicauth_ax",
            "realm": "AX",
            "removeHeader": True,
        },
    },
    "ax-strip-authorization": {
        "headers": {"customRequestHeaders": {"Authorization": ""}},
    },
}
# No healthCheck: a stopped panel answers 502 after the login instead of a
# WARN every interval. The forwarder passes the TLS session through intact.
ax_service = {
    "loadBalancer": {
        "passHostHeader": True,
        "serversTransport": "ax-web-mtls",
        "servers": [{"url": "https://ax-web-edge:8443"}],
    },
}
# Traefik v3.7.13 (pkg/server/service/transport.go) rejects a transport TLS
# configuration whose minVersion exceeds maxVersion, and an absent
# maxVersion counts as 0: the transport then dials with the default TLS
# configuration, without the client certificate. Hence both versions.
ax_servers_transport = {
    "serverName": "ax-web",
    "rootCAs": ["/run/secrets/ax_upstream_ca"],
    "certificates": [
        {
            "certFile": "/run/secrets/ax_upstream_client",
            "keyFile": "/run/secrets/ax_upstream_client",
        },
    ],
    "minVersion": "VersionTLS13",
    "maxVersion": "VersionTLS13",
    "forwardingTimeouts": {
        "dialTimeout": "5s",
        "responseHeaderTimeout": "60s",
        "idleConnTimeout": "180s",
    },
}
if set(dynamic["http"]["routers"]) != {
    "edge-health",
    "edge-ping-internal",
    "organizationweb",
    "racinggame",
    "monitorizacion",
    *edge_routes,
    *satisfactory_routers,
    *ax_routers,
}:
    fail("the rendered edge router allowlist differs from the service catalog")
if set(dynamic["http"]["services"]) != {
    *edge_routes,
    "organizationweb",
    "racinggame",
    "monitorizacion",
    *satisfactory_services,
    "ax",
}:
    fail("the rendered edge backend allowlist differs from the service catalog")
for name, expected_router in satisfactory_routers.items():
    if dynamic["http"]["routers"][name] != expected_router:
        fail(f"the {name} router differs from the reviewed ingress")
for name, expected_service in satisfactory_services.items():
    if dynamic["http"]["services"][name] != expected_service:
        fail(f"the {name} upstream differs from the reviewed ingress")
if set(dynamic["http"]["middlewares"]) != {
    "edge-security",
    "edge-rate-limit",
    "edge-compress",
    "edge-default",
    "passbolt-forwarded-proto",
    "passbolt-security",
    "satisfactory-log-auth",
    *ax_middlewares,
}:
    fail("the rendered edge middleware allowlist differs from the contract")
# Inline `users` take precedence over `usersFile` and would put a hash in
# this public repository, so the only reviewed shape is a secret file.
if dynamic["http"]["middlewares"]["satisfactory-log-auth"] != {
    "basicAuth": {
        "usersFile": "/run/secrets/basicauth_satisfactory_logs",
        "realm": "Satisfactory logs",
        "removeHeader": True,
    },
}:
    fail("the Satisfactory logs login differs from its users file contract")
for name, expected_router in ax_routers.items():
    if dynamic["http"]["routers"][name] != expected_router:
        fail(f"the {name} router differs from the reviewed AX ingress")
for name, expected_middleware in ax_middlewares.items():
    if dynamic["http"]["middlewares"][name] != expected_middleware:
        fail(f"the {name} middleware differs from the reviewed AX ingress")
if dynamic["http"]["services"]["ax"] != ax_service:
    fail("the ax upstream differs from the reviewed AX ingress")


def keys_anywhere(document: Any, key: str) -> bool:
    if isinstance(document, dict):
        return key in document or any(
            keys_anywhere(value, key) for value in document.values()
        )
    if isinstance(document, list):
        return any(keys_anywhere(value, key) for value in document)
    return False


# Backend TLS is verified everywhere: the only transport is the AX mTLS one,
# and only the ax backend uses it.
if keys_anywhere(dynamic, "insecureSkipVerify"):
    fail("the rendered dynamic configuration skips backend TLS verification")
if set(dynamic["http"]["serversTransports"]) != {"ax-web-mtls"}:
    fail("the rendered servers transports differ from the reviewed allowlist")
transport = dynamic["http"]["serversTransports"]["ax-web-mtls"]
if transport.get("minVersion") and not transport.get("maxVersion"):
    fail(
        "the ax-web-mtls minVersion needs a maxVersion, or Traefik v3.7.13 "
        "drops its TLS configuration and the client certificate"
    )
if transport != ax_servers_transport:
    fail("the ax-web-mtls transport differs from the reviewed mTLS contract")
if {
    name: service["loadBalancer"]["serversTransport"]
    for name, service in dynamic["http"]["services"].items()
    if "serversTransport" in service.get("loadBalancer", {})
} != {"ax": "ax-web-mtls"}:
    fail("only the ax backend may use the reviewed servers transport")


def secret_paths(document: Any) -> set[str]:
    if isinstance(document, dict):
        return set().union(*(secret_paths(value) for value in document.values()))
    if isinstance(document, list):
        return set().union(*(secret_paths(value) for value in document))
    if isinstance(document, str) and document.startswith("/run/secrets/"):
        return {document}
    return set()


# Traefik reads any path it cannot open as inline content, so a renamed or
# unmounted secret would fail quietly: each file the dynamic configuration
# names is a mounted secret, and each mounted secret but the ACME token is
# named there.
if secret_paths(dynamic) != {
    f"/run/secrets/{target}"
    for target in {**basicauth_secrets, **upstream_mtls_secrets}
}:
    fail("the dynamic configuration and the mounted secrets differ")
for rendered_name in ("stack.yml", "static.yml", "dynamic.yml"):
    rendered_text = (render_dir / rendered_name).read_text(encoding="utf-8")
    if re.search(r"\$(?:2[abxy]?|apr1)\$|\{SHA\}", rendered_text):
        fail(f"the rendered {rendered_name} contains a password hash")
organizationweb_route = dynamic["http"]["routers"]["organizationweb"]
if organizationweb_route != {
    "rule": "Host(`organizacion.apptolast.com`)",
    "entryPoints": ["websecure"],
    "middlewares": ["edge-security", "edge-rate-limit"],
    "service": "organizationweb",
    "tls": {"certResolver": "letsencrypt"},
}:
    fail("the OrganizationWeb router differs from its independent contract")
if dynamic["http"]["services"]["organizationweb"] != {
    "loadBalancer": {
        "passHostHeader": True,
        "servers": [{"url": "http://organizationweb_web:8080"}],
        "healthCheck": {
            "path": "/healthz", "hostname": "organizacion.apptolast.com",
            "interval": "15s", "timeout": "3s",
        },
    },
}:
    fail("the OrganizationWeb upstream differs from its independent contract")
racinggame_route = dynamic["http"]["routers"]["racinggame"]
if racinggame_route != {
    "rule": "Host(`racinggame.apptolast.com`)",
    "entryPoints": ["websecure"],
    "middlewares": ["edge-security"],
    "service": "racinggame",
    "tls": {"certResolver": "letsencrypt"},
}:
    fail("the RacingGame router differs from its independent contract")
if dynamic["http"]["services"]["racinggame"] != {
    "loadBalancer": {
        "passHostHeader": True,
        "servers": [{"url": "http://racinggame_web:3000"}],
        "healthCheck": {
            "path": "/info", "interval": "15s", "timeout": "3s",
        },
    },
}:
    fail("the RacingGame upstream differs from its independent contract")
# Codified from the live Traefik configuration: the dashboard itself
# is deployed outside this repository, so only its route is pinned.
monitorizacion_route = dynamic["http"]["routers"]["monitorizacion"]
if monitorizacion_route != {
    "rule": "Host(`monitor.apptolast.com`)",
    "entryPoints": ["websecure"],
    "service": "monitorizacion",
    "tls": {"certResolver": "letsencrypt"},
}:
    fail("the monitoring router differs from the reviewed ingress")
if dynamic["http"]["services"]["monitorizacion"] != {
    "loadBalancer": {
        "passHostHeader": True,
        "servers": [{"url": "http://monitor-api:8080"}],
    },
}:
    fail("the monitoring upstream differs from the reviewed ingress")
for route, (service_id, upstream) in edge_routes.items():
    expected_hostnames = approved_by_id[service_id]["hostnames"]
    if len(expected_hostnames) != 1:
        fail(f"{service_id} must have exactly one edge hostname")
    if (
        dynamic["http"]["routers"][route]["rule"]
        != f"Host(`{expected_hostnames[0]}`)"
    ):
        fail(f"the rendered {route} hostname differs from the service catalog")
    load_balancer = dynamic["http"]["services"][route]["loadBalancer"]
    # Una ruta aparcada conserva router y certificado, pero su backend no
    # tiene servidores (Traefik responde 503) ni sonda de salud.
    if route in parked_workloads:
        if load_balancer != {"passHostHeader": True, "servers": []}:
            fail(f"the parked {route} backend must have no server nor probe")
    elif load_balancer["servers"] != [{"url": upstream}]:
        fail(f"the rendered {route} upstream differs from the Swarm contract")

static = load_yaml(".build/edge/static.yml")
# A password typed as the user name must not reach the access log.
if (static.get("accessLog") or {}).get("fields", {}).get("names") != {
    "ClientUsername": "drop"
}:
    fail("the access log keeps the basicAuth user name")
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
