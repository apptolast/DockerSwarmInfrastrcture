"""Contract of Agent Substrate in the AX lab playbook `ax-lab`.

The validator's Substrate rules, the role's image, build and install tasks
(run through the reviewed-task harness against synthetic reads, never
against Docker or a cluster), the reproducibility workflow and the docs.
"""

from __future__ import annotations

import base64
import copy
import importlib.util
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from ansible_task_harness import AnsibleTaskAssertions
from test_ax_lab_contract import (
    ARTIFACTS,
    CLUSTER,
    CONFIG,
    IMAGES,
    IMAGES_READ,
    INSPECT,
    MAIN,
    MANAGER,
    NODE,
    ROLE,
    ROOT,
    SUBSTRATE,
    SUBSTRATE_READ,
    cluster_variables,
    container_read,
    load_script,
    load_tasks,
    probe,
    role_task_files,
    run_reviewed_tasks,
)

WORKFLOW = ROOT / ".github/workflows/ax-lab-reproducibility.yml"
NAMES = (
    "ateapi",
    "atecontroller",
    "atelet",
    "atenet",
    "podcertcontroller",
    "ateom-gvisor",
    "ate-setup",
)
INSTALLED = ["ateapi", "atecontroller", "atelet", "atenet", "podcertcontroller"]
# Read from the manual lab's registry with `curl -I` on 2026-09-25: the
# Docker-Content-Digest of each repository's `latest`.
MANUAL_LAB_DIGESTS = {
    "ateapi": "sha256:c1422c85315e87c0710ec9e22447ea68185f96cd47e10473b75966f79c2d30f3",
    "atecontroller": (
        "sha256:0893131c560ee326933dc06494c86d31011f48c0c60e8dbebb40aaaed4c80c14"
    ),
    "atelet": "sha256:8e88c4e0d0289c88cb84f18f679494645700e67bea0e1a4c0bf56f461bf30f7b",
    "atenet": "sha256:929ca468e20fc7d06e356cb844d3cd5d2d30632db68ba5360adb4850b975fcd9",
    "podcertcontroller": (
        "sha256:2baed2f1e88434f8d018e595c41f7117f06c6af6410b6fbd2210bb79c54c8f99"
    ),
    "ateom-gvisor": (
        "sha256:fdd1d0ad3b30578d86f11aaa67740448a8477ce706e70207b8b51a96646592e7"
    ),
}
ATE_SETUP = "sha256:" + "e" * 64
# Built by the ax-lab-reproducibility workflow from the pinned commit
# (run 36187353468); the manual lab never built ate-setup.
REVIEWED_ATE_SETUP = (
    "sha256:43c9e2db7a2cdacedcd8515fb7abb561b4b7de60d8e5ef9df3b9d0300c7a1098"
)
NODE_ID = "b" * 64
CREATE_ONCE = [
    "ate-system/Secret/actor-id-jwt-pool",
    "ate-system/Secret/actor-id-ca-pool",
    "ate-system/Secret/actor-id-ca-certs",
    "podcertificate-controller-system/Secret/service-dns-ca-pool",
    "podcertificate-controller-system/Secret/pod-identity-ca-pool",
    "ate-system/ConfigMap/ate-api-authentication",
]


def load_manager() -> Any:
    """The manager, in sys.modules while it loads: dataclasses looks there."""
    spec = importlib.util.spec_from_file_location(
        "manage_ax_lab_substrate_contract", ROOT / MANAGER
    )
    manager = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {spec.name: manager}):
        spec.loader.exec_module(manager)
    return manager


def document() -> dict[str, Any]:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def pinned_variables(**overrides: Any) -> dict[str, Any]:
    """The role's variables with ate-setup pinned, as after the CI run."""
    variables = copy.deepcopy(cluster_variables())
    variables["ax_lab"]["substrate"]["images"]["ate-setup"] = ATE_SETUP
    variables["operation_lock_guard_environment"] = {"DOCKERSWARM_IAC_LOCK_SCOPE": "x"}
    variables.update(overrides)
    return variables


def image_status(
    registry: dict[str, str] | None, backup: dict[str, str]
) -> dict[str, Any]:
    """A registered `image-status` read, with a pseudo-terminal's noise."""
    payload = {"registry": registry, "backup": backup}
    return {"rc": 0, "stdout_lines": ["\r", json.dumps(payload)]}


def everywhere(value: str) -> dict[str, str]:
    return dict.fromkeys(NAMES, value)


def expected_identity() -> dict[str, Any]:
    """The Substrate state the role records, pinned here independently."""
    return {
        "schema_version": 1,
        "version": "67253354",
        "images": {
            **{name: MANUAL_LAB_DIGESTS[name] for name in INSTALLED},
            "ate-setup": ATE_SETUP,
        },
        "install": {"atenet_router": "envoy", "rollout_timeout_seconds": 600},
        "node_container_id": NODE_ID,
    }


def cluster_read(**overrides: Any) -> dict[str, Any]:
    """A converged `cluster-status` read of a verified install."""
    lab = document()["ax_lab"]
    read = {
        "node_label": "67253354",
        "create_once": {
            key: {
                "uid": f"uid-{index}",
                "type": None if "ConfigMap" in key else "Opaque",
            }
            for index, key in enumerate(CREATE_ONCE)
        },
        "workloads": copy.deepcopy(lab["substrate"]["workloads"]),
        "objects": {},
        "immutable": {
            "ate-system/StatefulSet/postgres": "pg-uid@1",
            "ate-system/Job/rustfs-bucket-init": "job-uid@1",
        },
        "not_ready": [],
    }
    read.update(overrides)
    return read


def state_file(**overrides: Any) -> dict[str, Any]:
    state = {
        **expected_identity(),
        "phase": "installed",
        "create_once": {key: f"uid-{index}" for index, key in enumerate(CREATE_ONCE)},
    }
    state.update(overrides)
    return state


def substrate_reads(
    *,
    state: dict[str, Any] | None,
    cluster: dict[str, Any] | None,
    node_absent: bool = False,
    **node: Any,
) -> dict[str, Any]:
    """What substrate_read.yml registers before its first set_fact."""
    reads: dict[str, Any] = {
        "ax_lab_node": (
            None
            if node_absent
            else json.loads(container_read("kind-control-plane", **node)["stdout"])
        ),
        "ax_lab_substrate_state_file": {"stat": {"exists": state is not None}},
        "ax_lab_substrate_cluster_raw": (
            {"skipped": True, "changed": False}
            if cluster is None
            else {"rc": 0, "stdout_lines": [json.dumps(cluster)]}
        ),
    }
    if state is not None:
        reads["ax_lab_substrate_state_content"] = {
            "content": base64.b64encode(json.dumps(state).encode()).decode()
        }
    return reads


class SubstrateValidatorTests(unittest.TestCase):
    """scripts/validate-ax-lab.py pins the Substrate contract."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_script(
            "validate_ax_lab_substrate", "scripts/validate-ax-lab.py"
        )
        cls.document = cls.module.load_yaml(CONFIG)
        cls.reserved = cls.module.reserved_sysctl_keys()

    def rejected(self, change, message: str) -> None:
        mutated = copy.deepcopy(self.document)
        change(mutated)
        with self.assertRaisesRegex(self.module.AxLabError, message):
            self.module.validate_catalog(mutated, self.reserved)

    def set_substrate(self, path: str, value: Any):
        def change(document: dict[str, Any]) -> None:
            *parents, leaf = path.split("/")
            target = document["ax_lab"]["substrate"]
            for parent in parents:
                target = target[int(parent)] if parent.isdigit() else target[parent]
            if value is KeyError:
                del target[leaf]
            else:
                target[leaf] = value

        return change

    def test_reviewed_substrate_pins_are_the_manual_lab_digests(self) -> None:
        substrate = self.document["ax_lab"]["substrate"]
        self.assertEqual(substrate["version"], "67253354")
        self.assertEqual(
            substrate["images"],
            {**MANUAL_LAB_DIGESTS, "ate-setup": REVIEWED_ATE_SETUP},
        )
        self.assertEqual(
            substrate["backup_directory"], "/var/backups/dockerswarm/ax-lab/images"
        )
        self.assertEqual(
            substrate["build"],
            {
                "memory_limit_mib": 3072,
                "memory_reservation_mib": 1536,
                "cpu_limit_millicores": 2000,
                "pids_limit": 1024,
                "timeout_seconds": 3600,
                "min_mem_available_mib": 3584,
                "mem_available_floor_mib": 512,
            },
        )
        self.assertEqual(
            substrate["install"],
            {
                "atenet_router": "envoy",
                "rollout_timeout_seconds": 600,
                "timeout_seconds": 6480,
                "memory_limit_mib": 256,
                "memory_reservation_mib": 128,
                "cpu_limit_millicores": 500,
                "pids_limit": 256,
                "min_mem_available_mib": 768,
                "mem_available_floor_mib": 512,
            },
        )
        # Only the image the manual lab never built, for the first window.
        self.assertEqual(
            self.document["ax_lab_substrate_fallback_builds"], ["ate-setup"]
        )

    def test_workloads_are_what_ate_setup_deploys_on_kind(self) -> None:
        workloads = self.document["ax_lab"]["substrate"]["workloads"]
        digest = {
            "envoy": "envoyproxy/envoy:v1.39-latest@sha256:"
            "57e14a549d7bd43c8d3f6d03e8cfa653e037d4b38e133acd9b54f38c524401b4",
            "postgres": "postgres:18-alpine@sha256:"
            "9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15",
        }
        self.assertEqual(
            [
                (
                    w["namespace"],
                    w["kind"],
                    w["name"],
                    w.get("init_images"),
                    w["images"],
                )
                for w in workloads
            ],
            [
                ("ate-system", "DaemonSet", "atelet-67253354", None, ["atelet"]),
                ("ate-system", "Deployment", "ate-api-server", None, ["ateapi"]),
                ("ate-system", "Deployment", "ate-controller", None, ["atecontroller"]),
                (
                    "ate-system",
                    "Deployment",
                    "atenet-egress",
                    None,
                    [digest["envoy"], "atenet"],
                ),
                (
                    "ate-system",
                    "Deployment",
                    "atenet-router",
                    None,
                    ["atenet", digest["envoy"]],
                ),
                (
                    "ate-system",
                    "Deployment",
                    "rustfs",
                    None,
                    [
                        "rustfs/rustfs:1.0.0-beta.3@sha256:"
                        "378642b05b7dcb4849fb77ebe6aca4ced1c3f66e7e504247df95a5c9018d3358"
                    ],
                ),
                (
                    "ate-system",
                    "Job",
                    "rustfs-bucket-init",
                    None,
                    [
                        "amazon/aws-cli:2.17.0@sha256:"
                        "643507c10ada7964ca6157b3d799f030b90577643da9955d319a77399ed80d73"
                    ],
                ),
                (
                    "ate-system",
                    "StatefulSet",
                    "postgres",
                    [digest["postgres"]],
                    [digest["postgres"]],
                ),
                (
                    "otel-system",
                    "Deployment",
                    "jaeger",
                    None,
                    [
                        "jaegertracing/all-in-one:1.55@sha256:"
                        "f6b5d09073f14f76873d300f565a6691d815e81bea8e07e1dc3ff67e0596dd4e"
                    ],
                ),
                (
                    "otel-system",
                    "Deployment",
                    "opentelemetry-collector",
                    None,
                    [
                        "otel/opentelemetry-collector-contrib:0.157.0@sha256:"
                        "f2f01157055a9b2aab9df7118e1f1c9abf345e99b23bc7a2bc791db374a7d0f6"
                    ],
                ),
                (
                    "otel-system",
                    "Deployment",
                    "prometheus",
                    None,
                    [
                        "prom/prometheus:v3.5.3@sha256:"
                        "ddc2493835a1509976d5e4e0c94199c4f843ce1f42dd6bcfc8231ba734a93ff7"
                    ],
                ),
                (
                    "podcertificate-controller-system",
                    "Deployment",
                    "podcertificate-controller",
                    None,
                    ["podcertcontroller"],
                ),
            ],
        )

    def test_substrate_contract_is_fail_closed(self) -> None:
        for path, value, message in (
            ("extra", 1, "substrate: unexpected"),
            ("images", KeyError, "substrate: unexpected"),
            ("images/ateom-microvm", "sha256:" + "a" * 64, "substrate images"),
            ("images/ateapi", None, "ateapi must be pinned"),
            ("images/atelet", "sha256:" + "A" * 64, "atelet must be pinned"),
            ("images/atenet", "latest", "atenet must be pinned"),
            ("version", "672533", "abbreviation of the pinned"),
            ("version", "abcdef12", "abbreviation of the pinned"),
            ("version", 67253354, "abbreviation of the pinned"),
            ("version", "67253354-dirty", "abbreviation of the pinned"),
            ("backup_directory", "/var/backups/ax", "backup_directory must be"),
            ("build/memory_limit_mib", 4096, "memory_limit_mib exceeds the node"),
            ("build/memory_reservation_mib", 2048, "reservation_mib exceeds the node"),
            ("build/cpu_limit_millicores", 4000, "cpu_limit_millicores exceeds"),
            ("build/pids_limit", 8192, "pids_limit exceeds the node"),
            ("build/cpu_limit_millicores", 1500, "whole CPUs"),
            ("build/memory_reservation_mib", 1000, "ratio exceeds"),
            ("build/timeout_seconds", 60, "outside 600-7200"),
            ("build/min_mem_available_mib", 3072, "plus the operational headroom"),
            ("build/mem_available_floor_mib", 256, "operational headroom"),
            ("build/swap_mib", 0, "substrate build: unexpected"),
            ("install/atenet_router", "agentgateway", "must be envoy"),
            ("install/experimental_use_sdsmint", True, "install: unexpected"),
            ("install/rollout_timeout_seconds", 60, "outside 300-1800"),
            ("install/timeout_seconds", 1800, "cover ate-setup's own waits"),
            ("install/timeout_seconds", 20000, "cover ate-setup's own waits"),
            ("install/memory_limit_mib", 1024, "256 MiB of limits the capacity plan"),
            ("install/memory_reservation_mib", 64, "ratio exceeds"),
            ("install/cpu_limit_millicores", 2000, "850m of limits the capacity plan"),
            ("install/pids_limit", 2048, "PID limit is above the reviewed one"),
            ("install/min_mem_available_mib", 256, "plus the operational headroom"),
            ("install/mem_available_floor_mib", 1, "operational headroom"),
            ("workloads", [], "non-empty list"),
            ("workloads/0/name", "atelet-other", "atelet-<version> DaemonSet"),
            ("workloads/1/kind", "CronJob", "reviewed workload kind"),
            ("workloads/1/namespace", "kube-system", "outside ate-setup's"),
            ("workloads/1/images", ["ate-setup"], "installed Substrate image"),
            ("workloads/1/images", ["ateom-gvisor"], "installed Substrate image"),
            ("workloads/1/images", ["redis:7-alpine"], "installed Substrate image"),
            (
                "workloads/1/images",
                ["localhost:5001/ateapi:67253354@sha256:" + "a" * 64],
                "installed Substrate image",
            ),
            ("workloads/1/images", [], "non-empty list"),
            ("workloads/1/env", [], "unexpected"),
            ("workloads/2/name", "ate-api-server", "unique and sorted"),
            ("workloads/11/images", ["ateapi"], "every installed image"),
        ):
            with self.subTest(path=path, value=value):
                self.rejected(self.set_substrate(path, value), message)
        self.rejected(
            lambda document: document["ax_lab"]["substrate"]["workloads"].reverse(),
            "unique and sorted",
        )

    def test_only_ate_setup_may_wait_for_its_pin(self) -> None:
        pending = copy.deepcopy(self.document)
        pending["ax_lab"]["substrate"]["images"]["ate-setup"] = None
        pending["ax_lab_substrate_fallback_builds"] = []
        lab = self.module.validate_catalog(pending, self.reserved)
        self.assertIsNone(lab["substrate"]["images"]["ate-setup"])
        pinned = copy.deepcopy(self.document)
        pinned["ax_lab"]["substrate"]["images"]["ate-setup"] = ATE_SETUP
        self.module.validate_catalog(pinned, self.reserved)

    def test_toolbox_is_the_pinned_golang_image(self) -> None:
        for value, message in (
            (
                "docker.io/library/golang:1.26.0@sha256:" + "a" * 64,
                "toolbox must be golang 1.27.1",
            ),
            (
                "docker.io/library/debian:1.27.1@sha256:" + "a" * 64,
                "toolbox must use docker.io/library/golang",
            ),
            ("docker.io/library/golang:1.27.1", "repository:tag@sha256"),
        ):
            with self.subTest(value=value):
                self.rejected(
                    lambda document, value=value: document["ax_lab"]["images"].update(
                        toolbox=value
                    ),
                    message,
                )

    def test_fallback_builds_are_explicit_known_and_pinned(self) -> None:
        for value, message in (
            ("ate-setup", "distinct images"),
            (["ateapi", "ateapi"], "distinct images"),
            (["ateom-microvm"], "not a Substrate image"),
            (None, "distinct images"),
        ):
            with self.subTest(value=value):
                self.rejected(
                    lambda document, value=value: document.update(
                        ax_lab_substrate_fallback_builds=value
                    ),
                    message,
                )

        def pending_fallback(document: dict[str, Any]) -> None:
            document["ax_lab"]["substrate"]["images"]["ate-setup"] = None
            document.update(ax_lab_substrate_fallback_builds=["ate-setup"])

        self.rejected(pending_fallback, "no pinned digest to reproduce")
        allowed = copy.deepcopy(self.document)
        allowed["ax_lab"]["substrate"]["images"]["ate-setup"] = ATE_SETUP
        allowed["ax_lab_substrate_fallback_builds"] = ["ate-setup"]
        self.module.validate_catalog(allowed, self.reserved)
        self.rejected(
            lambda document: document.pop("ax_lab_substrate_fallback_builds"),
            "config: unexpected",
        )

    def test_ate_setup_fits_in_what_the_capacity_plan_leaves_free(self) -> None:
        """F11: never the operational headroom, which stays apart."""
        # 15981 - 3072 - 512 = 12397 MiB and (8000 - 1000) x 2.50 = 17500m of
        # limits, of which the organizationweb plan commits 12141 and 16650.
        self.assertEqual(self.module.load_free_limit_budget(), (256, 850))

        def install(**values: int):
            def change(document: dict[str, Any]) -> None:
                document["ax_lab"]["substrate"]["install"].update(values)

            return change

        for values, message in (
            ({"memory_limit_mib": 257, "min_mem_available_mib": 769}, "exceeds the 256 MiB"),
            ({"cpu_limit_millicores": 851}, "exceeds the 850m"),
        ):  # fmt: skip
            with self.subTest(values=values):
                self.rejected(install(**values), message)
        for values in ({"memory_limit_mib": 256}, {"cpu_limit_millicores": 850}):
            with self.subTest(values=values):
                accepted = copy.deepcopy(self.document)
                install(**values)(accepted)
                self.module.validate_catalog(accepted, self.reserved)
        # A plan that frees less memory refuses the reviewed 256 MiB.
        with self.assertRaisesRegex(self.module.AxLabError, "exceeds the 128 MiB"):
            self.module.validate_catalog(self.document, self.reserved, free=(128, 850))

    def test_the_install_timeout_outlasts_every_wait_of_ate_setup(self) -> None:
        """F2: 10 waits bounded by --rollout-timeout plus 180 s, and 300 s."""
        self.assertEqual(self.module.INSTALL_ROLLOUT_WAITS, 10)
        self.assertEqual(self.module.INSTALL_FIXED_WAIT_SECONDS, 180)
        for rollout, timeout, accepted in (
            (600, 6480, True),
            (600, 6479, False),
            (480, 5280, True),
            (480, 5279, False),
        ):
            with self.subTest(rollout=rollout, timeout=timeout):
                document = copy.deepcopy(self.document)
                document["ax_lab"]["substrate"]["install"].update(
                    rollout_timeout_seconds=rollout, timeout_seconds=timeout
                )
                if accepted:
                    self.module.validate_catalog(document, self.reserved)
                else:
                    with self.assertRaisesRegex(
                        self.module.AxLabError, "cover ate-setup's own waits"
                    ):
                        self.module.validate_catalog(document, self.reserved)

    def test_headroom_comes_from_the_capacity_contract(self) -> None:
        self.assertEqual(self.module.load_operational_headroom(), 512)
        with self.assertRaisesRegex(self.module.AxLabError, "headroom"):
            self.module.validate_catalog(self.document, self.reserved, headroom=256)

    def test_validator_prints_the_substrate_plan(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/validate-ax-lab.py"),
                "--substrate-plan",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        plan = json.loads(completed.stdout)
        lab = self.document["ax_lab"]
        self.assertEqual(
            plan,
            {
                "version": "67253354",
                "commit": lab["sources"]["substrate"]["commit"],
                "repository": "https://github.com/agent-substrate/substrate",
                "toolbox_image": lab["images"]["toolbox"],
                "registry_image": lab["images"]["registry"],
                "build": lab["substrate"]["build"],
                "images": lab["substrate"]["images"],
            },
        )


class SubstrateRoleTests(AnsibleTaskAssertions, unittest.TestCase):
    """The role's Substrate tasks, exercised on synthetic reads."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.main = load_tasks(MAIN)
        cls.artifacts = load_tasks(ARTIFACTS)
        cls.images = load_tasks(IMAGES)
        cls.images_read = load_tasks(IMAGES_READ)
        cls.node = load_tasks(NODE)
        cls.substrate = load_tasks(SUBSTRATE)
        cls.substrate_read = load_tasks(SUBSTRATE_READ)
        cls.variables = pinned_variables()

    def derive(self) -> list[dict[str, Any]]:
        return [
            self.main["Derive the pinned Substrate arguments and install identity"],
            self.main["Derive the installed Substrate image arguments and identity"],
        ]

    def run_tasks(self, tasks, **extra: Any):
        return run_reviewed_tasks(tasks, {**self.variables, **extra})

    def assert_pass(self, tasks, **extra: Any) -> None:
        completed = self.run_tasks(tasks, **extra)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def assert_fail(self, tasks, message: str, **extra: Any) -> None:
        completed = self.run_tasks(tasks, **extra)
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0, output)
        self.assertIn(message, " ".join(output.split()))

    # -- main.yml ----------------------------------------------------------

    def test_a_pending_pin_stops_every_run(self) -> None:
        name = "Refuse to run while a Substrate image pin is pending"
        pending = copy.deepcopy(cluster_variables())
        pending["ax_lab"]["substrate"]["images"]["ate-setup"] = None
        self.assert_task_rejects(MAIN, name, pending, "has no pinned digest yet")
        self.assert_task_accepts(MAIN, name, self.variables)
        names = list(self.main)
        self.assertLess(
            names.index(name), names.index("Reconcile the lab host prerequisites")
        )
        self.assertLess(
            names.index(
                "Compute the digest of the reviewed kind configuration locally"
            ),
            names.index(name),
        )

    def test_arguments_and_identity_derive_from_the_contract(self) -> None:
        self.assert_pass(
            [
                *self.derive(),
                probe("ax_lab_substrate_installed == expected_installed"),
                probe("ax_lab_substrate_identity == expected_identity"),
                probe("ax_lab_substrate_image_args == expected_args"),
                probe("ax_lab_substrate_installed_args == expected_installed_args"),
            ],
            expected_installed=INSTALLED,
            expected_identity={
                key: value
                for key, value in expected_identity().items()
                if key != "node_container_id"
            },
            expected_args=[
                f"--image={name}={digest}"
                for name, digest in {
                    **MANUAL_LAB_DIGESTS,
                    "ate-setup": ATE_SETUP,
                }.items()
            ],
            expected_installed_args=[
                f"--image={name}={MANUAL_LAB_DIGESTS[name]}" for name in INSTALLED
            ],
        )

    def test_the_node_starts_only_after_its_images_and_before_substrate(self) -> None:
        names = list(self.main)
        # AX's images are restored before the node starts, and AX is
        # installed after Substrate, whose WorkerPool kind it uses.
        applies = [
            ("Keep the Substrate checkout and fallback builds outside check mode", "artifacts.yml"),
            ("Reconcile the lab cluster and its local registry outside check mode", "cluster.yml"),
            ("Seed and verify the pinned Substrate images outside check mode", "images.yml"),
            ("Seed and verify the pinned AX images outside check mode", "ax_images.yml"),
            ("Start and prepare the lab node outside check mode", "node.yml"),
            ("Install Substrate only on drift outside check mode", "substrate.yml"),
            ("Install AX only on drift and repair its workers outside check mode", "ax.yml"),
        ]  # fmt: skip
        self.assertEqual(names[-7:], [name for name, _file in applies])
        for name, task_file in applies:
            with self.subTest(task=name):
                self.assertEqual(
                    self.main[name],
                    {
                        "name": name,
                        "ansible.builtin.import_tasks": task_file,
                        "when": "not ansible_check_mode",
                    },
                )
        checks = [
            ("Read the pinned Substrate images in the registry and the backup", "images_read.yml"),
            ("Read Substrate in the lab cluster and prove its ownership", "substrate_read.yml"),
            ("Read the pinned AX images in the registry and the backup", "ax_images_read.yml"),
            ("Read AX in the lab cluster and prove its ownership", "ax_read.yml"),
        ]  # fmt: skip
        for name, task_file in checks:
            with self.subTest(task=name):
                self.assertEqual(
                    self.main[name],
                    {
                        "name": name,
                        "ansible.builtin.import_tasks": task_file,
                        "when": "ansible_check_mode",
                    },
                )
                self.assertLess(
                    names.index("Reconcile the lab host prerequisites"),
                    names.index(name),
                )

    def test_check_mode_reports_the_substrate_plan(self) -> None:
        describe = self.main["Describe what an apply would change in Substrate"]
        self.assertEqual(describe["when"], "ansible_check_mode")
        paths = {
            "results": [
                {
                    "item": {"path": self.variables["ax_lab_substrate_source_path"]},
                    "stat": {"exists": False},
                }
            ]
        }
        backup = {**everywhere("complete"), "ate-setup": "missing"}
        clone = (
            "clone https://github.com/agent-substrate/substrate at "
            "672533541dbfcd29084e4de2475267088bda3651"
        )
        cases = (
            (
                "lab window, registry and node absent",
                image_status(None, backup),
                ["ate-setup"],
                None,
                True,
                [
                    clone,
                    "build with the fallback toolbox: ate-setup",
                    "the local registry is read once the apply starts it",
                    "Substrate is read after the apply starts the lab node",
                ],
            ),
            (
                "stopped node, build pending",
                image_status(None, backup),
                ["ate-setup"],
                None,
                {"status": "exited"},
                [
                    clone,
                    "build with the fallback toolbox: ate-setup",
                    "the local registry is read once the apply starts it",
                    "Substrate is read after the apply starts the lab node",
                ],
            ),
            # F3: the apply refuses to build beside a running node, so the
            # plan reports that refusal, never a build.
            (
                "running node, build pending",
                image_status({**everywhere("pinned"), "ate-setup": "missing"}, backup),
                ["ate-setup"],
                cluster_read(),
                {},
                [
                    clone,
                    "the apply stops before building ate-setup: kind-control-plane"
                    " runs, so delete the remaining AX Tasks (sudo ax delete task"
                    " <name> -a default), stop it under the host-global lock and"
                    " apply again (docs/AX.md, «Compilación de reserva»)",
                    "install Substrate with ate-setup: state",
                ],
            ),
            (
                "moved tag, drifted install",
                image_status({**everywhere("pinned"), "atelet": "moved"}, everywhere("complete")),
                [],
                cluster_read(node_label=None),
                {},
                [
                    clone,
                    "restore into the registry: atelet",
                    "install Substrate with ate-setup: state, label",
                ],
            ),
        )  # fmt: skip
        for label, status, fallback, cluster, node, expected in cases:
            with self.subTest(case=label):
                self.assert_pass(
                    [
                        *self.derive(),
                        *list(self.images_read.values())[1:],
                        *[
                            task
                            for task in self.substrate_read.values()
                            if "ansible.builtin.set_fact" in task
                        ],
                        {
                            key: value
                            for key, value in describe.items()
                            if key != "when"
                        },
                        probe("ax_lab_substrate_plan == expected"),
                    ],
                    ax_lab_image_status_raw=status,
                    ax_lab_substrate_fallback_builds=fallback,
                    ax_lab_install_paths=paths,
                    expected=expected,
                    **substrate_reads(
                        state=None,
                        cluster=cluster,
                        **({"node_absent": True} if node is True else node),
                    ),
                )

    # -- images_read.yml, artifacts.yml, images.yml -------------------------

    def test_images_move_build_or_are_lost_by_where_they_are_held(self) -> None:
        decide = [*self.derive(), *list(self.images_read.values())[1:]]
        self.assertEqual(
            list(self.images_read)[0],
            "Read the pinned Substrate images in the registry and the backup",
        )
        seeded = {**everywhere("complete"), "ate-setup": "missing"}
        six = [name for name in NAMES if name != "ate-setup"]
        for label, status, fallback, expected in (
            (
                "window: backup seeded, registry absent, ate-setup allowed",
                image_status(None, seeded),
                ["ate-setup"],
                {"builds": ["ate-setup"], "exports": [], "imports": six, "lost": ["ate-setup"]},
            ),
            (
                "converged",
                image_status(everywhere("pinned"), everywhere("complete")),
                ["ate-setup"],
                {"builds": [], "exports": [], "imports": [], "lost": []},
            ),
            (
                "registry volume lost",
                image_status(everywhere("missing"), everywhere("complete")),
                [],
                {"builds": [], "exports": [], "imports": list(NAMES), "lost": []},
            ),
            (
                "backup lost",
                image_status(everywhere("pinned"), everywhere("missing")),
                ["ateapi"],
                {"builds": [], "exports": list(NAMES), "imports": [], "lost": []},
            ),
            (
                "moved tag in the registry",
                image_status({**everywhere("pinned"), "ateapi": "moved"}, everywhere("complete")),
                [],
                {"builds": [], "exports": [], "imports": ["ateapi"], "lost": []},
            ),
            (
                "both lost, not allowed",
                image_status({**everywhere("pinned"), "atenet": "missing"}, {**everywhere("complete"), "atenet": "missing"}),
                [],
                {"builds": [], "exports": [], "imports": [], "lost": ["atenet"]},
            ),
        ):  # fmt: skip
            with self.subTest(case=label):
                self.assert_pass(
                    [
                        *decide,
                        probe(
                            "ax_lab_substrate_builds | sort == expected.builds | sort"
                        ),
                        probe(
                            "ax_lab_substrate_exports | sort == expected.exports | sort"
                        ),
                        probe(
                            "ax_lab_substrate_imports | sort == expected.imports | sort"
                        ),
                        probe("ax_lab_substrate_lost | sort == expected.lost | sort"),
                    ],
                    ax_lab_image_status_raw=status,
                    ax_lab_substrate_fallback_builds=fallback,
                    expected=expected,
                )

    def test_image_reads_are_check_safe_and_read_the_registry_only_while_it_runs(
        self,
    ) -> None:
        read = self.images_read[
            "Read the pinned Substrate images in the registry and the backup"
        ]
        self.assertIs(read["check_mode"], False)
        self.assertIs(read["changed_when"], False)
        self.assertEqual(
            read["ansible.builtin.script"]["executable"], "/usr/bin/python3"
        )
        for status, expected in (("running", True), ("exited", False)):
            with self.subTest(registry=status):
                self.assert_pass(
                    [
                        *self.derive(),
                        {
                            "name": "Render the read",
                            "ansible.builtin.set_fact": {
                                "probe_cmd": read["ansible.builtin.script"]["cmd"]
                            },
                        },
                        probe(
                            "('--registry 127.0.0.1:5001' in probe_cmd) == expected"
                            " and probe_cmd.split()[1] == 'image-status'"
                            " and probe_cmd.split()[0].endswith("
                            "'/scripts/manage-ax-lab-substrate.py')"
                        ),
                    ],
                    ax_lab_registry=json.loads(
                        container_read("kind-registry", status=status)["stdout"]
                    ),
                    expected=expected,
                )
        self.assert_pass(
            [
                *self.derive(),
                {
                    "name": "Render the read without a registry",
                    "ansible.builtin.set_fact": {
                        "probe_cmd": read["ansible.builtin.script"]["cmd"]
                    },
                },
                probe("'--registry' not in probe_cmd"),
            ],
            ax_lab_registry=None,
        )

    def test_the_manager_is_installed_with_the_lock_helpers_it_imports(self) -> None:
        install = self.artifacts[
            "Install the Substrate manager and the lock helpers it imports"
        ]
        self.assertEqual(
            install["loop"],
            [
                "ansible-operation-lock.py",
                "host_global_operation_lock.py",
                "manage-ax-lab-substrate.py",
                "run-locked-command.py",
            ],
        )
        self.assertEqual(install["ansible.builtin.copy"]["mode"], "0755")
        directories = self.artifacts[
            "Create the Substrate source, cache and backup directories"
        ]
        self.assertEqual(
            [(item["path"], item["mode"]) for item in directories["loop"]],
            [
                ("{{ ax_lab.install_root }}/src", "0750"),
                ("{{ ax_lab_cache_directory }}", "0700"),
                ("/var/backups/dockerswarm", "0700"),
                ("{{ ax_lab.substrate.backup_directory | dirname }}", "0700"),
            ],
        )

    def test_the_checkout_is_cloned_only_when_missing_and_must_be_pristine(
        self,
    ) -> None:
        clone = self.artifacts[
            "Clone the pinned Substrate commit only when the checkout is missing"
        ]
        self.assertEqual(clone["when"], "not ax_lab_substrate_checkout.stat.exists")
        self.assertEqual(
            clone["ansible.builtin.git"],
            {
                "repo": "{{ ax_lab.sources.substrate.repository }}",
                "dest": "{{ ax_lab_substrate_source_path }}",
                "version": "{{ ax_lab.sources.substrate.commit }}",
                "update": False,
            },
        )
        identity = self.artifacts["Read the identity of the Substrate checkout"]
        self.assertEqual(
            identity["loop"],
            [
                ["rev-parse", "HEAD"],
                ["remote", "get-url", "origin"],
                ["status", "--porcelain", "--untracked-files=all"],
            ],
        )
        self.assertIs(identity["changed_when"], False)
        # H3: no `git describe` in any task, only in the comment saying why.
        for path in role_task_files():
            for task in yaml.safe_load(path.read_text(encoding="utf-8")):
                with self.subTest(task=task["name"]):
                    self.assertNotIn("describe", json.dumps(task))
        name = "Require the pristine pinned Substrate checkout"
        lab = self.variables["ax_lab"]

        def reads(*stdout: str) -> dict[str, Any]:
            return {
                **self.variables,
                "ax_lab_substrate_checkout_identity": {
                    "results": [{"stdout": value} for value in stdout]
                },
            }

        commit = lab["sources"]["substrate"]["commit"]
        origin = lab["sources"]["substrate"]["repository"]
        self.assert_task_accepts(ARTIFACTS, name, reads(commit, origin, ""))
        for values in (
            ("0" * 40, origin, ""),
            (commit, "https://github.com/someone/substrate", ""),
            (commit, origin, "?? bin/"),
            (commit, origin, " M go.mod"),
        ):
            with self.subTest(values=values):
                self.assert_task_rejects(
                    ARTIFACTS, name, reads(*values), "is not a clean checkout"
                )

    def test_a_transient_container_left_behind_stops_every_run(self) -> None:
        """F12: nothing starts beside a build or install that escaped."""
        inspect = load_tasks(INSPECT)
        read = inspect["Read the transient Substrate build and install containers"]
        refuse = inspect[
            "Refuse a transient Substrate container left from an earlier run"
        ]
        names = list(inspect)
        # Both modes, before anything the role writes or starts.
        self.assertLess(
            names.index("Prove that every existing lab container is this role's"),
            names.index(read["name"]),
        )
        self.assertEqual(names.index(refuse["name"]), names.index(read["name"]) + 1)
        self.assertLess(
            list(self.main).index(
                "Read the lab cluster and registry and prove their ownership"
            ),
            list(self.main).index("Reconcile the lab host prerequisites"),
        )
        self.assertEqual(
            read["ansible.builtin.command"]["argv"][:4],
            ["/usr/bin/docker", "container", "inspect", "--format"],
        )
        for key, value in (
            ("check_mode", False),
            ("changed_when", False),
            ("failed_when", False),
        ):
            self.assertIs(read[key], value)
        expected = [f"ax-lab-build-{name}" for name in NAMES] + ["ax-lab-ate-setup"]
        self.assert_pass(
            [
                {
                    "name": "Render the loop",
                    "ansible.builtin.set_fact": {"probe_loop": read["loop"]},
                },
                probe("probe_loop | sort == expected | sort"),
            ],
            expected=expected,
        )
        manager = load_manager()
        self.assertEqual(
            sorted(expected),
            sorted(
                [manager.BUILD_CONTAINER_PREFIX + name for name in manager.IMAGE_NAMES]
                + [manager.INSTALL_CONTAINER]
            ),
        )

        def result(name: str, rc: int, stdout: str = "", stderr: str = ""):
            return {"item": name, "rc": rc, "stdout": stdout, "stderr": stderr}

        absent = [
            result(name, 1, stderr=f"Error: No such container: {name}\n")
            for name in expected
        ]
        self.assert_pass([refuse], ax_lab_substrate_transient_reads={"results": absent})
        for label, changed, message in (
            ("killed and kept", result("ax-lab-build-ateapi", 0, '"exited"'), "ax-lab-build-ateapi is left from an earlier run"),
            ("still running", result("ax-lab-ate-setup", 0, '"running"'), "ax-lab-ate-setup is still running under an earlier ax-lab run"),
            ("unreadable", result("ax-lab-ate-setup", 1, stderr="Error response from daemon: boom"), "Cannot read the container ax-lab-ate-setup"),
        ):  # fmt: skip
            with self.subTest(case=label):
                results = [
                    changed if item["item"] == changed["item"] else item
                    for item in absent
                ]
                self.assert_fail(
                    [refuse],
                    message,
                    ax_lab_substrate_transient_reads={"results": results},
                )

    def test_a_fallback_build_never_runs_beside_the_node(self) -> None:
        name = "Refuse a fallback build while the lab node runs"
        for builds, status, accepted in (
            ([], "running", True),
            (["ate-setup"], "exited", True),
            (["ate-setup"], "running", False),
        ):
            with self.subTest(builds=builds, node=status):
                variables = {
                    **self.variables,
                    "ax_lab_substrate_builds": builds,
                    "ax_lab_node": json.loads(
                        container_read("kind-control-plane", status=status)["stdout"]
                    ),
                }
                if accepted:
                    self.assert_task_accepts(ARTIFACTS, name, variables)
                else:
                    self.assert_task_rejects(
                        ARTIFACTS, name, variables, "borrows the node's budget"
                    )
        self.assert_task_accepts(
            ARTIFACTS,
            name,
            {
                **self.variables,
                "ax_lab_substrate_builds": ["ate-setup"],
                "ax_lab_node": None,
            },
        )
        names = list(self.artifacts)
        self.assertEqual(
            names[-3:],
            [
                "Read the pinned Substrate images before the cluster changes",
                name,
                "Build each missing Substrate image in the bounded toolbox",
            ],
        )

    def test_the_build_is_the_managers_bounded_build_of_each_listed_image(self) -> None:
        build = self.artifacts[
            "Build each missing Substrate image in the bounded toolbox"
        ]
        self.assertEqual(build["loop"], "{{ ax_lab_substrate_builds }}")
        self.assertEqual(build["environment"], "{{ operation_lock_guard_environment }}")
        self.assertEqual(
            build["async"], "{{ ax_lab.substrate.build.timeout_seconds + 600 }}"
        )
        self.assertEqual(build["poll"], 30)
        self.assertIs(build["changed_when"], True)
        argv = build["ansible.builtin.command"]["argv"]
        self.assertEqual(
            argv[:3], ["/usr/bin/python3", "{{ ax_lab_substrate_manager }}", "build"]
        )
        self.assert_pass(
            [
                {
                    "name": "Render the build argv",
                    "ansible.builtin.set_fact": {"probe_argv": argv},
                    "vars": {"item": "ate-setup"},
                },
                probe("probe_argv[3:] | map('string') | list == expected"),
            ],
            expected=[
                "--image", f"ate-setup={ATE_SETUP}",
                "--toolbox-image", self.variables["ax_lab"]["images"]["toolbox"],
                "--source", "/opt/dockerswarm/ax-lab/src/substrate",
                "--cache", "/opt/dockerswarm/ax-lab/cache",
                "--layout", "/var/backups/dockerswarm/ax-lab/images",
                "--version", "67253354",
                "--node-container", "kind-control-plane",
                "--memory-mib", "3072",
                "--memory-reservation-mib", "1536",
                "--cpu-millicores", "2000",
                "--pids-limit", "1024",
                "--timeout-seconds", "3600",
                "--min-mem-available-mib", "3584",
                "--mem-available-floor-mib", "512",
            ],
        )  # fmt: skip

    def test_lost_images_stop_the_apply_and_moves_happen_only_when_needed(self) -> None:
        lost = "Refuse a pinned Substrate image held by neither backup nor registry"
        base = {
            **self.variables,
            "ax_lab_image_status": {"registry": everywhere("pinned")},
            "ax_lab_substrate_lost": [],
        }
        self.assert_task_accepts(IMAGES, lost, base)
        self.assert_task_rejects(
            IMAGES,
            lost,
            {**base, "ax_lab_substrate_lost": ["atenet"]},
            "atenet is in neither",
        )
        self.assert_task_rejects(
            IMAGES,
            lost,
            {**base, "ax_lab_image_status": {"registry": None}},
            "is in neither",
        )
        for name, verb, fact in (
            ("Back up the pinned Substrate images the backup lacks", "export", "ax_lab_substrate_exports"),
            ("Restore the pinned Substrate images the registry lacks", "import", "ax_lab_substrate_imports"),
        ):  # fmt: skip
            with self.subTest(task=name):
                task = self.images[name]
                self.assertEqual(task["when"], f"{fact} | length > 0")
                self.assertEqual(
                    task["environment"], "{{ operation_lock_guard_environment }}"
                )
                self.assertIs(task["changed_when"], True)
                self.assert_pass(
                    [
                        {
                            "name": "Render the argv",
                            "ansible.builtin.set_fact": {
                                "probe_argv": task["ansible.builtin.command"]["argv"]
                            },
                        },
                        probe("probe_argv == expected"),
                    ],
                    **{fact: ["ateapi"]},
                    expected=[
                        "/usr/bin/python3",
                        "/opt/dockerswarm/ax-lab/bin/manage-ax-lab-substrate.py",
                        verb,
                        "--registry",
                        "127.0.0.1:5001",
                        "--layout",
                        "/var/backups/dockerswarm/ax-lab/images",
                        "--tag",
                        "67253354",
                        f"--image=ateapi={MANUAL_LAB_DIGESTS['ateapi']}",
                    ],
                )

    def test_every_pin_is_verified_after_seeding_and_the_registry_never_oomed(
        self,
    ) -> None:
        verify = "Verify every pinned Substrate image in the registry and the backup"
        good = {
            **self.variables,
            "ax_lab_image_status": {"registry": everywhere("pinned")},
            "ax_lab_substrate_in_registry": list(NAMES),
            "ax_lab_substrate_in_backup": list(NAMES),
        }
        self.assert_task_accepts(IMAGES, verify, good)
        for change in (
            {"ax_lab_substrate_in_registry": list(NAMES[:-1])},
            {"ax_lab_substrate_in_backup": list(NAMES[1:])},
            {"ax_lab_image_status": {"registry": None}},
        ):
            with self.subTest(change=list(change)):
                self.assert_task_rejects(
                    IMAGES, verify, {**good, **change}, "does not hold every pinned"
                )
        gate = "Require no OOM kill in the local registry"

        def events(text: str) -> dict[str, Any]:
            return {
                **self.variables,
                "ax_lab_registry_memory_events": {
                    "content": base64.b64encode(text.encode()).decode()
                },
            }

        self.assert_task_accepts(
            IMAGES, gate, events("low 0\nmax 12\noom 0\noom_kill 0\n")
        )
        for text in ("oom_kill 1\n", "low 0\n", "oom_kill 0\noom_kill 0\n"):
            with self.subTest(events=text):
                self.assert_task_rejects(IMAGES, gate, events(text), "killed a process")
        names = list(self.images)
        self.assertLess(names.index(verify), names.index(gate))

    # -- substrate_read.yml, substrate.yml ----------------------------------

    def decisions(self) -> list[dict[str, Any]]:
        return [
            *self.derive(),
            *[
                task
                for task in self.substrate_read.values()
                if "ansible.builtin.set_fact" in task
            ],
        ]

    def test_substrate_is_read_only_while_the_node_runs_with_its_kubeconfig(
        self,
    ) -> None:
        read = self.substrate_read["Read Substrate in the lab cluster"]
        self.assertEqual(
            read["when"],
            [
                "ax_lab_node is not none",
                'ax_lab_node.status == "running"',
                "ax_lab_substrate_kubeconfig.stat.exists",
            ],
        )
        self.assertIs(read["check_mode"], False)
        self.assertIs(read["changed_when"], False)
        self.assert_pass(
            [
                *self.derive(),
                {
                    "name": "Render the read",
                    "ansible.builtin.set_fact": {
                        "probe_cmd": read["ansible.builtin.script"]["cmd"]
                    },
                },
                probe("probe_cmd.split() == expected"),
            ],
            expected=[
                str(ROLE) + "/../../../scripts/manage-ax-lab-substrate.py",
                "cluster-status",
                "--kubectl", "/opt/dockerswarm/ax-lab/bin/kubectl",
                "--kubeconfig", "/opt/dockerswarm/ax-lab/home/.kube/config",
                "--home", "/opt/dockerswarm/ax-lab/home",
                "--context", "kind-kind",
                "--node", "kind-control-plane",
                "--registry-port", "5001",
                "--tag", "67253354",
                *[f"--image={name}={MANUAL_LAB_DIGESTS[name]}" for name in INSTALLED],
            ],
        )  # fmt: skip
        # M5: a stopped node or a first run reads nothing and decides nothing.
        self.assert_pass(
            [
                *self.decisions(),
                probe("ax_lab_substrate_cluster is none"),
                probe("ax_lab_substrate_drift == []"),
                probe("ax_lab_substrate_present == []"),
            ],
            **substrate_reads(state=None, cluster=None, status="exited"),
        )
        name = "Require a root-only regular Substrate state file when there is one"
        good = {
            "exists": True,
            "isreg": True,
            "islnk": False,
            "uid": 0,
            "gid": 0,
            "mode": "0600",
        }
        self.assert_task_accepts(
            SUBSTRATE_READ,
            name,
            {
                **self.variables,
                "ax_lab_substrate_state_file": {"stat": {"exists": False}},
            },
        )
        self.assert_task_accepts(
            SUBSTRATE_READ,
            name,
            {**self.variables, "ax_lab_substrate_state_file": {"stat": good}},
        )
        for change in (
            {"mode": "0644"},
            {"uid": 1001},
            {"islnk": True},
            {"isreg": False},
        ):
            with self.subTest(change=change):
                self.assert_task_rejects(
                    SUBSTRATE_READ,
                    name,
                    {
                        **self.variables,
                        "ax_lab_substrate_state_file": {"stat": {**good, **change}},
                    },
                    "is not a root:root 0600 regular file",
                )

    def test_drift_is_the_state_the_label_or_the_workloads(self) -> None:
        workloads = cluster_read()["workloads"]
        moved = copy.deepcopy(workloads)
        moved[1]["images"] = ["localhost:5001/ateapi:67253354@sha256:" + "9" * 64]
        other = expected_identity()
        other["install"] = {**other["install"], "rollout_timeout_seconds": 300}
        for label, state, cluster, expected in (
            ("converged", state_file(), cluster_read(), []),
            ("first install", None, cluster_read(node_label=None, workloads=[]), ["state", "label", "workloads"]),
            ("interrupted install", state_file(phase="installing"), cluster_read(), ["state"]),
            ("recreated node", state_file(node_container_id="c" * 64), cluster_read(), ["state"]),
            ("new pins", state_file(**{key: value for key, value in other.items()}), cluster_read(), ["state"]),
            ("label removed", state_file(), cluster_read(node_label=None), ["label"]),
            ("moved image", state_file(), cluster_read(workloads=moved), ["workloads"]),
            ("extra workload", state_file(), cluster_read(workloads=[*workloads, {"kind": "Job", "namespace": "ate-system", "name": "x", "images": ["y"]}]), ["workloads"]),
        ):  # fmt: skip
            with self.subTest(case=label):
                self.assert_pass(
                    [*self.decisions(), probe("ax_lab_substrate_drift == expected")],
                    expected=expected,
                    **substrate_reads(state=state, cluster=cluster, id=NODE_ID),
                )

    def test_create_once_objects_are_never_adopted_or_regenerated(self) -> None:
        refusals = [
            task
            for name, task in self.substrate_read.items()
            if name.startswith("Refuse")
        ]
        self.assertEqual(len(refusals), 6)
        for task in refusals:
            self.assertIn("ax_lab_substrate_cluster is not none", str(task["when"]))
        present = cluster_read()["create_once"]
        uids = {key: f"uid-{index}" for index, key in enumerate(CREATE_ONCE)}
        partial = {**present, CREATE_ONCE[4]: None}
        tls = copy.deepcopy(present)
        tls[CREATE_ONCE[1]]["type"] = "kubernetes.io/tls"
        regenerated = copy.deepcopy(present)
        regenerated[CREATE_ONCE[0]]["uid"] = "new"
        missing = {**present, CREATE_ONCE[2]: None}
        pool_gone = {**present, CREATE_ONCE[1]: None}
        pods_gone = {**present, CREATE_ONCE[3]: None, CREATE_ONCE[4]: None}
        first = {**dict.fromkeys(CREATE_ONCE), CREATE_ONCE[0]: present[CREATE_ONCE[0]]}
        for label, state, cluster, message in (
            ("converged", state_file(), cluster_read(), None),
            ("fresh cluster", None, cluster_read(create_once=dict.fromkeys(CREATE_ONCE)), None),
            ("recreated cluster", state_file(node_container_id="c" * 64), cluster_read(create_once=dict.fromkeys(CREATE_ONCE)), None),
            ("interrupted first install", state_file(phase="installing", create_once={}), cluster_read(create_once=first), None),
            ("interrupted reinstall", state_file(phase="installing", create_once=uids), cluster_read(), None),
            ("never adopted", None, cluster_read(), "are never adopted"),
            ("state of another node", state_file(node_container_id="c" * 64), cluster_read(), "are never adopted"),
            ("regenerated", state_file(), cluster_read(create_once=regenerated), "was regenerated"),
            ("deleted", state_file(), cluster_read(create_once=missing), "is missing or was regenerated"),
            ("appeared beside a verified install", state_file(create_once={k: v for k, v in uids.items() if k != CREATE_ONCE[5]}), cluster_read(), "is missing or was regenerated"),
            # F1: an interrupted reinstall is held to what the verified
            # install recorded, whatever its phase.
            ("interrupted reinstall, pool deleted", state_file(phase="installing", create_once=uids), cluster_read(create_once=pool_gone), "is missing or was regenerated"),
            ("interrupted reinstall, both pod pools deleted", state_file(phase="installing", create_once=uids), cluster_read(create_once=pods_gone), "is missing or was regenerated"),
            ("interrupted reinstall, regenerated", state_file(phase="installing", create_once=uids), cluster_read(create_once=regenerated), "was regenerated"),
            ("partial pod certificate pools", state_file(phase="installing", create_once={}), cluster_read(create_once=partial), "Only one of the pod certificate CA pools"),
            ("actor root without its pool", state_file(phase="installing", create_once={}), cluster_read(create_once=pool_gone), "actor-id-ca-certs exists without actor-id-ca-pool"),
            ("shell installer secret", state_file(phase="installing", create_once={}), cluster_read(create_once=tls), "is not of type Opaque"),
            ("other version label", state_file(), cluster_read(node_label="deadbeef1"), "ate-setup never relabels"),
        ):  # fmt: skip
            with self.subTest(case=label):
                tasks = [*self.decisions(), *refusals]
                variables = substrate_reads(state=state, cluster=cluster, id=NODE_ID)
                if message is None:
                    self.assert_pass(tasks, **variables)
                else:
                    self.assert_fail(tasks, message, **variables)

    def test_an_interrupted_reinstall_keeps_the_recorded_uids(self) -> None:
        """F1: installed, workloads drift, ate-setup fails, a pool is deleted.

        The intent of the failed reinstall carries the verified UIDs, so the
        next apply refuses instead of letting ate-setup mint a new CA pool
        beside the old root in actor-id-ca-certs.
        """
        intent = self.substrate["Record the Substrate install intent"]
        refusals = [
            task
            for name, task in self.substrate_read.items()
            if name.startswith("Refuse")
        ]
        uids = {key: f"uid-{index}" for index, key in enumerate(CREATE_ONCE)}
        moved = copy.deepcopy(cluster_read()["workloads"])
        moved[1]["images"] = ["localhost:5001/ateapi:67253354@sha256:" + "9" * 64]
        render = {
            "name": "Render the intent",
            "ansible.builtin.set_fact": {
                "probe_intent": intent["ansible.builtin.copy"]["content"]
            },
        }
        for label, state, expected in (
            ("verified install of this node", state_file(), uids),
            ("interrupted reinstall", state_file(phase="installing", create_once=uids), uids),
            ("first install", None, {}),
            ("another node", state_file(node_container_id="c" * 64), {}),
        ):  # fmt: skip
            with self.subTest(case=label):
                self.assert_pass(
                    [
                        *self.decisions(),
                        probe("ax_lab_substrate_drift | length > 0"),
                        render,
                        probe("(probe_intent | from_json).phase == 'installing'"),
                        probe("(probe_intent | from_json).create_once == expected"),
                    ],
                    expected=expected,
                    **substrate_reads(
                        state=state,
                        cluster=cluster_read(
                            workloads=moved,
                            create_once=(
                                cluster_read()["create_once"]
                                if state is not None
                                and state["node_container_id"] == NODE_ID
                                else dict.fromkeys(CREATE_ONCE)
                            ),
                        ),
                        id=NODE_ID,
                    ),
                )
        pool_gone = {**cluster_read()["create_once"], CREATE_ONCE[1]: None}
        self.assert_fail(
            [*self.decisions(), *refusals],
            "is missing or was regenerated",
            **substrate_reads(
                state={
                    **expected_identity(),
                    "phase": "installing",
                    "create_once": uids,
                },
                cluster=cluster_read(workloads=moved, create_once=pool_gone),
                id=NODE_ID,
            ),
        )

    def test_substrate_installs_only_on_drift_then_verifies_and_waits(self) -> None:
        names = list(self.substrate)
        self.assertEqual(
            names,
            [
                "Read Substrate in the lab cluster and prove its ownership",
                "Require Substrate to be readable in the running lab node",
                "Keep the Substrate reads from before any install",
                "Read the AX operator helpers that are running before Substrate changes",
                "Refuse to reinstall Substrate while an ax-tarea or the ax CLI runs",
                "Record the Substrate install intent",
                "Install Substrate with the pinned ate-setup only on drift",
                "Read Substrate after the install",
                "Verify the installed Substrate identity",
                "Record the proof of the installed Substrate",
                "Wait for every Substrate workload to be ready",
            ],
        )
        for name in (names[0], names[7]):
            self.assertEqual(
                self.substrate[name]["ansible.builtin.import_tasks"],
                "substrate_read.yml",
            )
        intent = self.substrate["Record the Substrate install intent"]
        install = self.substrate[
            "Install Substrate with the pinned ate-setup only on drift"
        ]
        proof = self.substrate["Record the proof of the installed Substrate"]
        self.assertEqual(intent["when"], "ax_lab_substrate_drift | length > 0")
        self.assertEqual(install["when"], "ax_lab_substrate_drift | length > 0")
        self.assertEqual(proof["when"], "ax_lab_substrate_drift_before | length > 0")
        for task in (intent, proof):
            self.assertEqual(
                {
                    key: value
                    for key, value in task["ansible.builtin.copy"].items()
                    if key != "content"
                },
                {
                    "dest": "{{ ax_lab_substrate_state_path }}",
                    "owner": "root",
                    "group": "root",
                    "mode": "0600",
                },
            )
        self.assertEqual(
            install["environment"], "{{ operation_lock_guard_environment }}"
        )
        # M4: Ansible outlasts the manager, which outlasts ate-setup's waits.
        self.assertEqual(
            install["async"], "{{ ax_lab.substrate.install.timeout_seconds + 300 }}"
        )
        self.assertIs(install["changed_when"], True)
        uids = {key: f"uid-{index}" for index, key in enumerate(CREATE_ONCE)}
        self.assert_pass(
            [
                *self.derive(),
                {
                    "name": "Render the install and both state files",
                    "ansible.builtin.set_fact": {
                        "probe_argv": install["ansible.builtin.command"]["argv"],
                        "probe_intent": intent["ansible.builtin.copy"]["content"],
                        "probe_proof": proof["ansible.builtin.copy"]["content"],
                    },
                },
                probe("probe_argv == expected_argv"),
                probe("probe_intent | from_json == expected_intent"),
                probe("probe_proof | from_json == expected_proof"),
            ],
            ax_lab_substrate_expected_state=expected_identity(),
            ax_lab_substrate_uids=uids,
            ax_lab_substrate_recorded=uids,
            expected_intent={**expected_identity(), "phase": "installing", "create_once": uids},
            expected_proof={**expected_identity(), "phase": "installed", "create_once": uids},
            expected_argv=[
                "/usr/bin/python3",
                "/opt/dockerswarm/ax-lab/bin/manage-ax-lab-substrate.py",
                "install",
                "--registry-port", "5001",
                "--tag", "67253354",
                "--installer", f"ate-setup={ATE_SETUP}",
                "--source", "/opt/dockerswarm/ax-lab/src/substrate",
                "--kubeconfig", "/opt/dockerswarm/ax-lab/home/.kube/config",
                "--context", "kind-kind",
                "--atenet-router", "envoy",
                "--rollout-timeout-seconds", "600",
                "--memory-mib", "256",
                "--memory-reservation-mib", "128",
                "--cpu-millicores", "500",
                "--pids-limit", "256",
                "--timeout-seconds", "6480",
                "--min-mem-available-mib", "768",
                "--mem-available-floor-mib", "512",
                *[f"--image={name}={MANUAL_LAB_DIGESTS[name]}" for name in INSTALLED],
            ],
        )  # fmt: skip
        health = self.substrate["Wait for every Substrate workload to be ready"]
        self.assertEqual((health["retries"], health["delay"]), (60, 10))
        self.assertIs(health["changed_when"], False)
        for results, ready in (
            ({"rc": 0, "stdout_lines": [json.dumps({"not_ready": []})]}, True),
            ({"rc": 0, "stdout_lines": [json.dumps({"not_ready": ["x"]})]}, False),
            ({"rc": 1, "stdout_lines": []}, False),
        ):
            with self.subTest(read=results):
                self.assert_pass(
                    [probe(health["until"] if ready else f"not ({health['until']})")],
                    ax_lab_substrate_health=results,
                )

    def test_substrate_reads_only_what_exists_and_needs_a_readable_cluster(
        self,
    ) -> None:
        slurp = self.substrate_read["Read the Substrate state file"]
        self.assertEqual(slurp["when"], "ax_lab_substrate_state_file.stat.exists")
        self.assertEqual(
            slurp["ansible.builtin.slurp"], {"src": "{{ ax_lab_substrate_state_path }}"}
        )
        name = "Require Substrate to be readable in the running lab node"
        self.assert_task_accepts(
            SUBSTRATE,
            name,
            {**self.variables, "ax_lab_substrate_cluster": cluster_read()},
        )
        self.assert_task_rejects(
            SUBSTRATE,
            name,
            {**self.variables, "ax_lab_substrate_cluster": None},
            "could not be read",
        )
        report = self.main["Report what an apply would change in Substrate"]
        self.assertEqual(
            report,
            {
                "name": "Report what an apply would change in Substrate",
                "ansible.builtin.debug": {"msg": "{{ ax_lab_substrate_plan }}"},
                "when": "ansible_check_mode",
            },
        )
        keep = self.substrate["Keep the Substrate reads from before any install"]
        self.assertEqual(
            keep["ansible.builtin.set_fact"],
            {
                "ax_lab_substrate_before": "{{ ax_lab_substrate_cluster }}",
                "ax_lab_substrate_uids_before": "{{ ax_lab_substrate_uids }}",
                "ax_lab_substrate_drift_before": "{{ ax_lab_substrate_drift }}",
            },
        )

    def test_substrate_is_never_reinstalled_under_a_running_ax_helper(self) -> None:
        # ate-setup rolls atenet-router: a running ax-tarea would lose its
        # `ax ssh` stream, and ax.yml would then refuse to restore the
        # router's route timeout (docs/AX.md, «Router»).
        read = self.substrate[
            "Read the AX operator helpers that are running before Substrate changes"
        ]
        self.assertEqual(
            read["ansible.builtin.command"]["argv"],
            [
                "/usr/bin/systemctl",
                "list-units",
                "--type=scope",
                "--state=active",
                "--plain",
                "--no-legend",
                "--no-pager",
                "ax-tarea-*",
                "ax-cli-*",
            ],
        )
        self.assertEqual(read["when"], "ax_lab_substrate_drift | length > 0")
        self.assertIs(read["changed_when"], False)
        refuse = self.substrate[
            "Refuse to reinstall Substrate while an ax-tarea or the ax CLI runs"
        ]
        running = ["ax-tarea-4242-1790000000.scope loaded active running ax-tarea"]
        self.assert_pass(
            [refuse],
            ax_lab_substrate_drift=[],
            ax_lab_substrate_helper_scopes={"skipped": True, "changed": False},
        )
        self.assert_pass(
            [refuse],
            ax_lab_substrate_drift=["workloads"],
            ax_lab_substrate_helper_scopes={"rc": 0, "stdout_lines": []},
        )
        self.assert_fail(
            [refuse],
            "(ax-tarea-4242-1790000000.scope)",
            ax_lab_substrate_drift=["workloads"],
            ax_lab_substrate_helper_scopes={"rc": 0, "stdout_lines": running},
        )
        names = list(self.substrate)
        self.assertLess(
            names.index(refuse["name"]),
            names.index("Record the Substrate install intent"),
        )

    def test_the_installed_substrate_is_verified_before_it_is_recorded(self) -> None:
        verify = self.substrate["Verify the installed Substrate identity"]
        before = cluster_read()
        uids = {key: f"uid-{index}" for index, key in enumerate(CREATE_ONCE)}

        def after(**changes: Any) -> dict[str, Any]:
            read = cluster_read(**changes)
            return {
                "ax_lab_substrate_cluster": read,
                "ax_lab_substrate_present": [
                    key
                    for key, value in read["create_once"].items()
                    if value is not None
                ],
                "ax_lab_substrate_uids": {
                    key: value["uid"]
                    for key, value in read["create_once"].items()
                    if value is not None
                },
            }

        def variables(uids_before: dict[str, str], **changes: Any) -> dict[str, Any]:
            return {
                "ax_lab_substrate_before": before,
                "ax_lab_substrate_uids_before": uids_before,
                **after(**changes),
            }

        self.assert_pass([verify], **variables(uids))
        # A first install: nothing existed before, every object exists after.
        self.assert_pass(
            [verify],
            **{
                **variables({}),
                "ax_lab_substrate_before": cluster_read(
                    immutable=dict.fromkeys(before["immutable"])
                ),
            },
        )
        regenerated = copy.deepcopy(before["create_once"])
        regenerated[CREATE_ONCE[1]]["uid"] = "rotated"
        workloads = copy.deepcopy(before["workloads"])
        workloads[0]["images"] = ["other"]
        for label, changes in (
            ("label", {"node_label": None}),
            ("workloads", {"workloads": workloads}),
            ("create-once missing", {"create_once": {**before["create_once"], CREATE_ONCE[5]: None}}),
            ("create-once regenerated", {"create_once": regenerated}),
            ("postgres generation", {"immutable": {**before["immutable"], "ate-system/StatefulSet/postgres": "pg-uid@2"}}),
            ("bucket job recreated", {"immutable": {**before["immutable"], "ate-system/Job/rustfs-bucket-init": "new@1"}}),
            ("bucket job gone", {"immutable": {**before["immutable"], "ate-system/Job/rustfs-bucket-init": None}}),
        ):  # fmt: skip
            with self.subTest(case=label):
                self.assert_fail(
                    [verify],
                    "differs from its pinned install",
                    **variables(uids, **changes),
                )

    # -- static rules over every new task file -------------------------------

    def test_check_mode_files_only_read(self) -> None:
        allowed = {
            "ansible.builtin.assert",
            "ansible.builtin.set_fact",
            "ansible.builtin.stat",
            "ansible.builtin.slurp",
            "ansible.builtin.script",
        }
        scripts = 0
        for relative_path in (IMAGES_READ, SUBSTRATE_READ):
            for task in yaml.safe_load((ROOT / relative_path).read_text()):
                with self.subTest(task=task["name"]):
                    self.assertEqual(len(allowed.intersection(task)), 1, task)
                    if "ansible.builtin.stat" in task:
                        self.assertIs(task["ansible.builtin.stat"]["follow"], False)
                    if "ansible.builtin.script" in task:
                        scripts += 1
                        self.assertIs(task["check_mode"], False)
                        self.assertIs(task["changed_when"], False)
                        self.assertEqual(
                            task["ansible.builtin.script"]["executable"],
                            "/usr/bin/python3",
                        )
                        self.assertRegex(
                            task["ansible.builtin.script"]["cmd"],
                            r'"(image|cluster)-status"',
                        )
        self.assertEqual(scripts, 2)

    def test_every_substrate_write_is_gated_and_locked(self) -> None:
        writers = 0
        for relative_path in (ARTIFACTS, IMAGES, NODE, SUBSTRATE):
            for task in yaml.safe_load((ROOT / relative_path).read_text()):
                command = task.get("ansible.builtin.command")
                if command is None or task.get("changed_when") is not True:
                    continue
                writers += 1
                with self.subTest(task=task["name"]):
                    self.assertTrue(
                        "when" in task
                        or task.get("loop") == "{{ ax_lab_substrate_builds }}"
                    )
                    argv = command["argv"]
                    if "ax_lab_substrate_manager" in str(argv):
                        self.assertEqual(
                            task["environment"],
                            "{{ operation_lock_guard_environment }}",
                        )
                        self.assertIn("/usr/bin/python3", str(argv))
        self.assertEqual(writers, 11)

        def strings(value: Any):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield str(key)
                    yield from strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)
            elif isinstance(value, str):
                yield value

        for path in role_task_files():
            tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
            text = " ".join(strings(tasks))
            with self.subTest(path=path.name):
                # Never Docker's image store (it re-serialises manifests),
                # never Secret data, never a compiler outside the manager.
                for forbidden in (
                    '"pull"',
                    '"push"',
                    "docker pull",
                    "docker push",
                    "get secret",
                    "go build",
                    "ko build",
                    "make build",
                    "install-ate",
                ):
                    self.assertNotIn(forbidden, text)
                for task in tasks:
                    argv = (task.get("ansible.builtin.command") or {}).get("argv")
                    if isinstance(argv, list):
                        self.assertFalse({"pull", "push", "secret"} & set(argv[1:3]))

    def test_the_manager_reads_the_registry_and_cluster_the_role_needs(self) -> None:
        manager = load_manager()
        self.assertEqual(manager.IMAGE_NAMES, NAMES)
        self.assertEqual(
            [f"{ns}/{kind}/{name}" for ns, kind, name in manager.CREATE_ONCE],
            CREATE_ONCE,
        )
        self.assertEqual(
            sorted(manager.NAMESPACES),
            sorted(
                {w["namespace"] for w in document()["ax_lab"]["substrate"]["workloads"]}
            ),
        )


class ReproducibilityWorkflowTests(unittest.TestCase):
    """The CI job that proves the pins reproducible, build-only."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.text)

    def test_triggers_and_least_privilege(self) -> None:
        on = self.workflow["on"]
        paths = [
            "config/ax-lab.yml",
            "images/ax/**",
            "scripts/manage-ax-lab-substrate.py",
            "scripts/validate-ax-lab.py",
            ".github/workflows/ax-lab-reproducibility.yml",
        ]
        self.assertEqual(on["pull_request"], {"paths": paths})
        self.assertEqual(on["push"], {"branches": ["main"], "paths": paths})
        self.assertIn("workflow_dispatch", on)
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(list(self.workflow["jobs"]), ["reproduce", "reproduce-ax"])
        for job in self.workflow["jobs"].values():
            self.assertNotIn("permissions", job)

    def test_every_action_is_pinned_by_full_commit(self) -> None:
        steps = self.workflow["jobs"]["reproduce"]["steps"]
        uses = [step["uses"] for step in steps if "uses" in step]
        self.assertEqual(len(uses), 3)
        for reference in uses:
            with self.subTest(uses=reference):
                self.assertRegex(reference, r"^[a-z-]+/[a-z-]+@[a-f0-9]{40}$")
        for step in steps:
            if step.get("uses", "").startswith("actions/checkout@"):
                self.assertIs(step["with"]["persist-credentials"], False)
        substrate = next(
            step for step in steps if step.get("with", {}).get("repository")
        )
        self.assertEqual(substrate["with"]["repository"], "agent-substrate/substrate")
        self.assertEqual(substrate["with"]["ref"], "${{ steps.plan.outputs.commit }}")

    def test_it_only_builds_and_compares_on_the_runner(self) -> None:
        for forbidden in (
            "docker push",
            "docker login",
            "secrets.",
            "GITHUB_TOKEN",
            "ko publish",
            "upload-artifact",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.text)
        run = "\n".join(
            step.get("run", "") for step in self.workflow["jobs"]["reproduce"]["steps"]
        )
        self.assertIn("--publish 127.0.0.1:5000:5000", run)
        self.assertIn("scripts/manage-ax-lab-substrate.py build", run)
        self.assertIn("scripts/manage-ax-lab-substrate.py import", run)
        self.assertIn("--registry 127.0.0.1:5000", run)
        self.assertIn("validate-ax-lab.py --substrate-plan", run)
        self.assertIn('"pending"', run)
        self.assertEqual(
            self.workflow["jobs"]["reproduce"]["steps"][-1]["if"], "always()"
        )


class SubstrateRegistrationTests(unittest.TestCase):
    def test_sensitive_path_guard_covers_the_manager(self) -> None:
        guard = (ROOT / ".github/workflows/guard-sensitive-paths.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("\n          ^scripts/manage-ax-lab-substrate\\.py$\n", guard)

    def test_the_docs_seed_command_carries_every_manual_lab_pin(self) -> None:
        docs = (ROOT / "docs/AX.md").read_text(encoding="utf-8")
        text = " ".join(docs.replace("\\\n", " ").split())
        self.assertIn("manage-ax-lab-substrate.py export", text)
        self.assertIn("--source-naming ko-md5", text)
        for name, digest in MANUAL_LAB_DIGESTS.items():
            with self.subTest(image=name):
                self.assertIn(f"--image={name}={digest}", text)
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("manage-ax-lab-substrate.py", changelog)
        self.assertIn("ax-lab-reproducibility", changelog)


if __name__ == "__main__":
    unittest.main()
