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
    "ax": "apptolast-edge-ax",
}
ADOPTED_NETWORKS = [
    "apptolast-edge-observatorio",
    "apptolast-edge-satisfactory",
    "apptolast-edge-ax",
]
NETWORK_SUBNETS = {"apptolast-edge-ax": "10.0.250.0/24"}
BASICAUTH_SECRETS = {
    "basicauth_satisfactory_logs": "edge-basicauth-satisfactory-logs-v1",
    "basicauth_ax": "edge-basicauth-ax-v1",
}
UPSTREAM_MTLS_SECRETS = {
    "ax_upstream_ca": "edge-ax-upstream-ca-v1",
    "ax_upstream_client": "edge-ax-upstream-client-v1",
}
# What the AX ingress adds to the hand-made live Config (docs/EDGE.md, «Ruta
# de AX»). EdgeLiveParityTests allows exactly this on top of the live file.
AX_LIMITS = [
    "edge-security",
    "ax-canonical-host",
    "ax-rl-ip",
    "ax-rl-host",
    "ax-inflight",
]
AX_ROUTE_ADDITIONS: dict[str, dict[str, Any]] = {
    "routers": {
        "ax": {
            "rule": "Host(`ax.apptolast.com`)",
            "entryPoints": ["websecure"],
            "middlewares": [*AX_LIMITS, "ax-auth"],
            "service": "ax",
            "tls": {"certResolver": "letsencrypt"},
        },
        "ax-health": {
            "rule": ("Host(`ax.apptolast.com`) && Path(`/healthz`) && Method(`GET`)"),
            "entryPoints": ["websecure"],
            "middlewares": [*AX_LIMITS, "ax-strip-authorization"],
            "service": "ax",
            "tls": {"certResolver": "letsencrypt"},
        },
    },
    "middlewares": {
        "ax-canonical-host": {
            "headers": {"customRequestHeaders": {"Host": "ax.apptolast.com"}}
        },
        "ax-rl-ip": {
            "rateLimit": {
                "average": 30,
                "period": "1m",
                "burst": 30,
                "sourceCriterion": {"ipStrategy": {"ipv6Subnet": 64}},
            }
        },
        "ax-rl-host": {
            "rateLimit": {
                "average": 2,
                "period": "1s",
                "burst": 10,
                "sourceCriterion": {"requestHost": True},
            }
        },
        "ax-inflight": {"inFlightReq": {"amount": 8}},
        "ax-auth": {
            "basicAuth": {
                "usersFile": "/run/secrets/basicauth_ax",
                "realm": "AX",
                "removeHeader": True,
            }
        },
        "ax-strip-authorization": {
            "headers": {"customRequestHeaders": {"Authorization": ""}}
        },
    },
    "services": {
        "ax": {
            "loadBalancer": {
                "passHostHeader": True,
                "serversTransport": "ax-web-mtls",
                "servers": [{"url": "https://ax-web-edge:8443"}],
            }
        },
    },
    "serversTransports": {
        "ax-web-mtls": {
            "serverName": "ax-web",
            "rootCAs": ["/run/secrets/ax_upstream_ca"],
            "certificates": [
                {
                    "certFile": "/run/secrets/ax_upstream_client",
                    "keyFile": "/run/secrets/ax_upstream_client",
                }
            ],
            "minVersion": "VersionTLS13",
            "maxVersion": "VersionTLS13",
            "forwardingTimeouts": {
                "dialTimeout": "5s",
                "responseHeaderTimeout": "60s",
                "idleConnTimeout": "180s",
            },
        },
    },
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
            # The router name as Traefik logs it, which the CrowdSec scenario
            # counts and the access log proof requires.
            targets.append(
                {
                    "hostname": match.group(1),
                    "realm": realms[0],
                    "router": f"{name}@file",
                }
            )
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
        *(
            secret_mount(name, target)
            for target, name in {**BASICAUTH_SECRETS, **UPSTREAM_MTLS_SECRETS}.items()
        ),
    ]

    MOUNTS = [
        {"Type": "bind", "Source": "/srv/edge/traefik", "Target": "/data"},
        {
            "Type": "bind",
            "Source": "/var/log/dockerswarm/edge",
            "Target": "/var/log/traefik",
        },
    ]

    def inspect(
        self,
        image: str,
        label: str = "",
        secrets: list[dict[str, Any]] | None = None,
        mounts: list[dict[str, Any]] | None = None,
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
                                "Mounts": (self.MOUNTS if mounts is None else mounts),
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
        mounts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "image_channels_map": {"edge": {"traefik": entry}},
            "edge_deployed_traefik_service": {
                "stdout": self.inspect(live, secrets=secrets, mounts=mounts)
            },
            "edge_traefik_image_before_deploy": before,
            "image_preflight_channel_resolutions": {
                self.channel["reference"]: self.channel["reference"] + "@" + RESOLVED
            },
            "edge_traefik_runtime_uid": 65532,
            "edge_traefik_runtime_gid": 65532,
            "edge_traefik_cloudflare_secret_name": "cloudflare-token",
            "edge_traefik_basicauth_secrets": BASICAUTH_SECRETS,
            "edge_traefik_upstream_mtls_secrets": UPSTREAM_MTLS_SECRETS,
            "edge_required_networks": ["a", "b"],
            "edge_state_root": "/srv/edge",
            "edge_traefik_access_log_dir": "/var/log/dockerswarm/edge",
        }

    def test_identity_gate_requires_every_reviewed_secret_read_only(self) -> None:
        cloudflare, *files = self.SECRETS
        by_target = {mount["File"]["Name"]: mount for mount in files}
        basicauth = by_target["basicauth_satisfactory_logs"]
        client = by_target["ax_upstream_client"]
        others = [mount for mount in files if mount is not client]
        for secrets in (
            # The hand-made spec before the codification: token only.
            [cloudflare],
            # The spec the Satisfactory codification applied, without AX.
            [cloudflare, basicauth],
            [*self.SECRETS, secret_mount("extra-v1", "extra")],
            [cloudflare, *others],
            [
                cloudflare,
                *others,
                dict(client, SecretName="edge-ax-upstream-client-v2"),
            ],
            [cloudflare, *others, secret_mount(client["SecretName"], "other_target")],
            [
                cloudflare,
                *others,
                secret_mount(client["SecretName"], "ax_upstream_client", 292),
            ],
            [
                cloudflare,
                *others,
                copy.deepcopy(client) | {"File": dict(client["File"], UID="0")},
            ],
        ):
            with self.subTest(secrets=secrets):
                self.assert_task_rejects(
                    self.DEPLOY,
                    self.IDENTITY,
                    self.identity(self.hold, self.hold["spec_exact"], secrets=secrets),
                    self.IDENTITY_MESSAGE,
                )

    def test_identity_gate_requires_the_state_and_access_log_binds(self) -> None:
        state, access_log = self.MOUNTS
        for label, mounts in {
            "the hand-made single bind": [state],
            "no access log bind": [state, dict(state, Target="/logs")],
            "read-only access log": [state, dict(access_log, ReadOnly=True)],
            "access log from elsewhere": [
                state,
                dict(access_log, Source="/var/lib/docker/containers"),
            ],
            "access log as a volume": [state, dict(access_log, Type="volume")],
            "a third mount": [
                state,
                access_log,
                {"Type": "bind", "Source": "/", "Target": "/host"},
            ],
        }.items():
            with self.subTest(label):
                self.assert_task_rejects(
                    self.DEPLOY,
                    self.IDENTITY,
                    self.identity(self.hold, self.hold["spec_exact"], mounts=mounts),
                    self.IDENTITY_MESSAGE,
                )
        # Order does not matter, and ReadOnly: false is what Docker reports.
        self.assert_task_accepts(
            self.DEPLOY,
            self.IDENTITY,
            self.identity(
                self.hold,
                self.hold["spec_exact"],
                mounts=[dict(access_log, ReadOnly=False), state],
            ),
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
            "edge_traefik_upstream_mtls_secrets": UPSTREAM_MTLS_SECRETS,
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
            "edge_network_subnets": NETWORK_SUBNETS,
            "edge_deployment_profile": "production",
            "edge_traefik_acme_ca_server": (
                "https://acme-v02.api.letsencrypt.org/directory"
            ),
            "edge_letsencrypt_staging_ca_bundle_sha256": "a" * 64,
            "edge_traefik_access_log_dir": "/var/log/dockerswarm/edge",
            "edge_traefik_access_log_rotation_unit": (
                "dockerswarm-edge-access-log-rotate"
            ),
        }

    def test_the_access_log_directory_and_its_rotation_are_pinned(self) -> None:
        # CrowdSec tails <dir>/access.log (config/host-security.yml).
        for key, value in (
            ("edge_traefik_access_log_dir", "/srv/dockerswarm/traefik"),
            ("edge_traefik_access_log_dir", "/var/lib/docker/containers"),
            ("edge_traefik_access_log_rotation_unit", "logrotate"),
        ):
            with self.subTest(key=key, value=value):
                self.assert_task_rejects(
                    self.MAIN,
                    self.TASK,
                    {**self.inputs(self.pin), key: value},
                    self.MESSAGE,
                )

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
                "edge_traefik_basicauth_secrets": BASICAUTH_SECRETS
                | {"basicauth_satisfactory_logs": "edge-basicauth-satisfactory-logs-v2"}
            },
            {
                "edge_traefik_basicauth_secrets": {
                    "basicauth_logs": "edge-basicauth-satisfactory-logs-v1",
                    "basicauth_ax": "edge-basicauth-ax-v1",
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

    def test_the_ax_network_and_secrets_are_pinned(self) -> None:
        without_network = dict(APPLICATION_NETWORKS)
        del without_network["ax"]
        without_login = dict(BASICAUTH_SECRETS)
        del without_login["basicauth_ax"]
        without_client = dict(UPSTREAM_MTLS_SECRETS)
        del without_client["ax_upstream_client"]
        for change in (
            {"edge_application_networks": without_network},
            {"edge_adopted_attachable_networks": ADOPTED_NETWORKS[:2]},
            # Same members, another order: the list is compared exactly.
            {"edge_adopted_attachable_networks": sorted(ADOPTED_NETWORKS)},
            {"edge_traefik_basicauth_secrets": without_login},
            {
                "edge_traefik_basicauth_secrets": BASICAUTH_SECRETS
                | {"basicauth_ax": "edge-basicauth-ax-v2"}
            },
            {"edge_traefik_upstream_mtls_secrets": without_client},
            {"edge_traefik_upstream_mtls_secrets": {}},
            {
                "edge_traefik_upstream_mtls_secrets": UPSTREAM_MTLS_SECRETS
                | {"ax_upstream_ca": "edge-ax-upstream-ca-v2"}
            },
            {
                "edge_traefik_upstream_mtls_secrets": {
                    "ax_ca": "edge-ax-upstream-ca-v1",
                    "ax_upstream_client": "edge-ax-upstream-client-v1",
                }
            },
            # The ACME token must never double as upstream material.
            {"edge_traefik_cloudflare_secret_name": "edge-ax-upstream-ca-v1"},
            # The AX forwarder admits only this subnet.
            {"edge_network_subnets": {}},
            {"edge_network_subnets": {"apptolast-edge-ax": "10.0.251.0/24"}},
        ):
            with self.subTest(change=change):
                self.assert_task_rejects(
                    self.MAIN,
                    self.TASK,
                    self.inputs(self.pin) | change,
                    self.MESSAGE,
                )

    def test_a_secret_or_target_shared_by_both_maps_is_rejected(self) -> None:
        # Only reachable with the exact-map pins relaxed: the disjointness
        # asserts are the second line of defence for the stack's targets.
        task = load_task(self.MAIN, self.TASK)
        disjoint = [
            condition
            for condition in task["ansible.builtin.assert"]["that"]
            if "intersect(edge_traefik_upstream_mtls_secrets" in condition
        ]
        self.assertEqual(len(disjoint), 2)
        for basicauth, upstream in (
            ({"a": "same-v1"}, {"b": "same-v1"}),
            ({"same": "a-v1"}, {"same": "b-v1"}),
        ):
            with self.subTest(basicauth=basicauth, upstream=upstream):
                completed = run_task_definition(
                    {
                        "name": "Evaluate the disjointness asserts",
                        "ansible.builtin.assert": {"that": disjoint},
                    },
                    {
                        "edge_traefik_basicauth_secrets": basicauth,
                        "edge_traefik_upstream_mtls_secrets": upstream,
                    },
                )
                self.assertNotEqual(completed.returncode, 0, completed.stdout)


class EdgeBasicAuthSecretProvenanceTests(AnsibleTaskAssertions, unittest.TestCase):
    """The users file secrets are checked by metadata only, never read."""

    MAIN = "ansible/roles/edge/tasks/main.yml"
    TASK = "Verify the basicAuth users file secret provenance labels"
    INSPECT = "Verify the basicAuth users file secrets exist"
    MESSAGE = "lacks the reviewed manual-bootstrap provenance"
    SECRETS = BASICAUTH_SECRETS
    REGISTER = "edge_basicauth_secret_inspect"
    VARIABLE = "edge_traefik_basicauth_secrets"
    PURPOSE = "traefik-basicauth"
    OTHER_PURPOSE = "traefik-cloudflare-dns"
    NAME = "edge-basicauth-ax-v1"

    def reviewed_labels(self) -> dict[str, str]:
        return {
            "com.apptolast.managed-by": "manual-bootstrap",
            "com.apptolast.purpose": self.PURPOSE,
        }

    def run_gate(
        self,
        labels: dict[str, str],
        name: str | None = None,
        results: list[str] | None = None,
    ):
        """Inspect every reviewed secret; the one called NAME gets labels."""
        task = load_task(self.MAIN, self.TASK)
        # no_log hides the failure message this test asserts on; the
        # expressions under test stay exactly the reviewed ones.
        self.assertIs(task.pop("no_log"), True)

        def inspected(item: str) -> str:
            if item != self.NAME:
                spec = {"Name": item, "Labels": self.reviewed_labels()}
            else:
                spec = {"Name": name or item, "Labels": labels}
            return json.dumps([{"Spec": spec}])

        items = sorted(self.SECRETS.values()) if results is None else results
        return run_task_definition(
            task,
            {
                self.VARIABLE: self.SECRETS,
                self.REGISTER: {
                    "results": [
                        {"item": item, "rc": 0, "stdout": inspected(item)}
                        for item in items
                    ]
                },
            },
        )

    def test_a_labelled_manual_bootstrap_secret_is_accepted(self) -> None:
        completed = self.run_gate(self.reviewed_labels())
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_every_reviewed_secret_must_be_inspected(self) -> None:
        for results in (
            sorted(self.SECRETS.values())[:1],
            [*sorted(self.SECRETS.values()), "edge-extra-v1"],
        ):
            with self.subTest(results=results):
                completed = self.run_gate(self.reviewed_labels(), results=results)
                self.assertNotEqual(completed.returncode, 0, completed.stdout)

    def test_unreviewed_provenance_is_rejected(self) -> None:
        for labels, name in (
            ({}, None),
            ({"com.apptolast.managed-by": "manual-bootstrap"}, None),
            (
                {
                    "com.apptolast.managed-by": "ansible",
                    "com.apptolast.purpose": self.PURPOSE,
                },
                None,
            ),
            (
                {
                    "com.apptolast.managed-by": "manual-bootstrap",
                    "com.apptolast.purpose": self.OTHER_PURPOSE,
                },
                None,
            ),
            (self.reviewed_labels(), "edge-other-v1"),
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
        inspect = next(task for task in tasks if task.get("name") == self.INSPECT)
        self.assertIs(inspect["no_log"], True)
        self.assertEqual(
            inspect["ansible.builtin.command"]["argv"],
            ["/usr/bin/docker", "secret", "inspect", "{{ item }}"],
        )
        self.assertIs(load_task(self.MAIN, self.TASK)["no_log"], True)


class EdgeUpstreamMtlsSecretProvenanceTests(EdgeBasicAuthSecretProvenanceTests):
    """The AX mTLS material is checked by metadata only, never read."""

    TASK = "Verify the upstream mTLS secret provenance labels"
    INSPECT = "Verify the upstream mTLS secrets exist"
    SECRETS = UPSTREAM_MTLS_SECRETS
    REGISTER = "edge_upstream_mtls_secret_inspect"
    VARIABLE = "edge_traefik_upstream_mtls_secrets"
    PURPOSE = "traefik-upstream-mtls"
    OTHER_PURPOSE = "traefik-basicauth"
    NAME = "edge-ax-upstream-client-v1"

    def test_the_inspect_loops_over_every_reviewed_secret(self) -> None:
        tasks = yaml.safe_load(
            (REPOSITORY_ROOT / self.MAIN).read_text(encoding="utf-8")
        )
        inspect = next(task for task in tasks if task.get("name") == self.INSPECT)
        self.assertEqual(
            inspect["loop"],
            "{{ edge_traefik_upstream_mtls_secrets.values() | list | sort }}",
        )
        self.assertIs(inspect["check_mode"], False)


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
                "edge_network_subnets": NETWORK_SUBNETS,
                "expected": expected,
            },
        )
        return completed.returncode == 0

    def test_adopted_networks_are_created_attachable_and_encrypted(self) -> None:
        for network in ADOPTED_NETWORKS:
            subnet = (
                ["--subnet", NETWORK_SUBNETS[network]]
                if network in NETWORK_SUBNETS
                else []
            )
            with self.subTest(network=network):
                self.assertTrue(
                    self.argv_matches(
                        network, [*self.BASE, "--attachable", *subnet, network]
                    )
                )
                self.assertFalse(self.argv_matches(network, [*self.BASE, network]))

    def test_the_ax_network_is_created_with_the_forwarder_subnet(self) -> None:
        # The AX panel's forwarder admits only this subnet (--allow-cidr).
        network = "apptolast-edge-ax"
        self.assertFalse(
            self.argv_matches(network, [*self.BASE, "--attachable", network])
        )
        self.assertTrue(
            self.argv_matches(
                network,
                [*self.BASE, "--attachable", "--subnet", "10.0.250.0/24", network],
            )
        )

    def verified(self, network: str, subnets: list[str] | None) -> bool:
        tasks = yaml.safe_load(
            (REPOSITORY_ROOT / self.DEPLOY).read_text(encoding="utf-8")
        )
        task = next(
            item
            for item in tasks
            if item.get("name") == "Verify every isolated edge network contract"
        )
        inspect = {
            "Driver": "overlay",
            "Scope": "swarm",
            "Attachable": network in ADOPTED_NETWORKS,
            "Internal": False,
            "Options": {"encrypted": ""},
            "IPAM": {
                "Config": (
                    None
                    if subnets is None
                    else [{"Subnet": subnet} for subnet in subnets]
                )
            },
        }
        completed = run_task_definition(
            {
                "name": "Evaluate the reviewed network contract",
                "ansible.builtin.assert": {
                    "that": task["ansible.builtin.assert"]["that"]
                },
            },
            {
                "item": {"item": network, "stdout": json.dumps([inspect])},
                "edge_adopted_attachable_networks": ADOPTED_NETWORKS,
                "edge_network_subnets": NETWORK_SUBNETS,
            },
        )
        return completed.returncode == 0

    def test_only_the_fixed_subnet_is_accepted_on_the_ax_network(self) -> None:
        self.assertTrue(self.verified("apptolast-edge-ax", ["10.0.250.0/24"]))
        for subnets in (["10.0.30.0/24"], ["10.0.250.0/24", "10.0.31.0/24"], []):
            with self.subTest(subnets=subnets):
                self.assertFalse(self.verified("apptolast-edge-ax", subnets))
        # Swarm numbers every other edge network.
        self.assertTrue(self.verified("apptolast-edge-satisfactory", ["10.0.7.0/24"]))
        self.assertTrue(self.verified("apptolast-edge-kropia", ["10.0.3.0/24"]))

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
    AX_CHALLENGE = CHALLENGE.replace("Satisfactory logs", "AX")

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

    def converges(self, rc: int, stdout: str, item: int = 0) -> bool:
        completed = run_task_definition(
            {
                "name": "Evaluate the reviewed until condition",
                "ansible.builtin.assert": {"that": self.task["until"]},
            },
            {
                "item": self.task["loop"][item],
                "edge_basicauth_challenges": {"rc": rc, "stdout": stdout},
            },
        )
        return completed.returncode == 0

    def test_the_probe_targets_each_login_route_without_credentials(self) -> None:
        self.assertEqual(
            self.task["loop"],
            [
                {
                    "hostname": "logs-satisfactory.apptolast.com",
                    "realm": "Satisfactory logs",
                    "router": "satisfactory-logs@file",
                },
                {"hostname": "ax.apptolast.com", "realm": "AX", "router": "ax@file"},
            ],
        )
        argv = self.task["ansible.builtin.command"]["argv"]
        for forbidden in ("--user", "--insecure", "Authorization", "--fail"):
            self.assertNotIn(forbidden, argv)

    def test_the_probe_waits_five_minutes_for_a_first_certificate(self) -> None:
        # ax.apptolast.com gets its certificate by DNS-01 on its first deploy
        # and sniStrict refuses its TLS handshake until then.
        self.assertEqual(self.task["retries"] * self.task["delay"], 300)
        self.assertTrue(self.converges(0, self.AX_CHALLENGE, item=1))
        # curl exit 35: the TLS handshake failed; the next attempt retries.
        self.assertFalse(self.converges(35, "", item=1))

    def test_only_the_reviewed_basic_challenge_converges(self) -> None:
        self.assertTrue(self.converges(0, self.CHALLENGE))
        self.assertTrue(self.converges(0, self.CHALLENGE.replace("HTTP/2", "HTTP/1.1")))
        for rc, stdout, item in (
            # The router was disabled because the users file did not load.
            (0, "HTTP/2 404 \r\ncontent-type: text/plain\r\n\r\n", 0),
            (0, self.CHALLENGE.replace("Satisfactory logs", "traefik"), 0),
            (0, self.CHALLENGE.replace("401", "200"), 0),
            (7, "", 0),
            # Each route answers with its own realm.
            (0, self.CHALLENGE, 1),
            (0, self.AX_CHALLENGE, 0),
            (0, "HTTP/2 404 \r\ncontent-type: text/plain\r\n\r\n", 1),
            # A route without its login reaches the panel.
            (0, self.AX_CHALLENGE.replace("401", "502"), 1),
        ):
            with self.subTest(stdout=stdout, rc=rc, item=item):
                self.assertFalse(self.converges(rc, stdout, item))


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

    # AX ingress (docs/EDGE.md, «Ruta de AX»).

    def test_the_ax_limits_must_run_in_order_before_the_login(self) -> None:
        reviewed = AX_ROUTE_ADDITIONS["routers"]["ax"]["middlewares"]
        canonical = ["edge-security", "ax-canonical-host"]
        for middlewares in (
            # The design's first order: a rate limiter holding a delayed
            # request would then occupy an in-flight slot.
            [*canonical, "ax-inflight", "ax-rl-ip", "ax-rl-host", "ax-auth"],
            # The login before the limits: every guess costs a bcrypt.
            [*canonical, "ax-auth", "ax-rl-ip", "ax-rl-host", "ax-inflight"],
            [m for m in reviewed if m != "ax-rl-host"],
            [m for m in reviewed if m != "ax-auth"],
        ):
            with self.subTest(middlewares=middlewares):
                self.assert_contract_rejects(
                    self.edit_dynamic(
                        lambda http: http["routers"]["ax"].update(
                            middlewares=middlewares
                        )
                    ),
                    "the ax router differs from the reviewed AX ingress",
                )

    def test_the_ax_limits_count_one_canonical_host(self) -> None:
        # rateLimit and inFlightReq group requestHost by the raw Host, while
        # the router matches it case-insensitively and without its port:
        # AX.apptolast.com or ax.apptolast.com:443 would each get fresh
        # counters. The Host is rewritten before the first limit.
        for router in ("ax", "ax-health"):
            reviewed = AX_ROUTE_ADDITIONS["routers"][router]["middlewares"]
            for middlewares in (
                [m for m in reviewed if m != "ax-canonical-host"],
                [
                    "edge-security",
                    "ax-rl-ip",
                    "ax-rl-host",
                    "ax-canonical-host",
                    "ax-inflight",
                    reviewed[-1],
                ],
            ):
                with self.subTest(router=router, middlewares=middlewares):
                    self.assert_contract_rejects(
                        self.edit_dynamic(
                            lambda http: http["routers"][router].update(
                                middlewares=middlewares
                            )
                        ),
                        f"the {router} router differs from the reviewed AX ingress",
                    )
        for request_headers in (
            {"Host": "AX.apptolast.com"},
            {"Host": "ax.apptolast.com:443"},
            {"X-Forwarded-Host": "ax.apptolast.com"},
            {"Host": "ax.apptolast.com", "Authorization": ""},
        ):
            with self.subTest(request_headers=request_headers):
                self.assert_contract_rejects(
                    self.edit_dynamic(
                        lambda http: http["middlewares"]["ax-canonical-host"].update(
                            headers={"customRequestHeaders": request_headers}
                        )
                    ),
                    "the ax-canonical-host middleware differs from the reviewed "
                    "AX ingress",
                )

    def test_the_health_route_drops_the_authorization_header(self) -> None:
        # Browsers resend cached Basic credentials to every path of the
        # protection space, and ax-health has no basicAuth removeHeader.
        reviewed = AX_ROUTE_ADDITIONS["routers"]["ax-health"]["middlewares"]
        self.assert_contract_rejects(
            self.edit_dynamic(
                lambda http: http["routers"]["ax-health"].update(
                    middlewares=reviewed[:-1]
                )
            ),
            "the ax-health router differs from the reviewed AX ingress",
        )
        for headers in (
            {"customRequestHeaders": {"Authorization": "Basic x"}},
            {"customResponseHeaders": {"Authorization": ""}},
        ):
            with self.subTest(headers=headers):
                self.assert_contract_rejects(
                    self.edit_dynamic(
                        lambda http: http["middlewares"][
                            "ax-strip-authorization"
                        ].update(headers=headers)
                    ),
                    "the ax-strip-authorization middleware differs from the "
                    "reviewed AX ingress",
                )

    def test_compression_is_rejected_on_both_ax_routers(self) -> None:
        for router in ("ax", "ax-health"):
            for change in (
                lambda middlewares: middlewares.append("edge-compress"),
                lambda middlewares: middlewares.__setitem__(0, "edge-default"),
            ):
                with self.subTest(router=router, change=change):
                    self.assert_contract_rejects(
                        self.edit_dynamic(
                            lambda http: change(http["routers"][router]["middlewares"])
                        ),
                        f"the {router} router differs from the reviewed AX ingress",
                    )

    def test_the_unauthenticated_health_route_cannot_widen(self) -> None:
        for rule in (
            "Host(`ax.apptolast.com`) && Path(`/healthz`)",
            "Host(`ax.apptolast.com`) && PathPrefix(`/healthz`) && Method(`GET`)",
            "Host(`ax.apptolast.com`) && Path(`/api/tasks`) && Method(`GET`)",
        ):
            with self.subTest(rule=rule):
                self.assert_contract_rejects(
                    self.edit_dynamic(
                        lambda http: http["routers"]["ax-health"].update(rule=rule)
                    ),
                    "the ax-health router differs from the reviewed AX ingress",
                )

    def test_inline_users_on_the_ax_login_are_rejected(self) -> None:
        def inline(http: Any) -> None:
            http["middlewares"]["ax-auth"]["basicAuth"]["users"] = ["user:placeholder"]

        self.assert_contract_rejects(
            self.edit_dynamic(inline),
            "the ax-auth middleware differs from the reviewed AX ingress",
        )

    def test_a_loosened_ax_limit_is_rejected(self) -> None:
        for name, path, value in (
            ("ax-rl-host", ("rateLimit", "burst"), 100),
            ("ax-rl-ip", ("rateLimit", "average"), 300),
            ("ax-inflight", ("inFlightReq", "amount"), 64),
        ):
            with self.subTest(name=name):

                def loosen(http: Any) -> None:
                    http["middlewares"][name][path[0]][path[1]] = value

                self.assert_contract_rejects(
                    self.edit_dynamic(loosen),
                    f"the {name} middleware differs from the reviewed AX ingress",
                )

    def test_insecure_skip_verify_is_rejected_anywhere(self) -> None:
        for change in (
            lambda http: http["serversTransports"]["ax-web-mtls"].update(
                insecureSkipVerify=True
            ),
            lambda http: http["serversTransports"]["ax-web-mtls"].update(
                insecureSkipVerify=False
            ),
            lambda http: http["services"]["kropia"]["loadBalancer"][
                "healthCheck"
            ].update(insecureSkipVerify=True),
        ):
            with self.subTest(change=change):
                self.assert_contract_rejects(
                    self.edit_dynamic(change),
                    "the rendered dynamic configuration skips backend TLS verification",
                )

    def test_min_version_without_max_version_is_rejected(self) -> None:
        self.assert_contract_rejects(
            self.edit_dynamic(
                lambda http: http["serversTransports"]["ax-web-mtls"].pop("maxVersion")
            ),
            "the ax-web-mtls minVersion needs a maxVersion",
        )

    def test_the_mtls_transport_is_pinned(self) -> None:
        client = "/run/secrets/ax_upstream_client"
        for key, value in (
            ("serverName", "ax-web-edge"),
            ("rootCAs", []),
            ("certificates", []),
            # Cert and key in separate, unmounted files.
            (
                "certificates",
                [{"certFile": client, "keyFile": "/run/secrets/ax_upstream_key"}],
            ),
            ("minVersion", "VersionTLS12"),
            ("maxVersion", "VersionTLS12"),
            ("forwardingTimeouts", None),
            (
                "forwardingTimeouts",
                {
                    "dialTimeout": "5s",
                    "responseHeaderTimeout": "0s",
                    "idleConnTimeout": "180s",
                },
            ),
        ):
            with self.subTest(key=key, value=value):

                def change(http: Any) -> None:
                    transport = http["serversTransports"]["ax-web-mtls"]
                    if value is None:
                        del transport[key]
                    else:
                        transport[key] = value

                self.assert_contract_rejects(
                    self.edit_dynamic(change),
                    "the ax-web-mtls transport differs from the reviewed mTLS contract",
                )

    def test_a_second_servers_transport_is_rejected(self) -> None:
        def add(http: Any) -> None:
            http["serversTransports"]["other"] = copy.deepcopy(
                http["serversTransports"]["ax-web-mtls"]
            )

        self.assert_contract_rejects(
            self.edit_dynamic(add),
            "the rendered servers transports differ from the reviewed allowlist",
        )

    def test_the_transport_serves_only_the_ax_backend(self) -> None:
        def reuse(http: Any) -> None:
            http["services"]["kropia"]["loadBalancer"][
                "serversTransport"
            ] = "ax-web-mtls"

        self.assert_contract_rejects(
            self.edit_dynamic(reuse),
            "only the ax backend may use the reviewed servers transport",
        )
        for change in (
            lambda balancer: balancer.pop("serversTransport"),
            lambda balancer: balancer.update(
                servers=[{"url": "http://ax-web-edge:8443"}]
            ),
            lambda balancer: balancer.update(
                healthCheck={"path": "/healthz", "interval": "15s", "timeout": "3s"}
            ),
            lambda balancer: balancer.update(passHostHeader=False),
        ):
            with self.subTest(change=change):
                self.assert_contract_rejects(
                    self.edit_dynamic(
                        lambda http: change(http["services"]["ax"]["loadBalancer"])
                    ),
                    "the ax upstream differs from the reviewed AX ingress",
                )

    def test_a_file_outside_the_mounted_secrets_is_rejected(self) -> None:
        # The TLS section is otherwise unpinned; Traefik reads a path it
        # cannot open as inline content, so it would fail quietly.
        def add(http_and_tls: Any) -> None:
            http_and_tls["tls"]["certificates"] = [
                {
                    "certFile": "/run/secrets/ax_upstream_client",
                    "keyFile": "/run/secrets/other_key",
                }
            ]

        def mutate(root: Path) -> None:
            self.edit_yaml(root / ".build/edge/dynamic.yml", add)

        self.assert_contract_rejects(
            mutate, "the dynamic configuration and the mounted secrets differ"
        )

    def test_a_tcp_router_section_is_rejected(self) -> None:
        def add(document: Any) -> None:
            document["tcp"] = {
                "routers": {"ax": {"rule": "HostSNI(`*`)", "service": "ax"}}
            }

        def mutate(root: Path) -> None:
            self.edit_yaml(root / ".build/edge/dynamic.yml", add)

        self.assert_contract_rejects(
            mutate,
            "the rendered dynamic configuration has unreviewed top-level sections",
        )

    def test_an_unreviewed_http_section_is_rejected(self) -> None:
        self.assert_contract_rejects(
            self.edit_dynamic(lambda http: http.update(models={"x": {}})),
            "the rendered dynamic HTTP configuration has unreviewed sections",
        )

    def test_a_renamed_or_missing_ax_secret_is_rejected(self) -> None:
        for variable, value, message in (
            (
                "edge_traefik_basicauth_secrets",
                BASICAUTH_SECRETS | {"basicauth_ax": "edge-basicauth-ax-v2"},
                "the basicAuth users file secrets differ from the reviewed map",
            ),
            (
                "edge_traefik_upstream_mtls_secrets",
                UPSTREAM_MTLS_SECRETS
                | {"ax_upstream_client": "edge-ax-upstream-client-v2"},
                "the upstream mTLS secrets differ from the reviewed map",
            ),
            (
                "edge_traefik_upstream_mtls_secrets",
                {},
                "the upstream mTLS secrets differ from the reviewed map",
            ),
        ):
            with self.subTest(variable=variable, value=value):
                self.assert_contract_rejects(
                    self.edit_group_vars(
                        lambda document: document.update({variable: value})
                    ),
                    message,
                )

    def test_an_unmounted_or_writable_ax_secret_is_rejected(self) -> None:
        for change in (
            lambda secrets: secrets.pop(),
            lambda secrets: secrets[-1].update(mode=0o444),
            lambda secrets: secrets[-1].update(target="ax_client"),
        ):
            with self.subTest(change=change):

                def mutate(root: Path) -> None:
                    self.edit_yaml(
                        root / ".build/edge/stack.yml",
                        lambda stack: change(stack["services"]["traefik"]["secrets"]),
                    )

                self.assert_contract_rejects(
                    mutate,
                    "Traefik does not mount exactly the reviewed read-only secrets",
                )

    def test_the_ax_network_subnet_is_pinned(self) -> None:
        for subnets in (
            {},
            {"apptolast-edge-ax": "10.0.251.0/24"},
            {**NETWORK_SUBNETS, "apptolast-edge-satisfactory": "10.0.251.0/24"},
        ):
            with self.subTest(subnets=subnets):
                self.assert_contract_rejects(
                    self.edit_group_vars(
                        lambda document: document.update(edge_network_subnets=subnets)
                    ),
                    "the fixed edge network subnets differ from the contract",
                )

    def test_the_ax_network_must_stay_attached_and_adopted(self) -> None:
        self.assert_contract_rejects(
            self.edit_group_vars(
                lambda document: document["edge_adopted_attachable_networks"].remove(
                    "apptolast-edge-ax"
                )
            ),
            "the adopted attachable edge networks differ from the contract",
        )

        def detach(root: Path) -> None:
            def change(stack: Any) -> None:
                del stack["networks"]["edge-ax"]
                stack["services"]["traefik"]["networks"].remove("edge-ax")

            self.edit_yaml(root / ".build/edge/stack.yml", change)

        self.assert_contract_rejects(
            detach, "the rendered edge networks differ from the isolation contract"
        )

    def test_the_access_log_must_be_a_json_file_on_the_bind(self) -> None:
        for label, change in {
            "back to stdout": lambda log: log.pop("filePath"),
            "another path": lambda log: log.update(filePath="/data/access.log"),
            "common format": lambda log: log.update(format="common"),
        }.items():
            with self.subTest(label):

                def mutate(root: Path, change=change) -> None:
                    self.edit_yaml(
                        root / ".build/edge/static.yml",
                        lambda static: change(static["accessLog"]),
                    )

                self.assert_contract_rejects(
                    mutate, "the access log must be JSON in /var/log/traefik/access.log"
                )

    def test_traefik_mounts_exactly_its_state_and_its_access_log(self) -> None:
        def service(root: Path, change: Callable[[list[Any]], None]) -> None:
            self.edit_yaml(
                root / ".build/edge/stack.yml",
                lambda stack: change(stack["services"]["traefik"]["volumes"]),
            )

        for label, change in {
            "no access log bind": lambda volumes: volumes.pop(1),
            "read-only access log": lambda volumes: volumes[1].update(read_only=True),
            "access log elsewhere": lambda volumes: volumes[1].update(
                source="/var/lib/docker/containers"
            ),
            "a third bind": lambda volumes: volumes.append(
                {"type": "bind", "source": "/", "target": "/host"}
            ),
        }.items():
            with self.subTest(label):
                self.assert_contract_rejects(
                    lambda root, change=change: service(root, change),
                    "Traefik does not mount exactly the reviewed state and access log",
                )

    def test_crowdsec_must_read_the_file_traefik_writes(self) -> None:
        def move(root: Path) -> None:
            self.edit_yaml(
                root / "config/host-security.yml",
                lambda contract: contract.update(
                    host_security_crowdsec_traefik_access_log=(
                        "/var/log/dockerswarm/other/access.log"
                    )
                ),
            )

        self.assert_contract_rejects(
            move, "CrowdSec does not read the access log that Traefik writes"
        )

        def move_directory(root: Path) -> None:
            self.edit_yaml(
                root / "ansible/group_vars/all.yml",
                lambda group_vars: group_vars.update(
                    edge_traefik_access_log_dir="/var/log/traefik"
                ),
            )

        self.assert_contract_rejects(
            move_directory,
            "the Traefik access log directory differs from the reviewed contract",
        )

    def test_the_access_log_must_drop_the_user_name(self) -> None:
        def keep(root: Path) -> None:
            self.edit_yaml(
                root / ".build/edge/static.yml",
                lambda static: static["accessLog"]["fields"].pop("names"),
            )

        self.assert_contract_rejects(
            keep, "the access log keeps the basicAuth user name"
        )


class EdgeTraefikRenderBootPreparationTests(unittest.TestCase):
    """scripts/prepare-traefik-validation.py, run without Docker.

    scripts/validate-traefik-config.sh boots the pinned Traefik with its
    output: the whole render but the ACME resolver and the health checks,
    and a throwaway stand-in for every secret file.
    """

    SCRIPT = REPOSITORY_ROOT / "scripts/prepare-traefik-validation.py"

    @classmethod
    def setUpClass(cls) -> None:
        render_edge()

    def prepare(
        self, change: Callable[[Any], None] | None = None
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        temporary = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, temporary)
        render = temporary / "render"
        shutil.copytree(REPOSITORY_ROOT / ".build/edge", render)
        if change is not None:
            EdgeStaticContractTests.edit_yaml(render / "dynamic.yml", change)
        output = temporary / "validation"
        completed = subprocess.run(
            [sys.executable, str(self.SCRIPT), str(render), str(output)],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed, output

    @staticmethod
    def load(path: Path) -> Any:
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_only_the_resolver_and_the_health_checks_are_removed(self) -> None:
        completed, output = self.prepare()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        static = self.load(REPOSITORY_ROOT / ".build/edge/static.yml")
        del static["certificatesResolvers"]
        del static["entryPoints"]["websecure"]["http"]["tls"]["certResolver"]
        self.assertEqual(self.load(output / "static.yml"), static)
        dynamic = self.load(REPOSITORY_ROOT / ".build/edge/dynamic.yml")
        for router in dynamic["http"]["routers"].values():
            router.get("tls", {}).pop("certResolver", None)
        for service in dynamic["http"]["services"].values():
            service["loadBalancer"].pop("healthCheck", None)
        self.assertEqual(self.load(output / "dynamic.yml"), dynamic)
        # The mTLS transport the boot has to load stays as rendered.
        self.assertEqual(
            dynamic["http"]["serversTransports"],
            AX_ROUTE_ADDITIONS["serversTransports"],
        )

    def test_every_secret_file_gets_a_throwaway_of_its_kind(self) -> None:
        from cryptography import x509
        from cryptography.x509.oid import ExtendedKeyUsageOID

        completed, output = self.prepare()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        secrets = output / "secrets"
        self.assertEqual(
            sorted(path.name for path in secrets.iterdir()),
            sorted({**BASICAUTH_SECRETS, **UPSTREAM_MTLS_SECRETS}),
        )
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                "ax_upstream_ca ca",
                "ax_upstream_client client",
                "basicauth_ax users",
                "basicauth_satisfactory_logs users",
            ],
        )
        # Readable by the container's 65532 through the bind mount.
        for path in (output, secrets):
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
        for path in [*secrets.iterdir(), output / "static.yml"]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        for target in BASICAUTH_SECRETS:
            self.assertRegex(
                (secrets / target).read_text(encoding="ascii"),
                r"\Avalidation:\{SHA\}[A-Za-z0-9+/]{27}=\n\Z",
            )
        ca = x509.load_pem_x509_certificate((secrets / "ax_upstream_ca").read_bytes())
        self.assertTrue(
            ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        )
        client_pem = (secrets / "ax_upstream_client").read_bytes()
        # One PEM: the certificate, then its key, as the real secret.
        self.assertEqual(client_pem.count(b"-----BEGIN CERTIFICATE-----"), 1)
        self.assertEqual(client_pem.count(b"-----BEGIN PRIVATE KEY-----"), 1)
        self.assertLess(client_pem.index(b"CERTIFICATE"), client_pem.index(b"PRIVATE"))
        client = x509.load_pem_x509_certificate(client_pem)
        client.verify_directly_issued_by(ca)
        self.assertEqual(
            list(
                client.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            ),
            [ExtendedKeyUsageOID.CLIENT_AUTH],
        )

    def test_an_unreviewed_secret_file_fails_closed(self) -> None:
        def elsewhere(document: Any) -> None:
            document["tls"]["certificates"] = [
                {
                    "certFile": "/run/secrets/ax_upstream_client",
                    "keyFile": "/run/secrets/ax_upstream_client",
                }
            ]

        def split_pair(document: Any) -> None:
            document["http"]["serversTransports"]["ax-web-mtls"]["certificates"] = [
                {
                    "certFile": "/run/secrets/ax_upstream_client",
                    "keyFile": "/run/secrets/ax_upstream_key",
                }
            ]

        def two_kinds(document: Any) -> None:
            document["http"]["middlewares"]["ax-auth"]["basicAuth"][
                "usersFile"
            ] = "/run/secrets/ax_upstream_ca"

        def odd_name(document: Any) -> None:
            document["http"]["middlewares"]["ax-auth"]["basicAuth"][
                "usersFile"
            ] = "/run/secrets/../basicauth_ax"

        for change in (elsewhere, split_pair, two_kinds, odd_name):
            with self.subTest(change=change.__name__):
                completed, output = self.prepare(change)
                self.assertEqual(completed.returncode, 1)
                self.assertTrue(completed.stderr.startswith("ERROR: "))
                self.assertFalse(output.exists())

    def test_the_validation_boots_the_render_with_the_stand_ins(self) -> None:
        script = (REPOSITORY_ROOT / "scripts/validate-traefik-config.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("prepare-traefik-validation.py", script)
        self.assertIn('"${validation_dir}/dynamic.yml"', script)
        self.assertIn('--volume "${validation_dir}/secrets:/run/secrets:ro"', script)
        # The rendered static configuration still boots unchanged too.
        self.assertIn('"${PROJECT_DIR}/.build/edge/static.yml"', script)
        # Like the service, the boot gets a writable access log directory:
        # Traefik only WARNs and runs without an access log when it cannot
        # open the file, and the validation fails on that WARN.
        self.assertIn(
            "--tmpfs /var/log/traefik:rw,noexec,nosuid,nodev,size=16m,"
            "uid=65532,gid=65532,mode=0700",
            script,
        )
        self.assertEqual(script.count("\nboot_traefik \\\n"), 2)


class EdgeAxLoginSecretCommandTests(unittest.TestCase):
    """The documented creation of the AX users file (docs/EDGE.md).

    The password reaches the host only on the command's stdin and only its
    bcrypt hash leaves it, into ``docker secret create -``.
    """

    @classmethod
    def setUpClass(cls) -> None:
        text = (REPOSITORY_ROOT / "docs/EDGE.md").read_text(encoding="utf-8")
        section = text.split("### Crear el secret del login\n", 1)[1]
        section = section.split("\n### ", 1)[0]
        cls.command = next(
            block
            for block in re.findall(r"```bash\n(.*?)```", section, re.S)
            if "docker secret create" in block
        )
        # -I (isolated): no current directory on sys.path and no PYTHON*
        # variables, so no stray module can stand in for bcrypt or getpass
        # in the process that holds the password.
        match = re.search(
            r"/usr/bin/python3 -I -c '\n(.*?)\n' \"\$\{ax_user\}\" \|\n",
            cls.command,
            re.S,
        )
        assert match is not None, "the documented bcrypt step changed shape"
        cls.code = match.group(1)

    def test_the_password_only_travels_on_stdin(self) -> None:
        self.assertTrue(self.command.startswith("set +x\nset -o pipefail\n"))
        # The user name is the owner's and never written in this repository.
        self.assertIn("ax_user='<usuario>'\n", self.command)
        for forbidden in (
            "host_global_operation_lock",
            "echo",
            "set -x",
            "tee",
            "mktemp",
            "/tmp",
            "--password",
        ):
            self.assertNotIn(forbidden, self.command)
        self.assertIsNone(re.search(HASH_PATTERN, self.command))
        self.assertIn("sys.stdin.buffer.read()", self.code)
        self.assertIn('bcrypt.gensalt(rounds=10, prefix=b"2b")', self.code)
        self.assertTrue(
            self.command.endswith(
                "  sudo -- docker secret create \\\n"
                "    --label com.apptolast.managed-by=manual-bootstrap \\\n"
                "    --label com.apptolast.purpose=traefik-basicauth \\\n"
                f"    {BASICAUTH_SECRETS['basicauth_ax']} - >/dev/null\n"
            )
        )

    def run_step(
        self, user: str, stdin: bytes, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["/usr/bin/python3", "-I", "-c", self.code, user],
            input=stdin,
            capture_output=True,
            check=False,
            cwd=cwd,
        )

    def require_host_bcrypt(self) -> None:
        probe = subprocess.run(
            ["/usr/bin/python3", "-I", "-c", "import bcrypt"],
            capture_output=True,
            check=False,
        )
        if probe.returncode != 0:
            self.skipTest("the host bcrypt module (python3-bcrypt) is absent")

    def test_the_step_writes_one_users_line_and_prints_no_hash(self) -> None:
        self.require_host_bcrypt()
        # A throwaway value, only for this test.
        password = b"not-a-real-password-" + b"x" * 8
        completed = self.run_step("labuser", password + b"\n")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, b"hash: prefijo $2 longitud 60\n")
        self.assertEqual(completed.stdout.count(b"\n"), 1)
        user, hashed = completed.stdout.rstrip(b"\n").split(b":", 1)
        self.assertEqual(user, b"labuser")
        self.assertEqual(len(hashed), 60)
        self.assertEqual(hashed.split(b"$")[1:3], [b"2b", b"10"])
        self.assertNotIn(password, completed.stdout + completed.stderr)
        for user, stdin in (
            ("<usuario>", password + b"\n"),
            ("labuser", b""),
            ("labuser", b"\n"),
            ("labuser", b"two\nlines\n"),
            ("labuser", b"x" * 73 + b"\n"),
        ):
            with self.subTest(user=user, stdin=stdin):
                rejected = self.run_step(user, stdin)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(rejected.stdout, b"")

    def test_a_module_in_the_working_directory_cannot_stand_in(self) -> None:
        self.require_host_bcrypt()
        with tempfile.TemporaryDirectory() as temporary:
            for module in ("bcrypt", "getpass", "re"):
                (Path(temporary) / f"{module}.py").write_text(
                    "raise SystemExit('shadowed')\n", encoding="utf-8"
                )
            completed = self.run_step(
                "labuser", b"not-a-real-password\n", Path(temporary)
            )
            # Without -I the current directory comes first on sys.path.
            shadowed = subprocess.run(
                ["/usr/bin/python3", "-c", self.code, "labuser"],
                input=b"not-a-real-password\n",
                capture_output=True,
                check=False,
                cwd=temporary,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, b"hash: prefijo $2 longitud 60\n")
        self.assertNotEqual(shadowed.returncode, 0)
        self.assertIn(b"shadowed", shadowed.stderr)

    def test_the_lock_runner_would_echo_stdin(self) -> None:
        # Why the command does not run under host_global_operation_lock.py:
        # its runner copies stdin into a pseudo-terminal with echo on, so a
        # line sent on stdin reaches stdout although the command drops it.
        marker = "throwaway-stdin-marker"
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import importlib.util, os, sys\n"
                "spec = importlib.util.spec_from_file_location('r', sys.argv[1])\n"
                "module = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(module)\n"
                "sys.exit(module.run(['/bin/sh', '-c', 'sleep 1; cat >/dev/null'],"
                " os.getppid()))\n",
                str(REPOSITORY_ROOT / "scripts/run-locked-command.py"),
            ],
            input=marker + "\n",
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(marker, completed.stdout)


class EdgeLiveParityTests(unittest.TestCase):
    """The render equals the hand-made live Traefik config, bar reviewed changes.

    The fixture is the Docker Config the live service used when this was
    codified, parsed and with every basicAuth user masked. The only
    reviewed differences are:

    - ``satisfactory-log-auth`` reads its users from a Docker Secret file
      instead of an inline (hashed) list;
    - a workload parked in ``config/platform.yml`` renders a backend with no
      server and no probe (PR #59), which the hand-made Config predates;
    - the AX ingress adds exactly ``AX_ROUTE_ADDITIONS`` and changes no
      existing router, middleware or backend.
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
        for section, additions in AX_ROUTE_ADDITIONS.items():
            existing = expected["http"].setdefault(section, {})
            self.assertFalse(set(existing) & set(additions), section)
            existing.update(copy.deepcopy(additions))
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
