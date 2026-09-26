"""Contract of AX in the AX lab playbook `ax-lab` (docs/AX.md, «AX»).

The validator's AX rules and the manifests it renders, the role's AX tasks
(run through the reviewed-task harness against synthetic reads, never
against Docker or a cluster), the two operator helpers run against fake
binaries in a temporary directory, the reproducibility job and the docs.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jinja2
import yaml

from ansible_task_harness import ANSIBLE_PLAYBOOK, SIDE_EFFECT_FREE_MODULES
from test_ax_lab_contract import (
    AX_APPLY,
    AX_HOST,
    AX_IMAGES,
    AX_IMAGES_READ,
    AX_READ,
    CONFIG,
    MAIN,
    ROLE,
    ROOT,
    WORKERS,
    container_read,
    load_script,
    load_tasks,
    probe,
    role_task_files,
    run_reviewed_tasks,
)
from test_ax_lab_substrate_contract import pinned_variables

WORKFLOW = ROOT / ".github/workflows/ax-lab-reproducibility.yml"
TEMPLATES = ROLE / "templates/ax"
AX_TAG = "f009cc8-issue375"
# Read from the manual lab's registry with `curl -I` on 2026-09-25: the ko
# manifests of ax-controller and ax-server, and the linux/amd64 manifests
# of the buildx indexes ax-task-runner@sha256:5d536baa... and
# ax-agents@sha256:f82e9848..., whose other entry is an attestation.
AX_DIGESTS = {
    "ax-controller": (
        "sha256:2a744737051c6e877213e25f49ff2408b3399e8f3176f4e203df9c515fc351d2"
    ),
    "ax-server": (
        "sha256:621ce24e8887a6fcbf4d4b31da1dc005e0c54210bc28275a912640b97c2667f1"
    ),
    "ax-task-runner": (
        "sha256:a5f9ee65df155af434ecf06cb79be4beb7d09b2621d05b38730c682925220bd4"
    ),
    "ax-agents": (
        "sha256:d136aebb3f8393e4c994ecd7e4dd4296d5eea7b38e2a32d8087776dae9bb2682"
    ),
}
# sha256sum of the manual lab's files on 2026-09-25: /opt/ax-lab/patches,
# /opt/ax-lab/images and /opt/ax-lab/bin/ax.
VENDORED = {
    "images/ax/google-ax-375.patch": (
        "7b8bff50ffc90f06970352f4576ee1dc0d3d9c2fcfb520fea86249beeeae9d72"
    ),
    "images/ax-task-runner/Dockerfile": (
        "f23b3b30bfd5093b2e7b6fa4d6e7abef82aec27e5238f33ea20793387789ecca"
    ),
    "images/ax-agents/Dockerfile": (
        "8a138a3620767fff1a7d7f911f91a7c4afcdc841f8a21f2b285165f0521cbf62"
    ),
    "images/ax-agents/ax-agent": (
        "38af834e3e36ac6a0b52fe5557c03c1ef314655439a34612a94e0063f306de4e"
    ),
    "images/ax-agents/package.json": (
        "58e12bda0702b8d182857e1fc181ebd973276a107a72cf89c0c2cffd7b9d9bc0"
    ),
    "images/ax-agents/package-lock.json": (
        "096ea7487c776216f401d3548664e91bd05356123224c9b71b577bda1f6687d9"
    ),
}
CLI_SHA256 = "acd1f36d86557a697c49956110c9f80ba260db2cefb1a6497b166fa43f52ebb7"
RUNNER_BINARY_SHA256 = (
    "0bcf4930c56d9a0b2343f8bfd493aa23bff1d4688cb2ddba6b3f0587a283faec"
)
NODE_ID = "b" * 64
STARTED = "2026-09-25T10:07:39.976123456Z"
KUBECTL_PREFIX = [
    "{{ ax_lab_bin_directory }}/kubectl",
    "--kubeconfig",
    "{{ ax_lab_kubeconfig_path }}",
    "--context",
    "kind-{{ ax_lab.cluster.name }}",
    "--request-timeout",
    "10s",
]
VALIDATOR = load_script("validate_ax_lab_ax", "scripts/validate-ax-lab.py")


def document() -> dict[str, Any]:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def sha256(path: str) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def rendered_manifests(lab: dict[str, Any] | None = None) -> dict[str, str]:
    return VALIDATOR.render_ax_manifests(lab or document()["ax_lab"])


def objects(text: str) -> list[dict[str, Any]]:
    return [item for item in yaml.safe_load_all(text) if item is not None]


def identity() -> dict[str, Any]:
    """What the AX state file records, pinned here independently."""
    return {
        "schema_version": 1,
        "tag": AX_TAG,
        "images": AX_DIGESTS,
        "manifests_sha256": VALIDATOR.ax_manifests_sha256(rendered_manifests()),
    }


def ax_state(**overrides: Any) -> dict[str, Any]:
    state = {
        **identity(),
        "node_container_id": NODE_ID,
        "phase": "installed",
        "namespaces": {"ax-system": "uid-system", "ax-workers": "uid-workers"},
    }
    state.update(overrides)
    return state


def ax_variables(**overrides: Any) -> dict[str, Any]:
    """The role's facts once main.yml derived AX's and the node runs."""
    variables = pinned_variables()
    lab = variables["ax_lab"]
    rendered = VALIDATOR.render_ax_manifests(lab)
    variables.update(
        {
            "ax_lab_ax_manifests_digest": {
                "stdout": json.dumps(
                    VALIDATOR.ax_manifests_sha256(rendered), sort_keys=True
                )
            },
            "ax_lab_ax_manifests": rendered,
            "ax_lab_ax_image_args": [
                f"--image={name}={digest}" for name, digest in AX_DIGESTS.items()
            ],
            "ax_lab_ax_identity": identity(),
            "ax_lab_node": json.loads(container_read("kind-control-plane")["stdout"]),
            "ax_lab_substrate_cluster": {"node_label": "67253354"},
            "ax_lab_substrate_drift": [],
        }
    )
    variables.update(overrides)
    return variables


def ax_reads(
    *,
    state: dict[str, Any] | None,
    namespaces: dict[str, str] | None,
    system_rc: int | None = 0,
    workers_rc: int | None = 0,
    router_args: list[str] | None = None,
    router_names: tuple[str, ...] = ("atenet-router", "envoy"),
    pods: list[str] | None = None,
    scopes: list[str] | None = None,
    started: str = STARTED,
) -> dict[str, Any]:
    """What ax_read.yml registers before each of its facts."""
    skipped = {"skipped": True, "changed": False}
    reads: dict[str, Any] = {
        "ax_lab_ax_state_file": {
            "stat": (
                {"exists": False}
                if state is None
                else {
                    "exists": True,
                    "isreg": True,
                    "islnk": False,
                    "uid": 0,
                    "gid": 0,
                    "mode": "0600",
                }
            )
        },
        "ax_lab_ax_namespaces_read": (
            skipped
            if namespaces is None
            else {
                "rc": 0,
                "stdout_lines": [
                    f"{key}   {value}" for key, value in namespaces.items()
                ],
            }
        ),
        "ax_lab_ax_system_diff": skipped if system_rc is None else {"rc": system_rc},
        "ax_lab_ax_workers_diff": skipped if workers_rc is None else {"rc": workers_rc},
        "ax_lab_ax_router_read": {
            "rc": 0,
            "stdout_lines": [
                " ".join(router_names),
                json.dumps(
                    ["router", "--drain-delay=13s", "--route-timeout=1h"]
                    if router_args is None
                    else router_args
                ),
            ],
        },
        "ax_lab_ax_node_started": {"rc": 0, "stdout": json.dumps(started)},
        "ax_lab_ax_worker_pods": {
            "rc": 0,
            "stdout_lines": (
                ["ax-workers   ax-7cf59bfc84-new   2026-09-25T15:11:02Z"]
                if pods is None
                else pods
            ),
        },
        "ax_lab_ax_helper_scopes": {"rc": 0, "stdout_lines": scopes or []},
    }
    if state is not None:
        reads["ax_lab_ax_state_content"] = {
            "content": base64.b64encode(json.dumps(state).encode()).decode()
        }
    return reads


def run_tasks(
    tasks: list[dict[str, Any]], variables: dict[str, Any], *, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """run_reviewed_tasks, optionally under --check like the role's reads."""
    if not check:
        return run_reviewed_tasks(tasks, variables)
    for task in tasks:
        if len(SIDE_EFFECT_FREE_MODULES.intersection(task)) != 1:
            raise AssertionError(f"{task.get('name')!r} is not side-effect free")
    with tempfile.TemporaryDirectory() as temporary:
        playbook = Path(temporary) / "tasks.yml"
        playbook.write_text(
            yaml.safe_dump(
                [
                    {
                        "name": "Exercise reviewed tasks in check mode",
                        "hosts": "localhost",
                        "connection": "local",
                        "gather_facts": False,
                        "become": False,
                        "vars": variables,
                        "tasks": tasks,
                    }
                ],
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return subprocess.run(
            [str(ANSIBLE_PLAYBOOK), "-i", "localhost,", "--check", str(playbook)],
            cwd=ROOT / "ansible",
            text=True,
            capture_output=True,
            check=False,
        )


def strip_when(task: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in task.items() if key != "when"}


# --------------------------------------------------------------------------
# Validator


class AxValidatorTests(unittest.TestCase):
    """scripts/validate-ax-lab.py pins AX and the manifests it renders."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = VALIDATOR.load_yaml(CONFIG)
        cls.reserved = VALIDATOR.reserved_sysctl_keys()

    def rejected(self, change, message: str) -> None:
        mutated = copy.deepcopy(self.document)
        change(mutated)
        with self.assertRaisesRegex(VALIDATOR.AxLabError, message):
            VALIDATOR.validate_catalog(mutated, self.reserved)

    def accepted(self, change) -> None:
        mutated = copy.deepcopy(self.document)
        change(mutated)
        VALIDATOR.validate_catalog(mutated, self.reserved)

    @staticmethod
    def set_ax(path: str, value: Any):
        def change(document: dict[str, Any]) -> None:
            *parents, leaf = path.split("/")
            target = document["ax_lab"]["ax"]
            for parent in parents:
                target = target[parent]
            if value is KeyError:
                del target[leaf]
            else:
                target[leaf] = value

        return change

    def test_ax_pins_are_the_manual_lab_platform_manifests(self) -> None:
        lab = self.document["ax_lab"]
        ax = lab["ax"]
        self.assertEqual(ax["tag"], AX_TAG)
        self.assertEqual(ax["images"], AX_DIGESTS)
        self.assertEqual(ax["cli_sha256"], CLI_SHA256)
        self.assertEqual(ax["task_runner_binary_sha256"], RUNNER_BINARY_SHA256)
        self.assertEqual(
            ax["ko_base_image"],
            "cgr.dev/chainguard/static@sha256:"
            "41e17ed83c594a64a9396b6ab96dd26d5ddc290dacf4c177464712ff21ad534f",
        )
        self.assertEqual(
            ax["cli_backup"], "/var/backups/dockerswarm/ax-lab/binaries/ax"
        )
        self.assertEqual(
            lab["sources"]["ax"]["patch"],
            {
                "path": "images/ax/google-ax-375.patch",
                "sha256": VENDORED["images/ax/google-ax-375.patch"],
            },
        )
        # The two vendored ko manifests are the pins, byte for byte: single
        # linux/amd64 OCI manifests on chainguard/static, three layers each.
        for name in ("ax-controller", "ax-server"):
            with self.subTest(image=name):
                path = f"images/ax/manifests/{name}.json"
                self.assertEqual("sha256:" + sha256(path), AX_DIGESTS[name])
                manifest = json.loads((ROOT / path).read_text(encoding="utf-8"))
                self.assertEqual(
                    manifest["mediaType"], "application/vnd.oci.image.manifest.v1+json"
                )
                self.assertEqual(len(manifest["layers"]), 3)
                self.assertEqual(
                    manifest["annotations"]["org.opencontainers.image.base.name"],
                    "cgr.dev/chainguard/static:latest",
                )
        # No pin of the dropped OpenAI proxy is left.
        self.assertNotIn("openai_proxy", lab["images"])

    def test_vendored_build_inputs_are_the_manual_labs(self) -> None:
        for path, digest in VENDORED.items():
            with self.subTest(path=path):
                self.assertEqual(sha256(path), digest)
        package = json.loads((ROOT / "images/ax-agents/package.json").read_text())
        self.assertEqual(
            package["dependencies"],
            {"@anthropic-ai/claude-code": "2.1.274", "@openai/codex": "0.156.1"},
        )
        lock = json.loads((ROOT / "images/ax-agents/package-lock.json").read_text())
        self.assertEqual(lock["lockfileVersion"], 3)
        packages = {key: value for key, value in lock["packages"].items() if key}
        self.assertEqual(len(packages), 16)
        for key, entry in packages.items():
            with self.subTest(package=key):
                self.assertRegex(entry["integrity"], r"^sha512-")
                self.assertTrue(
                    entry["resolved"].startswith("https://registry.npmjs.org/")
                )
        # Every base image by digest; the agents image builds on the runner.
        for path in ("images/ax-task-runner/Dockerfile", "images/ax-agents/Dockerfile"):
            for line in (ROOT / path).read_text().splitlines():
                if line.startswith("FROM "):
                    with self.subTest(path=path, line=line):
                        self.assertTrue(
                            "@sha256:" in line or line == "FROM ${RUNNER_IMAGE}"
                        )
        self.assertTrue(os.access(ROOT / "images/ax-agents/ax-agent", os.X_OK))

    def test_ax_contract_is_fail_closed(self) -> None:
        digest = "sha256:" + "a" * 64
        for label, change, message in (
            ("extra key", self.set_ax("extra", 1), "ax: unexpected"),
            ("missing key", self.set_ax("router_route_timeout", KeyError), "ax: unexpected"),
            ("latest tag", self.set_ax("tag", "f009cc8-latest"), "ax tag"),
            ("other commit", self.set_ax("tag", "0000000-issue375"), "ax tag"),
            ("upper case tag", self.set_ax("tag", "F009cc8-issue375"), "ax tag"),
            ("numeric tag", self.set_ax("tag", 1), "ax tag"),
            ("fifth image", self.set_ax("images/ax-other", digest), "ax images: unexpected"),
            ("image gone", self.set_ax("images/ax-agents", KeyError), "ax images: unexpected"),
            ("tagged image", self.set_ax("images/ax-agents", "latest"), "must be pinned"),
            ("short digest", self.set_ax("images/ax-task-runner", digest[:-1]), "must be pinned"),
            ("ko pin moved", self.set_ax("images/ax-server", digest), "does not hash to the ax-server pin"),
            ("runner binary", self.set_ax("task_runner_binary_sha256", "A" * 64), "64 lowercase hex"),
            ("cli digest", self.set_ax("cli_sha256", "sha256:" + CLI_SHA256), "64 lowercase hex"),
            ("ko base by tag", self.set_ax("ko_base_image", "cgr.dev/chainguard/static:latest"), "chainguard/static by digest"),
            ("cli backup", self.set_ax("cli_backup", "/root/ax"), "cli_backup must be"),
            ("bucket", self.set_ax("snapshots_bucket", "gs://dberkov-gke-dev3/ate-env/"), "snapshots_bucket"),
            ("redis small", self.set_ax("redis/volume_mib", 100), "volume_mib"),
            ("redis string", self.set_ax("redis/volume_mib", "1024"), "volume_mib"),
            ("redis saves often", self.set_ax("redis/save_seconds", 5), "save_seconds"),
            ("redis aof key", self.set_ax("redis/appendonly", "yes"), "ax redis: unexpected"),
            ("pool namespace", self.set_ax("worker_pool/namespace", "default"), "namespace must be"),
            ("pool name", self.set_ax("worker_pool/name", "AX"), "DNS label"),
            ("no replica", self.set_ax("worker_pool/replicas", 0), "replicas must be"),
            ("request over limit", self.set_ax("worker_pool/cpu_request_millicores", 1600), "more CPU than its limit"),
            ("cpu of the node", self.set_ax("worker_pool/cpu_limit_millicores", 2000), "must leave 500m"),
            ("two big workers", self.set_ax("worker_pool/replicas", 2), "exceeds the node's limit"),
            ("small reserve", self.set_ax("node_platform_reserve_mib", 1024), "at least 1792"),
            ("seconds", self.set_ax("router_route_timeout", "3600s"), "router_route_timeout"),
            ("three hours", self.set_ax("router_route_timeout", "3h"), "router_route_timeout"),
            ("zero", self.set_ax("router_route_timeout", "0h"), "router_route_timeout"),
            ("integer", self.set_ax("router_route_timeout", 60), "router_route_timeout"),
        ):  # fmt: skip
            with self.subTest(case=label):
                self.rejected(change, message)

    def test_the_patch_is_the_vendored_file_by_its_hash(self) -> None:
        def patch(key: str, value: str):
            def change(document: dict[str, Any]) -> None:
                document["ax_lab"]["sources"]["ax"]["patch"][key] = value

            return change

        self.rejected(patch("path", "images/ax/other.patch"), "the vendored")
        self.rejected(patch("sha256", "0" * 64), "does not hash to its reviewed")
        self.rejected(patch("sha256", "Z" * 64), "64 lowercase hex")
        self.rejected(
            lambda document: document["ax_lab"]["sources"]["ax"].pop("patch"),
            "ax: unexpected",
        )
        self.rejected(
            lambda document: document["ax_lab"]["sources"]["substrate"].update(
                patch={}
            ),
            "substrate: unexpected",
        )

    def test_workers_leave_the_node_its_cpu_and_memory(self) -> None:
        def pool(**values: int):
            def change(document: dict[str, Any]) -> None:
                document["ax_lab"]["ax"]["worker_pool"].update(values)

            return change

        # 1 x 1536 today; 2 x 896 also fits the 3 584 - 1 792 MiB. The
        # workers' CPU limits together may take at most the node's 2000m
        # minus 500m, so two workers get 750m each.
        for values in (
            {"replicas": 1, "memory_mib": 1536},
            {"replicas": 2, "memory_mib": 896, "cpu_limit_millicores": 750},
            {"cpu_limit_millicores": 1500},
            {"cpu_request_millicores": 1500, "cpu_limit_millicores": 1500},
        ):
            with self.subTest(accepted=values):
                self.accepted(pool(**values))
        for values, message in (
            ({"replicas": 2, "memory_mib": 1536}, "exceeds the node's limit"),
            ({"replicas": 2, "memory_mib": 1024}, "exceeds the node's limit"),
            ({"replicas": 1, "memory_mib": 1793}, "exceeds the node's limit"),
            ({"cpu_limit_millicores": 1501}, "at most 1500m"),
            # 3 000m of worker limits on a node capped at 2 000m.
            ({"replicas": 2, "memory_mib": 896}, "at most 1500m"),
            (
                {"replicas": 2, "memory_mib": 896, "cpu_limit_millicores": 751},
                "at most 1500m",
            ),
        ):
            with self.subTest(rejected=values):
                self.rejected(pool(**values), message)

    def test_rendered_manifests_are_exactly_the_reviewed_objects(self) -> None:
        rendered = rendered_manifests()
        self.assertEqual(list(rendered), ["ax-system.yaml", "ax-workers.yaml"])
        for name, text in rendered.items():
            with self.subTest(manifest=name):
                self.assertEqual(
                    [
                        (
                            item["kind"],
                            item["metadata"].get("namespace"),
                            item["metadata"]["name"],
                        )
                        for item in objects(text)
                    ],
                    VALIDATOR.AX_INVENTORY[name],
                )
                self.assertNotIn("ko://", text)
                self.assertNotIn("kind: Secret", text)
                self.assertNotIn("kind: ClusterRole", text)
                self.assertNotIn("kind: Role", text)
        system = {
            item["metadata"]["name"] + "/" + item["kind"]: item
            for item in objects(rendered["ax-system.yaml"])
        }
        pods = [
            system[f"{name}/Deployment"]["spec"]["template"]["spec"]
            for name in ("ax-redis", "ax-server", "ax-controller")
        ]
        self.assertEqual(
            [[c["image"] for c in pod["containers"]] for pod in pods],
            [
                [document()["ax_lab"]["images"]["redis"]],
                [f"localhost:5001/ax-server:{AX_TAG}@{AX_DIGESTS['ax-server']}"],
                [
                    f"localhost:5001/ax-controller:{AX_TAG}@{AX_DIGESTS['ax-controller']}"
                ],
            ],
        )
        for pod in pods:
            self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertIs(
            system["ax-controller/ServiceAccount"]["automountServiceAccountToken"],
            False,
        )

    def test_manifests_keep_upstream_but_for_the_documented_deviations(self) -> None:
        """Upstream deploy/*.yaml at sources.ax, field by field."""
        system = {
            item["metadata"]["name"] + "/" + item["kind"]: item
            for item in objects(rendered_manifests()["ax-system.yaml"])
        }
        server = system["ax-server/Deployment"]["spec"]["template"]["spec"]
        controller = system["ax-controller/Deployment"]["spec"]["template"]["spec"]
        redis = system["ax-redis/Deployment"]["spec"]["template"]["spec"]
        # deploy/ax-server.yaml, lines 36-63.
        self.assertEqual(
            server["containers"][0]["args"],
            ["--addr=:8080", "--redis-addr=ax-redis.ax-system.svc.cluster.local:6379"],
        )
        self.assertEqual(
            server["containers"][0]["readinessProbe"],
            {
                "httpGet": {"path": "/healthz", "port": 8080},
                "initialDelaySeconds": 2,
                "periodSeconds": 5,
            },
        )
        # deploy/ax-controller.yaml, lines 71-99.
        self.assertEqual(
            controller["containers"][0]["args"],
            [
                "--redis-addr=ax-redis.ax-system.svc.cluster.local:6379",
                "--substrate-endpoint=api.ate-system.svc.cluster.local:443",
                "--substrate-authority=api.ate-system.svc",
                "--substrate-token-file=/var/run/secrets/ateapi/token",
                "--substrate-ca-file=/run/servicedns-ca/trust-bundle.pem",
                "--template=default-template",
                "--template-atespace=ax-system",
            ],
        )
        self.assertEqual(
            controller["containers"][0]["env"],
            [
                {
                    "name": "ATENET_ROUTER_ADDR",
                    "value": "atenet-router.ate-system.svc.cluster.local:80",
                },
                # Deviation: the manual lab set it with `kubectl set env`.
                {"name": "AX_SNAPSHOTS_BUCKET", "value": "gs://ate-snapshots/ax/"},
            ],
        )
        self.assertEqual(
            controller["containers"][0]["securityContext"],
            {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False},
        )
        self.assertEqual(
            [volume["name"] for volume in controller["volumes"]],
            ["ate-token", "servicedns-ca"],
        )
        # Limits as upstream, in canonical quantities (1000m is "1").
        self.assertEqual(
            [
                pod["containers"][0]["resources"]
                for pod in (redis, server, controller)
            ],
            [
                {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
                {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
                {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
            ],
        )  # fmt: skip
        # Deviation: RDB only, on its volume, one pod at a time.
        self.assertEqual(
            redis["containers"][0]["args"], ["--save", "60 1", "--appendonly", "no"]
        )
        self.assertEqual(
            system["ax-redis/Deployment"]["spec"]["strategy"], {"type": "Recreate"}
        )
        self.assertEqual(
            system["ax-redis-data/PersistentVolumeClaim"]["spec"],
            {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": "standard",
                "resources": {"requests": {"storage": "1024Mi"}},
            },
        )
        # Deviation: the type, upstream's default, written out so that the
        # ax-lab field manager owns it and the server-side diff sees another
        # hand change it.
        for name in ("ax-redis", "ax-server"):
            self.assertEqual(system[f"{name}/Service"]["spec"]["type"], "ClusterIP")

    def test_rendered_manifests_never_publish_or_empower_ax(self) -> None:
        lab = document()["ax_lab"]
        rendered = rendered_manifests(lab)

        def mutate(name: str, change) -> dict[str, str]:
            items = objects(rendered[name])
            change(items)
            return {
                **rendered,
                name: yaml.safe_dump_all(items, sort_keys=False),
            }

        def by_name(items, kind: str, name: str) -> dict[str, Any]:
            return next(
                item
                for item in items
                if item["kind"] == kind and item["metadata"]["name"] == name
            )

        def pod(items, name: str) -> dict[str, Any]:
            return by_name(items, "Deployment", name)["spec"]["template"]["spec"]

        system = "ax-system.yaml"
        cases = (
            ("node port", lambda items: by_name(items, "Service", "ax-server")["spec"].update(type="NodePort"), "ClusterIP only"),
            ("load balancer", lambda items: by_name(items, "Service", "ax-server")["spec"].update(type="LoadBalancer"), "ClusterIP only"),
            ("external IP", lambda items: by_name(items, "Service", "ax-redis")["spec"].update(externalIPs=["1.2.3.4"]), "ClusterIP only"),
            ("node port number", lambda items: by_name(items, "Service", "ax-redis")["spec"]["ports"][0].update(nodePort=30000), "node port"),
            ("host port", lambda items: pod(items, "ax-server")["containers"][0]["ports"][0].update(hostPort=8080), "host port"),
            ("host network", lambda items: pod(items, "ax-server").update(hostNetwork=True), "hostNetwork"),
            ("host PID", lambda items: pod(items, "ax-redis").update(hostPID=True), "hostPID"),
            ("host path", lambda items: pod(items, "ax-redis")["volumes"].append({"name": "h", "hostPath": {"path": "/"}}), "hostPath"),
            ("API token", lambda items: pod(items, "ax-controller").pop("automountServiceAccountToken"), "Kubernetes API token"),
            ("API token on", lambda items: pod(items, "ax-server").update(automountServiceAccountToken=True), "Kubernetes API token"),
            ("privileged", lambda items: pod(items, "ax-server")["containers"][0].update(securityContext={"privileged": True}), "privileged"),
            ("other image", lambda items: pod(items, "ax-server")["containers"][0].update(image="localhost:5001/ax-server:" + AX_TAG), "pinned images"),
            ("sidecar", lambda items: pod(items, "ax-server")["containers"].append({"name": "x", "image": "busybox"}), "pinned images"),
            ("init container", lambda items: pod(items, "ax-redis").update(initContainers=[{"name": "x", "image": lab["images"]["redis"]}]), "pinned images"),
            ("a Secret", lambda items: items.append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "x", "namespace": "ax-system"}}), "other objects"),
            ("a ClusterRole", lambda items: items.append({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "ax-controller"}}), "other objects"),
            ("an Ingress", lambda items: items.append({"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": {"name": "x", "namespace": "ax-system"}}), "other objects"),
            ("another namespace", lambda items: by_name(items, "Service", "ax-server")["metadata"].update(namespace="default"), "other objects"),
            ("no bucket", lambda items: pod(items, "ax-controller")["containers"][0]["env"].pop(), "snapshots bucket"),
            ("default account", lambda items: pod(items, "ax-controller").pop("serviceAccountName"), "own service account"),
            ("API audience", lambda items: pod(items, "ax-controller")["volumes"][0]["projected"]["sources"][0]["serviceAccountToken"].update(audience="https://kubernetes.default.svc"), "service account token"),
            ("second controller token", lambda items: pod(items, "ax-controller")["volumes"][1]["projected"]["sources"].append({"serviceAccountToken": {"path": "api"}}), "service account token"),
            ("projected API token", lambda items: pod(items, "ax-server").update(volumes=[{"name": "t", "projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]}}]), "service account token"),
            ("token at the API path", lambda items: pod(items, "ax-server")["containers"][0].update(volumeMounts=[{"name": "d", "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount"}]), "must not mount anything at"),
            ("token at /run", lambda items: pod(items, "ax-redis")["containers"][0]["volumeMounts"].append({"name": "data", "mountPath": "/run/secrets/kubernetes.io/serviceaccount/"}), "must not mount anything at"),
            ("Secret volume", lambda items: pod(items, "ax-redis")["volumes"].append({"name": "s", "secret": {"secretName": "x"}}), "must not mount a Secret"),
            ("projected Secret", lambda items: pod(items, "ax-controller")["volumes"][1]["projected"]["sources"].append({"secret": {"name": "x"}}), "must not mount a Secret"),
            ("implicit type", lambda items: by_name(items, "Service", "ax-server")["spec"].pop("type"), "ClusterIP only, with its type set"),
            ("AOF", lambda items: pod(items, "ax-redis")["containers"][0].update(args=["--appendonly", "yes"]), "RDB snapshots only"),
            ("rolling redis", lambda items: by_name(items, "Deployment", "ax-redis")["spec"].pop("strategy"), "never rolled"),
            ("small volume", lambda items: by_name(items, "PersistentVolumeClaim", "ax-redis-data")["spec"]["resources"]["requests"].update(storage="1Gi"), "volume_mib"),
            ("open redis", lambda items: by_name(items, "NetworkPolicy", "ax-redis")["spec"].pop("ingress"), "NetworkPolicy ax-redis"),
            ("open server", lambda items: by_name(items, "NetworkPolicy", "ax-server")["spec"].update(ingress=[{}]), "NetworkPolicy ax-server"),
            ("unlabelled namespace", lambda items: items[0]["metadata"].pop("labels"), "must carry only"),
        )  # fmt: skip
        VALIDATOR.validate_ax_manifests(rendered, lab)
        for label, change, message in cases:
            with self.subTest(case=label):
                with self.assertRaisesRegex(VALIDATOR.AxLabError, message):
                    VALIDATOR.validate_ax_manifests(mutate(system, change), lab)
        workers = "ax-workers.yaml"
        for label, change in (
            ("replicas", lambda items: items[1]["spec"].update(replicas=2)),
            ("tagged worker image", lambda items: items[1]["spec"].update(workerImage="localhost:5001/ateom-gvisor:67253354")),
            ("uncapped CPU", lambda items: items[1]["spec"]["template"]["resources"]["limits"].update(cpu="2")),
            ("other node label", lambda items: items[1]["spec"]["template"]["nodeSelector"].update({"ate.dev/substrate-version": "x"})),
        ):  # fmt: skip
            with self.subTest(case=label):
                with self.assertRaisesRegex(VALIDATOR.AxLabError, "WorkerPool differs"):
                    VALIDATOR.validate_ax_manifests(mutate(workers, change), lab)
        with self.assertRaisesRegex(VALIDATOR.AxLabError, "ko://"):
            VALIDATOR.validate_ax_manifests(
                {
                    **rendered,
                    system: rendered[system] + "# ko://github.com/google/ax\n",
                },
                lab,
            )

    def test_validator_prints_the_ax_plan_and_the_manifest_digests(self) -> None:
        for flag in ("--ax-plan", "--ax-manifests-sha256"):
            completed = subprocess.run(
                [str(ROOT / ".venv/bin/python"), "scripts/validate-ax-lab.py", flag],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            with self.subTest(flag=flag):
                self.assertEqual(completed.returncode, 0, completed.stderr)
                printed = json.loads(completed.stdout)
                if flag == "--ax-manifests-sha256":
                    self.assertEqual(
                        printed,
                        {
                            name: hashlib.sha256(text.encode()).hexdigest()
                            for name, text in rendered_manifests().items()
                        },
                    )
                else:
                    self.assertEqual(
                        printed,
                        {
                            "commit": "f009cc81c9a571073bc1dd58cd2ed934bf2d5b1c",
                            "repository": "https://github.com/google/ax",
                            "patch": document()["ax_lab"]["sources"]["ax"]["patch"],
                            "substrate_commit": (
                                "672533541dbfcd29084e4de2475267088bda3651"
                            ),
                            "toolbox_image": document()["ax_lab"]["images"]["toolbox"],
                            "ko_base_image": document()["ax_lab"]["ax"][
                                "ko_base_image"
                            ],
                            "ko_images": {
                                name: AX_DIGESTS[name]
                                for name in ("ax-controller", "ax-server")
                            },
                            "ko_manifests": {
                                "ax-controller": "images/ax/manifests/ax-controller.json",
                                "ax-server": "images/ax/manifests/ax-server.json",
                            },
                            "cli_sha256": CLI_SHA256,
                            "task_runner_binary_sha256": RUNNER_BINARY_SHA256,
                        },
                    )


# --------------------------------------------------------------------------
# Role


class AxRoleTests(unittest.TestCase):
    """The role's AX tasks, exercised on synthetic reads."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.main = load_tasks(MAIN)
        cls.host = load_tasks(AX_HOST)
        cls.images = load_tasks(AX_IMAGES)
        cls.images_read = load_tasks(AX_IMAGES_READ)
        cls.read = load_tasks(AX_READ)
        cls.apply = load_tasks(AX_APPLY)
        cls.workers = load_tasks(WORKERS)
        cls.variables = ax_variables()

    def completed(self, tasks, check: bool = False, **extra: Any):
        return run_tasks(tasks, {**self.variables, **extra}, check=check)

    def assert_pass(self, tasks, check: bool = False, **extra: Any) -> None:
        completed = self.completed(tasks, check, **extra)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def assert_fail(self, tasks, message: str, check: bool = False, **extra) -> None:
        completed = self.completed(tasks, check, **extra)
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0, output)
        self.assertIn(message, " ".join(output.split()))

    def holds(self, condition: str, **extra: Any) -> bool:
        """Evaluate one production condition exactly as Ansible would."""
        completed = self.completed([probe(condition)], **extra)
        output = completed.stdout + completed.stderr
        self.assertTrue(
            completed.returncode == 0
            or "Assertion failed" in output
            or "evaluated_to" in output,
            output,
        )
        return completed.returncode == 0

    def facts(self, tasks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [task for task in tasks.values() if "ansible.builtin.set_fact" in task]

    def read_decisions(self) -> list[dict[str, Any]]:
        return self.facts(self.read)

    def read_refusals(self) -> list[dict[str, Any]]:
        return [task for name, task in self.read.items() if name.startswith("Refuse")]

    def read_gates(self) -> list[dict[str, Any]]:
        """Every fact and gate of ax_read.yml, in its own order."""
        return [
            task
            for task in self.read.values()
            if SIDE_EFFECT_FREE_MODULES.intersection(task)
        ]

    # -- main.yml ----------------------------------------------------------

    def test_ansible_applies_exactly_the_manifests_the_validator_checked(self) -> None:
        digest = self.main["Compute the digests of the reviewed AX manifests locally"]
        self.assertEqual(
            digest["ansible.builtin.command"]["argv"],
            [
                "{{ ansible_playbook_python }}",
                "{{ role_path }}/../../../scripts/validate-ax-lab.py",
                "--ax-manifests-sha256",
            ],
        )
        for key, value in (
            ("delegate_to", "localhost"),
            ("become", False),
            ("changed_when", False),
            ("check_mode", False),
        ):
            self.assertEqual(digest[key], value)
        render = self.main["Render the AX manifests the role applies"]
        require = self.main["Require the AX manifests the validator checked"]
        variables = {
            key: value
            for key, value in self.variables.items()
            if key != "ax_lab_ax_manifests"
        }
        completed = run_tasks(
            [render, require, probe("ax_lab_ax_manifests == expected")],
            {**variables, "expected": rendered_manifests(self.variables["ax_lab"])},
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        completed = run_tasks(
            [render, require],
            {
                **variables,
                "ax_lab_ax_manifests_digest": {
                    "stdout": json.dumps(
                        {"ax-system.yaml": "0" * 64, "ax-workers.yaml": "0" * 64}
                    )
                },
            },
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("differ from those the validator", completed.stdout)
        names = list(self.main)
        self.assertLess(
            names.index(require["name"]),
            names.index("Reconcile the lab host prerequisites"),
        )

    def test_ax_arguments_and_identity_derive_from_the_contract(self) -> None:
        derive = self.main["Derive the pinned AX image arguments and install identity"]
        variables = {
            key: value
            for key, value in self.variables.items()
            if key not in ("ax_lab_ax_image_args", "ax_lab_ax_identity")
        }
        completed = run_tasks(
            [
                derive,
                probe("ax_lab_ax_image_args == expected_args"),
                probe("ax_lab_ax_identity == expected_identity"),
            ],
            {
                **variables,
                "expected_args": [
                    f"--image={name}={digest}" for name, digest in AX_DIGESTS.items()
                ],
                "expected_identity": identity(),
            },
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_the_host_part_runs_in_both_modes_after_host_yml(self) -> None:
        names = list(self.main)
        task = self.main["Install the AX CLI and the operator helpers"]
        self.assertEqual(
            task,
            {
                "name": "Install the AX CLI and the operator helpers",
                "ansible.builtin.import_tasks": "ax_host.yml",
            },
        )
        self.assertEqual(
            names.index(task["name"]),
            names.index("Reconcile the lab host prerequisites") + 1,
        )

    def test_check_mode_reports_the_ax_plan(self) -> None:
        describe = self.main["Describe what an apply would change in AX"]
        report = self.main["Report what an apply would change in AX"]
        self.assertEqual(describe["when"], "ansible_check_mode")
        self.assertEqual(
            report,
            {
                "name": "Report what an apply would change in AX",
                "ansible.builtin.debug": {"msg": "{{ ax_lab_ax_plan }}"},
                "when": "ansible_check_mode",
            },
        )
        names = list(self.main)
        self.assertLess(
            names.index("Report what an apply would change in Substrate"),
            names.index(describe["name"]),
        )
        converged_images = {
            "ax_lab_ax_image_status_raw": {
                "rc": 0,
                "stdout_lines": [
                    json.dumps(
                        {
                            "registry": dict.fromkeys(AX_DIGESTS, "pinned"),
                            "backup": dict.fromkeys(AX_DIGESTS, "complete"),
                        }
                    )
                ],
            }
        }
        window_images = {
            "ax_lab_ax_image_status_raw": {
                "rc": 0,
                "stdout_lines": [
                    json.dumps(
                        {
                            "registry": None,
                            "backup": dict.fromkeys(AX_DIGESTS, "complete"),
                        }
                    )
                ],
            }
        }
        stale = "ax-workers   ax-old   2026-09-25T10:07:38Z"
        for label, images, reads, extra, expected in (
            (
                "first apply of the window",
                window_images,
                ax_reads(state=None, namespaces=None),
                {"ax_lab_node": None},
                [
                    "the local registry is read once the apply starts it",
                    "AX is read once the lab node runs and Substrate matches"
                    " its pinned install",
                ],
            ),
            (
                "Substrate drifts first",
                converged_images,
                ax_reads(state=None, namespaces=None),
                {"ax_lab_substrate_drift": ["state"]},
                [
                    "AX is read once the lab node runs and Substrate matches"
                    " its pinned install",
                ],
            ),
            # substrate.yml refuses to reinstall under a running helper.
            (
                "Substrate drifts under a running ax-tarea",
                converged_images,
                ax_reads(state=None, namespaces=None, scopes=["ax-tarea-1-2.scope loaded active running ax"]),
                {"ax_lab_substrate_drift": ["workloads"]},
                [
                    "AX is read once the lab node runs and Substrate matches"
                    " its pinned install",
                    "the apply stops: an ax-tarea or the ax CLI is running",
                ],
            ),
            (
                "converged, a task runs",
                converged_images,
                ax_reads(state=ax_state(), namespaces={"ax-system": "uid-system", "ax-workers": "uid-workers"}, scopes=["ax-tarea-1-2.scope loaded active running ax"]),
                {},
                ["AX matches its pinned install"],
            ),
            (
                "converged",
                converged_images,
                ax_reads(state=ax_state(), namespaces={"ax-system": "uid-system", "ax-workers": "uid-workers"}),
                {},
                ["AX matches its pinned install"],
            ),
            (
                "after a node restart and an ate-setup reinstall",
                converged_images,
                ax_reads(
                    state=ax_state(),
                    namespaces={"ax-system": "uid-system", "ax-workers": "uid-workers"},
                    router_args=["router", "--drain-delay=13s"],
                    pods=[stale],
                    scopes=["ax-tarea-1-2.scope loaded active running ax"],
                ),
                {},
                [
                    "AX matches its pinned install",
                    "set --route-timeout=1h on ate-system/atenet-router",
                    "recreate 1 worker pods created before the lab node started",
                    "the apply stops: an ax-tarea or the ax CLI is running",
                ],
            ),
            (
                "first install on a converged Substrate",
                {
                    "ax_lab_ax_image_status_raw": {
                        "rc": 0,
                        "stdout_lines": [
                            json.dumps(
                                {
                                    "registry": {**dict.fromkeys(AX_DIGESTS, "missing"), "ax-server": "moved"},
                                    "backup": dict.fromkeys(AX_DIGESTS, "complete"),
                                }
                            )
                        ],
                    }
                },
                ax_reads(state=None, namespaces={}, system_rc=None, workers_rc=None),
                {},
                [
                    *[f"restore into the registry: {name}" for name in sorted(AX_DIGESTS)],
                    "apply AX server-side: state, ax-system.yaml, ax-workers.yaml",
                ],
            ),
        ):  # fmt: skip
            with self.subTest(case=label):
                self.assert_pass(
                    [
                        *self.facts(self.images_read),
                        *self.read_decisions(),
                        strip_when(describe),
                        probe("ax_lab_ax_plan | sort == expected | sort"),
                    ],
                    check=True,
                    expected=expected,
                    **images,
                    **reads,
                    **extra,
                )

    # -- ax_host.yml -------------------------------------------------------

    def test_the_cli_comes_only_from_its_seeded_backup(self) -> None:
        name = "Require the seeded AX CLI backup"
        good = {
            "exists": True,
            "isreg": True,
            "islnk": False,
            "uid": 0,
            "gid": 0,
            "mode": "0600",
            "checksum": CLI_SHA256,
        }
        self.assert_pass([self.host[name]], ax_lab_ax_cli_backup={"stat": good})
        for change in (
            {"exists": False},
            {"islnk": True},
            {"isreg": False},
            {"uid": 1001},
            {"gid": 1001},
            {"mode": "0644"},
            {"checksum": "0" * 64},
        ):
            with self.subTest(change=change):
                self.assert_fail(
                    [self.host[name]],
                    "is not the seeded root:root 0600 ax CLI",
                    ax_lab_ax_cli_backup={"stat": {**good, **change}},
                )
        backup = self.host["Inspect the AX CLI backup without following links"]
        self.assertEqual(
            backup["ansible.builtin.stat"],
            {
                "path": "{{ ax_lab.ax.cli_backup }}",
                "follow": False,
                "checksum_algorithm": "sha256",
            },
        )
        install = self.host["Install the AX CLI from its backup only when it differs"]
        self.assertEqual(
            install["ansible.builtin.copy"],
            {
                "src": "{{ ax_lab.ax.cli_backup }}",
                "remote_src": True,
                "dest": "{{ ax_lab_ax_cli }}",
                "owner": "root",
                "group": "root",
                "mode": "0755",
            },
        )
        self.assertEqual(
            install["when"], "ax_lab_bin_directory_state.stat.isdir | default(false)"
        )
        for directory, expected in ((True, True), (False, False)):
            with self.subTest(bin_directory=directory):
                self.assertIs(
                    self.holds(
                        install["when"],
                        ax_lab_bin_directory_state={"stat": {"isdir": directory}},
                    ),
                    expected,
                )
        self.assertFalse(
            self.holds(install["when"], ax_lab_bin_directory_state={"stat": {}})
        )
        verify = self.host["Verify the installed AX CLI is its pinned build"]
        self.assertEqual(verify["when"], "not ansible_check_mode")
        installed = {**good, "mode": "0755"}
        self.assert_pass([verify], ax_lab_ax_cli_installed={"stat": installed})
        for change in ({"checksum": "1" * 64}, {"mode": "0777"}, {"islnk": True}):
            with self.subTest(installed=change):
                self.assert_fail(
                    [verify],
                    "is not the pinned ax CLI",
                    ax_lab_ax_cli_installed={"stat": {**installed, **change}},
                )

    def test_helpers_are_root_only_regular_files_checked_by_sh(self) -> None:
        install = self.host["Install the AX operator helpers"]
        self.assertEqual(
            install["ansible.builtin.template"],
            {
                "src": "ax/{{ item.template }}",
                "dest": "{{ ax_lab_ax_helper_directory }}/{{ item.name }}",
                "owner": "root",
                "group": "root",
                "mode": "0750",
                "validate": "/bin/sh -n %s",
            },
        )
        self.assertEqual(
            install["loop"],
            [
                {"name": "ax", "template": "ax.sh.j2"},
                {"name": "ax-tarea", "template": "ax-tarea.sh.j2"},
            ],
        )
        verify = self.host["Verify the AX operator helpers"]
        self.assertEqual(verify["when"], "not ansible_check_mode")
        good = {"exists": True, "isreg": True, "islnk": False, "uid": 0, "gid": 0}
        self.assert_pass(
            [verify],
            ax_lab_ax_helpers={
                "results": [
                    {"item": name, "stat": {**good, "mode": "0750"}}
                    for name in ("ax", "ax-tarea")
                ]
            },
        )
        for change in ({"islnk": True}, {"mode": "0755"}, {"uid": 1001}):
            with self.subTest(change=change):
                self.assert_fail(
                    [verify],
                    "is not a root:root 0750 regular file",
                    ax_lab_ax_helpers={
                        "results": [
                            {
                                "item": "ax-tarea",
                                "stat": {**good, "mode": "0750", **change},
                            }
                        ]
                    },
                )

    # -- ax_images_read.yml, ax_images.yml ---------------------------------

    def test_ax_images_move_or_are_lost_by_where_they_are_held(self) -> None:
        read = self.images_read[
            "Read the pinned AX images in the registry and the backup"
        ]
        self.assertIs(read["check_mode"], False)
        self.assertIs(read["changed_when"], False)
        self.assertEqual(
            read["ansible.builtin.script"]["executable"], "/usr/bin/python3"
        )
        for status, registry in (("running", True), ("exited", False)):
            with self.subTest(registry=status):
                self.assert_pass(
                    [
                        {
                            "name": "Render the read",
                            "ansible.builtin.set_fact": {
                                "probe_cmd": read["ansible.builtin.script"]["cmd"]
                            },
                        },
                        probe("probe_cmd.split()[1:6] == ['image-status', '--image-set', 'ax', '--layout', '/var/backups/dockerswarm/ax-lab/images']"),
                        probe("probe_cmd.split()[6:8] == ['--tag', 'f009cc8-issue375']"),
                        probe("('--registry 127.0.0.1:5001' in probe_cmd) == expected"),
                        probe("probe_cmd.split()[-4:] == ax_lab_ax_image_args"),
                    ],
                    ax_lab_registry=json.loads(container_read("kind-registry", status=status)["stdout"]),
                    expected=registry,
                )  # fmt: skip
        everywhere = dict.fromkeys(AX_DIGESTS, "pinned")
        complete = dict.fromkeys(AX_DIGESTS, "complete")
        names = sorted(AX_DIGESTS)
        for label, registry, backup, expected in (
            ("converged", everywhere, complete, ([], [], [])),
            ("the window: seeded backup, empty registry", dict.fromkeys(AX_DIGESTS, "missing"), complete, ([], names, [])),
            ("registry stopped", None, complete, ([], names, [])),
            ("backup lost", everywhere, dict.fromkeys(AX_DIGESTS, "missing"), (names, [], [])),
            ("moved tag", {**everywhere, "ax-agents": "moved"}, complete, ([], ["ax-agents"], [])),
            ("never seeded", dict.fromkeys(AX_DIGESTS, "missing"), {**complete, "ax-agents": "missing"}, ([], sorted(set(names) - {"ax-agents"}), ["ax-agents"])),
        ):  # fmt: skip
            with self.subTest(case=label):
                self.assert_pass(
                    [
                        *self.facts(self.images_read),
                        probe("ax_lab_ax_exports | sort == expected[0]"),
                        probe("ax_lab_ax_imports | sort == expected[1]"),
                        probe("ax_lab_ax_lost | sort == expected[2]"),
                    ],
                    ax_lab_ax_image_status_raw={
                        "rc": 0,
                        "stdout_lines": ["\r", json.dumps({"registry": registry, "backup": backup})],
                    },
                    expected=list(expected),
                )  # fmt: skip

    def test_lost_ax_images_stop_the_apply_and_moves_happen_only_when_needed(
        self,
    ) -> None:
        lost = self.images[
            "Refuse a pinned AX image held by neither backup nor registry"
        ]
        base = {"ax_lab_ax_image_status": {"registry": {}}, "ax_lab_ax_lost": []}
        self.assert_pass([lost], **base)
        self.assert_fail(
            [lost],
            "ax-agents is in neither",
            **{**base, "ax_lab_ax_lost": ["ax-agents"]},
        )
        self.assert_fail(
            [lost],
            "never compiled here",
            **{**base, "ax_lab_ax_image_status": {"registry": None}},
        )
        for name, verb, fact in (
            ("Back up the pinned AX images the backup lacks", "export", "ax_lab_ax_exports"),
            ("Restore the pinned AX images the registry lacks", "import", "ax_lab_ax_imports"),
        ):  # fmt: skip
            with self.subTest(task=name):
                task = self.images[name]
                self.assertEqual(task["when"], f"{fact} | length > 0")
                self.assertEqual(
                    task["environment"], "{{ operation_lock_guard_environment }}"
                )
                self.assertIs(task["changed_when"], True)
                self.assertTrue(self.holds(task["when"], **{fact: ["ax-agents"]}))
                self.assertFalse(self.holds(task["when"], **{fact: []}))
                self.assert_pass(
                    [
                        {
                            "name": "Render the argv",
                            "ansible.builtin.set_fact": {"probe_argv": task["ansible.builtin.command"]["argv"]},
                        },
                        probe("probe_argv == expected"),
                    ],
                    **{fact: ["ax-agents"]},
                    expected=[
                        "/usr/bin/python3",
                        "/opt/dockerswarm/ax-lab/bin/manage-ax-lab-substrate.py",
                        verb,
                        "--image-set", "ax",
                        "--registry", "127.0.0.1:5001",
                        "--layout", "/var/backups/dockerswarm/ax-lab/images",
                        "--tag", AX_TAG,
                        f"--image=ax-agents={AX_DIGESTS['ax-agents']}",
                    ],
                )  # fmt: skip

    def test_every_ax_pin_is_verified_and_the_registry_never_oomed(self) -> None:
        verify = self.images[
            "Verify every pinned AX image in the registry and the backup"
        ]
        names = list(AX_DIGESTS)
        good = {
            "ax_lab_ax_image_status": {"registry": {}},
            "ax_lab_ax_in_registry": names,
            "ax_lab_ax_in_backup": names,
        }
        self.assert_pass([verify], **good)
        for change in (
            {"ax_lab_ax_in_registry": names[1:]},
            {"ax_lab_ax_in_backup": names[:-1]},
            {"ax_lab_ax_image_status": {"registry": None}},
        ):
            with self.subTest(change=list(change)):
                self.assert_fail(
                    [verify],
                    "does not hold every pinned AX image",
                    **{**good, **change},
                )
        gate = self.images[
            "Require no OOM kill in the local registry after the AX images"
        ]
        for text, accepted in (
            ("low 0\nmax 3\noom 0\noom_kill 0\n", True),
            ("oom_kill 1\n", False),
            ("low 0\n", False),
        ):
            with self.subTest(events=text):
                variables = {
                    "ax_lab_ax_registry_memory_events": {
                        "content": base64.b64encode(text.encode()).decode()
                    }
                }
                if accepted:
                    self.assert_pass([gate], **variables)
                else:
                    self.assert_fail([gate], "while it took the AX images", **variables)
        names = list(self.images)
        self.assertLess(names.index(verify["name"]), names.index(gate["name"]))

    # -- ax_read.yml -------------------------------------------------------

    def test_ax_is_read_only_while_the_node_runs_after_substrate(self) -> None:
        record = self.read[
            "Record the AX state and whether AX can be read in the cluster"
        ]
        for label, check, extra, expected in (
            ("apply after substrate.yml", False, {}, True),
            ("apply, Substrate drift left in its last read", False, {"ax_lab_substrate_drift": ["state"]}, True),
            ("check, converged Substrate", True, {}, True),
            ("check, Substrate would reinstall", True, {"ax_lab_substrate_drift": ["workloads"]}, False),
            ("node stopped", False, {"ax_lab_node": json.loads(container_read("kind-control-plane", status="exited")["stdout"])}, False),
            ("no node", False, {"ax_lab_node": None}, False),
            ("Substrate unread", False, {"ax_lab_substrate_cluster": None}, False),
        ):  # fmt: skip
            with self.subTest(case=label):
                self.assert_pass(
                    [record, probe("ax_lab_ax_readable == expected")],
                    check=check,
                    expected=expected,
                    ax_lab_ax_state_file={"stat": {"exists": False}},
                    **extra,
                )
        for name, task in self.read.items():
            command = task.get("ansible.builtin.command")
            if command is None:
                continue
            with self.subTest(task=name):
                self.assertIs(task["check_mode"], False)
                self.assertIs(task["changed_when"], False)
                if name != "Read the AX operator helpers that are running":
                    self.assertIn("ax_lab_ax_readable", str(task["when"]))
                argv = command["argv"]
                if "kubectl" in argv[0]:
                    self.assertEqual(argv[:7], KUBECTL_PREFIX)
                    self.assertIn(argv[7], ("get", "diff"))
                    self.assertEqual(
                        task["environment"], {"HOME": "{{ ax_lab_home_directory }}"}
                    )
                else:
                    self.assertIn(
                        argv[:2],
                        (
                            ["/usr/bin/systemctl", "list-units"],
                            ["/usr/bin/docker", "container"],
                        ),
                    )
        scopes = self.read["Read the AX operator helpers that are running"]
        self.assertEqual(
            scopes["ansible.builtin.command"]["argv"][-2:], ["ax-tarea-*", "ax-cli-*"]
        )  # fmt: skip

    def test_the_ax_state_file_is_root_only(self) -> None:
        name = "Require a root-only regular AX state file when there is one"
        good = {
            "exists": True,
            "isreg": True,
            "islnk": False,
            "uid": 0,
            "gid": 0,
            "mode": "0600",
        }
        self.assert_pass(
            [self.read[name]], ax_lab_ax_state_file={"stat": {"exists": False}}
        )
        self.assert_pass([self.read[name]], ax_lab_ax_state_file={"stat": good})
        for change in (
            {"mode": "0644"},
            {"uid": 1001},
            {"islnk": True},
            {"isreg": False},
        ):
            with self.subTest(change=change):
                self.assert_fail(
                    [self.read[name]],
                    "is not a root:root 0600 regular file",
                    ax_lab_ax_state_file={"stat": {**good, **change}},
                )  # fmt: skip
        slurp = self.read["Read the AX state file"]
        self.assertEqual(slurp["when"], "ax_lab_ax_state_file.stat.exists")

    def test_ax_namespaces_are_never_adopted_or_swapped(self) -> None:
        both = {"ax-system": "uid-system", "ax-workers": "uid-workers"}
        for label, state, namespaces, message in (
            ("fresh cluster", None, {}, None),
            ("converged", ax_state(), both, None),
            ("interrupted first install", ax_state(phase="installing", namespaces={}), {"ax-system": "uid-new"}, None),
            ("namespace deleted since", ax_state(), {"ax-system": "uid-system"}, None),
            ("recreated cluster, old state", ax_state(node_container_id="c" * 64), {}, None),
            ("manual lab's namespaces", None, {"ax-system": "uid-manual"}, "are never adopted"),
            ("another node's state", ax_state(node_container_id="c" * 64), both, "are never adopted"),
            ("recreated by another hand", ax_state(), {**both, "ax-workers": "uid-other"}, "another hand recreated it"),
            ("interrupted reinstall, recreated", ax_state(phase="installing"), {**both, "ax-system": "uid-other"}, "another hand recreated it"),
        ):  # fmt: skip
            with self.subTest(case=label):
                tasks = [*self.read_decisions(), *self.read_refusals()]
                reads = ax_reads(state=state, namespaces=namespaces)
                if message is None:
                    self.assert_pass(tasks, **reads)
                else:
                    self.assert_fail(tasks, message, **reads)
        refuse = self.read["Refuse AX namespaces recreated since they were recorded"]
        self.assertEqual(
            refuse["when"], ["ax_lab_ax_readable", "ax_lab_ax_state_for_node"]
        )

    def test_drift_is_the_state_a_namespace_or_a_server_side_diff(self) -> None:
        both = {"ax-system": "uid-system", "ax-workers": "uid-workers"}
        other = identity()
        other["images"] = {**AX_DIGESTS, "ax-agents": "sha256:" + "1" * 64}
        for label, reads, extra, expected in (
            ("converged", ax_reads(state=ax_state(), namespaces=both), {}, []),
            ("first install", ax_reads(state=None, namespaces={}, system_rc=None, workers_rc=None), {}, ["state", "ax-system.yaml", "ax-workers.yaml"]),
            ("interrupted install", ax_reads(state=ax_state(phase="installing"), namespaces=both), {}, ["state"]),
            ("new pins", ax_reads(state=ax_state(images=other["images"]), namespaces=both), {}, ["state"]),
            ("other manifests", ax_reads(state=ax_state(manifests_sha256={"ax-system.yaml": "0" * 64, "ax-workers.yaml": "0" * 64}), namespaces=both), {}, ["state"]),
            ("hand edit of the control plane", ax_reads(state=ax_state(), namespaces=both, system_rc=1), {}, ["ax-system.yaml"]),
            ("WorkerPool scaled by hand", ax_reads(state=ax_state(), namespaces=both, workers_rc=1), {}, ["ax-workers.yaml"]),
            ("workers namespace deleted", ax_reads(state=ax_state(), namespaces={"ax-system": "uid-system"}, workers_rc=None), {}, ["ax-workers.yaml"]),
            ("node not read", ax_reads(state=ax_state(), namespaces=None, system_rc=None, workers_rc=None), {"ax_lab_node": None}, []),
        ):  # fmt: skip
            with self.subTest(case=label):
                self.assert_pass(
                    [*self.read_decisions(), probe("ax_lab_ax_drift == expected")],
                    expected=expected,
                    **reads,
                    **extra,
                )

    def test_diffs_are_server_side_and_only_where_the_namespace_exists(self) -> None:
        for name, manifest, namespace in (
            ("Diff the AX control plane against the cluster server-side", "ax-system.yaml", '"ax-system" in ax_lab_ax_namespaces'),
            ("Diff the AX WorkerPool against the cluster server-side", "ax-workers.yaml", "ax_lab.ax.worker_pool.namespace in ax_lab_ax_namespaces"),
        ):  # fmt: skip
            task = self.read[name]
            with self.subTest(task=name):
                self.assertEqual(
                    task["ansible.builtin.command"]["argv"],
                    [*KUBECTL_PREFIX, "diff", "--server-side", "--field-manager", "ax-lab", "--force-conflicts", "--filename", "-"],
                )  # fmt: skip
                self.assertEqual(
                    task["ansible.builtin.command"]["stdin"],
                    "{{ ax_lab_ax_manifests['" + manifest + "'] }}",
                )
                self.assertEqual(task["when"], ["ax_lab_ax_readable", namespace])
                register = task["register"]
                for rc, fails in ((0, False), (1, False), (2, True), (127, True)):
                    self.assertIs(
                        self.holds(task["failed_when"], **{register: {"rc": rc}}), fails
                    )
                for namespaces, runs in (
                    ({"ax-system": "u", "ax-workers": "w"}, True),
                    ({}, False),
                ):
                    self.assertIs(
                        self.holds(
                            " and ".join(f"({condition})" for condition in task["when"]),
                            ax_lab_ax_readable=True,
                            ax_lab_ax_namespaces=namespaces,
                        ),
                        runs,
                    )  # fmt: skip

    def test_the_route_timeout_is_added_once_or_replaced_in_place(self) -> None:
        both = {"ax-system": "uid-system", "ax-workers": "uid-workers"}
        base = ["router", "--mode=ingress", "--drain-delay=13s"]
        prefix = "/spec/template/spec/containers/0"
        test_name = {"op": "test", "path": prefix + "/name", "value": "atenet-router"}
        for label, args, names, drift, patch in (
            ("converged", [*base, "--route-timeout=1h"], ("atenet-router", "envoy"), False, None),
            ("reset by ate-setup", base, ("atenet-router", "envoy"), True, [test_name, {"op": "add", "path": prefix + "/args/-", "value": "--route-timeout=1h"}]),
            ("another value", [*base, "--route-timeout=30s"], ("atenet-router", "envoy"), True, [test_name, {"op": "test", "path": prefix + "/args/3", "value": "--route-timeout=30s"}, {"op": "replace", "path": prefix + "/args/3", "value": "--route-timeout=1h"}]),
            ("container second", base, ("envoy", "atenet-router"), True, [{**test_name, "path": "/spec/template/spec/containers/1/name"}, {"op": "add", "path": "/spec/template/spec/containers/1/args/-", "value": "--route-timeout=1h"}]),
        ):  # fmt: skip
            with self.subTest(case=label):
                self.assert_pass(
                    [
                        *self.read_decisions(),
                        *self.read_refusals(),
                        probe("ax_lab_ax_router_drift == expected_drift"),
                        probe("not expected_drift or ax_lab_ax_router_patch == expected_patch"),
                    ],
                    expected_drift=drift,
                    expected_patch=patch,
                    **ax_reads(state=ax_state(), namespaces=both, router_args=args, router_names=names),
                )  # fmt: skip
        for label, args, names in (
            ("two timeouts", [*base, "--route-timeout=1h", "--route-timeout=2h"], ("atenet-router", "envoy")),
            ("no router container", base, ("envoy",)),
        ):  # fmt: skip
            with self.subTest(refused=label):
                self.assert_fail(
                    self.read_gates(),
                    "only a hand edit adds",
                    **ax_reads(state=ax_state(), namespaces=both, router_args=args, router_names=names),
                )  # fmt: skip
        patch = self.read["Record the JSON patch that sets the route timeout"]
        self.assertEqual(patch["when"], "ax_lab_ax_router_drift")

    def test_stale_workers_are_those_created_before_the_node_started(self) -> None:
        both = {"ax-system": "uid-system", "ax-workers": "uid-workers"}
        for label, pods, expected in (
            ("one before the start", ["ax-workers   ax-a   2026-09-25T00:57:48Z"], [["ax-workers", "ax-a", "2026-09-25T00:57:48Z"]]),
            ("in the node's own second", ["ax-workers   ax-b   2026-09-25T10:07:39Z"], []),
            ("the second before", ["ax-workers   ax-c   2026-09-25T10:07:38Z"], [["ax-workers", "ax-c", "2026-09-25T10:07:38Z"]]),
            ("after the start", ["ax-workers   ax-d   2026-09-25T15:11:02Z"], []),
            ("another pool too", ["ax-workers   ax-e   2026-09-24T01:00:00Z", "other   pool-f   2026-09-24T02:00:00Z", "ax-workers   ax-g   2026-09-25T12:00:00Z"], [["ax-workers", "ax-e", "2026-09-24T01:00:00Z"], ["other", "pool-f", "2026-09-24T02:00:00Z"]]),
            ("none", [], []),
        ):  # fmt: skip
            with self.subTest(case=label):
                self.assert_pass(
                    [
                        *self.read_decisions(),
                        probe("ax_lab_ax_stale_workers == expected"),
                    ],
                    expected=expected,
                    **ax_reads(state=ax_state(), namespaces=both, pods=pods),
                )
        times = self.read["Require well-formed node and worker times"]
        for started, pods, accepted in (
            (STARTED, ["ax-workers   ax-a   2026-09-25T00:57:48Z"], True),
            ("2026-09-25T10:07:39Z", [], True),
            ("0001-01-01T00:00:00", [], False),
            ("2026-09-25 10:07:39+02:00", [], False),
            (STARTED, ["ax-workers   ax-a   2026-09-25T00:57:48+02:00"], False),
            (STARTED, ["ax-workers   ax-a"], False),
        ):
            with self.subTest(started=started, pods=pods):
                variables = {
                    **ax_reads(state=None, namespaces={}, started=started, pods=pods),
                    "ax_lab_ax_readable": True,
                }
                variables["ax_lab_ax_time_re"] = self.read[
                    "Record the AX state and whether AX can be read in the cluster"
                ]["ansible.builtin.set_fact"]["ax_lab_ax_time_re"]
                if accepted:
                    self.assert_pass([times], **variables)
                else:
                    self.assert_fail(
                        [times], "is not an RFC 3339 time in UTC", **variables
                    )

    # -- ax.yml, workers.yml -----------------------------------------------

    def test_ax_applies_only_on_drift_then_verifies_before_recording(self) -> None:
        self.assertEqual(
            list(self.apply),
            [
                "Read AX in the lab cluster and prove its ownership",
                "Require AX to be readable in the running lab node",
                "Keep the AX reads from before any change",
                "Refuse to change AX while an ax-tarea or the ax CLI runs",
                "Record the AX install intent",
                "Read the AX controller pods before the apply",
                "Apply the AX control plane server-side only on drift",
                "Wait for each AX control plane Deployment to roll out",
                "Read the AX controller pods after the rollout",
                "Record the AX controller pods this apply started",
                "Read the head of each AX controller log this apply started",
                "Require each AX controller this apply started to consume task events",
                "Give the atenet router its route timeout only when it differs",
                "Wait for the atenet router to roll out with its route timeout",
                "Apply the AX WorkerPool server-side only on drift",
                "Recreate the workers the node's last start left stale",
                "Wait for the WorkerPool to be ready",
                "Read AX after the changes",
                "Verify the installed AX",
                "Read the memory events of the lab node itself",
                "Require the lab node never to have run out of memory at its limit",
                "Record the proof of the installed AX",
            ],
        )
        for name in (
            "Read AX in the lab cluster and prove its ownership",
            "Read AX after the changes",
        ):
            self.assertEqual(
                self.apply[name]["ansible.builtin.import_tasks"], "ax_read.yml"
            )
        self.assertEqual(
            self.apply["Recreate the workers the node's last start left stale"]["ansible.builtin.import_tasks"],
            "workers.yml",
        )  # fmt: skip
        drift_gated = (
            "Record the AX install intent",
            "Read the AX controller pods before the apply",
            "Apply the AX control plane server-side only on drift",
            "Read the AX controller pods after the rollout",
            "Apply the AX WorkerPool server-side only on drift",
        )
        for name in drift_gated:
            with self.subTest(task=name):
                self.assertEqual(
                    self.apply[name]["when"], "ax_lab_ax_drift | length > 0"
                )
                self.assertTrue(
                    self.holds(self.apply[name]["when"], ax_lab_ax_drift=["state"])
                )
                self.assertFalse(
                    self.holds(self.apply[name]["when"], ax_lab_ax_drift=[])
                )
        proof = self.apply["Record the proof of the installed AX"]
        self.assertEqual(proof["when"], "ax_lab_ax_drift_before | length > 0")
        for name, manifest in (
            ("Apply the AX control plane server-side only on drift", "ax-system.yaml"),
            ("Apply the AX WorkerPool server-side only on drift", "ax-workers.yaml"),
        ):
            task = self.apply[name]
            with self.subTest(task=name):
                self.assertEqual(
                    task["ansible.builtin.command"]["argv"],
                    [*KUBECTL_PREFIX, "apply", "--server-side", "--field-manager", "ax-lab", "--force-conflicts", "--filename", "-"],
                )  # fmt: skip
                self.assertEqual(
                    task["ansible.builtin.command"]["stdin"],
                    "{{ ax_lab_ax_manifests['" + manifest + "'] }}",
                )
                self.assertIs(task["changed_when"], True)
        intent = self.apply["Record the AX install intent"]
        both = {"ax-system": "uid-system", "ax-workers": "uid-workers"}
        render = {
            "name": "Render both state files",
            "ansible.builtin.set_fact": {
                "probe_intent": intent["ansible.builtin.copy"]["content"],
                "probe_proof": proof["ansible.builtin.copy"]["content"],
            },
        }
        expected = {**identity(), "node_container_id": NODE_ID}
        self.assert_pass(
            [
                *self.read_decisions(),
                render,
                probe("probe_intent | from_json == expected_intent"),
                probe("probe_proof | from_json == expected_proof"),
            ],
            expected_intent={**expected, "phase": "installing", "namespaces": both},
            expected_proof={**expected, "phase": "installed", "namespaces": both},
            **ax_reads(state=ax_state(phase="installing"), namespaces=both),
        )
        for task in (intent, proof):
            self.assertEqual(
                {key: value for key, value in task["ansible.builtin.copy"].items() if key != "content"},
                {"dest": "{{ ax_lab_ax_state_path }}", "owner": "root", "group": "root", "mode": "0600"},
            )  # fmt: skip

    def test_ax_applies_only_once_ax_can_be_read(self) -> None:
        require = self.apply["Require AX to be readable in the running lab node"]
        self.assert_pass([require], ax_lab_ax_readable=True)
        self.assert_fail([require], "could not be read", ax_lab_ax_readable=False)

    def test_a_deleted_namespace_leaves_the_intent_and_is_created_again(self) -> None:
        intent = self.apply["Record the AX install intent"]
        render = {
            "name": "Render the intent",
            "ansible.builtin.set_fact": {
                "probe_intent": intent["ansible.builtin.copy"]["content"]
            },
        }
        # ax-workers was deleted by hand: its old UID leaves the intent, so
        # the read after the apply accepts the one the apply creates.
        reads = ax_reads(
            state=ax_state(), namespaces={"ax-system": "uid-system"}, workers_rc=None
        )
        self.assert_pass(
            [
                *self.read_decisions(),
                render,
                probe("(probe_intent | from_json).namespaces == expected"),
            ],
            expected={"ax-system": "uid-system"},
            **reads,
        )
        after = ax_reads(
            state={**ax_state(), "phase": "installing", "namespaces": {"ax-system": "uid-system"}},
            namespaces={"ax-system": "uid-system", "ax-workers": "uid-new"},
        )  # fmt: skip
        self.assert_pass(self.read_gates(), **after)

    def test_nothing_changes_under_a_running_ax_tarea(self) -> None:
        keep = self.apply["Keep the AX reads from before any change"]
        refuse = self.apply["Refuse to change AX while an ax-tarea or the ax CLI runs"]
        running = ["ax-tarea-4242-1790000000.scope loaded active running ax-tarea"]
        both = {"ax-system": "uid-system", "ax-workers": "uid-workers"}
        stale = ["ax-workers   ax-a   2026-09-25T00:57:48Z"]
        for label, reads, message in (
            ("converged, a task runs", ax_reads(state=ax_state(), namespaces=both, scopes=running), None),
            ("drift, nothing runs", ax_reads(state=None, namespaces={}, system_rc=None, workers_rc=None), None),
            ("drift under a task", ax_reads(state=None, namespaces={}, system_rc=None, workers_rc=None, scopes=running), "state, ax-system.yaml, ax-workers.yaml would change"),
            ("router under a task", ax_reads(state=ax_state(), namespaces=both, router_args=["router"], scopes=running), "router would change"),
            ("stale workers under a CLI", ax_reads(state=ax_state(), namespaces=both, pods=stale, scopes=["ax-cli-1-2.scope loaded active running ax"]), "workers would change"),
        ):  # fmt: skip
            with self.subTest(case=label):
                tasks = [*self.read_decisions(), keep, refuse]
                if message is None:
                    self.assert_pass(tasks, **reads)
                else:
                    self.assert_fail(tasks, message, **reads)
        self.assert_fail(
            [*self.read_decisions(), keep, refuse],
            "(ax-tarea-4242-1790000000.scope)",
            **ax_reads(state=ax_state(), namespaces=both, router_args=["router"], scopes=running),
        )  # fmt: skip

    def test_waits_are_bounded_polls_never_watches(self) -> None:
        rollout = self.apply["Wait for each AX control plane Deployment to roll out"]
        self.assertEqual(rollout["loop"], ["ax-redis", "ax-server", "ax-controller"])
        self.assertEqual(
            rollout["ansible.builtin.command"]["argv"],
            [*KUBECTL_PREFIX, "rollout", "status", "deployment/{{ item }}", "--namespace", "ax-system", "--timeout=10s"],
        )  # fmt: skip
        router = self.apply[
            "Wait for the atenet router to roll out with its route timeout"
        ]
        self.assertEqual(router["when"], "ax_lab_ax_router_drift")
        for rc, done in ((0, True), (1, False)):
            self.assertIs(
                self.holds(router["until"], ax_lab_ax_router_rollout={"rc": rc}), done
            )
        pool = self.apply["Wait for the WorkerPool to be ready"]
        for task in (rollout, router, pool):
            with self.subTest(task=task["name"]):
                self.assertIs(task["changed_when"], False)
                self.assertLessEqual(task["retries"] * task["delay"], 300)
                self.assertEqual(
                    task["ansible.builtin.command"]["argv"][:7], KUBECTL_PREFIX
                )
        for register, until, result, done in (
            ("ax_lab_ax_rollout", rollout["until"], {"rc": 0}, True),
            ("ax_lab_ax_rollout", rollout["until"], {"rc": 1}, False),
            ("ax_lab_ax_pool", pool["until"], {"rc": 0, "stdout": "1 1"}, True),
            ("ax_lab_ax_pool", pool["until"], {"rc": 0, "stdout": "1 "}, False),
            ("ax_lab_ax_pool", pool["until"], {"rc": 0, "stdout": "1 0"}, False),
            ("ax_lab_ax_pool", pool["until"], {"rc": 1, "stdout": ""}, False),
        ):  # fmt: skip
            with self.subTest(until=until, result=result):
                self.assertIs(self.holds(until, **{register: result}), done)
        self.assertEqual(
            pool["ansible.builtin.command"]["argv"][7:],
            ["get", "workerpool", "{{ ax_lab.ax.worker_pool.name }}", "--namespace", "{{ ax_lab.ax.worker_pool.namespace }}", "--output", 'jsonpath={.spec.replicas}{" "}{.status.readyReplicas}'],
        )  # fmt: skip

    def test_the_controller_must_consume_events_before_any_task(self) -> None:
        before = self.apply["Read the AX controller pods before the apply"]
        after = self.apply["Read the AX controller pods after the rollout"]
        selector = ["--namespace", "ax-system", "--selector", "app.kubernetes.io/name=ax-controller", "--no-headers", "--output"]  # fmt: skip
        self.assertEqual(
            before["ansible.builtin.command"]["argv"],
            [*KUBECTL_PREFIX, "get", "pods", *selector, "custom-columns=UID:.metadata.uid"],
        )  # fmt: skip
        self.assertEqual(
            after["ansible.builtin.command"]["argv"],
            [*KUBECTL_PREFIX, "get", "pods", *selector, "custom-columns=NAME:.metadata.name,UID:.metadata.uid,DELETED:.metadata.deletionTimestamp"],
        )  # fmt: skip
        names = list(self.apply)
        self.assertLess(
            names.index(before["name"]),
            names.index("Apply the AX control plane server-side only on drift"),
        )
        self.assertGreater(
            names.index(after["name"]),
            names.index("Wait for each AX control plane Deployment to roll out"),
        )
        started = self.apply["Record the AX controller pods this apply started"]
        skipped = {"skipped": True, "changed": False}
        for label, reads, expected in (
            ("no drift", {"ax_lab_ax_controller_before": skipped, "ax_lab_ax_controller_after": skipped}, []),
            ("first install", {"ax_lab_ax_controller_before": {"stdout_lines": []}, "ax_lab_ax_controller_after": {"stdout_lines": ["ax-controller-6987dc8cdb-5m767   665be81b-a4ce-42dd-b120-99d376f81df3   <none>"]}}, ["ax-controller-6987dc8cdb-5m767"]),
            # 'state' or 'ax-workers.yaml' drift: the same controller runs.
            ("unchanged controller", {"ax_lab_ax_controller_before": {"stdout_lines": ["665be81b-a4ce-42dd-b120-99d376f81df3"]}, "ax_lab_ax_controller_after": {"stdout_lines": ["ax-controller-6987dc8cdb-5m767   665be81b-a4ce-42dd-b120-99d376f81df3   <none>"]}}, []),
            ("rolled out", {"ax_lab_ax_controller_before": {"stdout_lines": ["uid-old"]}, "ax_lab_ax_controller_after": {"stdout_lines": ["ax-controller-a-old   uid-old   2026-09-26T00:00:00Z", "ax-controller-b-new   uid-new   <none>"]}}, ["ax-controller-b-new"]),
        ):  # fmt: skip
            with self.subTest(case=label):
                self.assert_pass(
                    [started, probe("ax_lab_ax_controller_started == expected")],
                    expected=expected,
                    **reads,
                )
        log = self.apply["Read the head of each AX controller log this apply started"]
        self.assertEqual(log["loop"], "{{ ax_lab_ax_controller_started }}")
        self.assertNotIn("when", log)
        self.assertEqual(
            log["ansible.builtin.command"]["argv"],
            [*KUBECTL_PREFIX, "logs", "pod/{{ item }}", "--namespace", "ax-system", "--limit-bytes=65536"],
        )  # fmt: skip
        self.assertIs(log["no_log"], True)
        self.assertIs(log["failed_when"], False)
        self.assertIs(log["changed_when"], False)
        self.assertLessEqual(log["retries"] * log["delay"], 300)
        for result, done in (
            ({"rc": 0, "stdout": "level=INFO msg=\"starting AX task worker\" group=ax-controllers"}, True),
            ({"rc": 0, "stdout": "level=INFO msg=\"starting ax-controller\""}, False),
            ({"rc": 1, "stdout": ""}, False),
        ):  # fmt: skip
            with self.subTest(log=result):
                self.assertIs(
                    self.holds(log["until"], ax_lab_ax_controller_log=result), done
                )
        require = self.apply[
            "Require each AX controller this apply started to consume task events"
        ]
        # What Ansible registers for a loop over no pod: nothing to prove.
        empty = {
            "name": "Register an empty loop as Ansible does",
            "ansible.builtin.assert": {"that": ["true"]},
            "loop": [],
            "register": "ax_lab_ax_controller_log",
        }
        self.assert_pass([empty, require])
        self.assert_pass(
            [require], ax_lab_ax_controller_log={"skipped": True, "changed": False}
        )
        good = {"rc": 0, "stdout": "x starting AX task worker y"}
        self.assert_pass([require], ax_lab_ax_controller_log={"results": [good]})
        for results in (
            [{"rc": 0, "stdout": "starting ax-controller"}],
            [good, {"rc": 1, "stdout": ""}],
        ):
            with self.subTest(results=results):
                self.assert_fail(
                    [require],
                    "google/ax#354",
                    ax_lab_ax_controller_log={"results": results},
                )

    def test_the_router_patch_is_a_guarded_json_patch_only_on_drift(self) -> None:
        patch = self.apply[
            "Give the atenet router its route timeout only when it differs"
        ]
        self.assertEqual(patch["when"], "ax_lab_ax_router_drift")
        self.assertIs(patch["changed_when"], True)
        self.assertEqual(
            patch["ansible.builtin.command"]["argv"],
            [*KUBECTL_PREFIX, "patch", "deployment", "atenet-router", "--namespace", "ate-system", "--type", "json", "--field-manager", "ax-lab", "--patch", "{{ ax_lab_ax_router_patch | to_json }}"],
        )  # fmt: skip

    def test_the_installed_ax_is_verified_before_it_is_recorded(self) -> None:
        verify = self.apply["Verify the installed AX"]
        before = {"ax-system": "uid-system", "ax-workers": "uid-workers"}
        good = {
            "ax_lab_ax_drift": ["state"],
            "ax_lab_ax_router_drift": False,
            "ax_lab_ax_stale_workers": [],
            "ax_lab_ax_namespaces": before,
            "ax_lab_ax_namespaces_before": before,
        }
        self.assert_pass([verify], **good)
        self.assert_pass([verify], **{**good, "ax_lab_ax_namespaces_before": {}})
        for change in (
            {"ax_lab_ax_drift": ["ax-system.yaml"]},
            {"ax_lab_ax_drift": ["state", "ax-workers.yaml"]},
            {"ax_lab_ax_router_drift": True},
            {
                "ax_lab_ax_stale_workers": [
                    ["ax-workers", "ax-a", "2026-09-25T00:57:48Z"]
                ]
            },
            {"ax_lab_ax_namespaces": {**before, "ax-system": "uid-other"}},
        ):
            with self.subTest(change=list(change)):
                self.assert_fail(
                    [verify], "differs from its pinned install", **{**good, **change}
                )
        gate = self.apply[
            "Require the lab node never to have run out of memory at its limit"
        ]
        for text, accepted in (
            ("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n", True),
            ("max 4\noom 1\noom_kill 0\n", False),
            ("low 0\n", False),
        ):
            with self.subTest(events=text):
                variables = {
                    "ax_lab_ax_node_memory_events": {
                        "content": base64.b64encode(text.encode()).decode()
                    }
                }
                if accepted:
                    self.assert_pass([gate], **variables)
                else:
                    self.assert_fail(
                        [gate], "ran out of memory at its own limit", **variables
                    )
        slurp = self.apply["Read the memory events of the lab node itself"]
        self.assertIn("memory.events.local", slurp["ansible.builtin.slurp"]["src"])  # fmt: skip

    def test_stale_workers_are_deleted_then_waited_for(self) -> None:
        delete = self.workers[
            "Recreate each worker pod created before the lab node started"
        ]
        self.assertEqual(delete["loop"], "{{ ax_lab_ax_stale_workers }}")
        self.assertIs(delete["changed_when"], True)
        self.assertEqual(
            delete["ansible.builtin.command"]["argv"],
            [*KUBECTL_PREFIX, "delete", "pod", "{{ item[1] }}", "--namespace", "{{ item[0] }}", "--ignore-not-found", "--wait=false"],
        )  # fmt: skip
        wait = self.workers["Wait until no worker pod predates the lab node's start"]
        self.assertEqual(wait["when"], "ax_lab_ax_stale_workers | length > 0")
        self.assertTrue(
            self.holds(wait["when"], ax_lab_ax_stale_workers=[["a", "b", "c"]])
        )
        self.assertFalse(self.holds(wait["when"], ax_lab_ax_stale_workers=[]))
        self.assertIs(wait["changed_when"], False)
        started = {"stdout": json.dumps(STARTED)}
        for lines, done in (
            (["ax-workers   ax-new   2026-09-25T15:11:02Z"], True),
            ([], True),
            (["ax-workers   ax-old   2026-09-25T00:57:48Z", "ax-workers   ax-new   2026-09-25T15:11:02Z"], False),
        ):  # fmt: skip
            with self.subTest(lines=lines):
                self.assertIs(
                    self.holds(
                        wait["until"],
                        ax_lab_ax_node_started=started,
                        ax_lab_ax_workers_after={"rc": 0, "stdout_lines": lines},
                    ),
                    done,
                )

    def test_every_ax_write_is_gated_and_never_reads_a_secret(self) -> None:
        writers = 0
        for relative_path in (AX_IMAGES, AX_APPLY, WORKERS):
            for task in yaml.safe_load((ROOT / relative_path).read_text()):
                command = task.get("ansible.builtin.command")
                if command is None or task.get("changed_when") is not True:
                    continue
                writers += 1
                with self.subTest(task=task["name"]):
                    self.assertTrue(
                        "when" in task
                        or task.get("loop") == "{{ ax_lab_ax_stale_workers }}"
                    )
                    if "ax_lab_substrate_manager" in str(command["argv"]):
                        self.assertEqual(
                            task["environment"],
                            "{{ operation_lock_guard_environment }}",
                        )
        # Two image moves, two applies, the router patch and the deletion.
        self.assertEqual(writers, 6)
        # No AX task names the credential files, reads a Secret or keeps the
        # dropped OpenAI proxy; only ax-tarea, run by the owner, reads them.
        for relative_path in (
            AX_HOST,
            AX_IMAGES,
            AX_IMAGES_READ,
            AX_READ,
            AX_APPLY,
            WORKERS,
        ):
            text = (ROOT / relative_path).read_text(encoding="utf-8").lower()
            with self.subTest(path=relative_path):
                for forbidden in (
                    "credential_directory",
                    "/etc/dockerswarm",
                    "openai",
                    "secret ",
                ):
                    self.assertNotIn(forbidden, text)
                for task in yaml.safe_load(text):
                    argv = (task.get("ansible.builtin.command") or {}).get("argv") or []
                    self.assertFalse({"secret", "secrets"} & set(map(str, argv)))  # fmt: skip


# --------------------------------------------------------------------------
# Operator helpers, against fake binaries

FAKE_AX = r'''#!/usr/bin/python3
"""The ax CLI as the helpers use it: records every call, never connects."""
import json, os, sys, time
from pathlib import Path
state = Path(os.environ["FAKE_DIR"])
argv = sys.argv[1:]
record = {"argv": argv, "env": {k: os.environ.get(k) for k in ("AX_HOME", "HOME", "KUBECONFIG", "PATH")}}
if argv[:1] == ["apply"]:
    record["stdin"] = sys.stdin.read()
with (state / "ax.log").open("a") as log:
    log.write(json.dumps(record) + "\n")
if argv[:1] == ["describe"]:
    print("Name:    x\nReady   " + ("False" if (state / "not-ready").exists() else "True"))
elif argv[:1] == ["ssh"]:
    command = argv[-1]
    if "auth.json" in command and (state / "sandbox-auth-endless").exists():
        # An auth.json linked to an endless stream: write until the reader
        # stops reading, and record how much got through.
        written = 0
        try:
            while True:
                written += os.write(1, b"0" * 4096)
        except BrokenPipeError:
            (state / "sandbox-written").write_text(str(written))
            os._exit(1)
    if "auth.json" in command:
        (state / "auth-read").touch()
        candidate = state / "sandbox-auth.json"
        if not candidate.exists():
            sys.exit(1)
        sys.stdout.write(candidate.read_text())
    else:
        (state / "ssh-started").touch()
        if (state / "ssh-sleep").exists():
            time.sleep(float((state / "ssh-sleep").read_text()))
        print("el agente ha terminado")
        sys.exit(int((state / "ssh-rc").read_text()) if (state / "ssh-rc").exists() else 0)
elif argv[:1] == ["delete"]:
    (state / f"deleted-{argv[1]}").touch()
elif argv[:1] == ["get"] and len(argv) > 2:
    # cmd/ax/main.go prints "Error: %v" of the gRPC status and exits 1.
    if (state / "unreachable").exists():
        print(f'Error: getting {argv[1]} "{argv[2]}": rpc error: code = Unavailable desc = connection refused', file=sys.stderr)
        sys.exit(1)
    if (state / f"deleted-{argv[1]}").exists() and not (state / "stuck").exists():
        print(f'Error: getting {argv[1]} "{argv[2]}": rpc error: code = NotFound desc = {argv[1]} "{argv[2]}" not found in atespace "default"', file=sys.stderr)
        sys.exit(1)
sys.exit(int((state / "cli-rc").read_text()) if (state / "cli-rc").exists() else 0)
'''
FAKE_DOCKER = r'''#!/usr/bin/python3
"""docker as ax-tarea's self-repair uses it on the kind node."""
import json, os, sys
from pathlib import Path
state = Path(os.environ["FAKE_DIR"])
argv = sys.argv[1:]
with (state / "docker.log").open("a") as log:
    log.write(json.dumps(argv) + "\n")
def read(name, default):
    path = state / name
    return path.read_text() if path.exists() else default
if argv[:2] == ["inspect", "-f"]:
    print(read("running", "true") if "Running" in argv[2] else read("started", "2026-09-25T10:07:39.976123456Z"))
elif argv[:1] == ["exec"] and argv[2:5] == ["sysctl", "-n", "net.ipv4.conf.all.proxy_arp"]:
    print(read("proxy_arp", "1"))
elif argv[:1] == ["exec"] and argv[2:4] == ["kubectl", "get"]:
    sys.stdout.write(read("pods", "2026-09-25T15:11:02Z\n"))
'''
FAKE_SYSTEMD_RUN = r'''#!/usr/bin/python3
"""systemd-run --scope: records the unit and runs the command in place."""
import itertools, json, os, sys
from pathlib import Path
argv = sys.argv[1:]
options = list(itertools.takewhile(lambda a: a.startswith("--"), argv))
command = argv[len(options):]
with (Path(os.environ["FAKE_DIR"]) / "systemd-run.log").open("a") as log:
    log.write(json.dumps({"options": options, "command": command}) + "\n")
os.execv(command[0], command)
'''
FAKE_RECORDER = r"""#!/usr/bin/python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
with (Path(os.environ["FAKE_DIR"]) / f"{name}.log").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
if name == "flock" and (Path(os.environ["FAKE_DIR"]) / "flock-busy").exists():
    sys.exit(1)
"""


class HelperScriptTests(unittest.TestCase):
    """/usr/local/sbin/ax and ax-tarea, rendered and run with fakes."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fake = self.root / "fake"
        self.state = self.root / "state"
        self.bin = self.root / "bin"
        self.run_dir = self.root / "run"
        self.locks = self.root / "lock"
        self.credentials = self.root / "credentials"
        for directory in (
            self.fake,
            self.state,
            self.bin,
            self.run_dir,
            self.locks,
            self.credentials / "codex",
        ):
            directory.mkdir(parents=True)
        for name, source in (
            ("docker", FAKE_DOCKER),
            ("systemd-run", FAKE_SYSTEMD_RUN),
            ("systemctl", FAKE_RECORDER),
            ("flock", FAKE_RECORDER),
        ):
            self.executable(self.fake / name, source)
        self.executable(self.fake / "id", '#!/bin/sh\necho "${FAKE_UID:-0}"\n')
        self.executable(self.fake / "sleep", "#!/bin/sh\nexit 0\n")
        self.executable(self.bin / "ax", FAKE_AX)
        # Credential-shaped values are assembled here, never stored as
        # literals, and have no entropy worth a secret scanner's attention.
        self.claude = "claude-" + "c" * 40
        self.write_private(self.credentials / "claude-oauth-token", self.claude)
        self.host_session = self.session("a", "2026-09-24T23:27:01.123456789Z")
        self.write_private(
            self.credentials / "codex/auth.json", json.dumps(self.host_session)
        )
        self.scripts = {}
        for name in ("ax", "ax-tarea"):
            path = self.root / "sbin" / name
            path.parent.mkdir(exist_ok=True)
            self.executable(path, self.render(name))
            self.scripts[name] = path

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def executable(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    @staticmethod
    def write_private(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)

    @staticmethod
    def session(letter: str, refreshed: str, account: str = "acct-" + "1" * 8):
        return {
            "OPENAI_API_KEY": None,
            "auth_mode": "chatgpt",
            "last_refresh": refreshed,
            "tokens": {
                "access_token": letter * 40,
                "account_id": account,
                "id_token": letter.upper() * 40,
                "refresh_token": letter * 20 + "r" * 20,
            },
        }

    def template_variables(self) -> dict[str, Any]:
        lab = copy.deepcopy(document()["ax_lab"])
        lab["credential_directory"] = str(self.credentials)
        return {
            "ax_lab": lab,
            "ax_lab_bin_directory": str(self.bin),
            "ax_lab_home_directory": str(self.root / "home"),
            "ax_lab_kubeconfig_path": str(self.root / "home/.kube/config"),
            "ax_lab_ax_runtime_directory": str(self.run_dir),
            "ax_lab_ax_lock_directory": str(self.locks),
            "ax_lab_ax_cli": str(self.bin / "ax"),
        }

    def render(self, name: str) -> str:
        environment = jinja2.Environment(
            loader=jinja2.FileSystemLoader(TEMPLATES),
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            keep_trailing_newline=True,
        )
        template = {"ax": "ax.sh.j2", "ax-tarea": "ax-tarea.sh.j2"}[name]
        return environment.get_template(template).render(**self.template_variables())

    def run_helper(
        self,
        name: str,
        *arguments: str,
        inner: bool = False,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.fake}:/usr/bin:/bin",
            "FAKE_DIR": str(self.state),
            "LANG": "C.UTF-8",
            **(environment or {}),
        }
        if inner:
            ax_home = self.run_dir / "ax-tarea.inner"
            ax_home.mkdir(exist_ok=True)
            env.update(AX_TAREA_SCOPE="1", AX_HOME=str(ax_home))
        return subprocess.run(
            [str(self.scripts[name]), *arguments],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    @staticmethod
    def recent(seconds: int = 0) -> str:
        """last_refresh as Codex writes it, `seconds` from now."""
        moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def reset(self) -> None:
        for path in self.state.iterdir():
            path.unlink()

    def interrupted(
        self,
        name: str,
        arguments: list[str],
        signum: int,
        *,
        group: bool,
        inner: bool = False,
    ) -> tuple[int, str]:
        """Run a helper, and signal it once `ax ssh` runs.

        With `group`, the whole process group, as a terminal's Ctrl+C does;
        otherwise only the helper's own process, as `kill` does.
        """
        (self.state / "ssh-sleep").write_text("2")
        env = {
            "PATH": f"{self.fake}:/usr/bin:/bin",
            "FAKE_DIR": str(self.state),
            "LANG": "C.UTF-8",
        }
        if inner:
            ax_home = self.run_dir / "ax-tarea.inner"
            ax_home.mkdir(exist_ok=True)
            env.update(AX_TAREA_SCOPE="1", AX_HOME=str(ax_home))
        process = subprocess.Popen(
            [str(self.scripts[name]), *arguments],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 60
        while not (self.state / "ssh-started").exists():
            if process.poll() is not None:
                self.fail(f"it ended before `ax ssh` ran: {process.communicate()}")
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        if group:
            os.killpg(process.pid, signum)
        else:
            process.send_signal(signum)
        stdout, stderr = process.communicate(timeout=120)
        return process.returncode, stdout + stderr

    def calls(self, name: str) -> list[Any]:
        path = self.state / f"{name}.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def ax_argv(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls("ax")]

    # -- /usr/local/sbin/ax -----------------------------------------------

    def test_the_wrapper_runs_the_cli_in_its_own_scope_and_ax_home(self) -> None:
        (self.state / "cli-rc").write_text("3")
        completed = self.run_helper("ax", "get", "tasks", "-a", "default")
        self.assertEqual(completed.returncode, 3, completed.stderr)
        (call,) = self.calls("ax")
        self.assertEqual(call["argv"], ["get", "tasks", "-a", "default"])
        ax_home = Path(call["env"]["AX_HOME"])
        self.assertEqual(ax_home.parent, self.run_dir)
        self.assertTrue(ax_home.name.startswith("ax-cli."))
        # No port-forward outlives the run: the scope is stopped and its
        # AX_HOME, where the CLI keeps its tunnels, is gone.
        self.assertFalse(ax_home.exists())
        self.assertEqual(call["env"]["HOME"], str(self.root / "home"))
        self.assertEqual(
            call["env"]["KUBECONFIG"], str(self.root / "home/.kube/config")
        )
        self.assertEqual(call["env"]["PATH"].split(":")[0], str(self.bin))
        (run,) = self.calls("systemd-run")
        self.assertEqual(run["options"][:3], ["--quiet", "--scope", "--collect"])
        unit = run["options"][3].removeprefix("--unit=")
        self.assertRegex(unit, r"^ax-cli-[0-9]+-[0-9]+$")
        self.assertEqual(
            run["command"], [str(self.bin / "ax"), "get", "tasks", "-a", "default"]
        )
        self.assertEqual(self.calls("systemctl"), [["stop", unit + ".scope"]])
        refused = self.run_helper(
            "ax", "get", "tasks", environment={"FAKE_UID": "1000"}
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("ejecútalo con sudo", refused.stderr)
        self.assertEqual(len(self.calls("ax")), 1)

    def test_int_term_and_hup_stop_the_scope_and_its_tunnels(self) -> None:
        # The CLI's port-forward runs in its own process group (tunnel.go,
        # Setpgid), so no signal reaches it: only stopping the scope ends
        # it. Both wrappers, like the manual lab's since 2026-09-25.
        repo = "https://github.com/apptolast/example"
        for name, arguments, prefix in (
            ("ax", ["ssh", "t", "-a", "default", "--", "sh", "-c", "x"], "ax-cli-"),
            ("ax-tarea", [repo, "x"], "ax-tarea-"),
        ):
            for signum, group, code in (
                (signal.SIGINT, True, 130),
                (signal.SIGTERM, False, 143),
                (signal.SIGHUP, False, 129),
                (signal.SIGHUP, True, 129),
            ):
                with self.subTest(helper=name, signal=signum.name, group=group):
                    self.reset()
                    returncode, output = self.interrupted(
                        name, arguments, signum, group=group
                    )
                    self.assertEqual(returncode, code, output)
                    (run,) = self.calls("systemd-run")
                    unit = run["options"][3].removeprefix("--unit=")
                    self.assertTrue(unit.startswith(prefix), unit)
                    self.assertEqual(
                        self.calls("systemctl"), [["stop", unit + ".scope"]]
                    )
                    self.assertEqual(list(self.run_dir.iterdir()), [])
                    if name == "ax-tarea":
                        # The inner part deleted the task first.
                        self.assertTrue((self.state / "deleted-task").exists())
                        self.assertTrue((self.state / "deleted-workspace").exists())

    # -- /usr/local/sbin/ax-tarea -----------------------------------------

    def test_ax_tarea_refuses_what_would_reach_the_task_or_the_sandbox_shell(
        self,
    ) -> None:
        repo = "https://github.com/apptolast/example.git"
        for label, arguments, environment, message in (
            ("no prompt", [repo], {}, "uso: sudo ax-tarea"),
            ("ssh URL", ["git@github.com:apptolast/example.git", "x"], {}, "solo repositorios https://"),
            ("http", ["http://github.com/apptolast/example", "x"], {}, "solo repositorios https://"),
            ("credentials in the URL", ["https://user:pw@github.com/a/b", "x"], {}, "repositorio no es válido"),
            ("quote in the URL", ['https://github.com/a/b"c', "x"], {}, "repositorio no es válido"),
            ("space in the URL", ["https://github.com/a/b c", "x"], {}, "repositorio no es válido"),
            ("dot directory", ["https://github.com/a/..", "x"], {}, "directorio no es válido"),
            ("other agent", [repo, "x", "gemini"], {}, "agente: claude o codex"),
            ("turns not an integer", [repo, "x"], {"TURNOS": "20; id"}, "TURNOS tiene que ser un número entero"),
            ("turns with a sign", [repo, "x"], {"TURNOS": "-1"}, "número entero"),
            ("option as branch", [repo, "x"], {"RAMA": "-x"}, "RAMA no es válido"),
            ("quoted branch", [repo, "x"], {"RAMA": 'main"'}, "RAMA no es válido"),
            ("CPU with YAML", [repo, "x"], {"CPU": '1"\n  x: y'}, "CPU no es válido"),
            ("memory unit", [repo, "x"], {"MEMORIA": "1Ti"}, "MEMORIA no es válido"),
            # grep -x matches line by line: a newline is refused before it.
            ("newline in the branch", [repo, "x"], {"RAMA": 'main\nx"   injected: "y'}, "RAMA no es válido: tiene un salto de línea"),
            ("newline in the URL", ['https://github.com/a/b\n"; evil', "x"], {}, "repositorio no es válido: tiene un salto de línea"),
            ("newline in the CPU", [repo, "x"], {"CPU": '1\n"\n  debug: false'}, "CPU no es válido: tiene un salto de línea"),
            ("newline in the memory", [repo, "x"], {"MEMORIA": "1Gi\n1Gi"}, "MEMORIA no es válido: tiene un salto de línea"),
            ("trailing newline", [repo + "\n", "x"], {}, "repositorio no es válido: tiene un salto de línea"),
        ):  # fmt: skip
            with self.subTest(case=label):
                for log in self.state.glob("*.log"):
                    log.unlink()
                completed = self.run_helper(
                    "ax-tarea", *arguments, inner=True, environment=environment
                )
                self.assertEqual(completed.returncode, 64, completed.stderr)
                self.assertIn(message, completed.stderr)
                self.assertEqual(self.calls("ax"), [])
                self.assertEqual(self.calls("docker"), [])  # fmt: skip

    def test_ax_tarea_runs_alone_and_never_beside_a_host_operation(self) -> None:
        repo = "https://github.com/apptolast/example"
        (self.state / "flock-busy").touch()
        busy = self.run_helper("ax-tarea", repo, "x", inner=True)
        self.assertEqual(busy.returncode, 75)
        self.assertIn("otra ax-tarea sigue en marcha", busy.stderr)
        self.assertEqual(self.calls("flock"), [["-w", "600", "9"]])
        self.assertTrue((self.locks / "ax-tarea.lock").exists())
        (self.state / "flock-busy").unlink()
        marker = self.locks / "dockerswarm-ansible.marker"
        marker.write_text("{}")
        blocked = self.run_helper("ax-tarea", repo, "x", inner=True)
        self.assertEqual(blocked.returncode, 75)
        self.assertIn(
            f"hay una operación del host en marcha ({marker})", blocked.stderr
        )
        self.assertEqual(self.calls("ax"), [])
        self.assertEqual(self.calls("docker"), [])

    def test_ax_tarea_repairs_the_lab_only_after_a_node_restart(self) -> None:
        repo = "https://github.com/apptolast/example"
        completed = self.run_helper("ax-tarea", repo, "x", inner=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        writes = [
            call
            for call in self.calls("docker")
            if call[2:4]
            in (["sysctl", "-q"], ["kubectl", "delete"], ["kubectl", "rollout"])
        ]
        self.assertEqual(writes, [])
        (self.root / "state/docker.log").unlink()
        (self.state / "proxy_arp").write_text("0")
        (self.state / "pods").write_text(
            "2026-09-25T00:57:48Z\n2026-09-25T10:07:39Z\n2026-09-25T15:11:02Z\n"
        )
        (self.root / "state/deleted-task").unlink()
        (self.root / "state/deleted-workspace").unlink()
        completed = self.run_helper("ax-tarea", repo, "x", inner=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            "recreando 1 workers anteriores al arranque del nodo", completed.stdout
        )
        self.assertEqual(
            [call for call in self.calls("docker") if call[2:4] in (["sysctl", "-q"], ["kubectl", "delete"], ["kubectl", "rollout"])],
            [
                ["exec", "kind-control-plane", "sysctl", "-q", "net.ipv4.conf.all.proxy_arp=1"],
                ["exec", "kind-control-plane", "sysctl", "-q", "-e", "net.ipv6.conf.all.proxy_ndp=1"],
                ["exec", "kind-control-plane", "kubectl", "delete", "pod", "-n", "ax-workers", "-l", "ate.dev/worker-pool", "--wait=true"],
                ["exec", "kind-control-plane", "kubectl", "rollout", "status", "-n", "ax-workers", "deploy/ax", "--timeout=180s"],
            ],
        )  # fmt: skip
        (self.state / "running").write_text("false")
        stopped = self.run_helper("ax-tarea", repo, "x", inner=True)
        self.assertEqual(stopped.returncode, 1)
        self.assertIn("el nodo kind-control-plane no está en marcha", stopped.stderr)

    def test_the_token_reaches_only_ax_apply_on_stdin(self) -> None:
        repo = "https://github.com/apptolast/example.git"
        completed = self.run_helper(
            "ax-tarea",
            repo,
            "revisa el README",
            inner=True,
            environment={"RAMA": "dev/x", "TURNOS": "5"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.calls("ax")
        applies = [call for call in calls if call["argv"][:1] == ["apply"]]
        self.assertEqual([call["argv"] for call in applies], [["apply", "-f", "-"]])
        manifest = list(yaml.safe_load_all(applies[0]["stdin"]))
        workspace, task = manifest
        self.assertEqual(
            workspace["spec"]["git"],
            [{"name": "origin", "repo": repo, "branch": "dev/x"}],
        )
        self.assertEqual(
            task["spec"]["image"],
            f"localhost:5001/ax-agents:{AX_TAG}@{AX_DIGESTS['ax-agents']}",
        )
        self.assertIs(task["spec"]["debug"], True)
        self.assertEqual(
            task["spec"]["env"],
            [{"name": "CLAUDE_CODE_OAUTH_TOKEN", "value": self.claude}],
        )
        for call in calls:
            self.assertNotIn(self.claude, json.dumps(call["argv"]))
        self.assertNotIn(self.claude, completed.stdout + completed.stderr)
        ssh = next(call["argv"] for call in calls if call["argv"][:1] == ["ssh"])
        self.assertEqual(
            ssh[:5], ["ssh", task["metadata"]["name"], "-a", "default", "--"]
        )
        self.assertIn("cd /workspace/example && ax-agent claude", ssh[-1])
        # As the manual lab's since 2026-09-25: no project settings or MCP
        # servers from the cloned repository beside the token.
        self.assertTrue(
            ssh[-1].endswith(
                "--max-turns 5 --setting-sources user --strict-mcp-config"
            ),
            ssh[-1],
        )
        self.assertIn(
            "AX no aplica (google/ax#369): el techo es el del worker, 1500m de CPU y 1536 MiB",
            completed.stdout,
        )
        # The task and its workspace are gone before it returns.
        names = [call["argv"][:3] for call in calls if call["argv"][:1] == ["delete"]]
        self.assertEqual(
            names,
            [["delete", "task", task["metadata"]["name"]], ["delete", "workspace", "ws-" + task["metadata"]["name"]]],
        )  # fmt: skip

    def test_cleanup_confirms_the_deletion_or_says_how_to_retry(self) -> None:
        repo = "https://github.com/apptolast/example"
        (self.state / "stuck").touch()
        completed = self.run_helper("ax-tarea", repo, "x", inner=True)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("siguen existiendo y guardan el token", completed.stderr)
        self.assertRegex(
            completed.stderr, r"Reintenta: sudo ax delete task tarea-[0-9-]+ -a default"
        )
        gets = [
            call["argv"]
            for call in self.calls("ax")
            if call["argv"][:2] == ["get", "task"]
        ]
        self.assertEqual(len(gets), 45)
        (self.state / "stuck").unlink()
        for path in self.state.glob("deleted-*"):
            path.unlink()
        kept = self.run_helper(
            "ax-tarea", repo, "x", inner=True, environment={"CONSERVAR": "1"}
        )
        self.assertEqual(kept.returncode, 0)
        self.assertIn("CONSERVAR=1: la tarea", kept.stderr)
        self.assertFalse(list(self.state.glob("deleted-*")))
        # Only ax-server's NotFound proves the deletion: a lost tunnel or
        # server also fails `ax get`, and the task may still hold the token.
        self.reset()
        (self.state / "unreachable").touch()
        lost = self.run_helper("ax-tarea", repo, "x", inner=True)
        self.assertEqual(lost.returncode, 0)
        self.assertIn("siguen existiendo y guardan el token", lost.stderr)
        self.assertEqual(
            len([call for call in self.ax_argv() if call[:2] == ["get", "task"]]),
            45,
        )

    def test_int_term_and_hup_still_delete_the_task(self) -> None:
        # HUP too: closing the terminal must not leave the task and its
        # token in Redis.
        repo = "https://github.com/apptolast/example"
        for signum, group, code in (
            (signal.SIGINT, True, 130),
            (signal.SIGTERM, False, 143),
            (signal.SIGHUP, False, 129),
        ):
            with self.subTest(signal=signum.name):
                self.reset()
                returncode, output = self.interrupted(
                    "ax-tarea", [repo, "x"], signum, group=group, inner=True
                )
                self.assertEqual(returncode, code, output)
                self.assertTrue((self.state / "deleted-task").exists())
                self.assertTrue((self.state / "deleted-workspace").exists())

    def test_the_scope_wrapper_leaves_no_ax_home_behind(self) -> None:
        completed = self.run_helper(
            "ax-tarea", "https://github.com/apptolast/example", "x"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        (run,) = self.calls("systemd-run")
        self.assertEqual(run["command"][0], str(self.scripts["ax-tarea"]))
        unit = run["options"][3].removeprefix("--unit=")
        self.assertRegex(unit, r"^ax-tarea-[0-9]+-[0-9]+$")
        self.assertEqual(self.calls("systemctl"), [["stop", unit + ".scope"]])
        ax_homes = {call["env"]["AX_HOME"] for call in self.calls("ax")}
        self.assertEqual(len(ax_homes), 1)
        self.assertFalse(Path(ax_homes.pop()).exists())
        self.assertEqual(list(self.run_dir.iterdir()), [])

    def codex(self, sandbox: Any | None, *, raw: str | None = None):
        if raw is not None:
            (self.state / "sandbox-auth.json").write_text(raw)
        elif sandbox is not None:
            (self.state / "sandbox-auth.json").write_text(json.dumps(sandbox))
        before = (self.credentials / "codex/auth.json").read_bytes()
        completed = self.run_helper(
            "ax-tarea", "https://github.com/apptolast/example", "x", "codex", inner=True
        )
        after = (self.credentials / "codex/auth.json").read_bytes()
        output = completed.stdout + completed.stderr
        for letter in ("a", "b"):
            self.assertNotIn(letter * 40, output)
            self.assertNotIn(letter.upper() * 40, output)
        self.assertEqual(
            sorted(path.name for path in (self.credentials / "codex").iterdir()),
            ["auth.json"],
        )
        return completed, before, after

    def test_a_refreshed_codex_session_is_copied_back_atomically(self) -> None:
        refreshed = self.session("b", self.recent())
        completed, before, after = self.codex(refreshed)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(after), refreshed)
        self.assertNotEqual(before, after)
        self.assertEqual(
            stat.S_IMODE((self.credentials / "codex/auth.json").stat().st_mode), 0o600
        )
        self.assertIn("copiada de vuelta", completed.stdout)
        # The task carried the host's session, base64 on stdin only.
        apply = next(call for call in self.calls("ax") if call["argv"][:1] == ["apply"])
        (task,) = [
            item
            for item in yaml.safe_load_all(apply["stdin"])
            if item["kind"] == "Task"
        ]
        (variable,) = task["spec"]["env"]
        self.assertEqual(variable["name"], "CODEX_AUTH_JSON_B64")
        self.assertEqual(
            json.loads(base64.b64decode(variable["value"])), self.host_session
        )
        self.assertFalse(list((self.run_dir / "ax-tarea.inner").iterdir()))

    def test_a_codex_session_that_is_not_the_hosts_newer_one_is_never_copied(
        self,
    ) -> None:
        now = self.recent()
        extra = self.session("b", now)
        extra["api_base"] = "https://example.invalid"
        other_tokens = self.session("b", now)
        other_tokens["tokens"]["scope"] = "x"
        empty = self.session("b", now)
        empty["tokens"]["refresh_token"] = ""
        mode = self.session("b", now)
        mode["auth_mode"] = "apikey"
        same_refresh = self.session("b", now)
        same_refresh["tokens"]["refresh_token"] = self.host_session["tokens"][
            "refresh_token"
        ]
        for label, sandbox, raw, message in (
            ("same session", self.host_session, None, "no cambió"),
            ("older", self.session("b", "2026-09-24T23:27:01.1Z"), None, "no es más reciente"),
            ("same instant", self.session("b", "2026-09-24T23:27:01.123456789Z"), None, "no es más reciente"),
            # Newer than the host's, but not refreshed during this task: a
            # far future one would block every later genuine refresh.
            ("before the task", self.session("b", self.recent(-3600)), None, "no cae dentro de esta tarea"),
            ("in the future", self.session("b", self.recent(600)), None, "no cae dentro de esta tarea"),
            ("far future", self.session("b", "9999-12-31T23:59:59Z"), None, "no cae dentro de esta tarea"),
            ("same refresh token", same_refresh, None, "no cambia el refresh token"),
            ("other account", self.session("b", now, "acct-" + "2" * 8), None, "es de otra cuenta"),
            ("extra key", extra, None, "tiene otras claves"),
            ("extra token", other_tokens, None, "sus tokens tienen otras claves"),
            ("empty token", empty, None, "tiene un token vacío"),
            ("other auth mode", mode, None, "cambia auth_mode"),
            ("local time", self.session("b", "2026-09-26T01:00:00+02:00"), None, "RFC 3339"),
            ("not JSON", None, "{not json", "no es JSON"),
            ("repeated key", None, '{"a": 1, "a": 2}', "repetida"),
            ("too large", None, " " * (64 * 1024 + 1), "mide más de 64 KiB"),
            ("not in the sandbox", None, None, "no se pudo leer la sesión de Codex"),
        ):  # fmt: skip
            with self.subTest(case=label):
                (self.state / "sandbox-auth.json").unlink(missing_ok=True)
                for path in self.state.glob("deleted-*"):
                    path.unlink()
                completed, before, after = self.codex(sandbox, raw=raw)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(before, after)
                self.assertIn(message, completed.stdout + completed.stderr)

    def test_the_host_codex_session_is_read_without_following_links(self) -> None:
        session = self.credentials / "codex/auth.json"
        target = self.credentials / "elsewhere.json"
        session.rename(target)
        session.symlink_to(target)
        completed, _before, _after = self.codex(self.session("b", self.recent()))
        self.assertIn("no se copia la sesión de Codex del sandbox", completed.stderr)
        self.assertTrue(session.is_symlink())
        self.assertEqual(json.loads(target.read_text()), self.host_session)
        session.unlink()
        target.rename(session)
        session.chmod(0o644)
        completed, before, after = self.codex(self.session("b", self.recent()))
        self.assertEqual(before, after)
        self.assertIn("no es un fichero regular 0600 de root", completed.stderr)

    def test_an_endless_sandbox_session_never_fills_the_host(self) -> None:
        # /run is a tmpfs, that is, host memory: the read is cut at 64 KiB
        # and one byte, and the host session is left alone.
        (self.state / "sandbox-auth-endless").touch()
        completed, before, after = self.codex(None)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(before, after)
        self.assertIn("mide más de 64 KiB", completed.stderr)
        # The writer was stopped: at most what the pipe holds got past the
        # 65 537 bytes head kept.
        self.assertLess(int((self.state / "sandbox-written").read_text()), 4 * 2**20)
        self.assertFalse(list((self.run_dir / "ax-tarea.inner").iterdir()))

    def test_an_interrupted_codex_run_still_copies_its_session_back(self) -> None:
        # Codex may have refreshed its single-use session before the
        # interruption: the copy in the sandbox is then the only valid one,
        # so it is read back before the task and its sandbox are deleted.
        repo = "https://github.com/apptolast/example"
        for signum, group, code in (
            (signal.SIGTERM, False, 143),
            (signal.SIGINT, True, 130),
        ):
            with self.subTest(signal=signum.name):
                self.reset()
                self.write_private(
                    self.credentials / "codex/auth.json",
                    json.dumps(self.host_session),
                )
                refreshed = self.session("b", self.recent())
                (self.state / "sandbox-auth.json").write_text(json.dumps(refreshed))
                returncode, output = self.interrupted(
                    "ax-tarea", [repo, "x", "codex"], signum, group=group, inner=True
                )
                self.assertEqual(returncode, code, output)
                self.assertEqual(
                    json.loads((self.credentials / "codex/auth.json").read_text()),
                    refreshed,
                )
                argv = self.ax_argv()
                reads = [
                    index
                    for index, call in enumerate(argv)
                    if call[:1] == ["ssh"] and "auth.json" in call[-1]
                ]
                self.assertEqual(len(reads), 1)
                self.assertLess(
                    reads[0],
                    argv.index(["delete", "task", argv[reads[0]][1], "-a", "default"]),
                )
                self.assertTrue((self.state / "deleted-workspace").exists())
                for letter in ("a", "b"):
                    self.assertNotIn(letter * 40, output)

    def test_ansible_renders_the_helpers_as_this_test_does(self) -> None:
        variables = self.template_variables()
        for name, template in (("ax", "ax.sh.j2"), ("ax-tarea", "ax-tarea.sh.j2")):
            with self.subTest(helper=name):
                # The helpers hold Docker's own {{...}} formats: compared by
                # digest, so the play never templates the expected text.
                completed = run_tasks(
                    [probe(f"lookup('ansible.builtin.template', '{TEMPLATES / template}') | hash('sha256') == expected")],
                    {**variables, "expected": hashlib.sha256(self.render(name).encode()).hexdigest()},
                )  # fmt: skip
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
                rendered = self.render(name)
                self.assertTrue(rendered.startswith("#!/bin/sh\n# Managed by Ansible"))
                # Only Docker's own formats are left between braces.
                self.assertNotRegex(rendered, r"ax_lab[._]")
                self.assertNotIn("{%", rendered)
                self.assertEqual(
                    rendered.count("{{"), rendered.count("{{.State."), rendered
                )
                syntax = subprocess.run(["/bin/sh", "-n", str(self.scripts[name])], capture_output=True, text=True, check=False)  # fmt: skip
                self.assertEqual(syntax.returncode, 0, syntax.stderr)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_the_rendered_helpers_pass_shellcheck(self) -> None:
        for name, path in self.scripts.items():
            with self.subTest(helper=name):
                completed = subprocess.run(
                    ["shellcheck", "--severity=style", str(path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout)


# --------------------------------------------------------------------------
# Reproducibility job, guard and docs


class AxReproducibilityWorkflowTests(unittest.TestCase):
    """reproduce-ax: builds only, and proves what can be proven."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.job = yaml.safe_load(cls.text)["jobs"]["reproduce-ax"]
        cls.script = "\n".join(step.get("run", "") for step in cls.job["steps"])

    def test_every_action_is_pinned_and_reads_only(self) -> None:
        self.assertNotIn("permissions", self.job)
        uses = [step for step in self.job["steps"] if "uses" in step]
        self.assertEqual(len(uses), 4)
        for step in uses:
            with self.subTest(uses=step["uses"]):
                self.assertRegex(step["uses"], r"^[a-z-]+/[a-z-]+@[a-f0-9]{40}$")
                if step["uses"].startswith("actions/checkout@"):
                    self.assertIs(step["with"]["persist-credentials"], False)
        checkouts = {
            step["with"].get("repository"): step["with"]
            for step in uses
            if step["uses"].startswith("actions/checkout@")
        }
        self.assertEqual(
            checkouts["google/ax"]["ref"], "${{ steps.plan.outputs.commit }}"
        )
        # Every tag: Go derives the CLI's pseudo-version from v0.3.0.
        self.assertEqual(checkouts["google/ax"]["fetch-depth"], 0)
        self.assertEqual(
            checkouts["agent-substrate/substrate"]["ref"],
            "${{ steps.plan.outputs.substrate }}",
        )
        for forbidden in (
            "docker push",
            "docker login",
            "secrets.",
            "GITHUB_TOKEN",
            "upload-artifact",
            "ko publish",
            "--push=true",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.text)  # fmt: skip

    def test_it_applies_the_pinned_patch_and_compares_every_pin(self) -> None:
        for fragment in (
            "validate-ax-lab.py --ax-plan",
            '[[ "$(git -C ax describe --tags --abbrev=0)" == v0.3.0 ]]',
            "sha256sum --check --strict",
            'git -C ax apply --index "${GITHUB_WORKSPACE}/${patch}"',
            "git -C ax diff --cached | sha256sum",
            'go build -trimpath -ldflags="-s -w" -o /out/ax ./cmd/ax',
            "GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath",
            "--env GOTOOLCHAIN=local --env GOFLAGS=-mod=readonly",
            "go tool -n ko",
            "build --push=false --sbom=none",
            'jq -r .ko_base_image "${RUNNER_TEMP}/ax-plan.json"',
            'jq -r .cli_sha256 "${plan}"',
            'jq -r .task_runner_binary_sha256 "${plan}"',
            'BASE_NAME = "org.opencontainers.image.base.name"',
            "go test ./... >/out/test-baseline.log",
            "go test ./... >/out/test-patched.log",
            'comm -13 "${RUNNER_TEMP}/failures-baseline.txt"',
            '[[ -z "${new}" ]]',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.script)
        # The baseline runs before the patch, the builds and comparison after.
        names = [step["name"] for step in self.job["steps"]]
        self.assertLess(
            names.index("Record the unpatched test baseline"),
            names.index("Apply the vendored patch exactly as the manual lab staged it"),
        )
        self.assertLess(
            names.index("Rebuild the ko images of ax-controller and ax-server"),
            names.index("Require the rebuilt binaries and ko images to match their pins"),
        )  # fmt: skip


class AxRegistrationTests(unittest.TestCase):
    def test_sensitive_path_guard_covers_the_vendored_inputs(self) -> None:
        guard = (ROOT / ".github/workflows/guard-sensitive-paths.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("\n          ^images/ax(-[a-z-]+)?/\n", guard)

    def test_the_docs_seed_every_ax_pin_and_the_cli(self) -> None:
        docs = (ROOT / "docs/AX.md").read_text(encoding="utf-8")
        text = " ".join(docs.replace("\\\n", " ").split())
        self.assertIn(
            "manage-ax-lab-substrate.py export --image-set ax --registry "
            "127.0.0.1:5001 --layout /var/backups/dockerswarm/ax-lab/images "
            "--tag f009cc8-issue375 --source-naming ko-md5",
            text,
        )
        for name, digest in AX_DIGESTS.items():
            with self.subTest(image=name):
                self.assertIn(f"--image={name}={digest}", text)
        self.assertIn(
            "/usr/bin/install -o root -g root -m 0600 /opt/ax-lab/bin/ax "
            "/var/backups/dockerswarm/ax-lab/binaries/ax",
            text,
        )
        self.assertIn(CLI_SHA256, text)
        for section in (
            "## AX",
            "### Imágenes de AX",
            "### Semilla de AX",
            "### Reproducibilidad de AX",
            "### Lo que no se puede codificar fielmente",
            "### Plano de control de AX",
            "### Propiedad de AX",
            "### Redis",
            "### WorkerPool y capacidad dentro del nodo",
            "### Router",
            "### Workers tras un reinicio",
            "### Herramientas del operador",
            "## Ventana del cambio 4",
        ):
            with self.subTest(section=section):
                self.assertIn("\n" + section + "\n", docs)
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        for fragment in ("--image-set ax", "ax-tarea", "google/ax#375", "reproduce-ax"):
            self.assertIn(fragment, changelog)
