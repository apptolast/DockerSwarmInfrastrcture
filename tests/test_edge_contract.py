"""Edge (Traefik) image channel gates: static contract and live identity."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

import yaml

from ansible_task_harness import REPOSITORY_ROOT, AnsibleTaskAssertions


def load_script(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, REPOSITORY_ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


channels = load_script("validate_image_channels", "scripts/validate-image-channels.py")

RESOLVED = "sha256:" + ("1" * 64)
KEPT = "sha256:" + ("2" * 64)
FOREIGN = "sha256:" + ("3" * 64)
EDGE_NETWORKS = {
    "kropia": "apptolast-edge-kropia",
    "minecraft-stats": "apptolast-edge-minecraft-stats",
    "n8n": "apptolast-edge-n8n",
    "openclaw": "apptolast-edge-openclaw",
    "passbolt": "apptolast-edge-passbolt",
    "portfolio-alberto": "apptolast-edge-portfolio-alberto",
    "portfolio-pablo": "apptolast-edge-portfolio-pablo",
    "shlink": "apptolast-edge-shlink",
}


def traefik_entry(reference: str) -> dict[str, Any]:
    """Derive a real Traefik channel entry for reference."""
    document = channels.load_unique_yaml(REPOSITORY_ROOT / "config/image-channels.yml")
    raw = next(
        item
        for item in document["image_channel_services"]
        if (item["stack"], item["service"]) == ("edge", "traefik")
    )
    return channels.derive_entry(
        dict(raw, reference=reference),
        channels.load_baselines(REPOSITORY_ROOT),
    )


class TraefikLiveIdentityGateTests(AnsibleTaskAssertions, unittest.TestCase):
    DEPLOY = "ansible/roles/edge/tasks/deploy.yml"
    IDENTITY = "Verify the deployed Traefik service identity"
    IDENTITY_MESSAGE = "The deployed Traefik service differs from the reviewed spec."
    PRECONDITION = "Require a live Traefik hold to run its reviewed identity"
    PRECONDITION_MESSAGE = "Traefik runs an image outside its unchanged hold entry"

    @classmethod
    def setUpClass(cls) -> None:
        channel_map = channels.load_channel_map(REPOSITORY_ROOT)["services"]
        cls.hold = channel_map["edge"]["traefik"]
        cls.channel = traefik_entry("docker.io/library/traefik:v3")

    def inspect(self, image: str, label: str = "") -> str:
        return json.dumps(
            [
                {
                    "Spec": {
                        "Labels": {"com.docker.stack.image": label},
                        "Mode": {"Replicated": {"Replicas": 1}},
                        "UpdateConfig": {"Order": "stop-first"},
                        "RollbackConfig": {"Order": "stop-first"},
                        "TaskTemplate": {
                            "ContainerSpec": {
                                "Image": image,
                                "User": "65532:65532",
                                "ReadOnly": True,
                                "Configs": [{"ConfigName": "a"}, {"ConfigName": "b"}],
                                "Secrets": [{"SecretName": "cloudflare-token"}],
                                "Mounts": [
                                    {
                                        "Type": "bind",
                                        "Source": "/srv/edge/traefik",
                                        "Target": "/data",
                                    }
                                ],
                            },
                            "Networks": [{"Target": "a"}, {"Target": "b"}],
                        },
                        "EndpointSpec": {"Ports": [{}, {}]},
                    }
                }
            ]
        )

    def identity(
        self,
        entry: dict[str, Any],
        live: str,
        before: str = "",
    ) -> dict[str, Any]:
        return {
            "image_channels_map": {"edge": {"traefik": entry}},
            "edge_deployed_traefik_service": {"stdout": self.inspect(live)},
            "edge_traefik_image_before_deploy": before,
            "image_preflight_channel_resolutions": {
                self.channel["reference"]: self.channel["reference"] + "@" + RESOLVED
            },
            "edge_traefik_runtime_uid": 65532,
            "edge_traefik_runtime_gid": 65532,
            "edge_traefik_cloudflare_secret_name": "cloudflare-token",
            "edge_required_networks": ["a", "b"],
            "edge_state_root": "/srv/edge",
        }

    def test_identity_gate_accepts_the_reviewed_hold_and_verified_heads(self) -> None:
        self.assert_task_accepts(
            self.DEPLOY, self.IDENTITY, self.identity(self.hold, self.hold["spec_exact"])
        )
        # A new channel head must be the one the preflight resolved ...
        self.assert_task_accepts(
            self.DEPLOY,
            self.IDENTITY,
            self.identity(self.channel, "traefik:v3@" + RESOLVED),
        )
        # ... or the live digest the CLI kept for an unchanged label.
        self.assert_task_accepts(
            self.DEPLOY,
            self.IDENTITY,
            self.identity(self.channel, "traefik:v3@" + KEPT, "traefik:v3@" + KEPT),
        )

    def test_identity_gate_rejects_a_hold_on_another_digest(self) -> None:
        wrong = self.hold["spec_exact"].split("@")[0] + "@" + FOREIGN
        self.assert_task_rejects(
            self.DEPLOY,
            self.IDENTITY,
            self.identity(self.hold, wrong),
            self.IDENTITY_MESSAGE,
        )

    def test_identity_gate_anchors_the_channel_repository_and_tag(self) -> None:
        for live in (
            "traefik:v3.7@" + RESOLVED,
            "docker.io/library/traefik:v3@" + RESOLVED,
            "evil/traefik:v3@" + RESOLVED,
            "traefik:v3@" + RESOLVED + "0",
        ):
            with self.subTest(live=live):
                self.assert_task_rejects(
                    self.DEPLOY,
                    self.IDENTITY,
                    self.identity(self.channel, live),
                    self.IDENTITY_MESSAGE,
                )

    def test_identity_gate_rejects_a_head_the_preflight_did_not_verify(self) -> None:
        for before in ("", "traefik:v3@" + KEPT):
            with self.subTest(before=before):
                self.assert_task_rejects(
                    self.DEPLOY,
                    self.IDENTITY,
                    self.identity(self.channel, "traefik:v3@" + FOREIGN, before),
                    self.IDENTITY_MESSAGE,
                )

    def test_identity_gate_applies_the_hold_rule_to_a_hold(self) -> None:
        # A hold whose pattern would accept any v3 digest still needs its
        # exact identity: the mode, not the pattern, selects the rule.
        loose_hold = dict(self.channel, mode="hold", spec_exact="traefik:v3@" + KEPT)
        self.assert_task_rejects(
            self.DEPLOY,
            self.IDENTITY,
            self.identity(loose_hold, "traefik:v3@" + RESOLVED),
            self.IDENTITY_MESSAGE,
        )
        unknown_mode = dict(self.channel, mode="pinned")
        self.assert_task_rejects(
            self.DEPLOY,
            self.IDENTITY,
            self.identity(unknown_mode, "traefik:v3@" + RESOLVED),
            self.IDENTITY_MESSAGE,
        )

    def precondition(self, rc: int, live: str, label: str, stderr: str = "") -> dict[str, Any]:
        return {
            "image_channels_map": {"edge": {"traefik": self.hold}},
            "edge_traefik_before_deploy": {
                "rc": rc,
                "stdout": self.inspect(live, label) if rc == 0 else "[]",
                "stderr": stderr,
            },
        }

    def test_precondition_accepts_a_reviewed_changed_or_absent_service(self) -> None:
        reference = self.hold["reference"]
        for variables in (
            self.precondition(0, self.hold["spec_exact"], reference),
            # A changed entry is re-resolved by the CLI: nothing to keep.
            self.precondition(0, "traefik@" + FOREIGN, "traefik:v3.3.6"),
            self.precondition(1, "", "", "Error: no such service: edge_traefik"),
        ):
            with self.subTest(variables=variables["edge_traefik_before_deploy"]):
                self.assert_task_accepts(self.DEPLOY, self.PRECONDITION, variables)

    def test_precondition_rejects_a_hold_moved_outside_git(self) -> None:
        self.assert_task_rejects(
            self.DEPLOY,
            self.PRECONDITION,
            self.precondition(0, "traefik@" + FOREIGN, self.hold["reference"]),
            self.PRECONDITION_MESSAGE,
        )

    def test_precondition_rejects_an_unreadable_service(self) -> None:
        self.assert_task_rejects(
            self.DEPLOY,
            self.PRECONDITION,
            self.precondition(
                1, "", "", "Cannot connect to the Docker daemon at unix:///run/x"
            ),
            self.PRECONDITION_MESSAGE,
        )


class EdgeInputGateTests(AnsibleTaskAssertions, unittest.TestCase):
    MAIN = "ansible/roles/edge/tasks/main.yml"
    TASK = "Verify the edge deployment inputs"
    MESSAGE = "Traefik image, hostname, email, secret or network is invalid."

    @classmethod
    def setUpClass(cls) -> None:
        group_vars = yaml.safe_load(
            (REPOSITORY_ROOT / "ansible/group_vars/all.yml").read_text(encoding="utf-8")
        )
        cls.pin = group_vars["edge_traefik_image"]

    def inputs(self, reference: str, **extra_entries: Any) -> dict[str, Any]:
        return {
            "edge_traefik_image": self.pin,
            "image_channels_map": {
                "edge": {"traefik": {"reference": reference}, **extra_entries}
            },
            "edge_traefik_version": "3.7.9",
            "edge_traefik_hostname": "edge.apptolast.com",
            "edge_traefik_acme_email": "ops@example.com",
            "edge_traefik_cloudflare_secret_name": "cloudflare_dns_api_token_v2",
            "edge_traefik_acme_storage_filename": "acme.json",
            "edge_monitoring_network": "apptolast-edge-monitoring",
            "edge_networks": EDGE_NETWORKS,
            "edge_application_networks": {
                "organizationweb": "apptolast-edge-organizationweb"
            },
            "organizationweb": {
                "hostname": "organizacion.apptolast.com",
                "edge_network": "apptolast-edge-organizationweb",
            },
            "edge_deployment_profile": "production",
            "edge_traefik_acme_ca_server": (
                "https://acme-v02.api.letsencrypt.org/directory"
            ),
            "edge_letsencrypt_staging_ca_bundle_sha256": "a" * 64,
        }

    def test_the_pin_and_the_v3_channel_are_accepted(self) -> None:
        for reference in (self.pin, "docker.io/library/traefik:v3", "traefik:v3"):
            with self.subTest(reference=reference):
                self.assert_task_accepts(self.MAIN, self.TASK, self.inputs(reference))

    def test_other_traefik_channels_are_rejected(self) -> None:
        for reference in (
            "docker.io/library/traefik:v3.7",
            "traefik:v4",
            "traefik:latest",
            "docker.io/library/traefik@" + FOREIGN,
        ):
            with self.subTest(reference=reference):
                self.assert_task_rejects(
                    self.MAIN, self.TASK, self.inputs(reference), self.MESSAGE
                )

    def test_a_second_edge_channel_entry_is_rejected(self) -> None:
        self.assert_task_rejects(
            self.MAIN,
            self.TASK,
            self.inputs(self.pin, sidecar={"reference": "traefik:v3"}),
            self.MESSAGE,
        )


class EdgeStaticContractTests(unittest.TestCase):
    """scripts/validate-contract.py against a disposable copy of its inputs."""

    INPUTS = (
        "ansible/group_vars/all.yml",
        "ansible/inventory/production/hosts.yml",
        "infra/terraform/cloudflare/apptolast-dns/dns.tf",
        "scripts/validate-contract.py",
        "scripts/validate-image-channels.py",
    )

    @classmethod
    def setUpClass(cls) -> None:
        # Same docker-free render validate-iac.sh runs before this validator.
        subprocess.run(
            [
                str(REPOSITORY_ROOT / ".venv/bin/ansible-playbook"),
                "--inventory",
                "ansible/inventory/local/hosts.yml",
                "ansible/playbooks/render-edge.yml",
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )

    def run_contract(
        self,
        mutate: Callable[[Path], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(REPOSITORY_ROOT / "config", root / "config")
            shutil.copytree(REPOSITORY_ROOT / ".build/edge", root / ".build/edge")
            for relative in self.INPUTS:
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPOSITORY_ROOT / relative, root / relative)
            if mutate is not None:
                mutate(root)
            return subprocess.run(
                [sys.executable, str(root / "scripts/validate-contract.py")],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )

    @staticmethod
    def edit_yaml(path: Path, change: Callable[[Any], None]) -> None:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        change(document)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    @classmethod
    def set_traefik_image(cls, root: Path, image: str) -> None:
        def change(stack: Any) -> None:
            stack["services"]["traefik"]["image"] = image

        cls.edit_yaml(root / ".build/edge/stack.yml", change)

    @classmethod
    def set_traefik_channel(cls, root: Path, reference: str) -> None:
        def change(document: Any) -> None:
            entry = next(
                item
                for item in document["image_channel_services"]
                if (item["stack"], item["service"]) == ("edge", "traefik")
            )
            entry["reference"] = reference

        cls.edit_yaml(root / "config/image-channels.yml", change)
        cls.set_traefik_image(root, reference)

    def assert_contract_rejects(
        self,
        mutate: Callable[[Path], None],
        message: str,
    ) -> None:
        completed = self.run_contract(mutate)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn(message, completed.stderr)

    def test_reviewed_render_is_accepted(self) -> None:
        completed = self.run_contract()
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_reviewed_v3_channel_is_accepted(self) -> None:
        completed = self.run_contract(
            lambda root: self.set_traefik_channel(root, "docker.io/library/traefik:v3")
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_rendered_image_that_drifts_from_its_entry_is_rejected(self) -> None:
        self.assert_contract_rejects(
            lambda root: self.set_traefik_image(root, "docker.io/library/traefik:v3"),
            "the rendered Traefik image differs from its image channel entry",
        )

    def test_flipped_autoupdate_label_is_rejected(self) -> None:
        def flip(root: Path) -> None:
            def change(stack: Any) -> None:
                stack["services"]["traefik"]["deploy"]["labels"][
                    "apptolast.autoupdate"
                ] = "true"

            self.edit_yaml(root / ".build/edge/stack.yml", change)

        self.assert_contract_rejects(
            flip,
            "the rendered Traefik autoupdate label differs from its channel entry",
        )

    def test_minor_pinned_traefik_channel_is_rejected(self) -> None:
        completed = self.run_contract(
            lambda root: self.set_traefik_channel(
                root, "docker.io/library/traefik:v3.7"
            )
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("image channel map:", completed.stderr)
        self.assertIn("not the reviewed major channel", completed.stderr)

    def test_channel_outside_the_pin_or_traefik_v3_form_is_rejected(self) -> None:
        # `library/traefik:v3` normalizes to the right repository, so the
        # channel validator accepts it; this contract only accepts two forms.
        self.assert_contract_rejects(
            lambda root: self.set_traefik_channel(root, "library/traefik:v3"),
            "the Traefik image channel is neither the pin nor traefik:v3",
        )

    def test_baseline_pin_outside_the_familiar_digest_form_is_rejected(self) -> None:
        group_vars = yaml.safe_load(
            (REPOSITORY_ROOT / "ansible/group_vars/all.yml").read_text(encoding="utf-8")
        )
        pin = "docker.io/library/" + group_vars["edge_traefik_image"]

        def repin(root: Path) -> None:
            def change_group_vars(document: Any) -> None:
                document["edge_traefik_image"] = pin

            def change_catalog(document: Any) -> None:
                service = next(
                    item
                    for item in document["approved_services"]
                    if item["id"] == "traefik-edge"
                )
                image = next(
                    item for item in service["images"] if item["component"] == "proxy"
                )
                image["reference"] = pin

            self.edit_yaml(root / "ansible/group_vars/all.yml", change_group_vars)
            self.edit_yaml(root / "config/services.yml", change_catalog)
            self.set_traefik_channel(root, pin)

        self.assert_contract_rejects(
            repin, "the reviewed Traefik baseline is not pinned by digest"
        )


if __name__ == "__main__":
    unittest.main()
