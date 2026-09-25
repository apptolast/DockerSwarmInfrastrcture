"""Contract of the AX lab host prerequisites (playbook `ax-lab`)."""

from __future__ import annotations

import ast
import copy
import importlib.util
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

from ansible_task_harness import AnsibleTaskAssertions, run_task_definition

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/ax-lab.yml"
PLAYBOOK = ROOT / "ansible/playbooks/ax-lab.yml"
ROLE = ROOT / "ansible/roles/ax_lab"
MAIN = "ansible/roles/ax_lab/tasks/main.yml"
HOST = "ansible/roles/ax_lab/tasks/host.yml"
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
        "platform_install_root": "/opt/dockerswarm",
        "role_path": str(ROLE),
    }


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

    def test_playbook_takes_the_lock_first_without_capacity_preflight(self) -> None:
        play = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
        self.assertEqual(len(play), 1)
        play = play[0]
        self.assertEqual(play["hosts"], "swarm_managers")
        self.assertIs(play["become"], True)
        self.assertIs(play["gather_facts"], False)
        self.assertEqual(play["serial"], 1)
        self.assertIs(play["any_errors_fatal"], True)
        self.assertEqual(
            play["vars_files"],
            [
                "../../config/platform.yml",
                "../../config/ax-lab.yml",
                "../group_vars/all.yml",
            ],
        )
        self.assertEqual(
            [item["role"] for item in play["roles"]],
            ["operation_lock_guard", "ax_lab", "deployment_metadata"],
        )

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
        self.assertIs(self.document["ax_lab_privileged_node_accepted"], False)
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
        for value in (2, True, "1"):
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
        accepted = self.mutated(
            lambda document: document.update(ax_lab_privileged_node_accepted=True)
        )
        self.module.validate_catalog(accepted, self.reserved)

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
        cases = [
            overridden,
            {**self.variables, "ax_lab_privileged_node_accepted": "false"},
            {**self.variables, "ax_lab_privileged_node_accepted": True},
            # 0 == False in Jinja: only the boolean test refuses it.
            {**self.variables, "ax_lab_privileged_node_accepted": 0},
            {**self.variables, "ax_lab_sysctl_path": "/etc/sysctl.d/99-other.conf"},
            {**self.variables, "ax_lab_bin_directory": "/usr/local/bin"},
            {**self.variables, "ax_lab_architecture": "aarch64"},
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
            [item["path"] for item in task["loop"]],
            [
                "/opt",
                "{{ platform_install_root }}",
                "{{ ax_lab.install_root }}",
                "{{ ax_lab_bin_directory }}",
                "{{ ax_lab_bin_directory }}/kind",
                "{{ ax_lab_bin_directory }}/kubectl",
                "/etc",
                "/etc/sysctl.d",
                "{{ ax_lab_sysctl_path }}",
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
        for path in role_task_files():
            for task in yaml.safe_load(path.read_text(encoding="utf-8")):
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
                self.assertNotIn("slurp", text)


if __name__ == "__main__":
    unittest.main()
