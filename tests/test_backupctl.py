"""Offline safety tests for the backup and recovery controller."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "backup" / "backupctl.py"
SPEC = importlib.util.spec_from_file_location("backupctl", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load backup controller")
backupctl = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backupctl
SPEC.loader.exec_module(backupctl)


def configuration(base: Path) -> dict[str, object]:
    datasets = [
        ("n8n-home", "quiesced"),
        ("passbolt-gpg", "quiesced"),
        ("passbolt-jwt", "quiesced"),
        ("minecraft-data", "minecraft-save"),
        ("minecraft-mods", "minecraft-save"),
        ("openclaw-clean-home", "quiesced"),
        ("traefik-acme", "quiesced"),
        ("runtime-secret-source", "immutable"),
        ("observability-secret-source", "immutable"),
        ("observability-prometheus", "quiesced"),
        ("observability-alertmanager", "quiesced"),
        ("observability-loki", "quiesced"),
        ("observability-grafana", "quiesced"),
    ]
    return {
        "schema_version": 1,
        "restic": {
            "version": "0.19.1",
            "binary": "/usr/local/bin/restic",
            "binary_sha256": "a" * 64,
            "repository": (
                "s3:https://"
                + "b" * 32
                + ".r2.cloudflarestorage.com/apptolast-backups/"
                "dockerswarm/restic"
            ),
            "password_file": "/etc/dockerswarm/backup/restic-password",
            "access_key_id_file": "/etc/dockerswarm/backup/r2-access-key-id",
            "secret_access_key_file": ("/etc/dockerswarm/backup/r2-secret-access-key"),
            "cache_directory": str(base / "cache"),
        },
        "runtime": {
            "hostname": "swarm-manager-01",
            "lock_file": str(base / "backup.lock"),
            "staging_directory": str(base / "staging"),
            "status_directory": str(base / "status"),
            "metrics_directory": str(base / "metrics"),
        },
        "services": {
            "minecraft": "workloads_minecraft",
        },
        "parked_services": [],
        "databases": [
            {
                "id": "n8n-postgres",
                "service": "workloads_n8n-db",
                "database": "n8n",
                "username": "n8n",
                "rehearsal_image": (
                    "docker.io/pgvector/pgvector@sha256:"
                    "7d400e340efb42f4d8c9c12c6427adb253f726881a9985d2"
                    "a471bf0eed824dff"
                ),
                "extensions": {},
            },
            {
                "id": "n8n-vectors",
                "service": "workloads_n8n-db",
                "database": "vectors",
                "username": "n8n",
                "rehearsal_image": (
                    "docker.io/pgvector/pgvector@sha256:"
                    "33198da2828a14c30348d2ccb4750833d5ed9a44c88d840a"
                    "0e523d7417120337"
                ),
                "extensions": {"vector": "0.8.1"},
            },
            {
                "id": "n8n-rag",
                "service": "workloads_n8n-db",
                "database": "rag",
                "username": "n8n",
                "rehearsal_image": (
                    "docker.io/pgvector/pgvector@sha256:"
                    "7d400e340efb42f4d8c9c12c6427adb253f726881a9985d2"
                    "a471bf0eed824dff"
                ),
                "extensions": {"vector": "0.8.2"},
            },
            {
                "id": "passbolt-postgres",
                "service": "workloads_passbolt-db",
                "database": "passbolt",
                "username": "passbolt",
                "rehearsal_image": (
                    "docker.io/library/postgres@sha256:"
                    "fceb6f86328c36f2438fae3b851b0cc57c4a7e69a58c866d"
                    "9ce24281f2cf0c9c"
                ),
                "extensions": {},
            },
            {
                "id": "shlink-postgres",
                "service": "workloads_shlink-db",
                "database": "shlink",
                "username": "shlink",
                "rehearsal_image": (
                    "docker.io/library/postgres@sha256:"
                    "66266770619a23ab310c7fa60043b6d1fa041038cb232ced5"
                    "9d2c509fecd297b"
                ),
                "extensions": {},
            },
        ],
        "datasets": [
            {
                "id": identifier,
                "source": str(base / "source" / identifier),
                "consistency": consistency,
            }
            for identifier, consistency in datasets
        ],
        "consistency_groups": [
            {
                "id": "n8n",
                "services": ["workloads_n8n", "workloads_n8n-runners"],
                "databases": ["n8n-postgres", "n8n-vectors", "n8n-rag"],
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
        "retention": {
            "hourly": 24,
            "daily": 14,
            "weekly": 8,
            "monthly": 12,
            "yearly": 5,
        },
        "swarm_state": {
            "source": "/var/lib/docker/swarm",
            "unlock_key_file": "/etc/dockerswarm/backup/swarm-unlock-key",
            "require_autolock": True,
            "rehearsal_tmpfs_size": "8G",
        },
    }


def write_config(base: Path, document: dict[str, object]) -> Path:
    path = base / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


PARKED_SERVICES = ["workloads_minecraft", "workloads_openclaw"]
IMAGE_DIGEST = "a" * 64


def application_manifest(
    document: dict[str, object],
    *,
    at_rest: frozenset[str] = frozenset(),
    protocol: str = backupctl.MINECRAFT_RCON_PROTOCOL,
) -> dict[str, object]:
    """Build the manifest a successful application backup would record."""

    groups = document["consistency_groups"]
    group_of = {
        identifier: group["id"]
        for group in groups
        for identifier in [*group["databases"], *group["datasets"]]
    }
    artifacts: list[dict[str, object]] = [
        {
            "id": database["id"],
            "type": "postgres-custom-dump",
            "service": database["service"],
            "database": database["database"],
            "image": f"registry.invalid/{database['service']}@sha256:{IMAGE_DIGEST}",
            "rehearsal_image": database["rehearsal_image"],
            "extensions": database["extensions"],
            "consistency_group": group_of[database["id"]],
        }
        for database in document["databases"]
    ]
    artifacts.extend(
        {
            "id": dataset["id"],
            "type": "filesystem-tar",
            "source": dataset["source"],
            "consistency_group": group_of.get(dataset["id"], "minecraft-rcon"),
        }
        for dataset in document["datasets"]
    )
    services = [service for group in groups for service in group["services"]]
    services.append(document["services"]["minecraft"])
    services.extend(database["service"] for database in document["databases"])
    return {
        "schema_version": 1,
        "kind": "application",
        "artifacts": artifacts,
        "metadata": {
            "consistency_groups": [
                {
                    "id": group["id"],
                    "services": group["services"],
                    "artifacts": [*group["databases"], *group["datasets"]],
                    "started_at": "2026-09-25T02:15:00Z",
                    "finished_at": "2026-09-25T02:16:00Z",
                }
                for group in groups
            ],
            "minecraft_consistency": {
                "service": document["services"]["minecraft"],
                "artifacts": ["minecraft-data", "minecraft-mods"],
                "protocol": protocol,
            },
            "services": {
                service: {
                    "desired_replicas": 0 if service in at_rest else 1,
                    "running_replicas": 0 if service in at_rest else 1,
                    "image": f"registry.invalid/{service}@sha256:{IMAGE_DIGEST}",
                }
                for service in dict.fromkeys(services)
            },
        },
    }


class FakeReplicaSwarm:
    """Swarm double whose replica counts follow the scale calls it receives."""

    docker = "/usr/bin/docker"

    def __init__(self, replicas: dict[str, tuple[int, int]]) -> None:
        self.state = dict(replicas)
        self.scaled: list[tuple[str, int]] = []

    def assert_manager(self) -> None:
        return None

    def replicas(self, service: str) -> tuple[int, int]:
        return self.state[service]

    def scale(self, service: str, replicas: int) -> None:
        self.scaled.append((service, replicas))
        self.state[service] = (replicas, replicas)

    def service_image(self, service: str) -> str:
        return f"registry.invalid/{service}@sha256:{IMAGE_DIGEST}"

    def one_container(self, service: str) -> str:
        raise AssertionError(f"a parked service has no container: {service}")


def reviewed_replicas(document: dict[str, object]) -> dict[str, tuple[int, int]]:
    """Every managed service at rest if parked, fully available otherwise."""

    services = {
        *(
            service
            for group in document["consistency_groups"]
            for service in group["services"]
        ),
        document["services"]["minecraft"],
        *(database["service"] for database in document["databases"]),
    }
    return {
        service: (0, 0) if service in document["parked_services"] else (1, 1)
        for service in services
    }


def contract_database_artifact(
    _swarm: object,
    database: dict[str, object],
    destination: Path,
) -> dict[str, object]:
    """dump_database stand-in returning the reviewed artifact contract."""

    return {
        "id": database["id"],
        "type": "postgres-custom-dump",
        "service": database["service"],
        "database": database["database"],
        "image": f"registry.invalid/{database['service']}@sha256:{IMAGE_DIGEST}",
        "rehearsal_image": database["rehearsal_image"],
        "extensions": database["extensions"],
        "file": f"databases/{destination.name}",
    }


def contract_filesystem_artifact(
    identifier: str, source: Path, destination: Path
) -> dict[str, object]:
    """archive_source stand-in returning the reviewed artifact contract."""

    return {
        "id": identifier,
        "type": "filesystem-tar",
        "source": str(source),
        "file": f"filesystems/{destination.name}",
    }


class ManifestRepository:
    """Repository double that keeps the manifest it was asked to upload."""

    def __init__(self) -> None:
        self.manifest: dict[str, object] | None = None

    def check_exists(self) -> None:
        return None

    def backup(self, source: Path, _tag: str, _hostname: str) -> str:
        self.manifest = json.loads(
            (source / "manifest.json").read_text(encoding="utf-8")
        )
        return "c" * 64

    def apply_retention(self, _tag: str, _hostname: str) -> None:
        return None


class ConfigurationTests(unittest.TestCase):
    def test_reviewed_configuration_is_valid_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = backupctl.BackupConfig.load(
                write_config(base, configuration(base))
            )
            self.assertEqual(config.document["schema_version"], 1)

    def test_credentials_in_repository_url_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            document = configuration(base)
            document["restic"][
                "repository"
            ] = "s3:https://key:secret@example.invalid/bucket"
            with self.assertRaisesRegex(
                backupctl.BackupError, "credential-free Cloudflare R2"
            ):
                backupctl.BackupConfig.load(write_config(base, document))

    def test_dataset_allowlist_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            document = configuration(base)
            document["datasets"].pop()
            with self.assertRaisesRegex(backupctl.BackupError, "dataset allowlist"):
                backupctl.BackupConfig.load(write_config(base, document))

    def test_swarm_autolock_cannot_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            document = configuration(base)
            document["swarm_state"]["require_autolock"] = False
            with self.assertRaisesRegex(backupctl.BackupError, "autolock"):
                backupctl.BackupConfig.load(write_config(base, document))

    def test_all_five_logical_databases_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            document = configuration(base)
            document["databases"].pop(1)
            with self.assertRaisesRegex(
                backupctl.BackupError,
                "exactly five logical PostgreSQL",
            ):
                backupctl.BackupConfig.load(write_config(base, document))

    def test_vector_extension_versions_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            document = configuration(base)
            document["databases"][1]["extensions"]["vector"] = "0.8.2"
            with self.assertRaisesRegex(
                backupctl.BackupError,
                "database allowlist",
            ):
                backupctl.BackupConfig.load(write_config(base, document))

    def test_reviewed_parkable_services_are_accepted(self) -> None:
        for parked in (
            [],
            ["workloads_minecraft"],
            ["workloads_openclaw"],
            PARKED_SERVICES,
        ):
            with (
                self.subTest(parked=parked),
                tempfile.TemporaryDirectory() as temporary,
            ):
                base = Path(temporary)
                document = configuration(base)
                document["parked_services"] = parked
                config = backupctl.BackupConfig.load(write_config(base, document))
                self.assertEqual(config.document["parked_services"], parked)

    def test_parked_services_outside_the_contract_are_rejected(self) -> None:
        cases: list[tuple[str, object, str]] = [
            ("missing", None, "parked_services must be a list"),
            ("scalar", "workloads_minecraft", "parked_services must be a list"),
            ("non-string", [1], "parked_services must be a list"),
            (
                "unknown",
                ["workloads_n8n"],
                "outside the reviewed parkable set",
            ),
            (
                "unsorted",
                ["workloads_openclaw", "workloads_minecraft"],
                "parked_services must be sorted",
            ),
            (
                "duplicate",
                ["workloads_minecraft", "workloads_minecraft"],
                "must not repeat",
            ),
        ]
        for label, parked, message in cases:
            with (
                self.subTest(label),
                tempfile.TemporaryDirectory() as temporary,
            ):
                base = Path(temporary)
                document = configuration(base)
                if parked is None:
                    del document["parked_services"]
                else:
                    document["parked_services"] = parked
                with self.assertRaisesRegex(backupctl.BackupError, message):
                    backupctl.BackupConfig.load(write_config(base, document))

    def test_parked_service_must_be_managed_by_the_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            document = configuration(base)
            document["services"]["minecraft"] = "workloads_minecraft-legacy"
            document["parked_services"] = ["workloads_minecraft"]
            with self.assertRaisesRegex(
                backupctl.BackupError,
                "outside the backup contract",
            ):
                backupctl.BackupConfig.load(write_config(base, document))


class ApplicationBackupTests(unittest.TestCase):
    def test_unexpected_command_failure_updates_status_without_detail_leak(
        self,
    ) -> None:
        writes: list[tuple[bool, str]] = []

        class FakeConfig:
            def ensure_runtime_security(self) -> None:
                return None

            def section(self, name: str) -> dict[str, str]:
                self.assert_runtime(name)
                return {"lock_file": "/run/lock/backup-test.lock"}

            @staticmethod
            def assert_runtime(name: str) -> None:
                if name != "runtime":
                    raise AssertionError(name)

        class FakeRepository:
            def __init__(self, _config: object) -> None:
                pass

            def verify_binary(self) -> None:
                return None

        class FakeStatus:
            def __init__(self, _config: object, _kind: str) -> None:
                pass

            def write(
                self,
                success: bool,
                snapshot: str = "",
                detail: str = "",
            ) -> None:
                del snapshot
                writes.append((success, detail))

        stderr = io.StringIO()
        with (
            mock.patch.object(
                backupctl.BackupConfig,
                "load",
                return_value=FakeConfig(),
            ),
            mock.patch.object(backupctl, "Repository", FakeRepository),
            mock.patch.object(backupctl, "RuntimeStatus", FakeStatus),
            mock.patch.object(
                backupctl,
                "exclusive_lock",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(
                backupctl,
                "host_lock_helper",
                return_value=mock.Mock(ensure_mutation_lock=mock.Mock()),
            ),
            mock.patch.object(backupctl, "install_signal_handlers"),
            mock.patch.object(
                backupctl,
                "command_application",
                side_effect=RuntimeError("must-not-be-disclosed"),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            result = backupctl.main(["--config", "/ignored/config.json", "application"])

        self.assertEqual(result, 1)
        self.assertEqual(
            writes,
            [(False, "unexpected internal failure (RuntimeError)")],
        )
        self.assertNotIn("must-not-be-disclosed", stderr.getvalue())

    def test_each_consistency_group_is_quiesced_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            document = configuration(base)
            for dataset in document["datasets"]:
                Path(dataset["source"]).mkdir(parents=True)
            config = backupctl.BackupConfig.load(write_config(base, document))
            events: list[tuple[str, object]] = []

            class FakeSwarm:
                def assert_manager(self) -> None:
                    events.append(("manager", None))

                def replicas(self, service: str) -> tuple[int, int]:
                    events.append(("replicas", service))
                    return (1, 1)

                def service_image(self, service: str) -> str:
                    return f"registry.invalid/{service}@sha256:{'a' * 64}"

            class FakeRepository:
                def check_exists(self) -> None:
                    events.append(("repository", None))

                def backup(self, source: Path, tag: str, hostname: str) -> str:
                    self.backup_call = (source, tag, hostname)
                    self.manifest = json.loads(
                        (source / "manifest.json").read_text(encoding="utf-8")
                    )
                    return "b" * 64

                def apply_retention(self, tag: str, hostname: str) -> None:
                    self.retention_call = (tag, hostname)

            @contextlib.contextmanager
            def fake_quiesce(
                _swarm: object,
                service_names: list[str],
                *,
                parked: frozenset[str],
            ) -> object:
                events.append(("parked", parked))
                events.append(("quiesce-enter", tuple(service_names)))
                try:
                    yield
                finally:
                    events.append(("quiesce-exit", tuple(service_names)))

            @contextlib.contextmanager
            def fake_minecraft(_swarm: object, service: str) -> object:
                events.append(("minecraft-enter", service))
                try:
                    yield "minecraft-container"
                finally:
                    events.append(("minecraft-exit", service))

            def fake_database(
                _swarm: object,
                database: dict[str, object],
                destination: Path,
            ) -> dict[str, object]:
                events.append(("database", database["id"]))
                return {
                    "id": database["id"],
                    "type": "postgres-custom-dump",
                    "file": f"databases/{destination.name}",
                }

            def fake_archive(
                identifier: str, _source: Path, destination: Path
            ) -> dict[str, object]:
                events.append(("dataset", identifier))
                return {
                    "id": identifier,
                    "type": "filesystem-tar",
                    "file": f"filesystems/{destination.name}",
                }

            repository = FakeRepository()
            with (
                mock.patch.object(backupctl, "Swarm", return_value=FakeSwarm()),
                mock.patch.object(backupctl, "quiesced_services", fake_quiesce),
                mock.patch.object(
                    backupctl, "suspended_minecraft_saves", fake_minecraft
                ),
                mock.patch.object(backupctl, "dump_database", fake_database),
                mock.patch.object(backupctl, "archive_source", fake_archive),
            ):
                snapshot = backupctl.command_application(config, repository)

            self.assertEqual(snapshot, "b" * 64)
            expected_group_events: list[tuple[str, object]] = []
            for group in document["consistency_groups"]:
                services = tuple(group["services"])
                expected_group_events.append(("quiesce-enter", services))
                expected_group_events.extend(
                    ("database", identifier) for identifier in group["databases"]
                )
                expected_group_events.extend(
                    ("dataset", identifier) for identifier in group["datasets"]
                )
                expected_group_events.append(("quiesce-exit", services))
            observed_group_events = [
                event
                for event in events
                if event[0] in {"quiesce-enter", "database", "dataset", "quiesce-exit"}
                and event[1] not in {"minecraft-data", "minecraft-mods"}
            ]
            self.assertEqual(observed_group_events, expected_group_events)
            self.assertIn(("minecraft-enter", "workloads_minecraft"), events)
            self.assertIn(("dataset", "minecraft-data"), events)
            self.assertIn(("dataset", "minecraft-mods"), events)
            self.assertIn(("minecraft-exit", "workloads_minecraft"), events)
            self.assertEqual(
                {event[1] for event in events if event[0] == "parked"},
                {frozenset()},
            )
            self.assertEqual(
                repository.manifest["metadata"]["minecraft_consistency"]["protocol"],
                "rcon-save-off-save-all-flush-save-on",
            )
            self.assertEqual(repository.backup_call[1], "application")
            self.assertEqual(repository.retention_call[0], "application")

    def test_parked_workloads_are_archived_at_rest_without_exec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            document = configuration(base)
            document["parked_services"] = PARKED_SERVICES
            for dataset in document["datasets"]:
                Path(dataset["source"]).mkdir(parents=True)
            config = backupctl.BackupConfig.load(write_config(base, document))
            swarm = FakeReplicaSwarm(reviewed_replicas(document))
            commands: list[list[str]] = []

            def forbidden_run(arguments: list[str], **_kwargs: object) -> object:
                commands.append(list(arguments))
                raise AssertionError(f"unexpected command: {arguments!r}")

            repository = ManifestRepository()
            with (
                mock.patch.object(backupctl, "Swarm", return_value=swarm),
                mock.patch.object(backupctl, "run", side_effect=forbidden_run),
                mock.patch.object(
                    backupctl, "dump_database", contract_database_artifact
                ),
                mock.patch.object(
                    backupctl, "archive_source", contract_filesystem_artifact
                ),
            ):
                snapshot = backupctl.command_application(config, repository)

            self.assertEqual(snapshot, "c" * 64)
            self.assertEqual(commands, [])
            archived = {
                artifact["id"]
                for artifact in repository.manifest["artifacts"]
                if artifact["type"] == "filesystem-tar"
            }
            self.assertIn("minecraft-data", archived)
            self.assertIn("minecraft-mods", archived)
            self.assertIn("openclaw-clean-home", archived)
            scaled_services = {service for service, _ in swarm.scaled}
            self.assertFalse(scaled_services & set(PARKED_SERVICES))
            self.assertIn(("workloads_n8n", 0), swarm.scaled)
            self.assertIn(("workloads_n8n", 1), swarm.scaled)
            metadata = repository.manifest["metadata"]
            self.assertEqual(
                metadata["minecraft_consistency"],
                {
                    "service": "workloads_minecraft",
                    "artifacts": ["minecraft-data", "minecraft-mods"],
                    "protocol": "service-parked-at-rest",
                },
            )
            for service in PARKED_SERVICES:
                self.assertEqual(
                    (
                        metadata["services"][service]["desired_replicas"],
                        metadata["services"][service]["running_replicas"],
                    ),
                    (0, 0),
                )
            self.assertEqual(
                metadata["services"]["workloads_n8n"]["running_replicas"], 1
            )

            # The parked snapshot stays verifiable after the owner unparks.
            document["parked_services"] = []
            unparked = backupctl.BackupConfig.load(write_config(base, document))
            backupctl.validate_restored_contract(unparked, repository.manifest)

    def test_unavailable_service_is_never_recorded_or_uploaded(self) -> None:
        # Database services sit outside every quiesce window, so the metadata
        # read is the first to observe one that is degraded or stopped.
        for replicas in ((1, 0), (0, 0), (2, 1)):
            with (
                self.subTest(replicas=replicas),
                tempfile.TemporaryDirectory() as temporary,
            ):
                base = Path(temporary)
                document = configuration(base)
                document["parked_services"] = PARKED_SERVICES
                for dataset in document["datasets"]:
                    Path(dataset["source"]).mkdir(parents=True)
                config = backupctl.BackupConfig.load(write_config(base, document))
                swarm = FakeReplicaSwarm(reviewed_replicas(document))
                swarm.state["workloads_n8n-db"] = replicas
                repository = ManifestRepository()
                with (
                    mock.patch.object(backupctl, "Swarm", return_value=swarm),
                    mock.patch.object(
                        backupctl, "dump_database", contract_database_artifact
                    ),
                    mock.patch.object(
                        backupctl, "archive_source", contract_filesystem_artifact
                    ),
                    self.assertRaisesRegex(
                        backupctl.BackupError,
                        "service is not fully available while recording metadata "
                        rf"\(desired={replicas[0]}, running={replicas[1]}\): "
                        "workloads_n8n-db",
                    ),
                ):
                    backupctl.command_application(config, repository)
                self.assertIsNone(repository.manifest)

    def test_running_parked_minecraft_fails_before_archiving(self) -> None:
        swarm = FakeReplicaSwarm({"workloads_minecraft": (1, 1)})
        with self.assertRaisesRegex(
            backupctl.BackupError,
            "parked service is not at rest before backup",
        ):
            with backupctl.parked_minecraft_at_rest(swarm, "workloads_minecraft"):
                self.fail("a running parked server must not be archived")
        self.assertEqual(swarm.scaled, [])

    def test_draining_parked_minecraft_is_rejected_before_archiving(self) -> None:
        # (0, 1): a task still stopping after the scale to zero; (1, 0): the
        # service was unparked and its task has not started yet.
        for desired, running in ((0, 1), (1, 0)):
            with self.subTest(replicas=(desired, running)):
                swarm = FakeReplicaSwarm({"workloads_minecraft": (desired, running)})
                with self.assertRaisesRegex(
                    backupctl.BackupError,
                    "parked service is not at rest before backup "
                    rf"\(desired={desired}, running={running}\): "
                    "workloads_minecraft",
                ):
                    with backupctl.parked_minecraft_at_rest(
                        swarm, "workloads_minecraft"
                    ):
                        self.fail("a draining parked server must not be archived")
                self.assertEqual(swarm.scaled, [])

    def test_parked_minecraft_started_mid_archive_fails(self) -> None:
        swarm = FakeReplicaSwarm({"workloads_minecraft": (0, 0)})
        with self.assertRaisesRegex(
            backupctl.BackupError,
            "parked service is not at rest after backup "
            r"\(desired=1, running=1\): workloads_minecraft",
        ):
            with backupctl.parked_minecraft_at_rest(swarm, "workloads_minecraft"):
                swarm.state["workloads_minecraft"] = (1, 1)
        self.assertEqual(swarm.scaled, [])

    def test_parked_minecraft_started_during_backup_is_never_uploaded(self) -> None:
        class StartedBeforeMetadata(FakeReplicaSwarm):
            """Minecraft starts after its window closed, before metadata."""

            def service_image(self, service: str) -> str:
                # Database dumps are faked, so only the metadata loop reads
                # images, after every backup window has closed.
                self.state["workloads_minecraft"] = (1, 1)
                return super().service_image(service)

        scenarios = (
            (
                "started while archiving",
                FakeReplicaSwarm,
                True,
                (
                    "parked service is not at rest after backup "
                    r"\(desired=1, running=1\): workloads_minecraft"
                ),
            ),
            (
                "started before metadata",
                StartedBeforeMetadata,
                False,
                (
                    "parked service is not at rest while recording metadata "
                    r"\(desired=1, running=1\): workloads_minecraft"
                ),
            ),
        )
        for label, swarm_class, start_while_archiving, message in scenarios:
            with (
                self.subTest(label),
                tempfile.TemporaryDirectory() as temporary,
            ):
                base = Path(temporary)
                document = configuration(base)
                document["parked_services"] = PARKED_SERVICES
                for dataset in document["datasets"]:
                    Path(dataset["source"]).mkdir(parents=True)
                config = backupctl.BackupConfig.load(write_config(base, document))
                swarm = swarm_class(reviewed_replicas(document))

                def archive(
                    identifier: str,
                    source: Path,
                    destination: Path,
                    swarm: FakeReplicaSwarm = swarm,
                    start: bool = start_while_archiving,
                ) -> dict[str, object]:
                    if start and identifier == "minecraft-mods":
                        swarm.state["workloads_minecraft"] = (1, 1)
                    return contract_filesystem_artifact(identifier, source, destination)

                repository = ManifestRepository()
                with (
                    mock.patch.object(backupctl, "Swarm", return_value=swarm),
                    mock.patch.object(
                        backupctl, "dump_database", contract_database_artifact
                    ),
                    mock.patch.object(backupctl, "archive_source", archive),
                    self.assertRaisesRegex(backupctl.BackupError, message),
                ):
                    backupctl.command_application(config, repository)
                self.assertIsNone(repository.manifest)
                self.assertNotIn(
                    "workloads_minecraft", {name for name, _ in swarm.scaled}
                )


class QuiesceWindowTests(unittest.TestCase):
    def test_parked_group_service_is_never_scaled(self) -> None:
        swarm = FakeReplicaSwarm(
            {"workloads_n8n": (1, 1), "workloads_openclaw": (0, 0)}
        )
        with backupctl.quiesced_services(
            swarm,
            ["workloads_n8n", "workloads_openclaw"],
            parked=frozenset({"workloads_openclaw"}),
        ):
            self.assertEqual(swarm.state["workloads_n8n"], (0, 0))
            self.assertEqual(swarm.state["workloads_openclaw"], (0, 0))
        self.assertEqual(
            swarm.scaled,
            [("workloads_n8n", 0), ("workloads_n8n", 1)],
        )
        self.assertEqual(swarm.state["workloads_openclaw"], (0, 0))

    def test_running_parked_service_fails_closed(self) -> None:
        swarm = FakeReplicaSwarm(
            {"workloads_n8n": (1, 1), "workloads_openclaw": (1, 1)}
        )
        with self.assertRaisesRegex(
            backupctl.BackupError,
            "parked service is not at rest before backup",
        ):
            with backupctl.quiesced_services(
                swarm,
                ["workloads_n8n", "workloads_openclaw"],
                parked=frozenset({"workloads_openclaw"}),
            ):
                self.fail("the window must not open around a running service")
        self.assertNotIn(("workloads_n8n", 0), swarm.scaled)
        self.assertNotIn("workloads_openclaw", {name for name, _ in swarm.scaled})
        self.assertEqual(swarm.state["workloads_n8n"], (1, 1))

    def test_draining_parked_group_service_is_rejected_before_any_scale(
        self,
    ) -> None:
        # The parked service is listed first so a rejection leaves no writer
        # to recover: the window must refuse before touching anything.
        for desired, running in ((0, 1), (1, 0)):
            with self.subTest(replicas=(desired, running)):
                swarm = FakeReplicaSwarm(
                    {
                        "workloads_openclaw": (desired, running),
                        "workloads_n8n": (1, 1),
                    }
                )
                with self.assertRaisesRegex(
                    backupctl.BackupError,
                    "parked service is not at rest before backup "
                    rf"\(desired={desired}, running={running}\): "
                    "workloads_openclaw",
                ):
                    with backupctl.quiesced_services(
                        swarm,
                        ["workloads_openclaw", "workloads_n8n"],
                        parked=frozenset({"workloads_openclaw"}),
                    ):
                        self.fail("the window must not open around a draining task")
                self.assertEqual(swarm.scaled, [])
                self.assertEqual(swarm.state["workloads_n8n"], (1, 1))

    def test_parked_service_started_during_window_fails_and_restores_writers(
        self,
    ) -> None:
        swarm = FakeReplicaSwarm(
            {"workloads_n8n": (1, 1), "workloads_openclaw": (0, 0)}
        )
        with self.assertRaisesRegex(
            backupctl.BackupError,
            "parked service is not at rest after backup",
        ):
            with backupctl.quiesced_services(
                swarm,
                ["workloads_n8n", "workloads_openclaw"],
                parked=frozenset({"workloads_openclaw"}),
            ):
                swarm.state["workloads_openclaw"] = (1, 1)
        self.assertEqual(
            swarm.scaled,
            [("workloads_n8n", 0), ("workloads_n8n", 1)],
        )


class ParkedSnapshotVerificationTests(unittest.TestCase):
    def test_consistent_snapshots_verify_whatever_is_parked_now(self) -> None:
        scenarios = [
            (frozenset(), backupctl.MINECRAFT_RCON_PROTOCOL),
            (frozenset({"workloads_openclaw"}), backupctl.MINECRAFT_RCON_PROTOCOL),
            (
                frozenset({"workloads_minecraft"}),
                backupctl.MINECRAFT_PARKED_PROTOCOL,
            ),
            (frozenset(PARKED_SERVICES), backupctl.MINECRAFT_PARKED_PROTOCOL),
        ]
        # Verification never reads the current parked_services: a snapshot
        # taken while parked verifies after unparking, and the reverse.
        for parked_now in ([], PARKED_SERVICES):
            for at_rest, protocol in scenarios:
                with (
                    self.subTest(parked_now=parked_now, at_rest=sorted(at_rest)),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    base = Path(temporary)
                    document = configuration(base)
                    manifest = application_manifest(
                        document,
                        at_rest=at_rest,
                        protocol=protocol,
                    )
                    document["parked_services"] = parked_now
                    config = backupctl.BackupConfig.load(write_config(base, document))
                    backupctl.validate_restored_contract(config, manifest)

    def test_non_parkable_service_at_rest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            document = configuration(base)
            config = backupctl.BackupConfig.load(write_config(base, document))
            manifest = application_manifest(
                document,
                at_rest=frozenset({"workloads_n8n"}),
            )
            with self.assertRaisesRegex(
                backupctl.BackupError,
                "non-parkable service at rest: workloads_n8n",
            ):
                backupctl.validate_restored_contract(config, manifest)

    def test_parked_protocol_with_running_minecraft_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            document = configuration(base)
            document["parked_services"] = PARKED_SERVICES
            config = backupctl.BackupConfig.load(write_config(base, document))
            manifest = application_manifest(
                document,
                protocol=backupctl.MINECRAFT_PARKED_PROTOCOL,
            )
            with self.assertRaisesRegex(
                backupctl.BackupError,
                "Minecraft consistency metadata differs",
            ):
                backupctl.validate_restored_contract(config, manifest)

    def test_rcon_protocol_with_parked_minecraft_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            document = configuration(base)
            config = backupctl.BackupConfig.load(write_config(base, document))
            manifest = application_manifest(
                document,
                at_rest=frozenset({"workloads_minecraft"}),
                protocol=backupctl.MINECRAFT_RCON_PROTOCOL,
            )
            with self.assertRaisesRegex(
                backupctl.BackupError,
                "Minecraft consistency metadata differs",
            ):
                backupctl.validate_restored_contract(config, manifest)

    def test_partially_stopped_or_boolean_replicas_are_rejected(self) -> None:
        # False == 0 and True == 1 in Python: without the bool guard a
        # recorded (False, False) would read as parked and (True, True) as
        # available, so both must be refused as malformed metadata.
        for replicas in ((0, 1), (1, 0), (2, 1), (False, False), (True, True)):
            with (
                self.subTest(replicas=replicas),
                tempfile.TemporaryDirectory() as temporary,
            ):
                base = Path(temporary)
                document = configuration(base)
                config = backupctl.BackupConfig.load(write_config(base, document))
                manifest = application_manifest(document)
                recorded = manifest["metadata"]["services"]["workloads_openclaw"]
                recorded["desired_replicas"], recorded["running_replicas"] = replicas
                with self.assertRaisesRegex(
                    backupctl.BackupError,
                    "service metadata is invalid: workloads_openclaw",
                ):
                    backupctl.validate_restored_contract(config, manifest)


class ArchiveSafetyTests(unittest.TestCase):
    def test_safe_archive_and_manifest_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "filesystems" / "n8n-home.tar"
            archive.parent.mkdir()
            payload = b"reviewed data\n"
            with tarfile.open(archive, "w") as stream:
                member = tarfile.TarInfo("home/config")
                member.size = len(payload)
                stream.addfile(member, io.BytesIO(payload))
            manifest = {
                "schema_version": 1,
                "kind": "application",
                "artifacts": [
                    {
                        "id": "n8n-home",
                        "type": "filesystem-tar",
                        "file": "filesystems/n8n-home.tar",
                        "sha256": backupctl.sha256_file(archive),
                        "size": archive.stat().st_size,
                        "members": 1,
                    }
                ],
            }
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            _, restored = backupctl.validate_manifest_tree(root)
            self.assertEqual(restored["kind"], "application")

    def test_parent_traversal_archive_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "unsafe.tar"
            payload = b"must not escape\n"
            with tarfile.open(archive, "w") as stream:
                member = tarfile.TarInfo("../escape")
                member.size = len(payload)
                stream.addfile(member, io.BytesIO(payload))
            with self.assertRaisesRegex(
                backupctl.BackupError,
                "escapes the archive root",
            ):
                backupctl.validate_tar_members(archive)

    def test_escaping_symlink_archive_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "unsafe-link.tar"
            with tarfile.open(archive, "w") as stream:
                member = tarfile.TarInfo("home/escape")
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
                stream.addfile(member)
            with self.assertRaisesRegex(
                backupctl.BackupError,
                "escapes the archive root",
            ):
                backupctl.validate_tar_members(archive)

    def test_duplicate_archive_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "duplicate.tar"
            with tarfile.open(archive, "w") as stream:
                for payload in (b"first\n", b"second\n"):
                    member = tarfile.TarInfo("home/value")
                    member.size = len(payload)
                    stream.addfile(member, io.BytesIO(payload))
            with self.assertRaisesRegex(
                backupctl.BackupError,
                "duplicate member",
            ):
                backupctl.validate_tar_members(archive)

    def test_checksum_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "database.dump"
            artifact.write_bytes(b"not the expected bytes")
            manifest = {
                "schema_version": 1,
                "kind": "application",
                "artifacts": [
                    {
                        "id": "n8n-postgres",
                        "type": "postgres-custom-dump",
                        "file": artifact.name,
                        "sha256": "0" * 64,
                        "extensions": {},
                        "schema_inventory": ["public.example|r"],
                        "dump_catalog_sha256": "1" * 64,
                        "dump_catalog_entries": 1,
                    }
                ],
            }
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(backupctl.BackupError, "checksum"):
                backupctl.validate_manifest_tree(root)

    def test_artifact_identifier_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "filesystem.tar"
            payload = b"safe bytes\n"
            with tarfile.open(archive, "w") as stream:
                member = tarfile.TarInfo("data/value")
                member.size = len(payload)
                stream.addfile(member, io.BytesIO(payload))
            manifest = {
                "schema_version": 1,
                "kind": "application",
                "artifacts": [
                    {
                        "id": "../../outside",
                        "type": "filesystem-tar",
                        "file": archive.name,
                        "sha256": backupctl.sha256_file(archive),
                    }
                ],
            }
            (root / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                backupctl.BackupError,
                "invalid artifact metadata",
            ):
                backupctl.validate_manifest_tree(root)


class RestoreGuardTests(unittest.TestCase):
    def test_nonempty_restore_target_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            owner_data = target / "owner-data"
            owner_data.write_text("keep\n", encoding="utf-8")
            with self.assertRaisesRegex(backupctl.BackupError, "must be absent"):
                backupctl.ensure_empty_target(target)
            self.assertEqual(owner_data.read_text(encoding="utf-8"), "keep\n")

    def test_cleanup_cannot_escape_staging_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = backupctl.BackupConfig.load(
                write_config(base, configuration(base))
            )
            outside = base / "outside"
            outside.mkdir()
            owner_data = outside / "owner-data"
            owner_data.write_text("keep\n", encoding="utf-8")
            with self.assertRaisesRegex(backupctl.BackupError, "outside"):
                backupctl.cleanup_stage(config, outside)
            self.assertEqual(owner_data.read_text(encoding="utf-8"), "keep\n")

    def test_lock_rejects_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "backup.lock"
            with backupctl.exclusive_lock(lock):
                with self.assertRaisesRegex(backupctl.BackupError, "another"):
                    with backupctl.exclusive_lock(lock):
                        self.fail("nested lock unexpectedly succeeded")

    def test_lock_detects_inode_replacement_before_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "backup.lock"
            with self.assertRaisesRegex(
                backupctl.BackupError,
                "replaced while held",
            ):
                with backupctl.exclusive_lock(lock):
                    lock.unlink()
                    lock.write_text("replacement\n", encoding="ascii")
                    lock.chmod(0o600)


class MinecraftSaveGuardTests(unittest.TestCase):
    def test_ambiguous_save_off_failure_still_attempts_save_on(self) -> None:
        calls: list[str] = []

        class FakeSwarm:
            docker = "/usr/bin/docker"

            def replicas(self, _service: str) -> tuple[int, int]:
                return (1, 1)

            def one_container(self, _service: str) -> str:
                return "container-id"

        def fake_run(arguments: list[str], **_kwargs: object) -> object:
            calls.append(arguments[-1])
            if arguments[-1] == "save-off":
                raise backupctl.BackupError(
                    "ambiguous interruption after save-off dispatch"
                )
            return backupctl.CommandResult("", "")

        with (
            mock.patch.object(backupctl, "run", side_effect=fake_run),
            self.assertRaisesRegex(
                backupctl.BackupError,
                "ambiguous interruption",
            ),
        ):
            with backupctl.suspended_minecraft_saves(
                FakeSwarm(),
                "minecraft",
            ):
                self.fail("backup body must not run after save-off failure")
        self.assertEqual(calls, ["save-off", "save-on"])


class SwarmIdentityTests(unittest.TestCase):
    def test_identity_uses_supported_docker_info_fields(self) -> None:
        observed_arguments: list[list[str]] = []

        def fake_run(arguments: list[str]) -> backupctl.CommandResult:
            observed_arguments.append(arguments)
            return backupctl.CommandResult(
                '"swarm-id"|true|"node-id"|"29.6.2"\n',
                "",
            )

        with mock.patch.object(backupctl, "run", fake_run):
            identity = backupctl.swarm_identity()

        self.assertEqual(
            identity,
            {
                "id": "swarm-id",
                "autolock": True,
                "node_id": "node-id",
                "engine_version": "29.6.2",
            },
        )
        self.assertEqual(
            observed_arguments[0][:3],
            ["/usr/bin/docker", "info", "--format"],
        )
        self.assertNotIn("inspect", observed_arguments[0])

    def test_malformed_identity_fields_are_rejected(self) -> None:
        with (
            mock.patch.object(
                backupctl,
                "run",
                return_value=backupctl.CommandResult(
                    '"swarm-id"|false|"node-id"\n',
                    "",
                ),
            ),
            self.assertRaisesRegex(
                backupctl.BackupError,
                "malformed Swarm identity",
            ),
        ):
            backupctl.swarm_identity()

    def test_docker_recovery_delegates_to_single_systemd_unlock_path(
        self,
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(
            arguments: list[str],
            **_kwargs: object,
        ) -> backupctl.CommandResult:
            calls.append(arguments)
            return backupctl.CommandResult("", "")

        class FakeSwarm:
            def assert_manager(self) -> None:
                return None

        with (
            mock.patch.object(backupctl, "run", fake_run),
            mock.patch.object(backupctl, "wait_systemd"),
            mock.patch.object(backupctl, "Swarm", return_value=FakeSwarm()),
            mock.patch.object(
                backupctl,
                "swarm_identity",
                return_value={"id": "expected-swarm"},
            ),
        ):
            backupctl.start_and_unlock_docker(
                socket_was_active=True,
                expected_swarm_id="expected-swarm",
            )

        self.assertIn(
            [
                "/usr/bin/systemctl",
                "start",
                "dockerswarm-swarm-unlock.service",
            ],
            calls,
        )
        self.assertNotIn(
            ["/usr/bin/docker", "swarm", "unlock"],
            calls,
        )


if __name__ == "__main__":
    unittest.main()
