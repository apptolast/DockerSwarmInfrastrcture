"""Contract of the AX web panel's deployment in the lab (docs/AX_WEB.md).

The validator's web rules and the manifest it renders, with a table of
negative mutations; the role's web tasks run through the reviewed-task
harness against synthetic reads (never Docker or a cluster); the
seed-layout subcommand against temporary OCI layouts; the owner-run
bootstrap against fake docker and kubectl binaries; the reproducibility
workflow's pin check; the examples the role installs; and the docs.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock

import jinja2
import yaml

from ansible_task_harness import SIDE_EFFECT_FREE_MODULES
from test_ax_lab_ax_contract import run_tasks
from test_ax_lab_contract import (
    CONFIG,
    MAIN,
    ROLE,
    ROOT,
    container_read,
    load_script,
    load_tasks,
    probe,
    role_task_files,
    role_variables,
    run_reviewed_tasks,
)
from test_manage_ax_lab_substrate import make_image, manager

VALIDATOR = load_script("validate_ax_lab_web", "scripts/validate-ax-lab.py")
WEB_READ = "ansible/roles/ax_lab/tasks/web_read.yml"
WEB_APPLY = "ansible/roles/ax_lab/tasks/web.yml"
EXAMPLES = "ansible/roles/ax_lab/tasks/examples.yml"
BOOTSTRAP = ROOT / "scripts/ax-web-bootstrap.sh"
WORKFLOW = ROOT / ".github/workflows/ax-web.yml"
# The two ko builds of images/ax-web at 87588c5 produced this manifest
# digest (images/ax-web/README.md); CI rebuilds it and compares.
PINNED_DIGEST = (
    "sha256:e8128547a95a3adcbb9a488faa9b379fdb9fa132337d7404cad073b2c3d78faa"
)
NODE_ID = "b" * 64
EDGE_ID = "d" * 64
NAMESPACE_UID = "uid-web"
MIB = 1048576
# The awk program the workflow runs to read the pin (see WorkflowTests).
PIN_PROGRAM = """
              /^[^ #]/ { web = 0; image = 0 }
              /^  [a-z_]+:/ { web = ($0 == "  web:"); image = 0 }
              web && /^    [a-z_]+:/ { image = ($0 == "    image:") }
              image && $0 == "      digest: >-" {
                getline; sub(/^ +/, ""); print
              }
"""


def document() -> dict[str, Any]:
    return VALIDATOR.load_yaml(CONFIG)


def lab() -> dict[str, Any]:
    return document()["ax_lab"]


def rendered(lab_: dict[str, Any] | None = None) -> str:
    return VALIDATOR.render_web_manifest(lab_ or lab())


def objects(text: str) -> list[dict[str, Any]]:
    return [item for item in yaml.safe_load_all(text) if item is not None]


def walk(value: Any):
    """Every mapping key anywhere in value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def forwarder_argv(lab_: dict[str, Any]) -> list[str]:
    """The forward subcommand's argv, pinned here independently."""
    return [
        "forward",
        "--listen",
        ":8443",
        "--target",
        "kind-control-plane:30843",
        "--allow-cidr",
        lab_["web"]["forwarder"]["edge_subnet"],
    ]


def forwarder_read(**changes: Any) -> dict[str, Any]:
    """`docker container inspect` of the forwarder as web.yml leaves it."""
    web = lab()["web"]
    record = {
        "id": EDGE_ID,
        "name": "/ax-web-edge",
        "status": "running",
        "image": f"localhost:5001/ax-web@{PINNED_DIGEST}",
        "labels": {
            "com.apptolast.managed-by": "ansible",
            "com.apptolast.ax-lab": "web-edge",
        },
        "user": "65532:65532",
        "entrypoint": ["/ko-app/ax-web"],
        "cmd": forwarder_argv(lab()),
        "read_only": True,
        "privileged": False,
        "cap_add": None,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "memory": 32 * MIB,
        "memory_swap": 32 * MIB,
        "memory_reservation": 16 * MIB,
        "nano_cpus": 250_000_000,
        "pids_limit": 64,
        "restart_policy": {"Name": "no", "MaximumRetryCount": 0},
        "network_mode": "kind",
        # As this host's Docker records kind-registry, run without them.
        "pid_mode": "",
        "ipc_mode": "private",
        "uts_mode": "",
        "userns_mode": "",
        "cgroupns_mode": "private",
        "devices": [],
        "group_add": None,
        "tmpfs": None,
        "port_bindings": {},
        "mounts": [],
        "networks": {
            "kind": {"IPAddress": "172.23.0.9"},
            web["forwarder"]["edge_network"]: {"IPAddress": "10.0.250.7"},
        },
    }
    record.update(changes)
    return record


def network_read(**changes: Any) -> dict[str, Any]:
    record = {
        "name": "apptolast-edge-ax",
        "driver": "overlay",
        "scope": "swarm",
        "attachable": True,
        "ipam": [{"Subnet": "10.0.250.0/24", "Gateway": "10.0.250.1"}],
    }
    record.update(changes)
    return record


def web_state(**changes: Any) -> dict[str, Any]:
    state = {
        "schema_version": 1,
        "image": PINNED_DIGEST,
        "manifest_sha256": VALIDATOR.web_manifest_sha256(rendered()),
        "node_container_id": NODE_ID,
        "phase": "installed",
        "namespace": NAMESPACE_UID,
    }
    state.update(changes)
    return state


def web_variables(**overrides: Any) -> dict[str, Any]:
    """The role's facts once main.yml rendered the manifest and AX runs."""
    variables = role_variables()
    variables.update(
        {
            "ax_lab_web_manifest": rendered(variables["ax_lab"]),
            "ax_lab_web_manifest_digest": {
                "stdout": VALIDATOR.web_manifest_sha256(rendered(variables["ax_lab"]))
            },
            "ax_lab_node": json.loads(container_read("kind-control-plane")["stdout"]),
            "ax_lab_registry": json.loads(container_read("kind-registry")["stdout"]),
            "ax_lab_ax_readable": True,
            "ax_lab_ax_namespaces": {"ax-system": "uid-system", "ax-workers": "u"},
            "ax_lab_ax_drift": [],
        }
    )
    variables.update(overrides)
    return variables


def web_reads(
    *,
    state: dict[str, Any] | None = None,
    namespace: str | None = NAMESPACE_UID,
    diff_rc: int = 0,
    secrets: list[str] | None = None,
    edge: dict[str, Any] | None = None,
    edge_absent: bool = False,
    network: dict[str, Any] | None = None,
    network_rc: int = 0,
    backup: str = "complete",
    registry: str | None = "pinned",
) -> dict[str, Any]:
    """What web_read.yml registers before each of its facts."""
    skipped = {"skipped": True, "changed": False}
    state = web_state() if state is None else state
    reads: dict[str, Any] = {
        "ax_lab_web_image_status_raw": {
            "stdout_lines": [
                json.dumps(
                    {
                        "backup": {"ax-web": backup},
                        "registry": (
                            None if registry is None else {"ax-web": registry}
                        ),
                    }
                )
            ]
        },
        "ax_lab_web_state_file": {
            "stat": (
                {"exists": False}
                if state == {}
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
        "ax_lab_web_namespace_read": {
            "rc": 0,
            "stdout_lines": [] if namespace is None else [f"ax-web   {namespace}"],
        },
        "ax_lab_web_diff": skipped if namespace is None else {"rc": diff_rc},
        "ax_lab_web_secrets_read": (
            skipped
            if namespace is None
            else {
                "rc": 0,
                "stdout_lines": (
                    [
                        "ax-web-tls     Opaque   3      2m",
                        "ax-web-agent   Opaque   1      2m",
                    ]
                    if secrets is None
                    else secrets
                ),
            }
        ),
        "ax_lab_web_edge_read": (
            {"rc": 1, "stdout": "", "stderr": "No such container: ax-web-edge"}
            if edge_absent
            else {
                "rc": 0,
                "stdout": json.dumps(forwarder_read() if edge is None else edge),
                "stderr": "",
            }
        ),
        "ax_lab_web_network_read": {
            "rc": network_rc,
            "stdout": (
                json.dumps(network_read() if network is None else network)
                if network_rc == 0
                else ""
            ),
        },
    }
    if state != {}:
        reads["ax_lab_web_state_content"] = {
            "content": base64.b64encode(json.dumps(state).encode()).decode()
        }
    return reads


# --------------------------------------------------------------------------
# Validator and rendered manifest


class WebValidatorTests(unittest.TestCase):
    """scripts/validate-ax-lab.py pins the panel and the manifest it renders."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = document()
        cls.lab = cls.document["ax_lab"]
        cls.text = rendered(cls.lab)
        cls.objects = objects(cls.text)
        cls.by_key = {
            (item["kind"], item["metadata"].get("namespace"), item["metadata"]["name"]): item
            for item in cls.objects
        }  # fmt: skip

    def test_contract_pins_the_panel_and_its_forwarder(self) -> None:
        web = self.lab["web"]
        self.assertEqual(web["image"]["digest"], PINNED_DIGEST)
        self.assertEqual(web["namespace"], "ax-web")
        self.assertEqual(web["node_port"], 30843)
        self.assertEqual(web["origin"], "https://ax.apptolast.com")
        self.assertEqual(web["tls_directory"], "/etc/dockerswarm/ax/web-tls")
        self.assertEqual(web["repo_hosts"], ["github.com"])
        self.assertEqual(web["blackout_utc"], "22:30-00:40")
        forwarder = web["forwarder"]
        self.assertEqual(forwarder["container"], "ax-web-edge")
        self.assertEqual(forwarder["edge_network"], "apptolast-edge-ax")
        self.assertEqual(forwarder["restart_policy"], "no")
        self.assertEqual(
            forwarder["resources"],
            {
                "memory_limit_mib": 32,
                "memory_reservation_mib": 16,
                "cpu_limit_millicores": 250,
                "pids_limit": 64,
            },
        )
        subnet = ipaddress.ip_network(forwarder["edge_subnet"])
        # Outside every network that exists on the host today (docker
        # network inspect, 2026-09-26: Swarm overlays in 10.0.0-29.0/24,
        # bridges in 172.17-23.0.0/16) and kind's pod and service subnets.
        for taken in ("10.0.0.0/19", "172.16.0.0/12", "10.244.0.0/16", "10.96.0.0/16"):
            self.assertFalse(subnet.overlaps(ipaddress.ip_network(taken)), taken)
        VALIDATOR.validate_catalog(self.document)

    def test_manifest_is_exactly_the_reviewed_objects(self) -> None:
        self.assertEqual(
            [
                (item["kind"], item["metadata"].get("namespace"), item["metadata"]["name"])
                for item in self.objects
            ],
            VALIDATOR.WEB_INVENTORY,
        )  # fmt: skip
        keys = set(walk(self.objects))
        for forbidden in (
            "hostNetwork",
            "hostPID",
            "hostIPC",
            "hostPort",
            "hostPath",
            "privileged",
            "stringData",
        ):
            self.assertNotIn(forbidden, keys)
        self.assertNotIn("Secret", [item["kind"] for item in self.objects])
        self.assertNotIn("Role", " ".join(item["kind"] for item in self.objects))
        namespace = self.by_key[("Namespace", None, "ax-web")]
        self.assertEqual(
            namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"],
            "restricted",
        )
        account = self.by_key[("ServiceAccount", "ax-web", "ax-web")]
        self.assertIs(account["automountServiceAccountToken"], False)

    def test_the_pod_runs_non_root_read_only_by_digest(self) -> None:
        deployment = self.by_key[("Deployment", "ax-web", "ax-web")]
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
        pod = deployment["spec"]["template"]["spec"]
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertEqual(pod["terminationGracePeriodSeconds"], 120)
        self.assertEqual(
            pod["securityContext"],
            {
                "runAsNonRoot": True,
                "runAsUser": 65532,
                "runAsGroup": 65532,
                "fsGroup": 65532,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        )
        (container,) = pod["containers"]
        self.assertEqual(
            container["image"], f"localhost:5001/ax-web:0.1.0@{PINNED_DIGEST}"
        )
        self.assertEqual(
            container["securityContext"],
            {
                "readOnlyRootFilesystem": True,
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            },
        )
        self.assertEqual(
            container["resources"],
            {
                "requests": {"cpu": "20m", "memory": "32Mi"},
                "limits": {"cpu": "250m", "memory": "128Mi"},
            },
        )
        for probe_name in ("readinessProbe", "livenessProbe"):
            self.assertEqual(container[probe_name]["httpGet"]["port"], 8081)
        # The Secrets are referenced by name, read-only, never rendered.
        self.assertEqual(
            [
                (volume["name"], (volume.get("secret") or {}).get("secretName"))
                for volume in pod["volumes"]
            ],
            [("config", None), ("tls", "ax-web-tls"), ("agent", "ax-web-agent")],
        )
        self.assertTrue(all(mount["readOnly"] for mount in container["volumeMounts"]))

    def test_the_configuration_is_the_panels_schema(self) -> None:
        config_map = self.by_key[("ConfigMap", "ax-web", "ax-web")]
        panel = json.loads(config_map["data"]["config.json"])
        # Every field of images/ax-web/internal/config.Config, and no other:
        # the panel decodes it with DisallowUnknownFields.
        source = (ROOT / "images/ax-web/internal/config/config.go").read_text(
            encoding="utf-8"
        )
        fields = set(re.findall(r'`json:"([a-z_]+)"`', source))
        self.assertEqual(set(panel), fields)
        self.assertEqual(panel["origin"], "https://ax.apptolast.com")
        self.assertEqual(
            panel["agent_image"],
            "localhost:5001/ax-agents:f009cc8-issue375@"
            + self.lab["ax"]["images"]["ax-agents"],
        )
        self.assertEqual(panel["token_directory"], "/var/run/ax-web/agent")
        self.assertEqual(panel["client_common_name"], "edge-traefik")
        self.assertEqual((panel["listen"], panel["health_listen"]), (":8443", ":8081"))
        annotations = self.by_key[("Deployment", "ax-web", "ax-web")]["spec"][
            "template"
        ]["metadata"]["annotations"]
        self.assertEqual(
            annotations["ax.apptolast.com/config-sha256"],
            hashlib.sha256(config_map["data"]["config.json"].encode()).hexdigest(),
        )

    def test_service_is_one_local_node_port(self) -> None:
        service = self.by_key[("Service", "ax-web", "ax-web")]
        self.assertEqual(service["spec"]["type"], "NodePort")
        self.assertEqual(service["spec"]["externalTrafficPolicy"], "Local")
        self.assertEqual(
            service["spec"]["ports"],
            [
                {
                    "name": "https",
                    "protocol": "TCP",
                    "port": 8443,
                    "targetPort": 8443,
                    "nodePort": 30843,
                }
            ],
        )

    def test_network_policies_admit_no_pod_and_reach_three_peers(self) -> None:
        policy = self.by_key[("NetworkPolicy", "ax-web", "ax-web")]["spec"]
        self.assertEqual(policy["policyTypes"], ["Ingress", "Egress"])
        (ingress,) = policy["ingress"]
        self.assertEqual(
            ingress["from"],
            [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.244.0.0/16"]}}],
        )
        # The probe port stays out: the kubelet is on the pod's node.
        self.assertEqual(ingress["ports"], [{"protocol": "TCP", "port": 8443}])
        self.assertEqual(
            [
                (
                    rule["to"][0]["namespaceSelector"]["matchLabels"][
                        "kubernetes.io/metadata.name"
                    ],
                    rule["to"][0]["podSelector"]["matchLabels"],
                    [(port["protocol"], port["port"]) for port in rule["ports"]],
                )
                for rule in policy["egress"]
            ],
            [
                ("kube-system", {"k8s-app": "kube-dns"}, [("UDP", 53), ("TCP", 53)]),
                ("ax-system", {"app.kubernetes.io/name": "ax-server"}, [("TCP", 8080)]),
                ("ate-system", {"app": "atenet-router"}, [("TCP", 8080)]),
            ],
        )  # fmt: skip
        widened = self.by_key[("NetworkPolicy", "ax-system", "ax-web-to-ax-server")]
        self.assertEqual(
            widened["spec"]["ingress"][0]["from"],
            [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "ax-web"}
                    },
                    "podSelector": {
                        "matchLabels": {"app.kubernetes.io/name": "ax-web"}
                    },
                }
            ],
        )

    # -- negative mutations ------------------------------------------------

    def manifest_rejected(
        self, change: Callable[[list[dict[str, Any]]], None], message: str
    ) -> None:
        mutated = copy.deepcopy(self.objects)
        change(mutated)
        text = yaml.safe_dump_all(mutated, sort_keys=False)
        with self.assertRaisesRegex(VALIDATOR.AxLabError, message):
            VALIDATOR.validate_web_manifest(text, self.lab)

    def config_rejected(self, change: Callable[[dict[str, Any]], None], message: str):
        mutated = copy.deepcopy(self.document)
        change(mutated["ax_lab"])
        with self.assertRaisesRegex(VALIDATOR.AxLabError, message):
            VALIDATOR.validate_catalog(mutated)

    @staticmethod
    def pod(items: list[dict[str, Any]]) -> dict[str, Any]:
        return items[3]["spec"]["template"]["spec"]

    def test_manifest_mutations_are_rejected(self) -> None:
        # The unmutated round trip passes.
        VALIDATOR.validate_web_manifest(
            yaml.safe_dump_all(self.objects, sort_keys=False), self.lab
        )
        pod = self.pod

        def container(items):
            return pod(items)["containers"][0]

        cases: list[tuple[str, Callable[[list[dict[str, Any]]], None], str]] = [
            ("hostNetwork", lambda o: pod(o).update(hostNetwork=True), "hostNetwork"),
            (
                "hostPort",
                lambda o: container(o)["ports"][0].update(hostPort=8443),
                "host port",
            ),
            (
                "hostPath volume",
                lambda o: pod(o)["volumes"].append(
                    {"name": "h", "hostPath": {"path": "/"}}
                ),
                "hostPath",
            ),
            (
                "API token",
                lambda o: pod(o).update(automountServiceAccountToken=True),
                "Kubernetes API token",
            ),
            (
                "image by tag",
                lambda o: container(o).update(image="localhost:5001/ax-web:0.1.0"),
                "pinned images",
            ),
            (
                "writable root",
                lambda o: container(o)["securityContext"].update(
                    readOnlyRootFilesystem=False
                ),
                "read-only",
            ),
            (
                "a capability back",
                lambda o: container(o)["securityContext"]["capabilities"].update(
                    add=["NET_ADMIN"]
                ),
                "every capability",
            ),
            (
                "root",
                lambda o: pod(o)["securityContext"].update(runAsNonRoot=False),
                "non-root",
            ),
            (
                "baseline standard",
                lambda o: o[0]["metadata"]["labels"].update(
                    {"pod-security.kubernetes.io/enforce": "baseline"}
                ),
                "restricted",
            ),
            (
                "LoadBalancer",
                lambda o: o[4]["spec"].update(type="LoadBalancer"),
                "one NodePort",
            ),
            (
                "Cluster traffic policy",
                lambda o: o[4]["spec"].update(externalTrafficPolicy="Cluster"),
                "one NodePort",
            ),
            (
                "the probe port published",
                lambda o: o[4]["spec"]["ports"].append(
                    {"name": "health", "port": 8081, "nodePort": 30844}
                ),
                "one NodePort",
            ),
            (
                "another node port",
                lambda o: o[4]["spec"]["ports"][0].update(nodePort=30000),
                "one NodePort",
            ),
            (
                "egress anywhere",
                lambda o: o[5]["spec"]["egress"].append({"to": []}),
                "NetworkPolicy ax-web differs",
            ),
            (
                "ingress from pods",
                lambda o: o[5]["spec"]["ingress"][0]["from"][0]["ipBlock"].pop(
                    "except"
                ),
                "NetworkPolicy ax-web differs",
            ),
            (
                "the probe port open beyond the node",
                lambda o: o[5]["spec"]["ingress"][0]["ports"].append(
                    {"protocol": "TCP", "port": 8081}
                ),
                "NetworkPolicy ax-web differs",
            ),
            (
                "ax-server open to every namespace",
                lambda o: o[6]["spec"]["ingress"][0]["from"][0].pop(
                    "namespaceSelector"
                ),
                "ax-web-to-ax-server differs",
            ),
            (
                "a rendered Secret",
                lambda o: o.append(
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {"name": "ax-web-tls", "namespace": "ax-web"},
                    }
                ),
                "other objects",
            ),
            (
                "a Role",
                lambda o: o.insert(
                    2,
                    {
                        "apiVersion": "rbac.authorization.k8s.io/v1",
                        "kind": "Role",
                        "metadata": {"name": "ax-web", "namespace": "ax-web"},
                    },
                ),
                "other objects",
            ),
            (
                "another origin",
                lambda o: o[2]["data"].update(
                    {
                        "config.json": o[2]["data"]["config.json"].replace(
                            "https://ax.apptolast.com", "https://evil.example"
                        )
                    }
                ),
                "config.json differs",
            ),
            (
                "stale configuration annotation",
                lambda o: o[3]["spec"]["template"]["metadata"]["annotations"].update(
                    {"ax.apptolast.com/config-sha256": "0" * 64}
                ),
                "roll with its configuration",
            ),
            (
                "environment",
                lambda o: container(o).update(env=[{"name": "A", "value": "b"}]),
                "no command or environment",
            ),
            (
                "writable Secret mount",
                lambda o: container(o)["volumeMounts"][2].update(readOnly=False),
                "mount exactly",
            ),
            (
                "second replica",
                lambda o: o[3]["spec"].update(replicas=2),
                "one pod",
            ),
            (
                "short grace period",
                lambda o: pod(o).update(terminationGracePeriodSeconds=30),
                "120 s",
            ),
        ]
        self.assertGreaterEqual(len(cases), 10)
        for label, change, message in cases:
            with self.subTest(mutation=label):
                self.manifest_rejected(change, message)

    def test_config_mutations_are_rejected(self) -> None:
        def forwarder(**values):
            return lambda lab_: lab_["web"]["forwarder"].update(values)

        def web(**values):
            return lambda lab_: lab_["web"].update(values)

        cases = [
            ("node port outside the range", web(node_port=8443), "node_port"),
            ("another namespace", web(namespace="default"), "namespace"),
            ("another origin", web(origin="https://ax.example.com"), "origin"),
            ("no Observatorio window", web(blackout_utc="23:00-23:30"), "blackout"),
            ("too many turns", web(max_turns=51), "max_turns"),
            ("too long", web(max_timeout_minutes=60), "max_timeout"),
            ("an IP as repo host", web(repo_hosts=["10.0.0.1"]), "repo_hosts"),
            ("no repo host", web(repo_hosts=[]), "repo_hosts"),
            ("an unknown prompt mode", web(prompt_mode="shell"), "prompt_mode"),
            (
                "TLS material elsewhere",
                web(tls_directory="/root/web-tls"),
                "tls_directory",
            ),
            (
                "image by tag",
                lambda lab_: lab_["web"]["image"].update(digest="latest"),
                "sha256",
            ),
            (
                "floating tag",
                lambda lab_: lab_["web"]["image"].update(tag="latest"),
                "fixed image tag",
            ),
            ("renamed forwarder", forwarder(container="ax-web"), "ax-web-edge"),
            (
                "another edge network",
                forwarder(edge_network="apptolast-edge-n8n"),
                "apptolast-edge-ax",
            ),
            (
                "subnet inside the pod network",
                forwarder(edge_subnet="10.244.7.0/24"),
                "outside the kind pod",
            ),
            (
                "public subnet",
                forwarder(edge_subnet="8.8.8.0/24"),
                "private IPv4",
            ),
            ("wide subnet", forwarder(edge_subnet="10.0.0.0/8"), "private IPv4"),
            ("catch-all subnet", forwarder(edge_subnet="0.0.0.0/0"), "private IPv4"),
            ("restart always", forwarder(restart_policy="always"), "restart_policy"),
            (
                "limits outside the budget",
                lambda lab_: lab_["web"]["forwarder"]["resources"].update(
                    memory_limit_mib=64, memory_reservation_mib=32
                ),
                "resources differ from host_containers",
            ),
            (
                "a credential-shaped key",
                lambda lab_: lab_["web"].update(client_token="x"),
                "secret-like key",
            ),
            (
                "an unknown key",
                lambda lab_: lab_["web"].update(host_network=True),
                "unexpected or missing keys",
            ),
        ]
        self.assertGreaterEqual(len(cases), 10)
        for label, change, message in cases:
            with self.subTest(mutation=label):
                self.config_rejected(change, message)

    def test_the_cli_prints_the_manifest_digest(self) -> None:
        completed = subprocess.run(
            [
                str(ROOT / ".venv/bin/python"),
                str(ROOT / "scripts/validate-ax-lab.py"),
                "--web-manifest-sha256",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.strip(), VALIDATOR.web_manifest_sha256(self.text)
        )

    def test_the_forwarder_is_budgeted_like_the_lab_containers(self) -> None:
        profiles = yaml.safe_load(
            (ROOT / "config/capacity-profiles.yml").read_text(encoding="utf-8")
        )["capacity_profiles"]
        budget = profiles["host_containers"]["ax-lab"]["ax-web-edge"]
        self.assertEqual(
            budget,
            {
                "reservations": {"cpu_millicores": 10, "memory_mib": 16},
                "limits": {"cpu_millicores": 250, "memory_mib": 32},
                "pids_limit": 64,
            },
        )
        # What the plan leaves free is ate-setup's ceiling: 224 MiB and 600m.
        self.assertEqual(VALIDATOR.load_free_limit_budget(), (224, 600))
        install = self.lab["substrate"]["install"]
        self.assertEqual(
            (install["memory_limit_mib"], install["cpu_limit_millicores"]),
            (224, 500),
        )


# --------------------------------------------------------------------------
# Role


class WebRoleTests(unittest.TestCase):
    """The role's web tasks, exercised on synthetic reads."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.main = load_tasks(MAIN)
        cls.read = load_tasks(WEB_READ)
        cls.apply = load_tasks(WEB_APPLY)
        cls.examples = load_tasks(EXAMPLES)
        cls.variables = web_variables()

    def decisions(self) -> list[dict[str, Any]]:
        """main.yml's derivation, then every fact and gate of web_read.yml."""
        derive = self.main["Derive the pinned web panel image and install identity"]
        return [derive] + [
            task
            for task in self.read.values()
            if SIDE_EFFECT_FREE_MODULES.intersection(task)
        ]

    def run_reads(self, *probes: str, check: bool = False, **reads: Any):
        variables = {**self.variables, **web_reads(**reads)}
        tasks = self.decisions() + [probe(condition) for condition in probes]
        return run_tasks(tasks, variables, check=check)

    def assert_reads(self, *probes: str, check: bool = False, **reads: Any) -> None:
        completed = self.run_reads(*probes, check=check, **reads)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def assert_refused(self, message: str, **reads: Any) -> None:
        completed = self.run_reads(**reads)
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0, output)
        self.assertIn(message, " ".join(output.split()))

    # -- main.yml ----------------------------------------------------------

    def test_main_renders_exactly_the_manifest_the_validator_checked(self) -> None:
        digest = self.main[
            "Compute the digest of the reviewed web panel manifest locally"
        ]
        self.assertEqual(
            digest["ansible.builtin.command"]["argv"],
            [
                "{{ ansible_playbook_python }}",
                "{{ role_path }}/../../../scripts/validate-ax-lab.py",
                "--web-manifest-sha256",
            ],
        )
        for key, value in (
            ("delegate_to", "localhost"),
            ("become", False),
            ("changed_when", False),
            ("check_mode", False),
        ):
            self.assertEqual(digest[key], value)
        render = self.main["Render the web panel manifest the role applies"]
        require = self.main["Require the web panel manifest the validator checked"]
        variables = {
            key: value
            for key, value in self.variables.items()
            if key != "ax_lab_web_manifest"
        }
        completed = run_reviewed_tasks(
            [render, require, probe("ax_lab_web_manifest == expected")],
            {**variables, "expected": rendered(variables["ax_lab"])},
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        completed = run_reviewed_tasks(
            [render, require],
            {**variables, "ax_lab_web_manifest_digest": {"stdout": "0" * 64}},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("differs from the one the", completed.stdout)
        names = list(self.main)
        self.assertLess(
            names.index(require["name"]),
            names.index("Reconcile the lab host prerequisites"),
        )

    def test_main_wires_the_web_tasks_after_ax_in_both_modes(self) -> None:
        names = list(self.main)
        self.assertEqual(
            self.main["Deploy the web panel and its forwarder outside check mode"],
            {
                "name": "Deploy the web panel and its forwarder outside check mode",
                "ansible.builtin.import_tasks": "web.yml",
                "when": "not ansible_check_mode",
            },
        )
        self.assertEqual(
            names[-1], "Deploy the web panel and its forwarder outside check mode"
        )
        self.assertEqual(
            names.index("Deploy the web panel and its forwarder outside check mode"),
            names.index("Install AX only on drift and repair its workers outside check mode")
            + 1,
        )  # fmt: skip
        read = "Read the web panel and its forwarder and prove their ownership"
        self.assertEqual(
            self.main[read],
            {
                "name": read,
                "ansible.builtin.import_tasks": "web_read.yml",
                "when": "ansible_check_mode",
            },
        )
        self.assertLess(
            names.index("Report what an apply would change in AX"), names.index(read)
        )
        report = self.main["Report what an apply would change in the web panel"]
        self.assertEqual(
            report["ansible.builtin.debug"], {"msg": "{{ ax_lab_web_plan }}"}
        )
        self.assertEqual(
            names.index("Install the AX examples"),
            names.index("Install the AX CLI and the operator helpers") + 1,
        )

    def test_the_forwarder_argv_and_identity_derive_from_the_contract(self) -> None:
        derive = self.main["Derive the pinned web panel image and install identity"]
        completed = run_reviewed_tasks(
            [
                derive,
                probe("ax_lab_web_forwarder_command == expected_command"),
                probe("ax_lab_web_forwarder_image == expected_image"),
                probe("ax_lab_web_image_args == expected_args"),
                probe("ax_lab_web_identity == expected_identity"),
            ],
            {
                **self.variables,
                "expected_command": forwarder_argv(lab()),
                "expected_image": f"localhost:5001/ax-web@{PINNED_DIGEST}",
                "expected_args": [f"--image=ax-web={PINNED_DIGEST}"],
                "expected_identity": {
                    "schema_version": 1,
                    "image": PINNED_DIGEST,
                    "manifest_sha256": VALIDATOR.web_manifest_sha256(rendered()),
                },
            },
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    # -- web_read.yml ------------------------------------------------------

    def test_a_converged_panel_is_read_as_such_in_both_modes(self) -> None:
        for check in (False, True):
            with self.subTest(check=check):
                self.assert_reads(
                    "ax_lab_web_drift == []",
                    "ax_lab_web_secrets_missing == []",
                    "not ax_lab_web_edge_drift",
                    "ax_lab_web_network_ready",
                    "ax_lab_web_in_backup and ax_lab_web_in_registry",
                    "ax_lab_web_plan == ['the web panel matches its pinned install',"
                    " 'ax-web-edge runs as reviewed']",
                    check=check,
                )

    def test_the_namespace_is_never_adopted_or_swapped(self) -> None:
        self.assert_refused("is never adopted", state={})
        self.assert_refused(
            "is never adopted", state=web_state(node_container_id="c" * 64)
        )
        self.assert_refused("is never adopted", state=web_state(namespace="uid-other"))
        # A namespace deleted since the proof is drift, created again.
        self.assert_reads(
            "ax_lab_web_drift == ['ax-web.yaml']",
            "ax_lab_web_secrets_missing == ['ax-web-tls', 'ax-web-agent']",
            namespace=None,
        )
        # An interrupted first apply left `installing` without a UID.
        self.assert_reads(
            "ax_lab_web_drift == ['state']",
            state=web_state(phase="installing", namespace=""),
        )

    def test_drift_is_the_state_or_a_server_side_diff(self) -> None:
        self.assert_reads("ax_lab_web_drift == ['ax-web.yaml']", diff_rc=1)
        self.assert_reads("ax_lab_web_drift == ['state']", state=web_state(image="x"))
        # Nothing is read in --check while AX itself drifts.
        completed = run_tasks(
            self.decisions() + [probe("not ax_lab_web_readable"), probe("ax_lab_web_drift == []")],
            {**self.variables, **web_reads(), "ax_lab_ax_drift": ["ax-system.yaml"]},
            check=True,
        )  # fmt: skip
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_the_secrets_are_checked_by_metadata_only(self) -> None:
        task = self.read["Read the web panel Secrets by metadata only"]
        argv = task["ansible.builtin.command"]["argv"]
        self.assertEqual(
            argv[7:],
            [
                "get",
                "secrets",
                "ax-web-tls",
                "ax-web-agent",
                "--namespace",
                "{{ ax_lab.web.namespace }}",
                "--ignore-not-found",
                "--no-headers",
            ],
        )
        # No output format: the server-side table carries metadata only.
        self.assertFalse(
            [
                arg
                for arg in argv
                if str(arg).startswith(("-o", "--output", "--sort-by"))
            ]
        )
        for rows, missing in (
            (["ax-web-tls   Opaque   3   1m"], ["ax-web-agent"]),
            (["ax-web-agent   Opaque   1   1m"], ["ax-web-tls"]),
            ([], ["ax-web-tls", "ax-web-agent"]),
            (["ax-web-tls   Opaque   2   1m", "ax-web-agent   Opaque   1   1m"], ["ax-web-tls"]),
            (["ax-web-tls   kubernetes.io/tls   3   1m", "ax-web-agent   Opaque   1   1m"], ["ax-web-tls"]),
        ):  # fmt: skip
            with self.subTest(rows=rows):
                self.assert_reads(
                    f"ax_lab_web_secrets_missing == {missing!r}", secrets=rows
                )
        self.assert_reads(
            "ax_lab_web_plan | select('search', 'ax-web-bootstrap.sh k8s') | list"
            " | length > 0",
            secrets=[],
        )

    def test_forwarder_drift_is_every_reviewed_setting(self) -> None:
        cases = {
            "root user": {"user": "0"},
            "writable root": {"read_only": False},
            "privileged": {"privileged": True},
            "a capability back": {"cap_drop": []},
            "added capability": {"cap_add": ["NET_ADMIN"]},
            "no no-new-privileges": {"security_opt": []},
            "memory": {"memory": 64 * MIB},
            "swap": {"memory_swap": -1},
            "reservation": {"memory_reservation": 0},
            "CPU": {"nano_cpus": 0},
            "PIDs": {"pids_limit": None},
            "restart always": {"restart_policy": {"Name": "always", "MaximumRetryCount": 0}},
            "another image": {"image": "localhost:5001/ax-web:0.1.0"},
            "any peer": {"cmd": forwarder_argv(lab())[:-1] + ["0.0.0.0/1"]},
            "another entrypoint": {"entrypoint": ["/bin/sh"]},
            "host network": {"network_mode": "host"},
            "host PID namespace": {"pid_mode": "host"},
            "another container's PID namespace": {"pid_mode": "container:" + NODE_ID},
            "host IPC namespace": {"ipc_mode": "host"},
            "shareable IPC": {"ipc_mode": "shareable"},
            "host UTS namespace": {"uts_mode": "host"},
            "host user namespace": {"userns_mode": "host"},
            "host cgroup namespace": {"cgroupns_mode": "host"},
            "a device": {"devices": [{"PathOnHost": "/dev/fuse", "PathInContainer": "/dev/fuse", "CgroupPermissions": "rwm"}]},
            "an extra group": {"group_add": ["docker"]},
            "a tmpfs": {"tmpfs": {"/tmp": ""}},
            "a published port": {"port_bindings": {"8443/tcp": [{"HostPort": "8443"}]}},
            "a mount": {"mounts": [{"Type": "bind", "Source": "/"}]},
            "missing edge network": {"networks": {"kind": {"IPAddress": "172.23.0.9"}}},
            "an extra network": {
                "networks": {
                    "kind": {},
                    "apptolast-edge-ax": {},
                    "bridge": {},
                }
            },
        }  # fmt: skip
        for label, change in cases.items():
            with self.subTest(drift=label):
                self.assert_reads(
                    "ax_lab_web_edge_drift",
                    "ax_lab_web_plan[-1] == 'recreate ax-web-edge: it drifted'",
                    edge=forwarder_read(**change),
                )
        self.assert_reads(
            "not ax_lab_web_edge_drift",
            "ax_lab_web_plan[-1] == 'start ax-web-edge'",
            edge=forwarder_read(status="exited"),
        )
        # Docker records "none" as null or as an empty value.
        self.assert_reads(
            "not ax_lab_web_edge_drift",
            "ax_lab_web_plan[-1] == 'ax-web-edge runs as reviewed'",
            edge=forwarder_read(devices=None, group_add=[], tmpfs={}),
        )

    def test_the_forwarder_fixture_is_what_the_role_inspects(self) -> None:
        argv = self.read["Read the web forwarder container"]["ansible.builtin.command"]["argv"]  # fmt: skip
        self.assertEqual(argv[:4], ["/usr/bin/docker", "container", "inspect", "--format"])  # fmt: skip
        keys = re.findall(r'"([a-z_]+)":\{\{json ', argv[4])
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), set(forwarder_read()))
        for key, field in (
            ("pid_mode", ".HostConfig.PidMode"),
            ("ipc_mode", ".HostConfig.IpcMode"),
            ("uts_mode", ".HostConfig.UTSMode"),
            ("userns_mode", ".HostConfig.UsernsMode"),
            ("cgroupns_mode", ".HostConfig.CgroupnsMode"),
            ("devices", ".HostConfig.Devices"),
            ("group_add", ".HostConfig.GroupAdd"),
            ("tmpfs", ".HostConfig.Tmpfs"),
        ):
            with self.subTest(key=key):
                self.assertIn(f'"{key}":{{{{json {field}}}}}', argv[4])
        self.assert_reads(
            "ax_lab_web_edge is none",
            "ax_lab_web_plan[-1] == 'create ax-web-edge'",
            edge_absent=True,
        )

    def test_a_foreign_forwarder_is_never_touched(self) -> None:
        for labels in (None, {}, {"com.apptolast.managed-by": "ansible"}):
            with self.subTest(labels=labels):
                self.assert_refused(
                    "without this role's labels", edge=forwarder_read(labels=labels)
                )
        completed = self.run_reads(edge={"name": "/ax-web-edge-old"}, edge_absent=False)
        self.assertNotEqual(completed.returncode, 0)

    def test_the_edge_network_is_only_inspected(self) -> None:
        for label, reads in (
            ("missing", {"network_rc": 1}),
            ("not attachable", {"network": network_read(attachable=False)}),
            ("another subnet", {"network": network_read(ipam=[{"Subnet": "10.0.30.0/24"}])}),
            ("local bridge", {"network": network_read(driver="bridge", scope="local")}),
            ("two subnets", {"network": network_read(ipam=[{"Subnet": "10.0.250.0/24"}, {"Subnet": "10.0.251.0/24"}])}),
        ):  # fmt: skip
            with self.subTest(network=label):
                self.assert_reads(
                    "not ax_lab_web_network_ready",
                    "ax_lab_web_plan | select('search', 'the apply stops: network')"
                    " | list | length > 0",
                    **reads,
                )
        for path in role_task_files():
            for task in yaml.safe_load(path.read_text(encoding="utf-8")):
                argv = (task.get("ansible.builtin.command") or {}).get("argv")
                if isinstance(argv, list) and argv[:2] == [
                    "/usr/bin/docker",
                    "network",
                ]:
                    with self.subTest(task=task["name"]):
                        self.assertIn(argv[2], ("inspect", "connect"))

    def test_the_image_comes_only_from_the_backup_or_the_registry(self) -> None:
        self.assert_reads(
            "ax_lab_web_plan[0] == 'restore into the registry: ax-web'",
            registry="missing",
        )
        self.assert_reads(
            "ax_lab_web_plan[0] == 'back up from the registry: ax-web'",
            backup="missing",
        )
        self.assert_reads(
            "ax_lab_web_plan[0] is search('seed-layout')",
            backup="missing",
            registry="missing",
        )
        refuse = self.apply[
            "Refuse a web panel image held by neither backup nor registry"
        ]
        variables = {
            **self.variables,
            **web_reads(backup="missing", registry="missing"),
        }
        completed = run_tasks(self.decisions() + [refuse], variables)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("seed-layout", completed.stdout)

    # -- web.yml -----------------------------------------------------------

    def test_every_write_is_gated_and_the_manager_is_locked(self) -> None:
        writers = []
        for task in self.apply.values():
            command = task.get("ansible.builtin.command")
            if command is None:
                continue
            if task.get("changed_when") is not True:
                self.assertIs(task["changed_when"], False, task["name"])
                continue
            writers.append(task["name"])
            with self.subTest(task=task["name"]):
                self.assertIn("when", task)
                if "ax_lab_substrate_manager" in str(command["argv"]):
                    self.assertEqual(
                        task["environment"], "{{ operation_lock_guard_environment }}"
                    )
        self.assertEqual(
            writers,
            [
                "Back up the pinned web panel image the backup lacks",
                "Restore the pinned web panel image the registry lacks",
                "Apply the web panel manifest server-side only on drift",
                "Pull the pinned web panel image when the host lacks it",
                "Remove the web forwarder only when it drifted",
                "Create the web forwarder with its reviewed settings",
                "Connect the new web forwarder to the edge network",
                "Start the web forwarder when it is stopped",
            ],
        )
        names = list(self.apply)
        order = [
            "Require the edge network the forwarder joins",
            "Refuse a web panel image held by neither backup nor registry",
            "Record the web panel install intent",
            "Apply the web panel manifest server-side only on drift",
            "Record the web panel namespace this role created",
            "Stop until the owner creates the web panel Secrets",
            "Wait for the web panel to roll out",
            "Remove the web forwarder only when it drifted",
            "Create the web forwarder with its reviewed settings",
            "Connect the new web forwarder to the edge network",
            "Verify the web panel and its forwarder",
            "Record the proof of the installed web panel",
        ]
        self.assertEqual([name for name in names if name in order], order)

    def test_the_forwarder_runs_with_its_exact_host_config(self) -> None:
        create = self.apply["Create the web forwarder with its reviewed settings"]
        self.assertEqual(
            create["when"], "ax_lab_web_edge_before is none or ax_lab_web_edge_drift"
        )
        derive = self.main["Derive the pinned web panel image and install identity"]
        record = self.read["Record the web forwarder as read and as it must run"]
        render = {
            "name": "Render the reviewed docker run",
            "ansible.builtin.set_fact": {
                "rendered_argv": create["ansible.builtin.command"]["argv"]
            },
        }
        expected = [
            "/usr/bin/docker", "run", "--detach", "--pull", "never",
            "--name", "ax-web-edge",
            "--restart", "no",
            "--user", "65532:65532",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--memory", str(32 * MIB),
            "--memory-swap", str(32 * MIB),
            "--memory-reservation", str(16 * MIB),
            "--cpus", "0.250",
            "--pids-limit", "64",
            "--label", "com.apptolast.managed-by=ansible",
            "--label", "com.apptolast.ax-lab=web-edge",
            "--network", "kind",
            f"localhost:5001/ax-web@{PINNED_DIGEST}",
            *forwarder_argv(lab()),
        ]  # fmt: skip
        completed = run_reviewed_tasks(
            [
                derive,
                {
                    "name": "Stand in for the forwarder read",
                    "ansible.builtin.set_fact": {"ax_lab_web_edge_read": {"rc": 1}},
                },
                record,
                render,
                probe("rendered_argv == expected"),
            ],
            {**self.variables, "expected": expected},
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        for flag in ("--privileged", "--publish", "-p", "--volume", "-v", "--mount"):
            self.assertNotIn(flag, expected)
        connect = self.apply["Connect the new web forwarder to the edge network"]
        self.assertEqual(
            connect["ansible.builtin.command"]["argv"],
            [
                "/usr/bin/docker",
                "network",
                "connect",
                "{{ ax_lab.web.forwarder.edge_network }}",
                "{{ ax_lab.web.forwarder.container }}",
            ],
        )
        self.assertEqual(connect["when"], "ax_lab_web_edge_created is not skipped")
        remove = self.apply["Remove the web forwarder only when it drifted"]
        self.assertEqual(remove["when"], "ax_lab_web_edge_drift")
        pull = self.apply["Pull the pinned web panel image when the host lacks it"]
        self.assertEqual(
            pull["ansible.builtin.command"]["argv"][-1],
            "{{ ax_lab_web_forwarder_image }}",
        )

    def test_the_secrets_stop_the_apply_with_the_bootstrap_command(self) -> None:
        gate = self.apply["Stop until the owner creates the web panel Secrets"]
        variables = {**self.variables, **web_reads(secrets=[])}
        completed = run_tasks(self.decisions() + [gate], variables)
        output = " ".join((completed.stdout + completed.stderr).split())
        self.assertNotEqual(completed.returncode, 0, output)
        self.assertIn("sudo -- ./scripts/ax-web-bootstrap.sh k8s", output)
        completed = run_tasks(
            self.decisions() + [gate], {**self.variables, **web_reads()}
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)

    def test_the_apply_verifies_before_it_records(self) -> None:
        verify = self.apply["Verify the web panel and its forwarder"]
        for label, reads, passes in (
            ("converged", {}, True),
            ("still drifting", {"diff_rc": 1}, False),
            ("stopped", {"edge": forwarder_read(status="exited")}, False),
            ("absent", {"edge_absent": True}, False),
            ("off the edge subnet", {"edge": forwarder_read(networks={"kind": {"IPAddress": "172.23.0.9"}, "apptolast-edge-ax": {"IPAddress": "10.0.30.7"}})}, False),
            ("image lost", {"registry": "missing"}, False),
        ):  # fmt: skip
            with self.subTest(case=label):
                completed = run_tasks(
                    self.decisions() + [verify],
                    {**self.variables, **web_reads(**reads)},
                )
                self.assertEqual(completed.returncode == 0, passes, completed.stdout)
        record = self.apply["Record the proof of the installed web panel"]
        self.assertEqual(record["ansible.builtin.copy"]["mode"], "0600")
        self.assertEqual(record["when"], "ax_lab_web_drift | length > 0")

    def test_no_web_task_reads_a_credential(self) -> None:
        for path in (ROOT / WEB_READ, ROOT / WEB_APPLY, ROOT / EXAMPLES):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("/etc/dockerswarm", text)
                self.assertNotIn("credential_directory", text)
                self.assertNotIn("claude-oauth-token", text)
                self.assertNotIn("no_log", text)

    # -- examples.yml ------------------------------------------------------

    def test_the_examples_are_root_owned_and_world_readable(self) -> None:
        create = self.examples["Create the AX examples directories"]
        self.assertEqual(create["ansible.builtin.file"]["mode"], "0755")
        for name in (
            "Render the AX guide and example manifests",
            "Copy the remote AX clients",
        ):
            module = self.examples[name].get(
                "ansible.builtin.template"
            ) or self.examples[name].get("ansible.builtin.copy")
            with self.subTest(task=name):
                self.assertEqual(
                    (module["owner"], module["group"], module["mode"]),
                    ("root", "root", "0644"),
                )


# --------------------------------------------------------------------------
# Examples


class ExamplesTests(unittest.TestCase):
    """The owner's examples, rendered with the codified lab's paths and pins."""

    @classmethod
    def setUpClass(cls) -> None:
        environment = jinja2.Environment(
            loader=jinja2.FileSystemLoader(ROLE / "templates/ejemplos"),
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            keep_trailing_newline=True,
        )
        variables = role_variables()
        cls.rendered = {
            name: environment.get_template(name + ".j2").render(**variables)
            for name in (
                "LEEME.md",
                "01-tarea-sencilla.yaml",
                "02-workspace.yaml",
                "03-tarea-con-workspace.yaml",
            )
        }
        cls.lab = variables["ax_lab"]

    def test_manifests_use_the_pinned_agents_image_and_the_new_paths(self) -> None:
        image = (
            "localhost:5001/ax-agents:f009cc8-issue375@"
            + self.lab["ax"]["images"]["ax-agents"]
        )
        for name, text in self.rendered.items():
            with self.subTest(example=name):
                self.assertNotIn("/opt/ax-lab/", text)
                self.assertNotIn("f82e9848", text)
                self.assertNotIn("{{", text)
                if name.endswith(".yaml"):
                    document_ = yaml.safe_load(text)
                    self.assertEqual(document_["metadata"]["atespace"], "default")
                    self.assertIn(
                        "sudo ax apply -f /opt/dockerswarm/ax-lab/ejemplos/", text
                    )
                    if document_["kind"] == "Task":
                        self.assertEqual(document_["spec"]["image"], image)
        self.assertIn("https://ax.apptolast.com", self.rendered["LEEME.md"])
        self.assertIn("1500m de CPU y 1536Mi", self.rendered["LEEME.md"])

    def test_the_clients_are_the_owners_scripts_without_secrets(self) -> None:
        for name in ("ax-remoto", "ax-remoto.ps1"):
            data = (ROLE / "files/ejemplos/cliente" / name).read_bytes()
            with self.subTest(client=name):
                self.assertNotIn(b"\r\n", data)
                self.assertNotIn(b"/opt/ax-lab", data)
                self.assertNotIn(b"-----BEGIN", data)
                self.assertIn(b"sudo -n ax", data)


# --------------------------------------------------------------------------
# seed-layout


def write_layout(root: Path, image: dict[str, Any], *, listed: bool = True) -> None:
    """An OCI layout as ko writes it, with extra files ko leaves beside it."""
    (root / "blobs/sha256").mkdir(parents=True)
    for digest, data in {**image["blobs"], image["digest"]: image["manifest"]}.items():
        (root / "blobs/sha256" / digest.split(":")[1]).write_bytes(data)
    (root / "oci-layout").write_text('{"imageLayoutVersion": "1.0.0"}')
    manifests = (
        [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "size": len(image["manifest"]),
                "digest": image["digest"],
            }
        ]
        if listed
        else []
    )
    (root / "index.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": manifests,
            }
        )
    )
    (root / "DIGEST").write_text(image["digest"] + "\n")
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)


class SeedLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temporary)
        self.image = make_image("ax-web", layers=4)
        self.source = self.temporary / "artifact"
        self.backup = self.temporary / "images"

    def seed(self, digest: str | None = None) -> dict[str, str]:
        with (
            manager.Layout(self.source, strict=False) as source,
            manager.Layout(self.backup) as backup,
        ):
            return manager.seed_layout(
                source, backup, "0.1.0", {"ax-web": digest or self.image["digest"]}
            )

    def test_one_pinned_image_is_copied_and_verified(self) -> None:
        write_layout(self.source, self.image)
        other = make_image("unrelated", layers=1)
        for digest, data in other["blobs"].items():
            (self.source / "blobs/sha256" / digest.split(":")[1]).write_bytes(data)
        self.assertEqual(self.seed(), {"ax-web": "seeded"})
        with manager.Layout(self.backup) as backup:
            self.assertEqual(
                backup.status("ax-web", "0.1.0", self.image["digest"]), "complete"
            )
            names = sorted(
                path.name for path in (self.backup / "blobs/sha256").iterdir()
            )
        # Only the pinned image's blobs, root-only like the rest of the backup.
        self.assertEqual(
            names,
            sorted(
                d.split(":")[1] for d in [*self.image["blobs"], self.image["digest"]]
            ),
        )
        self.assertEqual(stat.S_IMODE(self.backup.stat().st_mode), 0o700)
        self.assertEqual(self.seed(), {"ax-web": "present"})

    def test_a_tampered_or_unlisted_source_is_refused(self) -> None:
        write_layout(self.source, self.image)
        layer = next(iter(self.image["blobs"]))
        (self.source / "blobs/sha256" / layer.split(":")[1]).write_bytes(b"evil")
        with self.assertRaisesRegex(
            manager.SubstrateError, "larger|corrupt|does not match"
        ):
            self.seed()
        with manager.Layout(self.backup) as backup:
            self.assertEqual(
                backup.status("ax-web", "0.1.0", self.image["digest"]), "missing"
            )
        self.assertEqual(
            [p.name for p in (self.backup / "blobs/sha256").iterdir() if p.name.startswith(".partial")],
            [],
        )  # fmt: skip
        shutil.rmtree(self.source)
        write_layout(self.source, self.image, listed=False)
        with self.assertRaisesRegex(manager.SubstrateError, "does not list"):
            self.seed()
        # index.json lists the pin, but not as the manifest it names.
        for label, change in (
            ("another type", {"mediaType": "application/vnd.docker.distribution.manifest.v2+json"}),
            ("another size", {"size": len(self.image["manifest"]) + 1}),
        ):  # fmt: skip
            with self.subTest(index=label):
                shutil.rmtree(self.source)
                write_layout(self.source, self.image)
                index = json.loads((self.source / "index.json").read_text())
                index["manifests"][0].update(change)
                (self.source / "index.json").write_text(json.dumps(index))
                with self.assertRaisesRegex(
                    manager.SubstrateError, "describes .* as another type"
                ):
                    self.seed()
        # A manifest whose bytes do not hash to the pin.
        shutil.rmtree(self.source)
        write_layout(self.source, self.image)
        fake = "sha256:" + hashlib.sha256(b"another").hexdigest()
        (self.source / "blobs/sha256" / fake.split(":")[1]).write_bytes(
            self.image["manifest"]
        )
        (self.source / "blobs/sha256" / fake.split(":")[1]).chmod(0o644)
        index = json.loads((self.source / "index.json").read_text())
        index["manifests"][0]["digest"] = fake
        (self.source / "index.json").write_text(json.dumps(index))
        with self.assertRaisesRegex(manager.SubstrateError, "do not hash"):
            self.seed(fake)

    def test_links_and_writable_sources_are_refused(self) -> None:
        write_layout(self.source, self.image)
        layer = next(iter(self.image["blobs"]))
        target = self.source / "blobs/sha256" / layer.split(":")[1]
        moved = self.temporary / "moved"
        target.rename(moved)
        target.symlink_to(moved)
        with self.assertRaisesRegex(manager.SubstrateError, "cannot open safely"):
            self.seed()
        target.unlink()
        moved.rename(target)
        target.chmod(0o666)
        with self.assertRaisesRegex(manager.SubstrateError, "writable by others"):
            self.seed()

    def test_the_cli_seeds_only_the_web_image_under_the_lock(self) -> None:
        write_layout(self.source, self.image)
        argv = [
            "seed-layout",
            "--source",
            str(self.source),
            "--layout",
            str(self.backup),
            "--tag",
            "0.1.0",
            "--image",
            f"ax-web={self.image['digest']}",
        ]
        with mock.patch.object(manager, "ensure_host_lock") as lock:
            with mock.patch("sys.stdout") as stdout:
                self.assertEqual(manager.main(argv), 0)
        lock.assert_called_once()
        self.assertEqual(lock.call_args.args[0], "ax-lab-substrate-seed-layout")
        printed = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertEqual(json.loads(printed), {"ax-web": "seeded"})
        for bad in (
            [*argv[:-1], f"ax-server={self.image['digest']}"],
            [*argv[:4], str(self.source), *argv[5:]],
        ):
            with self.subTest(argv=bad):
                with mock.patch.object(manager, "ensure_host_lock"):
                    with mock.patch("sys.stderr"):
                        self.assertEqual(manager.main(bad), 1)
        self.assertEqual(manager.IMAGE_SETS["web"][1], ("ax-web",))


# --------------------------------------------------------------------------
# Bootstrap


FAKE_DOCKER = r"""#!/usr/bin/python3
import json, os, sys
log = os.environ["FAKE_LOG"]
existing = os.environ.get("FAKE_EXISTING", "").split(",")
argv = sys.argv[1:]
stdin = sys.stdin.buffer.read() if argv[:2] == ["secret", "create"] and argv[-1] == "-" else b""
with open(log, "a") as handle:
    handle.write(json.dumps({"argv": argv, "stdin": stdin.decode()}) + "\n")
if argv[:2] == ["secret", "inspect"]:
    sys.exit(0 if argv[2] in existing else 1)
sys.exit(0)
"""

FAKE_KUBECTL = r"""#!/usr/bin/python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as handle:
    handle.write(json.dumps({"kubectl": argv}) + "\n")
if "namespace" in argv and "get" in argv:
    print(os.environ.get("FAKE_NAMESPACE_LABEL", ""), end="")
if "secret" in argv and "get" in argv:
    name = argv[argv.index("secret") + 1]
    if name in os.environ.get("FAKE_EXISTING", "").split(","):
        print("secret/" + name)
sys.exit(0)
"""

FAKE_LOCK = r"""#!/usr/bin/python3
import sys
sys.exit(0 if sys.argv[1] == "prove" else 3)
"""


class BootstrapTests(unittest.TestCase):
    """scripts/ax-web-bootstrap.sh against fake docker and kubectl."""

    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temporary)
        root = self.temporary
        (root / "scripts").mkdir()
        (root / "bin").mkdir()
        (root / "run").mkdir(mode=0o700)
        self.credentials = root / "etc/ax"
        self.credentials.mkdir(parents=True, mode=0o700)
        self.lab_home = root / "lab/home/.kube"
        self.lab_home.mkdir(parents=True)
        (self.lab_home / "config").write_text("kubeconfig\n")
        (self.lab_home / "config").chmod(0o600)
        uid = os.getuid()
        # The reviewed script with this user and this directory in place of
        # root and the host paths; nothing else changes.
        text = BOOTSTRAP.read_text(encoding="utf-8")
        for old, new in (
            ("/etc/dockerswarm/ax", str(self.credentials)),
            ("/opt/dockerswarm/ax-lab/bin/kubectl", str(root / "bin/kubectl")),
            ("/opt/dockerswarm/ax-lab/home", str(root / "lab/home")),
            ("mktemp -d /run/ax-web-bootstrap", f"mktemp -d {root}/run/ax-web-bootstrap"),
            ('== "0:0 ${mode}"', f'== "{uid}:{os.getgid()} ${{mode}}"'),
            ("((EUID == 0))", f"((EUID == {uid}))"),
            ("-o root -g root", ""),
        ):  # fmt: skip
            self.assertIn(old, text)
            text = text.replace(old, new)
        self.script = root / "scripts/ax-web-bootstrap.sh"
        self.script.write_text(text)
        self.script.chmod(0o755)
        lock = root / "scripts/host_global_operation_lock.py"
        lock.write_text(FAKE_LOCK)
        for name, body in (("docker", FAKE_DOCKER), ("kubectl", FAKE_KUBECTL)):
            (root / "bin" / name).write_text(body)
            (root / "bin" / name).chmod(0o755)
        self.log = root / "calls.jsonl"
        self.environment = {
            "PATH": f"{root / 'bin'}:/usr/bin:/bin",
            "FAKE_LOG": str(self.log),
            "DOCKERSWARM_IAC_LOCK_SCOPE": "direct",
            "LC_ALL": "C",
        }

    def run_script(self, *argv: str, **environment: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.script), *argv],
            env={**self.environment, **environment},
            text=True,
            capture_output=True,
            check=False,
        )

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def openssl(self, *argv: str) -> str:
        return subprocess.run(
            ["openssl", *argv], text=True, capture_output=True, check=True
        ).stdout

    def test_init_issues_the_ca_and_both_leaves_and_never_prints_a_key(self) -> None:
        completed = self.run_script("init")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = completed.stdout + completed.stderr
        self.assertNotIn("PRIVATE KEY", output)
        tls = self.credentials / "web-tls"
        self.assertEqual(
            sorted(p.name for p in tls.iterdir()),
            ["client-ca.crt", "tls.crt", "tls.key"],
        )
        self.assertEqual(stat.S_IMODE(tls.stat().st_mode), 0o700)
        for path in tls.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        server = self.openssl(
            "x509",
            "-in",
            str(tls / "tls.crt"),
            "-noout",
            "-text",
            "-ext",
            "subjectAltName,extendedKeyUsage",
        )
        self.assertIn("DNS:ax-web", server)
        self.assertIn("TLS Web Server Authentication", server)
        self.assertIn("prime256v1", server)
        # No CA key anywhere, and the tmpfs work directory is gone.
        self.assertEqual(list((self.temporary / "run").iterdir()), [])
        self.assertEqual(
            [p.name for p in self.credentials.rglob("*") if "ca.key" in p.name], []
        )
        creates = [
            c for c in self.calls() if c.get("argv", [])[:2] == ["secret", "create"]
        ]
        self.assertEqual(
            [c["argv"][-2:] if c["argv"][-1] == "-" else c["argv"][-2:-1] for c in creates],
            [["edge-ax-upstream-client-v1", "-"], ["edge-ax-upstream-ca-v1"]],
        )  # fmt: skip
        for create in creates:
            self.assertEqual(
                create["argv"][2:6],
                [
                    "--label",
                    "com.apptolast.managed-by=manual-bootstrap",
                    "--label",
                    "com.apptolast.purpose=traefik-upstream-mtls",
                ],
            )
        # The client PEM goes on stdin, certificate and key, never in argv.
        bundle = creates[0]["stdin"]
        self.assertIn("BEGIN CERTIFICATE", bundle)
        self.assertIn("PRIVATE KEY", bundle)
        self.assertNotIn("PRIVATE KEY", json.dumps([c["argv"] for c in creates]))
        client = self.temporary / "client.pem"
        client.write_text(bundle)
        text = self.openssl(
            "x509", "-in", str(client), "-noout", "-subject", "-ext", "extendedKeyUsage"
        )
        self.assertIn("CN=edge-traefik", text.replace(" ", ""))
        self.assertIn("TLS Web Client Authentication", text)
        subprocess.run(
            ["openssl", "verify", "-CAfile", str(tls / "client-ca.crt"), "-purpose", "sslclient", str(client)],
            check=True, capture_output=True,
        )  # fmt: skip

    def test_init_refuses_existing_secrets_or_material(self) -> None:
        completed = self.run_script("init", FAKE_EXISTING="edge-ax-upstream-ca-v1")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("is never replaced", completed.stderr)
        self.assertFalse((self.credentials / "web-tls").exists())
        self.assertFalse(
            [c for c in self.calls() if c.get("argv", [])[:2] == ["secret", "create"]]
        )
        (self.credentials / "web-tls").mkdir(mode=0o700)
        completed = self.run_script("init")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("already exists", completed.stderr)

    def test_k8s_creates_the_secrets_from_files_only(self) -> None:
        self.assertEqual(self.run_script("init").returncode, 0)
        agent = self.credentials / "claude-oauth-token"
        agent.write_text("not-a-real-value\n")
        agent.chmod(0o600)
        (self.temporary / "bin/kubectl").chmod(0o755)
        completed = self.run_script("k8s")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("apply the ax-lab playbook first", completed.stderr)
        completed = self.run_script("k8s", FAKE_NAMESPACE_LABEL="ansible")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        creates = [
            c["kubectl"] for c in self.calls() if "create" in c.get("kubectl", [])
        ]
        self.assertEqual(len(creates), 2)
        joined = json.dumps(creates)
        self.assertIn(f"tls.crt={self.credentials}/web-tls/tls.crt", joined)
        self.assertIn(f"claude-oauth-token={agent}", joined)
        self.assertNotIn(
            "not-a-real-value", joined + completed.stdout + completed.stderr
        )
        for argv in creates:
            self.assertEqual(argv[:6], ["--kubeconfig", str(self.lab_home / "config"), "--context", "kind-kind", "--request-timeout", "10s"])  # fmt: skip
            self.assertTrue(
                all(a.startswith("--from-file=") for a in argv if "=" in a and "/" in a)
            )
        completed = self.run_script(
            "k8s", FAKE_NAMESPACE_LABEL="ansible", FAKE_EXISTING="ax-web-agent"
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("is never replaced", completed.stderr)

    def test_usage_and_root(self) -> None:
        for argv in ((), ("init", "k8s"), ("rotate",)):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_script(*argv).returncode, 64)

    def test_the_script_pins_the_contract(self) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        web = lab()["web"]
        for constant, value in (
            ("CREDENTIAL_DIRECTORY", lab()["credential_directory"]),
            ("TLS_DIRECTORY", web["tls_directory"]),
            ("SERVER_NAME", web["server_name"]),
            ("CLIENT_COMMON_NAME", web["client_common_name"]),
            ("NAMESPACE", web["namespace"]),
            ("KUBECTL", lab()["install_root"] + "/bin/kubectl"),
            ("KUBECONFIG_PATH", lab()["install_root"] + "/home/.kube/config"),
            ("KUBE_CONTEXT", "kind-" + lab()["cluster"]["name"]),
        ):
            with self.subTest(constant=constant):
                self.assertIn(f"\nreadonly {constant}={value}\n", text)
        self.assertIn('run --operation "${operation}"', text)
        self.assertIn("/usr/bin/python3", text)
        self.assertTrue(os.access(BOOTSTRAP, os.X_OK))

    # scripts/lint.sh runs its pinned image over scripts/*.sh; this is the
    # same check where a local shellcheck exists (the host has none).
    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_the_script_passes_shellcheck(self) -> None:
        completed = subprocess.run(
            ["shellcheck", "--severity=style", str(BOOTSTRAP)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)


# --------------------------------------------------------------------------
# Workflow, guard and docs


class WorkflowTests(unittest.TestCase):
    def test_the_build_must_equal_the_pin_before_it_is_kept(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(PIN_PROGRAM, text)
        completed = subprocess.run(
            ["awk", PIN_PROGRAM, str(CONFIG)],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(completed.stdout, lab()["web"]["image"]["digest"] + "\n")
        workflow = yaml.safe_load(text)
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        steps = [step["name"] for step in workflow["jobs"]["build"]["steps"]]
        self.assertLess(
            steps.index("Require the digest config/ax-lab.yml pins"),
            steps.index("Keep the OCI layout of the image"),
        )
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
        for event in ("pull_request", "push"):
            self.assertIn("config/ax-lab.yml", workflow["on"][event]["paths"])

    def test_the_guard_covers_the_bootstrap(self) -> None:
        guard = (ROOT / ".github/workflows/guard-sensitive-paths.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("\n          ^scripts/ax-web-bootstrap\\.sh$\n", guard)


class DocsTests(unittest.TestCase):
    def test_the_runbook_records_the_window_and_the_subnet(self) -> None:
        docs = (ROOT / "docs/AX_WEB.md").read_text(encoding="utf-8")
        text = " ".join(docs.replace("\\\n", " ").split())
        for fragment in (
            "10.0.250.0/24",
            "sudo -- ./scripts/ax-web-bootstrap.sh init",
            "sudo -- ./scripts/ax-web-bootstrap.sh k8s",
            "seed-layout",
            PINNED_DIGEST,
            "alert certificate required",
            "30843",
            "ax-web-edge",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)
        ax = (ROOT / "docs/AX.md").read_text(encoding="utf-8")
        self.assertIn("docs/AX_WEB.md", ax.replace("(AX_WEB.md)", "docs/AX_WEB.md"))
        self.assertNotIn("Nada del laboratorio se publica a\nInternet", ax)
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        for fragment in ("ax-web-bootstrap.sh", "seed-layout", "ax-web-edge"):
            self.assertIn(fragment, changelog)

    def test_the_rollback_and_the_teardown_leave_nothing_of_the_panel(self) -> None:
        docs = (ROOT / "docs/AX_WEB.md").read_text(encoding="utf-8")
        rollback = docs.split("## Marcha atrás", 1)[1].split("\n## ", 1)[0]
        rollback = " ".join(rollback.replace("\\\n", " ").split())
        self.assertIn("delete namespace ax-web", rollback)
        self.assertIn("docker rm --force ax-web-edge", rollback)
        # Every object the manifest renders outside the panel's namespace.
        for item in objects(rendered()):
            namespace = item["metadata"].get("namespace")
            if item["kind"] == "Namespace" or namespace == "ax-web":
                continue
            with self.subTest(object=item["metadata"]["name"]):
                self.assertIn(
                    f"--namespace {namespace} delete {item['kind'].lower()}"
                    f" {item['metadata']['name']}",
                    rollback,
                )
        ax = (ROOT / "docs/AX.md").read_text(encoding="utf-8")
        teardown = ax.split("### Si se descarta el laboratorio", 1)[1]
        teardown = " ".join(teardown.split("\n## ", 1)[0].split())
        self.assertLess(
            teardown.index("docker rm --force ax-web-edge"),
            teardown.index("docker network rm kind"),
        )
        for fragment in (
            "localhost:5001/ax-web@<digest>",
            "/etc/dockerswarm/ax/web-tls",
            "edge-ax-upstream-client-v1",
            "edge-ax-upstream-ca-v1",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, teardown)

    def test_the_docs_name_who_else_can_read_the_token(self) -> None:
        for path in ("docs/AX.md", "docs/AX_WEB.md"):
            with self.subTest(path=path):
                text = (ROOT / path).read_text(encoding="utf-8")
                self.assertIn("`ate-controller`", text)
