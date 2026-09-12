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


class CapacityProfileTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "capacity_profiles", ROOT / "scripts/validate-capacity-profiles.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        variables = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())
        template = jinja2.Environment(
            loader=jinja2.FileSystemLoader(ROOT / "stacks/organizationweb"),
            undefined=jinja2.StrictUndefined,
        ).get_template("stack.yml.j2")
        self.base = yaml.safe_load((ROOT / "config/capacity.yml").read_text())
        self.profiles = yaml.safe_load((ROOT / "config/capacity-profiles.yml").read_text())
        self.stacks = {
                **{
                    name: yaml.safe_load((ROOT / f".build/{name}/stack.yml").read_text())
                    for name in ("edge", "workloads", "observability")
                },
                "organizationweb": yaml.safe_load(template.render(**variables, image_channels_map=load_image_channels_map())),
                "autoupdater": load_autoupdater_stack(),
            }

    def test_application_profile_preserves_the_legacy_plan_and_reserves(self):
        totals = self.module.validate_profiles(self.base, self.profiles, self.stacks)
        self.assertEqual(totals["organizationweb"]["limits"]["memory_mib"], 11501)
        self.assertEqual(totals["organizationweb"]["reservations"]["memory_mib"], 6802)
        self.assertEqual(totals["organizationweb"]["limits"]["cpu_millicores"], 14600)
        self.assertEqual(totals["observability"]["limits"]["memory_mib"], 12397)
        self.assertEqual(totals["observability"]["limits"]["cpu_millicores"], 17450)

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
            {"name": "autoupdater_shepherd", "stack": "autoupdater"},
        ]
        for requested in ("autoupdater", "organizationweb", "edge", "workloads"):
            with self.subTest(requested=requested):
                self.module.validate_live(self.base, self.profiles, requested, live)
        for service in (
            {"name": "autoupdater_gantry", "stack": "autoupdater"},
            {"name": "autoupdater_shepherd", "stack": None},
            {"name": "shepherd", "stack": None},
        ):
            with self.subTest(service=service):
                with self.assertRaisesRegex(self.module.capacity.CapacityError, "live"):
                    self.module.validate_live(
                        self.base, self.profiles, "autoupdater", [*live, service]
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
                        [{"name": f"{other}_{service}", "stack": other}],
                    )

    def test_live_name_must_belong_to_its_claimed_stack(self):
        with self.assertRaisesRegex(self.module.capacity.CapacityError, "live"):
            self.module.validate_live(
                self.base, self.profiles, "organizationweb",
                [{"name": "workloads_kropia", "stack": "edge"}],
            )

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
        for name in ("edge", "workloads", "observability", "autoupdater", "site"):
            play = yaml.safe_load((ROOT / f"ansible/playbooks/{name}.yml").read_text())[0]
            roles = [item["role"] for item in play["roles"]]
            self.assertLess(roles.index("operation_lock_guard"), roles.index("capacity_preflight"))


if __name__ == "__main__":
    unittest.main()
