"""Explicit alternative capacity plans preserve the legacy budget."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]


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
        # The active plan now carries the game as well; both plans leave out
        # Minecraft and OpenClaw while config/platform.yml parks them and
        # count the external stacks (16/896 MiB, 50m/2050m).
        self.assertEqual(totals["organizationweb"]["limits"]["memory_mib"], 8045)
        self.assertEqual(totals["organizationweb"]["reservations"]["memory_mib"], 3618)
        self.assertEqual(totals["organizationweb"]["limits"]["cpu_millicores"], 14150)
        self.assertEqual(totals["observability"]["limits"]["memory_mib"], 8685)
        self.assertEqual(totals["observability"]["limits"]["cpu_millicores"], 16000)

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
                    self.base, self.profiles, requested, live, parked=self.parked
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
            )

    def test_external_stacks_must_match_their_live_services(self):
        live = [
            {"name": "edge_traefik", "stack": "edge"},
            *external_live(self.profiles),
        ]
        self.module.validate_live(
            self.base, self.profiles, "edge", live, parked=self.parked
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
                    self.base, self.profiles, requested, at_rest, parked=parked
                )
        # Only the playbooks that converge it to 0/0 may start while it runs.
        for requested in ("workloads", "site"):
            profiles = json.loads(json.dumps(self.profiles))
            if requested == "site":
                profiles["capacity_profiles"]["active"] = "observability"
            with self.subTest(requested=requested, state="running"):
                self.module.validate_live(
                    self.base, profiles, requested, running, parked=parked
                )
        for requested in ("edge", "autoupdater", "organizationweb", "racinggame"):
            with self.subTest(requested=requested, state="running"):
                with self.assertRaisesRegex(
                    self.module.capacity.CapacityError,
                    "parked live service runs over the budget: workloads_minecraft",
                ):
                    self.module.validate_live(
                        self.base, self.profiles, requested, running, parked=parked
                    )
        # An unparked service is not checked here: the stack owns its replicas.
        self.module.validate_live(
            self.base, self.profiles, "edge", running, parked=frozenset({"openclaw"})
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
            input=json.dumps([]), text=True, capture_output=True, check=False,
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
            "site",
        ):
            play = yaml.safe_load((ROOT / f"ansible/playbooks/{name}.yml").read_text())[0]
            roles = [item["role"] for item in play["roles"]]
            self.assertLess(roles.index("operation_lock_guard"), roles.index("capacity_preflight"))


if __name__ == "__main__":
    unittest.main()
