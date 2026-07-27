#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import datetime
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import yaml


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONTRACT = PROJECT_DIR / "config/services.yml"
PLATFORM_CONTRACT = PROJECT_DIR / "config/platform.yml"
ANSIBLE_GROUP_VARS = PROJECT_DIR / "ansible/group_vars/all.yml"

IDENTIFIER_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
COMPOUND_ALIAS_RE = re.compile(
    rf"{IDENTIFIER_RE.pattern}(?:/{IDENTIFIER_RE.pattern})+"
)
SEMVER_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?"
)
IMAGE_RE = re.compile(
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
    r"@sha256:[a-f0-9]{64}"
)

EXPECTED_APPROVED = frozenset(
    {
        "kropia",
        "traefik-edge",
        "minecraft-stats",
        "minecraft",
        "n8n",
        "openclaw-clean",
        "passbolt",
        "personal-website-alberto",
        "personal-website-pablo",
        "shlink",
    }
)
EXPECTED_MIGRATIONS = {
    "kropia": "redeploy",
    "traefik-edge": "restore-state",
    "minecraft-stats": "redeploy",
    "minecraft": "restore-state",
    "n8n": "restore-state",
    "openclaw-clean": "clean-install",
    "passbolt": "restore-state",
    "personal-website-alberto": "redeploy",
    "personal-website-pablo": "redeploy",
    "shlink": "restore-state",
}
EXPECTED_HOSTNAMES = {
    "kropia": frozenset({"kropia.apptolast.com"}),
    "traefik-edge": frozenset({"edge.apptolast.com"}),
    "minecraft-stats": frozenset({"minecraft-stats.apptolast.com"}),
    "minecraft": frozenset(),
    "n8n": frozenset({"n8n.apptolast.com"}),
    "openclaw-clean": frozenset({"openclaw.apptolast.com"}),
    "passbolt": frozenset({"passbolt.apptolast.com"}),
    "personal-website-alberto": frozenset(
        {"albertohidalgo.apptolast.com"}
    ),
    "personal-website-pablo": frozenset(
        {"pablohurtadohg.apptolast.com"}
    ),
    "shlink": frozenset({"generadorcodigosqr.apptolast.com"}),
}
EXPECTED_DENIED = {
    "greenhouse": frozenset(),
    "hermes": frozenset(),
    "inern-seller": frozenset({"inemsellar"}),
    "invernaderos-api": frozenset(),
    "menus-admin": frozenset({"menus-dev"}),
    "whoop": frozenset(),
    "cattle": frozenset({"rancher"}),
    "vpn": frozenset(),
    "cluster-ops": frozenset(),
    "redisinsight": frozenset(),
    "ficsit-monitor": frozenset(),
    "gibbon": frozenset(),
    "health-dashboard": frozenset(),
    "keel": frozenset(),
    "kube-system": frozenset(),
    "langflow": frozenset(),
    "longhorn-system": frozenset(),
    "metal": frozenset(),
    "monitoring-dozzle": frozenset(),
    "openclaw-legacy": frozenset(),
    "mcp-fullstack": frozenset({"cyberlab/platform"}),
}
EXPECTED_OBSERVABILITY_COMPONENTS = frozenset(
    {
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
)
EXPECTED_OBSERVABILITY_DATA = frozenset(
    {"prometheus", "alertmanager", "loki", "alloy", "grafana"}
)


class ValidationError(ValueError):
    """The catalog does not conform to its reviewed schema."""


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
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    construct_unique_mapping,
)


def load_yaml_text(text: str, context: str) -> Any:
    try:
        return yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise ValidationError(f"cannot parse {context}: {error}") from error


def load_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValidationError(f"cannot read {path}: {error}") from error
    return load_yaml_text(text, str(path))


def expect_mapping(
    value: Any,
    required: set[str],
    context: str,
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{context} must be a mapping")
    optional = optional or set()
    actual = set(value)
    allowed = required | optional
    missing = required - actual
    unexpected = actual - allowed
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append(
                "missing " + ", ".join(sorted(str(item) for item in missing))
            )
        if unexpected:
            details.append(
                "unexpected "
                + ", ".join(sorted(str(item) for item in unexpected))
            )
        raise ValidationError(f"{context} has {'; '.join(details)}")
    return value


def expect_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{context} must be a list")
    return value


def validate_identifier(value: Any, context: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise ValidationError(f"{context} is not a normalized identifier")
    return value


def validate_alias(value: Any, context: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{context} must be a string")
    if IDENTIFIER_RE.fullmatch(value) or COMPOUND_ALIAS_RE.fullmatch(value):
        return value
    raise ValidationError(f"{context} is not a normalized alias")


def validate_hostname(value: Any, context: str) -> str:
    if not isinstance(value, str) or len(value) > 253 or "." not in value:
        raise ValidationError(f"{context} is not a valid hostname")
    labels = value.split(".")
    label_re = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
    if any(not label_re.fullmatch(label) for label in labels):
        raise ValidationError(f"{context} is not a valid lowercase hostname")
    return value


def validate_unique_strings(
    value: Any,
    context: str,
    validator: Callable[[Any, str], str],
) -> list[str]:
    items = expect_list(value, context)
    validated = [
        validator(item, f"{context}[{index}]")
        for index, item in enumerate(items)
    ]
    if len(validated) != len(set(validated)):
        raise ValidationError(f"{context} contains duplicate entries")
    return validated


def validate_path(value: Any, context: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValidationError(f"{context} must be an absolute POSIX path")
    path = PurePosixPath(value)
    if path == PurePosixPath("/") or path.as_posix() != value:
        raise ValidationError(f"{context} must be a canonical non-root path")
    if ".." in path.parts:
        raise ValidationError(f"{context} must not contain parent traversal")
    return path


def require_child(
    path: PurePosixPath,
    root: PurePosixPath,
    context: str,
) -> None:
    if path == root or not path.is_relative_to(root):
        raise ValidationError(f"{context} must be below {root}")


def validate_no_path_overlap(
    entries: list[tuple[str, PurePosixPath]],
) -> None:
    for index, (left_context, left) in enumerate(entries):
        for right_context, right in entries[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise ValidationError(
                    f"path overlap between {left_context} ({left}) and "
                    f"{right_context} ({right})"
                )


def validate_image(value: Any, context: str) -> str:
    if not isinstance(value, str) or not IMAGE_RE.fullmatch(value):
        raise ValidationError(
            f"{context} must be an image reference pinned by sha256 digest"
        )
    return value


def validate_port_number(value: Any, context: str) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValidationError(f"{context} must be an integer from 1 to 65535")
    return value


def validate_port(
    value: Any,
    context: str,
    *,
    component_required: bool = False,
) -> tuple[int | None, str, int]:
    required = {
        "name",
        "target",
        "published",
        "protocol",
        "exposure",
    }
    if component_required:
        required.add("component")
    port = expect_mapping(value, required, context, {"source_target"})
    validate_identifier(port["name"], f"{context}.name")
    target = validate_port_number(port["target"], f"{context}.target")
    if "source_target" in port:
        validate_port_number(
            port["source_target"],
            f"{context}.source_target",
        )
    protocol = port["protocol"]
    if protocol not in {"tcp", "udp"}:
        raise ValidationError(f"{context}.protocol must be tcp or udp")
    exposure = port["exposure"]
    if exposure not in {"edge", "public", "internal"}:
        raise ValidationError(f"{context}.exposure is unsupported")
    published = port["published"]
    if exposure == "public":
        published = validate_port_number(
            published,
            f"{context}.published",
        )
    elif published is not None:
        raise ValidationError(
            f"{context}.published must be null for {exposure} exposure"
        )
    return published, protocol, target


def validate_external_contracts() -> tuple[dict[str, Any], dict[str, Any]]:
    platform = load_yaml(PLATFORM_CONTRACT)
    if not isinstance(platform, dict):
        raise ValidationError("config/platform.yml must contain a mapping")
    group_vars = load_yaml(ANSIBLE_GROUP_VARS)
    if not isinstance(group_vars, dict):
        raise ValidationError("ansible/group_vars/all.yml must contain a mapping")
    return platform, group_vars


def validate_catalog(
    catalog_value: Any,
    platform: dict[str, Any],
    group_vars: dict[str, Any],
) -> dict[str, int]:
    catalog = expect_mapping(
        catalog_value,
        {
            "schema_version",
            "catalog_version",
            "source_audit",
            "target",
            "approved_services",
            "denied_services",
            "datasets",
            "internal_platform",
        },
        "catalog",
    )
    if type(catalog["schema_version"]) is not int:
        raise ValidationError("schema_version must be the integer 1")
    if catalog["schema_version"] != 1:
        raise ValidationError("only schema_version 1 is supported")
    version = catalog["catalog_version"]
    if not isinstance(version, str) or not SEMVER_RE.fullmatch(version):
        raise ValidationError("catalog_version must be a semantic version")

    source = expect_mapping(
        catalog["source_audit"],
        {"repository", "audited_at", "runtime_root"},
        "source_audit",
    )
    if source["repository"] != "MigracionNetCup":
        raise ValidationError("source_audit.repository must be MigracionNetCup")
    audited_at = source["audited_at"]
    if not isinstance(audited_at, str):
        raise ValidationError("source_audit.audited_at must be an ISO date")
    try:
        datetime.date.fromisoformat(audited_at)
    except ValueError as error:
        raise ValidationError(
            "source_audit.audited_at must be an ISO date"
        ) from error
    source_root = validate_path(
        source["runtime_root"],
        "source_audit.runtime_root",
    )
    if source_root != PurePosixPath("/srv/apptolast"):
        raise ValidationError("the audited runtime root must remain /srv/apptolast")

    target = expect_mapping(
        catalog["target"],
        {"state_root", "services_root"},
        "target",
    )
    state_root = validate_path(target["state_root"], "target.state_root")
    services_root = validate_path(
        target["services_root"],
        "target.services_root",
    )
    require_child(services_root, state_root, "target.services_root")
    platform_state_root = validate_path(
        platform.get("platform_state_root"),
        "config/platform.yml platform_state_root",
    )
    if state_root != platform_state_root:
        raise ValidationError(
            "target.state_root differs from config/platform.yml"
        )

    approved_values = expect_list(
        catalog["approved_services"],
        "approved_services",
    )
    if not approved_values:
        raise ValidationError("approved_services must not be empty")
    approved: dict[str, dict[str, Any]] = {}
    all_hostnames: dict[str, str] = {}
    published_ports: dict[tuple[str, int], str] = {}
    service_dataset_refs: dict[str, set[str]] = {}

    for index, service_value in enumerate(approved_values):
        context = f"approved_services[{index}]"
        service = expect_mapping(
            service_value,
            {
                "id",
                "classification",
                "migration",
                "hostnames",
                "ports",
                "images",
                "datasets",
                "source_artifacts",
            },
            context,
        )
        service_id = validate_identifier(service["id"], f"{context}.id")
        if service_id in approved:
            raise ValidationError(
                f"approved_services contains duplicate id {service_id}"
            )
        approved[service_id] = service
        if service["classification"] != "migration-scope":
            raise ValidationError(
                f"{context}.classification must be migration-scope"
            )
        if service["migration"] not in {
            "redeploy",
            "restore-state",
            "clean-install",
        }:
            raise ValidationError(f"{context}.migration is unsupported")

        hostnames = validate_unique_strings(
            service["hostnames"],
            f"{context}.hostnames",
            validate_hostname,
        )
        for hostname in hostnames:
            previous = all_hostnames.get(hostname)
            if previous is not None:
                raise ValidationError(
                    f"hostname {hostname} is shared by {previous} and "
                    f"{service_id}"
                )
            all_hostnames[hostname] = service_id

        ports = expect_list(service["ports"], f"{context}.ports")
        if not ports:
            raise ValidationError(f"{context}.ports must not be empty")
        port_names: set[str] = set()
        targets: set[tuple[str, int]] = set()
        exposures: set[str] = set()
        for port_index, port_value in enumerate(ports):
            port_context = f"{context}.ports[{port_index}]"
            published, protocol, port_target = validate_port(
                port_value,
                port_context,
            )
            port_name = port_value["name"]
            if port_name in port_names:
                raise ValidationError(
                    f"{context}.ports contains duplicate name {port_name}"
                )
            port_names.add(port_name)
            target_key = (protocol, port_target)
            if target_key in targets:
                raise ValidationError(
                    f"{context}.ports contains duplicate target {target_key}"
                )
            targets.add(target_key)
            exposures.add(port_value["exposure"])
            if published is not None:
                published_key = (protocol, published)
                previous = published_ports.get(published_key)
                if previous is not None:
                    raise ValidationError(
                        f"published port {protocol}/{published} overlaps "
                        f"between {previous} and {service_id}"
                    )
                published_ports[published_key] = service_id
        if hostnames and not exposures.intersection({"edge", "public"}):
            raise ValidationError(
                f"{context} declares hostnames without an ingress port"
            )

        images = expect_list(service["images"], f"{context}.images")
        if not images:
            raise ValidationError(f"{context}.images must not be empty")
        image_components: set[str] = set()
        for image_index, image_value in enumerate(images):
            image_context = f"{context}.images[{image_index}]"
            image = expect_mapping(
                image_value,
                {"component", "reference"},
                image_context,
                {"source_reference"},
            )
            component = validate_identifier(
                image["component"],
                f"{image_context}.component",
            )
            if component in image_components:
                raise ValidationError(
                    f"{context}.images contains duplicate component {component}"
                )
            image_components.add(component)
            validate_image(image["reference"], f"{image_context}.reference")
            if "source_reference" in image:
                validate_image(
                    image["source_reference"],
                    f"{image_context}.source_reference",
                )

        datasets = validate_unique_strings(
            service["datasets"],
            f"{context}.datasets",
            validate_identifier,
        )
        service_dataset_refs[service_id] = set(datasets)
        artifacts = validate_unique_strings(
            service["source_artifacts"],
            f"{context}.source_artifacts",
            validate_identifier,
        )
        if not artifacts:
            raise ValidationError(
                f"{context}.source_artifacts must not be empty"
            )

    approved_ids = set(approved)
    if approved_ids != EXPECTED_APPROVED:
        raise ValidationError(
            "approved service ids differ from the reviewed allowlist"
        )
    for service_id, expected_migration in EXPECTED_MIGRATIONS.items():
        if approved[service_id]["migration"] != expected_migration:
            raise ValidationError(
                f"{service_id} must use migration {expected_migration}"
            )
        actual_hostnames = frozenset(approved[service_id]["hostnames"])
        if actual_hostnames != EXPECTED_HOSTNAMES[service_id]:
            raise ValidationError(
                f"{service_id} hostnames differ from the reviewed contract"
            )

    denied_values = expect_list(
        catalog["denied_services"],
        "denied_services",
    )
    denied: dict[str, frozenset[str]] = {}
    denied_identities: set[str] = set()
    for index, denied_value in enumerate(denied_values):
        context = f"denied_services[{index}]"
        rule = expect_mapping(denied_value, {"id", "aliases"}, context)
        rule_id = validate_identifier(rule["id"], f"{context}.id")
        aliases = frozenset(
            validate_unique_strings(
                rule["aliases"],
                f"{context}.aliases",
                validate_alias,
            )
        )
        if rule_id in aliases:
            raise ValidationError(f"{context} repeats its id as an alias")
        if rule_id in denied:
            raise ValidationError(
                f"denied_services contains duplicate id {rule_id}"
            )
        identities = {rule_id, *aliases}
        repeated = denied_identities.intersection(identities)
        if repeated:
            raise ValidationError(
                "denied identities are repeated: "
                + ", ".join(sorted(repeated))
            )
        denied_identities.update(identities)
        denied[rule_id] = aliases

    overlap = approved_ids.intersection(denied_identities)
    if overlap:
        raise ValidationError(
            "approved and denied services overlap: "
            + ", ".join(sorted(overlap))
        )
    if denied != EXPECTED_DENIED:
        raise ValidationError(
            "denied service rules differ from the reviewed denylist"
        )

    dataset_values = expect_list(catalog["datasets"], "datasets")
    datasets: dict[str, dict[str, Any]] = {}
    dataset_target_paths: list[tuple[str, PurePosixPath]] = []
    dataset_source_paths: list[tuple[str, PurePosixPath]] = []
    for index, dataset_value in enumerate(dataset_values):
        context = f"datasets[{index}]"
        dataset = expect_mapping(
            dataset_value,
            {
                "id",
                "owner",
                "consumers",
                "read_only_consumers",
                "migration",
                "source_path",
                "target_path",
            },
            context,
        )
        dataset_id = validate_identifier(dataset["id"], f"{context}.id")
        if dataset_id in datasets:
            raise ValidationError(f"datasets contains duplicate id {dataset_id}")
        datasets[dataset_id] = dataset
        owner = validate_identifier(dataset["owner"], f"{context}.owner")
        if owner not in approved_ids:
            raise ValidationError(f"{context}.owner is not approved")
        consumers = set(
            validate_unique_strings(
                dataset["consumers"],
                f"{context}.consumers",
                validate_identifier,
            )
        )
        if not consumers or not consumers.issubset(approved_ids):
            raise ValidationError(
                f"{context}.consumers must be approved services"
            )
        if owner not in consumers:
            raise ValidationError(f"{context}.owner must be a consumer")
        read_only = set(
            validate_unique_strings(
                dataset["read_only_consumers"],
                f"{context}.read_only_consumers",
                validate_identifier,
            )
        )
        if not read_only.issubset(consumers):
            raise ValidationError(
                f"{context}.read_only_consumers must be consumers"
            )
        migration = dataset["migration"]
        if migration not in {"restore", "initialize-empty"}:
            raise ValidationError(f"{context}.migration is unsupported")
        source_path_value = dataset["source_path"]
        if migration == "restore":
            source_path = validate_path(
                source_path_value,
                f"{context}.source_path",
            )
            require_child(source_path, source_root, f"{context}.source_path")
            dataset_source_paths.append((f"{context}.source_path", source_path))
        elif source_path_value is not None:
            raise ValidationError(
                f"{context}.source_path must be null when initialized empty"
            )
        target_path = validate_path(
            dataset["target_path"],
            f"{context}.target_path",
        )
        require_child(target_path, state_root, f"{context}.target_path")
        if owner != "traefik-edge":
            require_child(
                target_path,
                services_root,
                f"{context}.target_path",
            )
        dataset_target_paths.append((f"{context}.target_path", target_path))

    validate_no_path_overlap(dataset_source_paths)
    validate_no_path_overlap(dataset_target_paths)
    referenced_dataset_ids = set().union(*service_dataset_refs.values())
    if set(datasets) != referenced_dataset_ids:
        raise ValidationError(
            "declared datasets and service dataset references differ"
        )
    for dataset_id, dataset in datasets.items():
        actual_consumers = {
            service_id
            for service_id, references in service_dataset_refs.items()
            if dataset_id in references
        }
        if set(dataset["consumers"]) != actual_consumers:
            raise ValidationError(
                f"dataset {dataset_id} consumers differ from service references"
            )

    openclaw = approved["openclaw-clean"]
    if set(openclaw["datasets"]) != {"openclaw-clean-home"}:
        raise ValidationError(
            "OpenClaw clean must use only openclaw-clean-home"
        )
    openclaw_dataset = datasets["openclaw-clean-home"]
    if (
        openclaw_dataset["migration"] != "initialize-empty"
        or openclaw_dataset["source_path"] is not None
    ):
        raise ValidationError(
            "OpenClaw clean must not import any legacy state"
        )

    internal_platform = expect_mapping(
        catalog["internal_platform"],
        {"observability"},
        "internal_platform",
    )
    observability = expect_mapping(
        internal_platform["observability"],
        {
            "classification",
            "legacy_migration",
            "hostnames",
            "components",
            "ports",
            "data_paths",
        },
        "internal_platform.observability",
    )
    if observability["classification"] != "internal-platform":
        raise ValidationError(
            "observability.classification must be internal-platform"
        )
    if observability["legacy_migration"] is not False:
        raise ValidationError(
            "observability.legacy_migration must be false"
        )
    if observability["hostnames"] != []:
        raise ValidationError("observability must not declare public hostnames")

    component_values = expect_list(
        observability["components"],
        "internal_platform.observability.components",
    )
    component_ids: set[str] = set()
    for index, component_value in enumerate(component_values):
        context = f"internal_platform.observability.components[{index}]"
        component = expect_mapping(
            component_value,
            {"id", "image"},
            context,
        )
        component_id = validate_identifier(
            component["id"],
            f"{context}.id",
        )
        if component_id in component_ids:
            raise ValidationError(
                f"observability contains duplicate component {component_id}"
            )
        component_ids.add(component_id)
        validate_image(component["image"], f"{context}.image")
    if component_ids != EXPECTED_OBSERVABILITY_COMPONENTS:
        raise ValidationError(
            "observability components differ from the internal platform set"
        )
    component_scope_overlap = component_ids.intersection(
        approved_ids | denied_identities
    )
    if component_scope_overlap:
        raise ValidationError(
            "internal components overlap migration scope: "
            + ", ".join(sorted(component_scope_overlap))
        )

    observability_port_values = expect_list(
        observability["ports"],
        "internal_platform.observability.ports",
    )
    component_ports: set[tuple[str, str, int]] = set()
    port_components: set[str] = set()
    for index, port_value in enumerate(observability_port_values):
        context = f"internal_platform.observability.ports[{index}]"
        published, protocol, target_port = validate_port(
            port_value,
            context,
            component_required=True,
        )
        component = validate_identifier(
            port_value["component"],
            f"{context}.component",
        )
        if component not in component_ids:
            raise ValidationError(f"{context}.component is not declared")
        port_key = (component, protocol, target_port)
        if port_key in component_ports:
            raise ValidationError(
                f"observability contains duplicate component port {port_key}"
            )
        component_ports.add(port_key)
        port_components.add(component)
        if published is not None:
            published_key = (protocol, published)
            previous = published_ports.get(published_key)
            if previous is not None:
                raise ValidationError(
                    f"published port {protocol}/{published} overlaps "
                    f"between {previous} and observability/{component}"
                )
            published_ports[published_key] = f"observability/{component}"
    if port_components != component_ids:
        raise ValidationError(
            "every observability component must declare its known port"
        )
    if any(
        port["published"] is not None or port["exposure"] != "internal"
        for port in observability_port_values
    ):
        raise ValidationError(
            "observability ports must remain internal and unpublished"
        )

    observability_data_values = expect_list(
        observability["data_paths"],
        "internal_platform.observability.data_paths",
    )
    observability_root = state_root / "observability"
    observability_data_components: set[str] = set()
    observability_paths: list[tuple[str, PurePosixPath]] = []
    for index, data_value in enumerate(observability_data_values):
        context = f"internal_platform.observability.data_paths[{index}]"
        data_path = expect_mapping(
            data_value,
            {"component", "path", "initialization"},
            context,
        )
        component = validate_identifier(
            data_path["component"],
            f"{context}.component",
        )
        if component not in component_ids:
            raise ValidationError(f"{context}.component is not declared")
        if component in observability_data_components:
            raise ValidationError(
                f"observability has duplicate data path for {component}"
            )
        observability_data_components.add(component)
        if data_path["initialization"] != "empty":
            raise ValidationError(
                f"{context}.initialization must be empty"
            )
        path = validate_path(data_path["path"], f"{context}.path")
        require_child(path, observability_root, f"{context}.path")
        observability_paths.append((f"{context}.path", path))
    if observability_data_components != EXPECTED_OBSERVABILITY_DATA:
        raise ValidationError(
            "observability data paths differ from the reviewed internal set"
        )
    validate_no_path_overlap(observability_paths)
    validate_no_path_overlap(dataset_target_paths + observability_paths)

    traefik = approved["traefik-edge"]
    traefik_images = {
        image["component"]: image for image in traefik["images"]
    }
    if traefik_images["proxy"]["reference"] != group_vars.get(
        "edge_traefik_image"
    ):
        raise ValidationError(
            "Traefik target image differs from Ansible group vars"
        )
    if set(traefik["hostnames"]) != {
        platform.get("edge_traefik_hostname")
    }:
        raise ValidationError(
            "Traefik target hostname differs from config/platform.yml"
        )
    catalog_public_ports = sorted(
        port["published"]
        for service in approved.values()
        for port in service["ports"]
        if port["exposure"] == "public"
    )
    if catalog_public_ports != platform.get("platform_public_tcp_ports"):
        raise ValidationError(
            "catalog public ports differ from config/platform.yml"
        )

    return {
        "approved": len(approved),
        "denied_rules": len(denied),
        "denied_identities": len(denied_identities),
        "datasets": len(datasets),
        "observability_components": len(component_ids),
    }


def run_self_tests(
    valid_catalog: dict[str, Any],
    platform: dict[str, Any],
    group_vars: dict[str, Any],
) -> int:
    cases: list[
        tuple[str, str, Callable[[dict[str, Any]], None]]
    ] = []

    def unexpected_key(candidate: dict[str, Any]) -> None:
        candidate["unexpected"] = True

    cases.append(("strict schema", "unexpected", unexpected_key))

    def duplicate_service(candidate: dict[str, Any]) -> None:
        candidate["approved_services"].append(
            copy.deepcopy(candidate["approved_services"][0])
        )

    cases.append(("service uniqueness", "duplicate", duplicate_service))

    def mutable_image(candidate: dict[str, Any]) -> None:
        candidate["approved_services"][0]["images"][0][
            "reference"
        ] = "docker.io/apptolast/kropia-web:latest"

    cases.append(("digest pinning", "sha256 digest", mutable_image))

    def relative_path(candidate: dict[str, Any]) -> None:
        candidate["datasets"][0]["target_path"] = "relative/data"

    cases.append(("absolute paths", "absolute POSIX path", relative_path))

    def scope_overlap(candidate: dict[str, Any]) -> None:
        candidate["denied_services"].append(
            {"id": "kropia", "aliases": []}
        )

    cases.append(("allow/deny overlap", "overlap", scope_overlap))

    def openclaw_legacy(candidate: dict[str, Any]) -> None:
        for dataset in candidate["datasets"]:
            if dataset["id"] == "openclaw-clean-home":
                dataset["migration"] = "restore"
                dataset["source_path"] = "/srv/apptolast/openclaw"

    cases.append(("clean OpenClaw", "OpenClaw clean", openclaw_legacy))

    def exposed_observability(candidate: dict[str, Any]) -> None:
        port = candidate["internal_platform"]["observability"]["ports"][0]
        port["published"] = 9090
        port["exposure"] = "loopback"

    cases.append(
        (
            "unpublished observability",
            "exposure is unsupported",
            exposed_observability,
        )
    )

    passed = 0
    for name, expected_fragment, mutate in cases:
        candidate = copy.deepcopy(valid_catalog)
        mutate(candidate)
        try:
            validate_catalog(candidate, platform, group_vars)
        except ValidationError as error:
            if expected_fragment not in str(error):
                raise ValidationError(
                    f"self-test {name!r} failed with an unexpected error: "
                    f"{error}"
                ) from error
            passed += 1
        else:
            raise ValidationError(
                f"self-test {name!r} accepted an invalid catalog"
            )

    try:
        load_yaml_text(
            "schema_version: 1\nschema_version: 2\n",
            "duplicate-key self-test",
        )
    except ValidationError as error:
        if "duplicate key" not in str(error):
            raise ValidationError(
                "duplicate-key self-test returned an unexpected error"
            ) from error
        passed += 1
    else:
        raise ValidationError("duplicate-key self-test accepted duplicate YAML")

    return passed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the reviewed service migration catalog."
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=DEFAULT_CONTRACT,
        help=f"catalog to validate (default: {DEFAULT_CONTRACT})",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="also exercise direct negative validator cases",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        platform, group_vars = validate_external_contracts()
        catalog = load_yaml(args.contract)
        counts = validate_catalog(catalog, platform, group_vars)
        if args.self_test:
            passed = run_self_tests(catalog, platform, group_vars)
            print(f"{passed} direct validator self-tests passed.")
    except ValidationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(
        "Service catalog "
        f"{catalog['catalog_version']} validated: "
        f"{counts['approved']} approved, "
        f"{counts['denied_rules']} deny rules, "
        f"{counts['datasets']} datasets and "
        f"{counts['observability_components']} internal observability "
        "components."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
