"""Safety and retention tests for immutable Docker Config garbage collection."""

from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(
        name,
        REPOSITORY_ROOT / relative_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config_gc = load_script(
    "gc_docker_configs",
    "scripts/gc-docker-configs.py",
)


def managed_config(
    stack: str,
    series: str,
    generation: int,
    *,
    kind: str,
) -> dict[str, object]:
    digest_character = format(generation, "x")
    digest = (digest_character * 64)[:64]
    return {
        "ID": f"{stack}-config-id-{generation}",
        "CreatedAt": f"2026-07-{generation:02d}T12:00:00.000000000Z",
        "Spec": {
            "Name": f"{series}-{digest[:16]}",
            "Labels": {
                "com.apptolast.managed-by": "ansible",
                "com.apptolast.stack": stack,
                "com.apptolast.kind": kind,
                "com.apptolast.sha256": digest,
            },
        },
    }


def service(
    name: str,
    *,
    current: list[str] | None = None,
    previous: list[str] | None = None,
) -> dict[str, object]:
    def spec(config_ids: list[str]) -> dict[str, object]:
        return {
            "Name": name,
            "TaskTemplate": {
                "ContainerSpec": {
                    "Configs": [
                        {
                            "ConfigID": config_id,
                            "ConfigName": f"name-for-{config_id}",
                        }
                        for config_id in config_ids
                    ]
                }
            },
        }

    document: dict[str, object] = spec(current or [])
    result: dict[str, object] = {"Spec": document}
    if previous is not None:
        previous_document = spec(previous)
        previous_document.pop("Name")
        result["PreviousSpec"] = previous_document
    return result


class FakeDockerClient:
    def __init__(
        self,
        configs: list[dict[str, object]],
        service_snapshots: list[list[dict[str, object]]],
    ) -> None:
        self.configs = configs
        self.service_snapshots = service_snapshots
        self.service_calls = 0
        self.removed: list[str] = []

    def inspect_managed_configs(self):
        return copy.deepcopy(self.configs)

    def inspect_services(self):
        index = min(self.service_calls, len(self.service_snapshots) - 1)
        self.service_calls += 1
        return copy.deepcopy(self.service_snapshots[index])

    def remove_config(self, config_id: str) -> None:
        self.removed.append(config_id)


class ConfigGcPlanTests(unittest.TestCase):
    def test_retention_is_per_series_and_references_always_win(self) -> None:
        configs = [
            *[
                managed_config(
                    "edge",
                    "edge-traefik-static",
                    generation,
                    kind="static",
                )
                for generation in range(1, 5)
            ],
            *[
                managed_config(
                    "workloads",
                    "workloads-n8n-entrypoint",
                    generation,
                    kind="entrypoint",
                )
                for generation in range(5, 8)
            ],
            *[
                managed_config(
                    "observability",
                    "observability-prometheus-config",
                    generation,
                    kind="application-config",
                )
                for generation in range(8, 10)
            ],
        ]
        plan = config_gc.build_plan(
            configs,
            [
                service(
                    "edge_traefik",
                    current=["edge-config-id-1"],
                )
            ],
            selected_stacks={"edge", "workloads", "observability"},
            retain=2,
        )
        self.assertEqual(
            {item["id"] for item in plan["candidates"]},
            {
                "edge-config-id-2",
                "workloads-config-id-5",
            },
        )
        protected = {item["id"]: item["reasons"] for item in plan["protected"]}
        self.assertIn("service-reference", protected["edge-config-id-1"])
        self.assertEqual(
            plan["confirmation"],
            f"DELETE_DOCKER_CONFIGS:{plan['planSha256']}",
        )

    def test_current_and_previous_service_references_are_never_candidates(
        self,
    ) -> None:
        configs = [
            managed_config(
                "edge",
                "edge-traefik-dynamic",
                generation,
                kind="dynamic",
            )
            for generation in range(1, 4)
        ]
        plan = config_gc.build_plan(
            configs,
            [
                service(
                    "edge_traefik",
                    current=["edge-config-id-1"],
                    previous=["edge-config-id-2"],
                )
            ],
            selected_stacks={"edge"},
            retain=1,
        )
        self.assertEqual(plan["candidates"], [])
        references = set(plan["referencedConfigIds"])
        self.assertEqual(
            references,
            {"edge-config-id-1", "edge-config-id-2"},
        )

    def test_malformed_selected_kind_name_or_digest_fails_closed(self) -> None:
        original = managed_config(
            "workloads",
            "workloads-n8n-entrypoint",
            1,
            kind="entrypoint",
        )
        mutations = []

        candidate = copy.deepcopy(original)
        candidate["Spec"]["Labels"]["com.apptolast.kind"] = "static"
        mutations.append(candidate)

        candidate = copy.deepcopy(original)
        candidate["Spec"]["Labels"]["com.apptolast.sha256"] = "f" * 64
        mutations.append(candidate)

        candidate = copy.deepcopy(original)
        candidate["Spec"]["Name"] = "edge-n8n-entrypoint-" + ("1" * 16)
        mutations.append(candidate)

        for candidate in mutations:
            with self.subTest(candidate=candidate["Spec"]):
                with self.assertRaises(config_gc.ConfigGcError):
                    config_gc.build_plan(
                        [candidate],
                        [],
                        selected_stacks={
                            "edge",
                            "workloads",
                            "observability",
                        },
                        retain=2,
                    )

    def test_unselected_owner_is_never_considered_for_deletion(self) -> None:
        edge = managed_config(
            "edge",
            "edge-traefik-static",
            1,
            kind="static",
        )
        workloads = managed_config(
            "workloads",
            "workloads-n8n-entrypoint",
            2,
            kind="entrypoint",
        )
        workloads["Spec"]["Labels"]["com.apptolast.sha256"] = "invalid"
        unknown_owner = managed_config(
            "workloads",
            "workloads-unknown-owner-entrypoint",
            3,
            kind="entrypoint",
        )
        unknown_owner["Spec"]["Labels"]["com.apptolast.stack"] = "unknown"
        plan = config_gc.build_plan(
            [edge, workloads, unknown_owner],
            [],
            selected_stacks={"edge"},
            retain=1,
        )
        self.assertEqual(plan["managedConfigIds"], ["edge-config-id-1"])

    def test_invalid_retention_is_rejected(self) -> None:
        for retention in (0, 101):
            with self.subTest(retention=retention):
                with self.assertRaises(config_gc.ConfigGcError):
                    config_gc.build_plan(
                        [],
                        [],
                        selected_stacks={"edge"},
                        retain=retention,
                    )


class ConfigGcApplyTests(unittest.TestCase):
    def candidate_configs(self) -> list[dict[str, object]]:
        return [
            managed_config(
                "edge",
                "edge-traefik-static",
                generation,
                kind="static",
            )
            for generation in range(1, 4)
        ]

    def test_dry_run_is_default_and_wrong_confirmation_deletes_nothing(
        self,
    ) -> None:
        arguments = config_gc.parse_args([])
        self.assertFalse(arguments.apply)
        self.assertEqual(arguments.retain, 2)
        self.assertEqual(
            set(arguments.stacks),
            {"edge", "workloads", "observability"},
        )

        client = FakeDockerClient(self.candidate_configs(), [[]])
        report = config_gc.reconcile(
            client,
            selected_stacks={"edge"},
            retain=1,
            apply=False,
            confirmation=None,
        )
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(client.removed, [])

        client = FakeDockerClient(self.candidate_configs(), [[]])
        with self.assertRaisesRegex(
            config_gc.ConfigGcError,
            "confirmation does not bind",
        ):
            config_gc.reconcile(
                client,
                selected_stacks={"edge"},
                retain=1,
                apply=True,
                confirmation="DELETE_DOCKER_CONFIGS:" + ("0" * 64),
            )
        self.assertEqual(client.removed, [])

    def test_exact_confirmation_removes_only_planned_ids_one_by_one(
        self,
    ) -> None:
        dry_client = FakeDockerClient(self.candidate_configs(), [[]])
        dry_run = config_gc.reconcile(
            dry_client,
            selected_stacks={"edge"},
            retain=1,
            apply=False,
            confirmation=None,
        )

        apply_client = FakeDockerClient(self.candidate_configs(), [[]])
        applied = config_gc.reconcile(
            apply_client,
            selected_stacks={"edge"},
            retain=1,
            apply=True,
            confirmation=dry_run["confirmation"],
        )
        self.assertEqual(
            apply_client.removed,
            ["edge-config-id-1", "edge-config-id-2"],
        )
        self.assertEqual(
            [item["id"] for item in applied["removed"]],
            apply_client.removed,
        )

    def test_new_service_reference_aborts_before_candidate_removal(self) -> None:
        dry_run = config_gc.reconcile(
            FakeDockerClient(self.candidate_configs(), [[]]),
            selected_stacks={"edge"},
            retain=2,
            apply=False,
            confirmation=None,
        )
        client = FakeDockerClient(
            self.candidate_configs(),
            [
                [],
                [
                    service(
                        "late_consumer",
                        current=["edge-config-id-1"],
                    )
                ],
            ],
        )
        with self.assertRaisesRegex(
            config_gc.ConfigGcError,
            "became referenced",
        ):
            config_gc.reconcile(
                client,
                selected_stacks={"edge"},
                retain=2,
                apply=True,
                confirmation=dry_run["confirmation"],
            )
        self.assertEqual(client.removed, [])

    def test_apply_requires_confirmation_argument(self) -> None:
        with self.assertRaises(SystemExit):
            config_gc.parse_args(["--apply"])
        with self.assertRaises(SystemExit):
            config_gc.parse_args(["--confirm", "unexpected"])


class ConfigGcIntegrationTests(unittest.TestCase):
    def test_edge_wrapper_delegates_and_deploy_never_runs_gc(self) -> None:
        wrapper = (REPOSITORY_ROOT / "scripts/gc-edge-configs.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('exec "${SCRIPT_DIR}/gc-docker-configs.py"', wrapper)
        self.assertIn("--stack edge", wrapper)

        for role in ("edge", "workloads", "observability"):
            role_root = REPOSITORY_ROOT / f"ansible/roles/{role}/tasks"
            deployment_source = "\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted(role_root.glob("*.yml"))
            )
            self.assertNotIn("gc-docker-configs", deployment_source)
            self.assertNotIn("gc-edge-configs", deployment_source)

    def test_removal_uses_exact_config_id_without_force_or_glob(self) -> None:
        source = (REPOSITORY_ROOT / "scripts/gc-docker-configs.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('self.run(["config", "rm", config_id])', source)
        self.assertNotIn("--force", source)
        self.assertNotIn("config prune", source)


if __name__ == "__main__":
    unittest.main()
