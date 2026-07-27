from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_ROOT = REPOSITORY_ROOT / "migration"
sys.path.insert(0, str(MIGRATION_ROOT / "scripts"))

import upgrade_secret_identity_contract as upgrade  # noqa: E402


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def private_file(path: Path, data: bytes) -> None:
    private_directory(path.parent)
    path.write_bytes(data)
    path.chmod(0o600)


def encoded(document: dict[str, object]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


class SecretIdentityContractUpgradeTests(unittest.TestCase):
    def create_legacy_runtime(
        self,
        parent: Path,
    ) -> tuple[Path, bytes, bytes, Path]:
        services_root = parent / "services"
        private_directory(services_root)
        private_directory(services_root / "restore-state")
        private_directory(services_root / "secrets/files")
        private_directory(services_root / "recovery")

        service_catalog_path = REPOSITORY_ROOT / "config/services.yml"
        secret_catalog_path = REPOSITORY_ROOT / "stacks/workloads/secrets.yml"
        service_catalog = yaml.safe_load(
            service_catalog_path.read_text(encoding="utf-8")
        )
        secret_catalog = yaml.safe_load(secret_catalog_path.read_text(encoding="utf-8"))
        source_backup = "apptolast-data-20260723T225340Z"
        manifest = {
            "schemaVersion": 3,
            "preparedAt": "2026-07-26T10:00:00+00:00",
            "runtimeRootContract": "/srv/dockerswarm/services",
            "sourceBackup": source_backup,
            "secretDelivery": "individual-docker-secret-source-files",
            "secretFiles": sorted(
                item["source_file"] for item in secret_catalog["workloads_secrets"]
            ),
            "openClawLegacyImported": False,
            "openClawState": "initialized-empty",
            "edgeRecoveryArtifacts": [],
        }
        manifest_data = encoded(manifest)
        private_file(
            services_root / "runtime-manifest.json",
            manifest_data,
        )
        recovery_data = f"{'0' * 64}  databases/n8n.dump\n".encode()
        private_file(
            services_root / "recovery/SHA256SUMS",
            recovery_data,
        )
        approved, datasets = upgrade.expected_scope(service_catalog)
        marker = {
            "schemaVersion": 1,
            "completedAt": "2026-07-26T12:00:00+00:00",
            "catalogVersion": service_catalog["catalog_version"],
            "catalogSha256": hashlib.sha256(
                service_catalog_path.read_bytes()
            ).hexdigest(),
            "runtimeManifestSha256": hashlib.sha256(manifest_data).hexdigest(),
            "sourceBackup": source_backup,
            "sourceBackupSha256": hashlib.sha256(recovery_data).hexdigest(),
            "sourceBackupDigestKind": ("sha256-of-verified-recovery-manifest"),
            "approvedServices": sorted(approved),
            "datasets": datasets,
            "databaseRestores": {
                "n8n": "verified",
                "passbolt": "verified",
                "shlink": "verified",
            },
            "openClawLegacyImported": False,
        }
        marker_data = encoded(marker)
        private_file(
            services_root / "restore-state/workloads-ready-v1.json",
            marker_data,
        )
        application_file = services_root / "n8n/home/config"
        private_file(application_file, b"application-data-must-not-change")
        return services_root, manifest_data, marker_data, application_file

    def apply(self, services_root: Path) -> dict[str, object]:
        return upgrade.run_locked(
            "apply",
            services_root,
            REPOSITORY_ROOT / "config/services.yml",
            REPOSITORY_ROOT / "stacks/workloads/secrets.yml",
        )

    def rollback(self, services_root: Path) -> dict[str, object]:
        return upgrade.run_locked(
            "rollback",
            services_root,
            REPOSITORY_ROOT / "config/services.yml",
            REPOSITORY_ROOT / "stacks/workloads/secrets.yml",
        )

    def test_apply_is_atomic_idempotent_and_preserves_application_data(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, legacy_manifest, legacy_marker, application_file = (
                self.create_legacy_runtime(Path(temporary))
            )
            application_status = application_file.stat()

            result = self.apply(root)
            selected = upgrade.paths(root)
            manifest_v4 = selected["manifest"].read_bytes()
            marker_v2 = selected["current_marker"].read_bytes()
            identity_key = selected["identity_key"].read_bytes()
            evidence = selected["evidence"].read_bytes()

            self.assertEqual(result["action"], "applied")
            self.assertEqual(selected["manifest_v3"].read_bytes(), legacy_manifest)
            self.assertEqual(selected["marker_v1"].read_bytes(), legacy_marker)
            self.assertEqual(len(identity_key), 32)
            self.assertEqual(
                stat.S_IMODE(selected["identity_key"].stat().st_mode),
                0o600,
            )
            self.assertEqual(
                json.loads(manifest_v4)["secretIdentityKeyFile"],
                upgrade.IDENTITY_KEY_FILE,
            )
            marker = json.loads(marker_v2)
            self.assertEqual(marker["schemaVersion"], 2)
            self.assertEqual(marker["runtimeManifestSchemaVersion"], 4)
            self.assertEqual(
                marker["runtimeManifestSha256"],
                hashlib.sha256(manifest_v4).hexdigest(),
            )
            self.assertEqual(
                marker["secretIdentityKeySha256"],
                hashlib.sha256(identity_key).hexdigest(),
            )

            second_result = self.apply(root)
            self.assertEqual(second_result["action"], "applied")
            self.assertEqual(selected["manifest"].read_bytes(), manifest_v4)
            self.assertEqual(selected["current_marker"].read_bytes(), marker_v2)
            self.assertEqual(selected["identity_key"].read_bytes(), identity_key)
            self.assertEqual(selected["evidence"].read_bytes(), evidence)
            self.assertEqual(
                application_file.read_bytes(),
                b"application-data-must-not-change",
            )
            self.assertEqual(application_file.stat().st_ino, application_status.st_ino)
            self.assertEqual(
                stat.S_IMODE(application_file.stat().st_mode),
                stat.S_IMODE(application_status.st_mode),
            )

    def test_rollback_restores_exact_v3_and_reapply_is_deterministic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, legacy_manifest, legacy_marker, application_file = (
                self.create_legacy_runtime(Path(temporary))
            )
            selected = upgrade.paths(root)
            self.apply(root)
            manifest_v4 = selected["manifest"].read_bytes()
            marker_v2 = selected["current_marker"].read_bytes()
            identity_key = selected["identity_key"].read_bytes()

            result = self.rollback(root)
            self.assertEqual(result["action"], "rolled-back")
            self.assertEqual(selected["manifest"].read_bytes(), legacy_manifest)
            self.assertEqual(
                selected["legacy_marker"].read_bytes(),
                legacy_marker,
            )
            self.assertEqual(selected["manifest_v4"].read_bytes(), manifest_v4)
            self.assertEqual(selected["current_marker"].read_bytes(), marker_v2)
            self.assertEqual(selected["identity_key"].read_bytes(), identity_key)

            self.apply(root)
            self.assertEqual(selected["manifest"].read_bytes(), manifest_v4)
            self.assertEqual(selected["current_marker"].read_bytes(), marker_v2)
            self.assertEqual(selected["identity_key"].read_bytes(), identity_key)
            self.assertEqual(
                application_file.read_bytes(),
                b"application-data-must-not-change",
            )

    def test_apply_resumes_after_manifest_replacement_before_v2_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, legacy_manifest, _, _ = self.create_legacy_runtime(Path(temporary))
            selected = upgrade.paths(root)
            upgrade.ensure_private_directory(
                selected["compatibility"],
                canonical=False,
            )
            upgrade.write_exclusive(
                selected["manifest_v3"],
                legacy_manifest,
                canonical=False,
            )
            legacy_document = json.loads(legacy_manifest)
            private_file(
                selected["identity_key"],
                b"k" * upgrade.IDENTITY_KEY_SIZE,
            )
            upgrade.atomic_replace(
                selected["manifest"],
                upgrade.current_manifest_bytes(legacy_document),
            )

            self.apply(root)
            self.assertTrue(selected["current_marker"].is_file())
            self.assertTrue(selected["evidence"].is_file())
            self.assertEqual(
                json.loads(selected["manifest"].read_bytes())["schemaVersion"],
                4,
            )

    def test_tampering_refuses_rollback_without_overwriting_runtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _, _, _ = self.create_legacy_runtime(Path(temporary))
            selected = upgrade.paths(root)
            self.apply(root)
            tampered = selected["manifest"].read_bytes() + b" "
            private_file(selected["manifest"], tampered)

            with self.assertRaisesRegex(
                upgrade.UpgradeError,
                "current runtime manifest differs",
            ):
                self.rollback(root)
            self.assertEqual(selected["manifest"].read_bytes(), tampered)

    def test_tampered_identity_key_refuses_idempotent_apply(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _, _, _ = self.create_legacy_runtime(Path(temporary))
            selected = upgrade.paths(root)
            self.apply(root)
            private_file(
                selected["identity_key"],
                b"x" * upgrade.IDENTITY_KEY_SIZE,
            )

            with self.assertRaisesRegex(
                upgrade.UpgradeError,
                "current restore marker is stale",
            ):
                self.apply(root)

    def test_invalid_legacy_evidence_causes_no_upgrade_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, legacy_manifest, _, application_file = self.create_legacy_runtime(
                Path(temporary)
            )
            selected = upgrade.paths(root)
            marker = json.loads(selected["legacy_marker"].read_bytes())
            marker["runtimeManifestSha256"] = "f" * 64
            private_file(selected["legacy_marker"], encoded(marker))

            with self.assertRaisesRegex(
                upgrade.UpgradeError,
                "legacy restore evidence is stale",
            ):
                self.apply(root)
            self.assertEqual(selected["manifest"].read_bytes(), legacy_manifest)
            self.assertFalse(selected["compatibility"].exists())
            self.assertFalse(selected["identity_key"].exists())
            self.assertFalse(selected["current_marker"].exists())
            self.assertEqual(
                application_file.read_bytes(),
                b"application-data-must-not-change",
            )

    def test_unsafe_runtime_directory_permissions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, legacy_manifest, _, _ = self.create_legacy_runtime(Path(temporary))
            root.chmod(0o755)
            with self.assertRaisesRegex(
                upgrade.UpgradeError,
                "directory must be mode 0700",
            ):
                self.apply(root)
            self.assertEqual(
                (root / "runtime-manifest.json").read_bytes(),
                legacy_manifest,
            )


if __name__ == "__main__":
    unittest.main()
