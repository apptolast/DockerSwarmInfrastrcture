"""Parked workloads (config/platform.yml platform_parked_workloads).

A parked workload renders `replicas: 0` and keeps its image, data, secrets,
networks, edge route and capacity budget. Every layer that reacts to parking
is rendered here for both reviewed states, nothing parked and both parkable
services parked, into disposable directories so `.build` is never touched.
"""

from __future__ import annotations

import concurrent.futures
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
VARIANTS = {
    "none": [],
    "minecraft": ["minecraft"],
    "openclaw": ["openclaw"],
    "both": PARKABLE,
}
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
        cls.roots = {
            variant: Path(cls.temporary.name) / variant for variant in VARIANTS
        }
        jobs = [
            (variant, playbook, key, directory)
            for variant in VARIANTS
            for playbook, key, directory in (
                ("render-workloads", "workloads_render_root", "workloads"),
                ("render-edge", "edge_render_dir", "edge"),
                ("render-observability", "observability_render_root", "observability"),
            )
        ]

        def run(job: tuple[str, str, str, str]) -> subprocess.CompletedProcess[str]:
            variant, playbook, key, directory = job
            return render(
                playbook,
                {
                    "platform_parked_workloads": VARIANTS[variant],
                    key: str(cls.roots[variant] / directory),
                },
            )

        # Independent disposable renders; four at a time keeps the host calm.
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, jobs))
        for (variant, playbook, _, _), completed in zip(jobs, results, strict=True):
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

    def dynamic(self, variant: str) -> dict[str, Any]:
        return load_yaml(self.roots[variant] / "edge/dynamic.yml")["http"]

    def jobs(self, variant: str) -> dict[str, Any]:
        prometheus = load_yaml(
            self.roots[variant] / "observability/config/prometheus.yml"
        )
        return {job["job_name"]: job for job in prometheus["scrape_configs"]}

    def test_parking_changes_only_the_replica_count_of_parked_services(self) -> None:
        running = self.stack("none", "workloads")
        for variant, parked in VARIANTS.items():
            candidate = self.stack(variant, "workloads")
            self.assertEqual(set(candidate["services"]), set(running["services"]))
            for name, service in running["services"].items():
                with self.subTest(variant=variant, service=name):
                    self.assertEqual(service["deploy"]["replicas"], 1)
                    self.assertEqual(
                        candidate["services"][name]["deploy"]["replicas"],
                        0 if name in parked else 1,
                    )
                    # Image, data, port, networks, secrets and budget all stay.
                    self.assertEqual(
                        without_replicas(service),
                        without_replicas(candidate["services"][name]),
                    )
            self.assertEqual(
                {key: value for key, value in running.items() if key != "services"},
                {key: value for key, value in candidate.items() if key != "services"},
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

        def validate(stack: dict[str, Any], parked: Any) -> None:
            workload_validator.validate_stack(
                stack,
                services,
                channels,
                secrets,
                runner_metadata,
                platform_with(parked),
            )

        for variant, parked in VARIANTS.items():
            with self.subTest(variant=variant):
                validate(self.stack(variant, "workloads"), parked)
            for other, other_parked in VARIANTS.items():
                if other != variant:
                    with (
                        self.subTest(render=variant, platform=other),
                        self.assertRaisesRegex(
                            workload_validator.ContractError, "replica count drift"
                        ),
                    ):
                        validate(self.stack(variant, "workloads"), other_parked)
        for parked in (
            ["n8n-db"],
            ["openclaw", "minecraft"],
            ["minecraft", "minecraft"],
            "minecraft",
            None,
        ):
            with (
                self.subTest(parked=parked),
                self.assertRaisesRegex(
                    workload_validator.ContractError,
                    "outside the reviewed parkable set",
                ),
            ):
                validate(self.stack("both", "workloads"), parked)
        # A boolean is not a replica count, even where it compares equal.
        for service, replicas in (("kropia", True), ("minecraft", False)):
            stack = self.stack("both", "workloads")
            stack["services"][service]["deploy"]["replicas"] = replicas
            with (
                self.subTest(service=service, replicas=replicas),
                self.assertRaisesRegex(
                    workload_validator.ContractError,
                    f"replica count drift for {service}",
                ),
            ):
                validate(stack, PARKABLE)

    def test_parked_openclaw_keeps_its_route_without_server_or_probe(self) -> None:
        running = self.dynamic("none")
        self.assertEqual(
            running["services"]["openclaw"]["loadBalancer"],
            {
                "passHostHeader": True,
                "servers": [{"url": "http://workloads_openclaw:18789"}],
                "healthCheck": {"path": "/readyz", "interval": "15s", "timeout": "3s"},
            },
        )
        for variant, parked in VARIANTS.items():
            candidate = self.dynamic(variant)
            with self.subTest(variant=variant):
                self.assertEqual(running["routers"], candidate["routers"])
                # Traefik answers 503 "no available server" and runs no probe.
                self.assertEqual(
                    candidate["services"]["openclaw"],
                    {"loadBalancer": {"passHostHeader": True, "servers": []}}
                    if "openclaw" in parked
                    else running["services"]["openclaw"],
                )
                for name in set(running["services"]) - {"openclaw"}:
                    self.assertEqual(
                        running["services"][name], candidate["services"][name]
                    )

    def test_parked_services_lose_only_their_blackbox_probes(self) -> None:
        running = self.jobs("none")
        public = "blackbox-http-public"
        for variant, parked in VARIANTS.items():
            candidate = self.jobs(variant)
            with self.subTest(variant=variant):
                self.assertEqual(
                    set(running) - set(candidate),
                    {"blackbox-minecraft-tcp"} if "minecraft" in parked else set(),
                )
                self.assertEqual(
                    set(running[public]["static_configs"][0]["targets"])
                    - set(candidate[public]["static_configs"][0]["targets"]),
                    {"https://openclaw.apptolast.com/healthz"}
                    if "openclaw" in parked
                    else set(),
                )
                for name in set(candidate) - {public}:
                    self.assertEqual(running[name], candidate[name])
                # The alert rule stays loaded; without the job it has no series.
                rules = load_yaml(
                    self.roots[variant] / "observability/config/prometheus-alerts.yml"
                )
                self.assertIn(
                    "MinecraftEndpointDown",
                    {
                        rule["alert"]
                        for group in rules["groups"]
                        for rule in group["rules"]
                    },
                )

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

        for variant, parked in VARIANTS.items():
            with self.subTest(variant=variant):
                validate(variant, parked)
            for other, other_parked in VARIANTS.items():
                if other != variant:
                    with (
                        self.subTest(render=variant, platform=other),
                        self.assertRaisesRegex(
                            observability_validator.ContractError,
                            "scrape job set changed|public blackbox probes differ",
                        ),
                    ):
                        validate(variant, other_parked)
        for parked in (
            ["selenium"],
            ["openclaw", "minecraft"],
            ["minecraft", "minecraft"],
            "minecraft",
            None,
        ):
            with (
                self.subTest(parked=parked),
                self.assertRaisesRegex(
                    observability_validator.ContractError,
                    "outside the reviewed parkable set",
                ),
            ):
                validate("both", parked)

    def test_parking_keeps_the_reviewed_capacity_budget(self) -> None:
        contract = capacity.validate_contract(
            load_yaml(REPOSITORY_ROOT / "config/capacity.yml")
        )
        totals = []
        for variant in VARIANTS:
            documents = {
                stack_id: load_yaml(path)
                for stack_id, path in capacity.DEFAULT_STACKS.items()
            }
            documents["workloads"] = self.stack(variant, "workloads")
            totals.append(capacity.validate_stacks(contract, documents))
        for variant_totals in totals:
            self.assertEqual(variant_totals, totals[0])
            self.assertEqual(
                variant_totals["workloads"], contract["reviewed_totals"]["workloads"]
            )


class UnreviewedParkingRenderTests(unittest.TestCase):
    def test_render_refuses_to_park_a_service_with_dependents(self) -> None:
        for playbook, key, message in (
            (
                "render-workloads",
                "workloads_render_root",
                "outside the reviewed parkable services",
            ),
            (
                "render-observability",
                "observability_render_root",
                "outside the reviewed parkable services",
            ),
            ("render-edge", "edge_render_dir", "Assertion failed"),
        ):
            with self.subTest(playbook=playbook), tempfile.TemporaryDirectory() as root:
                for parked in (["n8n-db"], "openclaw", {"openclaw": True}):
                    with self.subTest(parked=parked):
                        completed = render(
                            playbook,
                            {"platform_parked_workloads": parked, key: root},
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
        edge = load_yaml(REPOSITORY_ROOT / "ansible/roles/edge/defaults/main.yml")
        self.assertEqual(workloads["workloads_parkable_services"], PARKABLE)
        self.assertEqual(edge["edge_parkable_workloads"], PARKABLE)
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
        cls.edges = {
            variant: Path(cls.temporary.name) / variant for variant in ("none", "both")
        }

        def run(variant: str) -> subprocess.CompletedProcess[str]:
            return render(
                "render-edge",
                {
                    "platform_parked_workloads": VARIANTS[variant],
                    "edge_render_dir": str(cls.edges[variant]),
                },
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, cls.edges))
        for completed in results:
            if completed.returncode != 0:
                cls.temporary.cleanup()
                raise AssertionError(completed.stdout + completed.stderr)

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

    def test_parked_backend_must_have_exactly_no_server_and_no_probe(self) -> None:
        def backend(load_balancer: dict[str, Any]) -> Callable[[Path], None]:
            def mutate(root: Path) -> None:
                path = root / ".build/edge/dynamic.yml"
                document = load_yaml(path)
                document["http"]["services"]["openclaw"] = {
                    "loadBalancer": load_balancer
                }
                path.write_text(
                    yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
                )

            return mutate

        for load_balancer in (
            {
                "passHostHeader": True,
                "servers": [],
                "healthCheck": {"path": "/readyz", "interval": "15s", "timeout": "3s"},
            },
            {
                "passHostHeader": True,
                "servers": [{"url": "http://workloads_openclaw:18789"}],
            },
            {"servers": []},
        ):
            with self.subTest(load_balancer=load_balancer):
                completed = self.run_contract("both", PARKABLE, backend(load_balancer))
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    "parked openclaw backend must have no server", completed.stderr
                )


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
        # A failed listing is no proof that nothing runs.
        failed = results([])
        failed["workloads_parked_task_container_ids"]["results"][0]["rc"] = 1
        self.assert_task_rejects(
            self.DEPLOY, task, failed, "still runs a task container"
        )

    def test_every_role_rejects_an_unreviewed_parked_list(self) -> None:
        task = "Require a reviewed parked workload list"
        message = "outside the reviewed parkable services"
        roles = (
            (
                self.DERIVE,
                "workloads_parkable_services",
                "ansible/roles/workloads/defaults/main.yml",
            ),
            (
                "ansible/roles/observability/tasks/derive.yml",
                "observability_parkable_catalog_ids",
                "ansible/roles/observability/defaults/main.yml",
            ),
            (
                "ansible/roles/edge/tasks/main.yml",
                "edge_parkable_workloads",
                "ansible/roles/edge/defaults/main.yml",
            ),
        )
        for task_file, key, defaults in roles:
            parkable = load_yaml(REPOSITORY_ROOT / defaults)[key]
            for parked in ([], ["minecraft"], ["openclaw"], PARKABLE):
                with self.subTest(role=task_file, parked=parked):
                    self.assert_task_accepts(
                        task_file,
                        task,
                        {"platform_parked_workloads": parked, key: parkable},
                    )
            for parked in (
                ["n8n-db"],
                ["openclaw", "minecraft"],
                ["minecraft", "minecraft"],
                "minecraft",
                {"minecraft": True},
                None,
                [1],
            ):
                with self.subTest(role=task_file, parked=parked):
                    self.assert_task_rejects(
                        task_file,
                        task,
                        {"platform_parked_workloads": parked, key: parkable},
                        message,
                    )

    def test_parked_minecraft_port_must_have_no_host_listener(self) -> None:
        task = "Refuse a host listener on the port a parked Minecraft leaves open"
        listener = 'LISTEN 0 4096 0.0.0.0:25565 0.0.0.0:* users:(("nc",pid=4242,fd=3))'

        def facts(parked: list[str], lines: list[str], public: bool = True):
            return {
                "platform_parked_workloads": parked,
                "platform_minecraft_public_enabled": public,
                "workloads_parked_minecraft_listeners": {
                    "rc": 0,
                    "stdout_lines": lines,
                },
            }

        self.assert_task_accepts(self.DEPLOY, task, facts(PARKABLE, []))
        self.assert_task_rejects(
            self.DEPLOY, task, facts(PARKABLE, [listener]), "listens on TCP 25565"
        )
        # A running Minecraft task legitimately holds the port through dockerd,
        # and a closed public gate leaves nothing admitted to protect.
        self.assert_task_accepts(self.DEPLOY, task, facts(["openclaw"], [listener]))
        self.assert_task_accepts(
            self.DEPLOY, task, facts(PARKABLE, [listener], public=False)
        )


if __name__ == "__main__":
    unittest.main()
