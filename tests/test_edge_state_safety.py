"""Static safety tests for Traefik's writable ACME state."""

from __future__ import annotations

import hashlib
from pathlib import Path
import unittest

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
