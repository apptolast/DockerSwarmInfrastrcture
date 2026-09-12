"""Contract tests for the registered image watcher stack (Shepherd)."""

from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from ansible_task_harness import AnsibleTaskAssertions

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = "ansible/roles/autoupdater/tasks/deploy.yml"
LIVE_GATE = "Require the live watcher spec to be exactly the reviewed one"


def load_script(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


autoupdater = load_script("validate_autoupdater", "scripts/validate-autoupdater.py")
channels = autoupdater.channels


class AutoupdaterValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = yaml.safe_load((ROOT / "config/autoupdater.yml").read_text())
        self.catalog = autoupdater.validate_catalog(copy.deepcopy(self.document))
        _rendered, self.stack = autoupdater.render_validated(dict(self.catalog))

    def service(self, stack: dict[str, Any]) -> dict[str, Any]:
        return stack["services"]["shepherd"]

    def assert_render_rejected(self, mutate, message: str) -> None:
        stack = copy.deepcopy(self.stack)
        mutate(stack)
        with self.assertRaisesRegex(autoupdater.AutoupdaterError, message):
            autoupdater.validate_render(stack, self.catalog)

    def assert_catalog_rejected(self, message: str, **changes: Any) -> None:
        document = copy.deepcopy(self.document)
        document["autoupdater"].update(changes)
        with self.assertRaisesRegex(autoupdater.AutoupdaterError, message):
            autoupdater.validate_catalog(document)

    def test_reviewed_catalog_renders_the_exact_watcher(self) -> None:
        service = self.service(self.stack)
        self.assertEqual(
            service["image"],
            "containrrr/shepherd:v1.8.1@sha256:"
            "b117c2394832e088932d5e1eebb8df6c1924f47d0fef31788cb310c0fe3bf7db",
        )
        self.assertEqual(
            service["environment"],
            {
                "FILTER_SERVICES": "label=apptolast.autoupdate=true",
                "SLEEP_TIME": "1h",
                "TIMEOUT": "900",
                "VERBOSE": "true",
                "TZ": "UTC",
                "WITH_REGISTRY_AUTH": "true",
                "REGISTRY_USER": "ocholoko888",
            },
        )
        self.assertEqual(service["deploy"]["replicas"], 1)
        self.assertEqual(
            self.stack["secrets"],
            {
                "registry_password": {
                    "external": True,
                    "name": "autoupdater-dockerhub-pat-v1",
                }
            },
        )
        # Shepherd reads the password from this exact file.
        self.assertEqual(service["secrets"][0]["target"], "shepherd_registry_password")
        self.assertNotIn("REGISTRY_PASSWORD", service["environment"])

    def test_kill_switch_renders_zero_replicas_and_keeps_the_service(self) -> None:
        catalog = dict(self.catalog, enabled=False)
        _rendered, stack = autoupdater.render_validated(catalog)
        self.assertEqual(self.service(stack)["deploy"]["replicas"], 0)
        self.assertEqual(set(stack["services"]), {"shepherd"})
        with self.assertRaisesRegex(autoupdater.AutoupdaterError, "replicas must be 1"):
            autoupdater.validate_render(stack, self.catalog)
        for value in ("false", 0, None):
            with self.subTest(enabled=value):
                self.assert_catalog_rejected("boolean kill switch", enabled=value)

    def test_catalog_pins_every_reviewed_value(self) -> None:
        other = "sha256:" + "0" * 64
        for image in (
            "containrrr/shepherd:latest",
            "containrrr/shepherd:v1.8.1",
            "containrrr/shepherd:latest@" + self.catalog["image"].split("@")[1],
            "containrrr/shepherd:v1.8.1@" + other,
            "docker.io/containrrr/shepherd:v1.8.1@"
            + self.catalog["image"].split("@")[1],
        ):
            with self.subTest(image=image):
                self.assert_catalog_rejected("reviewed Shepherd", image=image)
        for sleep_time in ("20m", "5m", "", "1h0m"):
            with self.subTest(sleep_time=sleep_time):
                self.assert_catalog_rejected("rate-limit", sleep_time=sleep_time)
        self.assert_catalog_rejected("timeout_seconds", timeout_seconds="900")
        self.assert_catalog_rejected("registry_user", registry_user="someone")
        for secret in ("", "autoupdater-dockerhub-pat", "shepherd_registry_password"):
            with self.subTest(secret=secret):
                self.assert_catalog_rejected(
                    "versioned external secret", registry_password_secret=secret
                )
        document = copy.deepcopy(self.document)
        document["autoupdater"]["registry_password"] = "never-in-git"
        with self.assertRaisesRegex(autoupdater.AutoupdaterError, "unexpected"):
            autoupdater.validate_catalog(document)

    def test_presence_switches_and_scope_wideners_are_rejected(self) -> None:
        def setter(key: str, value: str):
            return lambda stack: self.service(stack)["environment"].__setitem__(
                key, value
            )

        self.assert_render_rejected(
            setter("WITH_REGISTRY_AUTH", "false"), "absent, not false"
        )
        self.assert_render_rejected(
            setter("ROLLBACK_ON_FAILURE", "false"), "absent, not false"
        )
        self.assert_render_rejected(setter("FILTER_SERVICES", ""), "FILTER_SERVICES")
        self.assert_render_rejected(
            setter("IGNORELIST_SERVICES", "workloads_n8n"), "must be absent"
        )
        self.assert_render_rejected(setter("UPDATE_OPTIONS", "--force"), "absent")
        self.assert_render_rejected(
            setter("REGISTRY_PASSWORD", "secret"), "unexpected watcher settings"
        )
        self.assert_render_rejected(setter("SLEEP_TIME", "5m"), "exact set")
        self.assert_render_rejected(
            lambda stack: self.service(stack)["environment"].pop("VERBOSE"),
            "exact set",
        )

    def test_socket_secret_placement_and_resources_are_exact(self) -> None:
        def socket(**changes: Any):
            return lambda stack: self.service(stack)["volumes"][0].update(changes)

        self.assert_render_rejected(socket(read_only=False), "read-only")
        self.assert_render_rejected(socket(source="/run/docker.sock"), "read-only")
        self.assert_render_rejected(
            lambda stack: self.service(stack)["volumes"].append(
                {"type": "bind", "source": "/", "target": "/host"}
            ),
            "only the Docker socket",
        )
        cases = {
            "replicas must be 1": lambda s: self.service(s)["deploy"].update(
                replicas=2
            ),
            "only on the manager": lambda s: self.service(s)["deploy"].update(
                placement={"constraints": ["node.role == worker"]}
            ),
            "never opt itself in": lambda s: self.service(s)["deploy"].update(
                labels={"apptolast.autoupdate": "true"}
            ),
            "capacity budget": lambda s: self.service(s)["deploy"]["resources"][
                "limits"
            ].update(memory="128M"),
            "registry password secret": lambda s: self.service(s).pop("secrets"),
            "reviewed external secret": lambda s: s["secrets"][
                "registry_password"
            ].update(external=False),
            "reviewed mount": lambda s: self.service(s)["secrets"][0].update(
                mode=0o444
            ),
            "must not declare ports": lambda s: self.service(s).update(
                ports=["8080:8080"]
            ),
            "differs from the pin": lambda s: self.service(s).update(
                image="containrrr/shepherd:latest"
            ),
            "exactly the shepherd service": lambda s: s["services"].update(
                other={"image": "busybox"}
            ),
        }
        for message, mutate in cases.items():
            with self.subTest(message=message):
                self.assert_render_rejected(mutate, message)

    def test_cli_renders_after_the_docker_format_check(self) -> None:
        with mock.patch.object(autoupdater, "docker_stack_config") as check:
            self.assertEqual(autoupdater.main([]), 0)
        check.assert_called_once()
        with mock.patch.object(
            autoupdater,
            "docker_stack_config",
            side_effect=autoupdater.AutoupdaterError("Docker rejected"),
        ):
            self.assertEqual(autoupdater.main([]), 1)

    def test_rendered_watcher_passes_the_channel_gate(self) -> None:
        channel_map = channels.load_channel_map(ROOT)
        self.assertNotIn("autoupdater", channel_map["services"])
        self.assertEqual(channel_map["exclusions"]["autoupdater"], ["shepherd"])
        self.assertIn("autoupdater", channels.RENDERED_STACKS)


class AutoupdaterWiringTests(unittest.TestCase):
    def test_playbook_locks_and_checks_capacity_before_the_role(self) -> None:
        play = yaml.safe_load((ROOT / "ansible/playbooks/autoupdater.yml").read_text())[0]
        self.assertEqual(
            [item["role"] for item in play["roles"]],
            [
                "operation_lock_guard",
                "capacity_preflight",
                "autoupdater",
                "deployment_metadata",
            ],
        )
        self.assertIn("../../config/autoupdater.yml", play["vars_files"])
        site = yaml.safe_load((ROOT / "ansible/playbooks/site.yml").read_text())[0]
        roles = [item["role"] for item in site["roles"]]
        for application in ("edge", "workloads", "observability"):
            self.assertLess(roles.index(application), roles.index("autoupdater"))
        self.assertLess(roles.index("autoupdater"), roles.index("deployment_metadata"))
        role = yaml.safe_load(
            (ROOT / "ansible/roles/autoupdater/tasks/main.yml").read_text()
        )
        deploy = next(
            task for task in role if task.get("ansible.builtin.import_tasks") == "deploy.yml"
        )
        self.assertEqual(deploy["when"], "not ansible_check_mode")

    def test_stack_deploy_never_resolves_and_prunes(self) -> None:
        tasks = yaml.safe_load((ROOT / DEPLOY).read_text())
        deploys = [
            task["community.docker.docker_stack"]
            for task in tasks
            if "community.docker.docker_stack" in task
        ]
        self.assertEqual(len(deploys), 1)
        self.assertEqual(deploys[0]["resolve_image"], "never")
        self.assertIs(deploys[0]["prune"], True)
        self.assertEqual(deploys[0]["compose"], ["/opt/dockerswarm/autoupdater/stack.yml"])

    def test_playbook_is_allowed_everywhere_a_playbook_is_enumerated(self) -> None:
        lock = load_script("ansible_operation_lock", "scripts/ansible-operation-lock.py")
        self.assertIn("autoupdater", lock.SAFE_PLAYBOOKS)
        lock.require_metadata(
            "a" * 64, "b" * 40, "c" * 64,
            "autoupdater", "production", "check", "review-controller",
        )
        wrapper = (ROOT / "scripts/deploy-ansible.sh").read_text()
        self.assertIn("|autoupdater|", wrapper)
        self.assertIn('"${PROJECT_DIR}/config/autoupdater.yml"', wrapper)
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/deployment_metadata/defaults/main.yml").read_text()
        )["deployment_metadata_components_by_playbook"]
        self.assertEqual(defaults["autoupdater"], ["autoupdater"])
        self.assertEqual(defaults["site"][-1], "autoupdater")
        metadata = (ROOT / "scripts/validate-deployment-metadata.py").read_text()
        self.assertIn('"autoupdater": ["autoupdater"]', metadata)
        self.assertIn('PROJECT_DIR / "config/autoupdater.yml"', metadata)
        assertion = (
            ROOT / "ansible/roles/deployment_metadata/tasks/main.yml"
        ).read_text()
        self.assertIn('"autoupdater",', assertion)
        gate = (ROOT / "scripts/validate-iac.sh").read_text()
        self.assertIn("scripts/validate-autoupdater.py", gate)
        self.assertLess(
            gate.index("scripts/validate-autoupdater.py"),
            gate.index("scripts/validate-capacity.sh --reuse-rendered"),
        )
        profiles = load_script(
            "validate_capacity_profiles", "scripts/validate-capacity-profiles.py"
        )
        with self.assertRaises(SystemExit):
            profiles.main(["--live", "--requested-stack", "autoupdater-extra"])

    def test_wrapper_and_metadata_validator_hash_the_same_contracts(self) -> None:
        wrapper = (ROOT / "scripts/deploy-ansible.sh").read_text()
        metadata = (ROOT / "scripts/validate-deployment-metadata.py").read_text()
        block = wrapper.split("for contract_path in", 1)[1].split("; do", 1)[0]
        wrapper_paths = [
            line.strip().strip('\\').strip().strip('"').removeprefix("${PROJECT_DIR}/")
            for line in block.splitlines()
            if "PROJECT_DIR" in line
        ]
        listing = metadata.split("contract_paths = [", 1)[1].split("]", 1)[0]
        metadata_paths = [
            line.strip().rstrip(",").split('"')[1]
            for line in listing.splitlines()
            if "PROJECT_DIR" in line
        ]
        self.assertEqual(wrapper_paths, metadata_paths)


class AutoupdaterLiveGateTests(AnsibleTaskAssertions, unittest.TestCase):
    """The post-deploy spec gate, fed synthetic `docker service inspect`."""

    SECRET_ID = "s" * 25

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = yaml.safe_load((ROOT / "config/autoupdater.yml").read_text())[
            "autoupdater"
        ]

    def variables(self, **changes: Any) -> dict[str, Any]:
        container = {
            "Image": self.catalog["image"],
            "Env": [
                "WITH_REGISTRY_AUTH=true",
                "FILTER_SERVICES=label=apptolast.autoupdate=true",
                "SLEEP_TIME=1h",
                "TIMEOUT=900",
                "VERBOSE=true",
                "TZ=UTC",
                "REGISTRY_USER=ocholoko888",
            ],
            "Secrets": [
                {
                    "File": {
                        "Name": "shepherd_registry_password",
                        "UID": "0",
                        "GID": "0",
                        "Mode": 256,
                    },
                    "SecretID": self.SECRET_ID,
                    "SecretName": "autoupdater-dockerhub-pat-v1",
                }
            ],
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": "/var/run/docker.sock",
                    "Target": "/var/run/docker.sock",
                    "ReadOnly": True,
                }
            ],
        }
        replicas = changes.pop("replicas", 1)
        container_changes = changes.pop("container", {})
        container.update(container_changes)
        service = {
            "Spec": {
                "Labels": {
                    "com.docker.stack.namespace": "autoupdater",
                    "apptolast.autoupdate": "false",
                },
                "Mode": {"Replicated": {"Replicas": replicas}},
                "TaskTemplate": {
                    "ContainerSpec": container,
                    "Placement": {"Constraints": ["node.role == manager"]},
                },
                "EndpointSpec": {"Mode": "vip"},
            }
        }
        variables = {
            "autoupdater": dict(self.catalog, **changes.pop("catalog", {})),
            "autoupdater_expected_replicas": changes.pop("expected_replicas", 1),
            "autoupdater_expected_environment": [
                "FILTER_SERVICES=label=apptolast.autoupdate=true",
                "REGISTRY_USER=ocholoko888",
                "SLEEP_TIME=1h",
                "TIMEOUT=900",
                "TZ=UTC",
                "VERBOSE=true",
                "WITH_REGISTRY_AUTH=true",
            ],
            "autoupdater_service_inspection": {"stdout": json.dumps([service])},
            "autoupdater_secret_inspection": {
                "stdout": json.dumps([{"ID": self.SECRET_ID}])
            },
        }
        assert not changes, changes
        return variables

    def test_reviewed_spec_is_accepted(self) -> None:
        self.assert_task_accepts(DEPLOY, LIVE_GATE, self.variables())

    def test_unreviewed_live_spec_is_rejected(self) -> None:
        base_env = self.variables()["autoupdater_expected_environment"]
        cases = {
            "live ignorelist without filter": {
                "container": {
                    "Env": [
                        item for item in base_env if not item.startswith("FILTER")
                    ]
                    + ["IGNORELIST_SERVICES=a b", "SLEEP_TIME=20m"]
                }
            },
            "read-write socket": {
                "container": {
                    "Mounts": [
                        {
                            "Type": "bind",
                            "Source": "/var/run/docker.sock",
                            "Target": "/var/run/docker.sock",
                        }
                    ]
                }
            },
            "unpinned image": {
                "container": {
                    "Image": "containrrr/shepherd:latest@"
                    + self.catalog["image"].split("@")[1]
                }
            },
            "replicas differ from the kill switch": {"replicas": 0},
            "secret replaced": {"container": {"Secrets": []}},
        }
        for case, changes in cases.items():
            with self.subTest(case=case):
                self.assert_task_rejects(
                    DEPLOY, LIVE_GATE, self.variables(**changes), "live watcher differs"
                )


if __name__ == "__main__":
    unittest.main()
