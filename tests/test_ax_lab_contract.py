"""Contract of the AX lab playbook `ax-lab`: host prerequisites and cluster."""

from __future__ import annotations

import ast
import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from ansible_task_harness import (
    ANSIBLE_PLAYBOOK,
    SIDE_EFFECT_FREE_MODULES,
    AnsibleTaskAssertions,
    run_task_definition,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/ax-lab.yml"
PLAYBOOK = ROOT / "ansible/playbooks/ax-lab.yml"
ROLE = ROOT / "ansible/roles/ax_lab"
MAIN = "ansible/roles/ax_lab/tasks/main.yml"
HOST = "ansible/roles/ax_lab/tasks/host.yml"
INSPECT = "ansible/roles/ax_lab/tasks/inspect.yml"
READ = "ansible/roles/ax_lab/tasks/read.yml"
OWNERSHIP = "ansible/roles/ax_lab/tasks/ownership.yml"
CLUSTER = "ansible/roles/ax_lab/tasks/cluster.yml"
NODE = "ansible/roles/ax_lab/tasks/node.yml"
ARTIFACTS = "ansible/roles/ax_lab/tasks/artifacts.yml"
IMAGES = "ansible/roles/ax_lab/tasks/images.yml"
IMAGES_READ = "ansible/roles/ax_lab/tasks/images_read.yml"
SUBSTRATE = "ansible/roles/ax_lab/tasks/substrate.yml"
SUBSTRATE_READ = "ansible/roles/ax_lab/tasks/substrate_read.yml"
MANAGER = "scripts/manage-ax-lab-substrate.py"
# The files main.yml imports in --check too: every task in them must be
# read-only there.
CHECK_MODE_FILES = (MAIN, HOST, INSPECT, READ, OWNERSHIP, IMAGES_READ, SUBSTRATE_READ)
SYSCTL_PATH = "/etc/sysctl.d/99-z-dockerswarm-ax-lab.conf"
WATCHES = "fs.inotify.max_user_watches"
INSTANCES = "fs.inotify.max_user_instances"


def load_script(name: str, relative_path: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_tasks(relative_path: str) -> dict[str, dict[str, Any]]:
    tasks = yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))
    return {task["name"]: task for task in tasks}


def role_task_files() -> list[Path]:
    return sorted((ROLE / "tasks").glob("*.yml"))


def playbook_components() -> dict[str, list[str]]:
    """PLAYBOOK_COMPONENTS of the metadata validator, which parses argv."""
    source = (ROOT / "scripts/validate-deployment-metadata.py").read_text(
        encoding="utf-8"
    )
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", None) == "PLAYBOOK_COMPONENTS"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("PLAYBOOK_COMPONENTS not found")


def role_variables() -> dict[str, Any]:
    """The variables the role sees from the playbook's vars_files."""
    document = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    lab = document["ax_lab"]
    return {
        "ax_lab": lab,
        "ax_lab_privileged_node_accepted": document["ax_lab_privileged_node_accepted"],
        "ax_lab_sysctl_path": SYSCTL_PATH,
        "ax_lab_bin_directory": lab["install_root"] + "/bin",
        "ax_lab_architecture": "x86_64",
        "ax_lab_kind_config_path": lab["install_root"] + "/kind-config.yaml",
        "ax_lab_state_directory": lab["install_root"] + "/state",
        "ax_lab_cluster_state_path": lab["install_root"] + "/state/cluster.json",
        "ax_lab_home_directory": lab["install_root"] + "/home",
        "ax_lab_kubeconfig_path": lab["install_root"] + "/home/.kube/config",
        "ax_lab_substrate_fallback_builds": document[
            "ax_lab_substrate_fallback_builds"
        ],
        "ax_lab_substrate_source_path": lab["install_root"] + "/src/substrate",
        "ax_lab_cache_directory": lab["install_root"] + "/cache",
        "ax_lab_substrate_state_path": lab["install_root"] + "/state/substrate.json",
        "ax_lab_substrate_manager": (
            lab["install_root"] + "/bin/manage-ax-lab-substrate.py"
        ),
        "platform_install_root": "/opt/dockerswarm",
        "role_path": str(ROLE),
    }


def run_reviewed_tasks(
    tasks: list[dict[str, Any]], variables: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    """Run reviewed side-effect-free tasks in order in one disposable play.

    Like ansible_task_harness.run_task_definition, but for a sequence: the
    facts one reviewed set_fact records feed the next task, exactly as in
    the role. Every task must be an assert or a set_fact.
    """
    for task in tasks:
        if len(SIDE_EFFECT_FREE_MODULES.intersection(task)) != 1:
            raise AssertionError(f"{task.get('name')!r} is not side-effect free")
    with tempfile.TemporaryDirectory() as temporary:
        playbook = Path(temporary) / "tasks.yml"
        playbook.write_text(
            yaml.safe_dump(
                [
                    {
                        "name": "Exercise reviewed tasks in order",
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
            [str(ANSIBLE_PLAYBOOK), "-i", "localhost,", str(playbook)],
            cwd=ROOT / "ansible",
            text=True,
            capture_output=True,
            check=False,
        )


def probe(condition: str) -> dict[str, Any]:
    """A final assert over the facts the reviewed tasks recorded."""
    return {
        "name": "Probe the recorded facts",
        "ansible.builtin.assert": {"that": [condition], "quiet": True},
    }


def cluster_variables() -> dict[str, Any]:
    """The role's variables plus the controller digest of the kind config."""
    validator = load_script("validate_ax_lab_digest", "scripts/validate-ax-lab.py")
    lab = role_variables()["ax_lab"]
    digest = validator.kind_config_sha256(validator.render_kind_config(lab))
    return {**role_variables(), "ax_lab_kind_config_digest": {"stdout": digest}}


def lab_containers() -> list[dict[str, Any]]:
    """ax_lab_containers as inspect.yml derives it: registry, then node."""
    lab = role_variables()["ax_lab"]
    containers = []
    for name, owner in (
        (lab["registry"]["container"], "registry"),
        (lab["cluster"]["node_container"], "cluster"),
    ):
        resources = lab[owner]["resources"]
        containers.append(
            {
                "name": name,
                "resources": resources,
                "limits": {
                    "memory": resources["memory_limit_mib"] * 1048576,
                    "memory_swap": resources["memory_limit_mib"] * 1048576,
                    "memory_reservation": resources["memory_reservation_mib"] * 1048576,
                    "nano_cpus": resources["cpu_limit_millicores"] * 1_000_000,
                    "pids_limit": resources["pids_limit"],
                    "restart_policy": {"Name": "no", "MaximumRetryCount": 0},
                },
            }
        )
    return containers


def container_read(
    container: str,
    *,
    rc: int = 0,
    stderr: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """One loop result of read.yml's `docker container inspect` of a lab container.

    The default is the converged container as the role leaves it.
    """
    lab = role_variables()["ax_lab"]
    node = container == lab["cluster"]["node_container"]
    resources = lab["cluster" if node else "registry"]["resources"]
    document = {
        "id": ("b" if node else "c") * 64,
        "name": "/" + container,
        "status": "running",
        "image": lab["images"]["kind_node" if node else "registry"],
        "labels": (
            {"io.x-k8s.kind.cluster": "kind", "io.x-k8s.kind.role": "control-plane"}
            if node
            else {
                "com.apptolast.managed-by": "ansible",
                "com.apptolast.ax-lab": "registry",
                # registry:3 carries image labels of its own.
                "org.opencontainers.image.source": "https://github.com/distribution",
            }
        ),
        "memory": resources["memory_limit_mib"] * 1048576,
        "memory_swap": resources["memory_limit_mib"] * 1048576,
        "memory_reservation": resources["memory_reservation_mib"] * 1048576,
        "nano_cpus": resources["cpu_limit_millicores"] * 1_000_000,
        "pids_limit": resources["pids_limit"],
        "restart_policy": {"Name": "no", "MaximumRetryCount": 0},
        "port_bindings": (
            {"6443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "6443"}]}
            if node
            else {
                "5000/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": "5001"},
                    {"HostIp": "::1", "HostPort": "5001"},
                ]
            }
        ),
        "networks": {"kind": {"IPAddress": "172.18.0.2" if node else "172.18.0.3"}},
        "mounts": (
            [{"Type": "volume", "Name": "d" * 64, "Destination": "/var"}]
            if node
            else [
                {
                    "Type": "volume",
                    "Name": "ax-lab-registry",
                    "Destination": "/var/lib/registry",
                }
            ]
        ),
    }
    document.update(fields)
    item = next(
        (entry for entry in lab_containers() if entry["name"] == container),
        {"name": container},
    )
    return {
        "item": item,
        "rc": rc,
        "stdout": json.dumps(document) if rc == 0 else "",
        "stderr": stderr,
    }


def container_absent(name: str) -> dict[str, Any]:
    """Docker 29.6.2's read of a missing container (as capacity_preflight)."""
    return container_read(
        name, rc=1, stderr=f"Error response from daemon: No such container: {name}"
    )


# Substrate's kind configuration at the pinned commit, exactly as
# hack/create-kind-cluster.sh writes it (lines 88-126) for IP_FAMILY=ipv4 on a
# host without /dev/kvm. It equals the live /opt/ax-lab/substrate/bin/
# kind-config.yaml of the manual lab.
SUBSTRATE_KIND_CONFIG = """\
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
# cmd/podcertcontroller depends on ClusterTrustBundle & PodCertificateRequest.
# They are not enabled by default as of Kubernetes v1.36
# https://github.com/kubernetes/kubernetes/blob/master/test/compatibility_lifecycle/reference/versioned_feature_list.yaml
featureGates:
  ClusterTrustBundle: true
  ClusterTrustBundleProjection: true
  PodCertificateRequest: true
runtimeConfig:
  "certificates.k8s.io/v1beta1": "true"
networking:
  ipFamily: ipv4
# The install pulls ~570MB of third-party images (postgres, prometheus, the otel
# collector, rustfs, envoy, jaeger) onto this one node. kubelet serializes image
# pulls by default, so they queue behind one another and whichever workload draws
# the back of the queue can miss its readiness deadline.
# TODO: kind should probably default to parallel pulls for its single-node
# clusters rather than leaving every user to patch it in.
kubeadmConfigPatches:
- |
  kind: KubeletConfiguration
  serializeImagePulls: false
  maxParallelImagePulls: 4
"""


class AxLabRegistrationTests(unittest.TestCase):
    """The playbook is registered everywhere a playbook is enumerated."""

    def setUp(self) -> None:
        self.wrapper = (ROOT / "scripts/deploy-ansible.sh").read_text(encoding="utf-8")
        self.mapping = yaml.safe_load(
            (ROOT / "ansible/roles/deployment_metadata/defaults/main.yml").read_text(
                encoding="utf-8"
            )
        )["deployment_metadata_components_by_playbook"]

    def test_wrapper_allowlist_and_usage_name_the_playbook(self) -> None:
        case = re.search(
            r"^  ([a-z|-]+)\)\n    ;;\n  \*\)\n"
            r'    fail "--playbook is missing or invalid"',
            self.wrapper,
            re.MULTILINE,
        )
        self.assertIsNotNone(case)
        allowed = case.group(1).split("|")
        self.assertIn("ax-lab", allowed)
        self.assertEqual(set(allowed), set(self.mapping))
        usage = self.wrapper.split("usage() {", 1)[1].split("\nEOF\n", 1)[0]
        self.assertIn("autoupdater, ax-lab, backup, site.", " ".join(usage.split()))

    def test_lock_metadata_accepts_only_the_versioned_identity(self) -> None:
        lock = load_script("ax_lab_operation_lock", "scripts/ansible-operation-lock.py")
        self.assertIn("ax-lab", lock.SAFE_PLAYBOOKS)
        self.assertEqual(lock.SAFE_PLAYBOOKS - {"bootstrap-host"}, set(self.mapping))
        for mode in ("check", "apply"):
            lock.require_metadata(
                "a" * 64,
                "b" * 40,
                "c" * 64,
                "ax-lab",
                "production",
                mode,
                "review-controller",
            )
        for playbook, profile in (
            ("ax-lab-extra", "production"),
            ("ax_lab", "production"),
            ("ax-lab", "acme-staging"),
            ("ax-lab", "fresh-host"),
        ):
            with (
                self.subTest(playbook=playbook, profile=profile),
                self.assertRaises(lock.OperationLockError),
            ):
                lock.require_metadata(
                    "a" * 64,
                    "b" * 40,
                    "c" * 64,
                    playbook,
                    profile,
                    "check",
                    "review-controller",
                )

    def test_contract_hash_lists_are_identical_and_hash_the_lab(self) -> None:
        block = self.wrapper.split('contract_sha256="$(', 1)[1].split("done", 1)[0]
        wrapper_paths = re.findall(r'"\$\{PROJECT_DIR\}/([^"]+)"', block)
        validator = (ROOT / "scripts/validate-deployment-metadata.py").read_text(
            encoding="utf-8"
        )
        listing = validator.split("contract_paths = [", 1)[1].split("]", 1)[0]
        validator_paths = re.findall(r'PROJECT_DIR / "([^"]+)"', listing)
        self.assertEqual(wrapper_paths, validator_paths)
        self.assertIn("config/ax-lab.yml", wrapper_paths)
        self.assertEqual(len(wrapper_paths), len(set(wrapper_paths)))
        for relative_path in wrapper_paths:
            with self.subTest(path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())

    def test_deployment_metadata_records_the_lab_component(self) -> None:
        self.assertEqual(self.mapping["ax-lab"], ["ax-lab"])
        self.assertNotIn("ax-lab", self.mapping["site"])
        self.assertEqual(playbook_components(), self.mapping)
        tasks = load_tasks("ansible/roles/deployment_metadata/tasks/main.yml")
        that = tasks["Verify immutable deployment provenance"][
            "ansible.builtin.assert"
        ]["that"]
        allowlist = next(
            item
            for item in that
            if item.startswith("deployment_metadata_playbook in [")
        )
        self.assertEqual(set(re.findall(r'"([a-z-]+)"', allowlist)), set(self.mapping))

    def test_playbook_takes_the_lock_then_the_capacity_preflight(self) -> None:
        play = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
        self.assertEqual(len(play), 1)
        play = play[0]
        self.assertEqual(play["hosts"], "swarm_managers")
        self.assertIs(play["become"], True)
        self.assertIs(play["gather_facts"], False)
        self.assertEqual(play["serial"], 1)
        self.assertIs(play["any_errors_fatal"], True)
        # capacity_preflight reads capacity_contract from config/capacity.yml.
        self.assertEqual(
            play["vars_files"],
            [
                "../../config/capacity.yml",
                "../../config/platform.yml",
                "../../config/ax-lab.yml",
                "../group_vars/all.yml",
            ],
        )
        self.assertEqual(
            [item["role"] for item in play["roles"]],
            [
                "operation_lock_guard",
                "capacity_preflight",
                "ax_lab",
                "deployment_metadata",
            ],
        )
        self.assertEqual(play["roles"][1]["tags"], ["capacity"])

    def test_static_gate_lints_the_config_and_runs_the_validator(self) -> None:
        gate = (ROOT / "scripts/validate-iac.sh").read_text(encoding="utf-8")
        yamllint = gate.split("yamllint \\\n", 1)[1].split("ansible-lint ansible", 1)[0]
        self.assertIn("  config/ax-lab.yml \\\n", yamllint)
        call = (
            '"${VENV_DIR}/bin/python" scripts/validate-ax-lab.py \\\n'
            "  --output .build/ax-lab/99-z-dockerswarm-ax-lab.conf\n"
        )
        self.assertIn(call, gate)
        self.assertLess(gate.index("scripts/validate-racinggame.py"), gate.index(call))
        self.assertLess(gate.index(call), gate.index("-m unittest discover"))

    def test_sensitive_path_guard_covers_the_role(self) -> None:
        guard = (ROOT / ".github/workflows/guard-sensitive-paths.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("\n          ^ansible/roles/ax_lab/\n", guard)
        self.assertIn("\n          ^config/\n", guard)
        self.assertIn("\n          ^scripts/validate\n", guard)

    def test_documentation_lists_the_playbook(self) -> None:
        for relative_path, fragment in (
            ("CLAUDE.md", "`autoupdater`, `ax-lab`, `backup`, `site`."),
            (".claude/agents/judge.md", "`autoupdater`, `ax-lab`, `backup`, `site`;"),
            (
                ".claude/agents/ansible-operator.md",
                "`autoupdater`, `ax-lab`, `backup`, `bootstrap-host`, `site`.",
            ),
        ):
            with self.subTest(path=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn(fragment, " ".join(text.split()))


class AxLabMetadataTests(unittest.TestCase):
    """The real metadata role, fed the playbook's own vars files, in check mode."""

    def run_metadata_check(self, identity: str) -> subprocess.CompletedProcess[str]:
        play = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))[0]
        self.assertEqual(play["roles"][-1], {"role": "deployment_metadata"})
        (ROOT / ".build").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="ax-lab-metadata-", dir=ROOT / ".build"
        ) as temporary:
            directory = Path(temporary)
            installed = directory / "installed"
            (installed / "deployments").mkdir(parents=True)
            # Extra vars outrank vars_files, so the disposable root really
            # replaces platform_install_root and nothing reads the host.
            extra = directory / "extra.json"
            extra.write_text(
                json.dumps(
                    {
                        "platform_install_root": str(installed),
                        "deployment_metadata_source_revision": "a" * 40,
                        "deployment_metadata_contract_sha256": "b" * 64,
                        "deployment_metadata_profile": "production",
                        "deployment_metadata_playbook": identity,
                    }
                ),
                encoding="utf-8",
            )
            check = directory / "check.yml"
            check.write_text(
                yaml.safe_dump(
                    [
                        {
                            "name": "Check the lab metadata in a disposable path",
                            "hosts": "localhost",
                            "connection": "local",
                            "gather_facts": False,
                            "become": False,
                            "vars_files": [
                                str((PLAYBOOK.parent / item).resolve())
                                for item in play["vars_files"]
                            ],
                            "roles": ["deployment_metadata"],
                        }
                    ],
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            environment = dict(os.environ)
            environment["ANSIBLE_CONFIG"] = str(ROOT / "ansible/ansible.cfg")
            environment["ANSIBLE_NOCOLOR"] = "1"
            result = subprocess.run(
                [
                    str(ROOT / ".venv/bin/ansible-playbook"),
                    "-i",
                    "localhost,",
                    "--check",
                    "--diff",
                    "--extra-vars",
                    f"@{extra}",
                    str(check),
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
                check=False,
            )
            self.assertEqual(list(installed.rglob("*.yml")), [])
            return result

    def test_check_renders_only_the_lab_component(self) -> None:
        result = self.run_metadata_check("ax-lab")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('component: "ax-lab"', result.stdout)
        self.assertIn('playbook: "ax-lab"', result.stdout)
        self.assertNotIn('component: "host-baseline"', result.stdout)

    def test_unknown_playbook_is_rejected_before_rendering_metadata(self) -> None:
        result = self.run_metadata_check("ax-lab-unknown")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Deploy through scripts/deploy-ansible.sh", result.stdout)
        self.assertNotIn("Record source identity", result.stdout)


class AxLabValidatorTests(unittest.TestCase):
    """scripts/validate-ax-lab.py accepts the file and rejects every drift."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_script("validate_ax_lab", "scripts/validate-ax-lab.py")
        cls.document = cls.module.load_yaml(CONFIG)
        cls.reserved = cls.module.reserved_sysctl_keys()

    def mutated(self, change) -> dict[str, Any]:
        document = copy.deepcopy(self.document)
        change(document)
        return document

    def assert_rejected(self, change, message: str) -> None:
        with self.assertRaisesRegex(self.module.AxLabError, message):
            self.module.validate_catalog(self.mutated(change), self.reserved)

    def set_lab(self, path: str, value: Any):
        def change(document: dict[str, Any]) -> None:
            *parents, leaf = path.split("/")
            target = document["ax_lab"]
            for parent in parents:
                target = target[parent]
            target[leaf] = value

        return change

    def test_reviewed_contract_passes_and_renders_the_reviewed_sysctl(self) -> None:
        lab = self.module.validate_catalog(self.document)
        rendered = self.module.render_sysctl(lab)
        self.module.validate_sysctl_render(rendered, lab)
        self.assertEqual(
            self.module.parse_sysctl_text(rendered, "render"),
            {WATCHES: "524288", INSTANCES: "512"},
        )
        self.assertTrue(rendered.startswith("# Managed by Ansible (role ax_lab"))
        # The owner accepted the privileged node on 2026-09-25.
        self.assertIs(self.document["ax_lab_privileged_node_accepted"], True)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "ax-lab" / "sysctl.conf"
            with mock.patch("sys.stdout"):
                self.assertEqual(self.module.main(["--output", str(output)]), 0)
            self.assertEqual(output.read_text(encoding="utf-8"), rendered)

    def test_reviewed_pins_are_the_verified_upstream_artifacts(self) -> None:
        lab = self.document["ax_lab"]
        self.assertEqual(
            lab["sources"]["ax"]["commit"], "f009cc81c9a571073bc1dd58cd2ed934bf2d5b1c"
        )
        self.assertEqual(
            lab["sources"]["substrate"]["commit"],
            "672533541dbfcd29084e4de2475267088bda3651",
        )
        self.assertEqual(
            lab["binaries"]["kind"],
            {
                "version": "v0.33.0",
                "url": "https://github.com/kubernetes-sigs/kind/releases/download/"
                "v0.33.0/kind-linux-amd64",
                "sha256": "aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d",
            },
        )
        self.assertEqual(
            lab["binaries"]["kubectl"],
            {
                "version": "v1.37.0",
                "url": "https://dl.k8s.io/release/v1.37.0/bin/linux/amd64/kubectl",
                "sha256": "6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f",
            },
        )
        # Each tag resolved to this index digest on Docker Hub on 2026-09-25
        # (docker buildx imagetools inspect), and each is what the lab runs.
        self.assertEqual(
            lab["images"],
            {
                "kind_node": "docker.io/kindest/node:v1.37.0@sha256:"
                "a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5",
                "registry": "docker.io/library/registry:3@sha256:"
                "852b3e4d378c426dda6b318fe9d9bfe8e92a0eccb9926671ec3d3ea17a196696",
                "redis": "docker.io/library/redis:7-alpine@sha256:"
                "858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499",
                "openai_proxy": "docker.io/nginxinc/nginx-unprivileged:1.30-alpine"
                "@sha256:"
                "4714e0b1b2577eaa1a6131d07c958b67f0eb68e6d0521e90c6e5287db8cf0bc5",
                # The image the manual lab's toolbox was built on (the spike's
                # Dockerfile, FROM line 3), still cached on the host.
                "toolbox": "docker.io/library/golang:1.27.1@sha256:"
                "3680233e3204827fbdc66088528ae6d4b3d034f51d03a99d454f6de034888244",
            },
        )
        self.assertEqual(lab["install_root"], "/opt/dockerswarm/ax-lab")
        self.assertEqual(lab["credential_directory"], "/etc/dockerswarm/ax")

    def test_sysctl_keys_stay_disjoint_from_host_baseline_and_platform(self) -> None:
        self.assertEqual(self.reserved["fs.suid_dumpable"], "host_baseline")
        self.assertEqual(self.reserved["net.ipv4.ip_forward"], "platform")
        self.assertFalse(set(self.document["ax_lab"]["sysctl"]) & set(self.reserved))
        self.assert_rejected(
            self.set_lab("sysctl/fs.suid_dumpable", "0"),
            "fs.suid_dumpable overlaps a key the host_baseline role manages",
        )
        self.assert_rejected(
            self.set_lab("sysctl/net.ipv4.ip_forward", "1"),
            "net.ipv4.ip_forward overlaps a key the platform role manages",
        )

    def test_reserved_sysctl_keys_fail_closed_when_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            empty = Path(temporary) / "empty.conf"
            empty.write_text("# nothing\n", encoding="utf-8")
            with (
                mock.patch.object(self.module, "PLATFORM_SYSCTL_FILE", empty),
                self.assertRaisesRegex(self.module.AxLabError, "sets no key"),
            ):
                self.module.reserved_sysctl_keys()
            defaults = Path(temporary) / "defaults.yml"
            defaults.write_text("---\nother: 1\n", encoding="utf-8")
            with (
                mock.patch.object(self.module, "HOST_BASELINE_DEFAULTS", defaults),
                self.assertRaisesRegex(self.module.AxLabError, "host_baseline_sysctl"),
            ):
                self.module.reserved_sysctl_keys()

    def test_sysctl_values_are_the_reviewed_quoted_decimals(self) -> None:
        for value, message in (
            (524288, "quoted decimal string"),
            ("524288k", "quoted decimal string"),
            ("0524288", "quoted decimal string"),
            ("8192", "differs from the inotify limits kind recommends"),
        ):
            with self.subTest(value=value):
                self.assert_rejected(self.set_lab(f"sysctl/{WATCHES}", value), message)
        self.assert_rejected(
            self.set_lab("sysctl/fs.inotify.max_queued_events", "16384"),
            "differs from the inotify limits kind recommends",
        )
        self.assert_rejected(
            self.set_lab("sysctl/Fs.Inotify.Watches", "1"), "not a kernel setting name"
        )
        self.assert_rejected(self.set_lab("sysctl", {}), "non-empty mapping")

    def test_rendered_sysctl_must_match_the_contract(self) -> None:
        lab = self.document["ax_lab"]
        with self.assertRaisesRegex(self.module.AxLabError, "rendered sysctl"):
            self.module.validate_sysctl_render(
                f"{WATCHES} = 8192\n{INSTANCES} = 512\n", lab
            )
        with self.assertRaisesRegex(self.module.AxLabError, "repeated"):
            self.module.parse_sysctl_text(f"{WATCHES} = 1\n{WATCHES} = 2\n", "file")

    def test_schema_is_strict(self) -> None:
        self.assert_rejected(
            lambda document: document.update(extra=1), "config: unexpected"
        )
        self.assert_rejected(
            lambda document: document.pop("ax_lab_privileged_node_accepted"),
            "config: unexpected",
        )
        self.assert_rejected(self.set_lab("extra", 1), "ax_lab: unexpected")
        self.assert_rejected(
            lambda document: document["ax_lab"].pop("images"), "ax_lab: unexpected"
        )
        self.assert_rejected(self.set_lab("sources/extra", {}), "sources: unexpected")
        self.assert_rejected(
            self.set_lab("binaries/kind/os", "linux"), "kind: unexpected"
        )
        self.assert_rejected(self.set_lab("images/extra", "x"), "images: unexpected")
        for value in (1, 3, True, "2"):
            with self.subTest(schema_version=value):
                self.assert_rejected(
                    self.set_lab("schema_version", value), "schema_version"
                )

    def test_duplicate_yaml_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ax-lab.yml"
            path.write_text(
                "---\nax_lab: {}\nax_lab: {}\nax_lab_privileged_node_accepted: false\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(self.module.AxLabError, "duplicate key"):
                self.module.load_yaml(path)

    def test_acceptance_flag_is_a_real_boolean(self) -> None:
        for value in ("false", "true", 0, 1, None):
            with self.subTest(value=value):
                self.assert_rejected(
                    lambda document, value=value: document.update(
                        ax_lab_privileged_node_accepted=value
                    ),
                    "must be a boolean",
                )
        # Revoking the acceptance is a valid contract: the role then refuses
        # to touch the cluster.
        refused = self.mutated(
            lambda document: document.update(ax_lab_privileged_node_accepted=False)
        )
        self.module.validate_catalog(refused, self.reserved)

    def test_acceptance_records_the_owner_delegation(self) -> None:
        # The comment lines read as one text, without their `#` markers.
        text = " ".join(
            " ".join(
                line.strip().removeprefix("#").strip()
                for line in CONFIG.read_text(encoding="utf-8").splitlines()
            ).split()
        )
        for fragment in (
            "Accepted by the owner on 2026-09-25",
            '("Si a todo, autorizo todo, seguiré todas tus recomendaciones")',
            "--privileged and seccomp and AppArmor unconfined (kind v0.33.0",
            "provision.go, lines 226-228)",
            "ax_lab_privileged_node_accepted: true",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

    def test_commits_are_full_lowercase_shas_of_the_upstream_repositories(
        self,
    ) -> None:
        commit = "f009cc81c9a571073bc1dd58cd2ed934bf2d5b1c"
        for name in ("ax", "substrate"):
            for value in (
                commit[:7],
                commit.upper(),
                commit + "0",
                "g" * 40,
                int("1" * 20),
            ):
                with self.subTest(source=name, commit=value):
                    self.assert_rejected(
                        self.set_lab(f"sources/{name}/commit", value), "40-hex SHA"
                    )
        self.assert_rejected(
            self.set_lab("sources/ax/repository", "https://github.com/fork/ax"),
            "upstream repository",
        )
        self.assert_rejected(
            self.set_lab(
                "sources/substrate/repository",
                "http://github.com/agent-substrate/substrate",
            ),
            "upstream repository",
        )

    def test_binary_checksums_are_64_lowercase_hex(self) -> None:
        digest = "aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d"
        for name in ("kind", "kubectl"):
            for value in (digest[:63], digest.upper(), "z" * 64, "sha256:" + digest):
                with self.subTest(binary=name, sha256=value):
                    self.assert_rejected(
                        self.set_lab(f"binaries/{name}/sha256", value),
                        "64 lowercase hex",
                    )
        self.assert_rejected(
            self.set_lab("binaries/kubectl/sha256", None), "64 lowercase hex"
        )

    def test_binary_versions_are_exact_releases(self) -> None:
        for value in ("0.33.0", "v0.33", "latest", "v0.33.0-rc.1", 33):
            with self.subTest(version=value):
                self.assert_rejected(
                    self.set_lab("binaries/kind/version", value), "look like v1.2.3"
                )

    def test_binary_urls_are_the_official_https_release_assets(self) -> None:
        kind = "https://github.com/kubernetes-sigs/kind/releases/download/"
        for value, message in (
            (kind.replace("https", "http") + "v0.33.0/kind-linux-amd64", "https"),
            (
                "https://objects.githubusercontent.com/kubernetes-sigs/kind/"
                "releases/download/v0.33.0/kind-linux-amd64",
                "official host github.com",
            ),
            (
                "https://github.com/evil/kind/releases/download/"
                "v0.33.0/kind-linux-amd64",
                "official v0.33.0 linux-amd64 asset",
            ),
            (kind + "v0.32.0/kind-linux-amd64", "official v0.33.0 linux-amd64 asset"),
            (kind + "v0.33.0/kind-linux-arm64", "official v0.33.0 linux-amd64 asset"),
            (kind + "v0.33.0/kind-linux-amd64?raw=1", "official host"),
            (kind + "v0.33.0/kind-linux-amd64#x", "official host"),
            ("https://github.com:443/kubernetes-sigs/kind", "official host"),
            ("https://mirror@github.com/kubernetes-sigs/kind", "secret-like value"),
            (None, "url must be a string"),
        ):
            with self.subTest(url=value):
                self.assert_rejected(self.set_lab("binaries/kind/url", value), message)
        self.assert_rejected(
            self.set_lab(
                "binaries/kubectl/url",
                "https://storage.googleapis.com/kubernetes-release/release/"
                "v1.37.0/bin/linux/amd64/kubectl",
            ),
            "official host dl.k8s.io",
        )
        self.assert_rejected(
            self.set_lab(
                "binaries/kubectl/url",
                "https://dl.k8s.io/release/v1.36.0/bin/linux/amd64/kubectl",
            ),
            "official v1.37.0 linux-amd64 asset",
        )

    def test_images_are_pinned_by_tag_and_digest(self) -> None:
        digest = "a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
        node = "docker.io/kindest/node"
        for value, message in (
            (f"{node}:v1.37.0", "repository:tag@sha256"),
            (f"{node}@sha256:{digest}", "repository:tag@sha256"),
            (f"{node}:v1.37.0@sha256:{digest.upper()}", "repository:tag@sha256"),
            (f"{node}:v1.37.0@sha256:{digest[:63]}", "repository:tag@sha256"),
            (f"{node}:v1.37.0@sha512:{digest}", "repository:tag@sha256"),
            (f"{node}:latest@sha256:{digest}", "moving latest tag"),
            (f"kindest/node:v1.37.0@sha256:{digest}", "must use docker.io/kindest"),
            (
                f"docker.io/evil/node:v1.37.0@sha256:{digest}",
                "must use docker.io/kindest",
            ),
            (f"{node}:v1.36.0@sha256:{digest}", "share one version"),
            (None, "repository:tag@sha256"),
        ):
            with self.subTest(image=value):
                self.assert_rejected(self.set_lab("images/kind_node", value), message)
        for name in ("registry", "redis", "openai_proxy"):
            with self.subTest(image=name):
                self.assert_rejected(
                    self.set_lab(
                        f"images/{name}",
                        f"docker.io/library/{name}:latest@sha256:{digest}",
                    ),
                    "must use|moving latest tag",
                )
                bare = self.document["ax_lab"]["images"][name].split("@", 1)[0]
                self.assert_rejected(
                    self.set_lab(f"images/{name}", bare), "repository:tag@sha256"
                )

    def test_install_root_is_its_own_directory_under_the_platform_root(self) -> None:
        for value in (
            "/opt/ax-lab",
            "/opt/dockerswarm",
            "/opt/dockerswarm/../ax-lab",
            "/opt/dockerswarm/ax-lab/",
            "/opt/dockerswarm/ax-lab/bin",
            "/opt/dockerswarm/deployments",
            "/opt/dockerswarm/AX-lab",
            "//opt/dockerswarm/ax-lab",
            "opt/dockerswarm/ax-lab",
            "/srv/dockerswarm/services/ax-lab",
            None,
        ):
            with self.subTest(install_root=value):
                self.assert_rejected(
                    self.set_lab("install_root", value), "install_root"
                )

    def test_credential_directory_is_an_absolute_host_secret_path(self) -> None:
        for value in (
            "etc/dockerswarm/ax",
            "/etc/dockerswarm",
            "/etc/dockerswarm/../shadow",
            "/etc/dockerswarm/ax/",
            "/root/ax",
            "/opt/dockerswarm/ax-lab/credentials",
            ["/etc/dockerswarm/ax"],
        ):
            with self.subTest(credential_directory=value):
                self.assert_rejected(
                    self.set_lab("credential_directory", value), "credential_directory"
                )

    def test_secret_like_keys_and_values_are_refused_anywhere(self) -> None:
        for change in (
            lambda document: document.update(openai_api_key="x"),
            self.set_lab("claude_oauth_token", "x"),
            self.set_lab("sources/ax/password", "x"),
            self.set_lab("binaries/kind/private_key", "x"),
            self.set_lab("images/registry_secret", "x"),
        ):
            self.assert_rejected(change, "secret-like key")
        # Each shape is assembled at runtime from a prefix and a low-entropy
        # filler, so this file never holds a credential-shaped literal.
        filler = "x" * 40
        for prefix, body in (
            ("sk-", "proj-" + filler),
            ("sk-", "ant-oat01-" + filler),
            ("-----", "BEGIN OPENSSH PRIVATE KEY-----"),
            ("gh", "p_" + filler),
            ("github", "_pat_" + filler),
            ("AK", "IA" + "Q" * 16),
            ("xo", "xb-" + filler),
            ("ey", "J" + filler + "." + filler),
            ("Bear", "er " + filler),
            ("https:", "//user:pass@github.com/google/ax"),
            ("OPENAI_API", "_KEY=" + filler),
        ):
            with self.subTest(prefix=prefix):
                self.assert_rejected(
                    self.set_lab("credential_directory", prefix + body),
                    "secret-like value",
                )

    def test_reviewed_cluster_and_registry_are_pinned(self) -> None:
        lab = self.document["ax_lab"]
        self.assertEqual(
            lab["cluster"],
            {
                "name": "kind",
                "node_container": "kind-control-plane",
                "network": "kind",
                "api_server": {"address": "127.0.0.1", "port": 6443},
                "resources": {
                    "memory_limit_mib": 3584,
                    "memory_reservation_mib": 1792,
                    "cpu_limit_millicores": 2000,
                    "pids_limit": 4096,
                },
                "restart_policy": "no",
            },
        )
        self.assertEqual(
            lab["registry"],
            {
                "container": "kind-registry",
                "volume": "ax-lab-registry",
                "host_port": 5001,
                "container_port": 5000,
                "bind_addresses": ["127.0.0.1", "::1"],
                "resources": {
                    "memory_limit_mib": 256,
                    "memory_reservation_mib": 128,
                    "cpu_limit_millicores": 500,
                    "pids_limit": 256,
                },
                "restart_policy": "no",
            },
        )

    def test_cluster_contract_is_fail_closed(self) -> None:
        for path, value, message in (
            ("cluster/name", "Kind", "lowercase DNS label"),
            ("cluster/name", "ax-lab", "<name>-control-plane"),
            ("cluster/node_container", "kind-worker", "<name>-control-plane"),
            ("cluster/network", "bridge", "fixed kind"),
            ("cluster/api_server/address", "0.0.0.0", "canonical loopback"),
            ("cluster/api_server/address", "159.195.156.57", "canonical loopback"),
            ("cluster/api_server/address", "localhost", "canonical loopback"),
            ("cluster/api_server/address", "127.000.0.1", "canonical loopback"),
            ("cluster/api_server/address", "::1", "IPv4 loopback"),
            ("cluster/api_server/address", None, "canonical loopback"),
            # kind's random pick for the manual lab, in the ephemeral range.
            ("cluster/api_server/port", 42803, "fixed port from 1024 to 32767"),
            ("cluster/api_server/port", 443, "fixed port"),
            ("cluster/api_server/port", 0, "fixed port"),
            ("cluster/api_server/port", "6443", "fixed port"),
            ("cluster/api_server/port", True, "fixed port"),
            ("cluster/api_server/port", 5001, "differ from the API server port"),
            ("cluster/api_server/scheme", "https", "api_server: unexpected"),
            ("cluster/resources/memory_limit_mib", 0, "integer >= 1"),
            ("cluster/resources/pids_limit", True, "integer >= 1"),
            ("cluster/resources/cpu_limit_millicores", 2.5, "integer >= 1"),
            ("cluster/resources/memory_reservation_mib", 4096, "reserves more"),
            # 3584 / 1024 = 3.5, over the 2.50 policy of config/capacity.yml.
            ("cluster/resources/memory_reservation_mib", 1024, "ratio exceeds 2.50"),
            ("cluster/resources/swap_mib", 0, "resources: unexpected"),
            ("cluster/labels", {}, "cluster: unexpected"),
        ):
            with self.subTest(path=path, value=value):
                self.assert_rejected(self.set_lab(path, value), message)
        self.assert_rejected(
            lambda document: document["ax_lab"]["cluster"].pop("restart_policy"),
            "cluster: unexpected",
        )

    def test_restart_policy_is_the_string_no(self) -> None:
        # An unquoted YAML `no` loads as False.
        self.assertIs(yaml.safe_load("policy: no")["policy"], False)
        for owner in ("cluster", "registry"):
            for value in (False, None, "always", "on-failure:1", "unless-stopped"):
                with self.subTest(owner=owner, value=value):
                    self.assert_rejected(
                        self.set_lab(f"{owner}/restart_policy", value),
                        f'{owner} restart_policy must be the string "no"',
                    )

    def test_registry_contract_is_fail_closed(self) -> None:
        for path, value, message in (
            ("registry/container", "kind-control-plane", "differ from the node"),
            ("registry/container", "k", "container must be a Docker name"),
            ("registry/container", "-registry", "container must be a Docker name"),
            ("registry/volume", "", "volume must be a Docker name"),
            ("registry/volume", "/var/lib/registry", "volume must be a Docker name"),
            ("registry/host_port", 6443, "differ from the API server port"),
            ("registry/host_port", 80, "fixed port"),
            ("registry/host_port", 50001, "fixed port"),
            ("registry/container_port", 5001, "container_port must be 5000"),
            ("registry/container_port", "5000", "container_port must be 5000"),
            ("registry/bind_addresses", ["0.0.0.0"], "canonical loopback"),
            ("registry/bind_addresses", ["127.0.0.1", "::"], "canonical loopback"),
            ("registry/bind_addresses", ["159.195.156.57"], "canonical loopback"),
            ("registry/bind_addresses", ["[::1]"], "canonical loopback"),
            ("registry/bind_addresses", ["0:0:0:0:0:0:0:1"], "canonical loopback"),
            ("registry/bind_addresses", [], "distinct addresses"),
            ("registry/bind_addresses", "127.0.0.1", "distinct addresses"),
            (
                "registry/bind_addresses",
                ["127.0.0.1", "127.0.0.1"],
                "distinct addresses",
            ),
            ("registry/resources/memory_limit_mib", 1024, "ratio exceeds 2.50"),
            ("registry/resources/memory_reservation_mib", 0, "integer >= 1"),
            ("registry/published", True, "registry: unexpected"),
        ):
            with self.subTest(path=path, value=value):
                self.assert_rejected(self.set_lab(path, value), message)

    def test_limits_must_equal_the_capacity_declaration(self) -> None:
        ratio, group = self.module.load_capacity_declaration()
        self.assertEqual(ratio, self.module.Decimal("2.50"))
        self.assertEqual(set(group), {"kind-control-plane", "kind-registry"})
        self.module.validate_catalog(self.document, self.reserved, (ratio, group))

        def rejected(change, message: str) -> None:
            skewed = copy.deepcopy(group)
            change(skewed)
            with self.assertRaisesRegex(self.module.AxLabError, message):
                self.module.validate_catalog(
                    self.document, self.reserved, (ratio, skewed)
                )

        for field, value in (
            (("limits", "memory_mib"), 4096),
            (("reservations", "memory_mib"), 1536),
            (("limits", "cpu_millicores"), 1000),
            (("pids_limit",), 8192),
        ):
            with self.subTest(field=field):

                def change(skewed, field=field, value=value) -> None:
                    target = skewed["kind-control-plane"]
                    for key in field[:-1]:
                        target = target[key]
                    target[field[-1]] = value

                rejected(change, "kind-control-plane resources differ")
        rejected(lambda skewed: skewed.pop("kind-registry"), "exactly the node")
        rejected(
            lambda skewed: skewed.update({"kind-worker": skewed["kind-registry"]}),
            "exactly the node",
        )
        rejected(
            lambda skewed: skewed["kind-registry"].pop("limits"),
            "kind-registry is malformed",
        )
        # A stricter ratio policy applies to the lab limits too.
        with self.assertRaisesRegex(self.module.AxLabError, "ratio exceeds 1.50"):
            self.module.validate_catalog(
                self.document, self.reserved, (self.module.Decimal("1.50"), group)
            )
        with tempfile.TemporaryDirectory() as temporary:
            profiles = Path(temporary) / "capacity-profiles.yml"
            profiles.write_text(
                "---\ncapacity_profiles:\n  host_containers: {}\n", encoding="utf-8"
            )
            with (
                mock.patch.object(self.module, "CAPACITY_PROFILES", profiles),
                self.assertRaisesRegex(self.module.AxLabError, "declares no ax-lab"),
            ):
                self.module.load_capacity_declaration()

    def test_kind_configuration_is_substrate_plus_loopback_api_and_registry(
        self,
    ) -> None:
        lab = self.document["ax_lab"]
        rendered = self.module.render_kind_config(lab)
        self.module.validate_kind_config_render(rendered, lab)
        config = yaml.safe_load(rendered)
        upstream = yaml.safe_load(SUBSTRATE_KIND_CONFIG)
        # Everything Substrate generates is kept as is...
        for key, value in upstream.items():
            with self.subTest(key=key):
                if key == "networking":
                    self.assertEqual(config[key]["ipFamily"], value["ipFamily"])
                else:
                    self.assertEqual(config[key], value)
        # ...and exactly three things are added: the loopback API server on
        # its fixed port and containerd's registry directory.
        self.assertEqual(set(config) - set(upstream), {"containerdConfigPatches"})
        self.assertEqual(
            config["networking"],
            {
                "ipFamily": "ipv4",
                "apiServerAddress": "127.0.0.1",
                "apiServerPort": 6443,
            },
        )
        self.assertEqual(
            config["containerdConfigPatches"],
            [
                '[plugins."io.containerd.grpc.v1.cri".registry]\n'
                '  config_path = "/etc/containerd/certs.d"'
            ],
        )
        # The upstream text itself, comments included, survives line by line.
        rendered_lines = rendered.splitlines()
        position = 0
        for line in SUBSTRATE_KIND_CONFIG.splitlines():
            position = rendered_lines.index(line, position) + 1
        # No KVM: a single bare control-plane node, no device and no mount.
        self.assertEqual(config["nodes"], [{"role": "control-plane"}])
        settings = [line for line in rendered_lines if not line.startswith("#")]
        for word in ("kvm", "/dev/", "extraMounts", "extraPortMappings"):
            self.assertFalse(any(word in line for line in settings), word)
        self.assertEqual(
            self.module.kind_config_sha256(rendered),
            hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        )

    def test_kind_configuration_render_is_fail_closed(self) -> None:
        lab = self.document["ax_lab"]
        rendered = self.module.render_kind_config(lab)
        for label, old, new, message in (
            (
                "public API server",
                'apiServerAddress: "127.0.0.1"',
                'apiServerAddress: "0.0.0.0"',
                "loopback API server",
            ),
            (
                "random API port",
                "apiServerPort: 6443",
                "apiServerPort: 0",
                "loopback API server",
            ),
            (
                "KVM mount",
                "- role: control-plane\n",
                "- role: control-plane\n  extraMounts:\n  - hostPath: /dev/kvm\n"
                "    containerPath: /dev/kvm\n",
                "one bare control plane",
            ),
            (
                "second node",
                "- role: control-plane\n",
                "- role: control-plane\n- role: worker\n",
                "one bare control plane",
            ),
            (
                "another containerd directory",
                'config_path = "/etc/containerd/certs.d"',
                'config_path = "/etc/docker/certs.d"',
                "only set config_path",
            ),
            (
                "missing feature gate",
                "  PodCertificateRequest: true\n",
                "",
                "featureGates differ",
            ),
            (
                "kubelet patch that is not YAML",
                "  maxParallelImagePulls: 4",
                "  maxParallelImagePulls: [4",
                "kubeadmConfigPatches differ",
            ),
            (
                "containerd patch that is not TOML",
                "config_path = ",
                "config_path == ",
                "only set config_path",
            ),
            (
                "serialized pulls",
                "serializeImagePulls: false",
                "serializeImagePulls: true",
                "kubeadmConfigPatches differ",
            ),
            ("dual stack", "ipFamily: ipv4", "ipFamily: dual", "loopback API server"),
            (
                "runtime config",
                '"certificates.k8s.io/v1beta1": "true"',
                '"certificates.k8s.io/v1beta1": "false"',
                "runtimeConfig differs",
            ),
            (
                "cluster name in the file",
                "kind: Cluster\n",
                "kind: Cluster\nname: other\n",
                "unexpected or missing keys",
            ),
            (
                "duplicate key",
                "kind: Cluster\n",
                "kind: Cluster\nkind: Cluster\n",
                "not YAML",
            ),
        ):
            with self.subTest(case=label):
                self.assertIn(old, rendered)
                with self.assertRaisesRegex(self.module.AxLabError, message):
                    self.module.validate_kind_config_render(
                        rendered.replace(old, new, 1), lab
                    )
        for extra in ("extraPortMappings: []\n", "extraMounts: []\n"):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(self.module.AxLabError, "unexpected"):
                    self.module.validate_kind_config_render(rendered + extra, lab)

    def test_cli_prints_the_kind_configuration_digest(self) -> None:
        lab = self.document["ax_lab"]
        rendered = self.module.render_kind_config(lab)
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            self.assertEqual(self.module.main(["--kind-config-sha256"]), 0)
        self.assertEqual(
            stdout.getvalue(), hashlib.sha256(rendered.encode()).hexdigest() + "\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "ax-lab" / "kind-config.yaml"
            with mock.patch("sys.stdout"):
                self.assertEqual(
                    self.module.main(["--kind-config-output", str(output)]), 0
                )
            self.assertEqual(output.read_text(encoding="utf-8"), rendered)


class AxLabRoleTests(AnsibleTaskAssertions, unittest.TestCase):
    """The role converges idempotently and never fails in check mode."""

    CONVERGE = "Converge only the drifted lab kernel settings"
    PERSISTENT = "Require the persistent configuration to keep the lab values"
    RUNTIME = "Verify every lab kernel setting"
    BINARIES = "Verify each installed binary is its pinned release asset"
    INSTALL = "Install the pinned kind and kubectl release assets"

    @classmethod
    def setUpClass(cls) -> None:
        cls.main = load_tasks(MAIN)
        cls.host = load_tasks(HOST)
        cls.variables = role_variables()

    def condition_holds(self, condition: str, variables: dict[str, Any]) -> bool:
        """Evaluate one production `when` exactly as Ansible would."""
        completed = run_task_definition(
            {
                "name": "Evaluate one reviewed condition",
                "ansible.builtin.assert": {"that": [condition], "quiet": True},
            },
            variables,
        )
        output = completed.stdout + completed.stderr
        self.assertTrue(completed.returncode == 0 or "evaluated_to" in output, output)
        return completed.returncode == 0

    def test_defaults_are_the_fixed_paths_the_gate_pins(self) -> None:
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        self.assertEqual(
            defaults,
            {
                "ax_lab_sysctl_path": SYSCTL_PATH,
                "ax_lab_bin_directory": "{{ ax_lab.install_root }}/bin",
                "ax_lab_architecture": "x86_64",
                "ax_lab_kind_config_path": (
                    "{{ ax_lab.install_root }}/kind-config.yaml"
                ),
                "ax_lab_state_directory": "{{ ax_lab.install_root }}/state",
                "ax_lab_cluster_state_path": (
                    "{{ ax_lab_state_directory }}/cluster.json"
                ),
                "ax_lab_home_directory": "{{ ax_lab.install_root }}/home",
                "ax_lab_kubeconfig_path": "{{ ax_lab_home_directory }}/.kube/config",
                "ax_lab_substrate_source_path": (
                    "{{ ax_lab.install_root }}/src/substrate"
                ),
                "ax_lab_cache_directory": "{{ ax_lab.install_root }}/cache",
                "ax_lab_substrate_state_path": (
                    "{{ ax_lab_state_directory }}/substrate.json"
                ),
                "ax_lab_substrate_manager": (
                    "{{ ax_lab_bin_directory }}/manage-ax-lab-substrate.py"
                ),
            },
        )
        self.assertFalse((ROLE / "handlers").exists())
        self.assertFalse((ROLE / "vars").exists())

    def test_inputs_must_equal_the_versioned_contract(self) -> None:
        name = "Reject overrides of the reviewed AX lab contract"
        self.assertEqual(next(iter(self.main)), name)
        self.assert_task_accepts(MAIN, name, self.variables)
        overridden = copy.deepcopy(self.variables)
        overridden["ax_lab"]["sysctl"][WATCHES] = "8192"
        moved = copy.deepcopy(self.variables)
        moved["ax_lab"]["cluster"]["api_server"]["address"] = "0.0.0.0"
        cases = [
            overridden,
            moved,
            {**self.variables, "ax_lab_privileged_node_accepted": "true"},
            {**self.variables, "ax_lab_privileged_node_accepted": False},
            # 1 == True in Jinja: only the boolean test refuses it.
            {**self.variables, "ax_lab_privileged_node_accepted": 1},
            {**self.variables, "ax_lab_sysctl_path": "/etc/sysctl.d/99-other.conf"},
            {**self.variables, "ax_lab_bin_directory": "/usr/local/bin"},
            {**self.variables, "ax_lab_architecture": "aarch64"},
            {**self.variables, "ax_lab_kind_config_path": "/tmp/kind.yaml"},
            {**self.variables, "ax_lab_state_directory": "/tmp/state"},
            {**self.variables, "ax_lab_cluster_state_path": "/tmp/cluster.json"},
            {**self.variables, "ax_lab_home_directory": "/root"},
            {**self.variables, "ax_lab_kubeconfig_path": "/root/.kube/config"},
            {**self.variables, "ax_lab_substrate_fallback_builds": ["ateapi"]},
            {**self.variables, "ax_lab_substrate_source_path": "/tmp/substrate"},
            {**self.variables, "ax_lab_cache_directory": "/tmp/cache"},
            {**self.variables, "ax_lab_substrate_state_path": "/tmp/s.json"},
            {**self.variables, "ax_lab_substrate_manager": "/tmp/manager.py"},
        ]
        for index, variables in enumerate(cases):
            with self.subTest(case=index):
                self.assert_task_rejects(
                    MAIN, name, variables, "AX lab inputs differ from the versioned"
                )

    def test_validator_runs_locally_like_the_other_layers(self) -> None:
        task = self.main["Validate the AX lab contract and its sysctl render locally"]
        self.assertEqual(
            task["ansible.builtin.command"]["argv"],
            [
                "{{ ansible_playbook_python }}",
                "{{ role_path }}/../../../scripts/validate-ax-lab.py",
            ],
        )
        self.assertEqual(task["delegate_to"], "localhost")
        self.assertIs(task["become"], False)
        self.assertIs(task["changed_when"], False)
        self.assertIs(task["check_mode"], False)
        names = list(self.main)
        self.assertLess(
            names.index("Validate the AX lab contract and its sysctl render locally"),
            names.index("Reconcile the lab host prerequisites"),
        )
        self.assertEqual(
            self.main["Reconcile the lab host prerequisites"],
            {
                "name": "Reconcile the lab host prerequisites",
                "ansible.builtin.import_tasks": "host.yml",
            },
        )

    def test_install_root_and_architecture_gates(self) -> None:
        name = "Require the lab install root directly under the platform root"
        self.assert_task_accepts(MAIN, name, self.variables)
        self.assert_task_rejects(
            MAIN,
            name,
            {**self.variables, "platform_install_root": "/opt/other"},
            "platform_install_root",
        )
        name = "Require the architecture of the pinned binaries"
        self.assert_task_accepts(
            MAIN, name, {**self.variables, "ax_lab_machine": {"stdout": "x86_64"}}
        )
        self.assert_task_rejects(
            MAIN,
            name,
            {**self.variables, "ax_lab_machine": {"stdout": "aarch64"}},
            "linux-amd64",
        )

    def test_preflight_reads_every_path_without_following_links(self) -> None:
        stats = [
            task["ansible.builtin.stat"]
            for path in role_task_files()
            for task in yaml.safe_load(path.read_text(encoding="utf-8"))
            if "ansible.builtin.stat" in task
        ]
        self.assertTrue(stats)
        self.assertTrue(all(stat["follow"] is False for stat in stats))
        task = self.main[
            "Inspect the lab install and sysctl paths without following links"
        ]
        self.assertEqual(
            [(item["path"], item["directory"]) for item in task["loop"]],
            [
                ("/opt", True),
                ("{{ platform_install_root }}", True),
                ("{{ ax_lab.install_root }}", True),
                ("{{ ax_lab_bin_directory }}", True),
                ("{{ ax_lab_bin_directory }}/kind", False),
                ("{{ ax_lab_bin_directory }}/kubectl", False),
                ("{{ ax_lab_kind_config_path }}", False),
                ("{{ ax_lab_state_directory }}", True),
                ("{{ ax_lab_cluster_state_path }}", False),
                ("{{ ax_lab_home_directory }}", True),
                ("{{ ax_lab_home_directory }}/.kube", True),
                ("{{ ax_lab_kubeconfig_path }}", False),
                ("{{ ax_lab.install_root }}/src", True),
                ("{{ ax_lab_substrate_source_path }}", True),
                ("{{ ax_lab_cache_directory }}", True),
                ("{{ ax_lab_substrate_state_path }}", False),
                ("{{ ax_lab_substrate_manager }}", False),
                ("{{ ax_lab_bin_directory }}/host_global_operation_lock.py", False),
                ("{{ ax_lab_bin_directory }}/ansible-operation-lock.py", False),
                ("{{ ax_lab_bin_directory }}/run-locked-command.py", False),
                ("/var/backups", True),
                ("/var/backups/dockerswarm", True),
                ("{{ ax_lab.substrate.backup_directory | dirname }}", True),
                ("{{ ax_lab.substrate.backup_directory }}", True),
                ("/etc", True),
                ("/etc/sysctl.d", True),
                ("{{ ax_lab_sysctl_path }}", False),
            ],
        )

    def test_unsafe_install_paths_are_refused(self) -> None:
        name = "Reject unsafe types ownership and permissions in lab paths"

        def result(directory: bool, **stat: Any) -> dict[str, Any]:
            state = {
                "exists": True,
                "islnk": False,
                "isdir": directory,
                "isreg": not directory,
                "uid": 0,
                "gid": 0,
                "mode": "0750" if directory else "0755",
                **stat,
            }
            return {"item": {"path": "/x", "directory": directory}, "stat": state}

        def variables(*results: dict[str, Any]) -> dict[str, Any]:
            return {"ax_lab_install_paths": {"results": list(results)}}

        self.assert_task_accepts(
            MAIN,
            name,
            variables(
                result(True),
                result(False),
                {"item": {"path": "/y", "directory": False}, "stat": {"exists": False}},
            ),
        )
        for bad in (
            result(True, islnk=True),
            result(True, isdir=False),
            result(False, isreg=False),
            result(True, uid=1001),
            result(True, gid=1001),
            result(True, mode="0770"),
            result(False, mode="0757"),
        ):
            with self.subTest(stat=bad["stat"]):
                self.assert_task_rejects(
                    MAIN,
                    name,
                    variables(bad),
                    "Refusing to write the lab prerequisites",
                )

    def test_sysctl_file_is_persisted_without_a_blanket_reload(self) -> None:
        template = self.host["Persist the lab kernel settings for the next boot"]
        self.assertEqual(
            template["ansible.builtin.template"],
            {
                "src": "99-z-dockerswarm-ax-lab.conf.j2",
                "dest": "{{ ax_lab_sysctl_path }}",
                "owner": "root",
                "group": "root",
                "mode": "0644",
                "validate": "/usr/sbin/sysctl --dry-run --load=%s",
            },
        )
        self.assertNotIn("notify", template)
        for path in role_task_files():
            for task in yaml.safe_load(path.read_text(encoding="utf-8")):
                argv = (task.get("ansible.builtin.command") or {}).get("argv") or []
                with self.subTest(task=task["name"]):
                    if argv and argv[0] == "/usr/sbin/sysctl" and "--system" in argv:
                        self.assertIn("--dry-run", argv)

    def test_every_read_is_check_safe_and_only_drift_is_written(self) -> None:
        # cluster.yml never runs in --check; AxLabClusterTests covers it.
        for relative_path in CHECK_MODE_FILES:
            for task in yaml.safe_load((ROOT / relative_path).read_text()):
                command = task.get("ansible.builtin.command")
                if command is None:
                    continue
                with self.subTest(task=task["name"]):
                    if task["name"] == self.CONVERGE:
                        self.assertNotIn("check_mode", task)
                        self.assertIs(task["changed_when"], True)
                    else:
                        self.assertIs(task["changed_when"], False)
                        self.assertIs(task["check_mode"], False)
                        self.assertNotIn("-w", command["argv"])
        converge = self.host[self.CONVERGE]
        self.assertEqual(
            converge["ansible.builtin.command"]["argv"],
            ["/usr/sbin/sysctl", "-w", "{{ item.item.key }}={{ item.item.value }}"],
        )
        self.assertEqual(converge["loop"], "{{ ax_lab_observed_sysctl.results }}")
        self.assertEqual(converge["when"], "item.stdout != item.item.value")
        observed = self.host["Read every lab kernel setting before convergence"]
        self.assertEqual(observed["register"], "ax_lab_observed_sysctl")
        self.assertEqual(observed["loop"], "{{ ax_lab.sysctl | dict2items }}")
        names = list(self.host)
        order = [
            "Render and parse the lab sysctl candidate in memory",
            "Persist the lab kernel settings for the next boot",
            "Dry-run the complete persistent sysctl configuration",
            self.PERSISTENT,
            "Read every lab kernel setting before convergence",
            self.CONVERGE,
            "Read every lab kernel setting",
            self.RUNTIME,
            "Create the lab install root and its binary directory",
            "Inspect the binary directory before installing into it",
            self.INSTALL,
            "Inspect the installed lab binaries without following links",
            self.BINARIES,
        ]
        self.assertEqual([name for name in names if name in order], order)

    def test_runtime_checks_wait_for_a_real_apply(self) -> None:
        for name in (
            self.PERSISTENT,
            self.RUNTIME,
            "Verify the binary directory after the apply",
            self.BINARIES,
        ):
            with self.subTest(task=name):
                self.assertEqual(self.host[name]["when"], "not ansible_check_mode")

    def test_convergence_selects_only_drifted_keys(self) -> None:
        condition = self.host[self.CONVERGE]["when"]
        managed = {"key": WATCHES, "value": "524288"}
        self.assertTrue(
            self.condition_holds(
                condition, {"item": {"stdout": "8192", "item": managed}}
            )
        )
        self.assertFalse(
            self.condition_holds(
                condition, {"item": {"stdout": "524288", "item": managed}}
            )
        )

    def test_runtime_postcheck_rejects_live_drift(self) -> None:
        managed = {"key": WATCHES, "value": "524288"}
        self.assert_task_accepts(
            HOST,
            self.RUNTIME,
            {
                "ax_lab_runtime_sysctl": {
                    "results": [{"stdout": "524288", "item": managed}]
                }
            },
        )
        self.assert_task_rejects(
            HOST,
            self.RUNTIME,
            {
                "ax_lab_runtime_sysctl": {
                    "results": [{"stdout": "8192", "item": managed}]
                }
            },
            f"{WATCHES} is 8192, expected 524288.",
        )

    def test_persistent_configuration_must_boot_with_the_lab_values(self) -> None:
        applying = "* Applying /etc/sysctl.d/99-z-dockerswarm-ax-lab.conf ..."

        def variables(lines: list[str]) -> dict[str, Any]:
            return {
                "ax_lab": self.variables["ax_lab"],
                "ax_lab_persistent_sysctl": {"stdout_lines": lines},
            }

        converged = [
            applying,
            "fs.inotify.max_user_watches_other = 1",
            f"{INSTANCES} = 512",
            f"{WATCHES} = 524288",
        ]
        self.assert_task_accepts(HOST, self.PERSISTENT, variables(converged))
        self.assert_task_accepts(
            HOST, self.PERSISTENT, variables([f"{WATCHES} = 8192", *converged])
        )
        for lines in (
            [*converged, f"{WATCHES} = 8192"],
            [applying, f"{INSTANCES} = 512"],
            [applying, f"{INSTANCES} = 512", f"{WATCHES}=524288"],
        ):
            with self.subTest(lines=lines):
                self.assert_task_rejects(
                    HOST,
                    self.PERSISTENT,
                    variables(lines),
                    "would not boot with",
                )

    def test_binaries_are_installed_by_pinned_checksum(self) -> None:
        directories = self.host["Create the lab install root and its binary directory"]
        self.assertEqual(
            directories["ansible.builtin.file"],
            {
                "path": "{{ item }}",
                "state": "directory",
                "owner": "root",
                "group": "root",
                "mode": "0750",
            },
        )
        self.assertEqual(
            directories["loop"],
            ["{{ ax_lab.install_root }}", "{{ ax_lab_bin_directory }}"],
        )
        install = self.host[self.INSTALL]
        self.assertEqual(
            install["ansible.builtin.get_url"],
            {
                "url": "{{ item.value.url }}",
                "dest": "{{ ax_lab_bin_directory }}/{{ item.key }}",
                "checksum": "sha256:{{ item.value.sha256 }}",
                "owner": "root",
                "group": "root",
                "mode": "0755",
                "force": False,
            },
        )
        self.assertEqual(install["loop"], "{{ ax_lab.binaries | dict2items }}")
        inspect = self.host[
            "Inspect the installed lab binaries without following links"
        ]
        self.assertEqual(
            inspect["ansible.builtin.stat"],
            {
                "path": "{{ ax_lab_bin_directory }}/{{ item.key }}",
                "follow": False,
                "checksum_algorithm": "sha256",
            },
        )

    def test_check_mode_skips_downloads_into_a_missing_directory(self) -> None:
        condition = self.host[self.INSTALL]["when"]
        for stat, expected in (
            ({"exists": True, "isdir": True, "islnk": False}, True),
            ({"exists": False}, False),
            ({"exists": True, "isdir": False, "islnk": True}, False),
            ({"exists": True, "isdir": False, "islnk": False}, False),
        ):
            with self.subTest(stat=stat):
                self.assertEqual(
                    self.condition_holds(
                        condition, {"ax_lab_bin_directory_state": {"stat": stat}}
                    ),
                    expected,
                )

    def test_installed_binaries_must_be_the_pinned_assets(self) -> None:
        kind = {
            "key": "kind",
            "value": self.variables["ax_lab"]["binaries"]["kind"],
        }
        good = {
            "exists": True,
            "isreg": True,
            "islnk": False,
            "uid": 0,
            "gid": 0,
            "mode": "0755",
            "checksum": kind["value"]["sha256"],
        }

        def variables(stat: dict[str, Any]) -> dict[str, Any]:
            return {
                "ax_lab_installed_binaries": {"results": [{"item": kind, "stat": stat}]}
            }

        self.assert_task_accepts(HOST, self.BINARIES, variables(good))
        for change in (
            {"exists": False},
            {"checksum": "0" * 64},
            {"islnk": True, "isreg": False},
            {"mode": "0775"},
            {"uid": 1001},
            {"gid": 1001},
        ):
            with self.subTest(change=change):
                self.assert_task_rejects(
                    HOST,
                    self.BINARIES,
                    variables({**good, **change}),
                    "kind is not the pinned v0.33.0 release asset.",
                )

    def test_ansible_renders_the_same_sysctl_file_as_the_validator(self) -> None:
        validator = load_script("validate_ax_lab_render", "scripts/validate-ax-lab.py")
        expected = validator.render_sysctl(self.variables["ax_lab"])
        template = ROLE / "templates/99-z-dockerswarm-ax-lab.conf.j2"
        completed = run_task_definition(
            {
                "name": "Render the lab sysctl file with Ansible",
                "ansible.builtin.assert": {
                    "that": [
                        f"lookup('ansible.builtin.template', '{template}') | trim"
                        " == expected | trim"
                    ],
                    "quiet": True,
                },
            },
            {"ax_lab": self.variables["ax_lab"], "expected": expected},
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_role_never_touches_the_credential_files(self) -> None:
        for path in [*role_task_files(), ROLE / "defaults/main.yml"]:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("credential_directory", text)
                self.assertNotIn("/etc/dockerswarm", text)
        # The only files the role reads back are its own ownership proofs and
        # the registry's memory events.
        slurps = sorted(
            task["ansible.builtin.slurp"]["src"]
            for path in role_task_files()
            for task in yaml.safe_load(path.read_text(encoding="utf-8"))
            if "ansible.builtin.slurp" in task
        )
        self.assertEqual(
            slurps,
            [
                '{{ "/sys/fs/cgroup/system.slice/docker-" ~ ax_lab_registry.id'
                ' ~ ".scope/memory.events" }}',
                "{{ ax_lab_cluster_state_path }}",
                "{{ ax_lab_substrate_state_path }}",
            ],
        )
        for path in role_task_files():
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("slurp", text.replace("ansible.builtin.slurp", ""))


class AxLabClusterTests(AnsibleTaskAssertions, unittest.TestCase):
    """The kind cluster and registry: ownership first, drift only, --check reads."""

    DERIVE = "Derive the reviewed settings of the lab containers"
    RECORD = "Record the lab registry and node as read"
    DRIFT = "Record each lab container whose limits or restart policy differ"
    NO_PROOF = "Refuse a lab node this role cannot prove it created"
    PROOF = "Require the state file to prove this exact lab node"
    NODE = "Require the lab node to keep the settings it was created with"
    REGISTRY_LABELS = "Refuse a local registry this role did not create"
    REGISTRY = "Require the local registry to keep the settings it was created with"
    CREATE = "Create the lab cluster with the pinned kind and node image"
    RUN_REGISTRY = "Create the local registry from its pinned digest"
    UPDATE = "Converge the limits and restart policy of drifted lab containers"
    START = "Start the local registry when it is stopped"
    STATE = "Record the proof that this role created the lab node"
    VERIFY = "Verify the lab containers carry their reviewed limits"
    NODE_START = "Start the lab node when it is stopped"
    NODE_VERIFY = "Verify the lab node runs with its reviewed limits"

    @classmethod
    def setUpClass(cls) -> None:
        cls.main = load_tasks(MAIN)
        cls.inspect = load_tasks(INSPECT)
        cls.read = load_tasks(READ)
        cls.ownership = load_tasks(OWNERSHIP)
        cls.cluster = load_tasks(CLUSTER)
        cls.node = load_tasks(NODE)
        cls.variables = cluster_variables()
        cls.lab = cls.variables["ax_lab"]

    def facts(self, *reads: dict[str, Any]) -> list[dict[str, Any]]:
        """The reviewed tasks that derive the settings and record `reads`."""
        return [
            self.inspect[self.DERIVE],
            {
                "name": "Stand in for read.yml's docker container inspect",
                "ansible.builtin.set_fact": {
                    "ax_lab_container_reads": {"results": list(reads)}
                },
            },
            self.read[self.RECORD],
            self.read[self.DRIFT],
        ]

    def converged(self) -> list[dict[str, Any]]:
        return [container_read("kind-registry"), container_read("kind-control-plane")]

    def assert_tasks_pass(self, tasks: list[dict[str, Any]], **extra: Any) -> None:
        completed = run_reviewed_tasks(tasks, {**self.variables, **extra})
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def assert_tasks_fail(
        self, tasks: list[dict[str, Any]], message: str, **extra: Any
    ) -> None:
        completed = run_reviewed_tasks(tasks, {**self.variables, **extra})
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0, output)
        self.assertIn(message, " ".join(output.split()))

    def test_main_proves_ownership_then_changes_only_outside_check_mode(self) -> None:
        names = list(self.main)
        order = [
            "Validate the AX lab contract and its sysctl render locally",
            "Compute the digest of the reviewed kind configuration locally",
            "Require a sha256 digest of the reviewed kind configuration",
            "Reject unsafe types ownership and permissions in lab paths",
            # Ownership is proven before the host prerequisites write anything.
            "Read the lab cluster and registry and prove their ownership",
            "Reconcile the lab host prerequisites",
            "Report what an apply would change in the lab cluster",
            "Reconcile the lab cluster and its local registry outside check mode",
        ]
        self.assertEqual([name for name in names if name in order], order)
        self.assertLess(
            names.index(order[-1]),
            names.index(
                "Seed and verify the pinned Substrate images outside check mode"
            ),
        )
        self.assertEqual(
            self.main[order[4]],
            {"name": order[4], "ansible.builtin.import_tasks": "inspect.yml"},
        )
        self.assertEqual(
            self.main[order[6]],
            {
                "name": order[6],
                "ansible.builtin.debug": {"msg": "{{ ax_lab_cluster_plan }}"},
                "when": "ansible_check_mode",
            },
        )
        self.assertEqual(
            self.main[order[7]],
            {
                "name": order[7],
                "ansible.builtin.import_tasks": "cluster.yml",
                "when": "not ansible_check_mode",
            },
        )
        digest = self.main[order[1]]
        self.assertEqual(
            digest["ansible.builtin.command"]["argv"],
            [
                "{{ ansible_playbook_python }}",
                "{{ role_path }}/../../../scripts/validate-ax-lab.py",
                "--kind-config-sha256",
            ],
        )
        self.assertEqual(digest["register"], "ax_lab_kind_config_digest")
        self.assertEqual(digest["delegate_to"], "localhost")
        self.assertIs(digest["check_mode"], False)
        self.assertIs(digest["changed_when"], False)
        name = order[2]
        self.assert_task_accepts(
            MAIN, name, {"ax_lab_kind_config_digest": {"stdout": "a" * 64}}
        )
        for stdout in ("", "A" * 64, "a" * 63, "ERROR: x"):
            with self.subTest(stdout=stdout):
                self.assert_task_rejects(
                    MAIN,
                    name,
                    {"ax_lab_kind_config_digest": {"stdout": stdout}},
                    "did not print the kind configuration digest",
                )

    def test_inspection_imports_read_then_ownership(self) -> None:
        names = list(self.inspect)
        self.assertEqual(
            names,
            [
                "Refuse to create or touch the lab cluster without the owner acceptance",
                self.DERIVE,
                "Read the lab registry and node",
                "Prove that every existing lab container is this role's",
                "Read the transient Substrate build and install containers",
                "Refuse a transient Substrate container left from an earlier run",
                "Describe what an apply would change in the lab cluster",
            ],
        )
        self.assertEqual(
            self.inspect["Read the lab registry and node"][
                "ansible.builtin.import_tasks"
            ],
            "read.yml",
        )
        self.assertEqual(
            self.inspect["Prove that every existing lab container is this role's"][
                "ansible.builtin.import_tasks"
            ],
            "ownership.yml",
        )

    def test_check_mode_files_only_read(self) -> None:
        allowed = {
            "ansible.builtin.assert",
            "ansible.builtin.set_fact",
            "ansible.builtin.stat",
            "ansible.builtin.slurp",
            "ansible.builtin.import_tasks",
            "ansible.builtin.command",
        }
        for relative_path in (INSPECT, READ, OWNERSHIP):
            for task in yaml.safe_load((ROOT / relative_path).read_text()):
                with self.subTest(task=task["name"]):
                    modules = allowed.intersection(task)
                    self.assertEqual(len(modules), 1, task)
                    if "ansible.builtin.stat" in task:
                        self.assertIs(task["ansible.builtin.stat"]["follow"], False)
                    if "ansible.builtin.command" in task:
                        argv = task["ansible.builtin.command"]["argv"]
                        self.assertEqual(
                            argv[:4],
                            ["/usr/bin/docker", "container", "inspect", "--format"],
                        )
                        self.assertIs(task["check_mode"], False)
                        self.assertIs(task["changed_when"], False)
                        self.assertIs(task["failed_when"], False)

    def test_acceptance_gate_refuses_the_cluster(self) -> None:
        name = "Refuse to create or touch the lab cluster without the owner acceptance"
        self.assert_task_accepts(INSPECT, name, self.variables)
        for value in (False, "true", 1):
            with self.subTest(value=value):
                self.assert_task_rejects(
                    INSPECT,
                    name,
                    {**self.variables, "ax_lab_privileged_node_accepted": value},
                    "neither created nor changed" if value is False else "",
                )

    def test_derived_settings_are_the_contract_in_docker_units(self) -> None:
        mib = 1048576
        expected_containers = [
            {
                "name": "kind-registry",
                "resources": self.lab["registry"]["resources"],
                "limits": {
                    "memory": 256 * mib,
                    "memory_swap": 256 * mib,
                    "memory_reservation": 128 * mib,
                    "nano_cpus": 500_000_000,
                    "pids_limit": 256,
                    "restart_policy": {"Name": "no", "MaximumRetryCount": 0},
                },
            },
            {
                "name": "kind-control-plane",
                "resources": self.lab["cluster"]["resources"],
                "limits": {
                    "memory": 3584 * mib,
                    "memory_swap": 3584 * mib,
                    "memory_reservation": 1792 * mib,
                    "nano_cpus": 2_000_000_000,
                    "pids_limit": 4096,
                    "restart_policy": {"Name": "no", "MaximumRetryCount": 0},
                },
            },
        ]
        inspect_format = (
            '{"id":{{json .ID}},"name":{{json .Name}}, '
            '"status":{{json .State.Status}},"image":{{json .Config.Image}}, '
            '"labels":{{json .Config.Labels}}, '
            '"memory":{{json .HostConfig.Memory}}, '
            '"memory_swap":{{json .HostConfig.MemorySwap}}, '
            '"memory_reservation":{{json .HostConfig.MemoryReservation}}, '
            '"nano_cpus":{{json .HostConfig.NanoCPUs}}, '
            '"pids_limit":{{json .HostConfig.PidsLimit}}, '
            '"restart_policy":{{json .HostConfig.RestartPolicy}}, '
            '"port_bindings":{{json .HostConfig.PortBindings}}, '
            '"networks":{{json .NetworkSettings.Networks}}, '
            '"mounts":{{json .Mounts}}}'
        )
        state = {
            "schema_version": 1,
            "cluster_name": "kind",
            "node_container": "kind-control-plane",
            "node_image": self.lab["images"]["kind_node"],
            "kind_version": "v0.33.0",
            "kind_config_sha256": self.variables["ax_lab_kind_config_digest"]["stdout"],
        }
        self.assert_tasks_pass(
            [
                self.inspect[self.DERIVE],
                probe("ax_lab_containers == expected_containers"),
                # Compared by digest: the Go template is not Jinja. `.Id` is not
                # a field of the typed response and would switch the CLI to the
                # raw JSON map, where `.HostConfig.NanoCPUs` fails.
                probe(
                    "ax_lab_container_inspect_format | hash('sha256')"
                    " == inspect_format_sha256"
                ),
                probe(
                    "ax_lab_node_port_bindings == "
                    "{'6443/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '6443'}]}"
                ),
                probe(
                    "ax_lab_registry_hosts_directory == "
                    "'/etc/containerd/certs.d/localhost:5001'"
                ),
                probe(
                    "ax_lab_registry_mirror == "
                    "'[host.\"http://kind-registry:5000\"]'"
                ),
                probe(
                    "ax_lab_registry_hosting_data == {'localRegistryHosting.v1': "
                    '\'host: "localhost:5001"\\n'
                    'help: "https://kind.sigs.k8s.io/docs/user/local-registry/"\\n\'}'
                ),
                probe("ax_lab_expected_cluster_state == state"),
            ],
            expected_containers=expected_containers,
            inspect_format_sha256=hashlib.sha256(inspect_format.encode()).hexdigest(),
            state=state,
        )

    def test_inspect_format_reads_only_typed_fields(self) -> None:
        fields = re.findall(
            r"\{\{json (\.[A-Za-z.]+)\}\}",
            self.inspect[self.DERIVE]["ansible.builtin.set_fact"][
                "ax_lab_container_inspect_format"
            ],
        )
        self.assertEqual(
            fields,
            [
                ".ID",
                ".Name",
                ".State.Status",
                ".Config.Image",
                ".Config.Labels",
                ".HostConfig.Memory",
                ".HostConfig.MemorySwap",
                ".HostConfig.MemoryReservation",
                ".HostConfig.NanoCPUs",
                ".HostConfig.PidsLimit",
                ".HostConfig.RestartPolicy",
                ".HostConfig.PortBindings",
                ".NetworkSettings.Networks",
                ".Mounts",
            ],
        )
        self.assertNotIn(".Config.Env", " ".join(fields))

    def test_drift_is_recorded_only_for_containers_that_differ(self) -> None:
        cases = (
            ("converged", self.converged(), []),
            (
                "both absent",
                [
                    container_absent("kind-registry"),
                    container_absent("kind-control-plane"),
                ],
                [],
            ),
            (
                # What kind leaves: no limits, restart policy on-failure:1.
                "node as kind creates it",
                [
                    container_read("kind-registry"),
                    container_read(
                        "kind-control-plane",
                        memory=0,
                        memory_swap=0,
                        memory_reservation=0,
                        nano_cpus=0,
                        pids_limit=None,
                        restart_policy={"Name": "on-failure", "MaximumRetryCount": 1},
                    ),
                ],
                ["kind-control-plane"],
            ),
            (
                "swap allowed",
                [
                    container_read("kind-registry", memory_swap=-1),
                    container_read("kind-control-plane"),
                ],
                ["kind-registry"],
            ),
            (
                "restart always",
                [
                    container_read(
                        "kind-registry",
                        restart_policy={"Name": "always", "MaximumRetryCount": 0},
                    ),
                    container_read("kind-control-plane", pids_limit=8192),
                ],
                ["kind-registry", "kind-control-plane"],
            ),
            (
                "cpu only",
                [
                    container_read("kind-registry"),
                    container_read("kind-control-plane", nano_cpus=1_000_000_000),
                ],
                ["kind-control-plane"],
            ),
        )
        for label, reads, expected in cases:
            with self.subTest(case=label):
                self.assert_tasks_pass(
                    [*self.facts(*reads), probe("ax_lab_limits_drift == expected")],
                    expected=expected,
                )

    def test_absence_needs_docker_to_name_the_exact_container(self) -> None:
        name = "Require each lab container to be readable or absent"
        for read in (
            container_absent("kind-registry"),
            {
                **container_absent("kind-registry"),
                "stderr": "Error: No such container: kind-registry",
            },
            container_read("kind-registry"),
        ):
            with self.subTest(read=read["stderr"]):
                self.assert_task_accepts(
                    READ, name, {"ax_lab_container_reads": {"results": [read]}}
                )
        for read in (
            {**container_absent("kind-registry"), "stderr": "permission denied"},
            {
                **container_absent("kind-registry"),
                "stderr": "Error: No such container: kind-registry-old",
            },
            {**container_absent("kind-registry"), "stdout": "[]"},
            # An ID prefix resolved to another container.
            container_read("kind-registry", name="/kind-registry-old"),
        ):
            with self.subTest(read=read):
                self.assert_task_rejects(
                    READ,
                    name,
                    {"ax_lab_container_reads": {"results": [read]}},
                    "Cannot read the lab container kind-registry",
                )

    def test_node_without_proof_is_never_adopted(self) -> None:
        node = json.loads(container_read("kind-control-plane")["stdout"])
        owned = {
            "exists": True,
            "isreg": True,
            "islnk": False,
            "uid": 0,
            "gid": 0,
            "mode": "0600",
        }
        self.assert_task_accepts(
            OWNERSHIP,
            self.NO_PROOF,
            {
                **self.variables,
                "ax_lab_node": node,
                "ax_lab_cluster_state_file": {"stat": owned},
            },
        )
        # Absent node: nothing to prove, whatever the state file says.
        self.assert_task_accepts(
            OWNERSHIP,
            self.NO_PROOF,
            {
                **self.variables,
                "ax_lab_node": None,
                "ax_lab_cluster_state_file": {"stat": {"exists": False}},
            },
        )
        for stat in (
            {"exists": False},
            {**owned, "islnk": True, "isreg": False},
            {**owned, "uid": 1001},
            {**owned, "mode": "0644"},
        ):
            with self.subTest(stat=stat):
                self.assert_task_rejects(
                    OWNERSHIP,
                    self.NO_PROOF,
                    {
                        **self.variables,
                        "ax_lab_node": node,
                        "ax_lab_cluster_state_file": {"stat": stat},
                    },
                    "Retirar el laboratorio manual",
                )
        # On a first apply no state file exists yet, so a node that apply left
        # without proof lands here too: the message also names its runbook.
        self.assert_task_rejects(
            OWNERSHIP,
            self.NO_PROOF,
            {
                **self.variables,
                "ax_lab_node": node,
                "ax_lab_cluster_state_file": {"stat": {"exists": False}},
            },
            "is deleted as in docs/AX.md («Recrear el clúster»)",
        )

    def test_state_file_must_prove_this_exact_node(self) -> None:
        node = json.loads(container_read("kind-control-plane")["stdout"])
        digest = self.variables["ax_lab_kind_config_digest"]["stdout"]
        state = {
            "schema_version": 1,
            "cluster_name": "kind",
            "node_container": "kind-control-plane",
            "node_container_id": node["id"],
            "node_image": self.lab["images"]["kind_node"],
            "kind_version": "v0.33.0",
            "kind_config_sha256": digest,
        }

        def tasks(document: dict[str, Any]) -> list[dict[str, Any]]:
            content = base64.b64encode(json.dumps(document).encode()).decode()
            return [
                self.inspect[self.DERIVE],
                {
                    "name": "Stand in for the slurp of the state file",
                    "ansible.builtin.set_fact": {
                        "ax_lab_cluster_state_content": {"content": content}
                    },
                },
                self.ownership[self.PROOF],
            ]

        self.assert_tasks_pass(tasks(state), ax_lab_node=node)
        for change in (
            {"node_container_id": "e" * 64},
            {"kind_config_sha256": "0" * 64},
            {"node_image": "kindest/node:v1.37.0"},
            {"kind_version": "v0.32.0"},
            {"schema_version": 2},
            {"extra": True},
        ):
            with self.subTest(change=change):
                self.assert_tasks_fail(
                    tasks({**state, **change}),
                    "is not the node this role created",
                    ax_lab_node=node,
                )

    def test_node_must_keep_its_creation_settings(self) -> None:
        good = json.loads(container_read("kind-control-plane")["stdout"])

        def check(node: dict[str, Any]):
            return [
                self.inspect[self.DERIVE],
                {
                    "name": "Stand in for the recorded node",
                    "ansible.builtin.set_fact": {"ax_lab_node": node},
                },
                self.ownership[self.NODE],
            ]

        for status in ("running", "exited", "created"):
            with self.subTest(status=status):
                self.assert_tasks_pass(check({**good, "status": status}))
        for change in (
            # The manual lab: kind's default image string and a random port.
            {"image": "kindest/node:v1.37.0@sha256:" + "a" * 64},
            {
                "port_bindings": {
                    "6443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "42803"}]
                }
            },
            {
                "port_bindings": {
                    "6443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "6443"}]
                }
            },
            {
                "labels": {
                    "io.x-k8s.kind.cluster": "other",
                    "io.x-k8s.kind.role": "control-plane",
                }
            },
            {"labels": None},
            {"networks": {"bridge": {}}},
            {"status": "paused"},
            {"status": "restarting"},
        ):
            with self.subTest(change=change):
                self.assert_tasks_fail(check({**good, **change}), "Recrear el clúster")

    def test_registry_needs_its_labels_and_creation_settings(self) -> None:
        good = json.loads(container_read("kind-registry")["stdout"])

        def check(registry: dict[str, Any], name: str):
            return [
                self.inspect[self.DERIVE],
                {
                    "name": "Stand in for the recorded registry",
                    "ansible.builtin.set_fact": {"ax_lab_registry": registry},
                },
                self.ownership[name],
            ]

        self.assert_tasks_pass(check(good, self.REGISTRY_LABELS))
        self.assert_tasks_pass(check(good, self.REGISTRY))
        # Disconnected from kind: the apply reconnects it.
        self.assert_tasks_pass(check({**good, "networks": {}}, self.REGISTRY))
        # The manual lab's registry (hack/create-kind-cluster.sh, line 65).
        for labels in (
            {"created-by": "agent-substrate"},
            None,
            {"com.apptolast.managed-by": "ansible"},
        ):
            with self.subTest(labels=labels):
                self.assert_tasks_fail(
                    check({**good, "labels": labels}, self.REGISTRY_LABELS),
                    "Retirar el laboratorio manual",
                )
        for change in (
            {"image": "registry:3"},
            {
                "port_bindings": {
                    "5000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5001"}]
                }
            },
            {
                "port_bindings": {
                    "5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5001"}]
                }
            },
            {
                "port_bindings": {
                    "5000/tcp": [
                        {"HostIp": "127.0.0.1", "HostPort": "5001"},
                        {"HostIp": "::1", "HostPort": "5002"},
                    ]
                }
            },
            {"port_bindings": None},
            {"networks": {"kind": {}, "bridge": {}}},
            # The manual lab's anonymous volume.
            {
                "mounts": [
                    {
                        "Type": "volume",
                        "Name": "0" * 64,
                        "Destination": "/var/lib/registry",
                    }
                ]
            },
            {
                "mounts": [
                    {"Type": "bind", "Name": "", "Destination": "/var/lib/registry"}
                ]
            },
            {"mounts": []},
            {"status": "dead"},
        ):
            with self.subTest(change=change):
                self.assert_tasks_fail(
                    check({**good, **change}, self.REGISTRY), "Recrear el clúster"
                )

    def test_cluster_changes_follow_the_ownership_proof(self) -> None:
        names = list(self.cluster)
        mutations = [
            self.CREATE,
            self.RUN_REGISTRY,
            self.STATE,
            "Connect the local registry to the kind network when it is not on it",
            self.UPDATE,
            self.START,
        ]
        reprove = "Prove that the lab node is this role's before converging its limits"
        order = [
            "Require the rendered file to be the reviewed kind configuration",
            "Record which lab containers this apply creates",
            self.CREATE,
            # Only a read stands between the creation and its proof.
            "Read the lab node kind created",
            "Require the node after its creation",
            self.STATE,
            reprove,
            # The new node is capped before the registry is created or its
            # image pulled: a failure there no longer leaves it uncapped.
            self.UPDATE,
            self.RUN_REGISTRY,
            "Read the lab registry and node after creating them",
            "Require both lab containers after their creation",
            "Prove again that both lab containers are this role's",
            "Connect the local registry to the kind network when it is not on it",
            self.START,
            "Read the lab registry and node after converging them",
            self.VERIFY,
        ]
        self.assertEqual([name for name in names if name in order], order)
        self.assertEqual(names.index(self.STATE) - names.index(self.CREATE), 3)
        # Only the ownership proof stands between the proof and docker update.
        self.assertEqual(names.index(self.UPDATE) - names.index(self.STATE), 2)
        self.assertEqual(names.index(self.RUN_REGISTRY) - names.index(self.UPDATE), 1)
        for name in (reprove, "Prove again that both lab containers are this role's"):
            with self.subTest(import_task=name):
                self.assertEqual(
                    self.cluster[name],
                    {"name": name, "ansible.builtin.import_tasks": "ownership.yml"},
                )
        # Only a container the first inspection found absent is created, and
        # the proof is written only for a node this apply created.
        self.assertEqual(self.cluster[self.CREATE]["when"], "ax_lab_node_created")
        self.assertEqual(
            self.cluster[self.RUN_REGISTRY]["when"], "ax_lab_registry_created"
        )
        self.assertEqual(self.cluster[self.STATE]["when"], "ax_lab_node_created")
        self.assertEqual(
            self.cluster["Record which lab containers this apply creates"][
                "ansible.builtin.set_fact"
            ],
            {
                "ax_lab_node_created": "{{ ax_lab_node is none }}",
                "ax_lab_registry_created": "{{ ax_lab_registry is none }}",
            },
        )
        for name in mutations:
            with self.subTest(task=name):
                self.assertIn("when", self.cluster[name])

    def test_new_node_is_capped_before_the_registry_exists(self) -> None:
        # The node as kind v0.33.0 creates it: no memory, CPU or PID limit and
        # --restart=on-failure:1 (provision.go line 167), as a read-only
        # inspect of the manual node showed; the registry not created yet.
        uncapped = container_read(
            "kind-control-plane",
            memory=0,
            memory_swap=0,
            memory_reservation=0,
            nano_cpus=0,
            pids_limit=None,
            restart_policy={"Name": "on-failure", "MaximumRetryCount": 1},
        )
        update = self.cluster[self.UPDATE]
        self.assertEqual(update["loop"], "{{ ax_lab_containers }}")
        for label, reads, expected in (
            (
                "first apply",
                [container_absent("kind-registry"), uncapped],
                ["kind-control-plane"],
            ),
            (
                "converged node, registry still absent",
                [
                    container_absent("kind-registry"),
                    container_read("kind-control-plane"),
                ],
                [],
            ),
        ):
            with self.subTest(case=label):
                self.assert_tasks_pass(
                    [
                        *self.facts(*reads),
                        {
                            "name": "Select the containers docker update targets",
                            "ansible.builtin.set_fact": {
                                "probe_updated": (
                                    "{{ probe_updated | default([]) + [item.name] }}"
                                )
                            },
                            "loop": update["loop"],
                            "when": update["when"],
                        },
                        probe("probe_updated | default([]) == expected"),
                    ],
                    expected=expected,
                )

    def test_cluster_never_runs_substrate_or_deletes_anything(self) -> None:
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
                self.assertNotIn("create-kind-cluster", text)
                self.assertNotIn("hack/", text)
                for task in tasks:
                    argv = (task.get("ansible.builtin.command") or {}).get("argv")
                    if not isinstance(argv, list):
                        continue
                    for forbidden in ("delete", "rm", "kill", "stop", "restart"):
                        self.assertNotIn(forbidden, argv[1:3], task["name"])
                self.assertNotIn("ansible.builtin.shell", text)

    def test_every_cluster_command_reads_or_changes_only_on_drift(self) -> None:
        reads = 0
        tasks = [
            *yaml.safe_load((ROOT / CLUSTER).read_text()),
            *yaml.safe_load((ROOT / NODE).read_text()),
        ]
        for task in tasks:
            command = task.get("ansible.builtin.command")
            if command is None:
                continue
            with self.subTest(task=task["name"]):
                if task["changed_when"] is True:
                    self.assertIn("when", task)
                    self.assertTrue(
                        task["name"].startswith(
                            (
                                "Create",
                                "Connect",
                                "Converge",
                                "Start",
                                "Export",
                                "Enable",
                                "Write",
                                "Apply",
                            )
                        )
                    )
                else:
                    reads += 1
                    self.assertIs(task["changed_when"], False)
                    self.assertNotIn("when", task)
        self.assertEqual(reads, 10)

    def test_kind_creates_the_cluster_from_the_pinned_inputs_only(self) -> None:
        task = self.cluster[self.CREATE]
        self.assertEqual(
            task["ansible.builtin.command"]["argv"],
            [
                "{{ ax_lab_bin_directory }}/kind",
                "create",
                "cluster",
                "--name",
                "{{ ax_lab.cluster.name }}",
                "--config",
                "{{ ax_lab_kind_config_path }}",
                "--image",
                "{{ ax_lab.images.kind_node }}",
                "--kubeconfig",
                "{{ ax_lab_kubeconfig_path }}",
            ],
        )
        # No --wait (kind's default, 0s): `docker update` follows kubeadm
        # init at once, and the role's own waits run under the limits.
        self.assertFalse(
            any(
                str(argument).startswith(("--wait", "--retain"))
                for argument in task["ansible.builtin.command"]["argv"]
            )
        )
        self.assertEqual(
            task["environment"],
            {
                "HOME": "{{ ax_lab_home_directory }}",
                "KUBECONFIG": "{{ ax_lab_kubeconfig_path }}",
            },
        )
        self.assertIs(task["changed_when"], True)
        render = self.cluster["Render the reviewed kind configuration"]
        self.assertEqual(
            render["ansible.builtin.template"],
            {
                "src": "kind-config.yaml.j2",
                "dest": "{{ ax_lab_kind_config_path }}",
                "owner": "root",
                "group": "root",
                "mode": "0640",
            },
        )
        name = "Require the rendered file to be the reviewed kind configuration"
        digest = self.variables["ax_lab_kind_config_digest"]["stdout"]
        stat = {"isreg": True, "islnk": False, "checksum": digest}
        self.assert_task_accepts(
            CLUSTER,
            name,
            {**self.variables, "ax_lab_kind_config_file": {"stat": stat}},
        )
        for change in ({"checksum": "0" * 64}, {"islnk": True}):
            with self.subTest(change=change):
                self.assert_task_rejects(
                    CLUSTER,
                    name,
                    {
                        **self.variables,
                        "ax_lab_kind_config_file": {"stat": {**stat, **change}},
                    },
                    "differs from the kind configuration the validator renders",
                )

    def test_ansible_renders_the_same_kind_config_as_the_validator(self) -> None:
        validator = load_script("validate_ax_lab_kind", "scripts/validate-ax-lab.py")
        expected = validator.render_kind_config(self.lab)
        template = ROLE / "templates/kind-config.yaml.j2"
        # No trim: the file kind reads must hash to the validator's digest.
        self.assert_tasks_pass(
            [
                probe(f"lookup('ansible.builtin.template', '{template}') == expected"),
                probe(
                    f"lookup('ansible.builtin.template', '{template}')"
                    " | hash('sha256') == ax_lab_kind_config_digest.stdout"
                ),
            ],
            expected=expected,
        )

    def test_registry_is_created_on_loopback_with_its_limits(self) -> None:
        argv = self.cluster[self.RUN_REGISTRY]["ansible.builtin.command"]["argv"]
        expected = [
            "/usr/bin/docker",
            "run",
            "--detach",
            "--name",
            "kind-registry",
            "--restart",
            "no",
            "--memory",
            str(256 * 1048576),
            "--memory-swap",
            str(256 * 1048576),
            "--memory-reservation",
            str(128 * 1048576),
            "--cpus",
            "0.500",
            "--pids-limit",
            "256",
            "--label",
            "com.apptolast.managed-by=ansible",
            "--label",
            "com.apptolast.ax-lab=registry",
            "--network",
            "kind",
            "--mount",
            "type=volume,source=ax-lab-registry,target=/var/lib/registry",
            "--publish=127.0.0.1:5001:5000",
            "--publish=[::1]:5001:5000",
            self.lab["images"]["registry"],
        ]
        self.assert_tasks_pass(
            [
                self.inspect[self.DERIVE],
                {
                    "name": "Render the reviewed argv",
                    "ansible.builtin.set_fact": {"probe_argv": argv},
                },
                probe("probe_argv == expected"),
                # The labels it sets are the ones ownership.yml requires.
                probe(
                    "ax_lab_registry_labels == {'com.apptolast.managed-by':"
                    " 'ansible', 'com.apptolast.ax-lab': 'registry'}"
                ),
            ],
            expected=expected,
        )
        self.assertIs(self.cluster[self.RUN_REGISTRY]["changed_when"], True)

    def test_limits_are_updated_only_on_drift_and_verified(self) -> None:
        task = self.cluster[self.UPDATE]
        self.assertEqual(task["loop"], "{{ ax_lab_containers }}")
        self.assertEqual(task["when"], "item.name in ax_lab_limits_drift")
        argv = task["ansible.builtin.command"]["argv"]
        self.assertEqual(argv[:2], ["/usr/bin/docker", "update"])
        flags = [value for value in argv if str(value).startswith("--")]
        self.assertEqual(
            flags,
            [
                "--memory",
                "--memory-swap",
                "--memory-reservation",
                "--cpus",
                "--pids-limit",
                "--restart",
            ],
        )
        self.assertEqual(argv[-1], "{{ item.name }}")
        # The --cpus value, rendered for both containers.
        self.assert_tasks_pass(
            [
                self.inspect[self.DERIVE],
                {
                    "name": "Render the --cpus values",
                    "ansible.builtin.set_fact": {
                        "probe_cpus": (
                            "{{ ax_lab_containers | map(attribute='resources')"
                            " | map(attribute='cpu_limit_millicores')"
                            " | map('string') | list }}"
                        )
                    },
                },
                {
                    "name": "Render each --cpus argument as the task does",
                    "ansible.builtin.set_fact": {
                        "probe_rendered": "{{ probe_rendered | default([]) + [rendered] }}"
                    },
                    "loop": "{{ ax_lab_containers }}",
                    "vars": {"rendered": argv[argv.index("--cpus") + 1]},
                },
                probe("probe_rendered == ['0.500', '2.000']"),
            ]
        )
        # B3: cluster.yml starts only the registry; node.yml starts the node,
        # after images.yml.
        start = self.cluster[self.START]
        self.assertEqual(
            start["when"], 'ax_lab_registry.status in ["created", "exited"]'
        )
        self.assertEqual(
            start["ansible.builtin.command"]["argv"],
            ["/usr/bin/docker", "start", "{{ ax_lab.registry.container }}"],
        )
        node_start = self.node[self.NODE_START]
        self.assertEqual(
            node_start["when"], 'ax_lab_node.status in ["created", "exited"]'
        )
        self.assertEqual(
            node_start["ansible.builtin.command"]["argv"],
            ["/usr/bin/docker", "start", "{{ ax_lab.cluster.node_container }}"],
        )
        self.assertIs(node_start["changed_when"], True)
        self.assertEqual(
            list(self.node)[:3],
            [
                self.NODE_START,
                "Read the lab registry and node after starting the node",
                self.NODE_VERIFY,
            ],
        )
        for status, starts in (("exited", True), ("created", True), ("running", False)):
            with self.subTest(node=status):
                self.assert_tasks_pass(
                    [
                        *self.facts(
                            container_read("kind-registry"),
                            container_read("kind-control-plane", status=status),
                        ),
                        probe(
                            node_start["when"]
                            if starts
                            else f"not ({node_start['when']})"
                        ),
                    ]
                )
        converged = self.facts(*self.converged())
        self.assert_tasks_pass([*converged, self.cluster[self.VERIFY]])
        self.assert_tasks_pass([*converged, self.node[self.NODE_VERIFY]])
        # A node that a reboot left stopped passes cluster.yml, which no longer
        # needs it running, and only node.yml requires it running.
        stopped = self.facts(
            container_read("kind-registry"),
            container_read("kind-control-plane", status="exited"),
        )
        self.assert_tasks_pass([*stopped, self.cluster[self.VERIFY]])
        self.assert_tasks_fail(
            [*stopped, self.node[self.NODE_VERIFY]],
            "does not run with its reviewed limits",
        )
        for (
            label,
            reads,
        ) in (
            (
                "stopped registry",
                [
                    container_read("kind-registry", status="exited"),
                    container_read("kind-control-plane"),
                ],
            ),
            (
                "drifted registry",
                [
                    container_read("kind-registry", memory=0),
                    container_read("kind-control-plane"),
                ],
            ),
            (
                "registry also on the default bridge",
                [
                    container_read(
                        "kind-registry", networks={"kind": {}, "bridge": {}}
                    ),
                    container_read("kind-control-plane"),
                ],
            ),
            (
                "node absent",
                [
                    container_read("kind-registry"),
                    container_absent("kind-control-plane"),
                ],
            ),
        ):
            with self.subTest(case=label):
                self.assert_tasks_fail(
                    [*self.facts(*reads), self.cluster[self.VERIFY]],
                    "do not carry their reviewed limits",
                )

    def test_state_file_records_the_created_node(self) -> None:
        node = json.loads(container_read("kind-control-plane")["stdout"])
        task = self.cluster[self.STATE]
        copy_args = task["ansible.builtin.copy"]
        self.assertEqual(
            {key: value for key, value in copy_args.items() if key != "content"},
            {
                "dest": "{{ ax_lab_cluster_state_path }}",
                "owner": "root",
                "group": "root",
                "mode": "0600",
            },
        )
        expected = {
            "schema_version": 1,
            "cluster_name": "kind",
            "node_container": "kind-control-plane",
            "node_container_id": node["id"],
            "node_image": self.lab["images"]["kind_node"],
            "kind_version": "v0.33.0",
            "kind_config_sha256": self.variables["ax_lab_kind_config_digest"]["stdout"],
        }
        self.assert_tasks_pass(
            [
                self.inspect[self.DERIVE],
                {
                    "name": "Render the state file content",
                    "ansible.builtin.set_fact": {"probe_state": copy_args["content"]},
                },
                probe("probe_state | from_json == expected"),
            ],
            ax_lab_node=node,
            expected=expected,
        )

    def test_node_settings_converge_only_where_they_differ(self) -> None:
        for read, change, verify, expected_when in (
            (
                "Read proxy_arp inside the lab node",
                "Enable proxy_arp inside the lab node only when it is off",
                "Verify proxy_arp inside the lab node",
                'ax_lab_proxy_arp_before.stdout != "1"',
            ),
            (
                "Read proxy_ndp inside the lab node",
                "Enable proxy_ndp inside the lab node only when it is off",
                "Verify proxy_ndp inside the lab node",
                'ax_lab_proxy_ndp_before.stdout != "1"',
            ),
            (
                "Read the registry mirror configuration inside the lab node",
                "Write the registry mirror configuration inside the lab node",
                "Verify the registry mirror configuration inside the lab node",
                "ax_lab_registry_mirror_before.stdout != ax_lab_registry_mirror",
            ),
        ):
            with self.subTest(task=change):
                names = list(self.node)
                self.assertLess(names.index(read), names.index(change))
                self.assertLess(names.index(change), names.index(verify))
                self.assertEqual(self.node[change]["when"], expected_when)
        self.assertEqual(
            self.node["Enable proxy_arp inside the lab node only when it is off"][
                "ansible.builtin.command"
            ]["argv"],
            [
                "/usr/bin/docker",
                "exec",
                "{{ ax_lab.cluster.node_container }}",
                "/usr/sbin/sysctl",
                "-w",
                "net.ipv4.conf.all.proxy_arp=1",
            ],
        )
        # Substrate sets proxy_ndp too, with -e (hack/create-kind-cluster.sh,
        # line 181 at the pinned commit); the reads use -e as well, so the
        # verification is what stops a kernel without the key.
        self.assertEqual(
            self.node["Enable proxy_ndp inside the lab node only when it is off"][
                "ansible.builtin.command"
            ]["argv"],
            [
                "/usr/bin/docker",
                "exec",
                "{{ ax_lab.cluster.node_container }}",
                "/usr/sbin/sysctl",
                "-e",
                "-w",
                "net.ipv6.conf.all.proxy_ndp=1",
            ],
        )
        for read, register in (
            ("Read proxy_arp inside the lab node", "ax_lab_proxy_arp_before"),
            (
                "Read proxy_arp inside the lab node after convergence",
                "ax_lab_proxy_arp",
            ),
            ("Read proxy_ndp inside the lab node", "ax_lab_proxy_ndp_before"),
            (
                "Read proxy_ndp inside the lab node after convergence",
                "ax_lab_proxy_ndp",
            ),
        ):
            with self.subTest(read=read):
                key = register.removeprefix("ax_lab_").removesuffix("_before")
                self.assertEqual(self.node[read]["register"], register)
                self.assertEqual(
                    self.node[read]["ansible.builtin.command"]["argv"],
                    [
                        "/usr/bin/docker",
                        "exec",
                        "{{ ax_lab.cluster.node_container }}",
                        "/usr/sbin/sysctl",
                        "-n",
                        *(["-e"] if key == "proxy_ndp" else []),
                        (
                            "net.ipv6.conf.all.proxy_ndp"
                            if key == "proxy_ndp"
                            else "net.ipv4.conf.all.proxy_arp"
                        ),
                    ],
                )
        for key, family in (("proxy_arp", "ipv4"), ("proxy_ndp", "ipv6")):
            name = f"Verify {key} inside the lab node"
            with self.subTest(verify=key):
                self.assert_task_accepts(
                    NODE,
                    name,
                    {**self.variables, f"ax_lab_{key}": {"stdout": "1"}},
                )
                for stdout in ("0", ""):
                    self.assert_task_rejects(
                        NODE,
                        name,
                        {**self.variables, f"ax_lab_{key}": {"stdout": stdout}},
                        f"net.{family}.conf.all.{key} is {stdout} inside",
                    )
        apply = self.node[
            "Apply the local registry hosting ConfigMap only when it differs"
        ]
        self.assertEqual(
            apply["ansible.builtin.command"]["argv"][-6:],
            ["apply", "--server-side", "--field-manager", "ax-lab", "--filename", "-"],
        )
        hosting = {
            "localRegistryHosting.v1": 'host: "localhost:5001"\n'
            'help: "https://kind.sigs.k8s.io/docs/user/local-registry/"\n'
        }
        for stdout, expected in (
            ("", True),
            (json.dumps({"data": hosting}), False),
            (json.dumps({"data": {}}), True),
            (json.dumps({"metadata": {}}), True),
        ):
            with self.subTest(stdout=stdout):
                completed = run_reviewed_tasks(
                    [
                        (
                            probe(apply["when"])
                            if expected
                            else probe(f"not ({apply['when']})")
                        )
                    ],
                    {
                        "ax_lab_registry_hosting_before": {"stdout": stdout},
                        "ax_lab_registry_hosting_data": hosting,
                    },
                )
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
        self.assert_tasks_pass(
            [
                self.inspect[self.DERIVE],
                {
                    "name": "Render the ConfigMap",
                    "ansible.builtin.set_fact": {
                        "probe_manifest": apply["ansible.builtin.command"]["stdin"]
                    },
                },
                probe(
                    "probe_manifest | from_json == {'apiVersion': 'v1', 'kind':"
                    " 'ConfigMap', 'metadata': {'name': 'local-registry-hosting',"
                    " 'namespace': 'kube-public'}, 'data': ax_lab_registry_hosting_data}"
                ),
            ]
        )

    def test_kubeconfig_is_the_labs_own_root_only_file(self) -> None:
        name = "Require a root-only regular lab kubeconfig"
        good = {
            "exists": True,
            "isreg": True,
            "islnk": False,
            "uid": 0,
            "gid": 0,
            "mode": "0600",
        }
        self.assert_task_accepts(
            NODE, name, {**self.variables, "ax_lab_kubeconfig": {"stat": good}}
        )
        for change in (
            {"mode": "0644"},
            {"uid": 1001},
            {"islnk": True},
            {"exists": False},
        ):
            with self.subTest(change=change):
                self.assert_task_rejects(
                    NODE,
                    name,
                    {
                        **self.variables,
                        "ax_lab_kubeconfig": {"stat": {**good, **change}},
                    },
                    "is not a root:root 0600 regular file",
                )
        # Every kubectl call uses the lab's kubeconfig and HOME, so its cache
        # stays under the lab tree and never in /root, and gives up on a
        # request after 10 s (kubectl's default is never), so the retries
        # bound each wait.
        kubectl_tasks = 0
        for path in role_task_files():
            for task in yaml.safe_load(path.read_text(encoding="utf-8")):
                argv = (task.get("ansible.builtin.command") or {}).get("argv")
                if not isinstance(argv, list) or not any(
                    "kubectl" in str(argument) for argument in argv
                ):
                    continue
                kubectl_tasks += 1
                with self.subTest(task=task["name"]):
                    self.assertEqual(path.name, "node.yml")
                    self.assertEqual(argv[0], "{{ ax_lab_bin_directory }}/kubectl")
                    self.assertEqual(
                        argv[1:7],
                        [
                            "--kubeconfig",
                            "{{ ax_lab_kubeconfig_path }}",
                            "--context",
                            "kind-{{ ax_lab.cluster.name }}",
                            "--request-timeout",
                            "10s",
                        ],
                    )
                    self.assertEqual(
                        task["environment"], {"HOME": "{{ ax_lab_home_directory }}"}
                    )
        self.assertEqual(kubectl_tasks, 5)

    def test_check_mode_reports_the_planned_changes(self) -> None:
        describe = self.inspect[
            "Describe what an apply would change in the lab cluster"
        ]
        tail = (
            "then, only where they differ: proxy_arp, proxy_ndp, the registry"
            " mirror and the local-registry-hosting ConfigMap inside the node"
        )
        for label, reads, head in (
            ("converged", self.converged(), []),
            (
                "fresh host",
                [
                    container_absent("kind-registry"),
                    container_absent("kind-control-plane"),
                ],
                [
                    "kind create cluster --name kind from "
                    + self.lab["images"]["kind_node"],
                    "docker update kind-control-plane",
                    "create kind-registry from " + self.lab["images"]["registry"],
                ],
            ),
            (
                "after a reboot",
                [
                    container_read("kind-registry", status="exited"),
                    container_read(
                        "kind-control-plane", status="exited", pids_limit=None
                    ),
                ],
                [
                    "docker update kind-control-plane",
                    "docker start kind-registry",
                    "docker start kind-control-plane",
                ],
            ),
        ):
            with self.subTest(case=label):
                self.assert_tasks_pass(
                    [
                        *self.facts(*reads),
                        describe,
                        probe("ax_lab_cluster_plan == expected"),
                    ],
                    expected=[*head, tail],
                )


if __name__ == "__main__":
    unittest.main()
