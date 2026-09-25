#!/usr/bin/env python3
"""Validate the reviewed AX lab contract offline.

`config/ax-lab.yml` is the only input. The validator pins its exact shape:
the install root under /opt/dockerswarm, the credential directory as a path
only, the inotify limits kind recommends (disjoint from every sysctl key the
host_baseline and platform roles manage), the AX and Substrate source
commits, the official kind and kubectl release assets with their sha256, and
the upstream images by tag and digest. It rejects secret-like keys and
values anywhere in the file, then renders the role's sysctl file and checks
that it sets exactly the reviewed values. It reads nothing outside this
repository, so CI runs it without a production host.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/ax-lab.yml"
SYSCTL_TEMPLATE_DIRECTORY = ROOT / "ansible/roles/ax_lab/templates"
SYSCTL_TEMPLATE = "99-z-dockerswarm-ax-lab.conf.j2"
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


def validate_catalog(
    document: Any, reserved: dict[str, str] | None = None
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write the rendered sysctl file")
    args = parser.parse_args(argv)
    try:
        lab = validate_catalog(load_yaml(CONFIG))
        rendered = render_sysctl(lab)
        validate_sysctl_render(rendered, lab)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
    except (AxLabError, OSError, jinja2.TemplateError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("AX lab contract, pins and sysctl render passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
