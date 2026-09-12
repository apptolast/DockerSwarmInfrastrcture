"""Contract tests for the registered image watcher stack (Shepherd)."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from ansible_task_harness import AnsibleTaskAssertions, run_task_definition

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = "ansible/roles/autoupdater/tasks/deploy.yml"
POLL = "Wait for the watcher update to reach a terminal state"


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
            "registry password secret": lambda s: self.service(s).update(
                secrets=[]
            ),
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

    def test_service_keys_and_deploy_mapping_are_exact(self) -> None:
        def service(**changes: Any):
            return lambda stack: self.service(stack).update(changes)

        def deploy(**changes: Any):
            return lambda stack: self.service(stack)["deploy"].update(changes)

        cases = {
            "must not declare entrypoint": service(
                entrypoint=["sh", "-c", "cat /run/secrets/*"]
            ),
            "must not declare command": service(command=["--help"]),
            "must not declare user": service(user="1000"),
            "must not declare extra_hosts": service(
                extra_hosts=["registry-1.docker.io:203.0.113.1"]
            ),
            "must not declare dns": service(dns=["203.0.113.53"]),
            "must not declare cap_add": service(cap_add=["SYS_ADMIN"]),
            "lacks reviewed keys: logging": lambda s: self.service(s).pop(
                "logging"
            ),
            "init: true": service(init=False),
            "reviewed local driver": service(logging={"driver": "json-file"}),
            "update_config differs": lambda s: self.service(s)["deploy"][
                "update_config"
            ].update(failure_action="continue"),
            "rollback_config differs": lambda s: self.service(s)["deploy"][
                "rollback_config"
            ].update(failure_action="continue"),
            "restart_policy differs": deploy(restart_policy={"condition": "none"}),
            "reviewed exact mapping": deploy(endpoint_mode="dnsrr"),
            "top-level keys differ": lambda s: s.update(configs={}),
        }
        for message, mutate in cases.items():
            with self.subTest(message=message):
                self.assert_render_rejected(mutate, message)
        stack = copy.deepcopy(self.stack)
        stack["version"] = "3.9"
        with self.assertRaisesRegex(autoupdater.AutoupdaterError, "top-level"):
            autoupdater.validate_render(stack, self.catalog)

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
        # The exact argv capacity_preflight passes for `--playbook autoupdater`.
        live = json.dumps([{"name": "autoupdater_shepherd", "stack": "autoupdater"}])
        with mock.patch("sys.stdin", io.StringIO(live)), mock.patch(
            "sys.stdout", io.StringIO()
        ):
            self.assertEqual(
                profiles.main(["--live", "--requested-stack", "autoupdater"]), 0
            )

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


REVIEWED_ENV = [
    "FILTER_SERVICES=label=apptolast.autoupdate=true",
    "REGISTRY_USER=ocholoko888",
    "SLEEP_TIME=1h",
    "TIMEOUT=900",
    "TZ=UTC",
    "VERBOSE=true",
    "WITH_REGISTRY_AUTH=true",
]
# The hand-deployed watcher found on the host: no filter, no VERBOSE/TZ, an
# ignore list and a 20m cycle.
HAND_DEPLOYED_ENV = [
    item
    for item in REVIEWED_ENV
    if item.split("=", 1)[0] not in {"FILTER_SERVICES", "VERBOSE", "TZ", "SLEEP_TIME"}
] + ["IGNORELIST_SERVICES=autoupdater_shepherd", "SLEEP_TIME=20m"]

GATES = {
    "image": (
        "Require the reviewed live watcher image and service labels",
        "Live watcher image or service labels differ",
    ),
    "environment": (
        "Require the exact reviewed live watcher environment",
        "Live watcher environment differs from the reviewed exact set",
    ),
    "filter": (
        "Require the label filter in the live watcher environment",
        "Live watcher runs without the apptolast.autoupdate label filter",
    ),
    "wideners": (
        "Reject scope wideners in the live watcher environment",
        "Live watcher environment carries a scope widener",
    ),
    "replicas": (
        "Require the reviewed live watcher replicas placement and ports",
        "Live watcher replicas, placement or published ports differ",
    ),
    "secret": (
        "Require the reviewed live watcher registry secret",
        "Live watcher registry secret differs",
    ),
    "mount": (
        "Require only the read-only Docker socket mount on the live watcher",
        "Live watcher mounts differ",
    ),
    "container": (
        "Require only the reviewed live watcher container fields",
        "Live watcher container fields differ",
    ),
    "resources": (
        "Require the reviewed live watcher resources",
        "Live watcher resources differ",
    ),
}


class AutoupdaterLiveGateTests(AnsibleTaskAssertions, unittest.TestCase):
    """The post-deploy spec gates, fed synthetic `docker service inspect`."""

    SECRET_ID = "s" * 25

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = yaml.safe_load((ROOT / "config/autoupdater.yml").read_text())[
            "autoupdater"
        ]

    def variables(self, **changes: Any) -> dict[str, Any]:
        # What Swarm stores for the rendered stack: the CLI adds the stack
        # namespace label and an empty Privileges, the daemon Isolation.
        container = {
            "Image": self.catalog["image"],
            "Labels": {"com.docker.stack.namespace": "autoupdater"},
            "Env": list(REVIEWED_ENV),
            "Init": True,
            "Privileges": {"CredentialSpec": None, "SELinuxContext": None},
            "Isolation": "default",
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
        container.update(changes.pop("container", {}))
        resources = {
            "Limits": {"NanoCPUs": 250000000, "MemoryBytes": 47185920},
            "Reservations": {"NanoCPUs": 100000000, "MemoryBytes": 18874368},
        }
        resources.update(changes.pop("resources", {}))
        service = {
            "Spec": {
                "Labels": {
                    "com.docker.stack.namespace": "autoupdater",
                    "apptolast.autoupdate": "false",
                },
                "Mode": {"Replicated": {"Replicas": replicas}},
                "TaskTemplate": {
                    "ContainerSpec": container,
                    "Resources": resources,
                    "Placement": {"Constraints": ["node.role == manager"]},
                },
                "EndpointSpec": {"Mode": "vip"},
            }
        }
        variables = {
            "autoupdater": self.catalog,
            "autoupdater_expected_replicas": changes.pop("expected_replicas", 1),
            "autoupdater_expected_environment": list(REVIEWED_ENV),
            "autoupdater_service": service,
            "autoupdater_container": container,
            "autoupdater_secret_inspection": {
                "stdout": json.dumps([{"ID": self.SECRET_ID}])
            },
        }
        assert not changes, changes
        return variables

    def assert_gate_rejects(self, gate: str, **changes: Any) -> None:
        name, message = GATES[gate]
        self.assert_task_rejects(DEPLOY, name, self.variables(**changes), message)

    def test_parse_task_feeds_the_gates(self) -> None:
        tasks = yaml.safe_load((ROOT / DEPLOY).read_text())
        names = [task.get("name") for task in tasks]
        parse = names.index("Parse the converged watcher service")
        self.assertLess(names.index("Inspect the converged watcher service"), parse)
        for name, _message in GATES.values():
            self.assertLess(parse, names.index(name))
        self.assertLess(
            names.index("Reject a watcher update that did not complete"), parse
        )

    def test_reviewed_spec_passes_every_gate(self) -> None:
        for gate, (name, _message) in GATES.items():
            with self.subTest(gate=gate):
                self.assert_task_accepts(DEPLOY, name, self.variables())

    def test_kill_switch_with_zero_replicas_is_accepted(self) -> None:
        name, _message = GATES["replicas"]
        self.assert_task_accepts(
            DEPLOY, name, self.variables(replicas=0, expected_replicas=0)
        )
        self.assert_gate_rejects("replicas", replicas=1, expected_replicas=0)

    def test_hand_deployed_environment_fails_every_environment_rule(self) -> None:
        # Each rule is proved on its own: removing any one assertion breaks
        # exactly the matching subtest.
        for gate in ("environment", "filter", "wideners"):
            with self.subTest(gate=gate):
                self.assert_gate_rejects(gate, container={"Env": HAND_DEPLOYED_ENV})
        widened = REVIEWED_ENV + ["IGNORELIST_SERVICES=x"]
        self.assert_gate_rejects("wideners", container={"Env": widened})
        self.assert_task_accepts(
            DEPLOY, GATES["filter"][0], self.variables(container={"Env": widened})
        )

    def test_unreviewed_live_spec_is_rejected_by_its_rule(self) -> None:
        digest = self.catalog["image"].split("@")[1]
        cases = [
            ("image", {"container": {"Image": "containrrr/shepherd:latest@" + digest}}),
            ("replicas", {"replicas": 0}),
            ("secret", {"container": {"Secrets": []}}),
            (
                "mount",
                {
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
            ),
            ("container", {"container": {"Command": ["sh", "-c", "id"]}}),
            ("container", {"container": {"Args": ["--once"]}}),
            ("container", {"container": {"User": "0"}}),
            ("container", {"container": {"Hosts": ["203.0.113.1 index.docker.io"]}}),
            ("container", {"container": {"CapabilityAdd": ["CAP_SYS_ADMIN"]}}),
            ("container", {"container": {"Init": False}}),
            (
                "container",
                {
                    "container": {
                        "Privileges": {
                            "CredentialSpec": None,
                            "SELinuxContext": {"Disable": True},
                        }
                    }
                },
            ),
            ("container", {"container": {"DNSConfig": {"Nameservers": ["1.1.1.1"]}}}),
            (
                "resources",
                {
                    "resources": {
                        "Limits": {"NanoCPUs": 250000000, "MemoryBytes": 134217728}
                    }
                },
            ),
            (
                "resources",
                {"resources": {"Reservations": {"NanoCPUs": 100000000}}},
            ),
            (
                "resources",
                {
                    "resources": {
                        "Limits": {
                            "NanoCPUs": 250000000,
                            "MemoryBytes": 47185920,
                            "Pids": 1,
                        }
                    }
                },
            ),
        ]
        for gate, changes in cases:
            with self.subTest(gate=gate, changes=changes):
                self.assert_gate_rejects(gate, **changes)

    def test_container_gate_names_the_unexpected_key(self) -> None:
        name, _message = GATES["container"]
        self.assert_task_rejects(
            DEPLOY,
            name,
            self.variables(container={"Command": ["sh"]}),
            "Command",
        )

    def test_empty_tolerated_defaults_are_accepted(self) -> None:
        name, _message = GATES["container"]
        self.assert_task_accepts(
            DEPLOY,
            name,
            self.variables(container={"DNSConfig": {}}),
        )


class AutoupdaterUpdateGateTests(AnsibleTaskAssertions, unittest.TestCase):
    """The post-deploy update gate, fed pre-deploy and polled inspections."""

    GATE = "Reject a watcher update that did not complete"
    MESSAGE = "The watcher did not keep the reviewed spec"
    HAND = {"ContainerSpec": {"Image": "containrrr/shepherd:latest"}}
    REVIEWED = {"ContainerSpec": {"Image": "containrrr/shepherd:v1.8.1"}}

    @staticmethod
    def inspection(
        template: dict[str, Any], status: dict[str, Any] | None
    ) -> dict[str, Any]:
        service: dict[str, Any] = {
            "Spec": {"TaskTemplate": template, "EndpointSpec": {"Mode": "vip"}}
        }
        if status is not None:
            service["UpdateStatus"] = status
        return service

    def variables(
        self,
        before: dict[str, Any],
        after: dict[str, Any] | None,
        replicas: int = 1,
    ) -> dict[str, Any]:
        poll = (
            {"rc": 0, "stdout": json.dumps([after])}
            if after is not None
            else {"rc": 1, "stdout": "[]", "stderr": "no such service"}
        )
        return {
            "autoupdater_update_before": before,
            "autoupdater_update_poll": poll,
            "autoupdater_expected_replicas": replicas,
        }

    def test_accepts_a_completed_or_absent_update(self) -> None:
        old = {"State": "completed", "StartedAt": "2026-01-01T00:00:00Z"}
        new = {"State": "completed", "StartedAt": "2026-09-13T00:00:00Z"}
        cases = {
            "completed after a changed spec": (
                self.inspection(self.HAND, old),
                self.inspection(self.REVIEWED, new),
                1,
            ),
            "unchanged re-apply resets the status": (
                self.inspection(self.REVIEWED, old),
                self.inspection(self.REVIEWED, None),
                1,
            ),
            "unchanged re-apply keeping a completed status": (
                self.inspection(self.REVIEWED, old),
                self.inspection(self.REVIEWED, old),
                1,
            ),
            "first registration": ({}, self.inspection(self.REVIEWED, None), 1),
            "placement-only change starts no update": (
                self.inspection(
                    dict(self.REVIEWED, Placement={"Constraints": []}), old
                ),
                self.inspection(self.REVIEWED, None),
                1,
            ),
            "kill switch to zero replicas": (
                self.inspection(self.HAND, old),
                self.inspection(self.REVIEWED, None),
                0,
            ),
        }
        for case, (before, after, replicas) in cases.items():
            with self.subTest(case=case):
                self.assert_task_accepts(
                    DEPLOY, self.GATE, self.variables(before, after, replicas)
                )

    def test_rejects_a_rolled_back_paused_stale_or_unfinished_update(self) -> None:
        old = {"State": "completed", "StartedAt": "2026-01-01T00:00:00Z"}
        started = "2026-09-13T00:00:00Z"
        before = self.inspection(self.HAND, old)
        cases = {
            # A rollback restores PreviousSpec: the spec equals the old one.
            "rollback_completed": self.inspection(
                self.HAND, {"State": "rollback_completed", "StartedAt": started}
            ),
            "rollback_paused": self.inspection(
                self.HAND, {"State": "rollback_paused", "StartedAt": started}
            ),
            "rollback_started": self.inspection(
                self.HAND, {"State": "rollback_started", "StartedAt": started}
            ),
            "paused": self.inspection(
                self.REVIEWED, {"State": "paused", "StartedAt": started}
            ),
            "updating": self.inspection(
                self.REVIEWED, {"State": "updating", "StartedAt": started}
            ),
            "stale completed status": self.inspection(self.REVIEWED, old),
            "changed spec not started yet": self.inspection(self.REVIEWED, None),
        }
        for case, after in cases.items():
            with self.subTest(case=case):
                self.assert_task_rejects(
                    DEPLOY, self.GATE, self.variables(before, after), self.MESSAGE
                )
        # A stale rollback is rejected even without a new change.
        rolled = self.inspection(
            self.REVIEWED, {"State": "rollback_completed", "StartedAt": started}
        )
        self.assert_task_rejects(
            DEPLOY, self.GATE, self.variables(rolled, rolled), self.MESSAGE
        )
        self.assert_task_rejects(
            DEPLOY, self.GATE, self.variables(before, None), self.MESSAGE
        )

    def test_pre_deploy_read_accepts_only_a_readable_or_absent_service(self) -> None:
        name = "Require the watcher service to be readable or absent"
        self.assert_task_accepts(
            DEPLOY,
            name,
            {
                "autoupdater_service_before": {
                    "rc": 1,
                    "stderr": "Error: no such service: autoupdater_shepherd",
                }
            },
        )
        self.assert_task_rejects(
            DEPLOY,
            name,
            {"autoupdater_service_before": {"rc": 1, "stderr": "permission denied"}},
            "could not be read",
        )

    def poll_settles(self, variables: dict[str, Any]) -> bool:
        """Evaluate the poll's exact `until` against one synthetic read."""
        tasks = yaml.safe_load((ROOT / DEPLOY).read_text())
        poll = next(
            task
            for task in tasks
            if task.get("name") == POLL
        )
        probe = {
            "name": "Evaluate the poll condition",
            "ansible.builtin.assert": {"that": [poll["until"]], "quiet": True},
            "vars": poll["vars"],
        }
        completed = run_task_definition(probe, variables)
        output = completed.stdout + completed.stderr
        self.assertTrue(
            completed.returncode == 0 or "evaluated_to" in output, output
        )
        return completed.returncode == 0

    def test_poll_stops_only_on_a_terminal_state(self) -> None:
        old = {"State": "completed", "StartedAt": "2026-01-01T00:00:00Z"}
        started = "2026-09-13T00:00:00Z"
        before = self.inspection(self.HAND, old)
        settled = {
            "completed": self.inspection(
                self.REVIEWED, {"State": "completed", "StartedAt": started}
            ),
            "rollback_completed": self.inspection(
                self.HAND, {"State": "rollback_completed", "StartedAt": started}
            ),
            "rollback_paused": self.inspection(
                self.HAND, {"State": "rollback_paused", "StartedAt": started}
            ),
            "paused": self.inspection(
                self.REVIEWED, {"State": "paused", "StartedAt": started}
            ),
        }
        pending = {
            "not started yet": self.inspection(self.REVIEWED, None),
            "stale completed": self.inspection(self.REVIEWED, old),
            "updating": self.inspection(
                self.REVIEWED, {"State": "updating", "StartedAt": started}
            ),
            "rollback_started": self.inspection(
                self.HAND, {"State": "rollback_started", "StartedAt": started}
            ),
            "unreadable": None,
        }
        for case, after in settled.items():
            with self.subTest(case=case):
                self.assertTrue(self.poll_settles(self.variables(before, after)))
        for case, after in pending.items():
            with self.subTest(case=case):
                self.assertFalse(self.poll_settles(self.variables(before, after)))
        unchanged = self.inspection(self.REVIEWED, old)
        self.assertTrue(
            self.poll_settles(
                self.variables(unchanged, self.inspection(self.REVIEWED, None))
            )
        )

    def test_update_is_polled_before_any_convergence_read(self) -> None:
        tasks = yaml.safe_load((ROOT / DEPLOY).read_text())
        names = [task.get("name") for task in tasks]
        deploy = next(
            index
            for index, task in enumerate(tasks)
            if "community.docker.docker_stack" in task
        )
        poll = names.index(POLL)
        record = names.index("Record the watcher service before the deploy")
        self.assertLess(record, deploy)
        self.assertEqual(poll, deploy + 1)
        self.assertEqual(names.index(self.GATE), poll + 1)
        task = tasks[poll]
        # Above monitor 30s + restart delay 30s + start, plus a full rollback.
        self.assertGreaterEqual(task["retries"] * task["delay"], 180)
        self.assertIs(task["check_mode"], False)
        self.assertEqual(tasks[poll]["vars"], tasks[poll + 1]["vars"])
        for state in ("rollback_completed", "rollback_paused", "paused"):
            self.assertIn(state, task["until"])


if __name__ == "__main__":
    unittest.main()
