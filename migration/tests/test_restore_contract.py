from __future__ import annotations

from pathlib import Path
import json
import re
import subprocess
import tempfile
import unittest

import yaml

MIGRATION_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = MIGRATION_ROOT.parent
COMPOSE_PATH = MIGRATION_ROOT / "compose" / "restore.yml"
VECTOR_OVERRIDE_PATH = MIGRATION_ROOT / "compose" / "restore-vectors-081.yml"
RESTORE_SCRIPT = MIGRATION_ROOT / "scripts" / "restore_databases.sh"
WORKLOADS_TEMPLATE_PATH = REPOSITORY_ROOT / "stacks/workloads/stack.yml.j2"


class RestoreComposeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = COMPOSE_PATH.read_text(encoding="utf-8")
        cls.document = yaml.safe_load(cls.raw)
        cls.services = cls.document["services"]

    def test_restore_contains_only_databases_and_n8n_maintenance(self) -> None:
        self.assertEqual(
            set(self.services),
            {"n8n-db", "n8n-maintenance", "passbolt-db", "shlink-db"},
        )
        self.assertEqual(
            self.services["n8n-maintenance"]["profiles"],
            ["maintenance"],
        )
        self.assertNotIn("openclaw", self.raw.lower())
        self.assertNotIn("traefik", self.raw.lower())

    def test_restore_cannot_publish_ports(self) -> None:
        for name, service in self.services.items():
            with self.subTest(service=name):
                self.assertNotIn("ports", service)
                self.assertNotIn("network_mode", service)
                self.assertIsNot(service.get("privileged"), True)
        for network in self.document["networks"].values():
            self.assertIs(network.get("internal"), True)

    def test_images_are_immutable_and_runtime_root_is_canonical(self) -> None:
        for name, service in self.services.items():
            with self.subTest(service=name):
                self.assertRegex(
                    service["image"],
                    r"@sha256:[0-9a-f]{64}$",
                )
                self.assertNotIn("latest", service["image"].lower())
        self.assertNotIn("/srv/apptolast", self.raw)
        self.assertIn("/srv/dockerswarm/services", self.raw)
        self.assertNotIn("env_file", self.raw)
        self.assertNotIn("docker.sock", self.raw)

    def test_secret_sources_are_individual_files(self) -> None:
        for name, secret in self.document["secrets"].items():
            with self.subTest(secret=name):
                self.assertEqual(
                    secret["file"],
                    (
                        "${SERVICES_ROOT:-/srv/dockerswarm/services}"
                        f"/secrets/files/{name}"
                    ),
                )

    def test_vector_restore_override_is_exactly_081(self) -> None:
        override = yaml.safe_load(VECTOR_OVERRIDE_PATH.read_text(encoding="utf-8"))
        image = override["services"]["n8n-db"]["image"]
        self.assertEqual(
            image,
            "docker.io/pgvector/pgvector@"
            "sha256:33198da2828a14c30348d2ccb4750833d5ed9a44c88d840a0e"
            "523d7417120337",
        )

    def test_maintenance_uses_production_n8n_storage_migration_setting(
        self,
    ) -> None:
        variable = "N8N_MIGRATE_FS_STORAGE_PATH"
        restore_value = self.services["n8n-maintenance"]["environment"][variable]
        production_template = WORKLOADS_TEMPLATE_PATH.read_text(encoding="utf-8")
        service_match = re.search(
            r"(?ms)^  n8n:\n(?P<body>.*?)(?=^  [a-z0-9_-]+:\n|\Z)",
            production_template,
        )
        self.assertIsNotNone(service_match)
        value_match = re.search(
            rf'(?m)^      {variable}: "([^"]+)"$',
            service_match.group("body"),
        )
        self.assertIsNotNone(value_match)
        self.assertEqual(restore_value, value_match.group(1))
        self.assertEqual(restore_value, "true")


class RestoreScriptContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = RESTORE_SCRIPT.read_text(encoding="utf-8")

    def test_n8n_workflows_are_audited_and_left_quiescent(self) -> None:
        self.assertIn("'activeVersionId'", self.script)
        self.assertIn("unpublish:workflow --all", self.script)
        self.assertIn("n8n-active-workflows.json", self.script)
        self.assertIn("n8n-workflows-quiesced", self.script)
        self.assertNotIn("exec n8n publish:workflow", self.script)
        self.assertRegex(
            self.script,
            re.compile(r"activate-n8n\).*?fail ", re.DOTALL),
        )

    def test_restore_refuses_to_overwrite_populated_databases(self) -> None:
        self.assertIn("ensure_empty_database", self.script)
        self.assertIn("no se sobrescribe", self.script)
        self.assertIn("--single-transaction", self.script)
        self.assertIn("--exit-on-error", self.script)

    def test_partial_restore_is_fingerprinted_and_resume_safe(self) -> None:
        self.assertIn("write_database_marker", self.script)
        self.assertIn("validate_database_marker", self.script)
        self.assertIn('"dumpSha256"', self.script)
        self.assertIn(
            'document["tableCount"] != int(sys.argv[4])',
            self.script,
        )
        self.assertIn("Importación n8n previa adoptada", self.script)
        self.assertIn("restore_core_database n8n-db n8n n8n", self.script)

    def test_vector_restore_markers_bind_dump_schema_and_live_database(
        self,
    ) -> None:
        self.assertIn("write_vector_database_marker", self.script)
        self.assertIn("validate_vector_database_marker", self.script)
        self.assertIn('"schemaSha256"', self.script)
        self.assertIn('"vectorExtensionVersion"', self.script)
        self.assertIn("database_schema_sha256", self.script)
        self.assertNotIn("write_marker database-vectors-081", self.script)
        self.assertNotIn("write_marker database-rag-082", self.script)
        vectors_resume = re.search(
            r"(?ms)^restore_vectors_081\(\).*?(?=^restore_rag_082\(\))",
            self.script,
        )
        rag_resume = re.search(
            r"(?ms)^restore_rag_082\(\).*?(?=^case )",
            self.script,
        )
        self.assertIsNotNone(vectors_resume)
        self.assertIsNotNone(rag_resume)
        self.assertIn(
            "validate_vector_database_marker",
            vectors_resume.group(0),
        )
        self.assertGreaterEqual(
            rag_resume.group(0).count("validate_vector_database_marker"),
            3,
        )

    def test_vector_marker_writer_accepts_both_locked_pgvector_versions(
        self,
    ) -> None:
        function = re.search(
            r"(?ms)^write_vector_database_marker\(\) \{.*?^\}",
            self.script,
        )
        self.assertIsNotNone(function)
        harness = (
            "set -Eeuo pipefail\n"
            "fail() { printf 'ERROR: %s\\n' \"$*\" >&2; exit 1; }\n"
            f"{function.group(0)}\n"
            'state_dir="$1"\n'
            "write_vector_database_marker "
            "database-vectors-081 vectors "
            f"{'a' * 64} 3 {'b' * 64} 0.8.1\n"
            "write_vector_database_marker "
            "database-rag-082 rag "
            f"{'c' * 64} 4 {'d' * 64} 0.8.2\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(
                ["bash", "-c", harness, "marker-test", temporary],
                check=True,
                capture_output=True,
                text=True,
            )
            for phase, version in (
                ("database-vectors-081", "0.8.1"),
                ("database-rag-082", "0.8.2"),
            ):
                marker = json.loads(
                    (Path(temporary) / f"{phase}.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    marker["vectorExtensionVersion"],
                    version,
                )

    def test_maintenance_reads_as_root_then_drops_to_image_user(self) -> None:
        self.assertIn("--user 0:0", self.script)
        self.assertIn("--cap-add SETUID", self.script)
        self.assertIn("--cap-add SETGID", self.script)
        self.assertIn(
            'exec su node -s /bin/sh -c "exec n8n unpublish:workflow --all"',
            self.script,
        )


if __name__ == "__main__":
    unittest.main()
