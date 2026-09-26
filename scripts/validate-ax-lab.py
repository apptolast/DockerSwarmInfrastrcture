#!/usr/bin/env python3
"""Validate the reviewed AX lab contract offline.

`config/ax-lab.yml` is the main input. The validator pins its exact shape:
the install root under /opt/dockerswarm, the credential directory as a path
only, the inotify limits kind recommends (disjoint from every sysctl key the
host_baseline and platform roles manage), the AX and Substrate source
commits, the official kind and kubectl release assets with their sha256, the
upstream images by tag and digest, the kind cluster and local registry:
loopback-only binds, positive limits within the memory limit/reservation
ratio of config/capacity.yml, restart policy "no", and the same limits as
their host_containers.ax-lab entry in config/capacity-profiles.yml, and Agent
Substrate: its version, its images by digest, the backup directory, the
bounded fallback build and the ate-setup install with their limits, and the
workloads ate-setup deploys, and AX: the vendored #375 patch and ko manifests
by sha256, its four images by digest, the CLI backup, the WorkerPool within
the node's CPU and memory, and the router timeout. It rejects secret-like keys
and values anywhere in the file, then renders the role's sysctl file, kind
configuration and AX manifests and checks them against the contract: AX is
never published, never granted RBAC and never mounts a Kubernetes API token.
It reads nothing outside this repository, so CI runs it without a production
host.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
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
# The AX manifests the role applies server-side, in this order.
AX_MANIFEST_TEMPLATES = ("ax-system.yaml", "ax-workers.yaml")
CAPACITY_CONTRACT = ROOT / "config/capacity.yml"
CAPACITY_PROFILES = ROOT / "config/capacity-profiles.yml"
# The host_containers group of config/capacity-profiles.yml that budgets the
# node and the registry.
CAPACITY_GROUP = "ax-lab"
HOST_BASELINE_DEFAULTS = ROOT / "ansible/roles/host_baseline/defaults/main.yml"
PLATFORM_SYSCTL_FILE = (
    ROOT / "ansible/roles/platform/files/99-z-dockerswarm-network.conf"
)

TOP_LEVEL_KEYS = {
    "ax_lab",
    "ax_lab_privileged_node_accepted",
    "ax_lab_substrate_fallback_builds",
}
SCHEMA_VERSION = 3
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
    "substrate",
    "ax",
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
    "toolbox": "docker.io/library/golang",
}
# The Go release the manual lab built every pinned image with; Substrate's
# go.mod at the pinned commit says `go 1.27.0` with no toolchain line.
REVIEWED_TOOLBOX_TAG = "1.27.1"
IMAGE_RE = re.compile(
    r"(?P<repository>[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)+)"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})"
    r"@sha256:(?P<digest>[a-f0-9]{64})"
)

SUBSTRATE_KEYS = {
    "version",
    "images",
    "backup_directory",
    "build",
    "install",
    "workloads",
}
# ko images of ./cmd/<name>: the five ate-setup installs
# (cmd/ate-setup/internal/images/images.go, Components), the WorkerPool's
# gVisor worker image and ate-setup itself.
INSTALLED_IMAGES = ("ateapi", "atecontroller", "atelet", "atenet", "podcertcontroller")
SUBSTRATE_IMAGES = (*INSTALLED_IMAGES, "ateom-gvisor", "ate-setup")
# Only ate-setup has no digest of the manual lab: the reproducibility
# workflow prints it, and the role refuses to run until it is pinned.
PENDING_IMAGES = {"ate-setup"}
SUBSTRATE_DIGEST_RE = re.compile(r"sha256:[a-f0-9]{64}")
# A label value (at most 63 characters, alphanumeric at both ends) whose
# lowercase form is also the atelet DaemonSet suffix, capped at 30
# characters (internal/versionlabel/versionlabel.go at the pinned commit).
SUBSTRATE_VERSION_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,28}[a-z0-9])?")
BACKUP_DIRECTORY = "/var/backups/dockerswarm/ax-lab/images"
BUILD_KEYS = {
    "memory_limit_mib",
    "memory_reservation_mib",
    "cpu_limit_millicores",
    "pids_limit",
    "timeout_seconds",
    "min_mem_available_mib",
    "mem_available_floor_mib",
}
INSTALL_KEYS = {
    "atenet_router",
    "rollout_timeout_seconds",
    "timeout_seconds",
    "memory_limit_mib",
    "memory_reservation_mib",
    "cpu_limit_millicores",
    "pids_limit",
    "min_mem_available_mib",
    "mem_available_floor_mib",
}
BUILD_TIMEOUT_RANGE = range(600, 7201)
ROLLOUT_TIMEOUT_RANGE = range(300, 1801)
# ate-setup deploy ate-system waits, in sequence, for the namespace (60 s),
# two CRDs (30 s each), then with --rollout-timeout for the
# podcertificate-controller rollout and its trust bundles
# (cmd/ate-setup/internal/steps/deploy.go lines 55-98). SetupCSI runs even
# with no driver (deploy.go line 99) and, before it returns, waits for the
# namespace again and, with --rollout-timeout, for the same rollout and trust
# bundles (csi.go lines 61-104; its CRDs already exist). Six rollouts follow
# (deploy.go lines 150-174). --rollout-timeout reaches every wait once it is
# set (internal/config/config.go lines 343-347). The runner's own timeout
# must outlast all of them, with margin, so that ate-setup reports its own
# error.
INSTALL_FIXED_WAIT_SECONDS = 180
INSTALL_ROLLOUT_WAITS = 10
INSTALL_MARGIN_SECONDS = 300
INSTALL_TIMEOUT_MAX = 10800
WORKLOAD_KEYS = {"kind", "namespace", "name", "images"}
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job"}
SUBSTRATE_NAMESPACES = {"ate-system", "otel-system", "podcertificate-controller-system"}
DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
# An upstream reference exactly as a pinned manifest carries it: a Docker Hub
# short name is allowed, but always with a tag and a sha256 digest.
UPSTREAM_IMAGE_RE = re.compile(
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*"
    r":[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}@sha256:[a-f0-9]{64}"
)

AX_KEYS = {
    "tag",
    "images",
    "task_runner_binary_sha256",
    "ko_base_image",
    "cli_sha256",
    "cli_backup",
    "snapshots_bucket",
    "redis",
    "worker_pool",
    "node_platform_reserve_mib",
    "router_route_timeout",
}
# ko builds the first two from ./cmd/<name> (.ko.yaml at sources.ax); the
# manual lab built the runner and the agents images with buildx.
AX_KO_IMAGES = ("ax-controller", "ax-server")
AX_IMAGES = (*AX_KO_IMAGES, "ax-task-runner", "ax-agents")
AX_PATCH = "images/ax/google-ax-375.patch"
# The exact manifests the manual lab pushed for the ko images: their bytes
# hash to the pins, and the reproducibility workflow compares a rebuild's
# config and layers with them.
AX_KO_MANIFESTS = "images/ax/manifests"
AX_TAG_RE = re.compile(r"[a-z0-9][a-z0-9._-]{6,127}")
KO_BASE_IMAGE_RE = re.compile(r"cgr\.dev/chainguard/static@sha256:[a-f0-9]{64}")
AX_CLI_BACKUP = "/var/backups/dockerswarm/ax-lab/binaries/ax"
# Substrate's kind rustfs bucket (manifests/ate-install/kind/rustfs.yaml,
# lines 121-126), where the manual lab pointed AX_SNAPSHOTS_BUCKET.
AX_SNAPSHOTS_BUCKET = "gs://ate-snapshots/ax/"
AX_REDIS_KEYS = {"volume_mib", "save_seconds"}
AX_REDIS_VOLUME_RANGE = range(256, 4097)
AX_REDIS_SAVE_RANGE = range(10, 3601)
WORKER_POOL_KEYS = {
    "namespace",
    "name",
    "replicas",
    "memory_mib",
    "cpu_request_millicores",
    "cpu_limit_millicores",
}
AX_SYSTEM_NAMESPACE = "ax-system"
AX_WORKERS_NAMESPACE = "ax-workers"
# What the workers' CPU limits, all of them together, leave to etcd, the API
# server and Substrate: PR review item 4, since the node itself is capped at
# cluster.resources.
NODE_CPU_KEPT_MILLICORES = 500
# The memory of the node's limit that the workers' limits never take: the
# platform measured 1 296 MiB of anonymous and kernel memory on 2026-09-25,
# plus its hot page cache (docs/AX.md, «WorkerPool y capacidad dentro del
# nodo»).
NODE_PLATFORM_RESERVE_MIB = 1792
ROUTE_TIMEOUT_RE = re.compile(r"([1-9][0-9]*)(m|h)")
ROUTE_TIMEOUT_MAX_MINUTES = 120
# Every object the AX manifests may hold, in their order. Nothing else is
# ever applied: no Secret, no RBAC, no Ingress, no other namespace.
AX_INVENTORY = {
    "ax-system.yaml": [
        ("Namespace", None, AX_SYSTEM_NAMESPACE),
        ("ServiceAccount", AX_SYSTEM_NAMESPACE, "ax-controller"),
        ("PersistentVolumeClaim", AX_SYSTEM_NAMESPACE, "ax-redis-data"),
        ("Deployment", AX_SYSTEM_NAMESPACE, "ax-redis"),
        ("Service", AX_SYSTEM_NAMESPACE, "ax-redis"),
        ("Deployment", AX_SYSTEM_NAMESPACE, "ax-server"),
        ("Service", AX_SYSTEM_NAMESPACE, "ax-server"),
        ("Deployment", AX_SYSTEM_NAMESPACE, "ax-controller"),
        ("NetworkPolicy", AX_SYSTEM_NAMESPACE, "ax-redis"),
        ("NetworkPolicy", AX_SYSTEM_NAMESPACE, "ax-server"),
    ],
    "ax-workers.yaml": [
        ("Namespace", None, AX_WORKERS_NAMESPACE),
        ("WorkerPool", AX_WORKERS_NAMESPACE, "ax"),
    ],
}
MANAGED_LABEL = {"com.apptolast.managed-by": "ansible"}
POD_HOST_FIELDS = ("hostNetwork", "hostPID", "hostIPC")
# Where the kubelet mounts a Kubernetes API token when a pod automounts one
# (/var/run is /run in the images).
SERVICE_ACCOUNT_MOUNTS = (
    "/var/run/secrets/kubernetes.io",
    "/run/secrets/kubernetes.io",
)
SERVICE_PUBLISHING_FIELDS = (
    "externalIPs",
    "externalName",
    "loadBalancerIP",
    "loadBalancerSourceRanges",
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


def file_sha256(relative_path: str) -> str:
    try:
        return hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest()
    except OSError as error:
        raise AxLabError(f"cannot read {relative_path}: {error}") from error


def validate_sources(sources: Any) -> None:
    exact_keys(sources, set(SOURCE_REPOSITORIES), "sources")
    for name, repository in SOURCE_REPOSITORIES.items():
        keys = {"repository", "commit", "patch"} if name == "ax" else None
        source = exact_keys(sources[name], keys or {"repository", "commit"}, name)
        if source["repository"] != repository:
            raise AxLabError(f"source {name} must use its upstream repository")
        if not isinstance(source["commit"], str) or not COMMIT_RE.fullmatch(
            source["commit"]
        ):
            raise AxLabError(f"source {name} commit must be a full 40-hex SHA")
    patch = exact_keys(sources["ax"]["patch"], {"path", "sha256"}, "ax patch")
    if patch["path"] != AX_PATCH:
        raise AxLabError(f"the AX patch must be the vendored {AX_PATCH}")
    if not isinstance(patch["sha256"], str) or not SHA256_RE.fullmatch(patch["sha256"]):
        raise AxLabError("the AX patch sha256 must be 64 lowercase hex")
    if file_sha256(patch["path"]) != patch["sha256"]:
        raise AxLabError(f"{AX_PATCH} does not hash to its reviewed sha256")


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


def load_free_limit_budget() -> tuple[int, int]:
    """The memory (MiB) and CPU (millicores) of limits every plan that runs
    the ax-lab group leaves free under the budget of config/capacity.yml.

    A transient container beside the node, such as the ate-setup run, has to
    fit in it: the budgets are those of scripts/validate-capacity.py
    (validate_budget), the aggregates the reviewed ones of
    config/capacity-profiles.yml, which scripts/validate-capacity-profiles.py
    recomputes. The operational headroom stays apart, as docs/CAPACITY.md
    requires.
    """
    try:
        contract = load_yaml(CAPACITY_CONTRACT)["capacity_contract"]
        profiles = load_yaml(CAPACITY_PROFILES)["capacity_profiles"]["profiles"]
        memory_budget = int(
            Decimal(
                contract["host"]["minimum_memory_mib"]
                - contract["system_reserve"]["memory_mib"]
                - contract["operational_headroom"]["memory_mib"]
            )
            * Decimal(contract["policy"]["aggregate_memory_limit_overcommit_ratio"])
        )
        cpu_budget = int(
            Decimal(
                contract["host"]["minimum_cpu_millicores"]
                - contract["system_reserve"]["cpu_millicores"]
            )
            * Decimal(contract["policy"]["aggregate_cpu_limit_overcommit_ratio"])
        )
        limits = [
            plan["aggregate"]["limits"]
            for plan in profiles.values()
            if CAPACITY_GROUP in plan["host_containers"]
        ]
        if not limits:
            raise AxLabError(f"no capacity plan runs the {CAPACITY_GROUP} group")
        free = (
            min(memory_budget - limit["memory_mib"] for limit in limits),
            min(cpu_budget - limit["cpu_millicores"] for limit in limits),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise AxLabError("cannot read the capacity plans that run the lab") from error
    return free


def load_operational_headroom() -> int:
    """The memory config/capacity.yml keeps free for work outside budgets."""
    contract = load_yaml(CAPACITY_CONTRACT)
    try:
        headroom = contract["capacity_contract"]["operational_headroom"]["memory_mib"]
    except (KeyError, TypeError) as error:
        raise AxLabError("cannot read the operational memory headroom") from error
    return exact_int(headroom, "operational_headroom memory_mib")


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


def validate_substrate_images(images: Any) -> dict[str, str | None]:
    exact_keys(images, set(SUBSTRATE_IMAGES), "substrate images")
    for name, digest in images.items():
        if digest is None and name in PENDING_IMAGES:
            continue
        if not isinstance(digest, str) or not SUBSTRATE_DIGEST_RE.fullmatch(digest):
            raise AxLabError(
                f"substrate image {name} must be pinned as sha256:<64 hex>"
            )
    return images


def validate_build(build: Any, cluster: dict[str, Any], ratio: Decimal, headroom: int):
    """The fallback build borrows the stopped node's budget, never more."""
    exact_keys(build, BUILD_KEYS, "substrate build")
    for key in sorted(BUILD_KEYS):
        exact_int(build[key], f"substrate build {key}")
    node = cluster["resources"]
    for key in (
        "memory_limit_mib",
        "memory_reservation_mib",
        "cpu_limit_millicores",
        "pids_limit",
    ):
        if build[key] > node[key]:
            raise AxLabError(f"substrate build {key} exceeds the node's")
    if (
        build["memory_reservation_mib"] > build["memory_limit_mib"]
        or Decimal(build["memory_limit_mib"])
        > Decimal(build["memory_reservation_mib"]) * ratio
    ):
        raise AxLabError(
            f"substrate build memory limit/reservation ratio exceeds {ratio}"
        )
    if build["cpu_limit_millicores"] % 1000:
        raise AxLabError("substrate build CPU limit must be whole CPUs (GOMAXPROCS)")
    if build["timeout_seconds"] not in BUILD_TIMEOUT_RANGE:
        raise AxLabError("substrate build timeout_seconds is outside 600-7200")
    if build["mem_available_floor_mib"] != headroom:
        raise AxLabError("substrate build floor must be the operational headroom")
    if build["min_mem_available_mib"] != build["memory_limit_mib"] + headroom:
        raise AxLabError(
            "substrate build min_mem_available_mib must be its limit plus the "
            "operational headroom"
        )


def validate_install(
    install: Any, ratio: Decimal, headroom: int, free: tuple[int, int]
) -> None:
    """ate-setup runs beside the node: it has to fit in what the plan leaves.

    free is load_free_limit_budget(): the memory and CPU of limits the
    plans running the lab leave free. The operational headroom is not spent.
    """
    exact_keys(install, INSTALL_KEYS, "substrate install")
    # B2 of the PR-4 review: the egress MITM CA pool and the agentgateway
    # dataplane are not reviewed (create.go lines 84-90, overlay.go lines
    # 35-46), and there is no key for sdsmint at all.
    if install["atenet_router"] != "envoy":
        raise AxLabError("substrate install atenet_router must be envoy")
    for key in sorted(INSTALL_KEYS - {"atenet_router"}):
        exact_int(install[key], f"substrate install {key}")
    rollout = install["rollout_timeout_seconds"]
    if rollout not in ROLLOUT_TIMEOUT_RANGE:
        raise AxLabError(
            "substrate install rollout_timeout_seconds is outside 300-1800"
        )
    minimum = (
        INSTALL_ROLLOUT_WAITS * rollout
        + INSTALL_FIXED_WAIT_SECONDS
        + INSTALL_MARGIN_SECONDS
    )
    if not minimum <= install["timeout_seconds"] <= INSTALL_TIMEOUT_MAX:
        raise AxLabError(
            f"substrate install timeout_seconds must cover ate-setup's own waits "
            f"({minimum} s) and stay under {INSTALL_TIMEOUT_MAX} s"
        )
    free_memory, free_cpu = free
    if install["memory_limit_mib"] > free_memory:
        raise AxLabError(
            f"substrate install memory limit exceeds the {free_memory} MiB of "
            "limits the capacity plan leaves free"
        )
    if install["cpu_limit_millicores"] > free_cpu:
        raise AxLabError(
            f"substrate install CPU limit exceeds the {free_cpu}m of limits the "
            "capacity plan leaves free"
        )
    if (
        install["memory_reservation_mib"] > install["memory_limit_mib"]
        or Decimal(install["memory_limit_mib"])
        > Decimal(install["memory_reservation_mib"]) * ratio
    ):
        raise AxLabError(
            f"substrate install memory limit/reservation ratio exceeds {ratio}"
        )
    if install["pids_limit"] > 1024:
        raise AxLabError("substrate install PID limit is above the reviewed one")
    if install["mem_available_floor_mib"] != headroom:
        raise AxLabError("substrate install floor must be the operational headroom")
    if install["min_mem_available_mib"] != install["memory_limit_mib"] + headroom:
        raise AxLabError(
            "substrate install min_mem_available_mib must be its limit plus the "
            "operational headroom"
        )


def validate_workloads(workloads: Any, version: str) -> None:
    if not isinstance(workloads, list) or not workloads:
        raise AxLabError("substrate workloads must be a non-empty list")
    keys = []
    referenced = set()
    for index, workload in enumerate(workloads):
        context = f"substrate workloads[{index}]"
        allowed = (
            WORKLOAD_KEYS | {"init_images"}
            if isinstance(workload, dict) and "init_images" in workload
            else WORKLOAD_KEYS
        )
        exact_keys(workload, allowed, context)
        if workload["kind"] not in WORKLOAD_KINDS:
            raise AxLabError(f"{context} kind is not a reviewed workload kind")
        if workload["namespace"] not in SUBSTRATE_NAMESPACES:
            raise AxLabError(f"{context} is outside ate-setup's namespaces")
        if not isinstance(workload["name"], str) or not DNS_LABEL_RE.fullmatch(
            workload["name"]
        ):
            raise AxLabError(f"{context} name must be a DNS label")
        for field in sorted(allowed - {"kind", "namespace", "name"}):
            images = workload[field]
            if not isinstance(images, list) or not images:
                raise AxLabError(f"{context} {field} must be a non-empty list")
            for image in images:
                if image in INSTALLED_IMAGES:
                    referenced.add(image)
                elif not isinstance(image, str) or not UPSTREAM_IMAGE_RE.fullmatch(
                    image
                ):
                    raise AxLabError(
                        f"{context} image must be an installed Substrate image "
                        "or an upstream reference with a tag and a sha256 digest"
                    )
        keys.append((workload["namespace"], workload["kind"], workload["name"]))
    if keys != sorted(set(keys)):
        raise AxLabError("substrate workloads must be unique and sorted")
    if referenced != set(INSTALLED_IMAGES):
        raise AxLabError("substrate workloads must run every installed image")
    atelet = [key for key in keys if key[1] == "DaemonSet"]
    if atelet != [("ate-system", "DaemonSet", f"atelet-{version}")]:
        raise AxLabError("substrate workloads need the one atelet-<version> DaemonSet")


def validate_substrate(
    substrate: Any,
    lab: dict[str, Any],
    ratio: Decimal,
    headroom: int,
    free: tuple[int, int],
) -> dict[str, Any]:
    exact_keys(substrate, SUBSTRATE_KEYS, "substrate")
    version = substrate["version"]
    if (
        not isinstance(version, str)
        or not SUBSTRATE_VERSION_RE.fullmatch(version)
        or len(version) < 7
        or not lab["sources"]["substrate"]["commit"].startswith(version)
    ):
        raise AxLabError(
            "substrate version must be a label-safe abbreviation of the pinned "
            "Substrate commit (at least 7 characters)"
        )
    validate_substrate_images(substrate["images"])
    if substrate["backup_directory"] != BACKUP_DIRECTORY:
        raise AxLabError(f"substrate backup_directory must be {BACKUP_DIRECTORY}")
    validate_build(substrate["build"], lab["cluster"], ratio, headroom)
    validate_install(substrate["install"], ratio, headroom, free)
    validate_workloads(substrate["workloads"], version)
    return substrate


def validate_fallback_builds(value: Any, substrate: dict[str, Any]) -> None:
    """The images the owner lets the role build here, each once and pinned."""
    if not isinstance(value, list) or len(set(map(str, value))) != len(value):
        raise AxLabError("ax_lab_substrate_fallback_builds must list distinct images")
    for name in value:
        if name not in SUBSTRATE_IMAGES:
            raise AxLabError(f"fallback build {name!r} is not a Substrate image")
        if substrate["images"][name] is None:
            raise AxLabError(f"fallback build {name} has no pinned digest to reproduce")


def validate_worker_pool(pool: Any, cluster: dict[str, Any], reserve: Any) -> None:
    """The workers fit in the node and leave it the CPU and memory it needs."""
    exact_keys(pool, WORKER_POOL_KEYS, "ax worker_pool")
    if pool["namespace"] != AX_WORKERS_NAMESPACE:
        raise AxLabError(f"ax worker_pool namespace must be {AX_WORKERS_NAMESPACE}")
    if not isinstance(pool["name"], str) or not DNS_LABEL_RE.fullmatch(pool["name"]):
        raise AxLabError("ax worker_pool name must be a DNS label")
    for key in sorted(WORKER_POOL_KEYS - {"namespace", "name"}):
        exact_int(pool[key], f"ax worker_pool {key}")
    if exact_int(reserve, "ax node_platform_reserve_mib") < NODE_PLATFORM_RESERVE_MIB:
        raise AxLabError(
            f"ax node_platform_reserve_mib must keep at least "
            f"{NODE_PLATFORM_RESERVE_MIB} MiB of the node for the platform"
        )
    node = cluster["resources"]
    if pool["replicas"] * pool["memory_mib"] > node["memory_limit_mib"] - reserve:
        raise AxLabError(
            "ax worker_pool memory exceeds the node's limit minus "
            "node_platform_reserve_mib"
        )
    if pool["cpu_request_millicores"] > pool["cpu_limit_millicores"]:
        raise AxLabError("ax worker_pool requests more CPU than its limit")
    kept = node["cpu_limit_millicores"] - NODE_CPU_KEPT_MILLICORES
    if pool["replicas"] * pool["cpu_limit_millicores"] > kept:
        raise AxLabError(
            f"ax worker_pool CPU limits must leave {NODE_CPU_KEPT_MILLICORES}m of "
            f"the node's CPU: replicas x cpu_limit_millicores at most {kept}m"
        )


def validate_ax(ax: Any, lab: dict[str, Any]) -> dict[str, Any]:
    exact_keys(ax, AX_KEYS, "ax")
    tag = ax["tag"]
    commit = lab["sources"]["ax"]["commit"]
    if (
        not isinstance(tag, str)
        or not AX_TAG_RE.fullmatch(tag)
        or "latest" in tag
        or not tag.startswith(commit[:7] + "-")
    ):
        raise AxLabError(
            "ax tag must be an image tag naming the pinned AX commit's first 7 "
            "characters"
        )
    images = exact_keys(ax["images"], set(AX_IMAGES), "ax images")
    for name in AX_IMAGES:
        digest = images[name]
        if not isinstance(digest, str) or not SUBSTRATE_DIGEST_RE.fullmatch(digest):
            raise AxLabError(f"ax image {name} must be pinned as sha256:<64 hex>")
    for name in AX_KO_IMAGES:
        vendored = f"{AX_KO_MANIFESTS}/{name}.json"
        if "sha256:" + file_sha256(vendored) != images[name]:
            raise AxLabError(f"{vendored} does not hash to the {name} pin")
    for key in ("task_runner_binary_sha256", "cli_sha256"):
        if not isinstance(ax[key], str) or not SHA256_RE.fullmatch(ax[key]):
            raise AxLabError(f"ax {key} must be 64 lowercase hex")
    if not isinstance(ax["ko_base_image"], str) or not KO_BASE_IMAGE_RE.fullmatch(
        ax["ko_base_image"]
    ):
        raise AxLabError("ax ko_base_image must be cgr.dev/chainguard/static by digest")
    if ax["cli_backup"] != AX_CLI_BACKUP:
        raise AxLabError(f"ax cli_backup must be {AX_CLI_BACKUP}")
    if ax["snapshots_bucket"] != AX_SNAPSHOTS_BUCKET:
        raise AxLabError(f"ax snapshots_bucket must be {AX_SNAPSHOTS_BUCKET}")
    redis = exact_keys(ax["redis"], AX_REDIS_KEYS, "ax redis")
    if type(redis["volume_mib"]) is not int or (
        redis["volume_mib"] not in AX_REDIS_VOLUME_RANGE
    ):
        raise AxLabError("ax redis volume_mib is outside 256-4096")
    if type(redis["save_seconds"]) is not int or (
        redis["save_seconds"] not in AX_REDIS_SAVE_RANGE
    ):
        raise AxLabError("ax redis save_seconds is outside 10-3600")
    validate_worker_pool(
        ax["worker_pool"], lab["cluster"], ax["node_platform_reserve_mib"]
    )
    timeout = ax["router_route_timeout"]
    match = ROUTE_TIMEOUT_RE.fullmatch(timeout) if isinstance(timeout, str) else None
    minutes = None if match is None else int(match[1]) * (60 if match[2] == "h" else 1)
    if minutes is None or minutes > ROUTE_TIMEOUT_MAX_MINUTES:
        raise AxLabError(
            "ax router_route_timeout must be whole minutes or hours, at most 2h"
        )
    return ax


def validate_catalog(
    document: Any,
    reserved: dict[str, str] | None = None,
    capacity: tuple[Decimal, Any] | None = None,
    headroom: int | None = None,
    free: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Validate the whole file and return the `ax_lab` mapping."""
    reject_secret_like(document)
    exact_keys(document, TOP_LEVEL_KEYS, "config")
    if type(document["ax_lab_privileged_node_accepted"]) is not bool:
        raise AxLabError("ax_lab_privileged_node_accepted must be a boolean")
    lab = exact_keys(document["ax_lab"], LAB_KEYS, "ax_lab")
    if (
        type(lab["schema_version"]) is not int
        or lab["schema_version"] != SCHEMA_VERSION
    ):
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
    if images["toolbox"]["tag"] != REVIEWED_TOOLBOX_TAG:
        raise AxLabError(f"the toolbox must be golang {REVIEWED_TOOLBOX_TAG}")
    ratio, group = capacity if capacity is not None else load_capacity_declaration()
    cluster = validate_cluster(lab["cluster"], ratio)
    validate_registry(lab["registry"], cluster, ratio)
    validate_capacity_group(lab, group)
    substrate = validate_substrate(
        lab["substrate"],
        lab,
        ratio,
        load_operational_headroom() if headroom is None else headroom,
        load_free_limit_budget() if free is None else free,
    )
    validate_fallback_builds(document["ax_lab_substrate_fallback_builds"], substrate)
    validate_ax(lab["ax"], lab)
    return lab


def substrate_plan(lab: dict[str, Any]) -> dict[str, Any]:
    """What the reproducibility workflow builds and compares, as JSON."""
    substrate = lab["substrate"]
    return {
        "version": substrate["version"],
        "commit": lab["sources"]["substrate"]["commit"],
        "repository": lab["sources"]["substrate"]["repository"],
        "toolbox_image": lab["images"]["toolbox"],
        "registry_image": lab["images"]["registry"],
        "build": substrate["build"],
        "images": substrate["images"],
    }


def ax_plan(lab: dict[str, Any]) -> dict[str, Any]:
    """What the reproducibility workflow rebuilds and compares for AX, as JSON."""
    ax = lab["ax"]
    return {
        "commit": lab["sources"]["ax"]["commit"],
        "repository": lab["sources"]["ax"]["repository"],
        "patch": lab["sources"]["ax"]["patch"],
        "substrate_commit": lab["sources"]["substrate"]["commit"],
        "toolbox_image": lab["images"]["toolbox"],
        "ko_base_image": ax["ko_base_image"],
        "ko_images": {name: ax["images"][name] for name in AX_KO_IMAGES},
        "ko_manifests": {
            name: f"{AX_KO_MANIFESTS}/{name}.json" for name in AX_KO_IMAGES
        },
        "cli_sha256": ax["cli_sha256"],
        "task_runner_binary_sha256": ax["task_runner_binary_sha256"],
    }


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


def render_ax_manifests(lab: dict[str, Any]) -> dict[str, str]:
    """Render the AX manifests as Ansible's template lookup would."""
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(SYSCTL_TEMPLATE_DIRECTORY / "ax"),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        keep_trailing_newline=True,
    )
    return {
        name: environment.get_template(name + ".j2").render(ax_lab=lab)
        for name in AX_MANIFEST_TEMPLATES
    }


def registry_image(lab: dict[str, Any], name: str) -> str:
    """The reference the cluster pulls an AX image by: tag and pinned digest."""
    return f"localhost:5001/{name}:{lab['ax']['tag']}@{lab['ax']['images'][name]}"


def pod_spec(document: dict[str, Any], context: str) -> dict[str, Any]:
    try:
        spec = document["spec"]["template"]["spec"]
    except (KeyError, TypeError) as error:
        raise AxLabError(f"{context} has no pod template") from error
    if not isinstance(spec, dict):
        raise AxLabError(f"{context} has no pod template")
    return spec


def validate_ax_pod(
    spec: dict[str, Any],
    images: list[str],
    context: str,
    audiences: tuple[str, ...] = (),
) -> None:
    """Never a host namespace, a host path, a host port or an API token.

    `audiences` are the projected service account tokens the pod may hold,
    one each: only ax-controller's, for Substrate, never one the Kubernetes
    API accepts.
    """
    if spec.get("automountServiceAccountToken") is not False:
        raise AxLabError(f"{context} must not mount a Kubernetes API token")
    for field in POD_HOST_FIELDS:
        if spec.get(field):
            raise AxLabError(f"{context} must not use {field}")
    tokens = []
    for volume in spec.get("volumes") or []:
        if "hostPath" in volume:
            raise AxLabError(f"{context} must not mount a hostPath")
        if "secret" in volume:
            raise AxLabError(f"{context} must not mount a Secret")
        for source in (volume.get("projected") or {}).get("sources") or []:
            if "secret" in source:
                raise AxLabError(f"{context} must not mount a Secret")
            if "serviceAccountToken" in source:
                tokens.append((source["serviceAccountToken"] or {}).get("audience"))
    if tuple(tokens) != audiences:
        raise AxLabError(
            f"{context} must project no service account token but " f"{list(audiences)}"
        )
    containers = spec.get("containers") or []
    if spec.get("initContainers") or [c.get("image") for c in containers] != images:
        raise AxLabError(f"{context} must run exactly its pinned images")
    for container in containers:
        security = container.get("securityContext") or {}
        if security.get("privileged") or security.get("allowPrivilegeEscalation"):
            raise AxLabError(f"{context} must not run privileged")
        for port in container.get("ports") or []:
            if "hostPort" in port or "hostIP" in port:
                raise AxLabError(f"{context} must not publish a host port")
        for mount in container.get("volumeMounts") or []:
            path = "/" + str(mount.get("mountPath", "")).strip("/")
            if any(
                path == prefix or path.startswith(prefix + "/")
                for prefix in SERVICE_ACCOUNT_MOUNTS
            ):
                raise AxLabError(
                    f"{context} must not mount anything at {SERVICE_ACCOUNT_MOUNTS[0]}"
                )


def validate_ax_manifests(rendered: dict[str, str], lab: dict[str, Any]) -> None:
    """The AX objects: exactly the reviewed ones, never published, pinned."""
    ax = lab["ax"]
    if list(rendered) != list(AX_MANIFEST_TEMPLATES):
        raise AxLabError("the AX manifests differ from the reviewed files")
    objects: dict[tuple[str, str | None, str], dict[str, Any]] = {}
    for name, text in rendered.items():
        if "ko://" in text:
            raise AxLabError(f"{name} still names a ko:// image")
        try:
            documents = [
                document
                for document in yaml.load_all(text, Loader=UniqueKeyLoader)
                if document is not None
            ]
        except yaml.YAMLError as error:
            raise AxLabError(f"{name} is not YAML: {error}") from error
        keys = []
        for document in documents:
            if not isinstance(document, dict) or not isinstance(
                document.get("metadata"), dict
            ):
                raise AxLabError(f"{name} holds an object without metadata")
            metadata = document["metadata"]
            key = (
                document.get("kind"),
                metadata.get("namespace"),
                metadata.get("name"),
            )
            keys.append(key)
            objects[key] = document
        if keys != AX_INVENTORY[name]:
            raise AxLabError(f"{name} holds other objects than the reviewed ones")
    for namespace in (AX_SYSTEM_NAMESPACE, AX_WORKERS_NAMESPACE):
        labels = objects[("Namespace", None, namespace)]["metadata"].get("labels")
        if labels != MANAGED_LABEL:
            raise AxLabError(f"namespace {namespace} must carry only {MANAGED_LABEL}")
    for key, document in objects.items():
        if key[0] != "Service":
            continue
        spec = document.get("spec") or {}
        # Explicit, so that the ax-lab field manager owns it: server-side
        # apply leaves alone a field it does not own, so the drift diff
        # would never see another hand publish the Service.
        if spec.get("type") != "ClusterIP" or any(
            field in spec for field in SERVICE_PUBLISHING_FIELDS
        ):
            raise AxLabError(
                f"Service {key[2]} must be ClusterIP only, with its type set"
            )
        if any("nodePort" in port for port in spec.get("ports") or []):
            raise AxLabError(f"Service {key[2]} must not open a node port")
    system = AX_SYSTEM_NAMESPACE
    redis = objects[("Deployment", system, "ax-redis")]
    server = objects[("Deployment", system, "ax-server")]
    controller = objects[("Deployment", system, "ax-controller")]
    validate_ax_pod(pod_spec(redis, "ax-redis"), [lab["images"]["redis"]], "ax-redis")
    validate_ax_pod(
        pod_spec(server, "ax-server"), [registry_image(lab, "ax-server")], "ax-server"
    )
    controller_pod = pod_spec(controller, "ax-controller")
    # Substrate authentication: a projected token for Substrate's audience,
    # never the Kubernetes API one (deploy/ax-controller.yaml, lines 100-107).
    validate_ax_pod(
        controller_pod,
        [registry_image(lab, "ax-controller")],
        "ax-controller",
        ("api.ate-system.svc",),
    )
    env = {
        item.get("name"): item.get("value")
        for item in controller_pod["containers"][0].get("env") or []
    }
    if env.get("AX_SNAPSHOTS_BUCKET") != ax["snapshots_bucket"]:
        raise AxLabError("ax-controller must name the reviewed snapshots bucket")
    if controller_pod.get("serviceAccountName") != "ax-controller":
        raise AxLabError("ax-controller must run as its own service account")
    redis_pod = pod_spec(redis, "ax-redis")
    if redis_pod["containers"][0].get("args") != [
        "--save",
        f"{ax['redis']['save_seconds']} 1",
        "--appendonly",
        "no",
    ]:
        raise AxLabError("ax-redis must keep RDB snapshots only")
    if (redis.get("spec") or {}).get("strategy") != {"type": "Recreate"}:
        raise AxLabError("ax-redis must be replaced, never rolled beside itself")
    claim = objects[("PersistentVolumeClaim", system, "ax-redis-data")]
    try:
        storage = claim["spec"]["resources"]["requests"]["storage"]
    except (KeyError, TypeError) as error:
        raise AxLabError("ax-redis-data must request its volume") from error
    if storage != f"{ax['redis']['volume_mib']}Mi":
        raise AxLabError("ax-redis-data must request redis volume_mib")
    validate_ax_network_policies(objects)
    validate_worker_pool_object(
        objects[("WorkerPool", AX_WORKERS_NAMESPACE, "ax")], lab
    )


def validate_ax_network_policies(objects: dict[Any, dict[str, Any]]) -> None:
    """Sandboxes reach neither ax-server nor ax-redis (google/ax#376)."""
    expected = {
        "ax-server": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "ax-server"}},
            "policyTypes": ["Ingress"],
        },
        "ax-redis": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "ax-redis"}},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {
                            "podSelector": {
                                "matchExpressions": [
                                    {
                                        "key": "app.kubernetes.io/name",
                                        "operator": "In",
                                        "values": ["ax-controller", "ax-server"],
                                    }
                                ]
                            }
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 6379}],
                }
            ],
        },
    }
    for name, spec in expected.items():
        if objects[("NetworkPolicy", AX_SYSTEM_NAMESPACE, name)].get("spec") != spec:
            raise AxLabError(f"NetworkPolicy {name} differs from the reviewed one")


def validate_worker_pool_object(document: dict[str, Any], lab: dict[str, Any]) -> None:
    pool = lab["ax"]["worker_pool"]
    version = lab["substrate"]["version"]
    worker_image = (
        f"localhost:5001/ateom-gvisor:{version}"
        f"@{lab['substrate']['images']['ateom-gvisor']}"
    )
    memory = f"{pool['memory_mib']}Mi"
    expected = {
        "replicas": pool["replicas"],
        "sandboxClass": "gvisor",
        "workerImage": worker_image,
        "template": {
            "nodeSelector": {"ate.dev/substrate-version": version},
            "resources": {
                "requests": {
                    "cpu": f"{pool['cpu_request_millicores']}m",
                    "memory": memory,
                },
                "limits": {"cpu": f"{pool['cpu_limit_millicores']}m", "memory": memory},
            },
        },
    }
    if document.get("spec") != expected:
        raise AxLabError("the WorkerPool differs from ax.worker_pool")


def ax_manifests_sha256(rendered: dict[str, str]) -> dict[str, str]:
    return {
        name: hashlib.sha256(text.encode("utf-8")).hexdigest()
        for name, text in rendered.items()
    }


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
    parser.add_argument(
        "--substrate-plan",
        action="store_true",
        help="print only the Substrate build plan as JSON",
    )
    parser.add_argument(
        "--ax-plan",
        action="store_true",
        help="print only the AX reproducibility plan as JSON",
    )
    parser.add_argument(
        "--ax-manifests-sha256",
        action="store_true",
        help="print only the sha256 of each rendered AX manifest, as JSON",
    )
    args = parser.parse_args(argv)
    try:
        lab = validate_catalog(load_yaml(CONFIG))
        rendered = render_sysctl(lab)
        validate_sysctl_render(rendered, lab)
        kind_config = render_kind_config(lab)
        validate_kind_config_render(kind_config, lab)
        ax_manifests = render_ax_manifests(lab)
        validate_ax_manifests(ax_manifests, lab)
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
    if args.substrate_plan:
        print(json.dumps(substrate_plan(lab), sort_keys=True))
        return 0
    if args.ax_plan:
        print(json.dumps(ax_plan(lab), sort_keys=True))
        return 0
    if args.ax_manifests_sha256:
        print(json.dumps(ax_manifests_sha256(ax_manifests), sort_keys=True))
        return 0
    print(
        "AX lab contract, pins, sysctl, kind configuration and AX manifest "
        "renders passed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
