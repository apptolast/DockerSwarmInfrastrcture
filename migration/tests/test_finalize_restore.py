from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_ROOT = REPOSITORY_ROOT / "migration"
sys.path.insert(0, str(MIGRATION_ROOT / "scripts"))

import backup_safety  # noqa: E402
import finalize_restore  # noqa: E402


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)


def create_runtime(parent: Path) -> Path:
    services_root = parent / "services"
    services_root.mkdir(mode=0o700)
    for relative in finalize_restore.EXPECTED_RELATIVE_PATHS.values():
        (services_root / relative).mkdir(mode=0o700, parents=True)

    critical_content = {
        "minecraft/data/server_chavalda/level.dat": b"level",
        "minecraft/data/world/level.dat": b"level",
        "minecraft/data/world_berenejena/level.dat": b"level",
        "minecraft/mods/fabric.jar": b"mod",
        "n8n/home/config": b'{"encryptionKey":"fixture"}',
        "n8n/home/binaryData/fixture": b"binary",
        "n8n/postgres/pgdata/PG_VERSION": b"16\n",
        "passbolt/gpg/serverkey_private.asc": b"private",
        "passbolt/jwt/jwt.key": b"jwt",
        "passbolt/postgres/pgdata/PG_VERSION": b"15\n",
        "shlink/postgres/pgdata/PG_VERSION": b"16\n",
    }
    for relative, content in critical_content.items():
        write_private(services_root / relative, content)

    secret_catalog = yaml.safe_load(
        (REPOSITORY_ROOT / "stacks/workloads/secrets.yml").read_text(encoding="utf-8")
    )
    secret_names = []
    for item in secret_catalog["workloads_secrets"]:
        name = item["source_file"]
        secret_names.append(name)
        data = b"required\n" if item["required_nonempty"] else b""
        write_private(services_root / "secrets/files" / name, data)
    write_private(
        services_root / "secrets/files" / finalize_restore.SECRET_IDENTITY_KEY_FILE,
        b"k" * finalize_restore.SECRET_IDENTITY_KEY_SIZE,
    )

    write_private(
        services_root / "runtime-manifest.json",
        (
            json.dumps(
                {
                    "schemaVersion": 4,
                    "sourceBackup": "apptolast-data-20260723T225340Z",
                    "openClawLegacyImported": False,
                    "secretFiles": sorted(secret_names),
                    "secretIdentityKeyFile": (
                        finalize_restore.SECRET_IDENTITY_KEY_FILE
                    ),
                }
            )
            + "\n"
        ).encode(),
    )
    write_private(
        services_root / "recovery/SHA256SUMS",
        f"{'0' * 64}  databases/fixture.dump\n".encode(),
    )
    vector_dumps = {
        "vectors": b"vectors-dump-fixture",
        "rag": b"rag-dump-fixture",
    }
    for database, content in vector_dumps.items():
        write_private(
            services_root / f"recovery/databases/{database}.dump",
            content,
        )
    state_dir = services_root / "restore-state"
    state_dir.mkdir(mode=0o700)
    completed_at = "2026-07-26T12:00:00Z"
    write_private(
        state_dir / "databases-core.json",
        (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "phase": "databases-core",
                    "completedAt": completed_at,
                }
            )
            + "\n"
        ).encode(),
    )
    for phase, (database, version) in (
        ("database-vectors-081", ("vectors", "0.8.1")),
        ("database-rag-082", ("rag", "0.8.2")),
    ):
        write_private(
            state_dir / f"{phase}.json",
            (
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "phase": phase,
                        "database": database,
                        "dumpSha256": hashlib.sha256(
                            vector_dumps[database]
                        ).hexdigest(),
                        "tableCount": 3,
                        "schemaSha256": "a" * 64,
                        "vectorExtensionVersion": version,
                        "completedAt": completed_at,
                    }
                )
                + "\n"
            ).encode(),
        )
    return services_root


class FinalizeRestoreTests(unittest.TestCase):
    def test_database_critical_files_follow_pgdata_contract(self) -> None:
        self.assertEqual(
            {
                identifier: finalize_restore.CRITICAL_FILES[identifier]
                for identifier in (
                    "n8n-postgres",
                    "passbolt-postgres",
                    "shlink-postgres",
                )
            },
            {
                "n8n-postgres": ("pgdata/PG_VERSION",),
                "passbolt-postgres": ("pgdata/PG_VERSION",),
                "shlink-postgres": ("pgdata/PG_VERSION",),
            },
        )

    def test_writes_exact_gate_atomically_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            services_root = create_runtime(Path(temporary))
            marker = finalize_restore.write_ready_marker(
                services_root=services_root,
                canonical_services_root=Path("/srv/dockerswarm/services"),
                catalog_path=REPOSITORY_ROOT / "config/services.yml",
                secret_catalog_path=(REPOSITORY_ROOT / "stacks/workloads/secrets.yml"),
                enforce_owners=False,
            )
            document = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(document["schemaVersion"], 2)
            self.assertEqual(document["runtimeManifestSchemaVersion"], 4)
            self.assertRegex(
                document["secretIdentityKeySha256"],
                r"^[a-f0-9]{64}$",
            )
            self.assertEqual(
                set(document["approvedServices"]),
                finalize_restore.EXPECTED_SERVICES,
            )
            self.assertEqual(
                document["datasets"],
                finalize_restore.DATASET_STATUSES,
            )
            self.assertFalse(document["openClawLegacyImported"])
            self.assertRegex(
                document["sourceBackupSha256"],
                r"^[a-f0-9]{64}$",
            )
            if os.name == "posix":
                self.assertEqual(marker.stat().st_mode & 0o777, 0o600)

            with self.assertRaises(backup_safety.SafetyError):
                finalize_restore.write_ready_marker(
                    services_root=services_root,
                    canonical_services_root=Path("/srv/dockerswarm/services"),
                    catalog_path=REPOSITORY_ROOT / "config/services.yml",
                    secret_catalog_path=(
                        REPOSITORY_ROOT / "stacks/workloads/secrets.yml"
                    ),
                    enforce_owners=False,
                )

    def test_rejects_stale_vector_restore_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            services_root = create_runtime(Path(temporary))
            marker = services_root / "restore-state/database-vectors-081.json"
            document = json.loads(marker.read_text(encoding="utf-8"))
            document["dumpSha256"] = "f" * 64
            write_private(
                marker,
                (json.dumps(document) + "\n").encode(),
            )
            with self.assertRaises(backup_safety.SafetyError):
                finalize_restore.write_ready_marker(
                    services_root=services_root,
                    canonical_services_root=Path("/srv/dockerswarm/services"),
                    catalog_path=REPOSITORY_ROOT / "config/services.yml",
                    secret_catalog_path=(
                        REPOSITORY_ROOT / "stacks/workloads/secrets.yml"
                    ),
                    enforce_owners=False,
                )

    def test_refuses_nonempty_clean_openclaw_before_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            services_root = create_runtime(Path(temporary))
            write_private(
                services_root / "openclaw-clean/home/legacy-state",
                b"forbidden",
            )
            with self.assertRaises(backup_safety.SafetyError):
                finalize_restore.write_ready_marker(
                    services_root=services_root,
                    canonical_services_root=Path("/srv/dockerswarm/services"),
                    catalog_path=REPOSITORY_ROOT / "config/services.yml",
                    secret_catalog_path=(
                        REPOSITORY_ROOT / "stacks/workloads/secrets.yml"
                    ),
                    enforce_owners=False,
                )
            self.assertFalse(
                (services_root / "restore-state/workloads-ready-v2.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
