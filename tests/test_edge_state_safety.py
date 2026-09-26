"""Static safety tests for Traefik's writable ACME state."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EDGE_DEPLOY = (
    PROJECT_ROOT / "ansible/roles/edge/tasks/deploy.yml"
)
STAGING_ROOTS = (
    PROJECT_ROOT
    / "ansible/roles/edge/files/letsencrypt-staging-roots.pem"
)
EXPECTED_STAGING_ROOTS_SHA256 = (
    "0b549d3d047d7ab477a78f4c27bb1b2a"
    "15f5766619898ee8f21b7939566339a4"
)


class EdgeStateSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tasks = yaml.safe_load(EDGE_DEPLOY.read_text(encoding="utf-8"))
        flattened = []

        def visit(tasks: list[dict[str, object]]) -> None:
            for task in tasks:
                flattened.append(task)
                for section in ("block", "rescue", "always"):
                    nested = task.get(section)
                    if isinstance(nested, list):
                        visit(nested)

        visit(cls.tasks)
        cls.by_name = {task["name"]: task for task in flattened}

    def test_state_directory_is_checked_before_reconciliation(self) -> None:
        names = [task["name"] for task in self.tasks]
        inspect_index = names.index(
            "Inspect the Traefik persistent-state directory without following links"
        )
        reject_index = names.index(
            "Reject an unsafe Traefik persistent-state path"
        )
        create_index = names.index("Create Traefik persistent state")
        self.assertLess(inspect_index, reject_index)
        self.assertLess(reject_index, create_index)
        inspect = self.by_name[names[inspect_index]]["ansible.builtin.stat"]
        self.assertFalse(inspect["follow"])

    def test_runtime_cannot_replace_acme_directory_entries(self) -> None:
        directory = self.by_name["Create Traefik persistent state"][
            "ansible.builtin.file"
        ]
        self.assertEqual(directory["owner"], "root")
        self.assertEqual(directory["mode"], "0750")
        self.assertEqual(
            directory["group"],
            "{{ edge_traefik_runtime_gid }}",
        )

    def test_existing_acme_file_is_never_followed_or_repaired(self) -> None:
        inspect = self.by_name["Inspect ACME storage without following links"][
            "ansible.builtin.stat"
        ]
        self.assertFalse(inspect["follow"])
        assertion = " ".join(
            self.by_name["Reject unsafe or drifted existing ACME storage"][
                "ansible.builtin.assert"
            ]["that"]
        )
        for invariant in ("isreg", "islnk", "nlink", "uid", "gid", "0600"):
            self.assertIn(invariant, assertion)

        create = self.by_name["Create the ACME storage file"]
        self.assertFalse(create["ansible.builtin.file"]["follow"])
        self.assertIn("not edge_traefik_acme_storage.stat.exists", create["when"])

    def test_staging_uses_only_the_isolated_pinned_trust_bundle(self) -> None:
        self.assertEqual(
            hashlib.sha256(STAGING_ROOTS.read_bytes()).hexdigest(),
            EXPECTED_STAGING_ROOTS_SHA256,
        )
        content = STAGING_ROOTS.read_text(encoding="ascii")
        self.assertEqual(content.count("-----BEGIN CERTIFICATE-----"), 4)
        probe = self.by_name["Prove the forced local HTTPS health route"][
            "ansible.builtin.command"
        ]["argv"]
        self.assertEqual(probe, "{{ edge_traefik_https_probe_argv }}")
        derived = self.by_name[
            "Derive the profile-aware HTTPS health command"
        ]["ansible.builtin.set_fact"]["edge_traefik_https_probe_argv"]
        self.assertIn("--cacert", derived)
        self.assertIn('edge_deployment_profile == "acme-staging"', derived)
        self.assertNotIn("--insecure", derived)

    def test_deployment_prunes_and_gates_exact_service_inventory(self) -> None:
        deployment = self.by_name["Deploy the edge stack"][
            "community.docker.docker_stack"
        ]
        self.assertTrue(deployment["prune"])
        convergence = self.by_name[
            "Wait for the exact edge service inventory to converge"
        ]
        command = convergence["ansible.builtin.command"]["argv"]
        self.assertEqual(
            command[1:4],
            ["stack", "services", "{{ edge_traefik_stack_name }}"],
        )
        self.assertIn(
            '== [edge_traefik_stack_name ~ "_traefik=1/1"]',
            " ".join(convergence["until"]),
        )


if __name__ == "__main__":
    unittest.main()


class EdgeUpdateWaitTests(unittest.TestCase):
    """The runtime gate must not pick the task that stop-first is replacing."""

    @classmethod
    def setUpClass(cls) -> None:
        tasks = yaml.safe_load(EDGE_DEPLOY.read_text(encoding="utf-8"))
        cls.names = [task["name"] for task in tasks]
        cls.by_name = {task["name"]: task for task in tasks}
        read = cls.by_name["Read the Traefik update state"]
        cls.read = read
        import jinja2

        env = jinja2.Environment()
        env.filters["from_json"] = json.loads

        def default(value, fallback, boolean=False):
            return fallback if (not value if boolean else value is None) else value

        env.filters["default"] = default
        cls.until = env.compile_expression(read["until"].strip())

    def proceeds(self, stdout: str) -> bool:
        return bool(self.until(edge_traefik_update_state={"stdout": stdout}))

    def test_the_gate_waits_while_swarm_replaces_the_task(self) -> None:
        self.assertFalse(self.proceeds('{"State": "updating"}'))
        self.assertFalse(self.proceeds('{"State": "rollback_started"}'))

    def test_the_gate_proceeds_when_nothing_is_being_replaced(self) -> None:
        # An unchanged spec leaves UpdateStatus null (controlapi resets it).
        self.assertTrue(self.proceeds("null"))
        self.assertTrue(self.proceeds('{"State": "completed"}'))
        # Rolled back or paused: stop waiting so the next task rejects it.
        for state in ("rollback_completed", "rollback_paused", "paused"):
            with self.subTest(state=state):
                self.assertTrue(self.proceeds(json.dumps({"State": state})))

    def test_the_wait_outlasts_stop_first_start_and_monitor(self) -> None:
        # graceTimeOut 10 s, health start period 15 s, monitor 90 s.
        self.assertGreaterEqual(
            self.read["retries"] * self.read["delay"], 10 + 15 + 90 + 60
        )

    def test_the_wait_precedes_the_rollback_rejection_and_the_gate(self) -> None:
        wait = self.names.index("Read the Traefik update state")
        reject = self.names.index("Reject a Traefik update that rolled back or paused")
        gate = self.names.index(
            "Prove Traefik runtime health before declaring edge deployed"
        )
        self.assertLess(wait, reject)
        self.assertLess(reject, gate)
