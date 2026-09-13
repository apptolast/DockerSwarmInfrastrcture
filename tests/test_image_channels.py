"""Contract tests for config/image-channels.yml and its channel machinery."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from ansible_task_harness import AnsibleTaskAssertions

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, REPOSITORY_ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


channels = load_script(
    "validate_image_channels",
    "scripts/validate-image-channels.py",
)
resolver = load_script(
    "resolve_image_channel",
    "scripts/resolve-image-channel.py",
)

DIGEST = "sha256:" + ("a" * 64)
OTHER_DIGEST = "sha256:" + ("b" * 64)
ADOPTED_CHANNELS = {
    ("edge", "traefik"): "docker.io/library/traefik:v3",
    ("workloads", "kropia"): "docker.io/apptolast/kropia-web:latest",
    ("workloads", "minecraft"): "docker.io/itzg/minecraft-server:latest",
    ("workloads", "minecraft-stats"): (
        "docker.io/ocholoko888/minecraft-stats-web:latest"
    ),
    ("workloads", "passbolt"): "docker.io/passbolt/passbolt:latest",
    ("workloads", "portfolio-alberto"): (
        "docker.io/hgarciaalberto/personal-website:latest"
    ),
    ("workloads", "portfolio-pablo"): (
        "docker.io/ocholoko888/personal-website:latest"
    ),
    ("workloads", "selenium"): "docker.io/selenium/standalone-chrome:latest",
    ("workloads", "shlink"): "docker.io/apptolast/shlink-apptolast:latest",
    ("organizationweb", "backend"): (
        "docker.io/ocholoko888/organizationweb-api:latest"
    ),
    ("organizationweb", "web"): (
        "docker.io/ocholoko888/organizationweb-web:latest"
    ),
}
REDIS_HOLD = (
    "docker.io/library/redis:7.2-alpine@sha256:"
    "1a34bdba051ecd8a58ec8a3cc460acef"
    "697a1605e918149cc53d920673c1a0a7"
)


class ChannelMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = channels.load_unique_yaml(
            REPOSITORY_ROOT / "config/image-channels.yml"
        )
        cls.baselines = channels.load_baselines(REPOSITORY_ROOT)

    def derive(
        self,
        document: dict[str, Any],
        baselines: dict[Any, Any] | None = None,
    ) -> dict[str, Any]:
        return channels.derive_channels(document, baselines or self.baselines)

    def mutated(
        self,
        target_stack: str,
        target_service: str,
        **changes: Any,
    ) -> dict[str, Any]:
        document = copy.deepcopy(self.document)
        entry = next(
            item
            for item in document["image_channel_services"]
            if item["stack"] == target_stack and item["service"] == target_service
        )
        entry.update(changes)
        return document

    def assert_rejected(
        self,
        document: dict[str, Any],
        message: str,
        baselines: dict[Any, Any] | None = None,
    ) -> None:
        with self.assertRaisesRegex(channels.ChannelError, message):
            self.derive(document, baselines)

    def test_reviewed_map_covers_every_stack_and_changes_nothing(self) -> None:
        channel_map = self.derive(self.document)
        services = channel_map["services"]
        self.assertEqual(
            {stack: len(entries) for stack, entries in services.items()},
            {"edge": 1, "workloads": 14, "organizationweb": 4, "observability": 12},
        )
        self.assertEqual(
            channel_map["exclusions"],
            {"autoupdater": ["shepherd"], "workloads": ["n8n-runners"]},
        )
        self.assertEqual(channel_map["autoupdate_label"], "apptolast.autoupdate")
        for stack, entries in services.items():
            for name, entry in entries.items():
                with self.subTest(service=f"{stack}/{name}"):
                    # PR-B1 opts nothing in: the watcher selects no service.
                    self.assertIs(entry["autoupdate"], False)
                    self.assertEqual(entry["label"], "false")
                    if (stack, name) in ADOPTED_CHANNELS:
                        self.assertEqual(entry["mode"], "channel")
                        self.assertEqual(
                            entry["reference"], ADOPTED_CHANNELS[(stack, name)]
                        )
                    elif (stack, name) == ("workloads", "redis-coordinator"):
                        # The reviewed 7.2.11 bytes, proven by REDIS_VERSION.
                        self.assertEqual(entry["reference"], REDIS_HOLD)
                        self.assertEqual(entry["mode"], "hold")
                        self.assertTrue(entry["major_proof_required"])
                    else:
                        # Same string as rendered today, so Swarm keeps it.
                        self.assertEqual(entry["mode"], "hold")
                        self.assertTrue(entry["is_baseline"])

    def test_live_spec_identity_is_normalized_like_swarm(self) -> None:
        services = self.derive(self.document)["services"]
        redis = services["workloads"]["redis-coordinator"]
        self.assertEqual(
            redis["spec_exact"],
            REDIS_HOLD.removeprefix("docker.io/library/"),
        )
        self.assertEqual(
            redis["preflight_reference"],
            "docker.io/library/redis@" + REDIS_HOLD.split("@")[1],
        )
        self.assertTrue(
            services["workloads"]["passbolt-db"]["spec_exact"].startswith(
                "postgres@sha256:"
            )
        )
        self.assertEqual(
            services["edge"]["traefik"]["spec_pattern"],
            r"^traefik:v3@sha256:[a-f0-9]{64}$",
        )
        self.assertTrue(
            services["workloads"]["openclaw"]["spec_exact"].startswith(
                "ghcr.io/openclaw/openclaw:2026.7.1@sha256:"
            )
        )
        kropia = services["workloads"]["kropia"]
        self.assertIsNone(kropia["spec_exact"])
        self.assertEqual(
            kropia["spec_pattern"],
            r"^apptolast/kropia\-web:latest@sha256:[a-f0-9]{64}$",
        )
        self.assertEqual(kropia["preflight_reference"], kropia["reference"])

    def test_digest_only_reference_is_rejected(self) -> None:
        self.assert_rejected(
            self.mutated(
                "workloads",
                "kropia",
                reference="docker.io/apptolast/kropia-web@" + DIGEST,
            ),
            "digest-only reference",
        )
        # A digest-only reference would become :latest once the watcher
        # strips the digest, so even the exact baseline can never opt in.
        self.assert_rejected(
            self.mutated("workloads", "n8n-db", autoupdate=True),
            "hold can never auto-update",
        )
        self.assert_rejected(
            self.mutated(
                "workloads",
                "redis-coordinator",
                autoupdate=True,
            ),
            "hold can never auto-update",
        )

    def test_bare_repository_is_rejected(self) -> None:
        self.assert_rejected(
            self.mutated(
                "workloads",
                "kropia",
                reference="docker.io/apptolast/kropia-web",
            ),
            "implies :latest",
        )

    def test_another_repository_is_rejected(self) -> None:
        for reference in (
            "docker.io/example/kropia-web:latest",
            "ghcr.io/apptolast/kropia-web:latest",
            "docker.io/apptolast/kropia:latest",
        ):
            with self.subTest(reference=reference):
                self.assert_rejected(
                    self.mutated("workloads", "kropia", reference=reference),
                    "repository differs from its baseline",
                )

    def test_another_owner_tag_is_rejected(self) -> None:
        for reference in (
            "docker.io/apptolast/kropia-web:canary",
            "docker.io/apptolast/kropia-web:main@" + DIGEST,
        ):
            with self.subTest(reference=reference):
                self.assert_rejected(
                    self.mutated("workloads", "kropia", reference=reference),
                    "owner images follow :latest",
                )

    def test_malformed_references_are_rejected(self) -> None:
        for reference in (
            "Docker.io/apptolast/kropia-web:latest",
            "docker.io/apptolast/kropia-web:latest@sha256:short",
            "docker.io/apptolast/kropia-web:latest@sha512:" + ("a" * 128),
            "registry.example:5000/kropia-web:latest",
            42,
        ):
            with self.subTest(reference=reference):
                self.assert_rejected(
                    self.mutated("workloads", "kropia", reference=reference),
                    "reference",
                )

    def test_stateful_services_accept_only_their_reviewed_major(self) -> None:
        rejected = [
            # Bare `postgres:17` is Debian (uid 999) and breaks `user: 70:70`.
            ("organizationweb", "postgres", "docker.io/library/postgres:17"),
            ("organizationweb", "postgres", "postgres:latest"),
            ("organizationweb", "postgres", "postgres:17-alpine3.23"),
            # Bare `rabbitmq:4.3` is Ubuntu; `-alpine` drops management.
            ("organizationweb", "rabbitmq", "rabbitmq:4.3"),
            ("organizationweb", "rabbitmq", "rabbitmq:4.3-alpine"),
            ("organizationweb", "rabbitmq", "rabbitmq:4-management-alpine"),
            ("workloads", "shlink-db", "docker.io/library/postgres:16"),
            ("workloads", "shlink-db", "docker.io/library/postgres:17-alpine"),
            ("workloads", "passbolt-db", "docker.io/library/postgres:16-alpine"),
            ("workloads", "n8n-db", "docker.io/pgvector/pgvector:pg17"),
            ("workloads", "redis-coordinator", "docker.io/library/redis:7-alpine"),
            ("edge", "traefik", "traefik:latest"),
            ("edge", "traefik", "traefik:v3.7"),
            (
                "workloads",
                "shlink-db",
                "docker.io/library/postgres:16.10-alpine3.22@" + DIGEST,
            ),
        ]
        for stack, service, reference in rejected:
            with self.subTest(reference=reference, service=service):
                self.assert_rejected(
                    self.mutated(stack, service, reference=reference),
                    "not the reviewed major channel",
                )

    def test_reviewed_major_channels_are_accepted(self) -> None:
        accepted = {
            ("edge", "traefik"): "docker.io/library/traefik:v3",
            ("workloads", "n8n-db"): "docker.io/pgvector/pgvector:pg16",
            ("workloads", "redis-coordinator"): "docker.io/library/redis:7.2-alpine",
            ("workloads", "passbolt-db"): "docker.io/library/postgres:15-alpine",
            ("workloads", "shlink-db"): "docker.io/library/postgres:16-alpine",
            ("organizationweb", "postgres"): "postgres:17-alpine",
            ("organizationweb", "rabbitmq"): "rabbitmq:4.3-management-alpine",
        }
        document = copy.deepcopy(self.document)
        for item in document["image_channel_services"]:
            key = (item["stack"], item["service"])
            if key in accepted:
                item["reference"] = accepted.pop(key)
                item["autoupdate"] = True
        self.assertEqual(accepted, {})
        services = self.derive(document)["services"]
        self.assertEqual(
            services["organizationweb"]["postgres"]["spec_pattern"],
            r"^postgres:17\-alpine@sha256:[a-f0-9]{64}$",
        )
        self.assertEqual(
            services["edge"]["traefik"]["spec_pattern"],
            r"^traefik:v3@sha256:[a-f0-9]{64}$",
        )
        # A rollback hold on the major channel tag is also accepted.
        hold = self.mutated(
            "workloads",
            "shlink-db",
            reference="docker.io/library/postgres:16-alpine@" + DIGEST,
        )
        self.assertEqual(
            self.derive(hold)["services"]["workloads"]["shlink-db"]["spec_exact"],
            "postgres:16-alpine@" + DIGEST,
        )

    def test_major_that_differs_from_the_baseline_is_rejected(self) -> None:
        services = channels.load_unique_yaml(REPOSITORY_ROOT / "config/services.yml")
        passbolt = next(
            item for item in services["approved_services"] if item["id"] == "passbolt"
        )
        database = next(
            item for item in passbolt["images"] if item["component"] == "database"
        )
        database["source_reference"] = (
            "docker.io/library/postgres:16.10-alpine3.22@" + DIGEST
        )
        self.assert_rejected(
            self.document,
            "differs from the baseline major",
            channels.load_baselines(REPOSITORY_ROOT, services=services),
        )

        group_vars = channels.load_unique_yaml(
            REPOSITORY_ROOT / "ansible/group_vars/all.yml"
        )
        group_vars["edge_traefik_version"] = "4.0.0"
        self.assert_rejected(
            self.document,
            "differs from the baseline major",
            channels.load_baselines(REPOSITORY_ROOT, group_vars=group_vars),
        )

        organizationweb = channels.load_unique_yaml(
            REPOSITORY_ROOT / "config/organizationweb.yml"
        )
        organizationweb["organizationweb"]["images"]["postgres"] = (
            "postgres@" + DIGEST
        )
        document = self.mutated(
            "organizationweb",
            "postgres",
            reference="postgres@" + DIGEST,
        )
        self.assert_rejected(
            document,
            "re-review its version",
            channels.load_baselines(
                REPOSITORY_ROOT,
                organizationweb=organizationweb,
            ),
        )

    def test_traefik_baseline_must_equal_the_edge_pin(self) -> None:
        group_vars = channels.load_unique_yaml(
            REPOSITORY_ROOT / "ansible/group_vars/all.yml"
        )
        group_vars["edge_traefik_image"] = "traefik@" + DIGEST
        with self.assertRaisesRegex(channels.ChannelError, "edge pin"):
            channels.load_baselines(REPOSITORY_ROOT, group_vars=group_vars)

    def test_class_must_match_the_service(self) -> None:
        for stack, service, klass, expected in (
            ("workloads", "kropia", "third-party", "owner"),
            ("workloads", "selenium", "owner", "third-party"),
            ("workloads", "shlink-db", "third-party", "stateful-major"),
            ("edge", "traefik", "third-party", "stateful-major"),
        ):
            with self.subTest(service=service):
                self.assert_rejected(
                    self.mutated(stack, service, **{"class": klass}),
                    f"class must be {expected}",
                )
        self.assert_rejected(
            self.mutated("workloads", "kropia", **{"class": "floating"}),
            "class is not allowed",
        )

    def test_schema_is_strict(self) -> None:
        cases: list[tuple[dict[str, Any], str]] = []

        extra = copy.deepcopy(self.document)
        extra["unexpected"] = True
        cases.append((extra, "unexpected or missing keys"))

        missing = copy.deepcopy(self.document)
        del missing["image_channel_exclusions"]
        cases.append((missing, "unexpected or missing keys"))

        for version in (2, True, "1"):
            document = copy.deepcopy(self.document)
            document["image_channel_schema_version"] = version
            cases.append((document, "schema version"))

        label = copy.deepcopy(self.document)
        label["image_channel_autoupdate_label"] = "shepherd.enable"
        cases.append((label, "tool-neutral label"))

        empty = copy.deepcopy(self.document)
        empty["image_channel_services"] = []
        cases.append((empty, "non-empty list"))

        cases.append((self.mutated("workloads", "kropia", extra=1), "unexpected"))
        cases.append(
            (
                self.mutated(
                    "workloads",
                    "kropia",
                    baseline={"catalog": "kropia", "component": "app", "x": 1},
                ),
                "baseline: unexpected",
            )
        )
        cases.append(
            (
                self.mutated(
                    "workloads",
                    "kropia",
                    baseline={"catalog": "kropia", "component": "web"},
                ),
                "not in the reviewed catalogs",
            )
        )
        cases.append(
            (self.mutated("workloads", "kropia", autoupdate="false"), "boolean")
        )
        cases.append(
            (self.mutated("workloads", "kropia", stack="backup"), "not allowed")
        )
        cases.append(
            (self.mutated("workloads", "kropia", service="Kropia"), "invalid")
        )

        duplicate = copy.deepcopy(self.document)
        duplicate["image_channel_services"].append(
            copy.deepcopy(duplicate["image_channel_services"][1])
        )
        cases.append((duplicate, "duplicate channel entry"))

        shadowed = copy.deepcopy(self.document)
        shadowed["image_channel_exclusions"].append(
            {"stack": "workloads", "service": "kropia", "reason": "no"}
        )
        cases.append((shadowed, "duplicate channel entry"))

        no_reason = copy.deepcopy(self.document)
        no_reason["image_channel_exclusions"][0]["reason"] = " "
        cases.append((no_reason, "needs a reason"))

        fewer = copy.deepcopy(self.document)
        fewer["image_channel_exclusions"].pop()
        cases.append((fewer, "reviewed set"))

        more = copy.deepcopy(self.document)
        more["image_channel_exclusions"].append(
            {"stack": "workloads", "service": "extra", "reason": "extra"}
        )
        cases.append((more, "reviewed set"))

        for document, message in cases:
            with self.subTest(message=message):
                self.assert_rejected(document, message)

    def test_duplicate_yaml_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "channels.yml"
            for text in (
                "image_channel_schema_version: 1\nimage_channel_schema_version: 1\n",
                "image_channel_services:\n  - stack: edge\n    stack: edge\n",
            ):
                path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(channels.ChannelError, "duplicate key"):
                    channels.load_unique_yaml(path)
            path.write_text("- not-a-mapping\n", encoding="utf-8")
            with self.assertRaisesRegex(channels.ChannelError, "not a mapping"):
                channels.load_unique_yaml(path)

    def test_cli_derives_json_for_ansible(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(REPOSITORY_ROOT / "scripts/validate-image-channels.py"),
                "derive",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        derived = json.loads(completed.stdout)
        self.assertEqual(
            derived["services"]["workloads"]["kropia"]["reference"],
            ADOPTED_CHANNELS[("workloads", "kropia")],
        )


READ_ONLY_SOCKET = {
    "type": "bind",
    "source": "/var/run/docker.sock",
    "target": "/var/run/docker.sock",
    "read_only": True,
}


def synthetic_service(image: str | None, label: str | None) -> dict[str, Any]:
    service: dict[str, Any] = {
        "healthcheck": {"test": ["CMD", "true"]},
        "deploy": {
            "labels": {},
            "update_config": {"failure_action": "rollback", "monitor": "120s"},
        },
    }
    if image is not None:
        service["image"] = image
    if label is not None:
        service["deploy"]["labels"]["apptolast.autoupdate"] = label
    return service


class RenderedCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = channels.load_unique_yaml(
            REPOSITORY_ROOT / "config/image-channels.yml"
        )
        cls.baselines = channels.load_baselines(REPOSITORY_ROOT)
        cls.channel_map = channels.derive_channels(cls.document, cls.baselines)

    def rendered_for(self, channel_map: dict[str, Any]) -> dict[str, Any]:
        rendered: dict[str, Any] = {}
        for stack in channels.RENDERED_STACKS:
            services = {
                name: synthetic_service(entry["reference"], entry["label"])
                for name, entry in channel_map["services"].get(stack, {}).items()
            }
            for name in channel_map["exclusions"].get(stack, []):
                services[name] = synthetic_service("local/excluded:tag", "false")
            if stack == "autoupdater":
                services["shepherd"]["environment"] = dict(
                    AutoupdaterEnvironmentTests.VALID
                )
                services["shepherd"]["volumes"] = [dict(READ_ONLY_SOCKET)]
            rendered[stack] = {"services": services}
        return rendered

    def assert_rejected(
        self,
        rendered: dict[str, Any],
        message: str,
        channel_map: dict[str, Any] | None = None,
    ) -> None:
        with self.assertRaisesRegex(channels.ChannelError, message):
            channels.validate_rendered(channel_map or self.channel_map, rendered)

    def test_complete_render_is_accepted(self) -> None:
        channels.validate_rendered(self.channel_map, self.rendered_for(self.channel_map))

    @unittest.skipUnless(
        all(
            (REPOSITORY_ROOT / ".build" / stack / "stack.yml").is_file()
            for stack in (
                "edge",
                "workloads",
                "organizationweb",
                "observability",
                "autoupdater",
            )
        ),
        "rendered stacks are produced by scripts/validate-iac.sh",
    )
    def test_real_renders_are_accepted(self) -> None:
        channels.validate_rendered(
            self.channel_map,
            channels.load_rendered(REPOSITORY_ROOT / ".build"),
        )

    def test_rendered_service_missing_from_the_file_is_rejected(self) -> None:
        rendered = self.rendered_for(self.channel_map)
        rendered["workloads"]["services"]["new-service"] = synthetic_service(
            "docker.io/example/new:latest",
            "false",
        )
        self.assert_rejected(rendered, r"unlisted \['new-service'\]")

    def test_listed_service_missing_from_the_render_is_rejected(self) -> None:
        rendered = self.rendered_for(self.channel_map)
        del rendered["workloads"]["services"]["kropia"]
        self.assert_rejected(rendered, r"not rendered \['kropia'\]")
        rendered = self.rendered_for(self.channel_map)
        del rendered["workloads"]["services"]["n8n-runners"]
        self.assert_rejected(rendered, r"not rendered \['n8n-runners'\]")

    def test_missing_rendered_stack_is_rejected(self) -> None:
        rendered = self.rendered_for(self.channel_map)
        del rendered["edge"]
        self.assert_rejected(rendered, "rendered stack is missing: edge")
        rendered = self.rendered_for(self.channel_map)
        rendered["unknown"] = {"services": {}}
        self.assert_rejected(rendered, "not allowed")

    def test_label_true_while_autoupdate_false_is_rejected(self) -> None:
        for label in ("true", None, "False", "yes"):
            with self.subTest(label=label):
                rendered = self.rendered_for(self.channel_map)
                service = rendered["workloads"]["services"]["kropia"]
                if label is None:
                    del service["deploy"]["labels"]["apptolast.autoupdate"]
                else:
                    service["deploy"]["labels"]["apptolast.autoupdate"] = label
                self.assert_rejected(rendered, "label must be 'false'")

    def test_excluded_service_cannot_opt_in(self) -> None:
        rendered = self.rendered_for(self.channel_map)
        rendered["workloads"]["services"]["n8n-runners"]["deploy"]["labels"][
            "apptolast.autoupdate"
        ] = "true"
        self.assert_rejected(rendered, "excluded service opts in")

    def test_image_drift_from_the_channel_is_rejected(self) -> None:
        rendered = self.rendered_for(self.channel_map)
        rendered["workloads"]["services"]["kropia"]["image"] = (
            "docker.io/apptolast/kropia-web:latest@" + DIGEST
        )
        self.assert_rejected(rendered, "image drift")

    def test_opted_in_service_needs_rollback_monitor_and_healthcheck(self) -> None:
        document = copy.deepcopy(self.document)
        for item in document["image_channel_services"]:
            if item["service"] == "kropia":
                item["autoupdate"] = True
        channel_map = channels.derive_channels(document, self.baselines)
        valid = self.rendered_for(channel_map)
        channels.validate_rendered(channel_map, valid)
        mutations = {
            "failure_action rollback": lambda s: s["deploy"]["update_config"].update(
                failure_action="pause"
            ),
            "monitor window": lambda s: s["deploy"]["update_config"].update(
                monitor="0s"
            ),
            "healthcheck": lambda s: s.pop("healthcheck"),
        }
        for message, mutate in mutations.items():
            with self.subTest(message=message):
                rendered = copy.deepcopy(valid)
                mutate(rendered["workloads"]["services"]["kropia"])
                self.assert_rejected(rendered, message, channel_map)
        for healthcheck in (
            {"disable": True, "test": ["CMD", "true"]},
            {"test": ["NONE"]},
            {"test": []},
        ):
            with self.subTest(healthcheck=healthcheck):
                rendered = copy.deepcopy(valid)
                rendered["workloads"]["services"]["kropia"]["healthcheck"] = healthcheck
                self.assert_rejected(rendered, "healthcheck", channel_map)
        rendered = copy.deepcopy(valid)
        rendered["workloads"]["services"]["kropia"]["deploy"]["update_config"][
            "monitor"
        ] = "soon"
        self.assert_rejected(rendered, "duration is malformed", channel_map)

    def test_docker_socket_is_rejected_outside_autoupdater(self) -> None:
        for volume in (
            {"type": "bind", "source": "/var/run/docker.sock", "target": "/s"},
            {"type": "bind", "source": "/run/docker.sock", "target": "/s"},
            "/var/run/docker.sock:/var/run/docker.sock",
        ):
            for stack, service in (("workloads", "kropia"), ("edge", "traefik")):
                with self.subTest(volume=volume, stack=stack):
                    rendered = self.rendered_for(self.channel_map)
                    rendered[stack]["services"][service]["volumes"] = [volume]
                    self.assert_rejected(rendered, "Docker socket")

    def test_autoupdater_stack_is_required_and_checked(self) -> None:
        rendered = self.rendered_for(self.channel_map)
        channels.validate_rendered(self.channel_map, rendered)
        del rendered["autoupdater"]
        self.assert_rejected(rendered, "rendered stack is missing: autoupdater")
        rendered = self.rendered_for(self.channel_map)
        shepherd = rendered["autoupdater"]["services"]["shepherd"]
        shepherd["environment"]["FILTER_SERVICES"] = ""
        self.assert_rejected(rendered, "FILTER_SERVICES")
        rendered = self.rendered_for(self.channel_map)
        rendered["autoupdater"]["services"]["shepherd"]["environment"][
            "IGNORELIST_SERVICES"
        ] = "workloads_n8n"
        self.assert_rejected(rendered, "must be absent")
        rendered = self.rendered_for(self.channel_map)
        rendered["autoupdater"]["services"]["shepherd"]["deploy"]["labels"][
            "apptolast.autoupdate"
        ] = "true"
        self.assert_rejected(rendered, "excluded service opts in")

    def test_docker_socket_in_autoupdater_must_be_read_only(self) -> None:
        for volume in (
            {**READ_ONLY_SOCKET, "read_only": False},
            {key: value for key, value in READ_ONLY_SOCKET.items() if key != "read_only"},
            "/var/run/docker.sock:/var/run/docker.sock",
            "/var/run/docker.sock:/var/run/docker.sock:rw",
        ):
            with self.subTest(volume=volume):
                rendered = self.rendered_for(self.channel_map)
                rendered["autoupdater"]["services"]["shepherd"]["volumes"] = [volume]
                self.assert_rejected(rendered, "must be read-only")
        rendered = self.rendered_for(self.channel_map)
        rendered["autoupdater"]["services"]["shepherd"]["volumes"] = [
            "/var/run/docker.sock:/var/run/docker.sock:ro"
        ]
        channels.validate_rendered(self.channel_map, rendered)


class AutoupdaterEnvironmentTests(unittest.TestCase):
    VALID = {
        "FILTER_SERVICES": "label=apptolast.autoupdate=true",
        "SLEEP_TIME": "1h",
        "TIMEOUT": "900",
        "VERBOSE": "true",
        "TZ": "UTC",
    }

    def assert_rejected(self, environment: Any, message: str) -> None:
        with self.assertRaisesRegex(channels.ChannelError, message):
            channels.validate_autoupdater_environment(
                environment,
                channels.AUTOUPDATE_LABEL,
            )

    def test_reviewed_environment_is_accepted(self) -> None:
        channels.validate_autoupdater_environment(
            dict(self.VALID),
            channels.AUTOUPDATE_LABEL,
        )
        channels.validate_autoupdater_environment(
            [f"{key}={value}" for key, value in self.VALID.items()],
            channels.AUTOUPDATE_LABEL,
        )
        channels.validate_autoupdater_environment(
            {**self.VALID, "WITH_REGISTRY_AUTH": "true", "REGISTRY_USER": "bot"},
            channels.AUTOUPDATE_LABEL,
        )

    def test_false_value_for_a_presence_switch_is_rejected(self) -> None:
        for key in sorted(channels.SHEPHERD_PRESENCE_SWITCHES):
            for value in ("false", "0", "", "off", "FALSE"):
                with self.subTest(key=key, value=value):
                    self.assert_rejected(
                        {**self.VALID, key: value},
                        "must be absent, not false",
                    )

    def test_forbidden_settings_are_rejected_even_when_enabled(self) -> None:
        for key in sorted(channels.SHEPHERD_FORBIDDEN_KEYS):
            with self.subTest(key=key):
                self.assert_rejected({**self.VALID, key: "true"}, "must be absent")

    def test_empty_or_wrong_filter_is_rejected(self) -> None:
        for value in (
            "",
            "label=shepherd.enable=true",
            "label=apptolast.autoupdate",
            "label=apptolast.autoupdate=false",
            "name=workloads_kropia",
        ):
            with self.subTest(value=value):
                self.assert_rejected(
                    {**self.VALID, "FILTER_SERVICES": value},
                    "FILTER_SERVICES",
                )
        without_filter = dict(self.VALID)
        del without_filter["FILTER_SERVICES"]
        self.assert_rejected(without_filter, "FILTER_SERVICES")
        self.assert_rejected(None, "FILTER_SERVICES")

    def test_registry_auth_needs_a_user_and_unknown_keys_are_rejected(self) -> None:
        self.assert_rejected(
            {**self.VALID, "WITH_REGISTRY_AUTH": "true"},
            "set together",
        )
        self.assert_rejected({**self.VALID, "REGISTRY_USER": "bot"}, "set together")
        self.assert_rejected(
            {**self.VALID, "WITH_REGISTRY_AUTH": "yes", "REGISTRY_USER": "bot"},
            "exactly true",
        )
        self.assert_rejected(
            {**self.VALID, "APPRISE_SIDECAR_URL": "http://x"},
            "unexpected watcher settings",
        )
        self.assert_rejected(
            ["FILTER_SERVICES=a", "FILTER_SERVICES=b"],
            "duplicate environment key",
        )


class ChannelResolverTests(unittest.TestCase):
    REFERENCES = {
        "docker.io/apptolast/kropia-web:latest": "apptolast/kropia-web",
        "docker.io/library/postgres:17-alpine": "postgres",
    }
    REFERENCE = "docker.io/apptolast/kropia-web:latest"

    def descriptor(self, **changes: Any) -> dict[str, Any]:
        value: dict[str, Any] = {
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "digest": DIGEST,
            "size": 1024,
        }
        value.update(changes)
        return value

    def inspect(self, **changes: Any) -> list[dict[str, Any]]:
        value: dict[str, Any] = {
            "Os": "linux",
            "Architecture": "amd64",
            "Id": OTHER_DIGEST,
            "RepoDigests": ["apptolast/kropia-web@" + DIGEST],
        }
        value.update(changes)
        return [value]

    def test_channel_head_resolves_to_its_digest(self) -> None:
        for media_type in sorted(resolver.ALLOWED_MEDIA_TYPES):
            with self.subTest(media_type=media_type):
                self.assertEqual(
                    resolver.resolve_channel_reference(
                        self.REFERENCE,
                        self.REFERENCES,
                        self.descriptor(mediaType=media_type),
                    ),
                    f"{self.REFERENCE}@{DIGEST}",
                )
        self.assertEqual(
            resolver.resolve_channel_reference(
                self.REFERENCE,
                self.REFERENCES,
                self.descriptor(
                    manifests=[
                        {"platform": {"os": "linux", "architecture": "arm64"}},
                        {"platform": {"os": "linux", "architecture": "amd64"}},
                    ]
                ),
            ),
            f"{self.REFERENCE}@{DIGEST}",
        )

    def test_non_channel_references_and_bad_descriptors_are_rejected(self) -> None:
        for reference in (
            "docker.io/apptolast/kropia-web:canary",
            f"{self.REFERENCE}@{DIGEST}",
            "docker.io/example/other:latest",
        ):
            with self.subTest(reference=reference):
                with self.assertRaisesRegex(
                    resolver.ChannelImageError,
                    "not a reviewed channel",
                ):
                    resolver.resolve_channel_reference(
                        reference,
                        self.REFERENCES,
                        self.descriptor(),
                    )
        for descriptor in (
            None,
            [],
            {},
            self.descriptor(mediaType="text/plain"),
            self.descriptor(digest="sha256:invalid"),
            self.descriptor(size=0),
            self.descriptor(size=True),
            self.descriptor(
                manifests=[{"platform": {"os": "linux", "architecture": "arm64"}}]
            ),
            self.descriptor(
                mediaType="application/vnd.oci.image.manifest.v1+json",
                manifests=[],
            ),
        ):
            with self.subTest(descriptor=descriptor):
                with self.assertRaises(resolver.ChannelImageError):
                    resolver.resolve_channel_reference(
                        self.REFERENCE,
                        self.REFERENCES,
                        descriptor,
                    )

    def test_local_image_must_expose_the_resolved_digest(self) -> None:
        resolved = f"{self.REFERENCE}@{DIGEST}"
        resolver.verify_channel_image(resolved, self.REFERENCES, self.inspect())
        resolver.verify_channel_image(
            f"docker.io/library/postgres:17-alpine@{DIGEST}",
            self.REFERENCES,
            self.inspect(RepoDigests=["docker.io/library/postgres@" + DIGEST]),
        )
        rejected = [
            (f"docker.io/example/other:latest@{DIGEST}", self.inspect()),
            (f"{self.REFERENCE}@sha256:invalid", self.inspect()),
            (resolved, {}),
            (resolved, []),
            (resolved, self.inspect() * 2),
            (resolved, ["not-an-object"]),
            (resolved, self.inspect(Architecture="arm64")),
            (resolved, self.inspect(Os="windows")),
            (resolved, self.inspect(Id="not-a-content-id")),
            (resolved, self.inspect(RepoDigests=None)),
            (resolved, self.inspect(RepoDigests=[123])),
            (resolved, self.inspect(RepoDigests=["apptolast/kropia-web@" + OTHER_DIGEST])),
        ]
        for reference, document in rejected:
            with self.subTest(reference=reference, document=document):
                with self.assertRaises(resolver.ChannelImageError):
                    resolver.verify_channel_image(
                        reference,
                        self.REFERENCES,
                        document,
                    )

    def test_channel_set_comes_only_from_the_reviewed_file(self) -> None:
        references = resolver.load_channel_references(REPOSITORY_ROOT)
        self.assertEqual(set(references), set(ADOPTED_CHANNELS.values()))
        self.assertEqual(
            references["docker.io/itzg/minecraft-server:latest"],
            "itzg/minecraft-server",
        )

    def test_cli_resolves_and_rejects_invalid_input(self) -> None:
        command = [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts/resolve-image-channel.py"),
            "resolve",
            "--reference",
            self.REFERENCE,
        ]
        valid = subprocess.run(
            command,
            input=json.dumps(self.descriptor()).encode("utf-8"),
            capture_output=True,
            check=False,
        )
        self.assertEqual(valid.returncode, 0, valid.stderr.decode("utf-8"))
        self.assertEqual(
            valid.stdout.decode("utf-8").strip(),
            f"{self.REFERENCE}@{DIGEST}",
        )
        verify = subprocess.run(
            [
                sys.executable,
                str(REPOSITORY_ROOT / "scripts/resolve-image-channel.py"),
                "verify",
                "--resolved-reference",
                f"{self.REFERENCE}@{DIGEST}",
            ],
            input=json.dumps(self.inspect()).encode("utf-8"),
            capture_output=True,
            check=False,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr.decode("utf-8"))
        for payload, message in (
            (b"not-json", b"not valid JSON"),
            (b"\xff", b"not valid JSON"),
            (b" " * (resolver.MAX_JSON_BYTES + 1), b"exceeds the size limit"),
        ):
            with self.subTest(message=message):
                invalid = subprocess.run(
                    command,
                    input=payload,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(invalid.returncode, 0)
                self.assertIn(message, invalid.stderr)


class ChannelWiringTests(unittest.TestCase):
    def test_stacks_render_images_and_labels_only_from_the_channel_map(self) -> None:
        for stack in ("edge", "workloads", "organizationweb", "observability"):
            with self.subTest(stack=stack):
                template = (
                    REPOSITORY_ROOT / f"stacks/{stack}/stack.yml.j2"
                ).read_text(encoding="utf-8")
                self.assertIn("apptolast.autoupdate", template)
                self.assertIn("image_channels_map", template)
                self.assertNotIn("organizationweb.images.", template)
                self.assertNotIn("edge_traefik_image", template)

    def test_stack_deploys_keep_the_live_digest_of_unchanged_images(self) -> None:
        for path in (
            "ansible/roles/edge/tasks/deploy.yml",
            "ansible/roles/workloads/tasks/deploy.yml",
            "ansible/roles/organizationweb/tasks/deploy.yml",
            "ansible/roles/observability/tasks/deploy.yml",
        ):
            with self.subTest(path=path):
                tasks = yaml.safe_load((REPOSITORY_ROOT / path).read_text())
                deploys = [
                    task["community.docker.docker_stack"]
                    for task in tasks
                    if "community.docker.docker_stack" in task
                ]
                self.assertEqual(len(deploys), 1)
                self.assertEqual(deploys[0]["resolve_image"], "changed")
                self.assertIs(deploys[0]["prune"], True)

    def test_retired_single_entry_contract_is_gone(self) -> None:
        self.assertFalse((REPOSITORY_ROOT / "config/workload-image-updates.yml").exists())
        self.assertFalse((REPOSITORY_ROOT / "scripts/resolve-tracked-image.py").exists())
        for path in (
            "scripts/deploy-ansible.sh",
            "scripts/validate-deployment-metadata.py",
            "scripts/validate-iac.sh",
        ):
            with self.subTest(path=path):
                text = (REPOSITORY_ROOT / path).read_text(encoding="utf-8")
                self.assertIn("config/image-channels.yml", text)
                self.assertNotIn("workload-image-updates", text)
        gate = (REPOSITORY_ROOT / "scripts/validate-iac.sh").read_text(encoding="utf-8")
        self.assertIn("scripts/validate-image-channels.py validate", gate)


def derive_services(**references: str) -> dict[str, Any]:
    """Derive the reviewed map with `stack/service` references replaced."""
    document = channels.load_unique_yaml(REPOSITORY_ROOT / "config/image-channels.yml")
    for item in document["image_channel_services"]:
        key = f"{item['stack']}/{item['service']}"
        if key in references:
            item["reference"] = references.pop(key)
    if references:
        raise AssertionError(f"unknown entries: {sorted(references)}")
    return channels.derive_channels(
        document, channels.load_baselines(REPOSITORY_ROOT)
    )["services"]


class MajorProofTests(unittest.TestCase):
    def test_every_major_channel_has_a_proof(self) -> None:
        self.assertEqual(
            set(channels.MAJOR_PROOFS), set(channels.STATEFUL_MAJOR_CHANNELS)
        )
        for stack, entries in derive_services().items():
            for name, entry in entries.items():
                with self.subTest(service=f"{stack}/{name}"):
                    if entry["class"] == "stateful-major":
                        self.assertIn(entry["major_proof"]["source"], {"env", "label"})
                    else:
                        self.assertIsNone(entry["major_proof"])
                    # Every reviewed entry today is a baseline or a channel,
                    # except the redis hold back on its reviewed 7.2 bytes.
                    self.assertIs(
                        entry["major_proof_required"],
                        (stack, name) == ("workloads", "redis-coordinator"),
                    )

    def test_proof_patterns_accept_only_the_baseline_major(self) -> None:
        cases = {
            ("workloads", "n8n-db"): ("PG_MAJOR", ["16"], ["17", "160", "1", "16.1"]),
            ("workloads", "passbolt-db"): ("PG_MAJOR", ["15"], ["16", "5"]),
            ("workloads", "shlink-db"): ("PG_MAJOR", ["16"], ["17", "15"]),
            ("organizationweb", "postgres"): ("PG_MAJOR", ["17"], ["18", "16"]),
            ("workloads", "redis-coordinator"): (
                "REDIS_VERSION",
                ["7.2.11", "7.2.0"],
                ["7.4.0", "7.2", "17.2.1", "7x2.1"],
            ),
            ("organizationweb", "rabbitmq"): (
                "RABBITMQ_VERSION",
                ["4.3.5"],
                ["4.4.0", "4.30.1", "4.3"],
            ),
            ("edge", "traefik"): (
                "org.opencontainers.image.version",
                ["v3.7.9", "v3.0.0"],
                ["v4.0.0", "3.7.9", "v3.7"],
            ),
        }
        services = derive_services()
        for (stack, name), (key, accepted, rejected) in cases.items():
            proof = services[stack][name]["major_proof"]
            with self.subTest(service=f"{stack}/{name}"):
                self.assertEqual(proof["key"], key)
                for value in accepted:
                    self.assertRegex(value, proof["pattern"])
                for value in rejected:
                    self.assertNotRegex(value, proof["pattern"])

    def test_only_a_non_baseline_stateful_hold_must_prove_its_major(self) -> None:
        services = derive_services(
            **{
                "workloads/shlink-db": "docker.io/library/postgres:16-alpine@" + DIGEST,
                "workloads/passbolt-db": "docker.io/library/postgres:15-alpine",
                "edge/traefik": "docker.io/library/traefik:v3@" + DIGEST,
            }
        )
        self.assertIs(services["workloads"]["shlink-db"]["major_proof_required"], True)
        self.assertIs(services["edge"]["traefik"]["major_proof_required"], True)
        # A channel tag is resolved by the registry; a baseline was reviewed.
        for stack, name in (
            ("workloads", "passbolt-db"),
            ("organizationweb", "postgres"),
            ("workloads", "portfolio-alberto"),
        ):
            with self.subTest(service=f"{stack}/{name}"):
                self.assertIs(services[stack][name]["major_proof_required"], False)

    def test_major_is_proved_before_any_stack_mutation(self) -> None:
        name = "Prove the upstream major of every non-baseline stateful hold"
        preflight = yaml.safe_load(
            (REPOSITORY_ROOT / "ansible/roles/image_preflight/tasks/main.yml").read_text()
        )
        deploy = yaml.safe_load(
            (REPOSITORY_ROOT / "ansible/roles/organizationweb/tasks/deploy.yml").read_text()
        )
        for tasks, pull in ((preflight, False), (deploy, True)):
            include = next(task for task in tasks if task.get("name") == name)
            with self.subTest(pull=pull):
                self.assertEqual(
                    include["ansible.builtin.include_role"],
                    {"name": "image_channels", "tasks_from": "prove_major.yml"},
                )
                self.assertIs(include["vars"]["image_channels_major_proof_pull"], pull)
        mutation = next(
            index
            for index, task in enumerate(deploy)
            if "community.docker.docker_stack" in task
        )
        proof = next(i for i, task in enumerate(deploy) if task.get("name") == name)
        self.assertLess(proof, mutation)


class MajorProofGateTests(AnsibleTaskAssertions, unittest.TestCase):
    TASKS = "ansible/roles/image_channels/tasks/prove_major.yml"
    TASK = "Require every stateful hold to run its reviewed upstream major"
    MESSAGE = "does not run its reviewed upstream major"

    @classmethod
    def setUpClass(cls) -> None:
        services = derive_services(
            **{
                "workloads/shlink-db": "docker.io/library/postgres:16-alpine@" + DIGEST,
                "organizationweb/rabbitmq": "rabbitmq:4.3-management-alpine@" + DIGEST,
                "edge/traefik": "docker.io/library/traefik:v3@" + DIGEST,
            }
        )
        cls.shlink = services["workloads"]["shlink-db"]
        cls.rabbitmq = services["organizationweb"]["rabbitmq"]
        cls.traefik = services["edge"]["traefik"]
        cls.baseline = services["workloads"]["passbolt-db"]

    @staticmethod
    def inspection(entry: dict, env: Any = None, labels: Any = None) -> dict:
        return {
            "item": entry,
            "stdout": json.dumps([{"Config": {"Env": env, "Labels": labels}}]),
        }

    def gate(self, *inspections: dict) -> dict:
        return {"image_channels_major_proof_inspections": {"results": list(inspections)}}

    def test_images_that_state_their_reviewed_major_are_accepted(self) -> None:
        self.assert_task_accepts(
            self.TASKS,
            self.TASK,
            self.gate(
                self.inspection(
                    self.shlink, ["PATH=/usr/bin", "PG_MAJOR=16", "PG_VERSION=16.10"]
                ),
                self.inspection(self.rabbitmq, ["RABBITMQ_VERSION=4.3.5"]),
                self.inspection(
                    self.traefik, None, {"org.opencontainers.image.version": "v3.7.9"}
                ),
            ),
        )

    def test_images_of_another_or_unstated_major_are_rejected(self) -> None:
        cases = {
            "postgres 17 bytes under a 16 tag": self.inspection(
                self.shlink, ["PG_MAJOR=17", "PG_VERSION=17.11"]
            ),
            "no PG_MAJOR": self.inspection(self.shlink, ["PG_VERSION=16.10"]),
            "prefixed key": self.inspection(self.shlink, ["XPG_MAJOR=16"]),
            "traefik v4": self.inspection(
                self.traefik, None, {"org.opencontainers.image.version": "v4.0.0"}
            ),
            "env key only as a label": self.inspection(
                self.rabbitmq, None, {"RABBITMQ_VERSION": "4.3.5"}
            ),
            "entry not flagged for a proof": self.inspection(
                self.baseline, ["PG_MAJOR=15"]
            ),
        }
        for case, inspection in cases.items():
            with self.subTest(case=case):
                self.assert_task_rejects(
                    self.TASKS, self.TASK, self.gate(inspection), self.MESSAGE
                )


if __name__ == "__main__":
    unittest.main()
