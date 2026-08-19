from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, REPOSITORY_ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


secret_installer = load_script(
    "install_workload_secrets",
    "scripts/install-workload-secrets.py",
)
marker_validator = load_script(
    "validate_workloads_restore_marker",
    "scripts/validate-workloads-restore-marker.py",
)
workload_validator = load_script(
    "validate_workloads",
    "scripts/validate-workloads.py",
)
container_gate = load_script(
    "validate_workloads_container_gate",
    "scripts/validate-workloads-container-gate.py",
)
deployment_validator = load_script(
    "validate_swarm_deployment",
    "scripts/validate-swarm-deployment.py",
)
runner_manager = load_script(
    "manage_n8n_runner_image",
    "scripts/manage-n8n-runner-image.py",
)
tracked_image_resolver = load_script(
    "resolve_tracked_image",
    "scripts/resolve-tracked-image.py",
)


class TrackedImageResolutionTests(unittest.TestCase):
    REFERENCE = "docker.io/hgarciaalberto/personal-website:latest"
    DIGEST = "sha256:" + ("a" * 64)
    RESOLVED_REFERENCE = f"{REFERENCE}@{DIGEST}"
    APPROVED_REFERENCE = RESOLVED_REFERENCE

    def valid_descriptor(self) -> dict[str, object]:
        return {
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "digest": self.DIGEST,
            "size": 2623,
        }

    def valid_inspect(self) -> list[dict[str, object]]:
        return [
            {
                "Os": "linux",
                "Architecture": "amd64",
                "Id": "sha256:" + ("b" * 64),
                "RepoDigests": [
                    "docker.io/hgarciaalberto/personal-website@" + self.DIGEST
                ],
            }
        ]

    def test_exact_tracked_repository_digest_is_resolved(self) -> None:
        for media_type in tracked_image_resolver.ALLOWED_MEDIA_TYPES:
            with self.subTest(media_type=media_type):
                descriptor = self.valid_descriptor()
                descriptor["mediaType"] = media_type
                self.assertEqual(
                    tracked_image_resolver.resolve_tracked_reference(
                        self.REFERENCE,
                        self.APPROVED_REFERENCE,
                        descriptor,
                    ),
                    self.RESOLVED_REFERENCE,
                )

    def test_invalid_registry_descriptors_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            tracked_image_resolver.TrackedImageError,
            "not approved",
        ):
            tracked_image_resolver.resolve_tracked_reference(
                "docker.io/example/other:latest",
                self.APPROVED_REFERENCE,
                self.valid_descriptor(),
            )

        invalid_descriptors = [
            None,
            [],
            {},
            {**self.valid_descriptor(), "mediaType": "text/plain"},
            {**self.valid_descriptor(), "digest": "sha256:invalid"},
            {**self.valid_descriptor(), "size": 0},
            {**self.valid_descriptor(), "size": True},
        ]
        for descriptor in invalid_descriptors:
            with self.subTest(descriptor=descriptor):
                with self.assertRaises(tracked_image_resolver.TrackedImageError):
                    tracked_image_resolver.resolve_tracked_reference(
                        self.REFERENCE,
                        self.APPROVED_REFERENCE,
                        descriptor,
                    )

    def test_unreviewed_registry_digest_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            tracked_image_resolver.TrackedImageError,
            "differs from the reviewed runtime approval",
        ):
            tracked_image_resolver.resolve_tracked_reference(
                self.REFERENCE,
                self.REFERENCE + "@sha256:" + ("c" * 64),
                self.valid_descriptor(),
            )

        with self.assertRaisesRegex(
            tracked_image_resolver.TrackedImageError,
            "approved runtime reference is invalid",
        ):
            tracked_image_resolver.resolve_tracked_reference(
                self.REFERENCE,
                self.REFERENCE,
                self.valid_descriptor(),
            )

        for unapproved_reference in (
            "docker.io/example/other:latest@sha256:" + ("d" * 64),
            "docker.io/hgarciaalberto/personal-website:canary@sha256:"
            + ("e" * 64),
        ):
            with self.subTest(unapproved_reference=unapproved_reference):
                with self.assertRaisesRegex(
                    tracked_image_resolver.TrackedImageError,
                    "approved runtime reference is invalid",
                ):
                    tracked_image_resolver.resolve_tracked_reference(
                        self.REFERENCE,
                        unapproved_reference,
                        self.valid_descriptor(),
                    )

    def test_local_image_matches_the_resolved_digest(self) -> None:
        tracked_image_resolver.verify_tracked_image(
            self.RESOLVED_REFERENCE,
            self.valid_inspect(),
        )
        inspect_with_multiple_digests = self.valid_inspect()
        inspect_with_multiple_digests[0]["RepoDigests"] = [
            "hgarciaalberto/personal-website@sha256:" + ("c" * 64),
            "hgarciaalberto/personal-website@" + self.DIGEST,
            "docker.io/example/other@sha256:" + ("d" * 64),
        ]
        tracked_image_resolver.verify_tracked_image(
            self.RESOLVED_REFERENCE,
            inspect_with_multiple_digests,
        )

    def test_invalid_local_image_identities_are_rejected(self) -> None:
        invalid_resolved_references = [
            "docker.io/example/other:latest@" + self.DIGEST,
            self.REFERENCE + "@sha256:invalid",
        ]
        for reference in invalid_resolved_references:
            with self.subTest(reference=reference):
                with self.assertRaises(tracked_image_resolver.TrackedImageError):
                    tracked_image_resolver.verify_tracked_image(
                        reference,
                        self.valid_inspect(),
                    )

        malformed_documents = [
            {},
            [],
            [self.valid_inspect()[0], self.valid_inspect()[0]],
            ["not-an-object"],
        ]
        for document in malformed_documents:
            with self.subTest(document=document):
                with self.assertRaises(tracked_image_resolver.TrackedImageError):
                    tracked_image_resolver.verify_tracked_image(
                        self.RESOLVED_REFERENCE,
                        document,
                    )

        wrong_platform = self.valid_inspect()
        wrong_platform[0]["Architecture"] = "arm64"
        with self.assertRaisesRegex(
            tracked_image_resolver.TrackedImageError,
            "linux/amd64",
        ):
            tracked_image_resolver.verify_tracked_image(
                self.RESOLVED_REFERENCE,
                wrong_platform,
            )

        wrong_os = self.valid_inspect()
        wrong_os[0]["Os"] = "windows"
        with self.assertRaisesRegex(
            tracked_image_resolver.TrackedImageError,
            "linux/amd64",
        ):
            tracked_image_resolver.verify_tracked_image(
                self.RESOLVED_REFERENCE,
                wrong_os,
            )

        invalid_id = self.valid_inspect()
        invalid_id[0]["Id"] = "not-a-content-id"
        with self.assertRaisesRegex(
            tracked_image_resolver.TrackedImageError,
            "content ID",
        ):
            tracked_image_resolver.verify_tracked_image(
                self.RESOLVED_REFERENCE,
                invalid_id,
            )

        for repo_digests in (
            None,
            [],
            [123],
            ["docker.io/example/other@" + self.DIGEST],
        ):
            with self.subTest(repo_digests=repo_digests):
                inspected = self.valid_inspect()
                inspected[0]["RepoDigests"] = repo_digests
                with self.assertRaises(tracked_image_resolver.TrackedImageError):
                    tracked_image_resolver.verify_tracked_image(
                        self.RESOLVED_REFERENCE,
                        inspected,
                    )

    def test_cli_resolves_verifies_and_rejects_invalid_input(self) -> None:
        resolve_command = [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts/resolve-tracked-image.py"),
            "resolve",
            "--reference",
            self.REFERENCE,
            "--approved-reference",
            self.APPROVED_REFERENCE,
        ]
        valid = subprocess.run(
            resolve_command,
            input=json.dumps(self.valid_descriptor()).encode("utf-8"),
            capture_output=True,
            check=False,
        )
        self.assertEqual(valid.returncode, 0, valid.stderr.decode("utf-8"))
        self.assertEqual(
            valid.stdout.decode("utf-8").strip(),
            self.RESOLVED_REFERENCE,
        )

        verify_command = [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts/resolve-tracked-image.py"),
            "verify",
            "--resolved-reference",
            self.RESOLVED_REFERENCE,
        ]
        verified = subprocess.run(
            verify_command,
            input=json.dumps(self.valid_inspect()).encode("utf-8"),
            capture_output=True,
            check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr.decode("utf-8"))
        self.assertEqual(verified.stdout, b"")

        invalid = subprocess.run(
            resolve_command,
            input=b"not-json",
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn(b"not valid JSON", invalid.stderr)

        invalid_utf8 = subprocess.run(
            resolve_command,
            input=b"\xff",
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(invalid_utf8.returncode, 0)
        self.assertIn(b"not valid JSON", invalid_utf8.stderr)

        oversized = subprocess.run(
            resolve_command,
            input=b" " * (tracked_image_resolver.MAX_JSON_BYTES + 1),
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(oversized.returncode, 0)
        self.assertIn(b"exceeds the size limit", oversized.stderr)


class WorkloadSecretContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.secret_catalog = yaml.safe_load(
            (REPOSITORY_ROOT / "stacks/workloads/secrets.yml").read_text(
                encoding="utf-8"
            )
        )

    def test_materialized_sources_and_optional_empty_sentinel(self) -> None:
        entries = self.secret_catalog["workloads_secrets"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            for entry in entries:
                value = b"required-value\n" if entry["required_nonempty"] else b""
                path = root / entry["source_file"]
                path.write_bytes(value)
                path.chmod(0o600)
            manifest = {
                "schemaVersion": 4,
                "secretFiles": [entry["source_file"] for entry in entries],
                "openClawLegacyImported": False,
                "secretIdentityKeyFile": (secret_installer.SECRET_IDENTITY_KEY_FILE),
            }
            identity_key_path = root / secret_installer.SECRET_IDENTITY_KEY_FILE
            identity_key_path.write_bytes(b"k" * 32)
            identity_key_path.chmod(0o600)
            version, validated = secret_installer.validate_contract(
                self.secret_catalog, manifest
            )
            self.assertEqual(version, "v1")
            self.assertEqual(len(validated), len(entries))
            secret_installer.validate_private_directory(root)
            self.assertEqual(
                secret_installer.read_identity_key(root, manifest),
                b"k" * 32,
            )
            for entry in entries:
                materialized = secret_installer.read_secret_source(root, entry)
                if entry["required_nonempty"]:
                    self.assertEqual(materialized, b"required-value\n")
                else:
                    self.assertEqual(
                        materialized,
                        secret_installer.OPTIONAL_UNSET_SENTINEL,
                    )

    def test_n8n_quarantine_blocks_workload_restore_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_directory = Path(temporary)
            marker = state_directory / "workloads-ready-v2.json"
            quarantine = state_directory / marker_validator.N8N_QUARANTINE_FILENAME
            quarantine.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                marker_validator.MarkerError,
                "quarantine blocks workload deployment",
            ):
                marker_validator.reject_workflow_quarantine(marker)

    def test_required_empty_source_fails_closed(self) -> None:
        entry = next(
            item
            for item in self.secret_catalog["workloads_secrets"]
            if item["required_nonempty"]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            source = root / entry["source_file"]
            source.write_bytes(b"")
            source.chmod(0o600)
            with self.assertRaises(secret_installer.SecretInstallError):
                secret_installer.read_secret_source(root, entry)

            source.write_bytes(secret_installer.OPTIONAL_UNSET_SENTINEL)
            with self.assertRaises(secret_installer.SecretInstallError):
                secret_installer.read_secret_source(root, entry)

    def test_runtime_manifest_must_match_exactly(self) -> None:
        entries = self.secret_catalog["workloads_secrets"]
        source_files = [item["source_file"] for item in entries]
        manifest = {
            "schemaVersion": 4,
            "secretFiles": source_files + ["unexpected"],
            "openClawLegacyImported": False,
            "secretIdentityKeyFile": secret_installer.SECRET_IDENTITY_KEY_FILE,
        }
        with self.assertRaises(secret_installer.SecretInstallError):
            secret_installer.validate_contract(self.secret_catalog, manifest)
        manifest["secretFiles"] = source_files
        manifest["schemaVersion"] = 3
        with self.assertRaises(secret_installer.SecretInstallError):
            secret_installer.validate_contract(self.secret_catalog, manifest)

    def test_existing_secret_is_bound_to_exact_retained_source(self) -> None:
        entry = self.secret_catalog["workloads_secrets"][0]
        identity = secret_installer.source_identity(
            b"k" * 32,
            entry,
            "v1",
            b"exact-source\n",
        )
        metadata = {
            "Spec": {
                "Name": entry["external_name"],
                "Labels": {
                    "com.apptolast.managed-by": ("workload-secret-installer"),
                    "com.apptolast.secret-key": entry["key"],
                    "com.apptolast.secret-version": "v1",
                    secret_installer.SECRET_IDENTITY_LABEL: identity,
                },
            }
        }
        secret_installer.validate_secret_metadata(
            metadata,
            entry,
            "v1",
            identity,
        )
        changed_identity = secret_installer.source_identity(
            b"k" * 32,
            entry,
            "v1",
            b"tampered-source\n",
        )
        self.assertNotEqual(identity, changed_identity)
        with self.assertRaises(secret_installer.SecretInstallError):
            secret_installer.validate_secret_metadata(
                metadata,
                entry,
                "v1",
                changed_identity,
            )

    def test_identity_key_is_private_and_manifest_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            key_path = root / secret_installer.SECRET_IDENTITY_KEY_FILE
            key_path.write_bytes(b"k" * 32)
            key_path.chmod(0o644)
            manifest = {
                "secretIdentityKeyFile": (secret_installer.SECRET_IDENTITY_KEY_FILE)
            }
            with self.assertRaises(secret_installer.SecretInstallError):
                secret_installer.read_identity_key(root, manifest)
            key_path.chmod(0o600)
            manifest["secretIdentityKeyFile"] = "different-key"
            with self.assertRaises(secret_installer.SecretInstallError):
                secret_installer.read_identity_key(root, manifest)

    def test_secret_creation_includes_only_keyed_source_identity(self) -> None:
        entry = self.secret_catalog["workloads_secrets"][0]
        source = b"private-value\n"
        identity = secret_installer.source_identity(
            b"k" * 32,
            entry,
            "v1",
            source,
        )
        with mock.patch.object(
            secret_installer.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, b"", b""),
        ) as run:
            secret_installer.create_secret(
                entry,
                "v1",
                source,
                identity,
            )
        command = run.call_args.args[0]
        self.assertIn(
            f"{secret_installer.SECRET_IDENTITY_LABEL}={identity}",
            command,
        )
        self.assertNotIn(source.decode("utf-8").strip(), command)
        self.assertEqual(run.call_args.kwargs["input"], source)


class WorkloadRestoreMarkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service_catalog = yaml.safe_load(
            (REPOSITORY_ROOT / "config/services.yml").read_text(encoding="utf-8")
        )

    def valid_marker(
        self,
        *,
        catalog_sha256: str,
        runtime_sha256: str,
        recovery_sha256: str,
        identity_key_sha256: str,
    ) -> dict[str, object]:
        return {
            "schemaVersion": 2,
            "completedAt": "2026-07-26T12:00:00+00:00",
            "catalogVersion": self.service_catalog["catalog_version"],
            "catalogSha256": catalog_sha256,
            "runtimeManifestSha256": runtime_sha256,
            "sourceBackup": "apptolast-data-20260723T225340Z",
            "sourceBackupSha256": recovery_sha256,
            "sourceBackupDigestKind": ("sha256-of-verified-recovery-manifest"),
            "approvedServices": sorted(
                item["id"]
                for item in self.service_catalog["approved_services"]
                if item["id"] != "traefik-edge"
            ),
            "datasets": {
                item["id"]: (
                    "initialized-empty"
                    if item["migration"] == "initialize-empty"
                    else "restored-and-verified"
                )
                for item in self.service_catalog["datasets"]
                if item["owner"] != "traefik-edge"
            },
            "databaseRestores": {
                "n8n": "verified",
                "passbolt": "verified",
                "shlink": "verified",
            },
            "openClawLegacyImported": False,
            "runtimeManifestSchemaVersion": 4,
            "secretIdentityKeySha256": identity_key_sha256,
            "secretIdentityKeyDigestKind": "sha256-of-random-hmac-key",
        }

    def validate_fixture(
        self,
        root: Path,
        marker: dict[str, object],
    ) -> None:
        catalog_path = root / "services.yml"
        runtime_path = root / "runtime-manifest.json"
        recovery_path = root / "SHA256SUMS"
        identity_key_path = root / "workloads_secret_identity_hmac_key"
        marker_validator.validate_marker(
            marker,
            yaml.safe_load(catalog_path.read_text(encoding="utf-8")),
            catalog_sha256=hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
            runtime_manifest=json.loads(runtime_path.read_text(encoding="utf-8")),
            runtime_manifest_sha256=hashlib.sha256(
                runtime_path.read_bytes()
            ).hexdigest(),
            recovery_manifest_sha256=hashlib.sha256(
                recovery_path.read_bytes()
            ).hexdigest(),
            secret_identity_key_sha256=hashlib.sha256(
                identity_key_path.read_bytes()
            ).hexdigest(),
        )

    def create_fixture(self, root: Path) -> dict[str, object]:
        catalog_path = root / "services.yml"
        shutil.copyfile(REPOSITORY_ROOT / "config/services.yml", catalog_path)
        runtime_path = root / "runtime-manifest.json"
        runtime_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 4,
                    "sourceBackup": "apptolast-data-20260723T225340Z",
                    "secretIdentityKeyFile": ("workloads_secret_identity_hmac_key"),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        recovery_path = root / "SHA256SUMS"
        recovery_path.write_text(
            f"{'0' * 64}  databases/n8n.dump\n",
            encoding="utf-8",
        )
        identity_key_path = root / "workloads_secret_identity_hmac_key"
        identity_key_path.write_bytes(b"k" * 32)
        return self.valid_marker(
            catalog_sha256=hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
            runtime_sha256=hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
            recovery_sha256=hashlib.sha256(recovery_path.read_bytes()).hexdigest(),
            identity_key_sha256=hashlib.sha256(
                identity_key_path.read_bytes()
            ).hexdigest(),
        )

    def test_exact_restore_evidence_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = self.create_fixture(root)
            self.validate_fixture(root, marker)

    def test_legacy_openclaw_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = self.create_fixture(root)
            marker["openClawLegacyImported"] = True
            with self.assertRaises(marker_validator.MarkerError):
                self.validate_fixture(root, marker)

    def test_missing_dataset_evidence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = self.create_fixture(root)
            datasets = dict(marker["datasets"])
            datasets.pop("n8n-home")
            marker["datasets"] = datasets
            with self.assertRaises(marker_validator.MarkerError):
                self.validate_fixture(root, marker)

    def test_tampered_catalog_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = self.create_fixture(root)
            with (root / "services.yml").open("a", encoding="utf-8") as handle:
                handle.write("\n")
            with self.assertRaisesRegex(
                marker_validator.MarkerError,
                "catalogSha256",
            ):
                self.validate_fixture(root, marker)

    def test_tampered_runtime_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = self.create_fixture(root)
            with (root / "runtime-manifest.json").open(
                "a",
                encoding="utf-8",
            ) as handle:
                handle.write(" ")
            with self.assertRaisesRegex(
                marker_validator.MarkerError,
                "runtimeManifestSha256",
            ):
                self.validate_fixture(root, marker)

    def test_tampered_secret_identity_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = self.create_fixture(root)
            (root / "workloads_secret_identity_hmac_key").write_bytes(b"x" * 32)
            with self.assertRaisesRegex(
                marker_validator.MarkerError,
                "secretIdentityKeySha256",
            ):
                self.validate_fixture(root, marker)

    def test_tampered_recovery_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = self.create_fixture(root)
            with (root / "SHA256SUMS").open("a", encoding="utf-8") as handle:
                handle.write(f"{'1' * 64}  databases/passbolt.dump\n")
            with self.assertRaisesRegex(
                marker_validator.MarkerError,
                "sourceBackupSha256",
            ):
                self.validate_fixture(root, marker)


class WorkloadContainerGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        catalog = yaml.safe_load(
            (REPOSITORY_ROOT / "config/services.yml").read_text(encoding="utf-8")
        )
        paths = {item["id"]: item["target_path"] for item in catalog["datasets"]}
        cls.contract = {
            paths["n8n-postgres"]: "workloads_n8n-db",
            paths["passbolt-postgres"]: "workloads_passbolt-db",
            paths["shlink-postgres"]: "workloads_shlink-db",
        }

    @staticmethod
    def container(
        identifier: str,
        *,
        labels: dict[str, str] | None = None,
        source: str | None = None,
        destination: str = "/data",
        read_write: bool = True,
    ) -> dict[str, object]:
        mounts = (
            []
            if source is None
            else [
                {
                    "Type": "bind",
                    "Source": source,
                    "Destination": destination,
                    "RW": read_write,
                }
            ]
        )
        return {
            "Id": identifier,
            "Config": {"Labels": labels or {}},
            "Mounts": mounts,
        }

    def test_stopped_restore_compose_container_is_rejected(self) -> None:
        inspected = self.container(
            "restore-stopped",
            labels={"com.docker.compose.project": "apptolast-restore"},
        )
        with self.assertRaises(container_gate.ContainerGateError):
            container_gate.validate_containers([inspected], self.contract)

    def test_arbitrary_container_overlapping_database_is_rejected(self) -> None:
        database_path = next(iter(self.contract))
        inspected = self.container(
            "rogue",
            source=str(Path(database_path).parent),
        )
        with self.assertRaises(container_gate.ContainerGateError):
            container_gate.validate_containers([inspected], self.contract)

    def test_exact_swarm_database_task_is_accepted(self) -> None:
        database_path, service = next(iter(self.contract.items()))
        inspected = self.container(
            "approved-task",
            labels={
                "com.docker.swarm.service.name": service,
                "com.docker.swarm.task.id": "task-id",
                "com.docker.swarm.task.name": f"{service}.1.task-id",
            },
            source=database_path,
            destination="/var/lib/postgresql/data",
        )
        container_gate.validate_containers([inspected], self.contract)

    def test_spoofed_service_without_task_identity_is_rejected(self) -> None:
        database_path, service = next(iter(self.contract.items()))
        inspected = self.container(
            "spoofed",
            labels={"com.docker.swarm.service.name": service},
            source=database_path,
        )
        with self.assertRaises(container_gate.ContainerGateError):
            container_gate.validate_containers([inspected], self.contract)

    def test_exact_read_only_observability_root_consumers_are_accepted(
        self,
    ) -> None:
        for service, destination in (
            ("observability_cadvisor", "/rootfs"),
            ("observability_node-exporter", "/host"),
        ):
            inspected = self.container(
                service,
                labels={
                    "com.docker.stack.namespace": "observability",
                    "com.docker.swarm.service.name": service,
                    "com.docker.swarm.task.id": "task-id",
                    "com.docker.swarm.task.name": f"{service}.1.task-id",
                },
                source="/",
                destination=destination,
                read_write=False,
            )
            container_gate.validate_containers([inspected], self.contract)

    def test_observability_exception_rejects_rw_or_wrong_destination(self) -> None:
        labels = {
            "com.docker.stack.namespace": "observability",
            "com.docker.swarm.service.name": "observability_cadvisor",
            "com.docker.swarm.task.id": "task-id",
            "com.docker.swarm.task.name": "observability_cadvisor.1.task-id",
        }
        for destination, read_write in (
            ("/rootfs", True),
            ("/wrong", False),
        ):
            inspected = self.container(
                "observer",
                labels=labels,
                source="/",
                destination=destination,
                read_write=read_write,
            )
            with self.assertRaises(container_gate.ContainerGateError):
                container_gate.validate_containers([inspected], self.contract)


class WorkloadNetworkIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stack = workload_validator.load_yaml(
            REPOSITORY_ROOT / ".build/workloads/stack.yml"
        )
        cls.services = workload_validator.load_yaml(
            REPOSITORY_ROOT / "config/services.yml"
        )
        cls.image_updates = workload_validator.load_yaml(
            REPOSITORY_ROOT / "config/workload-image-updates.yml"
        )
        cls.secrets = workload_validator.load_yaml(
            REPOSITORY_ROOT / "stacks/workloads/secrets.yml"
        )
        cls.platform = workload_validator.load_yaml(
            REPOSITORY_ROOT / "config/platform.yml"
        )
        source_reference = workload_validator.find_image(
            {item["id"]: item for item in cls.services["approved_services"]},
            "n8n",
            "runner",
        )
        cls.runner_metadata = runner_manager.describe(
            REPOSITORY_ROOT / "images/n8n-runners",
            source_reference,
        )

    def assert_rejected(self, candidate: dict[str, object]) -> None:
        with self.assertRaises(workload_validator.ContractError):
            workload_validator.validate_stack(
                candidate,
                self.services,
                self.image_updates,
                self.secrets,
                self.runner_metadata,
                self.platform,
            )

    def test_minecraft_and_selenium_have_dedicated_egress(self) -> None:
        workload_validator.validate_stack(
            self.stack,
            self.services,
            self.image_updates,
            self.secrets,
            self.runner_metadata,
            self.platform,
        )
        self.assertEqual(
            set(self.stack["services"]["minecraft"]["networks"]),
            {"minecraft-egress", "minecraft-monitoring"},
        )
        self.assertEqual(
            self.stack["networks"]["minecraft-monitoring"],
            {
                "name": "apptolast-minecraft-monitoring",
                "driver": "overlay",
                "internal": True,
                "driver_opts": {"encrypted": ""},
            },
        )
        self.assertEqual(
            set(self.stack["services"]["selenium"]["networks"]),
            {"n8n-browser", "selenium-egress"},
        )
        self.assertEqual(
            set(self.stack["services"]["n8n-runners"]["networks"]),
            {"n8n-runner-broker"},
        )
        self.assertEqual(
            self.stack["networks"]["n8n-browser"],
            {
                "name": "workloads_n8n-browser",
                "driver": "overlay",
                "internal": True,
                "driver_opts": {"encrypted": ""},
            },
        )
        self.assertEqual(
            self.stack["networks"]["n8n-runner-broker"],
            {
                "name": "workloads_n8n-runner-broker",
                "driver": "overlay",
                "internal": True,
                "driver_opts": {"encrypted": ""},
            },
        )

    def test_only_alberto_uses_the_exact_reviewed_tracked_tag(self) -> None:
        import copy

        alberto_reference = workload_validator.ALBERTO_TRACKED_REFERENCE
        alberto_catalog = next(
            item
            for item in self.services["approved_services"]
            if item["id"] == "personal-website-alberto"
        )
        self.assertEqual(
            alberto_catalog["images"][0]["reference"],
            workload_validator.ALBERTO_CATALOG_REFERENCE,
        )
        self.assertEqual(
            self.stack["services"]["portfolio-alberto"]["image"],
            alberto_reference,
        )
        workload_validator.validate_stack(
            self.stack,
            self.services,
            self.image_updates,
            self.secrets,
            self.runner_metadata,
            self.platform,
        )

        wrong_tag_stack = copy.deepcopy(self.stack)
        wrong_tag_stack["services"]["portfolio-alberto"]["image"] = (
            "docker.io/hgarciaalberto/personal-website:canary"
        )
        with self.assertRaisesRegex(
            workload_validator.ContractError,
            "image drift for portfolio-alberto",
        ):
            workload_validator.validate_stack(
                wrong_tag_stack,
                self.services,
                self.image_updates,
                self.secrets,
                self.runner_metadata,
                self.platform,
            )

        invalid_contracts = []
        for key, value in (
            ("catalog_service", "kropia"),
            ("component", "database"),
            ("swarm_service", "kropia"),
            (
                "catalog_reference",
                "docker.io/hgarciaalberto/personal-website@sha256:"
                + ("f" * 64),
            ),
            (
                "tracked_reference",
                "docker.io/hgarciaalberto/personal-website:canary",
            ),
            (
                "approved_runtime_reference",
                "docker.io/hgarciaalberto/personal-website:latest@sha256:invalid",
            ),
            (
                "approved_runtime_reference",
                "docker.io/example/other:latest@sha256:" + ("f" * 64),
            ),
            (
                "approved_runtime_reference",
                "docker.io/hgarciaalberto/personal-website:canary@sha256:"
                + ("f" * 64),
            ),
            ("update_policy", "floating-tag"),
        ):
            candidate = copy.deepcopy(self.image_updates)
            candidate["workload_image_updates"][0][key] = value
            invalid_contracts.append(candidate)

        missing_update = copy.deepcopy(self.image_updates)
        missing_update["workload_image_updates"] = []
        invalid_contracts.append(missing_update)

        extra_update = copy.deepcopy(self.image_updates)
        extra_update["workload_image_updates"].append(
            copy.deepcopy(extra_update["workload_image_updates"][0])
        )
        invalid_contracts.append(extra_update)

        wrong_schema = copy.deepcopy(self.image_updates)
        wrong_schema["workload_image_update_schema_version"] = 2
        invalid_contracts.append(wrong_schema)

        boolean_schema = copy.deepcopy(self.image_updates)
        boolean_schema["workload_image_update_schema_version"] = True
        invalid_contracts.append(boolean_schema)

        extra_key = copy.deepcopy(self.image_updates)
        extra_key["unexpected"] = True
        invalid_contracts.append(extra_key)

        missing_approved_reference = copy.deepcopy(self.image_updates)
        del missing_approved_reference["workload_image_updates"][0][
            "approved_runtime_reference"
        ]
        invalid_contracts.append(missing_approved_reference)

        unexpected_update_key = copy.deepcopy(self.image_updates)
        unexpected_update_key["workload_image_updates"][0]["unexpected"] = True
        invalid_contracts.append(unexpected_update_key)

        for candidate in invalid_contracts:
            with self.subTest(candidate=candidate):
                with self.assertRaises(workload_validator.ContractError):
                    workload_validator.validate_stack(
                        self.stack,
                        self.services,
                        candidate,
                        self.secrets,
                        self.runner_metadata,
                        self.platform,
                    )

        renewed_approval = copy.deepcopy(self.image_updates)
        renewed_approval["workload_image_updates"][0][
            "approved_runtime_reference"
        ] = (
            workload_validator.ALBERTO_TRACKED_REFERENCE
            + "@sha256:"
            + ("f" * 64)
        )
        workload_validator.validate_stack(
            self.stack,
            self.services,
            renewed_approval,
            self.secrets,
            self.runner_metadata,
            self.platform,
        )

        changed_catalog = copy.deepcopy(self.services)
        alberto = next(
            item
            for item in changed_catalog["approved_services"]
            if item["id"] == "personal-website-alberto"
        )
        alberto["images"][0]["reference"] = (
            "docker.io/hgarciaalberto/personal-website@sha256:" + ("f" * 64)
        )
        with self.assertRaisesRegex(
            workload_validator.ContractError,
            "catalog baseline changed",
        ):
            workload_validator.validate_stack(
                self.stack,
                changed_catalog,
                self.image_updates,
                self.secrets,
                self.runner_metadata,
                self.platform,
            )

        missing_image = copy.deepcopy(self.services)
        alberto = next(
            item
            for item in missing_image["approved_services"]
            if item["id"] == "personal-website-alberto"
        )
        alberto["images"] = []
        with self.assertRaisesRegex(
            workload_validator.ContractError,
            "ambiguous image",
        ):
            workload_validator.validate_stack(
                self.stack,
                missing_image,
                self.image_updates,
                self.secrets,
                self.runner_metadata,
                self.platform,
            )

        with mock.patch.dict(
            workload_validator.IMAGE_CONTRACT,
            {"portfolio-alberto": ("kropia", "app")},
        ):
            with self.assertRaisesRegex(
                workload_validator.ContractError,
                "stack-to-catalog mapping changed",
            ):
                workload_validator.validate_stack(
                    self.stack,
                    self.services,
                    self.image_updates,
                    self.secrets,
                    self.runner_metadata,
                    self.platform,
                )

        mutable_expected = copy.deepcopy(
            workload_validator.EXPECTED_IMAGE_UPDATE
        )
        mutable_expected["catalog_reference"] = alberto_reference
        mutable_expected["approved_runtime_reference"] = (
            workload_validator.ALBERTO_TRACKED_REFERENCE
            + "@sha256:"
            + ("f" * 64)
        )
        mutable_contract = {
            "workload_image_update_schema_version": 1,
            "workload_image_updates": [mutable_expected],
        }
        mutable_catalog = copy.deepcopy(self.services)
        alberto = next(
            item
            for item in mutable_catalog["approved_services"]
            if item["id"] == "personal-website-alberto"
        )
        alberto["images"][0]["reference"] = alberto_reference
        with mock.patch.object(
            workload_validator,
            "EXPECTED_IMAGE_UPDATE",
            mutable_expected,
        ):
            with self.assertRaisesRegex(
                workload_validator.ContractError,
                "baseline is not immutable",
            ):
                workload_validator.validate_stack(
                    self.stack,
                    mutable_catalog,
                    mutable_contract,
                    self.secrets,
                    self.runner_metadata,
                    self.platform,
                )

    def test_workload_image_update_yaml_rejects_duplicate_keys(self) -> None:
        invalid_documents = {
            "duplicate-top-level": (
                "---\n"
                "workload_image_update_schema_version: 1\n"
                "workload_image_update_schema_version: 1\n"
                "workload_image_updates: []\n"
            ),
            "duplicate-nested": (
                "---\n"
                "workload_image_update_schema_version: 1\n"
                "workload_image_updates:\n"
                "  - tracked_reference: first\n"
                "    tracked_reference: second\n"
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "updates.yml"
            for name, document in invalid_documents.items():
                with self.subTest(name=name):
                    candidate.write_text(document, encoding="utf-8")
                    with self.assertRaisesRegex(
                        workload_validator.ContractError,
                        "duplicate key",
                    ):
                        workload_validator.load_unique_yaml(candidate)

    def test_workload_image_update_yaml_rejects_invalid_documents(self) -> None:
        invalid_documents = {
            "malformed": "---\nkey: [\n",
            "not-a-mapping": "---\n- item\n",
            "unhashable-key": "---\n? [one, two]\n: value\n",
        }
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "updates.yml"
            for name, document in invalid_documents.items():
                with self.subTest(name=name):
                    candidate.write_text(document, encoding="utf-8")
                    with self.assertRaises(workload_validator.ContractError):
                        workload_validator.load_unique_yaml(candidate)

            missing = Path(temporary) / "missing.yml"
            with self.assertRaisesRegex(
                workload_validator.ContractError,
                "cannot load YAML",
            ):
                workload_validator.load_unique_yaml(missing)

    def test_exact_public_http_backend_edge_matrix(self) -> None:
        observed = set().union(
            *(
                {
                    service_name
                    for service_name, service in self.stack["services"].items()
                    if network_name in service.get("networks", [])
                }
                for network_name in workload_validator.EXPECTED_EDGE_NETWORKS
            )
        )
        self.assertEqual(
            observed,
            workload_validator.EXPECTED_EDGE_CONSUMERS,
        )
        self.assertEqual(
            set(self.stack["services"]["n8n"]["networks"]),
            {
                "edge-n8n",
                "n8n-backend",
                "n8n-browser",
                "n8n-coordination",
                "n8n-monitoring",
                "n8n-runner-broker",
            },
        )
        for logical_name, (
            external_name,
            expected_consumers,
        ) in workload_validator.EXPECTED_EDGE_NETWORKS.items():
            self.assertEqual(
                self.stack["networks"][logical_name],
                {"external": True, "name": external_name},
            )
            self.assertEqual(
                {
                    service_name
                    for service_name, service in self.stack["services"].items()
                    if logical_name in service.get("networks", [])
                },
                expected_consumers,
            )
        self.assertEqual(
            self.stack["networks"]["n8n-monitoring"],
            {
                "name": "apptolast-n8n-monitoring",
                "driver": "overlay",
                "internal": True,
                "driver_opts": {"encrypted": ""},
            },
        )
        self.assertEqual(
            {
                service_name
                for service_name, service in self.stack["services"].items()
                if "n8n-monitoring" in service.get("networks", [])
            },
            {"n8n"},
        )

    def test_cross_backend_or_extra_egress_consumer_is_rejected(self) -> None:
        import copy

        candidate = copy.deepcopy(self.stack)
        candidate["services"]["minecraft"]["networks"].append("edge-kropia")
        self.assert_rejected(candidate)

        candidate = copy.deepcopy(self.stack)
        candidate["services"]["kropia"]["networks"].append("selenium-egress")
        self.assert_rejected(candidate)

        candidate = copy.deepcopy(self.stack)
        candidate["services"]["selenium"]["networks"].append("n8n-monitoring")
        self.assert_rejected(candidate)

        candidate = copy.deepcopy(self.stack)
        candidate["services"]["selenium"]["networks"].append("n8n-backend")
        self.assert_rejected(candidate)

        candidate = copy.deepcopy(self.stack)
        candidate["services"]["n8n-runners"]["networks"].append("n8n-coordination")
        self.assert_rejected(candidate)

        candidate = copy.deepcopy(self.stack)
        candidate["services"]["n8n-runners"]["networks"].append("n8n-browser")
        self.assert_rejected(candidate)

    def test_minecraft_public_port_tracks_its_gate(self) -> None:
        import copy

        published = [
            {
                "target": 25565,
                "published": 25565,
                "protocol": "tcp",
                "mode": "host",
            }
        ]

        # El contrato vigente publica el puerto porque el propietario registro
        # su aceptacion explicita del modo offline.
        self.assertIs(
            self.platform["platform_minecraft_public_enabled"], True
        )
        self.assertIs(
            self.platform["platform_minecraft_offline_public_accepted"], True
        )
        self.assertEqual(
            self.stack["services"]["minecraft"]["ports"], published
        )
        workload_validator.validate_stack(
            self.stack,
            self.services,
            self.image_updates,
            self.secrets,
            self.runner_metadata,
            self.platform,
        )

        # Con la compuerta cerrada el puerto desaparece del stack renderizado.
        disabled_platform = copy.deepcopy(self.platform)
        disabled_platform["platform_minecraft_public_enabled"] = False
        disabled_platform["platform_dns_cutover"]["minecraft"] = False
        disabled_stack = copy.deepcopy(self.stack)
        del disabled_stack["services"]["minecraft"]["ports"]
        workload_validator.validate_stack(
            disabled_stack,
            self.services,
            self.image_updates,
            self.secrets,
            self.runner_metadata,
            disabled_platform,
        )

        # Y publicarlo con la compuerta cerrada sigue rechazandose, que es la
        # direccion que impide que ingress y DNS se separen en silencio.
        with self.assertRaises(workload_validator.ContractError):
            workload_validator.validate_stack(
                copy.deepcopy(self.stack),
                self.services,
                self.image_updates,
                self.secrets,
                self.runner_metadata,
                disabled_platform,
            )

    def test_n8n_security_switches_are_fail_closed(self) -> None:
        import copy

        environment = self.stack["services"]["n8n"]["environment"]
        self.assertEqual(environment["N8N_BLOCK_ENV_ACCESS_IN_NODE"], "true")
        self.assertEqual(
            environment["N8N_UNVERIFIED_PACKAGES_ENABLED"],
            "false",
        )
        candidate = copy.deepcopy(self.stack)
        candidate["services"]["n8n"]["environment"][
            "N8N_BLOCK_ENV_ACCESS_IN_NODE"
        ] = "false"
        self.assert_rejected(candidate)

        candidate = copy.deepcopy(self.stack)
        candidate["services"]["n8n"]["environment"]["EXECUTIONS_MODE"] = "queue"
        candidate["services"]["n8n"]["environment"][
            "QUEUE_BULL_REDIS_HOST"
        ] = "redis-coordinator"
        self.assert_rejected(candidate)


class WorkloadDeploymentIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stack = workload_validator.load_yaml(
            REPOSITORY_ROOT / ".build/workloads/stack.yml"
        )

    def deployment_fixture(
        self,
    ) -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
        dict[str, dict[str, str]],
        dict[str, dict[str, str]],
        dict[str, dict[str, object]],
    ]:
        verified_configs = {
            alias: {
                "id": f"config-id-{index}",
                "name": declaration["name"],
            }
            for index, (alias, declaration) in enumerate(self.stack["configs"].items())
        }
        verified_secrets = {
            alias: {
                "id": f"secret-id-{index}",
                "name": declaration["name"],
            }
            for index, (alias, declaration) in enumerate(self.stack["secrets"].items())
        }
        network_ids: dict[str, str] = {}
        inspected_networks: list[dict[str, object]] = []
        for index, (logical_name, declaration) in enumerate(
            self.stack["networks"].items()
        ):
            physical_name = declaration.get(
                "name",
                f"workloads_{logical_name}",
            )
            network_id = f"network-id-{index}"
            network_ids[logical_name] = network_id
            inspected_networks.append(
                {
                    "Id": network_id,
                    "Name": physical_name,
                    "Driver": "overlay",
                    "Scope": "swarm",
                    "Attachable": False,
                    "Internal": declaration.get("internal", False),
                    "Options": {"encrypted": ""},
                }
            )

        inspected_services: list[dict[str, object]] = []
        for logical_name, contract in self.stack["services"].items():
            configs = []
            for reference in contract.get("configs", []):
                alias = reference["source"]
                identity = verified_configs[alias]
                configs.append(
                    {
                        "ConfigID": identity["id"],
                        "ConfigName": identity["name"],
                        "File": {
                            "Name": reference.get("target", alias),
                            "UID": str(reference.get("uid", "0")),
                            "GID": str(reference.get("gid", "0")),
                            "Mode": reference.get("mode", 0o444),
                        },
                    }
                )
            secrets = []
            for reference in contract.get("secrets", []):
                alias = reference["source"]
                identity = verified_secrets[alias]
                secrets.append(
                    {
                        "SecretID": identity["id"],
                        "SecretName": identity["name"],
                        "File": {
                            "Name": reference.get("target", alias),
                            "UID": str(reference.get("uid", "0")),
                            "GID": str(reference.get("gid", "0")),
                            "Mode": reference.get("mode", 0o444),
                        },
                    }
                )
            inspected_services.append(
                {
                    "Spec": {
                        "Name": f"workloads_{logical_name}",
                        "TaskTemplate": {
                            "ContainerSpec": {
                                "Configs": configs,
                                "Secrets": secrets,
                            },
                            "Networks": [
                                {"Target": network_ids[network]}
                                for network in contract.get("networks", [])
                            ],
                        },
                    }
                }
            )
        verified_external_networks = {
            logical_name: {
                "id": network_ids[logical_name],
                "name": declaration["name"],
                "internal": declaration.get("internal", False),
            }
            for logical_name, declaration in self.stack["networks"].items()
            if declaration.get("external") is True
        }
        return (
            inspected_services,
            inspected_networks,
            verified_configs,
            verified_secrets,
            verified_external_networks,
        )

    def validate(
        self,
        inspected_services: list[dict[str, object]],
        inspected_networks: list[dict[str, object]],
        verified_configs: dict[str, dict[str, str]],
        verified_secrets: dict[str, dict[str, str]],
        verified_external_networks: dict[str, dict[str, object]],
    ) -> None:
        deployment_validator.validate_deployment(
            self.stack,
            "workloads",
            inspected_services,
            inspected_networks,
            verified_configs,
            verified_secrets,
            verified_external_networks,
        )

    def test_exact_verified_resource_references_are_accepted(self) -> None:
        self.validate(*self.deployment_fixture())

    def test_rollback_to_stale_config_id_is_rejected(self) -> None:
        import copy

        fixture = list(self.deployment_fixture())
        services = copy.deepcopy(fixture[0])
        n8n = next(item for item in services if item["Spec"]["Name"] == "workloads_n8n")
        n8n["Spec"]["TaskTemplate"]["ContainerSpec"]["Configs"][0][
            "ConfigID"
        ] = "stale-config-id"
        fixture[0] = services
        with self.assertRaisesRegex(
            deployment_validator.DeploymentContractError,
            "Config references differ",
        ):
            self.validate(*fixture)

    def test_extra_secret_or_network_reference_is_rejected(self) -> None:
        import copy

        fixture = list(self.deployment_fixture())
        services = copy.deepcopy(fixture[0])
        kropia = next(
            item for item in services if item["Spec"]["Name"] == "workloads_kropia"
        )
        first_secret = next(iter(fixture[3].values()))
        kropia["Spec"]["TaskTemplate"]["ContainerSpec"]["Secrets"].append(
            {
                "SecretID": first_secret["id"],
                "SecretName": first_secret["name"],
                "File": {
                    "Name": "unexpected",
                    "UID": "0",
                    "GID": "0",
                    "Mode": 0o400,
                },
            }
        )
        fixture[0] = services
        with self.assertRaisesRegex(
            deployment_validator.DeploymentContractError,
            "Secret references differ",
        ):
            self.validate(*fixture)

        fixture = list(self.deployment_fixture())
        services = copy.deepcopy(fixture[0])
        selenium = next(
            item for item in services if item["Spec"]["Name"] == "workloads_selenium"
        )
        selenium["Spec"]["TaskTemplate"]["Networks"].append(
            {"Target": "network-id-unreviewed"}
        )
        fixture[0] = services
        with self.assertRaisesRegex(
            deployment_validator.DeploymentContractError,
            "network references differ",
        ):
            self.validate(*fixture)

    def test_drifted_network_identity_is_rejected(self) -> None:
        import copy

        fixture = list(self.deployment_fixture())
        networks = copy.deepcopy(fixture[1])
        browser = next(
            item for item in networks if item["Name"] == "workloads_n8n-browser"
        )
        browser["Internal"] = False
        fixture[1] = networks
        with self.assertRaisesRegex(
            deployment_validator.DeploymentContractError,
            "network differs",
        ):
            self.validate(*fixture)


class N8nRunnerImageContractTests(unittest.TestCase):
    SOURCE_REFERENCE = (
        "ghcr.io/apptolast/migracionnetcup-n8n-runners@sha256:"
        "4d8f879e9f70df8173c3b9b40406582fc9b460e4f8afcf5dcf51d3bce0e5e1b5"
    )

    def test_context_identity_is_stable_and_local(self) -> None:
        metadata = runner_manager.describe(
            REPOSITORY_ROOT / "images/n8n-runners",
            self.SOURCE_REFERENCE,
        )
        self.assertRegex(metadata["context_sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(
            metadata["image_reference"],
            f"apptolast/n8n-runners:src-{metadata['context_sha256']}",
        )
        self.assertEqual(
            metadata["dependencies"],
            {"ioredis": "5.11.1", "pdf-lib": "1.17.1", "uuid": "11.1.1"},
        )

    def test_cli_locks_reconcile_but_keeps_describe_read_only(self) -> None:
        describe_args = runner_manager.argparse.Namespace(
            command="describe",
            context=Path("/reviewed/context"),
            source_reference=self.SOURCE_REFERENCE,
        )
        reconcile_args = runner_manager.argparse.Namespace(
            command="reconcile",
            context=Path("/reviewed/context"),
            source_reference=self.SOURCE_REFERENCE,
            expected_context_sha256="a" * 64,
        )
        with (
            mock.patch.object(
                runner_manager,
                "parse_args",
                return_value=describe_args,
            ),
            mock.patch.object(
                runner_manager,
                "describe",
                return_value={"command": "describe"},
            ),
            mock.patch.object(
                runner_manager,
                "ensure_mutation_lock",
            ) as ensure_lock,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(runner_manager.main(), 0)
        ensure_lock.assert_not_called()

        with (
            mock.patch.object(
                runner_manager,
                "parse_args",
                return_value=reconcile_args,
            ),
            mock.patch.object(
                runner_manager,
                "reconcile",
                return_value={"command": "reconcile"},
            ),
            mock.patch.object(
                runner_manager,
                "ensure_mutation_lock",
            ) as ensure_lock,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(runner_manager.main(), 0)
        ensure_lock.assert_called_once()
        self.assertEqual(
            ensure_lock.call_args.args[0],
            "n8n-runner-image-reconcile",
        )

    def test_context_tampering_changes_identity(self) -> None:
        source = REPOSITORY_ROOT / "images/n8n-runners"
        original = runner_manager.describe(source, self.SOURCE_REFERENCE)
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            for name in runner_manager.CONTEXT_FILES:
                shutil.copyfile(source / name, target / name)
            with (target / "package-lock.json").open(
                "a",
                encoding="utf-8",
            ) as handle:
                handle.write("\n")
            changed = runner_manager.describe(target, self.SOURCE_REFERENCE)
        self.assertNotEqual(
            original["context_sha256"],
            changed["context_sha256"],
        )

    def test_context_rejects_unreviewed_files(self) -> None:
        source = REPOSITORY_ROOT / "images/n8n-runners"
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            for name in runner_manager.CONTEXT_FILES:
                shutil.copyfile(source / name, target / name)
            (target / "unexpected").write_text("drift", encoding="utf-8")
            with self.assertRaises(runner_manager.RunnerImageError):
                runner_manager.describe(target, self.SOURCE_REFERENCE)

    def test_existing_image_requires_labels_and_exact_modules(self) -> None:
        metadata = runner_manager.describe(
            REPOSITORY_ROOT / "images/n8n-runners",
            self.SOURCE_REFERENCE,
        )
        source_reference = self.SOURCE_REFERENCE

        class FakeDocker:
            def __init__(self) -> None:
                self.commands: list[list[str]] = []

            def run(
                self,
                argv,
                *,
                check: bool = True,
            ) -> subprocess.CompletedProcess[str]:
                command = list(argv)
                self.commands.append(command)
                if command[1:3] == ["image", "inspect"]:
                    image = {
                        "Id": "sha256:" + ("a" * 64),
                        "Architecture": "amd64",
                        "Os": "linux",
                        "Config": {
                            "User": "runner",
                            "Labels": {
                                **runner_manager.LABELS,
                                "com.apptolast.context-sha256": (
                                    metadata["context_sha256"]
                                ),
                                "com.apptolast.catalog-source-reference": (
                                    source_reference
                                ),
                            },
                        },
                    }
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps([image]),
                        "",
                    )
                if any(
                    argument == f"--entrypoint={runner_manager.PYTHON_COMMAND}"
                    for argument in command
                ):
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(
                            {
                                "isolated": 1,
                                "dont_write_bytecode": True,
                                "disable_remote_debug": True,
                            }
                        ),
                        "",
                    )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(metadata["dependencies"]),
                    "",
                )

        docker = FakeDocker()
        reconciled = runner_manager.reconcile(
            REPOSITORY_ROOT / "images/n8n-runners",
            self.SOURCE_REFERENCE,
            metadata["context_sha256"],
            docker=docker,
        )
        self.assertFalse(reconciled["changed"])
        self.assertTrue(any(command[1] == "run" for command in docker.commands))
        self.assertFalse(
            any(command[1:3] == ["buildx", "build"] for command in docker.commands)
        )

    def test_python_runner_uses_exact_upstream_isolation_flags(self) -> None:
        config = json.loads(
            (
                REPOSITORY_ROOT / "stacks/workloads/config/n8n-task-runners.json"
            ).read_text(encoding="utf-8")
        )
        python_runner = next(
            item for item in config["task-runners"] if item["runner-type"] == "python"
        )
        self.assertEqual(
            python_runner["args"],
            list(runner_manager.PYTHON_HARDENING_ARGS),
        )

    def test_python_runner_probe_rejects_ineffective_isolation(self) -> None:
        class FakeDocker:
            def run(
                self,
                argv,
                *,
                check: bool = True,
            ) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    list(argv),
                    0,
                    json.dumps(
                        {
                            "isolated": 0,
                            "dont_write_bytecode": True,
                            "disable_remote_debug": True,
                        }
                    ),
                    "",
                )

        with self.assertRaises(runner_manager.RunnerImageError):
            runner_manager.verify_python_hardening(
                FakeDocker(),
                "apptolast/n8n-runners:test",
            )

    def test_python_runner_isolation_flag_drift_is_rejected(self) -> None:
        import copy

        stack = workload_validator.load_yaml(
            REPOSITORY_ROOT / ".build/workloads/stack.yml"
        )
        candidate = copy.deepcopy(stack)
        source_root = REPOSITORY_ROOT / "stacks/workloads/config"
        with tempfile.TemporaryDirectory() as temporary:
            target_root = Path(temporary)
            for source in source_root.iterdir():
                shutil.copyfile(source, target_root / source.name)
            target = target_root / "n8n-task-runners.json"
            config = json.loads(target.read_text(encoding="utf-8"))
            python_runner = next(
                item
                for item in config["task-runners"]
                if item["runner-type"] == "python"
            )
            python_runner["args"] = ["-m", "src.main"]
            target.write_text(
                json.dumps(config, indent=2) + "\n",
                encoding="utf-8",
            )
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            candidate["configs"]["n8n_task_runners"][
                "name"
            ] = f"workloads-n8n-task-runners-{digest[:16]}"
            with self.assertRaises(workload_validator.ContractError):
                workload_validator.validate_configs(candidate, target_root)


class WorkloadAnsibleIntegrationTests(unittest.TestCase):
    UPDATE = {
        "catalog_service": "personal-website-alberto",
        "component": "app",
        "swarm_service": "portfolio-alberto",
        "catalog_reference": workload_validator.ALBERTO_CATALOG_REFERENCE,
        "tracked_reference": workload_validator.ALBERTO_TRACKED_REFERENCE,
        "approved_runtime_reference": (
            workload_validator.ALBERTO_TRACKED_REFERENCE
            + "@sha256:"
            + ("a" * 64)
        ),
        "update_policy": "tracked-tag",
    }

    def assert_ansible_role_gate_rejects(
        self,
        role: str,
        task_name: str,
        variables: dict[str, object],
        expected_message: str,
    ) -> None:
        """Run one real fail-closed role task with no Docker-side effects."""
        ansible_playbook = REPOSITORY_ROOT / ".venv/bin/ansible-playbook"
        self.assertTrue(ansible_playbook.is_file(), "locked Ansible is required")
        with tempfile.TemporaryDirectory() as temporary:
            playbook = Path(temporary) / "gate.yml"
            playbook.write_text(
                yaml.safe_dump(
                    [
                        {
                            "name": "Exercise one fail-closed Ansible gate",
                            "hosts": "localhost",
                            "connection": "local",
                            "gather_facts": False,
                            "become": False,
                            "vars": variables,
                            "roles": [{"role": role}],
                        }
                    ],
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    str(ansible_playbook),
                    "-i",
                    "localhost,",
                    "--start-at-task",
                    task_name,
                    str(playbook),
                ],
                cwd=REPOSITORY_ROOT / "ansible",
                text=True,
                capture_output=True,
                check=False,
            )
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0, output)
        self.assertIn(expected_message, output)

    def test_real_ansible_gates_reject_invalid_tracked_image_state(self) -> None:
        import copy

        image_updates = [copy.deepcopy(self.UPDATE)]
        self.assert_ansible_role_gate_rejects(
            "image_preflight",
            "Verify the image catalog is available",
            {
                "approved_services": [],
                "internal_platform": {"observability": {"components": []}},
                "workload_image_update_schema_version": 1,
                "workload_image_updates": [],
            },
            "The reviewed image catalog is missing or unsupported.",
        )
        invalid_runtime_approval = copy.deepcopy(self.UPDATE)
        invalid_runtime_approval["approved_runtime_reference"] = (
            "docker.io/hgarciaalberto/personal-website:canary@sha256:"
            + ("b" * 64)
        )
        self.assert_ansible_role_gate_rejects(
            "image_preflight",
            "Verify the image catalog is available",
            {
                "approved_services": [],
                "internal_platform": {"observability": {"components": []}},
                "image_preflight_expected_os": "linux",
                "image_preflight_expected_architecture": "amd64",
                "image_preflight_include_tracked_updates": True,
                "workload_image_update_schema_version": 1,
                "workload_image_updates": [invalid_runtime_approval],
            },
            "The reviewed image catalog is missing or unsupported.",
        )
        self.assert_ansible_role_gate_rejects(
            "image_preflight",
            "Bind the tracked image policy to its immutable catalog baseline",
            {
                "approved_services": [
                    {
                        "id": "personal-website-alberto",
                        "images": [
                            {
                                "component": "app",
                                "reference": self.UPDATE["catalog_reference"],
                            },
                            {
                                "component": "app",
                                "reference": "docker.io/example@sha256:"
                                + ("b" * 64),
                            },
                        ],
                    }
                ],
                "workload_image_updates": image_updates,
            },
            "does not match the immutable catalog baseline",
        )
        self.assert_ansible_role_gate_rejects(
            "image_preflight",
            "Enforce the complete tracked runtime reference map",
            {
                "image_preflight_include_tracked_updates": True,
                "image_preflight_tracked_references": [
                    self.UPDATE["tracked_reference"]
                ],
                "image_preflight_tracked_runtime_references": {
                    self.UPDATE["tracked_reference"]: (
                        workload_validator.ALBERTO_TRACKED_REFERENCE
                        + "@sha256:"
                        + ("b" * 64)
                    )
                },
                "workload_image_updates": image_updates,
            },
            "The tracked runtime image did not resolve fail-closed.",
        )
        self.assert_ansible_role_gate_rejects(
            "image_preflight",
            "Keep the tracked runtime map empty outside workload operations",
            {
                "image_preflight_include_tracked_updates": False,
                "image_preflight_tracked_runtime_references": {
                    self.UPDATE["tracked_reference"]: self.UPDATE[
                        "approved_runtime_reference"
                    ]
                },
            },
            "A tracked runtime image escaped its workload operation scope.",
        )
        self.assert_ansible_role_gate_rejects(
            "workloads",
            "Validate top-level workload inputs",
            {
                "schema_version": 1,
                "catalog_version": "1.0.0",
                "target": {"services_root": "/services"},
                "workloads_state_root": "/services",
                "workloads_stack_name": "workloads",
                "platform_minecraft_public_enabled": True,
                "platform_dns_cutover": {"minecraft": True},
                "platform_public_tcp_ports": [80, 443, 25565],
                "workloads_edge_networks": {
                    "kropia": "apptolast-edge-kropia",
                    "minecraft-stats": "apptolast-edge-minecraft-stats",
                    "n8n": "apptolast-edge-n8n",
                    "openclaw": "apptolast-edge-openclaw",
                    "passbolt": "apptolast-edge-passbolt",
                    "portfolio-alberto": "apptolast-edge-portfolio-alberto",
                    "portfolio-pablo": "apptolast-edge-portfolio-pablo",
                    "shlink": "apptolast-edge-shlink",
                },
                "workloads_secret_catalog_version": 1,
                "workloads_secret_version": "v1",
                "approved_services": [
                    {"id": service}
                    for service in [
                        "kropia",
                        "minecraft",
                        "minecraft-stats",
                        "n8n",
                        "openclaw-clean",
                        "passbolt",
                        "personal-website-alberto",
                        "personal-website-pablo",
                        "shlink",
                        "traefik-edge",
                    ]
                ],
                "denied_services": [],
                "workload_image_update_schema_version": 1,
                "workload_image_updates": [],
            },
            "outside the approved workloads contract.",
        )
        self.assert_ansible_role_gate_rejects(
            "workloads",
            "Enforce the preflighted Alberto runtime identity",
            {
                "workloads_portfolio_alberto_catalog_image": self.UPDATE[
                    "catalog_reference"
                ],
                "workloads_portfolio_alberto_image_update": self.UPDATE,
                "workloads_render_only": False,
                "image_preflight_tracked_runtime_references": {
                    self.UPDATE["tracked_reference"]: (
                        workload_validator.ALBERTO_TRACKED_REFERENCE
                        + "@sha256:"
                        + ("b" * 64)
                    )
                },
            },
            "Alberto's mutable runtime tag was not resolved to one preflighted digest.",
        )

    def test_image_preflight_normalizes_tagged_digests_exactly(self) -> None:
        import re

        defaults = yaml.safe_load(
            (
                REPOSITORY_ROOT / "ansible/roles/image_preflight/defaults/main.yml"
            ).read_text(encoding="utf-8")
        )
        source = "docker.io/grafana/alloy:v1.11.0@sha256:" + ("a" * 64)
        expected = "docker.io/grafana/alloy@sha256:" + ("a" * 64)
        self.assertEqual(
            re.sub(
                defaults["image_preflight_tag_digest_pattern"],
                defaults["image_preflight_digest_replacement"],
                source,
            ),
            expected,
        )

    def test_image_preflight_tracks_only_albertos_exact_latest_tag(self) -> None:
        image_updates = workload_validator.load_unique_yaml(
            REPOSITORY_ROOT / "config/workload-image-updates.yml"
        )
        self.assertEqual(
            image_updates["workload_image_updates"],
            [
                {
                    "catalog_service": "personal-website-alberto",
                    "component": "app",
                    "swarm_service": "portfolio-alberto",
                    "catalog_reference": (
                        workload_validator.ALBERTO_CATALOG_REFERENCE
                    ),
                    "tracked_reference": (
                        "docker.io/hgarciaalberto/personal-website:latest"
                    ),
                    "approved_runtime_reference": (
                        "docker.io/hgarciaalberto/personal-website:latest@"
                        "sha256:34c6854a3d7ff179e8fee8207696b1940747e84e9782d"
                        "2133417f17b60602f8d"
                    ),
                    "update_policy": "tracked-tag",
                }
            ],
        )
        tasks_path = (
            REPOSITORY_ROOT / "ansible/roles/image_preflight/tasks/main.yml"
        )
        tasks_text = tasks_path.read_text(encoding="utf-8")
        tasks = yaml.safe_load(tasks_text)
        catalog_digest = workload_validator.ALBERTO_CATALOG_REFERENCE.split(
            "@sha256:"
        )[1]
        self.assertIn(catalog_digest[:32], tasks_text)
        self.assertIn(catalog_digest[32:], tasks_text)
        self.assertIn(workload_validator.ALBERTO_TRACKED_REFERENCE, tasks_text)
        self.assertIn("approved_runtime_reference", tasks_text)
        policy_gate = next(
            item
            for item in tasks
            if item["name"]
            == "Bind the tracked image policy to its immutable catalog baseline"
        )
        self.assertIn(
            "immutable catalog baseline",
            policy_gate["ansible.builtin.assert"]["fail_msg"],
        )
        catalog_collector = next(
            item
            for item in tasks
            if item["name"]
            == "Collect approved immutable remote runtime images"
        )
        tracked_exclusion = catalog_collector["when"][1]
        self.assertIn(
            "image_preflight_include_tracked_updates",
            tracked_exclusion,
        )
        self.assertIn("catalog_service", tracked_exclusion)
        self.assertIn("component", tracked_exclusion)
        descriptor = next(
            item
            for item in tasks
            if item["name"]
            == "Read the exact tracked tag descriptor from its registry"
        )
        self.assertEqual(
            descriptor["ansible.builtin.command"]["argv"][1:4],
            ["buildx", "imagetools", "inspect"],
        )
        self.assertEqual(
            descriptor["ansible.builtin.command"]["argv"][5],
            "{{ '{{json .Manifest}}' }}",
        )
        self.assertEqual(
            descriptor["loop"],
            "{{ image_preflight_tracked_references }}",
        )
        self.assertEqual(
            descriptor["when"],
            [
                "image_preflight_include_tracked_updates | bool",
            ],
        )
        self.assertFalse(descriptor["check_mode"])
        resolver = next(
            item
            for item in tasks
            if item["name"]
            == "Resolve the tracked registry descriptor to an immutable reference"
        )
        self.assertEqual(
            resolver["ansible.builtin.command"]["argv"][1],
            "{{ playbook_dir }}/../../scripts/resolve-tracked-image.py",
        )
        self.assertEqual(resolver["ansible.builtin.command"]["argv"][2], "resolve")
        self.assertIn(
            "--approved-reference",
            resolver["ansible.builtin.command"]["argv"],
        )
        self.assertEqual(resolver["delegate_to"], "localhost")
        self.assertFalse(resolver["become"])
        self.assertFalse(resolver["check_mode"])
        self.assertEqual(
            resolver["when"],
            ["item.item in image_preflight_tracked_references"],
        )
        recorder = next(
            item
            for item in tasks
            if item["name"]
            == "Record the immutable runtime reference for the tracked image"
        )
        self.assertIn(
            "image_preflight_tracked_runtime_references",
            recorder["ansible.builtin.set_fact"],
        )
        resolution_gate = next(
            item
            for item in tasks
            if item["name"]
            == "Enforce the complete tracked runtime reference map"
        )
        self.assertIn(
            "fail_msg",
            resolution_gate["ansible.builtin.assert"],
        )
        self.assertNotIn("fail_msg", resolution_gate)
        self.assertNotIn("not ansible_check_mode", resolution_gate.get("when", []))
        runtime_report = next(
            item
            for item in tasks
            if item["name"] == "Report the reviewed tracked runtime reference"
        )
        self.assertIn(
            "approved for this operation",
            runtime_report["ansible.builtin.debug"]["msg"],
        )
        pull = next(
            item
            for item in tasks
            if item["name"]
            == "Pull the resolved tracked image before any stack mutation"
        )
        self.assertEqual(
            pull["loop"],
            "{{ image_preflight_tracked_resolved_references }}",
        )
        self.assertEqual(
            pull["when"],
            [
                "not ansible_check_mode",
                "image_preflight_include_tracked_updates | bool",
            ],
        )
        self.assertEqual(
            pull["ansible.builtin.command"]["argv"][3:5],
            [
                "--platform",
                (
                    "{{ image_preflight_expected_os\n"
                    "   }}/{{ image_preflight_expected_architecture }}"
                ),
            ],
        )
        self.assertIn("Downloaded newer image", pull["changed_when"])
        verifier = next(
            item
            for item in tasks
            if item["name"]
            == "Verify the tracked local image against its resolved registry digest"
        )
        self.assertEqual(
            verifier["ansible.builtin.command"]["argv"][2],
            "verify",
        )
        self.assertEqual(verifier["delegate_to"], "localhost")
        self.assertFalse(verifier["become"])
        self.assertEqual(
            verifier["when"],
            [
                "not ansible_check_mode",
                "item.item in image_preflight_tracked_resolved_references",
            ],
        )
        for playbook_name in (
            "edge",
            "observability",
            "preflight-images",
            "render-workloads",
            "site",
            "workloads",
        ):
            with self.subTest(playbook=playbook_name):
                play = yaml.safe_load(
                    (
                        REPOSITORY_ROOT
                        / f"ansible/playbooks/{playbook_name}.yml"
                    ).read_text(encoding="utf-8")
                )[0]
                self.assertIn(
                    "../../config/workload-image-updates.yml",
                    play["vars_files"],
                )
        for playbook_name, expected in (
            ("edge", False),
            ("observability", False),
            ("preflight-images", True),
            ("site", True),
            ("workloads", True),
        ):
            with self.subTest(tracked_scope=playbook_name):
                play = yaml.safe_load(
                    (
                        REPOSITORY_ROOT
                        / f"ansible/playbooks/{playbook_name}.yml"
                    ).read_text(encoding="utf-8")
                )[0]
                role = next(
                    item for item in play["roles"] if item["role"] == "image_preflight"
                )
                self.assertIs(
                    role.get("image_preflight_include_tracked_updates", False),
                    expected,
                )

    def test_workloads_deploys_the_preflighted_tracked_digest_exactly(
        self,
    ) -> None:
        derive = yaml.safe_load(
            (
                REPOSITORY_ROOT / "ansible/roles/workloads/tasks/derive.yml"
            ).read_text(encoding="utf-8")
        )
        image_derivation = next(
            item
            for item in derive
            if item["name"]
            == "Derive workload images from the catalog and reviewed update policy"
        )
        alberto_image = image_derivation["ansible.builtin.set_fact"][
            "workloads_images"
        ]["portfolio_alberto"]
        self.assertIn(
            "image_preflight_tracked_runtime_references",
            alberto_image,
        )
        self.assertNotIn("ansible_check_mode", alberto_image)
        self.assertIn("workloads_render_only", alberto_image)

        deploy = yaml.safe_load(
            (
                REPOSITORY_ROOT / "ansible/roles/workloads/tasks/deploy.yml"
            ).read_text(encoding="utf-8")
        )
        identity_gate = next(
            item
            for item in deploy
            if item["name"]
            == "Verify deployed workload image and placement identities"
        )
        image_assertion = identity_gate["ansible.builtin.assert"]["that"][0]
        self.assertIn("item.item.value", image_assertion)
        self.assertIn("^docker[.]io/", image_assertion)
        self.assertNotIn("portfolio-alberto", image_assertion)

    def test_site_role_order_is_safe_and_backup_is_explicitly_separate(self) -> None:
        site = yaml.safe_load(
            (REPOSITORY_ROOT / "ansible/playbooks/site.yml").read_text(encoding="utf-8")
        )[0]
        roles = [item["role"] for item in site["roles"]]
        self.assertEqual(
            roles,
            [
                "operation_lock_guard",
                "capacity_preflight",
                "host_security",
                "minecraft_preflight",
                "platform",
                "host_baseline",
                "image_preflight",
                "workloads_runner_image",
                "edge",
                "workloads",
                "observability",
                "deployment_metadata",
            ],
        )
        self.assertNotIn("backup", roles)
        self.assertIn("../../config/host-security.yml", site["vars_files"])
        self.assertIn(
            "../../stacks/workloads/secrets.yml",
            site["vars_files"],
        )
        self.assertIn(
            "../../stacks/observability/secrets.yml",
            site["vars_files"],
        )
        self.assertIn(
            "../../config/workload-image-updates.yml",
            site["vars_files"],
        )

    def test_mutating_standalone_playbooks_record_provenance(self) -> None:
        for name in ("workloads", "observability", "backup"):
            play = yaml.safe_load(
                (REPOSITORY_ROOT / f"ansible/playbooks/{name}.yml").read_text(
                    encoding="utf-8"
                )
            )[0]
            roles = [item["role"] for item in play["roles"]]
            self.assertEqual(roles[-1], "deployment_metadata")
            if name in ("workloads", "observability"):
                expected_prefix = (
                    [
                        "operation_lock_guard",
                        "capacity_preflight",
                        "minecraft_preflight",
                        "image_preflight",
                        "workloads_runner_image",
                    ]
                    if name == "workloads"
                    else [
                        "operation_lock_guard",
                        "capacity_preflight",
                        "image_preflight",
                        "workloads_runner_image",
                    ]
                )
                self.assertEqual(roles[: len(expected_prefix)], expected_prefix)

    def test_deployment_wrapper_exposes_all_explicit_targets(self) -> None:
        result = subprocess.run(
            [REPOSITORY_ROOT / "scripts/deploy-ansible.sh", "--help"],
            check=True,
            capture_output=True,
            text=True,
        )
        for target in (
            "preflight-images",
            "workloads",
            "observability",
            "backup",
            "site",
        ):
            self.assertIn(target, result.stdout)
        self.assertIn("Backup is deliberately separate from site", result.stdout)

    def test_deployment_identity_hashes_every_runtime_contract(self) -> None:
        wrapper = (REPOSITORY_ROOT / "scripts/deploy-ansible.sh").read_text(
            encoding="utf-8"
        )
        validator = (
            REPOSITORY_ROOT / "scripts/validate-deployment-metadata.py"
        ).read_text(encoding="utf-8")
        for relative_path in (
            "config/capacity.yml",
            "config/host-security.yml",
            "config/minecraft.yml",
            "config/platform.yml",
            "config/services.yml",
            "config/workload-image-updates.yml",
            "stacks/observability/secrets.yml",
            "stacks/workloads/secrets.yml",
        ):
            with self.subTest(relative_path=relative_path):
                self.assertIn(relative_path, wrapper)
                self.assertIn(relative_path, validator)

    def test_host_security_has_independent_deployment_provenance(self) -> None:
        defaults = yaml.safe_load(
            (
                REPOSITORY_ROOT / "ansible/roles/deployment_metadata/defaults/main.yml"
            ).read_text(encoding="utf-8")
        )
        mapping = defaults["deployment_metadata_components_by_playbook"]
        self.assertEqual(mapping["platform"][:2], ["host-security", "platform"])
        self.assertEqual(
            mapping["host-baseline"][:2],
            ["host-security", "host-baseline"],
        )
        self.assertEqual(mapping["site"][0], "host-security")
        validator = (
            REPOSITORY_ROOT / "scripts/validate-deployment-metadata.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"host-security"', validator)

    def test_check_mode_describes_images_without_mutating_workloads(self) -> None:
        runner_tasks = (
            REPOSITORY_ROOT / "ansible/roles/workloads_runner_image/tasks/main.yml"
        ).read_text(encoding="utf-8")
        workloads_main = (
            REPOSITORY_ROOT / "ansible/roles/workloads/tasks/main.yml"
        ).read_text(encoding="utf-8")
        workloads_render = (
            REPOSITORY_ROOT / "ansible/roles/workloads/tasks/render.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("when: not ansible_check_mode", runner_tasks)
        self.assertIn("- not ansible_check_mode", workloads_main)
        self.assertIn("workloads_check_mode_stack", workloads_render)
        self.assertIn('--compose-file\n      - "-"', workloads_render)

    def test_post_deploy_gate_covers_health_diagnostics_data_and_smokes(
        self,
    ) -> None:
        deploy = (
            REPOSITORY_ROOT / "ansible/roles/workloads/tasks/deploy.yml"
        ).read_text(encoding="utf-8")
        derive = (
            REPOSITORY_ROOT / "ansible/roles/workloads/tasks/derive.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("docker\n      - service\n      - ps", deploy)
        self.assertIn('State.Health.Status == "healthy"', deploy)
        self.assertIn("workloads_database_table_counts", deploy)
        self.assertIn("workloads_vector_versions", deploy)
        self.assertIn("--resolve", deploy)
        self.assertNotIn('socket.create_connection(("127.0.0.1", 25565)', deploy)
        self.assertIn("platform_minecraft_public_enabled", deploy)
        prometheus = (
            REPOSITORY_ROOT / "stacks/observability/config/prometheus.yml.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("workloads_minecraft:25565", prometheus)
        derived = yaml.safe_load(derive)
        smoke_task = next(
            task
            for task in derived
            if task.get("name") == "Derive exact post-deployment smoke targets"
        )
        targets = smoke_task["ansible.builtin.set_fact"]["workloads_http_smoke_targets"]
        self.assertEqual(len(targets), 8)
        self.assertEqual(
            {item["service"] for item in targets},
            workload_validator.EXPECTED_EDGE_CONSUMERS,
        )

    def test_post_deploy_gate_binds_verified_resource_ids_exactly(self) -> None:
        render = (
            REPOSITORY_ROOT / "ansible/roles/workloads/tasks/render.yml"
        ).read_text(encoding="utf-8")
        deploy = (
            REPOSITORY_ROOT / "ansible/roles/workloads/tasks/deploy.yml"
        ).read_text(encoding="utf-8")
        validator = (
            REPOSITORY_ROOT / "scripts/validate-swarm-deployment.py"
        ).read_text(encoding="utf-8")
        self.assertIn("validate-swarm-deployment.py", render)
        self.assertIn("--verified-configs", deploy)
        self.assertIn("--verified-secrets", deploy)
        self.assertIn("--verified-external-networks", deploy)
        self.assertIn("ConfigID", validator)
        self.assertIn("ConfigName", validator)
        self.assertIn("SecretID", validator)
        self.assertIn("SecretName", validator)
        self.assertIn('task_template.get("Networks")', validator)


if __name__ == "__main__":
    unittest.main()
