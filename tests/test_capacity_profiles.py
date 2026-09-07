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
                "organizationweb": yaml.safe_load(template.render(**variables)),
            }

    def test_application_profile_preserves_the_legacy_plan_and_reserves(self):
        totals = self.module.validate_profiles(self.base, self.profiles, self.stacks)
        self.assertEqual(totals["organizationweb"]["limits"]["memory_mib"], 11456)
        self.assertEqual(totals["organizationweb"]["reservations"]["memory_mib"], 6784)
        self.assertEqual(totals["observability"]["limits"]["memory_mib"], 12352)

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
        for name in ("edge", "workloads", "observability", "site"):
            play = yaml.safe_load((ROOT / f"ansible/playbooks/{name}.yml").read_text())[0]
            roles = [item["role"] for item in play["roles"]]
            self.assertLess(roles.index("operation_lock_guard"), roles.index("capacity_preflight"))


if __name__ == "__main__":
    unittest.main()
