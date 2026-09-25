#!/usr/bin/env python3
"""Validate the reviewed AX lab contract offline.

`config/ax-lab.yml` is the main input. The validator pins its exact shape:
the install root under /opt/dockerswarm, the credential directory as a path
only, the inotify limits kind recommends (disjoint from every sysctl key the
host_baseline and platform roles manage), the AX and Substrate source
commits, the official kind and kubectl release assets with their sha256, the
upstream images by tag and digest, and the kind cluster and local registry:
loopback-only binds, positive limits within the memory limit/reservation
ratio of config/capacity.yml, restart policy "no", and the same limits as
their host_containers.ax-lab entry in config/capacity-profiles.yml. It
rejects secret-like keys and values anywhere in the file, then renders the
role's sysctl file and kind configuration and checks both against the
contract. It reads nothing outside this repository, so CI runs it without a
production host.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import posixpath
import re
import sys
import tomllib
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/ax-lab.yml"
SYSCTL_TEMPLATE_DIRECTORY = ROOT / "ansible/roles/ax_lab/templates"
SYSCTL_TEMPLATE = "99-z-dockerswarm-ax-lab.conf.j2"
KIND_CONFIG_TEMPLATE = "kind-config.yaml.j2"
CAPACITY_CONTRACT = ROOT / "config/capacity.yml"
CAPACITY_PROFILES = ROOT / "config/capacity-profiles.yml"
# The host_containers group of config/capacity-profiles.yml that budgets the
# node and the registry.
CAPACITY_GROUP = "ax-lab"
HOST_BASELINE_DEFAULTS = ROOT / "ansible/roles/host_baseline/defaults/main.yml"
PLATFORM_SYSCTL_FILE = (
    ROOT / "ansible/roles/platform/files/99-z-dockerswarm-network.conf"
)

TOP_LEVEL_KEYS = {"ax_lab", "ax_lab_privileged_node_accepted"}
LAB_KEYS = {
    "schema_version",
    "install_root",
    "credential_directory",
    "sysctl",
    "sources",
    "binaries",
    "images",
    "cluster",
    "registry",
}
CLUSTER_KEYS = {
    "name",
    "node_container",
    "network",
    "api_server",
    "resources",
    "restart_policy",
}
REGISTRY_KEYS = {
    "container",
    "volume",
    "host_port",
    "container_port",
    "bind_addresses",
    "resources",
    "restart_policy",
}
RESOURCE_KEYS = {
    "memory_limit_mib",
    "memory_reservation_mib",
    "cpu_limit_millicores",
    "pids_limit",
}
# kind names a cluster's single control-plane node <name>-control-plane and
# puts every node on its fixed user-defined network `kind` (kind v0.33.0
# pkg/cluster/internal/providers/docker/network.go, fixedNetworkName).
CLUSTER_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
KIND_NETWORK = "kind"
# Docker's rule for container and volume names: an alphanumeric first
# character, then at least one more alphanumeric, `_`, `.` or `-`.
DOCKER_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+")
# The port registry:3 serves inside its container, as in kind's guide.
REGISTRY_CONTAINER_PORT = 5000
# A fixed host port must stay out of the kernel's ephemeral port range
# (net.ipv4.ip_local_port_range, 32768-60999 on this host), where an
# outgoing connection could already hold it, and off the privileged ports.
FIXED_PORT_RANGE = range(1024, 32768)
# The lab starts only through its playbook, never by Docker on its own.
RESTART_POLICY = "no"
# Substrate's kind configuration at the pinned commit
# (hack/create-kind-cluster.sh, lines 88-126, IP_FAMILY=ipv4, no /dev/kvm).
SUBSTRATE_FEATURE_GATES = {
    "ClusterTrustBundle": True,
    "ClusterTrustBundleProjection": True,
    "PodCertificateRequest": True,
}
SUBSTRATE_RUNTIME_CONFIG = {"certificates.k8s.io/v1beta1": "true"}
SUBSTRATE_KUBELET_PATCH = {
    "kind": "KubeletConfiguration",
    "serializeImagePulls": False,
    "maxParallelImagePulls": 4,
}
# kind's local registry guide at v0.33.0
# (site/static/examples/kind-with-registry.sh, lines 37-44): containerd reads
# per-registry hosts.toml files from this directory.
CONTAINERD_REGISTRY_PATCH = {
    "plugins": {
        "io.containerd.grpc.v1.cri": {
            "registry": {"config_path": "/etc/containerd/certs.d"}
        }
    }
}
KIND_CONFIG_KEYS = {
    "kind",
    "apiVersion",
    "nodes",
    "featureGates",
    "runtimeConfig",
    "networking",
    "kubeadmConfigPatches",
    "containerdConfigPatches",
}
PLATFORM_INSTALL_ROOT = PurePosixPath("/opt/dockerswarm")
# Children of the platform root that already belong to the platform itself.
RESERVED_INSTALL_NAMES = {"deployments"}
INSTALL_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# Host secrets live in /etc/dockerswarm/<area>/ (root:root, docs/ARCHITECTURE.md).
CREDENTIAL_ROOT = PurePosixPath("/etc/dockerswarm")

# kind known issues, "Pod errors due to too many open files":
# https://kind.sigs.k8s.io/docs/user/known-issues/
REVIEWED_SYSCTL = {
    "fs.inotify.max_user_watches": "524288",
    "fs.inotify.max_user_instances": "512",
}
SYSCTL_KEY_RE = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+)+")
SYSCTL_VALUE_RE = re.compile(r"0|[1-9][0-9]*")

SOURCE_REPOSITORIES = {
    "ax": "https://github.com/google/ax",
    "substrate": "https://github.com/agent-substrate/substrate",
}
COMMIT_RE = re.compile(r"[a-f0-9]{40}")
SHA256_RE = re.compile(r"[a-f0-9]{64}")
VERSION_RE = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
# Official release asset hosts and the exact linux-amd64 asset path.
BINARY_SOURCES = {
    "kind": (
        "github.com",
        "/kubernetes-sigs/kind/releases/download/{version}/kind-linux-amd64",
    ),
    "kubectl": ("dl.k8s.io", "/release/{version}/bin/linux/amd64/kubectl"),
}

IMAGE_REPOSITORIES = {
    "kind_node": "docker.io/kindest/node",
    "registry": "docker.io/library/registry",
    "redis": "docker.io/library/redis",
    "openai_proxy": "docker.io/nginxinc/nginx-unprivileged",
}
IMAGE_RE = re.compile(
    r"(?P<repository>[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)+)"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})"
    r"@sha256:(?P<digest>[a-f0-9]{64})"
)

# Names and shapes of credentials. This public file holds paths and public
# pins only, so any match is refused before the schema is even read.
SECRET_KEY_RE = re.compile(
    r"passw(?:or)?d|passphrase|secret|token|api[_-]?key|private[_-]?key"
    r"|bearer|oauth|cookie|session",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bbearer\s+\S", re.IGNORECASE),
    re.compile(r"[a-z][a-z0-9+.-]*://[^/\s@]+@", re.IGNORECASE),
    re.compile(r"(?:passw(?:or)?d|secret|token|api[_-]?key)\s*[:=]", re.IGNORECASE),
)


class AxLabError(ValueError):
    """The AX lab contract differs from its reviewed shape."""


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader which rejects duplicate mapping keys."""


def construct_unique_mapping(
    loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found an unhashable key: {error}",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping
)


def load_yaml(path: Path) -> Any:
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as error:
        raise AxLabError(f"cannot read {path}: {error}") from error


def exact_keys(value: Any, keys: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise AxLabError(f"{context}: unexpected or missing keys")
    return value


def reject_secret_like(value: Any, context: str = "config") -> None:
    """Refuse credential-shaped keys or values anywhere in the document."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and SECRET_KEY_RE.search(key):
                raise AxLabError(f"{context}: secret-like key {key!r}")
            reject_secret_like(item, f"{context}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_secret_like(item, f"{context}[{index}]")
    elif isinstance(value, str):
        for pattern in SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                raise AxLabError(f"{context}: secret-like value")


def absolute_path(value: Any, context: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or posixpath.normpath(value) != value
    ):
        raise AxLabError(f"{context} must be a normalized absolute path")
    return PurePosixPath(value)


def validate_install_root(value: Any) -> None:
    path = absolute_path(value, "install_root")
    if (
        path.parent != PLATFORM_INSTALL_ROOT
        or not INSTALL_NAME_RE.fullmatch(path.name)
        or path.name in RESERVED_INSTALL_NAMES
    ):
        raise AxLabError(
            f"install_root must be its own directory under {PLATFORM_INSTALL_ROOT}"
        )


def validate_credential_directory(value: Any) -> None:
    path = absolute_path(value, "credential_directory")
    if CREDENTIAL_ROOT not in path.parents:
        raise AxLabError(f"credential_directory must be under {CREDENTIAL_ROOT}")


def parse_sysctl_text(text: str, context: str) -> dict[str, str]:
    """Parse sysctl.d syntax: `key = value`, `#` or `;` comments."""
    settings: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        key, separator, setting = line.removeprefix("-").partition("=")
        key = key.strip()
        if not separator or not SYSCTL_KEY_RE.fullmatch(key) or key in settings:
            raise AxLabError(f"{context}:{number}: invalid or repeated setting")
        settings[key] = setting.strip()
    return settings


def reserved_sysctl_keys() -> dict[str, str]:
    """Return every sysctl key another role persists, mapped to its owner."""
    defaults = load_yaml(HOST_BASELINE_DEFAULTS)
    baseline = (
        defaults.get("host_baseline_sysctl") if isinstance(defaults, dict) else None
    )
    if not isinstance(baseline, dict) or not baseline:
        raise AxLabError("cannot read host_baseline_sysctl")
    try:
        platform_text = PLATFORM_SYSCTL_FILE.read_text(encoding="utf-8")
    except OSError as error:
        raise AxLabError(f"cannot read the platform sysctl file: {error}") from error
    platform = parse_sysctl_text(platform_text, "platform sysctl file")
    if not platform:
        raise AxLabError("the platform sysctl file sets no key")
    reserved = dict.fromkeys(baseline, "host_baseline")
    reserved.update(dict.fromkeys(platform, "platform"))
    return reserved


def validate_sysctl(settings: Any, reserved: dict[str, str]) -> None:
    if not isinstance(settings, dict) or not settings:
        raise AxLabError("sysctl must be a non-empty mapping")
    for key, value in settings.items():
        if not isinstance(key, str) or not SYSCTL_KEY_RE.fullmatch(key):
            raise AxLabError(f"sysctl key {key!r} is not a kernel setting name")
        if key in reserved:
            raise AxLabError(
                f"sysctl key {key} overlaps a key the {reserved[key]} role manages"
            )
        if not isinstance(value, str) or not SYSCTL_VALUE_RE.fullmatch(value):
            raise AxLabError(f"sysctl {key} must be a quoted decimal string")
    if settings != REVIEWED_SYSCTL:
        raise AxLabError("sysctl differs from the inotify limits kind recommends")


def validate_sources(sources: Any) -> None:
    exact_keys(sources, set(SOURCE_REPOSITORIES), "sources")
    for name, repository in SOURCE_REPOSITORIES.items():
        source = exact_keys(sources[name], {"repository", "commit"}, name)
        if source["repository"] != repository:
            raise AxLabError(f"source {name} must use its upstream repository")
        if not isinstance(source["commit"], str) or not COMMIT_RE.fullmatch(
            source["commit"]
        ):
            raise AxLabError(f"source {name} commit must be a full 40-hex SHA")


def validate_binary_url(name: str, url: Any, version: str) -> None:
    host, path = BINARY_SOURCES[name]
    if not isinstance(url, str):
        raise AxLabError(f"binary {name} url must be a string")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise AxLabError(f"binary {name} must download over https")
    if parts.netloc != host or parts.query or parts.fragment:
        raise AxLabError(f"binary {name} must come from its official host {host}")
    if url != f"https://{host}{path.format(version=version)}":
        raise AxLabError(
            f"binary {name} url must be the official {version} linux-amd64 asset"
        )


def validate_binaries(binaries: Any) -> None:
    exact_keys(binaries, set(BINARY_SOURCES), "binaries")
    for name in BINARY_SOURCES:
        binary = exact_keys(binaries[name], {"version", "url", "sha256"}, name)
        version = binary["version"]
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            raise AxLabError(f"binary {name} version must look like v1.2.3")
        if not isinstance(binary["sha256"], str) or not SHA256_RE.fullmatch(
            binary["sha256"]
        ):
            raise AxLabError(f"binary {name} sha256 must be 64 lowercase hex")
        validate_binary_url(name, binary["url"], version)


def validate_images(images: Any) -> dict[str, re.Match[str]]:
    exact_keys(images, set(IMAGE_REPOSITORIES), "images")
    references = {}
    for name, repository in IMAGE_REPOSITORIES.items():
        reference = images[name]
        match = IMAGE_RE.fullmatch(reference) if isinstance(reference, str) else None
        if match is None:
            raise AxLabError(f"image {name} must be repository:tag@sha256:<64 hex>")
        if match["repository"] != repository:
            raise AxLabError(f"image {name} must use {repository}")
        if match["tag"] == "latest":
            raise AxLabError(f"image {name} must not use the moving latest tag")
        references[name] = match
    return references


def exact_int(value: Any, context: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise AxLabError(f"{context} must be an integer >= {minimum}")
    return value


def fixed_port(value: Any, context: str) -> int:
    if type(value) is not int or value not in FIXED_PORT_RANGE:
        raise AxLabError(
            f"{context} must be a fixed port from {FIXED_PORT_RANGE.start} "
            f"to {FIXED_PORT_RANGE.stop - 1}"
        )
    return value


def loopback_address(
    value: Any, context: str
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """A canonical loopback IP address: nothing else may publish the lab."""
    try:
        address = ipaddress.ip_address(value) if isinstance(value, str) else None
    except ValueError:
        address = None
    if address is None or not address.is_loopback or str(address) != value:
        raise AxLabError(f"{context} must be a canonical loopback address")
    return address


def restart_policy(value: Any, context: str) -> None:
    # An unquoted YAML `no` is the boolean false, not Docker's policy name.
    if value != RESTART_POLICY or type(value) is not str:
        raise AxLabError(f'{context} restart_policy must be the string "no"')


def load_capacity_declaration(
    ratio: Decimal | None = None, group: Any = None
) -> tuple[Decimal, Any]:
    """The memory ratio policy and the ax-lab host container group."""
    if ratio is None:
        contract = load_yaml(CAPACITY_CONTRACT)
        try:
            ratio = Decimal(
                contract["capacity_contract"]["policy"][
                    "service_memory_limit_to_reservation_ratio"
                ]
            )
        except (KeyError, TypeError, InvalidOperation) as error:
            raise AxLabError(
                "cannot read the memory limit/reservation ratio"
            ) from error
    if group is None:
        profiles = load_yaml(CAPACITY_PROFILES)
        try:
            group = profiles["capacity_profiles"]["host_containers"][CAPACITY_GROUP]
        except (KeyError, TypeError) as error:
            raise AxLabError(
                f"config/capacity-profiles.yml declares no {CAPACITY_GROUP} group"
            ) from error
    return ratio, group


def validate_resources(resources: Any, context: str, ratio: Decimal) -> None:
    exact_keys(resources, RESOURCE_KEYS, f"{context}.resources")
    for key in sorted(RESOURCE_KEYS):
        exact_int(resources[key], f"{context}.resources.{key}")
    limit = resources["memory_limit_mib"]
    reservation = resources["memory_reservation_mib"]
    if reservation > limit:
        raise AxLabError(f"{context} reserves more memory than its limit")
    if Decimal(limit) > Decimal(reservation) * ratio:
        raise AxLabError(f"{context} memory limit/reservation ratio exceeds {ratio}")


def validate_cluster(cluster: Any, ratio: Decimal) -> dict[str, Any]:
    exact_keys(cluster, CLUSTER_KEYS, "cluster")
    name = cluster["name"]
    if not isinstance(name, str) or not CLUSTER_NAME_RE.fullmatch(name):
        raise AxLabError("cluster name must be a lowercase DNS label")
    if cluster["node_container"] != f"{name}-control-plane":
        raise AxLabError("cluster node_container must be kind's <name>-control-plane")
    if cluster["network"] != KIND_NETWORK:
        raise AxLabError(f"cluster network must be kind's fixed {KIND_NETWORK}")
    api_server = exact_keys(cluster["api_server"], {"address", "port"}, "api_server")
    # The cluster is ipv4-only, so the API is published on the IPv4 loopback.
    if loopback_address(api_server["address"], "api_server address").version != 4:
        raise AxLabError("api_server address must be the IPv4 loopback")
    fixed_port(api_server["port"], "api_server port")
    validate_resources(cluster["resources"], "cluster", ratio)
    restart_policy(cluster["restart_policy"], "cluster")
    return cluster


def validate_registry(registry: Any, cluster: dict[str, Any], ratio: Decimal) -> None:
    exact_keys(registry, REGISTRY_KEYS, "registry")
    for key in ("container", "volume"):
        if not isinstance(registry[key], str) or not DOCKER_NAME_RE.fullmatch(
            registry[key]
        ):
            raise AxLabError(f"registry {key} must be a Docker name")
    if registry["container"] == cluster["node_container"]:
        raise AxLabError("registry container must differ from the node")
    host_port = fixed_port(registry["host_port"], "registry host_port")
    if host_port == cluster["api_server"]["port"]:
        raise AxLabError("registry host_port must differ from the API server port")
    if registry["container_port"] != REGISTRY_CONTAINER_PORT or (
        type(registry["container_port"]) is not int
    ):
        raise AxLabError(f"registry container_port must be {REGISTRY_CONTAINER_PORT}")
    addresses = registry["bind_addresses"]
    if (
        not isinstance(addresses, list)
        or not addresses
        or len(set(map(str, addresses))) != len(addresses)
    ):
        raise AxLabError("registry bind_addresses must list distinct addresses")
    for address in addresses:
        loopback_address(address, "registry bind address")
    validate_resources(registry["resources"], "registry", ratio)
    restart_policy(registry["restart_policy"], "registry")


def validate_capacity_group(lab: dict[str, Any], group: Any) -> None:
    """The limits the role applies are the ones the capacity plan budgets."""
    declared = {
        lab["cluster"]["node_container"]: lab["cluster"]["resources"],
        lab["registry"]["container"]: lab["registry"]["resources"],
    }
    if not isinstance(group, dict) or set(group) != set(declared):
        raise AxLabError(
            f"host_containers.{CAPACITY_GROUP} must declare exactly the node "
            "and the registry"
        )
    for name, resources in declared.items():
        budget = group[name]
        try:
            budgeted = {
                "memory_limit_mib": budget["limits"]["memory_mib"],
                "memory_reservation_mib": budget["reservations"]["memory_mib"],
                "cpu_limit_millicores": budget["limits"]["cpu_millicores"],
                "pids_limit": budget["pids_limit"],
            }
        except (KeyError, TypeError) as error:
            raise AxLabError(
                f"host_containers.{CAPACITY_GROUP}.{name} is malformed"
            ) from error
        if budgeted != resources:
            raise AxLabError(
                f"{name} resources differ from host_containers.{CAPACITY_GROUP}"
            )


def validate_catalog(
    document: Any,
    reserved: dict[str, str] | None = None,
    capacity: tuple[Decimal, Any] | None = None,
) -> dict[str, Any]:
    """Validate the whole file and return the `ax_lab` mapping."""
    reject_secret_like(document)
    exact_keys(document, TOP_LEVEL_KEYS, "config")
    if type(document["ax_lab_privileged_node_accepted"]) is not bool:
        raise AxLabError("ax_lab_privileged_node_accepted must be a boolean")
    lab = exact_keys(document["ax_lab"], LAB_KEYS, "ax_lab")
    if type(lab["schema_version"]) is not int or lab["schema_version"] != 1:
        raise AxLabError("ax_lab schema_version is unsupported")
    validate_install_root(lab["install_root"])
    validate_credential_directory(lab["credential_directory"])
    validate_sysctl(
        lab["sysctl"], reserved_sysctl_keys() if reserved is None else reserved
    )
    validate_sources(lab["sources"])
    validate_binaries(lab["binaries"])
    images = validate_images(lab["images"])
    if images["kind_node"]["tag"] != lab["binaries"]["kubectl"]["version"]:
        raise AxLabError("the kind node image and kubectl must share one version")
    ratio, group = capacity if capacity is not None else load_capacity_declaration()
    cluster = validate_cluster(lab["cluster"], ratio)
    validate_registry(lab["registry"], cluster, ratio)
    validate_capacity_group(lab, group)
    return lab


def render_sysctl(lab: dict[str, Any]) -> str:
    """Render the role's sysctl file as Ansible's template module would."""
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(SYSCTL_TEMPLATE_DIRECTORY),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        keep_trailing_newline=True,
    )
    return environment.get_template(SYSCTL_TEMPLATE).render(ax_lab=lab)


def validate_sysctl_render(rendered: str, lab: dict[str, Any]) -> None:
    if parse_sysctl_text(rendered, SYSCTL_TEMPLATE) != lab["sysctl"]:
        raise AxLabError("the rendered sysctl file differs from the contract")


def render_kind_config(lab: dict[str, Any]) -> str:
    """Render the kind configuration as Ansible's template module would."""
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(SYSCTL_TEMPLATE_DIRECTORY),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        keep_trailing_newline=True,
    )
    return environment.get_template(KIND_CONFIG_TEMPLATE).render(ax_lab=lab)


def validate_kind_config_render(rendered: str, lab: dict[str, Any]) -> None:
    """Substrate's configuration, the loopback API and the registry directory."""
    try:
        config = yaml.load(rendered, Loader=UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise AxLabError(
            f"the rendered kind configuration is not YAML: {error}"
        ) from error
    exact_keys(config, KIND_CONFIG_KEYS, "kind configuration")
    if config["kind"] != "Cluster" or config["apiVersion"] != "kind.x-k8s.io/v1alpha4":
        raise AxLabError("the kind configuration must be a v1alpha4 Cluster")
    # One control-plane node without extraMounts: this host has no /dev/kvm,
    # so Substrate's script adds no mount, and nothing else reaches the node.
    if config["nodes"] != [{"role": "control-plane"}]:
        raise AxLabError("the kind configuration must declare one bare control plane")
    if config["featureGates"] != SUBSTRATE_FEATURE_GATES:
        raise AxLabError("kind featureGates differ from Substrate's configuration")
    if config["runtimeConfig"] != SUBSTRATE_RUNTIME_CONFIG:
        raise AxLabError("kind runtimeConfig differs from Substrate's configuration")
    api_server = lab["cluster"]["api_server"]
    if config["networking"] != {
        "ipFamily": "ipv4",
        "apiServerAddress": api_server["address"],
        "apiServerPort": api_server["port"],
    }:
        raise AxLabError("kind networking must be ipv4 with the loopback API server")
    patches = config["kubeadmConfigPatches"]
    try:
        kubelet = (
            yaml.safe_load(patches[0])
            if isinstance(patches, list)
            and len(patches) == 1
            and isinstance(patches[0], str)
            else None
        )
    except yaml.YAMLError:
        kubelet = None
    if kubelet != SUBSTRATE_KUBELET_PATCH:
        raise AxLabError("kind kubeadmConfigPatches differ from Substrate's")
    patches = config["containerdConfigPatches"]
    try:
        registry = (
            tomllib.loads(patches[0])
            if isinstance(patches, list)
            and len(patches) == 1
            and isinstance(patches[0], str)
            else None
        )
    except tomllib.TOMLDecodeError:
        registry = None
    if registry != CONTAINERD_REGISTRY_PATCH:
        raise AxLabError("kind containerdConfigPatches must only set config_path")


def kind_config_sha256(rendered: str) -> str:
    """The digest the role records and compares, of the exact file bytes."""
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write the rendered sysctl file")
    parser.add_argument(
        "--kind-config-output", type=Path, help="write the rendered kind configuration"
    )
    parser.add_argument(
        "--kind-config-sha256",
        action="store_true",
        help="print only the sha256 of the rendered kind configuration",
    )
    args = parser.parse_args(argv)
    try:
        lab = validate_catalog(load_yaml(CONFIG))
        rendered = render_sysctl(lab)
        validate_sysctl_render(rendered, lab)
        kind_config = render_kind_config(lab)
        validate_kind_config_render(kind_config, lab)
        for path, text in (
            (args.output, rendered),
            (args.kind_config_output, kind_config),
        ):
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
    except (AxLabError, OSError, jinja2.TemplateError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if args.kind_config_sha256:
        print(kind_config_sha256(kind_config))
        return 0
    print("AX lab contract, pins, sysctl and kind configuration renders passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
