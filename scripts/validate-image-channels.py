#!/usr/bin/env python3
"""Validate the reviewed image channel map against baselines and renders.

`config/image-channels.yml` is the single source of what every rendered
Docker Swarm service runs. Each entry is either a *channel* (`repo:tag`,
resolved to a digest by the Docker CLI on deploy and, when `autoupdate` is
true, by the watcher between deploys) or a *hold* (`repo:tag@sha256:...`, or
the byte-exact digest-only baseline reference, never updated automatically).

The reviewed baselines (`config/services.yml`, `config/organizationweb.yml`
and the Traefik pin in `ansible/group_vars/all.yml`) stay untouched as
restore evidence. This validator binds every entry to its baseline
repository, forces stateful services onto a hardcoded major channel that
matches the baseline major, and proves that the rendered stacks cover every
service exactly once with the matching image and opt-in label.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

import yaml

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1
AUTOUPDATE_LABEL = "apptolast.autoupdate"
TOP_LEVEL_KEYS = {
    "image_channel_schema_version",
    "image_channel_autoupdate_label",
    "image_channel_services",
    "image_channel_exclusions",
}
SERVICE_KEYS = {"stack", "service", "baseline", "reference", "class", "autoupdate"}
BASELINE_KEYS = {"catalog", "component"}
EXCLUSION_KEYS = {"stack", "service", "reason"}
ALLOWED_STACKS = (
    "edge",
    "workloads",
    "organizationweb",
    "observability",
    "autoupdater",
)
RENDERED_STACKS = ("edge", "workloads", "organizationweb", "observability")
CLASSES = {"owner", "third-party", "stateful-major"}
# Owner images follow `:latest`; every other namespace is third-party.
OWNER_NAMESPACES = {
    "docker.io/apptolast",
    "docker.io/hgarciaalberto",
    "docker.io/ocholoko888",
}
OWNER_CHANNEL_TAG = "latest"
# Services that can never join a channel. Adding one is a reviewed edit here.
REQUIRED_EXCLUSIONS = {
    ("workloads", "n8n-runners"),
    ("autoupdater", "shepherd"),
}
DOCKER_SOCKETS = {"/var/run/docker.sock", "/run/docker.sock"}
SOCKET_STACKS = {"autoupdater"}
SERVICE_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")
REFERENCE_RE = re.compile(
    r"(?P<name>[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*)"
    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?"
    r"(?:@(?P<digest>sha256:[a-f0-9]{64}))?"
)
DURATION_RE = re.compile(r"(?:(?:[0-9]+(?:\.[0-9]+)?)(?:ns|us|ms|s|m|h))+")
DURATION_PART_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ns|us|ms|s|m|h)")

# Shepherd switches these on by *presence* (`${VAR+x}`), so `false` enables
# them too. The forbidden ones must be absent; the rest must be absent or on.
SHEPHERD_PRESENCE_SWITCHES = {
    "WITH_REGISTRY_AUTH",
    "WITH_INSECURE_REGISTRY",
    "WITH_NO_RESOLVE_IMAGE",
    "ROLLBACK_ON_FAILURE",
    "RUN_ONCE_AND_EXIT",
}
SHEPHERD_FORBIDDEN_KEYS = {
    "WITH_NO_RESOLVE_IMAGE",
    "RUN_ONCE_AND_EXIT",
    "WITH_INSECURE_REGISTRY",
    "ROLLBACK_ON_FAILURE",
    "IMAGE_AUTOCLEAN_LIMIT",
    "UPDATE_OPTIONS",
    "IGNORELIST_SERVICES",
}
SHEPHERD_ALLOWED_KEYS = {
    "FILTER_SERVICES",
    "SLEEP_TIME",
    "TIMEOUT",
    "VERBOSE",
    "TZ",
    "WITH_REGISTRY_AUTH",
    "REGISTRY_USER",
}
FALSE_LIKE = {"", "0", "false", "no", "off"}


class MajorChannel(NamedTuple):
    """One reviewed major channel for a stateful or ingress service."""

    repository: str
    version_pattern: str
    tag_template: str
    tag: str


# Changing a major is always a reviewed edit of this table plus a data
# migration: the tag must equal the one derived from the baseline version.
STATEFUL_MAJOR_CHANNELS = {
    ("edge", "traefik"): MajorChannel(
        "docker.io/library/traefik",
        r"(?P<major>[0-9]+)\.[0-9]+\.[0-9]+",
        "v{major}",
        "v3",
    ),
    ("workloads", "n8n-db"): MajorChannel(
        "docker.io/pgvector/pgvector",
        r"[0-9]+\.[0-9]+\.[0-9]+-pg(?P<major>[0-9]+)",
        "pg{major}",
        "pg16",
    ),
    ("workloads", "redis-coordinator"): MajorChannel(
        "docker.io/library/redis",
        r"(?P<major>[0-9]+\.[0-9]+)\.[0-9]+-alpine[0-9.]*",
        "{major}-alpine",
        "7.2-alpine",
    ),
    ("workloads", "passbolt-db"): MajorChannel(
        "docker.io/library/postgres",
        r"(?P<major>[0-9]+)\.[0-9]+-alpine[0-9.]*",
        "{major}-alpine",
        "15-alpine",
    ),
    ("workloads", "shlink-db"): MajorChannel(
        "docker.io/library/postgres",
        r"(?P<major>[0-9]+)\.[0-9]+-alpine[0-9.]*",
        "{major}-alpine",
        "16-alpine",
    ),
    ("organizationweb", "postgres"): MajorChannel(
        "docker.io/library/postgres",
        r"(?P<major>[0-9]+)\.[0-9]+-alpine[0-9.]*",
        "{major}-alpine",
        "17-alpine",
    ),
    ("organizationweb", "rabbitmq"): MajorChannel(
        "docker.io/library/rabbitmq",
        r"(?P<major>[0-9]+\.[0-9]+)\.[0-9]+-management-alpine",
        "{major}-management-alpine",
        "4.3-management-alpine",
    ),
}
# The OrganizationWeb baselines are digest-only. Their upstream version was
# reviewed for exactly these digests; a new digest must be re-reviewed here.
REVIEWED_BASELINE_VERSIONS = {
    ("organizationweb", "postgres"): (
        "sha256:"
        "9ae4e8f8d0284836a505f0b2e825144e"
        "32e20499856e7dc5f7b99e19d10eedd6",
        "17.11-alpine3.23",
    ),
    ("organizationweb", "rabbitmq"): (
        "sha256:"
        "ac1201d1dc227779d93c607f976e7f91"
        "56fd5b551ee5ff7946cab6c242091bf3",
        "4.3.5-management-alpine",
    ),
}


class ChannelError(RuntimeError):
    """The image channel map or a rendered stack breaks the contract."""


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader which rejects duplicate mapping keys."""


def construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found an unhashable key: {exc}",
                key_node.start_mark,
            ) from exc
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
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    construct_unique_mapping,
)


def load_unique_yaml(path: Path) -> dict[str, Any]:
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ChannelError(f"cannot load YAML: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ChannelError(f"YAML document is not a mapping: {path}")
    return document


def parse_reference(reference: Any) -> dict[str, str | None]:
    if not isinstance(reference, str):
        raise ChannelError("image reference must be a string")
    match = REFERENCE_RE.fullmatch(reference)
    if match is None:
        raise ChannelError(f"image reference is malformed: {reference!r}")
    return {
        "name": match.group("name"),
        "tag": match.group("tag"),
        "digest": match.group("digest"),
    }


def normalize_repository(name: str) -> str:
    """Expand a repository name the way the Docker CLI does."""
    first, _, rest = name.partition("/")
    if not rest or ("." not in first and first != "localhost"):
        name = f"docker.io/{name}"
    if name.startswith("docker.io/") and name.count("/") == 1:
        name = "docker.io/library/" + name.removeprefix("docker.io/")
    return name


def familiar_repository(repository: str) -> str:
    """Return the short form Swarm stores in a service spec."""
    if repository.startswith("docker.io/library/"):
        return repository.removeprefix("docker.io/library/")
    return repository.removeprefix("docker.io/")


def reference_tag(reference: Any) -> str | None:
    parsed = parse_reference(reference)
    return parsed["tag"]


def repository_of(reference: Any) -> str:
    return normalize_repository(str(parse_reference(reference)["name"]))


def load_baselines(
    root: Path = PROJECT_DIR,
    services: dict[str, Any] | None = None,
    organizationweb: dict[str, Any] | None = None,
    group_vars: dict[str, Any] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Index every reviewed baseline image by (catalog, component)."""
    if services is None:
        services = load_unique_yaml(root / "config/services.yml")
    if organizationweb is None:
        organizationweb = load_unique_yaml(root / "config/organizationweb.yml")
    if group_vars is None:
        group_vars = load_unique_yaml(root / "ansible/group_vars/all.yml")
    baselines: dict[tuple[str, str], dict[str, Any]] = {}

    def add(key: tuple[str, str], reference: Any, version: Any) -> None:
        if key in baselines:
            raise ChannelError(f"ambiguous baseline {key[0]}/{key[1]}")
        if not isinstance(reference, str):
            raise ChannelError(f"baseline {key[0]}/{key[1]} has no reference")
        baselines[key] = {"reference": reference, "version": version}

    approved = services.get("approved_services")
    if not isinstance(approved, list):
        raise ChannelError("service catalog has no approved services")
    for service in approved:
        if not isinstance(service, dict) or not isinstance(service.get("id"), str):
            raise ChannelError("service catalog entry is malformed")
        if service["id"] in {"organizationweb", "observability"}:
            raise ChannelError("service catalog shadows a reserved baseline")
        for image in service.get("images", []):
            if not isinstance(image, dict):
                raise ChannelError("service catalog image is malformed")
            source = image.get("source_reference")
            add(
                (service["id"], str(image.get("component"))),
                image.get("reference"),
                reference_tag(source) if source is not None else None,
            )

    app = organizationweb.get("organizationweb")
    if not isinstance(app, dict) or not isinstance(app.get("images"), dict):
        raise ChannelError("OrganizationWeb catalog has no images")
    for name, reference in app["images"].items():
        add(("organizationweb", str(name)), reference, None)

    components = (
        services.get("internal_platform", {})
        .get("observability", {})
        .get("components")
    )
    if not isinstance(components, list):
        raise ChannelError("observability component catalog is incomplete")
    for component in components:
        if not isinstance(component, dict):
            raise ChannelError("observability component is malformed")
        image = component.get("image")
        add(
            ("observability", str(component.get("id"))),
            image,
            reference_tag(image),
        )

    traefik = baselines.get(("traefik-edge", "proxy"))
    traefik_image = group_vars.get("edge_traefik_image")
    traefik_version = group_vars.get("edge_traefik_version")
    if traefik is None or traefik["reference"] != traefik_image:
        raise ChannelError("Traefik catalog baseline differs from the edge pin")
    if not isinstance(traefik_version, str):
        raise ChannelError("Traefik baseline version is missing")
    traefik["version"] = traefik_version
    return baselines


def exact_keys(value: Any, keys: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ChannelError(f"{context}: unexpected or missing keys")
    return value


def expected_class(stack: str, service: str, repository: str) -> str:
    if (stack, service) in STATEFUL_MAJOR_CHANNELS:
        return "stateful-major"
    if repository.rsplit("/", 1)[0] in OWNER_NAMESPACES:
        return "owner"
    return "third-party"


def baseline_version(
    stack: str,
    service: str,
    baseline: dict[str, Any],
) -> str:
    reviewed = REVIEWED_BASELINE_VERSIONS.get((stack, service))
    if reviewed is not None:
        digest, version = reviewed
        if parse_reference(baseline["reference"])["digest"] != digest:
            raise ChannelError(
                f"{stack}/{service}: baseline digest changed; re-review its version"
            )
        return version
    version = baseline.get("version")
    if not isinstance(version, str):
        raise ChannelError(f"{stack}/{service}: baseline has no reviewed version")
    return version


def check_major_channel(
    stack: str,
    service: str,
    repository: str,
    tag: str | None,
    baseline: dict[str, Any],
) -> None:
    channel = STATEFUL_MAJOR_CHANNELS[(stack, service)]
    if repository != channel.repository:
        raise ChannelError(f"{stack}/{service}: major channel repository differs")
    version = baseline_version(stack, service, baseline)
    match = re.fullmatch(channel.version_pattern, version)
    if match is None:
        raise ChannelError(f"{stack}/{service}: baseline version is unparseable")
    derived = channel.tag_template.format(major=match.group("major"))
    if derived != channel.tag:
        raise ChannelError(
            f"{stack}/{service}: major channel {channel.tag} differs from the "
            f"baseline major {derived}"
        )
    if tag is not None and tag != channel.tag:
        raise ChannelError(
            f"{stack}/{service}: tag {tag} is not the reviewed major channel "
            f"{channel.tag}"
        )


def derive_entry(
    raw: Any,
    baselines: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    entry = exact_keys(raw, SERVICE_KEYS, "channel entry")
    stack = entry["stack"]
    service = entry["service"]
    if stack not in ALLOWED_STACKS:
        raise ChannelError(f"channel entry stack is not allowed: {stack!r}")
    if not isinstance(service, str) or SERVICE_NAME_RE.fullmatch(service) is None:
        raise ChannelError(f"{stack}: channel entry service is invalid")
    context = f"{stack}/{service}"
    baseline_key = exact_keys(entry["baseline"], BASELINE_KEYS, f"{context} baseline")
    if not all(isinstance(value, str) for value in baseline_key.values()):
        raise ChannelError(f"{context}: baseline keys must be strings")
    baseline = baselines.get((baseline_key["catalog"], baseline_key["component"]))
    if baseline is None:
        raise ChannelError(f"{context}: baseline is not in the reviewed catalogs")
    if type(entry["autoupdate"]) is not bool:
        raise ChannelError(f"{context}: autoupdate must be a boolean")
    if entry["class"] not in CLASSES:
        raise ChannelError(f"{context}: class is not allowed")

    reference = entry["reference"]
    parsed = parse_reference(reference)
    tag = parsed["tag"]
    digest = parsed["digest"]
    repository = normalize_repository(str(parsed["name"]))
    baseline_repository = repository_of(baseline["reference"])
    if repository != baseline_repository:
        raise ChannelError(f"{context}: repository differs from its baseline")
    if tag is None and digest is None:
        raise ChannelError(f"{context}: a bare repository implies :latest")
    if tag is None and reference != baseline["reference"]:
        raise ChannelError(
            f"{context}: a digest-only reference is allowed only as the exact "
            "baseline hold"
        )
    mode = "hold" if digest is not None else "channel"
    if mode == "hold" and entry["autoupdate"]:
        raise ChannelError(f"{context}: a hold can never auto-update")

    klass = expected_class(stack, service, repository)
    if entry["class"] != klass:
        raise ChannelError(f"{context}: class must be {klass}")
    if klass == "owner" and tag is not None and tag != OWNER_CHANNEL_TAG:
        raise ChannelError(f"{context}: owner images follow :{OWNER_CHANNEL_TAG}")
    if klass == "stateful-major":
        check_major_channel(stack, service, repository, tag, baseline)

    familiar = familiar_repository(repository)
    spec_base = familiar + (f":{tag}" if tag is not None else "")
    if mode == "hold":
        spec_exact = f"{spec_base}@{digest}"
        spec_pattern = "^" + re.escape(spec_exact) + "$"
        preflight_reference = f"{parsed['name']}@{digest}"
    else:
        spec_exact = None
        spec_pattern = "^" + re.escape(spec_base) + "@sha256:[a-f0-9]{64}$"
        preflight_reference = reference
    return {
        "stack": stack,
        "service": service,
        "baseline": dict(baseline_key),
        "is_baseline": reference == baseline["reference"],
        "reference": reference,
        "class": klass,
        "autoupdate": entry["autoupdate"],
        "label": "true" if entry["autoupdate"] else "false",
        "mode": mode,
        "repository": repository,
        "repository_familiar": familiar,
        "tag": tag,
        "digest": digest,
        "spec_exact": spec_exact,
        "spec_pattern": spec_pattern,
        "preflight_reference": preflight_reference,
    }


def derive_channels(
    document: Any,
    baselines: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Validate the channel document and return the per-stack channel map."""
    exact_keys(document, TOP_LEVEL_KEYS, "image channel map")
    version = document["image_channel_schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ChannelError("image channel schema version is unsupported")
    if document["image_channel_autoupdate_label"] != AUTOUPDATE_LABEL:
        raise ChannelError("autoupdate label must be the tool-neutral label")
    raw_services = document["image_channel_services"]
    raw_exclusions = document["image_channel_exclusions"]
    if not isinstance(raw_services, list) or not raw_services:
        raise ChannelError("image channel services must be a non-empty list")
    if not isinstance(raw_exclusions, list):
        raise ChannelError("image channel exclusions must be a list")

    seen: set[tuple[str, str]] = set()
    services: dict[str, dict[str, Any]] = {}
    for raw in raw_services:
        entry = derive_entry(raw, baselines)
        key = (entry["stack"], entry["service"])
        if key in seen:
            raise ChannelError(f"duplicate channel entry {key[0]}/{key[1]}")
        seen.add(key)
        services.setdefault(entry["stack"], {})[entry["service"]] = entry

    exclusions: dict[str, list[str]] = {}
    excluded: set[tuple[str, str]] = set()
    for raw in raw_exclusions:
        item = exact_keys(raw, EXCLUSION_KEYS, "channel exclusion")
        key = (item["stack"], item["service"])
        if key in seen or key in excluded:
            raise ChannelError(f"duplicate channel entry {key[0]}/{key[1]}")
        if not isinstance(item["reason"], str) or not item["reason"].strip():
            raise ChannelError(f"{key[0]}/{key[1]}: exclusion needs a reason")
        excluded.add(key)
        exclusions.setdefault(str(item["stack"]), []).append(str(item["service"]))
    if excluded != REQUIRED_EXCLUSIONS:
        raise ChannelError("channel exclusions differ from the reviewed set")
    for names in exclusions.values():
        names.sort()
    return {
        "schema_version": SCHEMA_VERSION,
        "autoupdate_label": AUTOUPDATE_LABEL,
        "services": services,
        "exclusions": exclusions,
    }


def load_channel_map(root: Path = PROJECT_DIR, **baseline_overrides: Any) -> dict[str, Any]:
    document = load_unique_yaml(root / "config/image-channels.yml")
    return derive_channels(document, load_baselines(root, **baseline_overrides))


def parse_duration(value: Any) -> float:
    if not isinstance(value, str) or DURATION_RE.fullmatch(value) is None:
        raise ChannelError(f"duration is malformed: {value!r}")
    scale = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}
    return sum(
        float(amount) * scale[unit]
        for amount, unit in DURATION_PART_RE.findall(value)
    )


def has_active_healthcheck(service: dict[str, Any]) -> bool:
    healthcheck = service.get("healthcheck")
    if not isinstance(healthcheck, dict) or healthcheck.get("disable") is True:
        return False
    test = healthcheck.get("test")
    if isinstance(test, list):
        return bool(test) and test[0] != "NONE"
    return isinstance(test, str) and bool(test.strip())


def socket_binds(service: dict[str, Any]) -> list[str]:
    found = []
    for volume in service.get("volumes", []) or []:
        source = None
        if isinstance(volume, dict):
            source = volume.get("source")
        elif isinstance(volume, str):
            source = volume.split(":", 1)[0]
        if isinstance(source, str) and (
            source in DOCKER_SOCKETS or source.endswith("/docker.sock")
        ):
            found.append(source)
    return found


def normalize_environment(environment: Any) -> dict[str, str]:
    if environment is None:
        return {}
    if isinstance(environment, dict):
        result = {}
        for key, value in environment.items():
            if not isinstance(key, str):
                raise ChannelError("environment key must be a string")
            result[key] = "" if value is None else str(value)
        return result
    if isinstance(environment, list):
        result = {}
        for item in environment:
            if not isinstance(item, str):
                raise ChannelError("environment item must be a string")
            key, _, value = item.partition("=")
            if key in result:
                raise ChannelError(f"duplicate environment key {key}")
            result[key] = value
        return result
    raise ChannelError("environment must be a mapping or a list")


def validate_autoupdater_environment(environment: Any, label: str) -> None:
    """Reject every Shepherd setting that widens or silently alters its scope."""
    values = normalize_environment(environment)
    for key in sorted(values):
        if key in SHEPHERD_PRESENCE_SWITCHES and values[key].strip().lower() in FALSE_LIKE:
            raise ChannelError(
                f"{key} must be absent, not false: its presence alone enables it"
            )
        if key in SHEPHERD_FORBIDDEN_KEYS:
            raise ChannelError(f"{key} must be absent from the watcher")
    unexpected = set(values) - SHEPHERD_ALLOWED_KEYS
    if unexpected:
        raise ChannelError(f"unexpected watcher settings: {sorted(unexpected)}")
    if values.get("FILTER_SERVICES", "") != f"label={label}=true":
        raise ChannelError(
            "FILTER_SERVICES must select exactly the opt-in label; an empty "
            "filter selects every service"
        )
    if ("WITH_REGISTRY_AUTH" in values) != ("REGISTRY_USER" in values):
        raise ChannelError("registry auth and registry user must be set together")
    if "WITH_REGISTRY_AUTH" in values and values["WITH_REGISTRY_AUTH"] != "true":
        raise ChannelError("WITH_REGISTRY_AUTH must be absent or exactly true")
    if "REGISTRY_USER" in values and not values["REGISTRY_USER"]:
        raise ChannelError("REGISTRY_USER must not be empty")


def service_label(service: dict[str, Any], label: str) -> Any:
    deploy = service.get("deploy")
    labels = deploy.get("labels") if isinstance(deploy, dict) else None
    if labels is None:
        return None
    if isinstance(labels, list):
        labels = dict(item.partition("=")[::2] for item in labels)
    if not isinstance(labels, dict):
        raise ChannelError("service deploy labels are malformed")
    return labels.get(label)


def validate_rendered(
    channel_map: dict[str, Any],
    rendered: dict[str, Any],
) -> None:
    """Prove each rendered stack covers every service exactly once."""
    label = channel_map["autoupdate_label"]
    services = channel_map["services"]
    exclusions = channel_map["exclusions"]
    for stack in RENDERED_STACKS:
        if stack not in rendered:
            raise ChannelError(f"rendered stack is missing: {stack}")
    for stack in ALLOWED_STACKS:
        if stack not in rendered and services.get(stack):
            raise ChannelError(f"channel entries target an unrendered stack: {stack}")
    for stack, document in rendered.items():
        if stack not in ALLOWED_STACKS:
            raise ChannelError(f"rendered stack is not allowed: {stack}")
        if not isinstance(document, dict) or not isinstance(
            document.get("services"), dict
        ):
            raise ChannelError(f"rendered stack {stack} has no services")
        rendered_services = document["services"]
        entries = services.get(stack, {})
        excluded = set(exclusions.get(stack, []))
        expected = set(entries) | excluded
        if set(rendered_services) != expected:
            missing = sorted(set(rendered_services) - expected)
            extra = sorted(expected - set(rendered_services))
            raise ChannelError(
                f"{stack}: rendered services differ from the channel map "
                f"(unlisted {missing}, not rendered {extra})"
            )
        for name, service in rendered_services.items():
            if not isinstance(service, dict):
                raise ChannelError(f"{stack}/{name}: rendered service is malformed")
            if stack not in SOCKET_STACKS and socket_binds(service):
                raise ChannelError(
                    f"{stack}/{name}: the Docker socket is allowed only in "
                    "the autoupdater stack"
                )
            observed_label = service_label(service, label)
            if name in excluded:
                if observed_label not in (None, "false"):
                    raise ChannelError(f"{stack}/{name}: excluded service opts in")
                continue
            entry = entries[name]
            if service.get("image") != entry["reference"]:
                raise ChannelError(f"{stack}/{name}: image drift from its channel")
            if observed_label != entry["label"]:
                raise ChannelError(
                    f"{stack}/{name}: {label} label must be {entry['label']!r}"
                )
            if entry["autoupdate"]:
                deploy = service.get("deploy") or {}
                update = deploy.get("update_config") or {}
                if update.get("failure_action") != "rollback":
                    raise ChannelError(
                        f"{stack}/{name}: auto-update needs failure_action rollback"
                    )
                if parse_duration(update.get("monitor")) <= 0:
                    raise ChannelError(
                        f"{stack}/{name}: auto-update needs a monitor window"
                    )
                if not has_active_healthcheck(service):
                    raise ChannelError(
                        f"{stack}/{name}: auto-update needs a healthcheck"
                    )
        if stack == "autoupdater":
            for name, service in rendered_services.items():
                validate_autoupdater_environment(service.get("environment"), label)


def load_rendered(build_dir: Path) -> dict[str, Any]:
    """Load rendered stacks; YAML merge keys (`<<:`) may be overridden."""
    rendered: dict[str, Any] = {}
    for stack in ALLOWED_STACKS:
        path = build_dir / stack / "stack.yml"
        if stack not in RENDERED_STACKS and not path.exists():
            continue
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ChannelError(f"cannot load rendered stack: {path}: {exc}") from exc
        if not isinstance(document, dict):
            raise ChannelError(f"rendered stack is not a mapping: {path}")
        rendered[stack] = document
    return rendered


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=PROJECT_DIR)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--build-dir", type=Path)
    subparsers.add_parser("derive")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        channel_map = load_channel_map(args.root)
        if args.command == "derive":
            print(json.dumps(channel_map, sort_keys=True))
            return 0
        build_dir = args.build_dir or args.root / ".build"
        validate_rendered(channel_map, load_rendered(build_dir))
    except ChannelError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("Image channel map, baselines and rendered stacks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
