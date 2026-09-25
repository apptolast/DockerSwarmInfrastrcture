#!/usr/bin/env python3
"""Validate additive, mutually exclusive capacity plans without altering v1."""

from __future__ import annotations

import argparse
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import re
import sys

import jinja2


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "legacy_capacity", ROOT / "scripts/validate-capacity.py"
)
capacity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capacity)

# The independent application stacks: rendered from their own
# config/<name>.yml plus stacks/<name>/stack.yml.j2, budgeted in
# config/capacity-profiles.yml rather than in the v1 contract.
APP_STACKS = ("organizationweb", "racinggame")
APP_SERVICES = {
    "organizationweb": ["backend", "postgres", "rabbitmq", "web"],
    "racinggame": ["web"],
}
# A plan is always edge + workloads + applications + autoupdater.
PLAN_HEAD = ["edge", "workloads"]
PLAN_TAIL = ["autoupdater"]
MIB = 1024 * 1024
NANO_CPUS_PER_MILLICORE = 1_000_000
# Docker's own rule for a container name: an alphanumeric first character,
# then at least one more alphanumeric, `_`, `.` or `-`.
CONTAINER_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+")
# The --live record of one declared host container: the Ansible loop result
# of `docker container inspect` for that name, reduced to these keys.
HOST_CONTAINER_RECORD_KEYS = frozenset({"item", "rc", "stdout", "stderr"})
# Docker's container states. Only these hold no running process.
STOPPED_CONTAINER_STATES = frozenset({"created", "exited", "dead"})
CONTAINER_STATES = STOPPED_CONTAINER_STATES | {
    "running",
    "paused",
    "restarting",
    "removing",
}


def validate_external_stacks(document):
    """Swarm stacks run here but defined elsewhere, counted in every plan."""
    if not isinstance(document, dict):
        raise capacity.CapacityError("external_stacks must be a mapping")
    external = {}
    for stack, services in document.items():
        if (
            not isinstance(stack, str)
            or not capacity.IDENTIFIER_RE.fullmatch(stack)
            or stack in capacity.STACK_IDS
            or stack in APP_STACKS
        ):
            raise capacity.CapacityError(f"external stack name is invalid: {stack!r}")
        if not isinstance(services, dict) or not services:
            raise capacity.CapacityError(f"external stack {stack} has no services")
        external[stack] = {}
        for name, service in services.items():
            context = f"external_stacks.{stack}.{name}"
            if not isinstance(name, str) or not capacity.IDENTIFIER_RE.fullmatch(name):
                raise capacity.CapacityError(f"{context} is not a service name")
            declared = capacity.expect_mapping(
                service, {"replicas", "reservations", "limits"}, context
            )
            replicas = capacity.expect_int(
                declared["replicas"], f"{context}.replicas", minimum=1
            )
            resources = capacity.validate_resource_totals(
                {key: declared[key] for key in ("reservations", "limits")}, context
            )
            for resource_name in ("cpu_millicores", "memory_mib"):
                # Docker reads a zero limit as "unlimited", which no budget
                # can count; every external service must be bounded.
                if resources["limits"][resource_name] < 1:
                    raise capacity.CapacityError(
                        f"{context} must declare a positive {resource_name} limit"
                    )
                if (
                    resources["reservations"][resource_name]
                    > resources["limits"][resource_name]
                ):
                    raise capacity.CapacityError(
                        f"{context} reserves more {resource_name} than its limit"
                    )
            external[stack][name] = {"replicas": replicas, "resources": resources}
    return external


def external_totals(profiles):
    total = capacity.empty_resources()
    for services in profiles["external_stacks"].values():
        for service in services.values():
            capacity.add_resources(
                total,
                capacity.scaled_resources(service["resources"], service["replicas"]),
            )
    return total


def validate_host_containers(document):
    """Plain Docker containers outside Swarm, grouped by what runs together."""
    if not isinstance(document, dict):
        raise capacity.CapacityError("host_containers must be a mapping")
    groups = {}
    seen = set()
    for group, containers in document.items():
        if not isinstance(group, str) or not capacity.IDENTIFIER_RE.fullmatch(group):
            raise capacity.CapacityError(
                f"host container group name is invalid: {group!r}"
            )
        if not isinstance(containers, dict) or not containers:
            raise capacity.CapacityError(
                f"host container group {group} has no containers"
            )
        groups[group] = {}
        for name, container in containers.items():
            context = f"host_containers.{group}.{name}"
            if not isinstance(name, str) or not CONTAINER_NAME_RE.fullmatch(name):
                raise capacity.CapacityError(f"{context} is not a container name")
            # Docker names are unique per host, so one name is one budget.
            if name in seen:
                raise capacity.CapacityError(
                    f"host container {name} is declared more than once"
                )
            seen.add(name)
            declared = capacity.expect_mapping(
                container, {"reservations", "limits", "pids_limit"}, context
            )
            resources = capacity.validate_resource_totals(
                {key: declared[key] for key in ("reservations", "limits")}, context
            )
            for resource_name in ("cpu_millicores", "memory_mib"):
                # Docker reads a zero limit as "unlimited", which no budget
                # can count; every host container must be bounded.
                if resources["limits"][resource_name] < 1:
                    raise capacity.CapacityError(
                        f"{context} must declare a positive {resource_name} limit"
                    )
                if (
                    resources["reservations"][resource_name]
                    > resources["limits"][resource_name]
                ):
                    raise capacity.CapacityError(
                        f"{context} reserves more {resource_name} than its limit"
                    )
            pids_limit = capacity.expect_int(
                declared["pids_limit"], f"{context}.pids_limit", minimum=1
            )
            groups[group][name] = {"resources": resources, "pids_limit": pids_limit}
    return groups


def check_host_container_ratios(base, profiles):
    """Hold host containers to the per-service memory ratio of the policy."""
    maximum = base["policy"]["service_memory_limit_to_reservation_ratio"]
    for group, containers in profiles["host_containers"].items():
        for name, container in containers.items():
            memory = {
                resource_class: container["resources"][resource_class]["memory_mib"]
                for resource_class in ("reservations", "limits")
            }
            # Multiplied rather than divided: a zero reservation fails too.
            if Decimal(memory["limits"]) > Decimal(memory["reservations"]) * maximum:
                raise capacity.CapacityError(
                    f"host_containers.{group}.{name} memory limit/reservation "
                    f"ratio exceeds {maximum}"
                )


def host_container_totals(profiles, groups):
    total = capacity.empty_resources()
    for group in groups:
        for container in profiles["host_containers"][group].values():
            capacity.add_resources(total, container["resources"])
    return total


def host_container_names(profiles):
    """Every declared container name, whichever plan runs its group."""
    return sorted(
        name
        for containers in profiles["host_containers"].values()
        for name in containers
    )


def live_replicas(service):
    """Desired replicas of one inspected replicated live service."""
    mode = service.get("mode")
    replicated = mode.get("Replicated") if isinstance(mode, dict) else None
    if (
        not isinstance(mode, dict)
        or set(mode) != {"Replicated"}
        or not isinstance(replicated, dict)
    ):
        raise capacity.CapacityError("live service is not replicated")
    replicas = replicated.get("Replicas")
    if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 0:
        raise capacity.CapacityError("live replicas are invalid")
    return replicas


def parked_workloads(platform_document):
    """The reviewed parked list of config/platform.yml, validated."""
    parked = (
        platform_document.get("platform_parked_workloads")
        if isinstance(platform_document, dict)
        else None
    )
    parkable = {name for stack, name in capacity.PARKABLE_SERVICES}
    if (
        not isinstance(parked, list)
        or any(not isinstance(name, str) for name in parked)
        or parked != sorted(set(parked))
        or not set(parked) <= parkable
    ):
        raise capacity.CapacityError(
            "platform_parked_workloads is outside the reviewed parkable set"
        )
    return frozenset(parked)


def live_plan(service):
    """Replicas and per-task resources of one inspected live service."""
    replicas = live_replicas(service)
    # Docker prints `null` for an unset block; any other non-mapping is invalid.
    resources = service.get("resources")
    if resources is None:
        resources = {}
    if not isinstance(resources, dict) or not set(resources) <= {
        "Limits",
        "Reservations",
    }:
        raise capacity.CapacityError("external live resources are invalid")
    plan = capacity.empty_resources()
    for resource_class, key in (("limits", "Limits"), ("reservations", "Reservations")):
        values = resources.get(key)
        if values is None:
            values = {}
        if not isinstance(values, dict) or not set(values) <= {
            "NanoCPUs",
            "MemoryBytes",
        }:
            raise capacity.CapacityError("external live resources are invalid")
        for field, unit, name in (
            ("NanoCPUs", NANO_CPUS_PER_MILLICORE, "cpu_millicores"),
            ("MemoryBytes", MIB, "memory_mib"),
        ):
            value = values.get(field, 0)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value % unit
            ):
                raise capacity.CapacityError("external live resources are invalid")
            plan[resource_class][name] = value // unit
    return replicas, plan


def live_inventory(document):
    """Split the --live standard input into Swarm services and host containers.

    The preflight sends {"services": [...], "host_containers": [...]}, the
    only accepted shape: an input without the host container reads cannot
    prove the declared containers are in their required state.
    """
    if not isinstance(document, dict) or set(document) != {
        "services",
        "host_containers",
    }:
        raise capacity.CapacityError(
            "live inventory must hold exactly services and host_containers"
        )
    services = document["services"]
    containers = document["host_containers"]
    if not isinstance(containers, list):
        raise capacity.CapacityError("live host container inventory must be a list")
    if not isinstance(services, list) or not all(
        isinstance(item, dict) for item in services
    ):
        raise capacity.CapacityError("live inventory must be a service list")
    return services, containers


# The HostConfig fields the preflight reads with `docker container inspect`.
# In the Go template the CPU limit is `.HostConfig.NanoCPUs`; its JSON key,
# and the one compared here, is NanoCpus.
HOST_CONFIG_FIELDS = ("Memory", "MemoryReservation", "NanoCpus", "PidsLimit")


def inspected_host_container(name, record):
    """Status and resources of one declared container, or None when absent."""
    rc = record["rc"]
    stdout = record["stdout"]
    stderr = record["stderr"]
    if (
        isinstance(rc, bool)
        or not isinstance(rc, int)
        or not isinstance(stdout, str)
        or not isinstance(stderr, str)
    ):
        raise capacity.CapacityError(f"live host container {name} is malformed")
    if rc != 0:
        # Only Docker naming this exact container as missing proves absence;
        # any other failure leaves its state unknown.
        if stdout.strip() or not re.fullmatch(
            rf"(?:.*: )?No such container: {re.escape(name)}", stderr.strip()
        ):
            raise capacity.CapacityError(
                f"cannot inspect live host container {name}: "
                f"{stderr.strip() or f'exit code {rc}'}"
            )
        return None
    try:
        inspected = json.loads(stdout)
    except ValueError as error:
        raise capacity.CapacityError(
            f"live host container {name} is malformed"
        ) from error
    if not isinstance(inspected, dict) or set(inspected) != {
        "name",
        "status",
        "host_config",
    }:
        raise capacity.CapacityError(f"live host container {name} is malformed")
    # `docker container inspect` also resolves an ID prefix: the object read
    # must carry this exact name.
    if inspected["name"] != f"/{name}":
        raise capacity.CapacityError(f"inspected container is not {name}")
    status = inspected["status"]
    host_config = inspected["host_config"]
    if (
        not isinstance(status, str)
        or status not in CONTAINER_STATES
        or not isinstance(host_config, dict)
        or set(host_config) != set(HOST_CONFIG_FIELDS)
    ):
        raise capacity.CapacityError(f"live host container {name} is malformed")
    resources = {}
    for field in ("Memory", "MemoryReservation", "NanoCpus"):
        value = host_config.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise capacity.CapacityError(
                f"live host container {name} has an invalid {field}"
            )
        resources[field] = value
    # Docker prints `null` for an unset PidsLimit and 0 or -1 for an unlimited
    # one; none of them can match a declared limit, which is at least 1.
    pids_limit = host_config.get("PidsLimit")
    if pids_limit is not None and (
        isinstance(pids_limit, bool) or not isinstance(pids_limit, int)
    ):
        raise capacity.CapacityError(
            f"live host container {name} has an invalid PidsLimit"
        )
    resources["PidsLimit"] = pids_limit
    return status, resources


def validate_live_host_containers(profiles, live_containers):
    """Active host containers run as declared; every other one is stopped."""
    declared = {
        name: (group, container)
        for group, containers in profiles["host_containers"].items()
        for name, container in containers.items()
    }
    if not isinstance(live_containers, list):
        raise capacity.CapacityError("live host container inventory must be a list")
    records = {}
    for record in live_containers:
        if not isinstance(record, dict) or set(record) != HOST_CONTAINER_RECORD_KEYS:
            raise capacity.CapacityError("live host container record is malformed")
        name = record["item"]
        if not isinstance(name, str) or name not in declared or name in records:
            raise capacity.CapacityError(
                f"live host container record is not one declared container: {name!r}"
            )
        records[name] = record
    missing = sorted(set(declared) - set(records))
    if missing:
        raise capacity.CapacityError(
            "live host container data is missing: " + ", ".join(missing)
        )
    active_groups = set(profiles["profiles"][profiles["active"]]["host_containers"])
    for name, (group, container) in sorted(declared.items()):
        state = inspected_host_container(name, records[name])
        if group not in active_groups:
            if state is not None and state[0] not in STOPPED_CONTAINER_STATES:
                raise capacity.CapacityError(
                    f"host container outside the active profile is {state[0]}: {name}"
                )
            continue
        if state is None:
            raise capacity.CapacityError(f"active host container is absent: {name}")
        status, resources = state
        if status != "running":
            raise capacity.CapacityError(
                f"active host container is {status}, not running: {name}"
            )
        expected = {
            "Memory": container["resources"]["limits"]["memory_mib"] * MIB,
            "MemoryReservation": (
                container["resources"]["reservations"]["memory_mib"] * MIB
            ),
            "NanoCpus": (
                container["resources"]["limits"]["cpu_millicores"]
                * NANO_CPUS_PER_MILLICORE
            ),
            "PidsLimit": container["pids_limit"],
        }
        for field, value in expected.items():
            if resources[field] != value:
                raise capacity.CapacityError(
                    f"live host container differs from its declaration: "
                    f"{name} {field}"
                )


def validate_profile_contract(document):
    root = capacity.expect_mapping(document, {"capacity_profiles"}, "profiles")
    profiles = capacity.expect_mapping(
        root["capacity_profiles"],
        {
            "schema_version",
            "active",
            "profiles",
            "external_stacks",
            "host_containers",
            *APP_STACKS,
        },
        "profiles",
    )
    if type(profiles["schema_version"]) is not int or profiles["schema_version"] != 1:
        raise capacity.CapacityError("unsupported profile schema")
    if profiles["active"] not in ("observability", "organizationweb"):
        raise capacity.CapacityError("unknown active profile")
    plans = capacity.expect_mapping(
        profiles["profiles"], {"observability", "organizationweb"}, "profiles.plans"
    )
    host_containers = validate_host_containers(profiles["host_containers"])
    for name, plan in plans.items():
        capacity.expect_mapping(
            plan, {"stacks", "host_containers", "aggregate"}, f"profile.{name}"
        )
        groups = plan["host_containers"]
        if (
            not isinstance(groups, list)
            or any(not isinstance(group, str) for group in groups)
            or len(set(groups)) != len(groups)
        ):
            raise capacity.CapacityError(
                f"profile {name} host_containers must list distinct group names"
            )
        for group in groups:
            if group not in host_containers:
                raise capacity.CapacityError(
                    f"profile {name} runs an undeclared host container group: {group}"
                )
        stacks = plan["stacks"]
        if not isinstance(stacks, list):
            raise capacity.CapacityError("profile stacks must be a list")
        middle = stacks[len(PLAN_HEAD):-len(PLAN_TAIL)]
        if (
            stacks[:len(PLAN_HEAD)] != PLAN_HEAD
            or stacks[-len(PLAN_TAIL):] != PLAN_TAIL
            or len(set(stacks)) != len(stacks)
            or name not in middle
            or any(
                stack not in APP_STACKS
                and stack not in capacity.STACK_IDS
                for stack in middle
            )
        ):
            raise capacity.CapacityError(
                "profile must include edge, workloads, its own stack and autoupdater"
            )
        capacity.validate_resource_totals(plan["aggregate"], f"profile.{name}.aggregate")
    for app_name in APP_STACKS:
        app = capacity.expect_mapping(
            profiles[app_name],
            {"expected_services", "aggregate"},
            f"profile.{app_name}",
        )
        if app["expected_services"] != APP_SERVICES[app_name]:
            raise capacity.CapacityError(
                "profile application services are not the reviewed set"
            )
        capacity.validate_resource_totals(
            app["aggregate"], f"profile.{app_name}.aggregate"
        )
    # A validated copy: the caller's document is never rewritten.
    return {
        **profiles,
        "external_stacks": validate_external_stacks(profiles["external_stacks"]),
        "host_containers": host_containers,
    }


def validate_profiles(base_document, profile_document, stacks):
    base = capacity.validate_contract(base_document)
    legacy = capacity.validate_stacks(
        base, {name: stacks[name] for name in capacity.STACK_IDS}
    )
    profiles = validate_profile_contract(profile_document)
    check_host_container_ratios(base, profiles)
    totals = dict(legacy)
    for app_name in APP_STACKS:
        app_contract = profiles[app_name]
        services = stacks[app_name]["services"]
        if set(services) != set(app_contract["expected_services"]):
            raise capacity.CapacityError(
                "application services differ from reviewed set"
            )
        app_total = capacity.empty_resources()
        for name in app_contract["expected_services"]:
            plan = capacity.service_resources(
                app_name,
                name,
                services[name],
                set(),
                base["topology"]["eligible_nodes"],
                base["policy"]["service_memory_limit_to_reservation_ratio"],
            )
            capacity.add_resources(app_total, plan)
        if app_total != app_contract["aggregate"]:
            raise capacity.CapacityError(
                "application totals differ from reviewed budget"
            )
        totals[app_name] = app_total
    result = {}
    for name, profile in profiles["profiles"].items():
        aggregate = external_totals(profiles)
        for stack in profile["stacks"]:
            capacity.add_resources(aggregate, totals[stack])
        capacity.add_resources(
            aggregate, host_container_totals(profiles, profile["host_containers"])
        )
        if aggregate != profile["aggregate"]:
            raise capacity.CapacityError("profile totals differ from reviewed budget")
        capacity.validate_budget(base, aggregate)
        result[name] = aggregate
    return result


# The playbooks that converge a parked service to 0/0 may start while it still
# runs; every other one requires it already parked, or it runs over budget.
PARKING_PLAYBOOKS = frozenset({"workloads", "site"})


def validate_live(
    base_document,
    profile_document,
    requested_stack,
    live_services,
    *,
    parked,
    live_containers=None,
):
    base = capacity.validate_contract(base_document)
    converging = requested_stack in PARKING_PLAYBOOKS
    profiles = validate_profile_contract(profile_document)
    check_host_container_ratios(base, profiles)
    for name, profile in profiles["profiles"].items():
        total = external_totals(profiles)
        for stack in profile["stacks"]:
            capacity.add_resources(
                total,
                profiles[stack]["aggregate"]
                if stack in APP_STACKS
                else base["reviewed_totals"][stack],
            )
        capacity.add_resources(
            total, host_container_totals(profiles, profile["host_containers"])
        )
        if total != profile["aggregate"]:
            raise capacity.CapacityError(f"profile {name} has inconsistent arithmetic")
        capacity.validate_budget(base, total)
    allowed_stacks = profiles["profiles"][profiles["active"]]["stacks"]
    if requested_stack == "site":
        requested_stack = "observability"
    if requested_stack not in allowed_stacks:
        raise capacity.CapacityError("requested stack is outside the active profile")
    allowed_names = {
        (stack, f"{stack}_{service}")
        for stack in allowed_stacks
        for service in (
            profiles[stack]["expected_services"]
            if stack in APP_STACKS
            else base["stacks"][stack]["expected_services"]
        )
    }
    external_names = {
        (stack, f"{stack}_{service}"): declared
        for stack, services in profiles["external_stacks"].items()
        for service, declared in services.items()
    }
    seen_external = set()
    for service in live_services:
        identity = (service.get("stack"), service.get("name"))
        if identity in external_names:
            declared = external_names[identity]
            if live_plan(service) != (declared["replicas"], declared["resources"]):
                raise capacity.CapacityError(
                    f"external live service differs from its declaration: {identity[1]}"
                )
            seen_external.add(identity)
        elif identity not in allowed_names:
            raise capacity.CapacityError(
                "live service is outside the reviewed active profile"
            )
        elif (
            identity[0] == "workloads"
            and identity[1].removeprefix("workloads_") in parked
            and not converging
            and live_replicas(service) != 0
        ):
            raise capacity.CapacityError(
                f"parked live service runs over the budget: {identity[1]}"
            )
    if seen_external != set(external_names):
        raise capacity.CapacityError("a declared external service is not live")
    validate_live_host_containers(profiles, live_containers)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-contract", type=Path, default=ROOT / "config/capacity.yml")
    parser.add_argument("--profile-contract", type=Path, default=ROOT / "config/capacity-profiles.yml")
    parser.add_argument(
        "--platform-contract", type=Path, default=ROOT / "config/platform.yml"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "read the live inventory from stdin: "
            '{"services": [...], "host_containers": [...]}, '
            "or a bare service list while no host container is declared"
        ),
    )
    parser.add_argument(
        "--host-container-names",
        action="store_true",
        help="print every declared host container name as a JSON list",
    )
    parser.add_argument(
        "--requested-stack",
        choices=(
            "edge",
            "workloads",
            "observability",
            "organizationweb",
            "racinggame",
            "autoupdater",
            "site",
        ),
    )
    args = parser.parse_args(argv)
    if args.live and args.host_container_names:
        parser.error("--host-container-names cannot be combined with --live")
    try:
        base = capacity.load_yaml(args.base_contract)
        profiles = capacity.load_yaml(args.profile_contract)
        if args.host_container_names:
            # The preflight inspects these names before the live check, so
            # nothing from an invalid contract reaches Docker.
            validated = validate_profile_contract(profiles)
            check_host_container_ratios(capacity.validate_contract(base), validated)
            names = host_container_names(validated)
        elif args.live:
            if args.requested_stack is None:
                raise capacity.CapacityError("live validation requires the requested stack")
            live_services, live_containers = live_inventory(json.load(sys.stdin))
            validate_live(
                base,
                profiles,
                args.requested_stack,
                live_services,
                parked=parked_workloads(capacity.load_yaml(args.platform_contract)),
                live_containers=live_containers,
            )
        else:
            import yaml

            channel_spec = importlib.util.spec_from_file_location(
                "validate_image_channels",
                ROOT / "scripts/validate-image-channels.py",
            )
            channels = importlib.util.module_from_spec(channel_spec)
            channel_spec.loader.exec_module(channels)
            try:
                image_channels_map = channels.load_channel_map(ROOT)["services"]
            except channels.ChannelError as error:
                raise capacity.CapacityError(f"image channel map: {error}") from error
            stacks = {
                name: capacity.load_yaml(path)
                for name, path in capacity.DEFAULT_STACKS.items()
                if name != "autoupdater"
            }
            for app_name in APP_STACKS:
                variables = capacity.load_yaml(
                    ROOT / f"config/{app_name}.yml"
                )
                template = jinja2.Environment(
                    loader=jinja2.FileSystemLoader(
                        ROOT / f"stacks/{app_name}"
                    ),
                    undefined=jinja2.StrictUndefined,
                ).get_template("stack.yml.j2")
                stacks[app_name] = yaml.safe_load(
                    template.render(
                        **variables,
                        image_channels_map=image_channels_map,
                    )
                )
            autoupdater_spec = importlib.util.spec_from_file_location(
                "validate_autoupdater",
                ROOT / "scripts/validate-autoupdater.py",
            )
            autoupdater = importlib.util.module_from_spec(autoupdater_spec)
            autoupdater_spec.loader.exec_module(autoupdater)
            try:
                _rendered, stacks["autoupdater"] = autoupdater.render_validated()
            except autoupdater.AutoupdaterError as error:
                raise capacity.CapacityError(f"autoupdater: {error}") from error
            validate_profiles(base, profiles, stacks)
    except (capacity.CapacityError, ValueError, KeyError, TypeError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if args.host_container_names:
        print(json.dumps(names))
        return 0
    print("Explicit capacity profiles and requested inventory passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
