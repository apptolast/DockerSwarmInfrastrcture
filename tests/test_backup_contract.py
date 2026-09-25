"""Static integration contract for backup IaC."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANSIBLE_ENVIRONMENT_CONFIG = PROJECT_ROOT / "ansible/ansible.cfg"
BACKUP_TASKS = "ansible/roles/backup/tasks/main.yml"
PARKED_GATE = "Reject parked workloads outside the reviewed backup contract"
PARKED_GATE_MESSAGE = (
    "platform_parked_workloads must be a sorted, duplicate-free list of "
    "workloads named in backup_parkable_services"
)
PARKING_VARIANTS = {"none": [], "both": ["minecraft", "openclaw"]}


def load_module(name: str, relative_path: str) -> ModuleType:
    """Import a module by path under a private name.

    scripts/backup-self-test.sh runs `python3 -m unittest tests.<module>`,
    which puts no tests/ directory on sys.path, so sibling helpers cannot be
    imported by name as they are under `unittest discover -s tests`.
    """
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


harness = load_module("backup_contract_task_harness", "tests/ansible_task_harness.py")
backupctl = load_module("backup_contract_backupctl", "backup/backupctl.py")


def load_yaml(relative_path: str) -> Any:
    return yaml.safe_load((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))


class BackupContractTests(unittest.TestCase):
    def test_backup_identity_never_depends_on_inventory_alias(self) -> None:
        defaults = (
            PROJECT_ROOT / "ansible/roles/backup/defaults/main.yml"
        ).read_text(encoding="utf-8")
        platform = yaml.safe_load(
            (PROJECT_ROOT / "config/platform.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn('backup_hostname: "{{ platform_node_id }}"', defaults)
        self.assertNotIn(
            'backup_hostname: "{{ inventory_hostname }}"',
            defaults,
        )
        self.assertEqual(platform["platform_node_id"], "netcup-manager-01")

    def test_activation_is_blocked_without_external_credentials(self) -> None:
        defaults = yaml.safe_load(
            (
                PROJECT_ROOT
                / "ansible/roles/backup/defaults/main.yml"
            ).read_text(encoding="utf-8")
        )
        self.assertIs(defaults["backup_activation_enabled"], False)
        self.assertEqual(defaults["backup_r2_account_id"], "")
        self.assertEqual(defaults["backup_r2_bucket"], "")
        self.assertEqual(defaults["backup_initialize_repository"], False)

    def test_restic_release_and_binaries_are_cryptographically_locked(self) -> None:
        defaults = yaml.safe_load(
            (
                PROJECT_ROOT
                / "ansible/roles/backup/defaults/main.yml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(defaults["backup_restic_version"], "0.19.1")
        for architecture in ("x86_64", "aarch64"):
            lock = defaults["backup_restic_locks"][architecture]
            self.assertRegex(lock["archive_sha256"], r"^[a-f0-9]{64}$")
            self.assertRegex(lock["binary_sha256"], r"^[a-f0-9]{64}$")

    def test_exact_workload_paths_and_service_names_are_shared(self) -> None:
        defaults = yaml.safe_load(
            (
                PROJECT_ROOT
                / "ansible/roles/backup/defaults/main.yml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            defaults["backup_consistency_groups"],
            [
                {
                    "id": "n8n",
                    "services": [
                        "workloads_n8n",
                        "workloads_n8n-runners",
                    ],
                    "databases": [
                        "n8n-postgres",
                        "n8n-vectors",
                        "n8n-rag",
                    ],
                    "datasets": ["n8n-home"],
                },
                {
                    "id": "passbolt",
                    "services": ["workloads_passbolt"],
                    "databases": ["passbolt-postgres"],
                    "datasets": ["passbolt-gpg", "passbolt-jwt"],
                },
                {
                    "id": "shlink",
                    "services": ["workloads_shlink"],
                    "databases": ["shlink-postgres"],
                    "datasets": [],
                },
                {
                    "id": "openclaw",
                    "services": ["workloads_openclaw"],
                    "databases": [],
                    "datasets": ["openclaw-clean-home"],
                },
                {
                    "id": "traefik",
                    "services": ["edge_traefik"],
                    "databases": [],
                    "datasets": ["traefik-acme"],
                },
                {
                    "id": "observability-prometheus",
                    "services": ["observability_prometheus"],
                    "databases": [],
                    "datasets": ["observability-prometheus"],
                },
                {
                    "id": "observability-alertmanager",
                    "services": ["observability_alertmanager"],
                    "databases": [],
                    "datasets": ["observability-alertmanager"],
                },
                {
                    "id": "observability-loki",
                    "services": ["observability_loki"],
                    "databases": [],
                    "datasets": ["observability-loki"],
                },
                {
                    "id": "observability-grafana",
                    "services": ["observability_grafana"],
                    "databases": [],
                    "datasets": ["observability-grafana"],
                },
                {
                    "id": "immutable-secrets",
                    "services": [],
                    "databases": [],
                    "datasets": [
                        "runtime-secret-source",
                        "observability-secret-source",
                    ],
                },
            ],
        )
        self.assertEqual(
            [
                (
                    value["id"],
                    value["service"],
                    value["database"],
                    value["rehearsal_image"],
                    value["extensions"],
                )
                for value in defaults["backup_databases"]
            ],
            [
                (
                    "n8n-postgres",
                    "workloads_n8n-db",
                    "n8n",
                    (
                        "docker.io/pgvector/pgvector@sha256:"
                        "7d400e340efb42f4d8c9c12c6427adb253f726881a9985d2"
                        "a471bf0eed824dff"
                    ),
                    {},
                ),
                (
                    "n8n-vectors",
                    "workloads_n8n-db",
                    "vectors",
                    (
                        "docker.io/pgvector/pgvector@sha256:"
                        "33198da2828a14c30348d2ccb4750833d5ed9a44c88d840a"
                        "0e523d7417120337"
                    ),
                    {"vector": "0.8.1"},
                ),
                (
                    "n8n-rag",
                    "workloads_n8n-db",
                    "rag",
                    (
                        "docker.io/pgvector/pgvector@sha256:"
                        "7d400e340efb42f4d8c9c12c6427adb253f726881a9985d2"
                        "a471bf0eed824dff"
                    ),
                    {"vector": "0.8.2"},
                ),
                (
                    "passbolt-postgres",
                    "workloads_passbolt-db",
                    "passbolt",
                    (
                        "docker.io/library/postgres@sha256:"
                        "fceb6f86328c36f2438fae3b851b0cc57c4a7e69a58c866d"
                        "9ce24281f2cf0c9c"
                    ),
                    {},
                ),
                (
                    "shlink-postgres",
                    "workloads_shlink-db",
                    "shlink",
                    (
                        "docker.io/library/postgres@sha256:"
                        "66266770619a23ab310c7fa60043b6d1fa041038cb232ced5"
                        "9d2c509fecd297b"
                    ),
                    {},
                ),
            ],
        )
        self.assertEqual(
            {value["id"] for value in defaults["backup_datasets"]},
            {
                "n8n-home",
                "passbolt-gpg",
                "passbolt-jwt",
                "minecraft-data",
                "minecraft-mods",
                "openclaw-clean-home",
                "traefik-acme",
                "runtime-secret-source",
                "observability-secret-source",
                "observability-prometheus",
                "observability-alertmanager",
                "observability-loki",
                "observability-grafana",
            },
        )

    def test_parked_workloads_share_one_reviewed_parkable_set(self) -> None:
        defaults = load_yaml("ansible/roles/backup/defaults/main.yml")
        parkable = defaults["backup_parkable_services"]
        self.assertEqual(
            parkable,
            {
                "minecraft": "workloads_minecraft",
                "openclaw": "workloads_openclaw",
            },
        )
        # The role renders exactly the Swarm names the controller may park.
        self.assertEqual(frozenset(parkable.values()), backupctl.PARKABLE_SERVICES)
        self.assertEqual(parkable["minecraft"], defaults["backup_minecraft_service"])
        openclaw_group = next(
            group
            for group in defaults["backup_consistency_groups"]
            if group["id"] == "openclaw"
        )
        self.assertEqual(openclaw_group["services"], [parkable["openclaw"]])

        # The gate runs before the role renders or installs anything.
        tasks = [task["name"] for task in load_yaml(BACKUP_TASKS)]
        self.assertEqual(
            tasks.index(PARKED_GATE),
            tasks.index("Reject activation without reviewed backup inputs") + 1,
        )
        self.assertLess(
            tasks.index(PARKED_GATE),
            tasks.index("Render the credential-free backup configuration"),
        )

    def test_swarm_state_unit_requires_explicit_stop_acknowledgement(self) -> None:
        wrapper = (
            PROJECT_ROOT / "scripts/backup-swarm-state.sh"
        ).read_text(encoding="utf-8")
        controller = (
            PROJECT_ROOT / "backup/backupctl.py"
        ).read_text(encoding="utf-8")
        self.assertIn("--confirm-docker-stop", wrapper)
        self.assertIn('Path("/var/lib/docker/swarm")', controller)
        self.assertIn('"unlock-key", "-q"', controller)
        self.assertIn('"dockerswarm-swarm-unlock.service"', controller)
        self.assertNotIn('"swarm", "unlock"', controller)

    def test_swarm_metadata_uses_supported_docker_info_contract(self) -> None:
        controller = (
            PROJECT_ROOT / "backup/backupctl.py"
        ).read_text(encoding="utf-8")
        provisioner = (
            PROJECT_ROOT / "scripts/backup-provision-secrets.sh"
        ).read_text(encoding="utf-8")
        self.assertNotIn("docker swarm inspect", provisioner)
        self.assertNotIn('"swarm", "inspect"', controller)
        self.assertIn(
            ".Swarm.Cluster.Spec.EncryptionConfig.AutoLockManagers",
            provisioner,
        )
        self.assertIn(".Swarm.Cluster.ID", controller)

    def test_scheduled_units_share_one_controller_lock(self) -> None:
        defaults = (
            PROJECT_ROOT
            / "ansible/roles/backup/defaults/main.yml"
        ).read_text(encoding="utf-8")
        controller = (
            PROJECT_ROOT / "backup/backupctl.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "backup_lock_file: /run/lock/dockerswarm-backup.lock", defaults
        )
        self.assertIn("with exclusive_lock(lock_path):", controller)


class BackupParkingGateTests(harness.AnsibleTaskAssertions, unittest.TestCase):
    """The real role assert, run by Ansible against synthetic parking lists."""

    def variables(self, parked: Any) -> dict[str, Any]:
        return {
            "platform_parked_workloads": parked,
            "backup_parkable_services": load_yaml(
                "ansible/roles/backup/defaults/main.yml"
            )["backup_parkable_services"],
        }

    def test_reviewed_parking_lists_are_accepted(self) -> None:
        reviewed = load_yaml("config/platform.yml")["platform_parked_workloads"]
        for parked in ([], ["minecraft"], ["openclaw"], ["minecraft", "openclaw"]):
            with self.subTest(parked=parked):
                self.assert_task_accepts(
                    BACKUP_TASKS, PARKED_GATE, self.variables(parked)
                )
        with self.subTest(parked="config/platform.yml"):
            self.assert_task_accepts(
                BACKUP_TASKS, PARKED_GATE, self.variables(reviewed)
            )

    def test_parking_lists_outside_the_contract_are_rejected(self) -> None:
        for parked in (
            ["n8n"],
            ["openclaw", "minecraft"],
            ["minecraft", "minecraft"],
            "minecraft",
            {"minecraft": True},
            [1],
        ):
            with self.subTest(parked=parked):
                self.assert_task_rejects(
                    BACKUP_TASKS,
                    PARKED_GATE,
                    self.variables(parked),
                    PARKED_GATE_MESSAGE,
                )


class BackupConfigRenderTests(unittest.TestCase):
    """Render config.json.j2 with real Ansible, then validate it offline."""

    temporary: tempfile.TemporaryDirectory[str]
    configs: dict[str, Path]

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.configs = {}
        defaults = load_yaml("ansible/roles/backup/defaults/main.yml")
        for variant, parked in PARKING_VARIANTS.items():
            root = Path(cls.temporary.name) / variant
            root.mkdir()
            destination = root / "config.json"
            playbook = root / "render.yml"
            playbook.write_text(
                yaml.safe_dump(
                    [
                        {
                            "name": "Render the backup configuration",
                            "hosts": "localhost",
                            "connection": "local",
                            "gather_facts": False,
                            "become": False,
                            # The same inputs ansible/playbooks/backup.yml
                            # loads for the role template.
                            "vars_files": [
                                str(PROJECT_ROOT / "config/platform.yml"),
                                str(
                                    PROJECT_ROOT
                                    / "ansible/roles/backup/defaults/main.yml"
                                ),
                            ],
                            "tasks": [
                                harness.load_task(BACKUP_TASKS, PARKED_GATE),
                                {
                                    "name": "Render the reviewed template",
                                    "ansible.builtin.template": {
                                        "src": str(
                                            PROJECT_ROOT
                                            / "ansible/roles/backup/templates"
                                            / "config.json.j2"
                                        ),
                                        "dest": str(destination),
                                        "mode": "0600",
                                    },
                                },
                            ],
                        }
                    ],
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            # Extra vars only replace what the role derives on a real host
            # (the architecture lock) or what activation supplies (R2).
            extra_variables = {
                "platform_parked_workloads": parked,
                "backup_r2_account_id": "b" * 32,
                "backup_r2_bucket": "apptolast-backups",
                "backup_restic_lock": defaults["backup_restic_locks"]["x86_64"],
            }
            completed = subprocess.run(
                [
                    str(harness.ANSIBLE_PLAYBOOK),
                    "-i",
                    "localhost,",
                    str(playbook),
                    "--extra-vars",
                    json.dumps(extra_variables),
                ],
                cwd=PROJECT_ROOT / "ansible",
                env={
                    "ANSIBLE_CONFIG": str(ANSIBLE_ENVIRONMENT_CONFIG),
                    "PATH": "/usr/bin:/bin",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                cls.temporary.cleanup()
                raise AssertionError(
                    f"backup config render ({variant}) failed:\n"
                    + completed.stdout
                    + completed.stderr
                )
            cls.configs[variant] = destination

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_rendered_configs_pass_controller_validation(self) -> None:
        expected = {
            "none": [],
            "both": ["workloads_minecraft", "workloads_openclaw"],
        }
        for variant, parked_services in expected.items():
            with self.subTest(variant=variant):
                config = backupctl.BackupConfig.load(self.configs[variant])
                self.assertEqual(config.document["parked_services"], parked_services)

    def test_parking_changes_only_the_parked_services_key(self) -> None:
        rendered = {
            variant: json.loads(path.read_text(encoding="utf-8"))
            for variant, path in self.configs.items()
        }
        for document in rendered.values():
            del document["parked_services"]
        self.assertEqual(rendered["none"], rendered["both"])


if __name__ == "__main__":
    unittest.main()
