"""Edge (Traefik) image channel gates: static contract and live identity."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

import yaml

from ansible_task_harness import (
    REPOSITORY_ROOT,
    AnsibleTaskAssertions,
    load_task,
    run_task_definition,
    run_task_definitions,
)


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
APPLICATION_NETWORKS = {
    "organizationweb": "apptolast-edge-organizationweb",
    "racinggame": "apptolast-edge-racinggame",
    "observatorio": "apptolast-edge-observatorio",
    "satisfactory": "apptolast-edge-satisfactory",
}
ADOPTED_NETWORKS = ["apptolast-edge-observatorio", "apptolast-edge-satisfactory"]
BASICAUTH_SECRETS = {
    "basicauth_satisfactory_logs": "edge-basicauth-satisfactory-logs-v1",
}
# The hand-made Docker Config Traefik ran since 2026-09-22, masked. Configs
# are immutable, so the name pins the content. Regenerate or re-check with
# the read-only command in docs/EDGE.md, «Rutas de Satisfactory».
LIVE_DYNAMIC_FIXTURE = (
    REPOSITORY_ROOT
    / "tests/fixtures/edge-live-dynamic-companions-a0952eace071.masked.json"
)
HASH_PATTERN = r"\$(?:2[abxy]?|apr1)\$|\{SHA\}"


def render_edge() -> None:
    """Same docker-free render validate-iac.sh runs before the validators."""
    subprocess.run(
        [
            str(REPOSITORY_ROOT / ".venv/bin/ansible-playbook"),
            "--inventory",
            "ansible/inventory/local/hosts.yml",
            "ansible/playbooks/render-edge.yml",
        ],
        cwd=REPOSITORY_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
    )


def secret_mount(name: str, target: str, mode: int = 256) -> dict[str, Any]:
    return {
        "SecretName": name,
        "File": {"Name": target, "UID": "65532", "GID": "65532", "Mode": mode},
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


def basicauth_realms(middlewares: dict[str, Any], name: str) -> list[str]:
    """Realms a middleware reference applies, following chains."""
    middleware = middlewares[name.removesuffix("@file")]
    if "basicAuth" in middleware:
        # Traefik's default realm when none is set.
        return [middleware["basicAuth"].get("realm", "traefik")]
    if "chain" in middleware:
        return [
            realm
            for nested in middleware["chain"]["middlewares"]
            for realm in basicauth_realms(middlewares, nested)
        ]
    return []


def basicauth_probe_targets(http: dict[str, Any]) -> list[dict[str, str]]:
    """The probe entries that the rendered basicAuth routers require.

    The probe requests ``/`` of a hostname, so a protected router must be a
    bare ``Host`` rule behind exactly one realm. Anything else is returned
    as unprobeable, so the comparison fails and the probe gets reviewed.
    """
    middlewares = http.get("middlewares", {})
    targets = []
    for name, router in http["routers"].items():
        realms = [
            realm
            for reference in router.get("middlewares", [])
            for realm in basicauth_realms(middlewares, reference)
        ]
        if not realms:
            continue
        match = re.fullmatch(r"Host\(`([^`]+)`\)", router["rule"])
        if match is None or len(realms) != 1:
            targets.append({"router": name, "unprobeable": router["rule"]})
        else:
            targets.append({"hostname": match.group(1), "realm": realms[0]})
    return sorted(targets, key=json.dumps)


class TraefikLiveIdentityGateTests(AnsibleTaskAssertions, unittest.TestCase):
    DEPLOY = "ansible/roles/edge/tasks/deploy.yml"
    IDENTITY = "Verify the deployed Traefik service identity"
    IDENTITY_MESSAGE = "The deployed Traefik service differs from the reviewed spec."
    PRECONDITION = "Require a live Traefik hold to run its reviewed identity"
    PRECONDITION_MESSAGE = "Traefik runs an image outside its unchanged hold entry"

    @classmethod
    def setUpClass(cls) -> None:
        # The gates are exercised on both modes, whatever the reviewed map
        # holds today: the exact baseline hold and the v3 channel.
        baseline = channels.load_baselines(REPOSITORY_ROOT)[("traefik-edge", "proxy")]
        cls.hold = traefik_entry(baseline["reference"])
        cls.channel = traefik_entry("docker.io/library/traefik:v3")

    SECRETS = [
        secret_mount("cloudflare-token", "cloudflare_dns_api_token"),
        secret_mount(
            "edge-basicauth-satisfactory-logs-v1", "basicauth_satisfactory_logs"
        ),
    ]

    def inspect(
        self,
        image: str,
        label: str = "",
        secrets: list[dict[str, Any]] | None = None,
    ) -> str:
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
                                "Secrets": (
                                    self.SECRETS if secrets is None else secrets
                                ),
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
        secrets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "image_channels_map": {"edge": {"traefik": entry}},
            "edge_deployed_traefik_service": {
                "stdout": self.inspect(live, secrets=secrets)
            },
            "edge_traefik_image_before_deploy": before,
            "image_preflight_channel_resolutions": {
                self.channel["reference"]: self.channel["reference"] + "@" + RESOLVED
            },
            "edge_traefik_runtime_uid": 65532,
            "edge_traefik_runtime_gid": 65532,
            "edge_traefik_cloudflare_secret_name": "cloudflare-token",
            "edge_traefik_basicauth_secrets": BASICAUTH_SECRETS,
            "edge_required_networks": ["a", "b"],
            "edge_state_root": "/srv/edge",
        }

    def test_identity_gate_requires_every_reviewed_secret_read_only(self) -> None:
        cloudflare, basicauth = self.SECRETS
        for secrets in (
            # The hand-made spec before the codification: token only.
            [cloudflare],
            [cloudflare, basicauth, secret_mount("extra-v1", "extra")],
            [cloudflare, dict(basicauth, SecretName="edge-basicauth-other-v1")],
            [cloudflare, secret_mount(basicauth["SecretName"], "other_target")],
            [
                cloudflare,
                secret_mount(
                    basicauth["SecretName"], "basicauth_satisfactory_logs", 292
                ),
            ],
            [
                cloudflare,
                copy.deepcopy(basicauth) | {"File": dict(basicauth["File"], UID="0")},
            ],
        ):
            with self.subTest(secrets=secrets):
                self.assert_task_rejects(
                    self.DEPLOY,
                    self.IDENTITY,
                    self.identity(self.hold, self.hold["spec_exact"], secrets=secrets),
                    self.IDENTITY_MESSAGE,
                )

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
            "edge_traefik_basicauth_secrets": BASICAUTH_SECRETS,
            "edge_application_networks": APPLICATION_NETWORKS,
            "organizationweb": {
                "hostname": "organizacion.apptolast.com",
                "edge_network": "apptolast-edge-organizationweb",
            },
            "racinggame": {
                "hostname": "racinggame.apptolast.com",
                "edge_network": "apptolast-edge-racinggame",
            },
            "edge_adopted_attachable_networks": ADOPTED_NETWORKS,
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

    def test_the_satisfactory_network_and_users_file_secret_are_pinned(self) -> None:
        without_network = dict(APPLICATION_NETWORKS)
        del without_network["satisfactory"]
        for change in (
            {"edge_application_networks": without_network},
            {"edge_adopted_attachable_networks": ADOPTED_NETWORKS[:1]},
            {"edge_traefik_basicauth_secrets": {}},
            {
                "edge_traefik_basicauth_secrets": {
                    "basicauth_satisfactory_logs": "edge-basicauth-satisfactory-logs-v2"
                }
            },
            {
                "edge_traefik_basicauth_secrets": {
                    "basicauth_logs": "edge-basicauth-satisfactory-logs-v1"
                }
            },
        ):
            with self.subTest(change=change):
                self.assert_task_rejects(
                    self.MAIN,
                    self.TASK,
                    self.inputs(self.pin) | change,
                    self.MESSAGE,
                )


class EdgeBasicAuthSecretProvenanceTests(AnsibleTaskAssertions, unittest.TestCase):
    """The users file secrets are checked by metadata only, never read."""

    MAIN = "ansible/roles/edge/tasks/main.yml"
    TASK = "Verify the basicAuth users file secret provenance labels"
    MESSAGE = "lacks the reviewed manual-bootstrap provenance"
    NAME = "edge-basicauth-satisfactory-logs-v1"

    def run_gate(self, labels: dict[str, str], name: str | None = None):
        task = load_task(self.MAIN, self.TASK)
        # no_log hides the failure message this test asserts on; the
        # expressions under test stay exactly the reviewed ones.
        self.assertIs(task.pop("no_log"), True)
        inspected = {"Spec": {"Name": name or self.NAME, "Labels": labels}}
        return run_task_definition(
            task,
            {
                "edge_traefik_basicauth_secrets": BASICAUTH_SECRETS,
                "edge_basicauth_secret_inspect": {
                    "results": [
                        {
                            "item": self.NAME,
                            "rc": 0,
                            "stdout": json.dumps([inspected]),
                        }
                    ]
                },
            },
        )

    def test_a_labelled_manual_bootstrap_secret_is_accepted(self) -> None:
        completed = self.run_gate(
            {
                "com.apptolast.managed-by": "manual-bootstrap",
                "com.apptolast.purpose": "traefik-basicauth",
            }
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_unreviewed_provenance_is_rejected(self) -> None:
        for labels, name in (
            ({}, None),
            ({"com.apptolast.managed-by": "manual-bootstrap"}, None),
            (
                {
                    "com.apptolast.managed-by": "ansible",
                    "com.apptolast.purpose": "traefik-basicauth",
                },
                None,
            ),
            (
                {
                    "com.apptolast.managed-by": "manual-bootstrap",
                    "com.apptolast.purpose": "traefik-cloudflare-dns",
                },
                None,
            ),
            (
                {
                    "com.apptolast.managed-by": "manual-bootstrap",
                    "com.apptolast.purpose": "traefik-basicauth",
                },
                "edge-basicauth-other-v1",
            ),
        ):
            with self.subTest(labels=labels, name=name):
                completed = self.run_gate(labels, name)
                output = completed.stdout + completed.stderr
                self.assertNotEqual(completed.returncode, 0, output)
                # A missing label is an undefined-key error, not the message.
                self.assertIn("failed=1", output)

    def test_the_inspect_never_logs_and_the_gate_is_metadata_only(self) -> None:
        tasks = yaml.safe_load(
            (REPOSITORY_ROOT / self.MAIN).read_text(encoding="utf-8")
        )
        inspect = next(
            task
            for task in tasks
            if task.get("name") == "Verify the basicAuth users file secrets exist"
        )
        self.assertIs(inspect["no_log"], True)
        self.assertEqual(
            inspect["ansible.builtin.command"]["argv"][:3],
            ["/usr/bin/docker", "secret", "inspect"],
        )
        self.assertIs(load_task(self.MAIN, self.TASK)["no_log"], True)


class EdgeNetworkCreationTests(unittest.TestCase):
    """Only an adopted network is born attachable on a rebuilt host."""

    DEPLOY = "ansible/roles/edge/tasks/deploy.yml"
    BASE = [
        "/usr/bin/docker",
        "network",
        "create",
        "--driver",
        "overlay",
        "--opt",
        "encrypted",
    ]

    def argv_matches(self, network: str, expected: list[str]) -> bool:
        tasks = yaml.safe_load(
            (REPOSITORY_ROOT / self.DEPLOY).read_text(encoding="utf-8")
        )
        task = next(
            item
            for item in tasks
            if item.get("name") == "Create missing isolated overlay edge networks"
        )
        completed = run_task_definitions(
            [
                {
                    "name": "Derive the reviewed argv",
                    "ansible.builtin.set_fact": {
                        "created": task["ansible.builtin.command"]["argv"]
                    },
                },
                {
                    "name": "Compare it",
                    "ansible.builtin.assert": {"that": ["created == expected"]},
                },
            ],
            {
                "item": {"item": network},
                "edge_adopted_attachable_networks": ADOPTED_NETWORKS,
                "expected": expected,
            },
        )
        return completed.returncode == 0

    def test_adopted_networks_are_created_attachable_and_encrypted(self) -> None:
        for network in ADOPTED_NETWORKS:
            with self.subTest(network=network):
                self.assertTrue(
                    self.argv_matches(network, [*self.BASE, "--attachable", network])
                )
                self.assertFalse(self.argv_matches(network, [*self.BASE, network]))

    def test_every_other_edge_network_is_created_not_attachable(self) -> None:
        network = "apptolast-edge-kropia"
        self.assertTrue(self.argv_matches(network, [*self.BASE, network]))
        self.assertFalse(
            self.argv_matches(network, [*self.BASE, "--attachable", network])
        )


class EdgeBasicAuthChallengeProbeTests(unittest.TestCase):
    """A users file that failed to load disables its router (404), silently."""

    DEPLOY = "ansible/roles/edge/tasks/deploy.yml"
    TASK = "Prove every basicAuth route answers its Basic challenge"
    CHALLENGE = (
        "HTTP/2 401 \r\n"
        'www-authenticate: Basic realm="Satisfactory logs"\r\n'
        "content-type: text/plain\r\n\r\n"
    )

    @classmethod
    def setUpClass(cls) -> None:
        tasks = yaml.safe_load(
            (REPOSITORY_ROOT / cls.DEPLOY).read_text(encoding="utf-8")
        )
        flattened = [
            nested
            for task in tasks
            for nested in (task.get("block", []) + task.get("rescue", []) or [task])
        ]
        cls.task = next(task for task in flattened if task.get("name") == cls.TASK)
        render_edge()
        cls.http = yaml.safe_load(
            (REPOSITORY_ROOT / ".build/edge/dynamic.yml").read_text(encoding="utf-8")
        )["http"]

    def probed(self) -> list[dict[str, str]]:
        return sorted(self.task["loop"], key=json.dumps)

    def test_the_probe_covers_exactly_the_rendered_basicauth_routers(self) -> None:
        self.assertEqual(basicauth_probe_targets(self.http), self.probed())

    def test_a_new_basicauth_router_needs_its_own_probe(self) -> None:
        http = copy.deepcopy(self.http)
        http["middlewares"]["other-auth"] = {
            "basicAuth": {"usersFile": "/run/secrets/other", "realm": "Other"}
        }
        http["middlewares"]["other-chain"] = {
            "chain": {"middlewares": ["edge-security", "other-auth@file"]}
        }
        for rule, middlewares in (
            ("Host(`other.apptolast.com`)", ["edge-security", "other-auth"]),
            ("Host(`other.apptolast.com`)", ["other-chain"]),
            (
                "Host(`logs-satisfactory.apptolast.com`) && PathPrefix(`/x/`)",
                ["satisfactory-log-auth"],
            ),
        ):
            with self.subTest(rule=rule, middlewares=middlewares):
                changed = copy.deepcopy(http)
                changed["routers"]["other"] = {
                    "rule": rule,
                    "middlewares": middlewares,
                    "service": "satisfactory-logs",
                }
                self.assertNotEqual(basicauth_probe_targets(changed), self.probed())

    def converges(self, rc: int, stdout: str) -> bool:
        completed = run_task_definition(
            {
                "name": "Evaluate the reviewed until condition",
                "ansible.builtin.assert": {"that": self.task["until"]},
            },
            {
                "item": self.task["loop"][0],
                "edge_basicauth_challenges": {"rc": rc, "stdout": stdout},
            },
        )
        return completed.returncode == 0

    def test_the_probe_targets_the_logs_route_without_credentials(self) -> None:
        self.assertEqual(
            self.task["loop"],
            [
                {
                    "hostname": "logs-satisfactory.apptolast.com",
                    "realm": "Satisfactory logs",
                }
            ],
        )
        argv = self.task["ansible.builtin.command"]["argv"]
        for forbidden in ("--user", "--insecure", "Authorization", "--fail"):
            self.assertNotIn(forbidden, argv)
        self.assertEqual(self.task["retries"], 36)

    def test_only_the_reviewed_basic_challenge_converges(self) -> None:
        self.assertTrue(self.converges(0, self.CHALLENGE))
        self.assertTrue(self.converges(0, self.CHALLENGE.replace("HTTP/2", "HTTP/1.1")))
        for rc, stdout in (
            # The router was disabled because the users file did not load.
            (0, "HTTP/2 404 \r\ncontent-type: text/plain\r\n\r\n"),
            (0, self.CHALLENGE.replace("Satisfactory logs", "traefik")),
            (0, self.CHALLENGE.replace("401", "200")),
            (7, ""),
        ):
            with self.subTest(stdout=stdout, rc=rc):
                self.assertFalse(self.converges(rc, stdout))


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
        render_edge()

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
            lambda root: self.set_traefik_image(root, "docker.io/library/traefik:latest"),
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

    @classmethod
    def edit_dynamic(cls, change: Callable[[Any], None]) -> Callable[[Path], None]:
        def mutate(root: Path) -> None:
            cls.edit_yaml(root / ".build/edge/dynamic.yml", lambda d: change(d["http"]))

        return mutate

    @classmethod
    def edit_group_vars(cls, change: Callable[[Any], None]) -> Callable[[Path], None]:
        def mutate(root: Path) -> None:
            cls.edit_yaml(root / "ansible/group_vars/all.yml", change)

        return mutate

    def test_a_satisfactory_route_without_its_priority_is_rejected(self) -> None:
        for router in ("satisfactory-ws", "satisfactory-companions"):
            with self.subTest(router=router):
                self.assert_contract_rejects(
                    self.edit_dynamic(
                        lambda http: http["routers"][router].pop("priority")
                    ),
                    f"the {router} router differs from the reviewed ingress",
                )

    def test_a_satisfactory_route_with_the_rate_limit_is_rejected(self) -> None:
        # Live never limited Satisfactory; adding it is not a codification.
        self.assert_contract_rejects(
            self.edit_dynamic(
                lambda http: http["routers"]["satisfactory-web"]["middlewares"].append(
                    "edge-rate-limit"
                )
            ),
            "the satisfactory-web router differs from the reviewed ingress",
        )

    def test_a_moved_satisfactory_upstream_is_rejected(self) -> None:
        def move(http: Any) -> None:
            http["services"]["satisfactory-reverb"]["loadBalancer"]["servers"] = [
                {"url": "http://satisfactory-web:80"}
            ]

        self.assert_contract_rejects(
            self.edit_dynamic(move),
            "the satisfactory-reverb upstream differs from the reviewed ingress",
        )

    def test_inline_users_are_rejected(self) -> None:
        def inline(http: Any) -> None:
            http["middlewares"]["satisfactory-log-auth"]["basicAuth"]["users"] = [
                "user:placeholder"
            ]

        self.assert_contract_rejects(
            self.edit_dynamic(inline),
            "the Satisfactory logs login differs from its users file contract",
        )

    def test_an_unreviewed_middleware_is_rejected(self) -> None:
        def add(http: Any) -> None:
            http["middlewares"]["other-auth"] = {
                "basicAuth": {"usersFile": "/run/secrets/other"}
            }

        self.assert_contract_rejects(
            self.edit_dynamic(add),
            "the rendered edge middleware allowlist differs from the contract",
        )

    def test_a_password_hash_anywhere_in_the_render_is_rejected(self) -> None:
        # Built here so no bcrypt-shaped literal lives in the repository.
        marker = "$" + "2y" + "$" + "10" + "$" + "x" * 53

        def append(root: Path) -> None:
            path = root / ".build/edge/dynamic.yml"
            path.write_text(
                path.read_text(encoding="utf-8") + f"# {marker}\n", encoding="utf-8"
            )

        self.assert_contract_rejects(
            append, "the rendered dynamic.yml contains a password hash"
        )

    def test_a_renamed_or_missing_users_file_secret_is_rejected(self) -> None:
        for secrets in (
            {"basicauth_satisfactory_logs": "edge-basicauth-satisfactory-logs-v2"},
            {},
        ):
            with self.subTest(secrets=secrets):
                self.assert_contract_rejects(
                    self.edit_group_vars(
                        lambda document: document.update(
                            edge_traefik_basicauth_secrets=secrets
                        )
                    ),
                    "the basicAuth users file secrets differ from the reviewed map",
                )

    def test_an_unmounted_users_file_secret_is_rejected(self) -> None:
        def unmount(root: Path) -> None:
            def change(stack: Any) -> None:
                stack["services"]["traefik"]["secrets"].pop()

            self.edit_yaml(root / ".build/edge/stack.yml", change)

        self.assert_contract_rejects(
            unmount, "Traefik does not mount exactly the reviewed read-only secrets"
        )

    def test_the_satisfactory_network_must_stay_attached_and_adopted(self) -> None:
        self.assert_contract_rejects(
            self.edit_group_vars(
                lambda document: document["edge_adopted_attachable_networks"].remove(
                    "apptolast-edge-satisfactory"
                )
            ),
            "the adopted attachable edge networks differ from the contract",
        )

        def detach(root: Path) -> None:
            def change(stack: Any) -> None:
                del stack["networks"]["edge-satisfactory"]
                stack["services"]["traefik"]["networks"].remove("edge-satisfactory")

            self.edit_yaml(root / ".build/edge/stack.yml", change)

        self.assert_contract_rejects(
            detach, "the rendered edge networks differ from the isolation contract"
        )


class EdgeLiveParityTests(unittest.TestCase):
    """The render equals the hand-made live Traefik config, bar two changes.

    The fixture is the Docker Config the live service used when this was
    codified, parsed and with every basicAuth user masked. The only
    reviewed differences are:

    - ``satisfactory-log-auth`` reads its users from a Docker Secret file
      instead of an inline (hashed) list;
    - a workload parked in ``config/platform.yml`` renders a backend with no
      server and no probe (PR #59), which the hand-made Config predates.
    """

    MASK = "<masked>"

    @classmethod
    def setUpClass(cls) -> None:
        render_edge()
        cls.rendered = yaml.safe_load(
            (REPOSITORY_ROOT / ".build/edge/dynamic.yml").read_text(encoding="utf-8")
        )
        cls.fixture_text = LIVE_DYNAMIC_FIXTURE.read_text(encoding="utf-8")
        cls.live = json.loads(cls.fixture_text)
        cls.parked = yaml.safe_load(
            (REPOSITORY_ROOT / "config/platform.yml").read_text(encoding="utf-8")
        )["platform_parked_workloads"]

    def expected_render(self) -> dict[str, Any]:
        expected = copy.deepcopy(self.live)
        auth = expected["http"]["middlewares"]["satisfactory-log-auth"]["basicAuth"]
        self.assertEqual(auth.pop("users"), [self.MASK])
        auth["usersFile"] = "/run/secrets/basicauth_satisfactory_logs"
        if "openclaw" in self.parked:
            expected["http"]["services"]["openclaw"]["loadBalancer"] = {
                "passHostHeader": True,
                "servers": [],
            }
        return expected

    def test_the_fixture_holds_no_credential(self) -> None:
        self.assertIsNone(re.search(HASH_PATTERN, self.fixture_text))
        middlewares = self.live["http"]["middlewares"]
        basic = [name for name, item in middlewares.items() if "basicAuth" in item]
        self.assertEqual(basic, ["satisfactory-log-auth"])
        # Neither the hash nor the user name of the live entry is kept.
        auth = middlewares["satisfactory-log-auth"]["basicAuth"]
        self.assertEqual(auth["users"], [self.MASK])
        self.assertNotIn("usersFile", auth)

    def test_the_render_is_the_live_config_with_only_the_reviewed_changes(self) -> None:
        self.assertEqual(self.rendered, self.expected_render())

    def test_every_live_route_survives_with_its_hostname_and_priority(self) -> None:
        for name, router in self.live["http"]["routers"].items():
            with self.subTest(router=name):
                self.assertEqual(self.rendered["http"]["routers"][name], router)


if __name__ == "__main__":
    unittest.main()
