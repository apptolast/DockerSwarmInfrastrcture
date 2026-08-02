"""Static integration contract for backup IaC."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


if __name__ == "__main__":
    unittest.main()
