"""Contract of the independent WinNest website stack (docs/WINNEST.md)."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any, Callable
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_winnest", ROOT / "scripts/validate-winnest.py"
)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def load_yaml(relative: str) -> Any:
    return yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = load_yaml("config/winnest.yml")

    def rejected(self, change: Callable[[dict[str, Any]], None], message: str) -> None:
        document = copy.deepcopy(self.document)
        change(document["winnest"])
        with self.assertRaisesRegex(ValueError, message):
            VALIDATOR.validate_catalog(document)

    def test_reviewed_catalog_is_accepted(self) -> None:
        app = VALIDATOR.validate_catalog(self.document)
        self.assertEqual(app["hostname"], "winnest.apptolast.com")
        self.assertEqual(app["edge_network"], "apptolast-edge-winnest")

    def test_identity_fields_are_pinned(self) -> None:
        for key, value in (
            ("hostname", "www.winnest.apptolast.com"),
            ("edge_network", "apptolast-edge-racinggame"),
            ("stack_name", "website"),
            ("schema_version", 2),
            ("schema_version", True),
        ):
            with self.subTest(key=key, value=value):
                self.rejected(
                    lambda app, key=key, value=value: app.update({key: value}),
                    f"{key} differs",
                )

    def test_release_must_be_a_full_commit(self) -> None:
        for value in ("9fcfdea", "9FCFDEA7ED7C4768E1A1F8DE6DCF22E0621C3AB4", 1):
            with self.subTest(value=value):
                self.rejected(
                    lambda app, value=value: app.update({"release": value}),
                    "release must identify the published commit",
                )

    def test_baseline_image_needs_its_repository_and_a_digest(self) -> None:
        digest = "sha256:" + "a" * 64
        for value in (
            "ocholoko888/winnest-website:latest",
            f"ocholoko888/racinggame@{digest}",
            f"winnest-website@{digest}",
        ):
            with self.subTest(value=value):
                self.rejected(
                    lambda app, value=value: app["images"].update({"web": value}),
                    "reviewed repository and digest",
                )

    def test_unexpected_or_missing_keys_are_rejected(self) -> None:
        self.rejected(lambda app: app.update({"replicas": 2}), "unexpected or missing")
        self.rejected(lambda app: app.pop("hostname"), "unexpected or missing")
        self.rejected(
            lambda app: app["images"].update({"api": "x"}), "unexpected or missing"
        )


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = load_yaml("config/winnest.yml")
        cls.channels = VALIDATOR.load_image_channels(cls.document)
        cls.stack = yaml.safe_load(VALIDATOR.render(cls.document, cls.channels))

    def rejected(self, change: Callable[[dict[str, Any]], None], message: str) -> None:
        stack = copy.deepcopy(self.stack)
        change(stack["services"]["web"])
        with self.assertRaisesRegex(ValueError, message):
            VALIDATOR.validate_render(stack, self.channels)

    def test_reviewed_render_is_accepted_and_runs_the_channel(self) -> None:
        VALIDATOR.validate_render(self.stack, self.channels)
        web = self.stack["services"]["web"]
        self.assertEqual(web["image"], "docker.io/ocholoko888/winnest-website:latest")
        self.assertEqual(web["deploy"]["labels"]["apptolast.autoupdate"], "true")
        self.assertEqual(web["networks"], ["edge"])
        self.assertEqual(
            self.stack["networks"]["edge"],
            {"external": True, "name": "apptolast-edge-winnest"},
        )

    def test_the_channel_is_bound_to_the_catalog_and_watched(self) -> None:
        entry = self.channels["winnest"]["web"]
        self.assertEqual(entry["baseline"], {"catalog": "winnest", "component": "web"})
        self.assertEqual(entry["mode"], "channel")
        self.assertEqual(entry["label"], "true")

    def test_the_watcher_contract_is_met(self) -> None:
        # docs/AUTOUPDATE.md: rollback, a monitor window and a health check.
        web = self.stack["services"]["web"]
        self.assertEqual(web["deploy"]["update_config"]["failure_action"], "rollback")
        self.assertEqual(web["deploy"]["update_config"]["monitor"], "120s")
        self.assertEqual(
            web["healthcheck"]["test"],
            ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/healthz"],
        )

    def test_resources_are_the_reviewed_budget(self) -> None:
        self.assertEqual(
            self.stack["services"]["web"]["deploy"]["resources"],
            {
                "reservations": {"cpus": "0.05", "memory": "16M"},
                "limits": {"cpus": "0.10", "memory": "32M"},
            },
        )

    def test_state_and_exposure_are_rejected(self) -> None:
        for change, message in (
            (
                lambda web: web["volumes"].append(
                    {"type": "bind", "source": "/srv", "target": "/srv"}
                ),
                "may only mount tmpfs",
            ),
            (lambda web: web.update({"volumes": ["/srv:/srv"]}), "may only mount tmpfs"),
            (lambda web: web.update({"secrets": ["token"]}), "secrets or configs"),
            (lambda web: web.update({"configs": ["site"]}), "secrets or configs"),
            (lambda web: web.update({"ports": ["8080:8080"]}), "through Traefik"),
        ):
            with self.subTest(message=message):
                self.rejected(change, message)

    def test_hardening_is_required(self) -> None:
        for change, message in (
            (lambda web: web.update({"read_only": False}), "read-only root"),
            (lambda web: web.pop("read_only"), "read-only root"),
            (lambda web: web.update({"cap_drop": []}), "drop every capability"),
            (lambda web: web.update({"cap_add": ["NET_RAW"]}), "add capabilities back"),
            (lambda web: web.update({"user": "0:0"}), "must run as 101:101"),
            (lambda web: web.pop("user"), "must run as 101:101"),
            (
                lambda web: web["environment"].update({"NGINX_PORT": "80"}),
                "environment differs",
            ),
            (
                lambda web: web["healthcheck"].update(
                    {"test": ["CMD", "wget", "-q", "http://127.0.0.1:80/healthz"]}
                ),
                "port Traefik forwards to",
            ),
        ):
            with self.subTest(message=message):
                self.rejected(change, message)

    def test_image_and_label_must_follow_the_channel(self) -> None:
        self.rejected(
            lambda web: web.update(
                {"image": "docker.io/ocholoko888/winnest-website:sha-1"}
            ),
            "differs from its channel entry",
        )
        self.rejected(
            lambda web: web["deploy"]["labels"].update({"apptolast.autoupdate": "false"}),
            "autoupdate label",
        )

    def test_extra_service_and_internal_network_are_rejected(self) -> None:
        stack = copy.deepcopy(self.stack)
        stack["services"]["admin"] = copy.deepcopy(stack["services"]["web"])
        with self.assertRaisesRegex(ValueError, "services changed"):
            VALIDATOR.validate_render(stack, self.channels)
        stack = copy.deepcopy(self.stack)
        stack["networks"]["edge"]["external"] = False
        with self.assertRaisesRegex(ValueError, "must stay external"):
            VALIDATOR.validate_render(stack, self.channels)


class RoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main = load_yaml("ansible/roles/winnest/tasks/main.yml")
        cls.deploy = load_yaml("ansible/roles/winnest/tasks/deploy.yml")

    def test_the_role_never_creates_the_edge_network(self) -> None:
        for path in sorted((ROOT / "ansible/roles/winnest/tasks").glob("*.yml")):
            with self.subTest(path=path.name):
                self.assertNotIn("network create", path.read_text(encoding="utf-8"))

    def test_deploy_keeps_live_digests_and_prunes(self) -> None:
        stacks = [
            task["community.docker.docker_stack"]
            for task in self.deploy
            if "community.docker.docker_stack" in task
        ]
        self.assertEqual(len(stacks), 1)
        self.assertEqual(stacks[0]["name"], "winnest")
        self.assertEqual(stacks[0]["resolve_image"], "changed")
        self.assertIs(stacks[0]["prune"], True)

    def test_mutation_only_outside_check_mode(self) -> None:
        last = self.main[-1]
        self.assertEqual(last["ansible.builtin.import_tasks"], "deploy.yml")
        self.assertEqual(last["when"], "not ansible_check_mode")

    def test_the_apply_ends_with_verified_public_https(self) -> None:
        probe = self.deploy[-1]
        argv = probe["ansible.builtin.command"]["argv"]
        self.assertEqual(argv[0], "/usr/bin/curl")
        self.assertIn("--fail", argv)
        self.assertNotIn("--insecure", argv)
        self.assertNotIn("-k", argv)
        self.assertEqual(
            argv[argv.index("--resolve") + 1],
            "{{ winnest.hostname }}:443:{{ platform_public_ipv4 }}",
        )
        self.assertEqual(argv[-1], "https://{{ winnest.hostname }}/healthz")
        self.assertEqual(probe["until"], 'winnest_public_https.stdout == "ok"')
        self.assertIs(probe["changed_when"], False)

    def test_the_playbook_takes_the_lock_and_the_capacity_preflight_first(self) -> None:
        play = load_yaml("ansible/playbooks/winnest.yml")[0]
        roles = [item["role"] for item in play["roles"]]
        self.assertEqual(
            roles,
            ["operation_lock_guard", "capacity_preflight", "winnest", "deployment_metadata"],
        )
        self.assertIn("../../config/winnest.yml", play["vars_files"])


class EdgeRouteTests(unittest.TestCase):
    def test_route_and_upstream_are_the_reviewed_ones(self) -> None:
        template = (ROOT / "stacks/edge/dynamic.yml.j2").read_text(encoding="utf-8")
        self.assertIn("rule: \"Host(`{{ winnest.hostname }}`)\"", template)
        self.assertIn("- url: http://winnest_web:8080", template)
        self.assertEqual(
            load_yaml("ansible/group_vars/all.yml")["edge_application_networks"][
                "winnest"
            ],
            "apptolast-edge-winnest",
        )


if __name__ == "__main__":
    unittest.main()
