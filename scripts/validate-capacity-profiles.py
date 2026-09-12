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


def validate_profile_contract(document):
    root = capacity.expect_mapping(document, {"capacity_profiles"}, "profiles")
    profiles = capacity.expect_mapping(
        root["capacity_profiles"],
        {"schema_version", "active", "profiles", "organizationweb"},
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
        if plan["stacks"] != ["edge", "workloads", name]:
            raise capacity.CapacityError("profile must include edge, workloads and its own stack")
        capacity.validate_resource_totals(plan["aggregate"], f"profile.{name}.aggregate")
    app = capacity.expect_mapping(
        profiles["organizationweb"], {"expected_services", "aggregate"}, "profile.application"
    )
    if app["expected_services"] != ["backend", "postgres", "rabbitmq", "web"]:
        raise capacity.CapacityError("profile application services are not the reviewed set")
    capacity.validate_resource_totals(app["aggregate"], "profile.application.aggregate")
    return profiles


def validate_profiles(base_document, profile_document, stacks):
    base = capacity.validate_contract(base_document)
    legacy = capacity.validate_stacks(
        base, {name: stacks[name] for name in capacity.STACK_IDS}
    )
    profiles = validate_profile_contract(profile_document)
    app_contract = profiles["organizationweb"]
    services = stacks["organizationweb"]["services"]
    if set(services) != set(app_contract["expected_services"]):
        raise capacity.CapacityError("application services differ from reviewed set")
    app_total = capacity.empty_resources()
    for name in app_contract["expected_services"]:
        plan = capacity.service_resources(
            "organizationweb",
            name,
            services[name],
            set(),
            base["topology"]["eligible_nodes"],
            base["policy"]["service_memory_limit_to_reservation_ratio"],
        )
        capacity.add_resources(app_total, plan)
    if app_total != app_contract["aggregate"]:
        raise capacity.CapacityError("application totals differ from reviewed budget")
    totals = {**legacy, "organizationweb": app_total}
    result = {}
    for name, profile in profiles["profiles"].items():
        aggregate = capacity.empty_resources()
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
        total = capacity.empty_resources()
        for stack in profile["stacks"]:
            capacity.add_resources(
                total,
                profiles["organizationweb"]["aggregate"]
                if stack == "organizationweb"
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
            profiles["organizationweb"]["expected_services"]
            if stack == "organizationweb"
            else base["stacks"][stack]["expected_services"]
        )
    }
    for service in live_services:
        if (service.get("stack"), service.get("name")) not in allowed_names:
            raise capacity.CapacityError("live service is outside the reviewed active profile")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-contract", type=Path, default=ROOT / "config/capacity.yml")
    parser.add_argument("--profile-contract", type=Path, default=ROOT / "config/capacity-profiles.yml")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--requested-stack", choices=("edge", "workloads", "observability", "organizationweb", "site"))
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
            variables = capacity.load_yaml(ROOT / "config/organizationweb.yml")
            template = jinja2.Environment(
                loader=jinja2.FileSystemLoader(ROOT / "stacks/organizationweb"),
                undefined=jinja2.StrictUndefined,
            ).get_template("stack.yml.j2")
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
            }
            stacks["organizationweb"] = yaml.safe_load(
                template.render(**variables, image_channels_map=image_channels_map)
            )
            validate_profiles(base, profiles, stacks)
    except (capacity.CapacityError, ValueError, KeyError, TypeError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Explicit capacity profiles and requested inventory passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
