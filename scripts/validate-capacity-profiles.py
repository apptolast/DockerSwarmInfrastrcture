#!/usr/bin/env python3
"""Validate additive, mutually exclusive capacity plans without altering v1."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
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


def live_plan(service):
    """Replicas and per-task resources of one inspected live service."""
    mode = service.get("mode")
    replicated = mode.get("Replicated") if isinstance(mode, dict) else None
    if (
        not isinstance(mode, dict)
        or set(mode) != {"Replicated"}
        or not isinstance(replicated, dict)
    ):
        raise capacity.CapacityError("external live service is not replicated")
    replicas = replicated.get("Replicas")
    if isinstance(replicas, bool) or not isinstance(replicas, int):
        raise capacity.CapacityError("external live replicas are invalid")
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
        values = resources.get(key) or {}
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


def validate_profile_contract(document):
    root = capacity.expect_mapping(document, {"capacity_profiles"}, "profiles")
    profiles = capacity.expect_mapping(
        root["capacity_profiles"],
        {"schema_version", "active", "profiles", "external_stacks", *APP_STACKS},
        "profiles",
    )
    if type(profiles["schema_version"]) is not int or profiles["schema_version"] != 1:
        raise capacity.CapacityError("unsupported profile schema")
    if profiles["active"] not in ("observability", "organizationweb"):
        raise capacity.CapacityError("unknown active profile")
    plans = capacity.expect_mapping(
        profiles["profiles"], {"observability", "organizationweb"}, "profiles.plans"
    )
    for name, plan in plans.items():
        capacity.expect_mapping(plan, {"stacks", "aggregate"}, f"profile.{name}")
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
    }


def validate_profiles(base_document, profile_document, stacks):
    base = capacity.validate_contract(base_document)
    legacy = capacity.validate_stacks(
        base, {name: stacks[name] for name in capacity.STACK_IDS}
    )
    profiles = validate_profile_contract(profile_document)
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
        if aggregate != profile["aggregate"]:
            raise capacity.CapacityError("profile totals differ from reviewed budget")
        capacity.validate_budget(base, aggregate)
        result[name] = aggregate
    return result


def validate_live(base_document, profile_document, requested_stack, live_services):
    base = capacity.validate_contract(base_document)
    profiles = validate_profile_contract(profile_document)
    for name, profile in profiles["profiles"].items():
        total = external_totals(profiles)
        for stack in profile["stacks"]:
            capacity.add_resources(
                total,
                profiles[stack]["aggregate"]
                if stack in APP_STACKS
                else base["reviewed_totals"][stack],
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
    if seen_external != set(external_names):
        raise capacity.CapacityError("a declared external service is not live")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-contract", type=Path, default=ROOT / "config/capacity.yml")
    parser.add_argument("--profile-contract", type=Path, default=ROOT / "config/capacity-profiles.yml")
    parser.add_argument("--live", action="store_true")
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
    try:
        base = capacity.load_yaml(args.base_contract)
        profiles = capacity.load_yaml(args.profile_contract)
        if args.live:
            if args.requested_stack is None:
                raise capacity.CapacityError("live validation requires the requested stack")
            live_services = json.load(sys.stdin)
            if not isinstance(live_services, list) or not all(isinstance(item, dict) for item in live_services):
                raise capacity.CapacityError("live inventory must be a service list")
            validate_live(base, profiles, args.requested_stack, live_services)
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
    print("Explicit capacity profiles and requested inventory passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
