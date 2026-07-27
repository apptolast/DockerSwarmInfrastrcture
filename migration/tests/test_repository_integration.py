from __future__ import annotations

from pathlib import Path
import sys
import unittest

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_ROOT = REPOSITORY_ROOT / "migration"
sys.path.insert(0, str(MIGRATION_ROOT / "scripts"))

import prepare_runtime  # noqa: E402


class RepositoryIntegrationTests(unittest.TestCase):
    def test_secret_files_match_workload_catalog_exactly(self) -> None:
        catalog = yaml.safe_load(
            (REPOSITORY_ROOT / "stacks/workloads/secrets.yml").read_text(
                encoding="utf-8"
            )
        )
        entries = catalog["workloads_secrets"]
        source_files = {entry["source_file"] for entry in entries}
        materialized = set(prepare_runtime.DIRECT_SECRET_NAMES)
        for mapping in prepare_runtime.SECRET_FILE_NAMES.values():
            materialized.update(mapping.values())
        self.assertEqual(materialized, source_files)

        required = {
            entry["source_file"] for entry in entries if entry["required_nonempty"]
        }
        self.assertEqual(
            required,
            set(prepare_runtime.DIRECT_SECRET_NAMES)
            | prepare_runtime.REQUIRED_IMPORTED_SECRET_NAMES,
        )

    def test_service_dataset_paths_match_catalog_exactly(self) -> None:
        catalog = yaml.safe_load(
            (REPOSITORY_ROOT / "config/services.yml").read_text(encoding="utf-8")
        )
        services_root = Path(catalog["target"]["services_root"])
        self.assertEqual(
            services_root,
            prepare_runtime.DEFAULT_SERVICES_ROOT,
        )
        observed = {}
        for dataset in catalog["datasets"]:
            target = Path(dataset["target_path"])
            if target.is_relative_to(services_root):
                observed[dataset["id"]] = target.relative_to(services_root).as_posix()
        self.assertEqual(
            observed,
            prepare_runtime.SERVICE_DATASET_DIRECTORIES,
        )

        traefik = next(
            item for item in catalog["datasets"] if item["id"] == "traefik-acme"
        )
        self.assertEqual(
            traefik["target_path"],
            "/srv/dockerswarm/traefik",
        )


if __name__ == "__main__":
    unittest.main()
