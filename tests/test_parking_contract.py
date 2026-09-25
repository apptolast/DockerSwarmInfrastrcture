"""Parked workloads (config/platform.yml platform_parked_workloads).

A parked workload renders `replicas: 0` and keeps its image, data, secrets,
networks, edge route and capacity budget. Every layer that reacts to parking
is rendered here for both reviewed states, nothing parked and both parkable
services parked, into disposable directories so `.build` is never touched.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from ansible_task_harness import (
    ANSIBLE_PLAYBOOK,
    REPOSITORY_ROOT,
    AnsibleTaskAssertions,
    load_task,
)

PARKABLE = ["minecraft", "openclaw"]
VARIANTS = {"none": [], "both": PARKABLE}
ANSIBLE_ENVIRONMENT_CONFIG = REPOSITORY_ROOT / "ansible/ansible.cfg"


def load_script(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, REPOSITORY_ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workload_validator = load_script(
    "validate_workloads_parking", "scripts/validate-workloads.py"
)
observability_validator = load_script(
    "validate_observability_parking", "scripts/validate-observability.py"
)
capacity = load_script("validate_capacity_parking", "scripts/validate-capacity.py")


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def render(
    playbook: str, variables: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(ANSIBLE_PLAYBOOK),
            "--inventory",
            "ansible/inventory/local/hosts.yml",
            f"ansible/playbooks/{playbook}.yml",
            "--extra-vars",
            json.dumps(variables),
        ],
        cwd=REPOSITORY_ROOT,
        env={
            "ANSIBLE_CONFIG": str(ANSIBLE_ENVIRONMENT_CONFIG),
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def platform_with(parked: Any) -> dict[str, Any]:
    platform = load_yaml(REPOSITORY_ROOT / "config/platform.yml")
    platform["platform_parked_workloads"] = parked
    return platform


def without_replicas(service: dict[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(service)
    candidate["deploy"].pop("replicas")
    return candidate


class ParkedRenderTests(unittest.TestCase):
    """Render every parking-aware layer once per reviewed parking state."""

    temporary: tempfile.TemporaryDirectory[str]
    roots: dict[str, Path]

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.roots = {}
        for variant, parked in VARIANTS.items():
            root = Path(cls.temporary.name) / variant
            cls.roots[variant] = root
            for playbook, key, directory in (
                ("render-workloads", "workloads_render_root", "workloads"),
                ("render-edge", "edge_render_dir", "edge"),
                ("render-observability", "observability_render_root", "observability"),
            ):
                completed = render(
                    playbook,
                    {"platform_parked_workloads": parked, key: str(root / directory)},
                )
                if completed.returncode != 0:
                    cls.temporary.cleanup()
                    raise AssertionError(
                        f"{playbook} ({variant}) failed:\n"
                        + completed.stdout
                        + completed.stderr
                    )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def stack(self, variant: str, name: str) -> dict[str, Any]:
        return load_yaml(self.roots[variant] / name / "stack.yml")

    def test_parking_changes_only_the_replica_count_of_parked_services(self) -> None:
        running = self.stack("none", "workloads")["services"]
        parked = self.stack("both", "workloads")["services"]
        self.assertEqual(set(running), set(parked))
        for name, service in running.items():
            with self.subTest(service=name):
                self.assertEqual(service["deploy"]["replicas"], 1)
                self.assertEqual(
                    parked[name]["deploy"]["replicas"], 0 if name in PARKABLE else 1
                )
                # Image, data, port, networks, secrets and budget all stay.
                self.assertEqual(
                    without_replicas(service), without_replicas(parked[name])
                )
        self.assertEqual(
            {
                key: value
                for key, value in self.stack("none", "workloads").items()
                if key != "services"
            },
            {
                key: value
                for key, value in self.stack("both", "workloads").items()
                if key != "services"
            },
        )

    def test_workload_validator_binds_each_render_to_its_parking_list(self) -> None:
        services = load_yaml(REPOSITORY_ROOT / "config/services.yml")
        channels = workload_validator.load_unique_yaml(
            REPOSITORY_ROOT / "config/image-channels.yml"
        )
        secrets = load_yaml(REPOSITORY_ROOT / "stacks/workloads/secrets.yml")
        # Same runner identity scripts/validate-workloads.py main() derives.
        runner_metadata = workload_validator.load_runner_manager().describe(
            REPOSITORY_ROOT / "images/n8n-runners",
            workload_validator.find_image(
                {item["id"]: item for item in services["approved_services"]},
                "n8n",
                "runner",
            ),
        )

        def validate(variant: str, parked: Any) -> None:
            workload_validator.validate_stack(
                self.stack(variant, "workloads"),
                services,
                channels,
                secrets,
                runner_metadata,
                platform_with(parked),
            )

        validate("none", [])
        validate("both", PARKABLE)
        for variant, parked, message in (
            ("both", [], "replica count drift for minecraft"),
            ("none", PARKABLE, "replica count drift for minecraft"),
            ("both", ["minecraft"], "replica count drift for openclaw"),
            ("both", ["n8n-db"], "outside the reviewed parkable set"),
            ("both", ["openclaw", "minecraft"], "outside the reviewed parkable set"),
            ("both", "minecraft", "outside the reviewed parkable set"),
        ):
            with self.subTest(variant=variant, parked=parked):
                with self.assertRaisesRegex(workload_validator.ContractError, message):
                    validate(variant, parked)

    def test_parked_openclaw_keeps_its_route_without_server_or_probe(self) -> None:
        running = load_yaml(self.roots["none"] / "edge/dynamic.yml")["http"]
        parked = load_yaml(self.roots["both"] / "edge/dynamic.yml")["http"]
        self.assertEqual(running["routers"], parked["routers"])
        self.assertEqual(
            running["services"]["openclaw"]["loadBalancer"],
            {
                "passHostHeader": True,
                "servers": [{"url": "http://workloads_openclaw:18789"}],
                "healthCheck": {"path": "/readyz", "interval": "15s", "timeout": "3s"},
            },
        )
        # Traefik answers 503 "no available server" and runs no probe.
        self.assertEqual(
            parked["services"]["openclaw"],
            {"loadBalancer": {"passHostHeader": True, "servers": []}},
        )
        for name in running["services"]:
            if name != "openclaw":
                with self.subTest(service=name):
                    self.assertEqual(
                        running["services"][name], parked["services"][name]
                    )

    def test_parked_services_lose_only_their_blackbox_probes(self) -> None:
        def prometheus(variant: str) -> dict[str, Any]:
            return load_yaml(
                self.roots[variant] / "observability/config/prometheus.yml"
            )

        def jobs(variant: str) -> dict[str, Any]:
            return {
                job["job_name"]: job for job in prometheus(variant)["scrape_configs"]
            }

        running, parked = jobs("none"), jobs("both")
        self.assertEqual(set(running) - set(parked), {"blackbox-minecraft-tcp"})
        public = "blackbox-http-public"
        self.assertEqual(
            set(running[public]["static_configs"][0]["targets"])
            - set(parked[public]["static_configs"][0]["targets"]),
            {"https://openclaw.apptolast.com/healthz"},
        )
        for name in set(parked) - {public}:
            with self.subTest(job=name):
                self.assertEqual(running[name], parked[name])
        # The alert rule stays loaded; without the job it has no series.
        for variant in VARIANTS:
            rules = load_yaml(
                self.roots[variant] / "observability/config/prometheus-alerts.yml"
            )
            names = {
                rule["alert"] for group in rules["groups"] for rule in group["rules"]
            }
            self.assertIn("MinecraftEndpointDown", names)

    def test_observability_validator_binds_each_render_to_its_parking_list(
        self,
    ) -> None:
        services = load_yaml(REPOSITORY_ROOT / "config/services.yml")

        def validate(variant: str, parked: Any) -> None:
            root = self.roots[variant] / "observability"
            observability_validator.validate_configs(
                load_yaml(root / "stack.yml"),
                services,
                root / "config",
                platform_with(parked),
            )

        validate("none", [])
        validate("both", PARKABLE)
        for variant, parked, message in (
            ("both", [], "scrape job set changed"),
            ("none", PARKABLE, "scrape job set changed"),
            ("both", ["minecraft"], "public blackbox probes differ"),
            ("both", ["selenium"], "outside the reviewed parkable set"),
            ("both", ["openclaw", "minecraft"], "outside the reviewed parkable set"),
        ):
            with self.subTest(variant=variant, parked=parked):
                with self.assertRaisesRegex(
                    observability_validator.ContractError, message
                ):
                    validate(variant, parked)

    def test_parking_keeps_the_reviewed_capacity_budget(self) -> None:
        contract = capacity.validate_contract(
            load_yaml(REPOSITORY_ROOT / "config/capacity.yml")
        )
        totals = {}
        for variant in VARIANTS:
            documents = {
                stack_id: load_yaml(path)
                for stack_id, path in capacity.DEFAULT_STACKS.items()
            }
            documents["workloads"] = self.stack(variant, "workloads")
            totals[variant] = capacity.validate_stacks(contract, documents)
        self.assertEqual(totals["none"], totals["both"])
        self.assertEqual(
            totals["both"]["workloads"], contract["reviewed_totals"]["workloads"]
        )


class UnreviewedParkingRenderTests(unittest.TestCase):
    def test_render_refuses_to_park_a_service_with_dependents(self) -> None:
        for playbook, message in (
            (
                "render-workloads",
                "outside the approved workloads contract",
            ),
            (
                "render-observability",
                "outside the reviewed parkable services",
            ),
        ):
            with self.subTest(playbook=playbook), tempfile.TemporaryDirectory() as root:
                key = (
                    "workloads_render_root"
                    if playbook == "render-workloads"
                    else "observability_render_root"
                )
                completed = render(
                    playbook,
                    {"platform_parked_workloads": ["n8n-db"], key: root},
                )
                self.assertNotEqual(completed.returncode, 0, completed.stdout)
                self.assertIn(message, completed.stdout + completed.stderr)


class ParkableSetTests(unittest.TestCase):
    """Every layer names the same reviewed parkable services."""

    def test_all_layers_share_the_reviewed_parkable_set(self) -> None:
        workloads = load_yaml(
            REPOSITORY_ROOT / "ansible/roles/workloads/defaults/main.yml"
        )
        observability = load_yaml(
            REPOSITORY_ROOT / "ansible/roles/observability/defaults/main.yml"
        )
        backup = load_yaml(REPOSITORY_ROOT / "ansible/roles/backup/defaults/main.yml")
        self.assertEqual(workloads["workloads_parkable_services"], PARKABLE)
        self.assertEqual(set(workload_validator.PARKABLE_SERVICES), set(PARKABLE))
        self.assertEqual(
            {
                name
                for stack, name in capacity.SUSPENDABLE_SERVICES
                if stack == "workloads"
            },
            set(PARKABLE),
        )
        self.assertEqual(
            observability["observability_parkable_catalog_ids"],
            observability_validator.PARKABLE_CATALOG_IDS,
        )
        # Each parkable service maps to its own catalog entry and Swarm name.
        for name in PARKABLE:
            with self.subTest(service=name):
                self.assertEqual(
                    observability["observability_parkable_catalog_ids"][name],
                    workloads["workloads_image_baselines"][name]["catalog"],
                )
                self.assertEqual(
                    backup["backup_parkable_services"][name], f"workloads_{name}"
                )

    def test_reviewed_platform_list_is_sorted_and_parkable(self) -> None:
        parked = load_yaml(REPOSITORY_ROOT / "config/platform.yml")[
            "platform_parked_workloads"
        ]
        self.assertEqual(parked, sorted(set(parked)))
        self.assertLessEqual(set(parked), set(PARKABLE))


class ContractValidatorParkingTests(unittest.TestCase):
    """scripts/validate-contract.py on a disposable copy of its inputs."""

    INPUTS = (
        "ansible/group_vars/all.yml",
        "ansible/inventory/production/hosts.yml",
        "infra/terraform/cloudflare/apptolast-dns/dns.tf",
        "scripts/validate-contract.py",
        "scripts/validate-image-channels.py",
    )

    temporary: tempfile.TemporaryDirectory[str]
    edges: dict[str, Path]

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.edges = {}
        for variant, parked in VARIANTS.items():
            directory = Path(cls.temporary.name) / variant
            completed = render(
                "render-edge",
                {
                    "platform_parked_workloads": parked,
                    "edge_render_dir": str(directory),
                },
            )
            if completed.returncode != 0:
                cls.temporary.cleanup()
                raise AssertionError(completed.stdout + completed.stderr)
            cls.edges[variant] = directory

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def run_contract(
        self,
        edge: str,
        parked: Any,
        mutate: Callable[[Path], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(REPOSITORY_ROOT / "config", root / "config")
            shutil.copytree(self.edges[edge], root / ".build/edge")
            for relative in self.INPUTS:
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPOSITORY_ROOT / relative, root / relative)
            platform = load_yaml(root / "config/platform.yml")
            if parked is None:
                del platform["platform_parked_workloads"]
            else:
                platform["platform_parked_workloads"] = parked
            (root / "config/platform.yml").write_text(
                yaml.safe_dump(platform, sort_keys=False), encoding="utf-8"
            )
            if mutate is not None:
                mutate(root)
            return subprocess.run(
                [sys.executable, str(root / "scripts/validate-contract.py")],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_each_edge_render_passes_with_its_own_parking_list(self) -> None:
        for edge, parked in (("none", []), ("both", PARKABLE), ("none", ["minecraft"])):
            with self.subTest(edge=edge, parked=parked):
                completed = self.run_contract(edge, parked)
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_parking_list_outside_the_reviewed_contract_is_rejected(self) -> None:
        for parked in (["n8n-db"], ["openclaw", "minecraft"], ["minecraft"] * 2, "x"):
            with self.subTest(parked=parked):
                completed = self.run_contract("both", parked)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("platform_parked_workloads must be", completed.stderr)
        completed = self.run_contract("both", None)
        self.assertIn("missing or unexpected keys", completed.stderr)

    def test_edge_backend_must_follow_the_openclaw_parking_state(self) -> None:
        completed = self.run_contract("none", PARKABLE)
        self.assertIn("parked openclaw backend must have no server", completed.stderr)
        completed = self.run_contract("both", [])
        self.assertIn("openclaw upstream differs from the Swarm", completed.stderr)


class WorkloadsParkingGateTests(AnsibleTaskAssertions, unittest.TestCase):
    """The workloads role gates, fed synthetic Docker results."""

    DERIVE = "ansible/roles/workloads/tasks/derive.yml"
    DEPLOY = "ansible/roles/workloads/tasks/deploy.yml"
    SERVICES = load_yaml(REPOSITORY_ROOT / "ansible/roles/workloads/defaults/main.yml")[
        "workloads_expected_stack_services"
    ]

    def derived(self, task_name: str, parked: list[str], that: list[str]) -> str:
        """Run one reviewed set_fact task, then assert on what it derived."""
        task = load_task(self.DERIVE, task_name)
        with tempfile.TemporaryDirectory() as temporary:
            playbook = Path(temporary) / "derive.yml"
            playbook.write_text(
                yaml.safe_dump(
                    [
                        {
                            "name": "Derive one reviewed fact",
                            "hosts": "localhost",
                            "connection": "local",
                            "gather_facts": False,
                            "become": False,
                            "vars": {
                                "platform_parked_workloads": parked,
                                "workloads_stack_name": "workloads",
                                "workloads_expected_stack_services": self.SERVICES,
                                "workloads_approved_by_id": {
                                    identifier: {"hostnames": [f"{identifier}.example"]}
                                    for identifier in (
                                        "kropia",
                                        "minecraft-stats",
                                        "n8n",
                                        "openclaw-clean",
                                        "passbolt",
                                        "personal-website-alberto",
                                        "personal-website-pablo",
                                        "shlink",
                                    )
                                },
                            },
                            "tasks": [
                                task,
                                {
                                    "name": "Check the derived fact",
                                    "ansible.builtin.assert": {"that": that},
                                },
                            ],
                        }
                    ],
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [str(ANSIBLE_PLAYBOOK), "-i", "localhost,", str(playbook)],
                cwd=REPOSITORY_ROOT / "ansible",
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return completed.stdout

    def test_expected_replica_lines_follow_the_parking_list(self) -> None:
        running = sorted(f"workloads_{name}=1/1" for name in self.SERVICES)
        self.derived(
            "Derive the reviewed replica state of every workload service",
            [],
            [
                f"workloads_expected_replica_lines == {running!r}",
                f"workloads_running_stack_services | sort == {sorted(self.SERVICES)!r}",
            ],
        )
        parked = sorted(
            f"workloads_{name}=0/0" if name in PARKABLE else f"workloads_{name}=1/1"
            for name in self.SERVICES
        )
        self.derived(
            "Derive the reviewed replica state of every workload service",
            PARKABLE,
            [
                f"workloads_expected_replica_lines == {parked!r}",
                (
                    "workloads_running_stack_services | sort == "
                    f"{sorted(set(self.SERVICES) - set(PARKABLE))!r}"
                ),
            ],
        )

    def test_parked_openclaw_smoke_expects_the_edge_503(self) -> None:
        for parked, status in (([], "200"), (PARKABLE, "503")):
            with self.subTest(parked=parked):
                openclaw = (
                    'workloads_http_smoke_targets | selectattr("service", '
                    '"equalto", "openclaw") | first'
                )
                self.derived(
                    "Derive exact post-deployment smoke targets",
                    parked,
                    [
                        f"({openclaw}).status is string",
                        f'({openclaw}).status == "{status}"',
                        (
                            'workloads_http_smoke_targets | rejectattr("service", '
                            '"equalto", "openclaw") | map(attribute="status") '
                            '| unique | list == ["200"]'
                        ),
                    ],
                )

    def convergence(self, lines: list[str], expected: list[str]) -> dict[str, Any]:
        return {
            "workloads_stack_replicas": {"rc": 0, "stdout_lines": lines},
            "workloads_expected_replica_lines": expected,
            "workloads_expected_stack_services": self.SERVICES,
            "workloads_service_ps_diagnostics": {
                "results": [{"rc": 0, "stdout": ""} for _ in self.SERVICES]
            },
        }

    def test_convergence_gate_requires_the_exact_parked_state(self) -> None:
        expected = sorted(
            f"workloads_{name}=0/0" if name in PARKABLE else f"workloads_{name}=1/1"
            for name in self.SERVICES
        )
        task = "Enforce exact workload convergence"
        self.assert_task_accepts(
            self.DEPLOY, task, self.convergence(list(reversed(expected)), expected)
        )
        for replace, by in (
            ("workloads_minecraft=0/0", "workloads_minecraft=1/1"),
            ("workloads_openclaw=0/0", "workloads_openclaw=1/0"),
            ("workloads_kropia=1/1", "workloads_kropia=0/0"),
        ):
            with self.subTest(observed=by):
                observed = [by if line == replace else line for line in expected]
                self.assert_task_rejects(
                    self.DEPLOY,
                    task,
                    self.convergence(observed, expected),
                    "Workload convergence failed",
                )
        self.assert_task_rejects(
            self.DEPLOY,
            task,
            self.convergence(expected[1:], expected),
            "Workload convergence failed",
        )

    def test_parked_service_must_not_run_a_task(self) -> None:
        task = "Enforce that every parked workload service runs no task"

        def results(stdout_lines: list[str]) -> dict[str, Any]:
            return {
                "workloads_parked_task_container_ids": {
                    "results": [
                        {"item": name, "rc": 0, "stdout_lines": stdout_lines}
                        for name in PARKABLE
                    ]
                }
            }

        self.assert_task_accepts(self.DEPLOY, task, results([]))
        self.assert_task_rejects(
            self.DEPLOY, task, results(["a" * 64]), "still runs a task container"
        )


if __name__ == "__main__":
    unittest.main()
