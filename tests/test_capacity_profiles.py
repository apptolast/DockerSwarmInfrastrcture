"""Explicit alternative capacity plans preserve the legacy budget."""

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import jinja2
import yaml

from ansible_task_harness import run_task_definition

ROOT = Path(__file__).resolve().parents[1]
PROFILE_TASKS = "ansible/roles/capacity_preflight/tasks/profiles.yml"
MIB = 1024 * 1024
# A synthetic group for these tests only, declared on a copy of the contract
# from which the repository's own groups (the AX lab) are removed first.
LAB_GROUP = {
    "kind-control-plane": {
        "reservations": {"cpu_millicores": 250, "memory_mib": 1024},
        "limits": {"cpu_millicores": 1000, "memory_mib": 2048},
        "pids_limit": 4096,
    },
    "kind-registry": {
        "reservations": {"cpu_millicores": 0, "memory_mib": 30},
        "limits": {"cpu_millicores": 250, "memory_mib": 64},
        "pids_limit": 256,
    },
}
LAB_TOTALS = {
    "reservations": {"cpu_millicores": 250, "memory_mib": 1054},
    "limits": {"cpu_millicores": 1250, "memory_mib": 2112},
}


def load_image_channels_map():
    """Return the reviewed per-stack channel map the stack templates render."""
    spec = importlib.util.spec_from_file_location(
        "validate_image_channels", ROOT / "scripts/validate-image-channels.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_channel_map(ROOT)["services"]


def load_autoupdater_stack():
    """Render the reviewed watcher stack without Docker."""
    spec = importlib.util.spec_from_file_location(
        "validate_autoupdater", ROOT / "scripts/validate-autoupdater.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render_validated()[1]


def external_live(profiles):
    """The live inspect of every declared external service, as Docker prints it."""
    services = []
    for stack, declared in profiles["capacity_profiles"]["external_stacks"].items():
        for name, service in declared.items():
            resources = {}
            for key, resource_class in (
                ("Limits", "limits"),
                ("Reservations", "reservations"),
            ):
                values = {
                    field: service[resource_class][source] * unit
                    for field, source, unit in (
                        ("NanoCPUs", "cpu_millicores", 1_000_000),
                        ("MemoryBytes", "memory_mib", 1024 * 1024),
                    )
                    if service[resource_class][source]
                }
                if values:
                    resources[key] = values
            services.append(
                {
                    "name": f"{stack}_{name}",
                    "stack": stack,
                    "mode": {"Replicated": {"Replicas": service["replicas"]}},
                    "resources": resources,
                }
            )
    return services


def group_totals(containers):
    """Sum the reservations and limits of one host container group."""
    totals = {
        resource_class: {"cpu_millicores": 0, "memory_mib": 0}
        for resource_class in ("reservations", "limits")
    }
    for container in containers.values():
        for resource_class, values in totals.items():
            for resource_name in values:
                values[resource_name] += container[resource_class][resource_name]
    return totals


def without_host_container_groups(profiles):
    """A copy of the contract without any host container group.

    Every plan that ran a group stops listing it and stops paying for it, so
    the copy is the contract as it was before the AX lab was declared.
    """
    candidate = json.loads(json.dumps(profiles))
    contract = candidate["capacity_profiles"]
    for group, containers in contract["host_containers"].items():
        totals = group_totals(containers)
        for plan in contract["profiles"].values():
            if group in plan["host_containers"]:
                plan["host_containers"].remove(group)
                for resource_class, values in totals.items():
                    for resource_name, value in values.items():
                        plan["aggregate"][resource_class][resource_name] -= value
    contract["host_containers"] = {}
    return candidate


def as_declared(name, declared, status="running"):
    """The preflight's read of a host container running exactly as declared."""
    return {
        "item": name,
        "rc": 0,
        "stdout": json.dumps(
            {
                "name": f"/{name}",
                "status": status,
                "host_config": {
                    "Memory": declared["limits"]["memory_mib"] * MIB,
                    "MemoryReservation": declared["reservations"]["memory_mib"] * MIB,
                    "NanoCpus": declared["limits"]["cpu_millicores"] * 1_000_000,
                    "PidsLimit": declared["pids_limit"],
                },
            }
        ),
        "stderr": "",
    }


def live_host_containers(profiles):
    """What the preflight reads on a converged host for this contract.

    The containers of the active plan's groups run exactly as declared; every
    other declared container is absent.
    """
    contract = profiles["capacity_profiles"]
    active = contract["profiles"][contract["active"]]["host_containers"]
    records = []
    for group, containers in contract["host_containers"].items():
        for name, declared in containers.items():
            records.append(
                as_declared(name, declared) if group in active else absent(name)
            )
    return records


def with_lab(profiles, running_plans=("organizationweb",)):
    """A copy of the profile contract that declares LAB_GROUP as group `lab`.

    The repository's own groups are removed first. Every plan in
    running_plans lists the group and pays for it.
    """
    candidate = without_host_container_groups(profiles)
    contract = candidate["capacity_profiles"]
    contract["host_containers"] = {"lab": json.loads(json.dumps(LAB_GROUP))}
    for name in running_plans:
        plan = contract["profiles"][name]
        plan["host_containers"].append("lab")
        for resource_class, values in LAB_TOTALS.items():
            for resource_name, value in values.items():
                plan["aggregate"][resource_class][resource_name] += value
    return candidate


def inspected(name, status="running", **host_config):
    """The preflight's read of one declared container, as Docker prints it."""
    declared = LAB_GROUP[name]
    config = {
        "Memory": declared["limits"]["memory_mib"] * MIB,
        "MemoryReservation": declared["reservations"]["memory_mib"] * MIB,
        "NanoCpus": declared["limits"]["cpu_millicores"] * 1_000_000,
        "PidsLimit": declared["pids_limit"],
    }
    config.update(host_config)
    return {
        "item": name,
        "rc": 0,
        "stdout": json.dumps(
            {"name": f"/{name}", "status": status, "host_config": config}
        ),
        "stderr": "",
    }


def absent(name, prefix="Error response from daemon: "):
    """The preflight's read of a declared container that does not exist.

    The default is Docker 29.6.2's exact stderr on the production host
    (checked 2026-09-25 with a missing name).
    """
    return {
        "item": name,
        "rc": 1,
        "stdout": "",
        "stderr": f"{prefix}No such container: {name}",
    }


class CapacityProfileTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "capacity_profiles", ROOT / "scripts/validate-capacity-profiles.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.base = yaml.safe_load((ROOT / "config/capacity.yml").read_text())
        self.profiles = yaml.safe_load((ROOT / "config/capacity-profiles.yml").read_text())
        self.parked = self.module.parked_workloads(
            yaml.safe_load((ROOT / "config/platform.yml").read_text())
        )
        channel_map = load_image_channels_map()
        self.stacks = {
                **{
                    name: yaml.safe_load((ROOT / f".build/{name}/stack.yml").read_text())
                    for name in ("edge", "workloads", "observability")
                },
                "autoupdater": load_autoupdater_stack(),
            }
        # Every independent application renders from its own catalog
        # and template, so the fixture follows APP_STACKS.
        for app_name in self.module.APP_STACKS:
            variables = yaml.safe_load(
                (ROOT / f"config/{app_name}.yml").read_text()
            )
            template = jinja2.Environment(
                loader=jinja2.FileSystemLoader(ROOT / f"stacks/{app_name}"),
                undefined=jinja2.StrictUndefined,
            ).get_template("stack.yml.j2")
            self.stacks[app_name] = yaml.safe_load(
                template.render(
                    **variables, image_channels_map=channel_map
                )
            )

    def test_application_profile_preserves_the_legacy_plan_and_reserves(self):
        totals = self.module.validate_profiles(self.base, self.profiles, self.stacks)
        # The active plan now carries the game and the AX lab (host container
        # group ax-lab: 560m/1936 MiB reserved, 2750m/3872 MiB limited, with
        # the web forwarder) as well; both plans leave out Minecraft and
        # OpenClaw while config/platform.yml parks them and count the
        # external stacks (16/896 MiB, 50m/2050m).
        self.assertEqual(
            totals["organizationweb"],
            {
                "reservations": {"cpu_millicores": 3110, "memory_mib": 5682},
                "limits": {"cpu_millicores": 16900, "memory_mib": 12173},
            },
        )
        # 224 MiB under the 15981 - 3072 - 512 = 12397 MiB memory limit budget.
        self.assertEqual(15981 - 3072 - 512 - 12173, 224)
        self.assertEqual(totals["observability"]["limits"]["memory_mib"], 8941)
        self.assertEqual(totals["observability"]["limits"]["cpu_millicores"], 16000)
        # Without the lab, the active plan is what it was before it.
        before = self.module.validate_profiles(
            self.base, without_host_container_groups(self.profiles), self.stacks
        )
        self.assertEqual(
            before["organizationweb"],
            {
                "reservations": {"cpu_millicores": 2550, "memory_mib": 3746},
                "limits": {"cpu_millicores": 14150, "memory_mib": 8301},
            },
        )
        self.assertEqual(before["observability"], totals["observability"])

    def test_host_container_group_counts_only_in_the_plans_that_run_it(self):
        before = self.module.validate_profiles(
            self.base, without_host_container_groups(self.profiles), self.stacks
        )
        totals = self.module.validate_profiles(
            self.base, with_lab(self.profiles), self.stacks
        )
        expected = json.loads(json.dumps(before["organizationweb"]))
        for resource_class, values in LAB_TOTALS.items():
            for resource_name, value in values.items():
                expected[resource_class][resource_name] += value
        self.assertEqual(totals["organizationweb"], expected)
        self.assertEqual(totals["observability"], before["observability"])
        # Declared but run by no plan: no plan pays for it.
        self.assertEqual(
            self.module.validate_profiles(
                self.base, with_lab(self.profiles, running_plans=()), self.stacks
            ),
            before,
        )
        # A plan that runs the group without paying for it breaks the
        # arithmetic, and one that pays for it must still fit the budget.
        unpaid = with_lab(self.profiles, running_plans=())
        unpaid["capacity_profiles"]["profiles"]["organizationweb"][
            "host_containers"
        ].append("lab")
        with self.assertRaisesRegex(
            self.module.capacity.CapacityError, "profile totals differ"
        ):
            self.module.validate_profiles(self.base, unpaid, self.stacks)
        oversized = with_lab(self.profiles, running_plans=("observability",))
        contract = oversized["capacity_profiles"]
        for resources in (
            contract["host_containers"]["lab"]["kind-control-plane"],
            contract["profiles"]["observability"]["aggregate"],
        ):
            resources["limits"]["memory_mib"] += 2048
            resources["reservations"]["memory_mib"] += 1024
        with self.assertRaisesRegex(
            self.module.capacity.CapacityError, "aggregate memory limits"
        ):
            self.module.validate_profiles(self.base, oversized, self.stacks)
        # The static check CI runs enforces the memory limit/reservation ratio
        # too, even for a group no plan runs, and a missing reservation fails.
        for reservation in (0, 20):
            with self.subTest(reservation=reservation):
                skewed = with_lab(self.profiles, running_plans=())
                skewed["capacity_profiles"]["host_containers"]["lab"]["kind-registry"][
                    "reservations"
                ]["memory_mib"] = reservation
                with self.assertRaisesRegex(
                    self.module.capacity.CapacityError, "ratio exceeds 2.50"
                ):
                    self.module.validate_profiles(self.base, skewed, self.stacks)

    def test_every_profile_must_include_the_watcher(self):
        for name in ("observability", "organizationweb"):
            with self.subTest(profile=name):
                profiles = yaml.safe_load(
                    (ROOT / "config/capacity-profiles.yml").read_text()
                )
                plan = profiles["capacity_profiles"]["profiles"][name]
                plan["stacks"].remove("autoupdater")
                plan["aggregate"]["reservations"]["memory_mib"] -= 18
                plan["aggregate"]["reservations"]["cpu_millicores"] -= 100
                plan["aggregate"]["limits"]["memory_mib"] -= 45
                plan["aggregate"]["limits"]["cpu_millicores"] -= 250
                with self.assertRaisesRegex(
                    self.module.capacity.CapacityError, "autoupdater"
                ):
                    self.module.validate_profiles(self.base, profiles, self.stacks)

    def test_live_registered_watcher_is_accepted_and_requestable(self):
        live = [
            {"name": "edge_traefik", "stack": "edge"},
            {"name": "organizationweb_web", "stack": "organizationweb"},
            {"name": "racinggame_web", "stack": "racinggame"},
            {"name": "autoupdater_shepherd", "stack": "autoupdater"},
            *external_live(self.profiles),
        ]
        for requested in (
            "autoupdater",
            "organizationweb",
            "racinggame",
            "edge",
            "workloads",
        ):
            with self.subTest(requested=requested):
                self.module.validate_live(
                    self.base,
                    self.profiles,
                    requested,
                    live,
                    parked=self.parked,
                    live_containers=live_host_containers(self.profiles),
                )
        for service in (
            {"name": "autoupdater_gantry", "stack": "autoupdater"},
            {"name": "autoupdater_shepherd", "stack": None},
            {"name": "shepherd", "stack": None},
        ):
            with self.subTest(service=service):
                with self.assertRaisesRegex(self.module.capacity.CapacityError, "live"):
                    self.module.validate_live(
                        self.base,
                        self.profiles,
                        "autoupdater",
                        [*live, service],
                        parked=self.parked,
                        live_containers=live_host_containers(self.profiles),
                    )

    def test_unaccounted_application_service_is_rejected(self):
        self.stacks["organizationweb"]["services"]["unreviewed"] = {
            "image": "unexpected",
        }
        with self.assertRaisesRegex(self.module.capacity.CapacityError, "services"):
            self.module.validate_profiles(self.base, self.profiles, self.stacks)

    def test_profile_cannot_omit_edge_even_with_matching_arithmetic(self):
        profile = self.profiles["capacity_profiles"]["profiles"]["organizationweb"]
        profile["stacks"].remove("edge")
        profile["aggregate"]["reservations"]["memory_mib"] -= 64
        profile["aggregate"]["reservations"]["cpu_millicores"] -= 100
        profile["aggregate"]["limits"]["memory_mib"] -= 128
        profile["aggregate"]["limits"]["cpu_millicores"] -= 500
        with self.assertRaisesRegex(self.module.capacity.CapacityError, "profile"):
            self.module.validate_profiles(self.base, self.profiles, self.stacks)

    def test_live_alternative_stack_blocks_deployment_in_both_directions(self):
        for active, other, service in (
            ("organizationweb", "observability", "prometheus"),
            ("observability", "organizationweb", "backend"),
        ):
            with self.subTest(active=active):
                self.profiles["capacity_profiles"]["active"] = active
                with self.assertRaisesRegex(self.module.capacity.CapacityError, "live"):
                    self.module.validate_live(
                        self.base, self.profiles, active,
                        [
                            {"name": f"{other}_{service}", "stack": other},
                            *external_live(self.profiles),
                        ],
                        parked=self.parked,
                        live_containers=live_host_containers(self.profiles),
                    )

    def test_live_name_must_belong_to_its_claimed_stack(self):
        with self.assertRaisesRegex(self.module.capacity.CapacityError, "live"):
            self.module.validate_live(
                self.base, self.profiles, "organizationweb",
                [
                    {"name": "workloads_kropia", "stack": "edge"},
                    *external_live(self.profiles),
                ],
                parked=self.parked,
                live_containers=live_host_containers(self.profiles),
            )

    def test_external_stacks_must_match_their_live_services(self):
        live = [
            {"name": "edge_traefik", "stack": "edge"},
            *external_live(self.profiles),
        ]
        self.module.validate_live(
            self.base,
            self.profiles,
            "edge",
            live,
            parked=self.parked,
            live_containers=live_host_containers(self.profiles),
        )

        def mutated(change):
            candidate = json.loads(json.dumps(live))
            sftp = next(item for item in candidate if item["name"] == "sftp_downloads")
            change(candidate, sftp)
            return candidate

        def set_path(path, value):
            def change(_candidate, service):
                target = service
                for key in path[:-1]:
                    target = target.setdefault(key, {})
                target[path[-1]] = value

            return change

        for label, change, message in (
            (
                "limit raised",
                set_path(("resources", "Limits", "MemoryBytes"), 256 * 1024 * 1024),
                "differs from its declaration",
            ),
            (
                "reservation dropped",
                lambda _c, service: service["resources"].pop("Reservations"),
                "differs from its declaration",
            ),
            (
                "second replica",
                set_path(("mode", "Replicated", "Replicas"), 2),
                "differs from its declaration",
            ),
            (
                "boolean replicas",
                set_path(("mode", "Replicated", "Replicas"), True),
                "replicas are invalid",
            ),
            (
                "global mode",
                lambda _c, service: service.update(mode={"Global": {}}),
                "not replicated",
            ),
            (
                "unaligned memory",
                set_path(("resources", "Limits", "MemoryBytes"), 134217729),
                "resources are invalid",
            ),
            (
                "unknown resource",
                set_path(("resources", "Limits", "Pids"), 64),
                "resources are invalid",
            ),
            (
                "empty limits list",
                set_path(("resources", "Limits"), []),
                "resources are invalid",
            ),
            (
                "false resources",
                lambda _c, service: service.update(resources=False),
                "resources are invalid",
            ),
            (
                "fractional millicore",
                set_path(("resources", "Limits", "NanoCPUs"), 500_000_001),
                "resources are invalid",
            ),
            (
                "replicated job",
                lambda _c, service: service.update(
                    mode={"ReplicatedJob": {"MaxConcurrent": 1}}
                ),
                "not replicated",
            ),
            (
                "negative replicas",
                set_path(("mode", "Replicated", "Replicas"), -1),
                "replicas are invalid",
            ),
            (
                "declared service gone",
                lambda candidate, service: candidate.remove(service),
                "declared external service is not live",
            ),
            (
                "undeclared service in an external stack",
                lambda candidate, service: candidate.append(
                    dict(service, name="sftp_uploads")
                ),
                "outside the reviewed active profile",
            ),
        ):
            with self.subTest(case=label):
                with self.assertRaisesRegex(
                    self.module.capacity.CapacityError, message
                ):
                    self.module.validate_live(
                        self.base,
                        self.profiles,
                        "edge",
                        mutated(change),
                        parked=self.parked,
                        live_containers=live_host_containers(self.profiles),
                    )

    def test_external_stack_contract_is_fail_closed(self):
        external = self.profiles["capacity_profiles"]["external_stacks"]
        for label, change, message in (
            (
                "stack named like a reviewed stack",
                lambda stacks: stacks.update(workloads=stacks.pop("sftp")),
                "external stack name is invalid",
            ),
            (
                "stack named like an application stack",
                lambda stacks: stacks.update(racinggame=stacks.pop("sftp")),
                "external stack name is invalid",
            ),
            (
                "invalid service name",
                lambda stacks: stacks["sftp"].update(
                    {"Downloads!": stacks["sftp"].pop("downloads")}
                ),
                "is not a service name",
            ),
            (
                "unlimited memory",
                lambda stacks: stacks["sftp"]["downloads"]["limits"].update(
                    memory_mib=0
                ),
                "positive memory_mib limit",
            ),
            (
                "unlimited cpu",
                lambda stacks: stacks["satisfactory-events"]["audit"]["limits"].update(
                    cpu_millicores=0
                ),
                "positive cpu_millicores limit",
            ),
            (
                "zero replicas",
                lambda stacks: stacks["sftp"]["downloads"].update(replicas=0),
                "replicas must be an integer >= 1",
            ),
            (
                "reservation above limit",
                lambda stacks: stacks["sftp"]["downloads"]["reservations"].update(
                    memory_mib=512
                ),
                "reserves more memory_mib",
            ),
            (
                "unknown field",
                lambda stacks: stacks["sftp"]["downloads"].update(ports=[2222]),
                "external_stacks.sftp.downloads",
            ),
            (
                "empty stack",
                lambda stacks: stacks.update(sftp={}),
                "has no services",
            ),
        ):
            with self.subTest(case=label):
                profiles = json.loads(json.dumps(self.profiles))
                change(profiles["capacity_profiles"]["external_stacks"])
                with self.assertRaisesRegex(
                    self.module.capacity.CapacityError, message
                ):
                    self.module.validate_profile_contract(profiles)
        # Every plan pays for the external stacks: dropping one without
        # re-reviewing the totals breaks the arithmetic.
        profiles = json.loads(json.dumps(self.profiles))
        del profiles["capacity_profiles"]["external_stacks"]["sftp"]
        with self.assertRaisesRegex(
            self.module.capacity.CapacityError, "profile totals differ"
        ):
            self.module.validate_profiles(self.base, profiles, self.stacks)
        self.assertEqual(
            set(external), {"satisfactory-companions", "satisfactory-events", "sftp"}
        )

    def test_contract_validation_never_rewrites_its_input(self):
        before = json.dumps(self.profiles, sort_keys=True)
        self.module.validate_profile_contract(self.profiles)
        self.module.validate_profiles(self.base, self.profiles, self.stacks)
        self.assertEqual(json.dumps(self.profiles, sort_keys=True), before)

    def test_hand_started_parked_service_blocks_every_other_playbook(self):
        parked = frozenset({"minecraft", "openclaw"})

        def workload(name, replicas):
            return {
                "name": f"workloads_{name}",
                "stack": "workloads",
                "mode": {"Replicated": {"Replicas": replicas}},
                "resources": {},
            }

        at_rest = [
            workload("minecraft", 0),
            workload("openclaw", 0),
            workload("kropia", 1),
            *external_live(self.profiles),
        ]
        running = [workload("minecraft", 1), *at_rest[1:]]
        for requested in ("edge", "autoupdater", "organizationweb", "workloads"):
            with self.subTest(requested=requested, state="parked"):
                self.module.validate_live(
                    self.base,
                    self.profiles,
                    requested,
                    at_rest,
                    parked=parked,
                    live_containers=live_host_containers(self.profiles),
                )
        # Only the playbooks that converge it to 0/0 may start while it runs.
        for requested in ("workloads", "site"):
            profiles = json.loads(json.dumps(self.profiles))
            if requested == "site":
                profiles["capacity_profiles"]["active"] = "observability"
            with self.subTest(requested=requested, state="running"):
                self.module.validate_live(
                    self.base,
                    profiles,
                    requested,
                    running,
                    parked=parked,
                    live_containers=live_host_containers(profiles),
                )
        for requested in ("edge", "autoupdater", "organizationweb", "racinggame"):
            with self.subTest(requested=requested, state="running"):
                with self.assertRaisesRegex(
                    self.module.capacity.CapacityError,
                    "parked live service runs over the budget: workloads_minecraft",
                ):
                    self.module.validate_live(
                        self.base,
                        self.profiles,
                        requested,
                        running,
                        parked=parked,
                        live_containers=live_host_containers(self.profiles),
                    )
        # An unparked service is not checked here: the stack owns its replicas.
        self.module.validate_live(
            self.base,
            self.profiles,
            "edge",
            running,
            parked=frozenset({"openclaw"}),
            live_containers=live_host_containers(self.profiles),
        )
        for platform in (
            {"platform_parked_workloads": ["n8n-db"]},
            {"platform_parked_workloads": ["openclaw", "minecraft"]},
            {"platform_parked_workloads": "minecraft"},
            {},
        ):
            with self.subTest(platform=platform):
                with self.assertRaisesRegex(
                    self.module.capacity.CapacityError, "parkable set"
                ):
                    self.module.parked_workloads(platform)

    def test_cli_blocks_an_inactive_requested_stack_before_any_mutation(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts/validate-capacity-profiles.py"),
             "--live", "--requested-stack", "observability"],
            input=json.dumps({"services": [], "host_containers": []}),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("outside the active profile", completed.stderr)

    def test_cli_static_profiles_render_organizationweb_from_image_channels(self):
        # scripts/validate-iac.sh runs the static mode, which renders the
        # OrganizationWeb template itself and must pass the channel map.
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts/validate-capacity-profiles.py")],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Explicit capacity profiles", completed.stdout)

    def test_shared_preflight_checks_live_profiles_in_check_and_apply(self):
        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/capacity_preflight/tasks/main.yml").read_text()
        )
        self.assertIn(
            "profiles.yml",
            [task.get("ansible.builtin.import_tasks") for task in tasks],
        )
        checks = yaml.safe_load(
            (ROOT / "ansible/roles/capacity_preflight/tasks/profiles.yml").read_text()
        )
        live_check = next(
            task for task in checks
            if "--live" in task.get("ansible.builtin.command", {}).get("argv", [])
        )
        self.assertIs(live_check["check_mode"], False)
        self.assertIs(live_check["changed_when"], False)
        inspect = next(
            task for task in checks
            if "inspect" in task.get("ansible.builtin.command", {}).get("argv", [])
        )
        inspect_format = " ".join(inspect["ansible.builtin.command"]["argv"])
        for field in ('"mode":{{json .Spec.Mode}}',
                      '"resources":{{json .Spec.TaskTemplate.Resources}}'):
            self.assertIn(field, inspect_format)
        for name in (
            "edge",
            "workloads",
            "observability",
            "autoupdater",
            "racinggame",
            "ax-lab",
            "site",
        ):
            play = yaml.safe_load((ROOT / f"ansible/playbooks/{name}.yml").read_text())[0]
            roles = [item["role"] for item in play["roles"]]
            self.assertLess(roles.index("operation_lock_guard"), roles.index("capacity_preflight"))


class HostContainerTests(unittest.TestCase):
    """Plain Docker containers budgeted beside the Swarm services."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "capacity_profiles", ROOT / "scripts/validate-capacity-profiles.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.error = self.module.capacity.CapacityError
        self.base = yaml.safe_load((ROOT / "config/capacity.yml").read_text())
        self.profiles = yaml.safe_load(
            (ROOT / "config/capacity-profiles.yml").read_text()
        )
        self.parked = self.module.parked_workloads(
            yaml.safe_load((ROOT / "config/platform.yml").read_text())
        )
        self.services = [
            {"name": "edge_traefik", "stack": "edge"},
            *external_live(self.profiles),
        ]

    def check_live(self, profiles, containers):
        self.module.validate_live(
            self.base,
            profiles,
            "edge",
            self.services,
            parked=self.parked,
            live_containers=containers,
        )

    def run_cli(self, argv, stdin="", profiles=None):
        """Run main() as the preflight does, on an optional temporary contract."""
        with tempfile.TemporaryDirectory() as directory:
            if profiles is not None:
                path = Path(directory) / "capacity-profiles.yml"
                path.write_text(yaml.safe_dump(profiles), encoding="utf-8")
                argv = ["--profile-contract", str(path), *argv]
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(stdin)), mock.patch(
                "sys.stdout", stdout
            ), mock.patch("sys.stderr", stderr):
                code = self.module.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_repository_declares_the_ax_lab_group_in_the_active_plan(self):
        contract = self.profiles["capacity_profiles"]
        self.assertEqual(
            contract["host_containers"],
            {
                "ax-lab": {
                    "kind-control-plane": {
                        "reservations": {"cpu_millicores": 500, "memory_mib": 1792},
                        "limits": {"cpu_millicores": 2000, "memory_mib": 3584},
                        "pids_limit": 4096,
                    },
                    "kind-registry": {
                        "reservations": {"cpu_millicores": 50, "memory_mib": 128},
                        "limits": {"cpu_millicores": 500, "memory_mib": 256},
                        "pids_limit": 256,
                    },
                    "ax-web-edge": {
                        "reservations": {"cpu_millicores": 10, "memory_mib": 16},
                        "limits": {"cpu_millicores": 250, "memory_mib": 32},
                        "pids_limit": 64,
                    },
                }
            },
        )
        self.assertEqual(contract["active"], "organizationweb")
        self.assertEqual(
            contract["profiles"]["organizationweb"]["host_containers"], ["ax-lab"]
        )
        self.assertEqual(contract["profiles"]["observability"]["host_containers"], [])
        # The limits the ax_lab role applies are the ones budgeted here.
        spec = importlib.util.spec_from_file_location(
            "validate_ax_lab_capacity", ROOT / "scripts/validate-ax-lab.py"
        )
        lab_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lab_module)
        lab_module.validate_capacity_group(
            yaml.safe_load((ROOT / "config/ax-lab.yml").read_text())["ax_lab"],
            contract["host_containers"]["ax-lab"],
        )
        # The preflight inspects every name: absent, stopped or exact.
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/validate-capacity-profiles.py"),
                "--host-container-names",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            ["ax-web-edge", "kind-control-plane", "kind-registry"],
        )
        running = live_host_containers(self.profiles)
        self.assertEqual(
            [record["item"] for record in running],
            ["kind-control-plane", "kind-registry", "ax-web-edge"],
        )
        edge = absent("ax-web-edge")
        self.check_live(self.profiles, running)
        code, _stdout, stderr = self.run_cli(
            ["--live", "--requested-stack", "edge"],
            json.dumps({"services": self.services, "host_containers": running}),
        )
        self.assertEqual(code, 0, stderr)
        # A production playbook never depends on the lab being up: absent (a
        # rebuilt host) or stopped (restart policy "no" after a reboot or a
        # Docker restart) runs no process and stays budgeted in the plan.
        node = contract["host_containers"]["ax-lab"]["kind-control-plane"]
        registry = contract["host_containers"]["ax-lab"]["kind-registry"]
        forwarder = contract["host_containers"]["ax-lab"]["ax-web-edge"]
        for label, containers in (
            (
                "absent",
                [absent("kind-control-plane"), absent("kind-registry"), edge],
            ),
            ("registry absent", [running[0], absent("kind-registry"), running[2]]),
            (
                "stopped after a reboot",
                [
                    as_declared("kind-control-plane", node, status="exited"),
                    as_declared("kind-registry", registry, status="exited"),
                    as_declared("ax-web-edge", forwarder, status="exited"),
                ],
            ),
            (
                "node stopped",
                [
                    as_declared("kind-control-plane", node, status="exited"),
                    running[1],
                    edge,
                ],
            ),
        ):
            with self.subTest(case=label):
                self.check_live(self.profiles, containers)
                code, _stdout, stderr = self.run_cli(
                    ["--live", "--requested-stack", "edge"],
                    json.dumps(
                        {"services": self.services, "host_containers": containers}
                    ),
                )
                self.assertEqual(code, 0, stderr)
        # A lab container that exists must carry its declared limits, running
        # or stopped. The manual lab has no CPU or PID limit and no memory
        # reservation, so every other playbook stops while it exists.
        manual_node = json.loads(running[0]["stdout"])
        manual_node["host_config"].update(
            NanoCpus=0, PidsLimit=None, MemoryReservation=0
        )
        manual = {**running[0], "stdout": json.dumps(manual_node)}
        stopped_manual = {
            **manual,
            "stdout": json.dumps({**manual_node, "status": "exited"}),
        }
        for label, containers in (
            ("the manual lab", [manual, running[1], edge]),
            ("the manual lab, stopped", [stopped_manual, running[1], edge]),
        ):
            with self.subTest(case=label):
                with self.assertRaisesRegex(
                    self.error,
                    "differs from its declaration: kind-control-plane "
                    "MemoryReservation",
                ):
                    self.check_live(self.profiles, containers)
                code, _stdout, stderr = self.run_cli(
                    ["--live", "--requested-stack", "edge"],
                    json.dumps(
                        {"services": self.services, "host_containers": containers}
                    ),
                )
                self.assertEqual(code, 1)
                self.assertIn("differs from its declaration", stderr)
        code, _stdout, stderr = self.run_cli(
            ["--live", "--requested-stack", "edge"], json.dumps(self.services)
        )
        self.assertEqual(code, 1)
        self.assertIn("must hold exactly services and host_containers", stderr)

    def test_only_the_ax_lab_playbook_converges_its_own_group(self):
        group = self.profiles["capacity_profiles"]["host_containers"]["ax-lab"]
        node = group["kind-control-plane"]
        registry = group["kind-registry"]
        forwarder = group["ax-web-edge"]
        drifted_node = as_declared("kind-control-plane", node)
        document = json.loads(drifted_node["stdout"])
        document["host_config"].update(Memory=0, NanoCpus=0, PidsLimit=None)
        drifted_node["stdout"] = json.dumps(document)
        stopped_drifted_node = {
            **drifted_node,
            "stdout": json.dumps({**document, "status": "exited"}),
        }
        drifted_registry = as_declared("kind-registry", registry)
        document = json.loads(drifted_registry["stdout"])
        document["host_config"].update(PidsLimit=512)
        drifted_registry["stdout"] = json.dumps(document)

        def ax_lab(profiles, containers):
            self.module.validate_live(
                self.base,
                profiles,
                "ax-lab",
                self.services,
                parked=self.parked,
                live_containers=containers,
            )

        # The playbook that creates, starts and converges the group may start
        # while it is absent (first apply), stopped (after a reboot, restart
        # policy "no") or drifted (kind creates the node without limits).
        # Every other playbook accepts the group absent or stopped, but never
        # a container with other limits, running or stopped.
        for label, containers, others in (
            ("converged", live_host_containers(self.profiles), None),
            (
                "absent",
                [
                    absent("kind-control-plane"),
                    absent("kind-registry"),
                    absent("ax-web-edge"),
                ],
                None,
            ),
            (
                "stopped",
                [
                    as_declared("kind-control-plane", node, status="exited"),
                    as_declared("kind-registry", registry, status="exited"),
                    as_declared("ax-web-edge", forwarder, status="exited"),
                ],
                None,
            ),
            (
                "created, not started",
                [
                    as_declared("kind-control-plane", node, status="created"),
                    absent("kind-registry"),
                    absent("ax-web-edge"),
                ],
                None,
            ),
            (
                "drifted",
                [
                    drifted_node,
                    as_declared("kind-registry", registry),
                    absent("ax-web-edge"),
                ],
                "differs from its declaration: kind-control-plane Memory",
            ),
            (
                "drifted and stopped",
                [stopped_drifted_node, absent("kind-registry"), absent("ax-web-edge")],
                "differs from its declaration: kind-control-plane Memory",
            ),
            (
                "registry drifted",
                [
                    as_declared("kind-control-plane", node, status="exited"),
                    drifted_registry,
                    absent("ax-web-edge"),
                ],
                "differs from its declaration: kind-registry PidsLimit",
            ),
        ):
            envelope = json.dumps(
                {"services": self.services, "host_containers": containers}
            )
            with self.subTest(case=label):
                ax_lab(self.profiles, containers)
                code, _stdout, stderr = self.run_cli(
                    ["--live", "--requested-stack", "ax-lab"], envelope
                )
                self.assertEqual(code, 0, stderr)
                if others is None:
                    self.check_live(self.profiles, containers)
                else:
                    with self.assertRaisesRegex(self.error, others):
                        self.check_live(self.profiles, containers)
                    code, _stdout, stderr = self.run_cli(
                        ["--live", "--requested-stack", "edge"], envelope
                    )
                    self.assertEqual(code, 1)
                    self.assertIn(others, stderr)
        # A state that is neither running nor stopped stops every playbook,
        # ax-lab included: its role converges only running or stopped ones.
        for status in ("paused", "restarting", "removing"):
            containers = [
                as_declared("kind-control-plane", node, status=status),
                as_declared("kind-registry", registry),
                absent("ax-web-edge"),
            ]
            for requested in ("ax-lab", "edge"):
                with self.subTest(status=status, requested=requested):
                    with self.assertRaisesRegex(
                        self.error,
                        f"active host container is {status}, neither running "
                        "nor stopped: kind-control-plane",
                    ):
                        self.module.validate_live(
                            self.base,
                            self.profiles,
                            requested,
                            self.services,
                            parked=self.parked,
                            live_containers=containers,
                        )
        # Its reads must still be well formed and complete.
        for label, containers, message in (
            (
                "unreadable",
                [
                    {**absent("kind-control-plane"), "stderr": "permission denied"},
                    absent("kind-registry"),
                    absent("ax-web-edge"),
                ],
                "cannot inspect live host container kind-control-plane",
            ),
            (
                "missing read",
                [absent("kind-control-plane"), absent("ax-web-edge")],
                "live host container data is missing: kind-registry",
            ),
            (
                "another container through an ID prefix",
                [
                    {
                        **as_declared("kind-control-plane", node),
                        "stdout": json.dumps(
                            {
                                "name": "/kind-control-plane-old",
                                "status": "running",
                                "host_config": {
                                    "Memory": 0,
                                    "MemoryReservation": 0,
                                    "NanoCpus": 0,
                                    "PidsLimit": None,
                                },
                            }
                        ),
                    },
                    absent("kind-registry"),
                    absent("ax-web-edge"),
                ],
                "inspected container is not kind-control-plane",
            ),
        ):
            with self.subTest(case=label):
                with self.assertRaisesRegex(self.error, message):
                    ax_lab(self.profiles, containers)
        # Swarm services and parked workloads are checked as for any playbook.
        with self.assertRaisesRegex(self.error, "outside the reviewed active profile"):
            self.module.validate_live(
                self.base,
                self.profiles,
                "ax-lab",
                [
                    *self.services,
                    {"name": "observability_grafana", "stack": "observability"},
                ],
                parked=self.parked,
                live_containers=live_host_containers(self.profiles),
            )
        # Another group of the active plan stays strict even for ax-lab: it
        # may be absent or stopped, never with other limits.
        both = json.loads(json.dumps(self.profiles))
        contract = both["capacity_profiles"]
        contract["host_containers"]["extra"] = {
            "extra-box": {
                "reservations": {"cpu_millicores": 0, "memory_mib": 1},
                "limits": {"cpu_millicores": 1, "memory_mib": 2},
                "pids_limit": 1,
            }
        }
        plan = contract["profiles"]["organizationweb"]
        plan["host_containers"].append("extra")
        plan["aggregate"]["reservations"]["memory_mib"] += 1
        plan["aggregate"]["limits"]["memory_mib"] += 2
        plan["aggregate"]["limits"]["cpu_millicores"] += 1
        extra = contract["host_containers"]["extra"]["extra-box"]
        lab = [drifted_node, absent("kind-registry"), absent("ax-web-edge")]
        for state in (
            as_declared("extra-box", extra),
            as_declared("extra-box", extra, status="exited"),
            absent("extra-box"),
        ):
            ax_lab(both, [*lab, state])
        drifted_extra = json.loads(as_declared("extra-box", extra)["stdout"])
        drifted_extra["host_config"].update(PidsLimit=None)
        for status in ("running", "exited"):
            with self.subTest(extra=status):
                with self.assertRaisesRegex(
                    self.error, "differs from its declaration: extra-box PidsLimit"
                ):
                    ax_lab(
                        both,
                        [
                            *lab,
                            {
                                **as_declared("extra-box", extra),
                                "stdout": json.dumps(
                                    {**drifted_extra, "status": status}
                                ),
                            },
                        ],
                    )

    def test_ax_lab_is_requestable_only_while_the_active_plan_runs_it(self):
        # The observability plan requires the lab stopped: ax-lab is outside it.
        observability = json.loads(json.dumps(self.profiles))
        observability["capacity_profiles"]["active"] = "observability"
        stopped = [
            absent("kind-control-plane"),
            absent("kind-registry"),
            absent("ax-web-edge"),
        ]
        services = [
            {"name": "observability_prometheus", "stack": "observability"},
            *external_live(self.profiles),
        ]
        for profiles, message in (
            (observability, "runs no host container group ax-lab"),
            (
                without_host_container_groups(self.profiles),
                "runs no host container group ax-lab",
            ),
        ):
            with self.subTest(active=profiles["capacity_profiles"]["active"]):
                with self.assertRaisesRegex(self.error, message):
                    self.module.validate_live(
                        self.base,
                        profiles,
                        "ax-lab",
                        services,
                        parked=self.parked,
                        live_containers=live_host_containers(profiles),
                    )
        # There, the lab's containers must not run for any other playbook.
        self.module.validate_live(
            self.base,
            observability,
            "observability",
            services,
            parked=self.parked,
            live_containers=stopped,
        )
        with self.assertRaisesRegex(
            self.error, "outside the active profile is running: ax-web-edge"
        ):
            self.module.validate_live(
                self.base,
                observability,
                "observability",
                services,
                parked=self.parked,
                live_containers=live_host_containers(self.profiles),
            )
        # The CLI accepts the name, and no other lab spelling.
        self.assertIn("ax-lab", self.module.HOST_CONTAINER_PLAYBOOKS)
        for requested in ("ax_lab", "ax-lab-extra", "kind"):
            with self.subTest(requested=requested):
                with self.assertRaises(SystemExit):
                    self.run_cli(["--live", "--requested-stack", requested], "{}")

    def test_host_container_contract_is_fail_closed(self):
        def lab(contract):
            return contract["host_containers"]["lab"]

        def registry(contract):
            return lab(contract)["kind-registry"]

        for label, change, message in (
            (
                "not a mapping",
                lambda c: c.update(host_containers=["kind-registry"]),
                "host_containers must be a mapping",
            ),
            (
                "missing section",
                lambda c: c.pop("host_containers"),
                "profiles has missing host_containers",
            ),
            (
                "unknown section",
                lambda c: c.update(host_services={}),
                "profiles has unexpected host_services",
            ),
            (
                "invalid group name",
                lambda c: c["host_containers"].update(
                    {"Lab!": c["host_containers"].pop("lab")}
                ),
                "host container group name is invalid",
            ),
            (
                "empty group",
                lambda c: c["host_containers"].update(lab={}),
                "host container group lab has no containers",
            ),
            (
                "one-character name",
                lambda c: lab(c).update({"k": lab(c).pop("kind-registry")}),
                "is not a container name",
            ),
            (
                "name with a leading dash",
                lambda c: lab(c).update({"-kind": lab(c).pop("kind-registry")}),
                "is not a container name",
            ),
            (
                "name with a slash",
                lambda c: lab(c).update({"/kind": lab(c).pop("kind-registry")}),
                "is not a container name",
            ),
            (
                "name with a space",
                lambda c: lab(c).update({"kind registry": lab(c).pop("kind-registry")}),
                "is not a container name",
            ),
            (
                "name declared in two groups",
                lambda c: c["host_containers"].update(
                    other={"kind-registry": registry(c)}
                ),
                "host container kind-registry is declared more than once",
            ),
            (
                "unlimited memory",
                lambda c: registry(c)["limits"].update(memory_mib=0),
                "positive memory_mib limit",
            ),
            (
                "unlimited cpu",
                lambda c: registry(c)["limits"].update(cpu_millicores=0),
                "positive cpu_millicores limit",
            ),
            (
                "memory reservation above limit",
                lambda c: registry(c)["reservations"].update(memory_mib=128),
                "reserves more memory_mib",
            ),
            (
                "cpu reservation above limit",
                lambda c: registry(c)["reservations"].update(cpu_millicores=500),
                "reserves more cpu_millicores",
            ),
            (
                "negative reservation",
                lambda c: registry(c)["reservations"].update(cpu_millicores=-1),
                "cpu_millicores must be an integer >= 0",
            ),
            (
                "zero pids limit",
                lambda c: registry(c).update(pids_limit=0),
                "pids_limit must be an integer >= 1",
            ),
            (
                "boolean pids limit",
                lambda c: registry(c).update(pids_limit=True),
                "pids_limit must be an integer >= 1",
            ),
            (
                "missing pids limit",
                lambda c: registry(c).pop("pids_limit"),
                "kind-registry has missing pids_limit",
            ),
            (
                "unknown container field",
                lambda c: registry(c).update(privileged=True),
                "kind-registry has unexpected privileged",
            ),
            (
                "plan without the list",
                lambda c: c["profiles"]["observability"].pop("host_containers"),
                "profile.observability has missing host_containers",
            ),
            (
                "plan list as a string",
                lambda c: c["profiles"]["observability"].update(host_containers="lab"),
                "must list distinct group names",
            ),
            (
                "plan list with a repeated group",
                lambda c: c["profiles"]["organizationweb"]["host_containers"].append(
                    "lab"
                ),
                "must list distinct group names",
            ),
            (
                "plan running an undeclared group",
                lambda c: c["profiles"]["observability"]["host_containers"].append(
                    "ghost"
                ),
                "runs an undeclared host container group: ghost",
            ),
        ):
            with self.subTest(case=label):
                profiles = with_lab(self.profiles)
                change(profiles["capacity_profiles"])
                with self.assertRaisesRegex(self.error, message):
                    self.module.validate_profile_contract(profiles)
        # The valid declaration passes and is returned, not rewritten.
        profiles = with_lab(self.profiles)
        before = json.dumps(profiles, sort_keys=True)
        validated = self.module.validate_profile_contract(profiles)
        self.assertEqual(set(validated["host_containers"]["lab"]), set(LAB_GROUP))
        self.assertEqual(json.dumps(profiles, sort_keys=True), before)

    def test_host_container_memory_follows_the_service_ratio_policy(self):
        for label, reservation, limit, message in (
            ("above 2.50", 512, 2048, "ratio exceeds 2.50"),
            ("zero reservation", 0, 64, "ratio exceeds 2.50"),
            ("exactly 2.50", 32, 80, None),
        ):
            with self.subTest(case=label):
                profiles = with_lab(self.profiles, running_plans=())
                declared = profiles["capacity_profiles"]["host_containers"]["lab"]
                declared["kind-registry"]["reservations"]["memory_mib"] = reservation
                declared["kind-registry"]["limits"]["memory_mib"] = limit
                containers = [absent(name) for name in LAB_GROUP]
                if message is None:
                    self.check_live(profiles, containers)
                    continue
                with self.assertRaisesRegex(self.error, message):
                    self.check_live(profiles, containers)
                # The preflight never gets a name to inspect from it either.
                code, stdout, stderr = self.run_cli(
                    ["--host-container-names"], profiles=profiles
                )
                self.assertEqual((code, stdout), (1, ""))
                self.assertIn(message, stderr)

    def test_live_budget_and_arithmetic_include_the_running_groups(self):
        running = [inspected(name) for name in LAB_GROUP]
        self.check_live(with_lab(self.profiles), running)
        unpaid = with_lab(self.profiles, running_plans=())
        unpaid["capacity_profiles"]["profiles"]["organizationweb"][
            "host_containers"
        ].append("lab")
        with self.assertRaisesRegex(
            self.error, "profile organizationweb has inconsistent arithmetic"
        ):
            self.check_live(unpaid, running)
        oversized = with_lab(self.profiles, running_plans=("observability",))
        contract = oversized["capacity_profiles"]
        for resources in (
            contract["host_containers"]["lab"]["kind-control-plane"],
            contract["profiles"]["observability"]["aggregate"],
        ):
            resources["limits"]["cpu_millicores"] += 1000
        with self.assertRaisesRegex(self.error, "aggregate CPU limits"):
            self.check_live(oversized, [absent(name) for name in LAB_GROUP])

    def test_active_host_containers_are_absent_stopped_or_exact(self):
        profiles = with_lab(self.profiles)
        self.check_live(profiles, [inspected(name) for name in LAB_GROUP])
        # Absent or stopped runs no process and stays budgeted in the plan.
        for label, registry in (
            ("absent", absent("kind-registry")),
            ("absent, older Docker", absent("kind-registry", "Error: ")),
            ("stopped", inspected("kind-registry", status="exited")),
            ("never started", inspected("kind-registry", status="created")),
            ("dead", inspected("kind-registry", status="dead")),
        ):
            with self.subTest(case=label):
                self.check_live(profiles, [inspected("kind-control-plane"), registry])
        for label, registry, message in (
            (
                "paused",
                inspected("kind-registry", status="paused"),
                "is paused, neither running nor stopped: kind-registry",
            ),
            (
                "restarting",
                inspected("kind-registry", status="restarting"),
                "is restarting, neither running nor stopped: kind-registry",
            ),
            (
                "removing",
                inspected("kind-registry", status="removing"),
                "is removing, neither running nor stopped: kind-registry",
            ),
            (
                "a HostConfig field the preflight does not read",
                inspected("kind-registry", CpuQuota=25_000),
                "live host container kind-registry is malformed",
            ),
        ):
            with self.subTest(case=label):
                with self.assertRaisesRegex(self.error, message):
                    self.check_live(
                        profiles, [inspected("kind-control-plane"), registry]
                    )
        # A container that exists carries exactly its declared limits, running
        # or stopped: once started, nothing converges it again.
        for label, host_config, field in (
            ("memory limit", {"Memory": 128 * MIB}, "Memory"),
            ("unlimited memory", {"Memory": 0}, "Memory"),
            ("memory reservation", {"MemoryReservation": 0}, "MemoryReservation"),
            ("cpu limit", {"NanoCpus": 500_000_000}, "NanoCpus"),
            ("pids limit", {"PidsLimit": 512}, "PidsLimit"),
            ("unset pids limit", {"PidsLimit": None}, "PidsLimit"),
            ("unlimited pids", {"PidsLimit": -1}, "PidsLimit"),
        ):
            for status in ("running", "exited", "created", "dead"):
                with self.subTest(case=label, status=status):
                    with self.assertRaisesRegex(
                        self.error,
                        f"differs from its declaration: kind-registry {field}$",
                    ):
                        self.check_live(
                            profiles,
                            [
                                inspected("kind-control-plane"),
                                inspected(
                                    "kind-registry", status=status, **host_config
                                ),
                            ],
                        )

    def test_host_containers_outside_the_active_plan_must_not_run(self):
        for running_plans in ((), ("observability",)):
            profiles = with_lab(self.profiles, running_plans=running_plans)
            for label, state in (
                ("absent", absent("kind-control-plane")),
                ("absent, older Docker", absent("kind-control-plane", "Error: ")),
                ("stopped", inspected("kind-control-plane", status="exited")),
                ("never started", inspected("kind-control-plane", status="created")),
                ("dead", inspected("kind-control-plane", status="dead")),
                # A stopped container holds no memory: its limits are not read.
                (
                    "stopped with other limits",
                    inspected("kind-control-plane", status="exited", Memory=0),
                ),
            ):
                with self.subTest(plans=running_plans, case=label):
                    self.check_live(profiles, [state, absent("kind-registry")])
            for status in ("running", "paused", "restarting", "removing"):
                with self.subTest(plans=running_plans, status=status):
                    with self.assertRaisesRegex(
                        self.error,
                        f"host container outside the active profile is {status}: "
                        "kind-control-plane",
                    ):
                        self.check_live(
                            profiles,
                            [
                                inspected("kind-control-plane", status=status),
                                absent("kind-registry"),
                            ],
                        )

    def test_live_host_container_data_is_fail_closed(self):
        profiles = with_lab(self.profiles)
        control_plane = inspected("kind-control-plane")
        registry = inspected("kind-registry")

        def rewritten(record, **fields):
            return {**record, **fields}

        def reinspected(**document):
            record = json.loads(registry["stdout"])
            record.update(document)
            return rewritten(registry, stdout=json.dumps(record))

        for label, containers, message in (
            (
                "no host container inventory",
                None,
                "live host container inventory must be a list",
            ),
            (
                "declared container not read",
                [control_plane],
                "live host container data is missing: kind-registry",
            ),
            (
                "container read twice",
                [control_plane, registry, registry],
                "not one declared container: 'kind-registry'",
            ),
            (
                # The preflight reads declared names only: a record for any
                # other one means its input and the contract disagree.
                "record for a name the contract does not declare",
                [control_plane, registry, absent("kind-worker")],
                "not one declared container: 'kind-worker'",
            ),
            (
                "record without stderr",
                [control_plane, {k: v for k, v in registry.items() if k != "stderr"}],
                "live host container record is malformed",
            ),
            (
                "record with an extra key",
                [control_plane, rewritten(registry, cmd=["docker"])],
                "live host container record is malformed",
            ),
            (
                "boolean return code",
                [control_plane, rewritten(registry, rc=False)],
                "live host container kind-registry is malformed",
            ),
            (
                "daemon unreachable",
                [
                    control_plane,
                    rewritten(
                        absent("kind-registry"),
                        stderr="Cannot connect to the Docker daemon at "
                        "unix:///var/run/docker.sock. Is the docker daemon running?",
                    ),
                ],
                "cannot inspect live host container kind-registry: Cannot connect",
            ),
            (
                "failure without a diagnostic",
                [control_plane, rewritten(absent("kind-registry"), stderr="")],
                "cannot inspect live host container kind-registry: exit code 1",
            ),
            (
                "absence of another container",
                [
                    control_plane,
                    rewritten(
                        absent("kind-registry"),
                        stderr="Error: No such container: kind-registry-old",
                    ),
                ],
                "cannot inspect live host container kind-registry",
            ),
            (
                "absence with output",
                [control_plane, rewritten(absent("kind-registry"), stdout="[]")],
                "cannot inspect live host container kind-registry",
            ),
            (
                "template error",
                [
                    control_plane,
                    rewritten(
                        absent("kind-registry"),
                        stderr="template parsing error: can't evaluate field Name",
                    ),
                ],
                "cannot inspect live host container kind-registry",
            ),
            (
                "output is not JSON",
                [control_plane, rewritten(registry, stdout="running")],
                "live host container kind-registry is malformed",
            ),
            (
                "output with an extra field",
                [control_plane, reinspected(image="registry:2")],
                "live host container kind-registry is malformed",
            ),
            (
                "another container read through an ID prefix",
                [control_plane, reinspected(name="/kind-registry-old")],
                "inspected container is not kind-registry",
            ),
            (
                "unknown status",
                [control_plane, reinspected(status="sleeping")],
                "live host container kind-registry is malformed",
            ),
            (
                "host config missing",
                [control_plane, reinspected(host_config=None)],
                "live host container kind-registry is malformed",
            ),
            (
                "memory as text",
                [control_plane, inspected("kind-registry", Memory="64m")],
                "kind-registry has an invalid Memory",
            ),
            (
                "negative memory reservation",
                [control_plane, inspected("kind-registry", MemoryReservation=-1)],
                "kind-registry has an invalid MemoryReservation",
            ),
            (
                "boolean cpu limit",
                [control_plane, inspected("kind-registry", NanoCpus=True)],
                "kind-registry has an invalid NanoCpus",
            ),
            (
                "pids limit as text",
                [control_plane, inspected("kind-registry", PidsLimit="256")],
                "kind-registry has an invalid PidsLimit",
            ),
        ):
            with self.subTest(case=label):
                with self.assertRaisesRegex(self.error, message):
                    self.check_live(profiles, containers)
        # The same holds for data of a group no plan runs.
        with self.assertRaisesRegex(self.error, "cannot inspect"):
            self.check_live(
                with_lab(self.profiles, running_plans=()),
                [
                    absent("kind-control-plane"),
                    rewritten(absent("kind-registry"), stderr="permission denied"),
                ],
            )

    def test_cli_reads_the_preflight_envelope(self):
        profiles = with_lab(self.profiles)
        code, stdout, stderr = self.run_cli(
            ["--host-container-names"], profiles=profiles
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), ["kind-control-plane", "kind-registry"])
        live = ["--live", "--requested-stack", "edge"]
        envelope = {
            "services": self.services,
            "host_containers": [inspected(name) for name in LAB_GROUP],
        }
        code, stdout, stderr = self.run_cli(live, json.dumps(envelope), profiles)
        self.assertEqual(code, 0, stderr)
        self.assertIn("passed", stdout)
        for label, stdin, message in (
            (
                "bare service list",
                json.dumps(self.services),
                "must hold exactly services and host_containers",
            ),
            (
                "extra key",
                json.dumps({**envelope, "networks": []}),
                "must hold exactly services and host_containers",
            ),
            (
                "containers not a list",
                json.dumps({**envelope, "host_containers": {}}),
                "live host container inventory must be a list",
            ),
            (
                "services not a list",
                json.dumps({**envelope, "services": {}}),
                "live inventory must be a service list",
            ),
            ("not JSON", "services", "ERROR"),
        ):
            with self.subTest(case=label):
                code, _stdout, stderr = self.run_cli(live, stdin, profiles)
                self.assertEqual(code, 1)
                self.assertIn(message, stderr)
        with self.assertRaises(SystemExit):
            self.run_cli(["--live", "--host-container-names"])

    def test_preflight_reads_every_declared_container_before_the_live_check(self):
        tasks = yaml.safe_load((ROOT / PROFILE_TASKS).read_text())

        def argv(task):
            return task.get("ansible.builtin.command", {}).get("argv", [])

        names = next(t for t in tasks if "--host-container-names" in argv(t))
        reads = next(t for t in tasks if argv(t)[1:3] == ["container", "inspect"])
        services = next(t for t in tasks if argv(t)[1:3] == ["service", "inspect"])
        check = next(t for t in tasks if "--live" in argv(t))
        order = [tasks.index(task) for task in (services, names, reads, check)]
        self.assertEqual(order, sorted(order))
        for task in (names, reads, check):
            self.assertIs(task["changed_when"], False)
            self.assertIs(task["check_mode"], False)
        # The contract is read on the controller, like the live check.
        for task in (names, check):
            self.assertEqual(task["delegate_to"], "localhost")
            self.assertIs(task["become"], False)
        # A missing container must reach the validator instead of failing.
        self.assertIs(reads["failed_when"], False)
        self.assertEqual(argv(reads)[0], "/usr/bin/docker")
        self.assertEqual(argv(reads)[-1], "{{ item }}")
        self.assertIn("capacity_preflight_host_container_names.stdout", reads["loop"])
        read_format = " ".join(argv(reads))
        for field in (
            '"name":{{json .Name}}',
            '"status":{{json .State.Status}}',
            '"Memory":{{json .HostConfig.Memory}}',
            '"MemoryReservation":{{json .HostConfig.MemoryReservation}}',
            # The Go field is NanoCPUs; its JSON key is NanoCpus.
            '"NanoCpus":{{json .HostConfig.NanoCPUs}}',
            '"PidsLimit":{{json .HostConfig.PidsLimit}}',
        ):
            self.assertIn(field, read_format)
        # Only the fields the validator compares are read.
        self.assertNotIn("{{json .HostConfig}}", read_format)
        self.assertEqual(
            read_format.count("{{json .HostConfig."),
            len(self.module.HOST_CONFIG_FIELDS),
        )
        # The exact stdin expression, rendered from synthetic registered
        # results: loop bookkeeping is dropped, absences are kept.
        service = {"name": "edge_traefik", "stack": "edge"}
        bookkeeping = {"changed": False, "failed": False, "ansible_loop_var": "item"}
        for label, results, expected in (
            ("none declared", [], []),
            (
                "one absent, one running",
                [
                    {
                        **absent("kind-registry"),
                        **bookkeeping,
                        "msg": "non-zero return code",
                        "stderr_lines": ["No such container: kind-registry"],
                    },
                    {**inspected("kind-control-plane"), **bookkeeping},
                ],
                [absent("kind-registry"), inspected("kind-control-plane")],
            ),
        ):
            with self.subTest(case=label):
                probe = {
                    "name": "Render the live inventory the validator reads",
                    "ansible.builtin.assert": {
                        "that": [
                            "capacity_preflight_probe_stdin | from_json"
                            " == capacity_preflight_probe_expected"
                        ],
                        "quiet": True,
                    },
                    "vars": {
                        "capacity_preflight_probe_stdin": check[
                            "ansible.builtin.command"
                        ]["stdin"]
                    },
                }
                completed = run_task_definition(
                    probe,
                    {
                        "capacity_preflight_live_specs": {
                            "results": [
                                {"item": "id", "rc": 0, "stdout": json.dumps(service)}
                            ]
                        },
                        "capacity_preflight_host_container_specs": {"results": results},
                        "capacity_preflight_probe_expected": {
                            "services": [service],
                            "host_containers": expected,
                        },
                    },
                )
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )


if __name__ == "__main__":
    unittest.main()
