"""Contract tests for CrowdSec bans of repeated Traefik basicAuth 401s.

They cover the pinned Traefik parser, the file acquisition of Traefik's
access log (never the Docker API), the local leaky scenario, its short-ban
profile, the exact Hub inventory in apply and check mode, and the host-only
allowlist source, its preflight and strict gates and its convergence.
"""

from __future__ import annotations

import copy
import importlib.util
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

import yaml
from ansible_task_harness import load_task, run_task_definitions
from jinja2 import Environment, StrictUndefined

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PROJECT_ROOT / "config/host-security.yml"
ROLE = PROJECT_ROOT / "ansible/roles/host_security"
TASKS = "ansible/roles/host_security/tasks/main.yml"
GATES = "ansible/roles/host_security/tasks/crowdsec_allowlist_gates.yml"
TEMPLATES = ROLE / "templates"
SOURCE_SCRIPT = PROJECT_ROOT / "scripts/validate-crowdsec-allowlist.py"
VALIDATOR = PROJECT_ROOT / "scripts/validate-host-security.py"

TRAEFIK_LOGS = {
    "type": "parsers",
    "name": "crowdsecurity/traefik-logs",
    "version": "1.5",
    "sha256": "706321c134508c2bc2f8f79b1403ef4c4d3716be5e0b8436ff5a3bd2f41b505e",
}
# Keys the file datasource of CrowdSec v1.7.8 accepts: its own
# (pkg/acquisition/modules/file/config.go, Configuration, strict YAML) plus
# the common datasource keys (pkg/acquisition/configuration).
FILE_SOURCE_KEYS = {
    "filenames",
    "exclude_regexps",
    "filename",
    "force_inotify",
    "max_buffer_size",
    "poll_without_inotify",
    "discovery_poll_enable",
    "discovery_poll_interval",
    "mode",
    "labels",
    "log_level",
    "source",
    "name",
    "use_time_machine",
    "unique_id",
    "transform",
}
# ProfileCfg of CrowdSec v1.7.8 (pkg/csconfig/profiles.go), decoded with
# KnownFields(true).
PROFILE_KEYS = {
    "name",
    "debug",
    "filters",
    "decisions",
    "duration_expr",
    "on_success",
    "on_failure",
    "on_error",
    "notifications",
}
PERMANENT = "0001-01-01T00:00:00.000Z"


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


allowlist_source = load_module("crowdsec_allowlist_source", SOURCE_SCRIPT)
host_security_validator = load_module("validate_host_security", VALIDATOR)


def contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def defaults() -> dict[str, Any]:
    return yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))


def render(template: str, variables: dict[str, Any] | None = None) -> str:
    source = (TEMPLATES / template).read_text(encoding="utf-8")
    return (
        Environment(
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )
        .from_string(source)
        .render(**(variables or contract()))
    )


def tasks_by_name(path: str = TASKS) -> dict[str, dict[str, Any]]:
    tasks = yaml.safe_load((PROJECT_ROOT / path).read_text(encoding="utf-8"))
    by_name = {task["name"]: task for task in tasks}
    if len(by_name) != len(tasks):
        raise AssertionError(f"{path} repeats a task name")
    return by_name


def task_names(path: str = TASKS) -> list[str]:
    return list(tasks_by_name(path))


class TraefikBasicAuthContractTests(unittest.TestCase):
    """The reviewed contract, its validator and the rendered CrowdSec files."""

    def test_traefik_parser_is_pinned_outside_any_collection(self) -> None:
        document = contract()
        self.assertEqual(
            document["host_security_crowdsec_standalone_hub_lock"], [TRAEFIK_LOGS]
        )
        names = [
            item["name"]
            for key in (
                "host_security_crowdsec_collection_lock",
                "host_security_crowdsec_hub_dependency_lock",
                "host_security_crowdsec_standalone_hub_lock",
            )
            for item in document[key]
        ]
        self.assertEqual(len(names), len(set(names)))
        # The collection would add base-http-scenarios and http-cve to every
        # site; only the parser is reviewed.
        self.assertNotIn("crowdsecurity/traefik", names)
        self.assertNotIn("crowdsecurity/base-http-scenarios", names)

    def test_contract_validator_accepts_the_reviewed_policy(self) -> None:
        host_security_validator.validate_traefik_basicauth(contract(), set())

    def test_contract_validator_rejects_unsafe_policies(self) -> None:
        cases: dict[str, tuple[str, Any]] = {
            "four hour ban": ("host_security_crowdsec_basicauth_ban_duration", "4h"),
            "ban too long": ("host_security_crowdsec_basicauth_ban_duration", "60m"),
            "ban too short": ("host_security_crowdsec_basicauth_ban_duration", "10m"),
            "tiny capacity": ("host_security_crowdsec_basicauth_capacity", 3),
            "huge capacity": ("host_security_crowdsec_basicauth_capacity", 50),
            "boolean capacity": ("host_security_crowdsec_basicauth_capacity", True),
            "bad leak": ("host_security_crowdsec_basicauth_leakspeed", "1 minute"),
            "no routers": ("host_security_crowdsec_basicauth_routers", []),
            "docker router": (
                "host_security_crowdsec_basicauth_routers",
                ["ax@docker"],
            ),
            "duplicate router": (
                "host_security_crowdsec_basicauth_routers",
                ["ax@file", "ax@file"],
            ),
            "glob access log": (
                "host_security_crowdsec_traefik_access_log",
                "/var/log/dockerswarm/edge/*.log",
            ),
            "rotated access log": (
                "host_security_crowdsec_traefik_access_log",
                "/var/log/dockerswarm/edge/access.log.1",
            ),
            "access log outside the edge tree": (
                "host_security_crowdsec_traefik_access_log",
                "/var/lib/docker/containers/x/x-json.log",
            ),
            "relative access log": (
                "host_security_crowdsec_traefik_access_log",
                "var/log/dockerswarm/edge/access.log",
            ),
            "no access log": ("host_security_crowdsec_traefik_access_log", None),
            "Docker source back": (
                "host_security_crowdsec_traefik_container_name_regexp",
                "^/?edge_traefik\\.[0-9]+\\.[a-z0-9]+$",
            ),
            "scenario without author": (
                "host_security_crowdsec_basicauth_scenario",
                "traefik-basicauth-bf",
            ),
            "source outside the host-only directory": (
                "host_security_crowdsec_allowlist_source",
                "/home/admin/trusted-ips",
            ),
            "allowlist name with spaces": (
                "host_security_crowdsec_allowlist_name",
                "apptolast trusted",
            ),
        }
        for label, (key, value) in cases.items():
            with self.subTest(label):
                document = contract()
                document[key] = value
                with self.assertRaises(
                    host_security_validator.HostSecurityContractError
                ):
                    host_security_validator.validate_traefik_basicauth(document, set())
        with self.assertRaises(host_security_validator.HostSecurityContractError):
            host_security_validator.validate_traefik_basicauth(
                contract(), {"apptolast/traefik-basicauth-bf"}
            )

    def test_full_validator_rejects_a_standalone_item_without_a_version(
        self,
    ) -> None:
        now = datetime.strptime(
            contract()["host_security_ubuntu_snapshot"], "%Y%m%dT%H%M%SZ"
        ).replace(tzinfo=timezone.utc)
        host_security_validator.validate(contract(), now)
        for label, change in {
            "collection type": {"type": "collections"},
            "no version": {"version": None},
            "short digest": {"sha256": "706321c1"},
        }.items():
            with self.subTest(label):
                document = contract()
                item = copy.deepcopy(TRAEFIK_LOGS)
                item.update(change)
                document["host_security_crowdsec_standalone_hub_lock"] = [item]
                with self.assertRaises(
                    host_security_validator.HostSecurityContractError
                ):
                    host_security_validator.validate(document, now)
        duplicated = contract()
        duplicated["host_security_crowdsec_standalone_hub_lock"].append(
            copy.deepcopy(duplicated["host_security_crowdsec_hub_dependency_lock"][0])
        )
        with self.assertRaises(host_security_validator.HostSecurityContractError):
            host_security_validator.validate(duplicated, now)

    def test_role_input_contract_bounds_the_policy(self) -> None:
        task = copy.deepcopy(
            load_task(TASKS, "Verify the host-security input contract")
        )
        task["vars"] = {
            "ansible_facts": {"distribution": "Ubuntu", "distribution_version": "26.04"}
        }
        document = contract()
        cases: dict[str, tuple[dict[str, Any], bool]] = {
            "reviewed": ({}, True),
            "four hour ban": (
                {"host_security_crowdsec_basicauth_ban_duration": "4h"},
                False,
            ),
            "capacity as text": (
                {"host_security_crowdsec_basicauth_capacity": "10"},
                False,
            ),
            "docker provider router": (
                {"host_security_crowdsec_basicauth_routers": ["ax@docker"]},
                False,
            ),
            "glob access log": (
                {
                    "host_security_crowdsec_traefik_access_log": (
                        "/var/log/dockerswarm/edge/*.log"
                    )
                },
                False,
            ),
            "access log in Docker's own tree": (
                {
                    "host_security_crowdsec_traefik_access_log": (
                        "/var/lib/docker/containers/x/x-json.log"
                    )
                },
                False,
            ),
            "name locked twice": (
                {
                    "host_security_crowdsec_standalone_hub_lock": [
                        *document["host_security_crowdsec_standalone_hub_lock"],
                        document["host_security_crowdsec_hub_dependency_lock"][0],
                    ]
                },
                False,
            ),
            "scenario shadows a Hub item": (
                {"host_security_crowdsec_basicauth_scenario": "crowdsecurity/ssh-bf"},
                False,
            ),
            "source outside the host-only directory": (
                {"host_security_crowdsec_allowlist_source": "/tmp/trusted-ips"},
                False,
            ),
        }
        for label, (change, accepted) in cases.items():
            with self.subTest(label):
                completed = run_task_definitions(
                    [task], {**document, **defaults(), **change}
                )
                output = completed.stdout + completed.stderr
                self.assertEqual(completed.returncode == 0, accepted, output)
                if not accepted:
                    self.assertIn("Traefik", output)

    def test_no_address_is_published_in_the_reviewed_files(self) -> None:
        candidate = re.compile(
            r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\w.])"
            r"|(?<![\w:])(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{0,4}(?![\w:])",
            re.IGNORECASE,
        )
        for path in (
            CONTRACT_PATH,
            TEMPLATES / "02-dockerswarm-traefik.yaml.j2",
            TEMPLATES / "dockerswarm-traefik-basicauth-bf.yaml.j2",
            TEMPLATES / "profiles.yaml.local.j2",
        ):
            with self.subTest(path.name):
                found = []
                for match in candidate.findall(path.read_text(encoding="utf-8")):
                    try:
                        ipaddress.ip_network(match, strict=False)
                    except ValueError:
                        continue
                    found.append(match)
                self.assertEqual(found, [])

    def test_traefik_acquisition_tails_one_plain_host_file(self) -> None:
        document = contract()
        rendered = yaml.safe_load(render("02-dockerswarm-traefik.yaml.j2"))
        self.assertLessEqual(set(rendered), FILE_SOURCE_KEYS)
        self.assertEqual(rendered["source"], "file")
        self.assertEqual(rendered["labels"], {"type": "traefik"})
        # Exactly the access log: no glob, so neither a rotated sibling nor
        # another container's log is ever read.
        self.assertEqual(
            rendered["filenames"],
            [document["host_security_crowdsec_traefik_access_log"]],
        )
        self.assertEqual(
            document["host_security_crowdsec_traefik_access_log"],
            "/var/log/dockerswarm/edge/access.log",
        )
        self.assertNotRegex(rendered["filenames"][0], r"[*?\[]")
        # CrowdSec 1.7.8 only watches for a file created after it starts when
        # force_inotify puts a watch on the (existing) directory.
        self.assertIs(rendered["force_inotify"], True)
        # Documented v1.7 keys only; discovery polling is not in the v1.7
        # docs, and inotify polling would be needless CPU.
        for key in (
            "discovery_poll_enable",
            "discovery_poll_interval",
            "poll_without_inotify",
            "exclude_regexps",
            "transform",
            "mode",
        ):
            with self.subTest(key):
                self.assertNotIn(key, rendered)

    def test_no_crowdsec_source_depends_on_docker(self) -> None:
        # A CrowdSec 1.7.8 Docker source that loses the daemon for about 15
        # minutes stops every acquisition, auth.log included
        # (pkg/acquisition/modules/docker/run.go, acquisition.go
        # StartAcquisition). Every managed source is a file.
        for name in defaults()["host_security_managed_acquisition_files"]:
            with self.subTest(name):
                rendered = yaml.safe_load(render(f"{name}.j2"))
                self.assertEqual(rendered["source"], "file")
                self.assertLessEqual(set(rendered), FILE_SOURCE_KEYS)
        # Nothing in the role reads the Docker API, so a Docker outage
        # cannot stop a playbook that runs host_security.
        for path in sorted(
            [
                *ROLE.glob("tasks/*.yml"),
                *ROLE.glob("templates/*.j2"),
                ROLE / "defaults/main.yml",
                CONTRACT_PATH,
            ]
        ):
            with self.subTest(path.name):
                text = path.read_text(encoding="utf-8")
                for needle in (
                    "docker.sock",
                    "/usr/bin/docker",
                    "source: docker",
                    "container_name",
                    "DOCKER_HOST",
                ):
                    self.assertNotIn(needle, text)

    def test_scenario_counts_401_only_on_the_basicauth_routers(self) -> None:
        document = contract()
        scenario = yaml.safe_load(render("dockerswarm-traefik-basicauth-bf.yaml.j2"))
        self.assertEqual(scenario["type"], "leaky")
        self.assertEqual(
            scenario["name"], document["host_security_crowdsec_basicauth_scenario"]
        )
        self.assertEqual(scenario["groupby"], "evt.Meta.source_ip")
        self.assertEqual(
            scenario["capacity"], document["host_security_crowdsec_basicauth_capacity"]
        )
        self.assertEqual(
            scenario["leakspeed"],
            document["host_security_crowdsec_basicauth_leakspeed"],
        )
        self.assertEqual(
            scenario["blackhole"],
            document["host_security_crowdsec_basicauth_blackhole"],
        )
        self.assertIs(scenario["labels"]["remediation"], True)
        self.assertEqual(scenario["labels"]["service"], "http")
        clauses = [clause.strip() for clause in scenario["filter"].split("&&")]
        self.assertEqual(clauses[0], "evt.Meta.log_type == 'http_access-log'")
        self.assertEqual(clauses[1], "evt.Meta.http_status == '401'")
        routers = re.fullmatch(
            r"evt\.Meta\.traefik_router_name in \[(.*)\]", clauses[2]
        )
        self.assertIsNotNone(routers)
        assert routers is not None
        self.assertEqual(
            [item.strip().strip("'") for item in routers.group(1).split(",")],
            document["host_security_crowdsec_basicauth_routers"],
        )
        self.assertEqual(len(clauses), 3)
        # ax@file and the live satisfactory-logs@file router, nothing else.
        self.assertEqual(
            sorted(document["host_security_crowdsec_basicauth_routers"]),
            ["ax@file", "satisfactory-logs@file"],
        )

    def test_scenario_leaves_a_margin_for_the_owner(self) -> None:
        document = contract()
        capacity = document["host_security_crowdsec_basicauth_capacity"]
        leak_seconds = int(document["host_security_crowdsec_basicauth_leakspeed"][:-1])
        leak_seconds *= (
            60
            if document["host_security_crowdsec_basicauth_leakspeed"][-1] == "m"
            else 1
        )
        # A browser session costs one 401 challenge; ten failures in a burst
        # never overflow, and a bucket drains fully in capacity * leakspeed.
        self.assertGreaterEqual(capacity, 10)
        self.assertLessEqual(capacity * leak_seconds, 20 * 60)
        blackhole = document["host_security_crowdsec_basicauth_blackhole"]
        ban = document["host_security_crowdsec_basicauth_ban_duration"]
        self.assertLess(int(blackhole[:-1]), int(ban[:-1]))

    def test_profile_gives_only_this_scenario_a_short_ban(self) -> None:
        document = contract()
        rendered = render("profiles.yaml.local.j2")
        profiles = list(yaml.safe_load_all(rendered))
        self.assertEqual(len(profiles), 1)
        [profile] = profiles
        self.assertLessEqual(set(profile), PROFILE_KEYS)
        self.assertEqual(profile["on_success"], "break")
        self.assertNotIn("on_failure", profile)
        self.assertEqual(
            profile["decisions"],
            [
                {
                    "type": "ban",
                    "duration": document[
                        "host_security_crowdsec_basicauth_ban_duration"
                    ],
                }
            ],
        )
        self.assertEqual(
            profile["filters"],
            [
                'Alert.Remediation == true && Alert.GetScope() == "Ip" && '
                'Alert.GetScenario() == "'
                + document["host_security_crowdsec_basicauth_scenario"]
                + '"'
            ],
        )

    def test_role_manages_every_new_crowdsec_file(self) -> None:
        role_defaults = defaults()
        self.assertEqual(
            role_defaults["host_security_managed_acquisition_files"],
            [
                "00-dockerswarm-linux.yaml",
                "01-dockerswarm-sshd.yaml",
                "02-dockerswarm-traefik.yaml",
            ],
        )
        for name in role_defaults["host_security_managed_acquisition_files"]:
            with self.subTest(name):
                self.assertTrue((TEMPLATES / f"{name}.j2").is_file())
        self.assertEqual(
            role_defaults["host_security_crowdsec_profiles_override"],
            "/etc/crowdsec/profiles.yaml.local",
        )
        self.assertTrue(
            role_defaults["host_security_crowdsec_basicauth_scenario_path"].startswith(
                "/etc/crowdsec/scenarios/"
            )
        )


class TraefikBasicAuthRoleOrderTests(unittest.TestCase):
    """Static order and safety properties of the host_security tasks."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tasks = tasks_by_name()
        cls.names = list(cls.tasks)
        cls.gates = tasks_by_name(GATES)
        cls.all_tasks = {**cls.tasks, **cls.gates}

    def assert_before(self, first: str, second: str) -> None:
        self.assertLess(self.names.index(first), self.names.index(second))

    def test_standalone_items_pass_the_digest_gate_before_install(self) -> None:
        install = "Install the reviewed standalone CrowdSec Hub items"
        self.assert_before(
            "Reject unreviewed CrowdSec Hub candidates before mutation", install
        )
        self.assert_before(install, "Inspect every available locked CrowdSec Hub item")
        task = self.tasks[install]
        self.assertEqual(
            task["ansible.builtin.command"]["argv"][:4],
            [
                "/usr/bin/cscli",
                "{{ item.item.type }}",
                "install",
                "{{ item.item.name }}",
            ],
        )
        self.assertIn("not ansible_check_mode", task["when"])
        self.assertEqual(task["notify"], "Restart CrowdSec")
        for name in (
            "Inspect locked CrowdSec Hub candidates before installation",
            "Inspect every available locked CrowdSec Hub item",
        ):
            with self.subTest(name):
                self.assertIn(
                    "host_security_crowdsec_standalone_hub_lock",
                    self.tasks[name]["loop"],
                )

    def test_local_files_are_written_before_they_are_inventoried(self) -> None:
        self.assert_before(
            "Install the reviewed local Traefik basicAuth scenario",
            "Read the complete installed CrowdSec Hub inventory",
        )
        self.assert_before(
            "Inspect the local Traefik basicAuth scenario file",
            "Derive the reviewed CrowdSec items only an apply installs",
        )
        self.assert_before(
            "Derive the reviewed CrowdSec items only an apply installs",
            "Reject every unreviewed installed CrowdSec Hub item",
        )
        for name in (
            "Install the reviewed local Traefik basicAuth scenario",
            "Install the reviewed CrowdSec decision profile override",
        ):
            with self.subTest(name):
                task = self.tasks[name]
                self.assertEqual(task["notify"], "Restart CrowdSec")
                self.assertEqual(task["ansible.builtin.template"]["owner"], "root")
                self.assertEqual(task["ansible.builtin.template"]["mode"], "0644")

    def test_configuration_is_tested_before_the_restart_handler(self) -> None:
        validation = "Validate the CrowdSec configuration before a restart loads it"
        for name in (
            "Install exact CrowdSec acquisition sources",
            "Install the reviewed CrowdSec decision profile override",
            "Install the reviewed local Traefik basicAuth scenario",
            "Install the reviewed standalone CrowdSec Hub items",
        ):
            with self.subTest(name):
                self.assert_before(name, validation)
        self.assert_before(
            validation, "Apply service and firewall handlers before validation"
        )
        self.assertEqual(
            self.tasks[validation]["ansible.builtin.command"]["argv"],
            ["/usr/bin/crowdsec", "-c", "/etc/crowdsec/config.yaml", "-t", "-error"],
        )
        self.assertEqual(self.tasks[validation]["when"], "not ansible_check_mode")

    def test_access_log_directories_exist_before_the_source_loads(self) -> None:
        order = [
            "Create the CrowdSec acquisition directory",
            "Inspect the Traefik access log directories without following links",
            "Reject an unsafe Traefik access log directory",
            "Create the Traefik access log directories CrowdSec watches",
            "Install exact CrowdSec acquisition sources",
            "Validate the CrowdSec configuration before a restart loads it",
            "Apply service and firewall handlers before validation",
        ]
        self.assertEqual([name for name in self.names if name in order], order)
        inspect = self.tasks[order[1]]
        self.assertIs(inspect["ansible.builtin.stat"]["follow"], False)
        create = self.tasks[order[3]]
        self.assertEqual(create["loop"], inspect["loop"])
        self.assertEqual(
            {
                key: create["ansible.builtin.file"][key]
                for key in ("state", "owner", "group", "mode")
            },
            {"state": "directory", "owner": "root", "group": "root", "mode": "0755"},
        )
        # The source list is exactly the managed one again: no Docker gate.
        self.assertEqual(
            self.tasks["Install exact CrowdSec acquisition sources"]["loop"],
            "{{ host_security_managed_acquisition_files }}",
        )
        self.assertIn(
            "== host_security_managed_acquisition_files | sort",
            self.tasks["Enforce the exact CrowdSec acquisition set"][
                "ansible.builtin.assert"
            ]["that"][0],
        )
        completed = run_task_definitions(
            [
                {
                    "name": "Evaluate the reviewed loop",
                    "ansible.builtin.assert": {
                        "that": [
                            (
                                "item in ['/var/log/dockerswarm',"
                                " '/var/log/dockerswarm/edge']"
                            )
                        ]
                    },
                    "loop": inspect["loop"],
                },
                {
                    "name": "Evaluate the reviewed loop length",
                    "ansible.builtin.assert": {"that": ["probe | length == 2"]},
                    "vars": {"probe": inspect["loop"]},
                },
            ],
            contract(),
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_a_linked_access_log_directory_stops_the_role(self) -> None:
        reject = load_task(TASKS, "Reject an unsafe Traefik access log directory")
        cases = {
            "absent": ({"exists": False}, True),
            "directory": ({"exists": True, "isdir": True, "islnk": False}, True),
            "link": ({"exists": True, "isdir": False, "islnk": True}, False),
            "file": ({"exists": True, "isdir": False, "islnk": False}, False),
        }
        for label, (stat_result, accepted) in cases.items():
            with self.subTest(label):
                completed = run_task_definitions(
                    [reject],
                    {
                        "host_security_traefik_access_log_dirs": {
                            "results": [
                                {"item": "/var/log/dockerswarm", "stat": stat_result}
                            ]
                        }
                    },
                )
                output = completed.stdout + completed.stderr
                self.assertEqual(completed.returncode == 0, accepted, output)
                if not accepted:
                    self.assertIn("is a link or not a directory", output)

    def test_allowlist_gates_run_before_any_crowdsec_change(self) -> None:
        preflight = "Gate the managed CrowdSec allowlist before any CrowdSec change"
        self.assert_before("Enforce the CrowdSec package origin", preflight)
        self.assert_before(
            preflight, "Inspect locked CrowdSec Hub candidates before installation"
        )
        # Before the first task of the role that can restart CrowdSec with
        # new content.
        first_restart = next(
            name
            for name in self.names
            if self.tasks[name].get("notify") == "Restart CrowdSec"
        )
        self.assert_before(preflight, first_restart)
        task = self.tasks[preflight]
        self.assertEqual(
            task["ansible.builtin.include_tasks"],
            {"file": "crowdsec_allowlist_gates.yml"},
        )
        self.assertIs(
            task["vars"]["host_security_crowdsec_allowlist_gate_strict"], False
        )

    def test_allowlist_writes_follow_the_strict_gates(self) -> None:
        strict = "Gate the managed CrowdSec allowlist before writing it"
        self.assert_before("Enable and start required host-security services", strict)
        self.assert_before("Validate the CrowdSec engine", strict)
        order = [
            "Create the host-only CrowdSec allowlist source directory",
            strict,
            "Create the managed CrowdSec allowlist",
            "Remove stale entries from the managed CrowdSec allowlist",
            "Add the reviewed entries to the managed CrowdSec allowlist",
            "Re-read the CrowdSec allowlists",
            "Verify the managed CrowdSec allowlist equals its source",
        ]
        self.assertEqual([name for name in self.names if name in order], order)
        self.assertEqual(
            self.tasks[strict]["ansible.builtin.include_tasks"],
            {"file": "crowdsec_allowlist_gates.yml"},
        )
        self.assertIs(
            self.tasks[strict]["vars"]["host_security_crowdsec_allowlist_gate_strict"],
            True,
        )
        self.assertIs(defaults()["host_security_crowdsec_allowlist_gate_strict"], True)
        directory = self.tasks[order[0]]["ansible.builtin.file"]
        self.assertEqual(directory["mode"], "0700")
        self.assertEqual(directory["owner"], "root")
        self.assertEqual(
            task_names(GATES),
            [
                "Read the host-only CrowdSec allowlist source",
                "Require a safe host-only CrowdSec allowlist source",
                "Read the CrowdSec allowlists",
                "Decode the CrowdSec allowlists and their host-only source",
                "Select the managed CrowdSec allowlist entries",
                "Reject every unreviewed CrowdSec allowlist",
                "Require well-formed entries in the managed CrowdSec allowlist",
                "Refuse to guess an absent CrowdSec allowlist source",
                "Plan the exact CrowdSec allowlist convergence",
            ],
        )
        # Every gate after the LAPI read needs its answer; the preflight pass
        # skips them when the LAPI is silent.
        for name, task in tasks_by_name(GATES).items():
            if task_names(GATES).index(name) > 2:
                with self.subTest(name):
                    self.assertEqual(
                        task["when"], "host_security_crowdsec_allowlists_raw.rc == 0"
                    )

    def test_every_task_that_sees_an_address_hides_it(self) -> None:
        for name in (
            "Read the host-only CrowdSec allowlist source",
            "Read the CrowdSec allowlists",
            "Decode the CrowdSec allowlists and their host-only source",
            "Select the managed CrowdSec allowlist entries",
            "Plan the exact CrowdSec allowlist convergence",
            "Remove stale entries from the managed CrowdSec allowlist",
            "Add the reviewed entries to the managed CrowdSec allowlist",
            "Re-read the CrowdSec allowlists",
        ):
            with self.subTest(name):
                self.assertIs(self.all_tasks[name].get("no_log"), True)
        # Assert messages may show names and counts, never an entry.
        for name in (
            "Require a safe host-only CrowdSec allowlist source",
            "Reject every unreviewed CrowdSec allowlist",
            "Require well-formed entries in the managed CrowdSec allowlist",
            "Refuse to guess an absent CrowdSec allowlist source",
            "Verify the managed CrowdSec allowlist equals its source",
        ):
            with self.subTest(name):
                message = self.all_tasks[name]["ansible.builtin.assert"]["fail_msg"]
                self.assertNotIn(".entries", message)
                self.assertNotIn("_add", message)
                self.assertNotIn("_remove", message)
                self.assertNotIn("stdout", message)
                for reference in re.findall(
                    r"host_security_crowdsec_allowlist_entries[^}]*", message
                ):
                    self.assertIn("| length", reference)

    def test_allowlist_mutations_never_run_in_check_mode(self) -> None:
        for name in (
            "Create the managed CrowdSec allowlist",
            "Remove stale entries from the managed CrowdSec allowlist",
            "Add the reviewed entries to the managed CrowdSec allowlist",
        ):
            with self.subTest(name):
                self.assertEqual(self.tasks[name]["when"][0], "not ansible_check_mode")
                argv = self.tasks[name]["ansible.builtin.command"]["argv"]
                self.assertIn("/usr/bin/cscli", str(argv))
        # A read-only gate still runs in check mode so it can stop an apply.
        for name in (
            "Read the host-only CrowdSec allowlist source",
            "Read the CrowdSec allowlists",
        ):
            with self.subTest(name):
                self.assertIs(self.gates[name]["check_mode"], False)
                self.assertNotIn("when", self.gates[name])
                self.assertIs(self.gates[name]["changed_when"], False)

    def test_only_the_strict_pass_needs_the_local_api(self) -> None:
        read = self.gates["Read the CrowdSec allowlists"]
        probes: list[dict[str, Any]] = []
        expected: list[str] = []
        # (strict, rc) -> (retry loop ends, task fails)
        # 124 and 137 are timeout(1) giving up on a LAPI that never answers.
        cases = {
            (True, 0): (True, False),
            (True, 1): (False, True),
            (True, 124): (False, True),
            (True, 137): (False, True),
            (False, 0): (True, False),
            (False, 1): (True, False),
            (False, 124): (True, False),
            (False, 137): (True, False),
        }
        for index, ((strict, rc), (stops, fails)) in enumerate(cases.items()):
            probes.append(
                {
                    "name": f"Probe {index}",
                    "ansible.builtin.set_fact": {
                        f"stops_{index}": f"{{{{ {read['until']} }}}}",
                        f"fails_{index}": f"{{{{ {read['failed_when']} }}}}",
                    },
                    "vars": {
                        "host_security_crowdsec_allowlist_gate_strict": strict,
                        "host_security_crowdsec_allowlists_raw": {"rc": rc},
                    },
                }
            )
            expected += [
                f"({'' if stops else 'not '}stops_{index} | bool)",
                f"({'' if fails else 'not '}fails_{index} | bool)",
            ]
        probes.append({"name": "Report", "ansible.builtin.assert": {"that": expected}})
        completed = run_task_definitions(probes, {})
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        # The strict pass waits a minute for a LAPI that is starting.
        self.assertEqual(read["retries"] * read["delay"], 60)

    def test_every_local_api_read_is_bounded(self) -> None:
        # cscli's LAPI client has no timeout (CrowdSec v1.7.8,
        # pkg/apiclient/auth_jwt.go:247-248): a LAPI that accepts and never
        # answers would hold the apply and the host-global lock forever.
        # `list`, `inspect` and `check` go through the LAPI; `create`, `add`,
        # `remove` and `delete` open the database.
        lapi = {"list", "inspect", "check"}
        bounded = []
        for name, task in self.all_tasks.items():
            argv = task.get("ansible.builtin.command", {}).get("argv")
            if not isinstance(argv, list) or "allowlists" not in argv:
                continue
            command = argv[argv.index("allowlists") + 1]
            if command not in lapi:
                continue
            with self.subTest(name):
                self.assertEqual(
                    argv[: argv.index("/usr/bin/cscli") + 1],
                    ["/usr/bin/timeout", "--kill-after=5", "30", "/usr/bin/cscli"],
                )
                bounded.append(name)
        self.assertEqual(
            sorted(bounded),
            ["Re-read the CrowdSec allowlists", "Read the CrowdSec allowlists"],
        )


def hub_inventory(
    *,
    missing: tuple[str, ...] = (),
    extra_scenarios: tuple[str, ...] = (),
) -> dict[str, list[dict[str, str]]]:
    """The cscli hub list JSON of a converged host, minus or plus items."""
    document = contract()
    items: dict[str, list[str]] = {
        "appsec-configs": [],
        "appsec-rules": [],
        "collections": [
            item["name"] for item in document["host_security_crowdsec_collection_lock"]
        ],
        "contexts": [],
        "parsers": [],
        "postoverflows": [],
        "scenarios": [document["host_security_crowdsec_basicauth_scenario"]],
    }
    for item in (
        document["host_security_crowdsec_hub_dependency_lock"]
        + document["host_security_crowdsec_standalone_hub_lock"]
    ):
        items[item["type"]].append(item["name"])
    items["scenarios"].extend(extra_scenarios)
    return {
        kind: [{"name": name} for name in names if name not in missing]
        for kind, names in items.items()
    }


class TraefikBasicAuthInventoryTests(unittest.TestCase):
    """The exact Hub inventory tolerates only what a --check cannot install."""

    DERIVE = "Derive the reviewed CrowdSec items only an apply installs"
    REJECT = "Reject every unreviewed installed CrowdSec Hub item"

    def run_inventory(
        self,
        inventory: dict[str, Any],
        *,
        scenario_file_exists: bool,
        check_mode: bool,
    ) -> subprocess.CompletedProcess[str]:
        variables = {
            **contract(),
            "host_security_crowdsec_hub_inventory": inventory,
            "host_security_crowdsec_basicauth_scenario_file": {
                "stat": {"exists": scenario_file_exists}
            },
        }
        return run_task_definitions(
            [load_task(TASKS, self.DERIVE), load_task(TASKS, self.REJECT)],
            variables,
            check_mode=check_mode,
        )

    def assert_outcome(
        self,
        completed: subprocess.CompletedProcess[str],
        accepted: bool,
    ) -> None:
        output = completed.stdout + completed.stderr
        if accepted:
            self.assertEqual(completed.returncode, 0, output)
        else:
            self.assertNotEqual(completed.returncode, 0, output)
            self.assertIn("unreviewed or missing item", output)

    def test_apply_requires_the_exact_inventory(self) -> None:
        cases = {
            "converged": (hub_inventory(), True),
            "parser missing": (
                hub_inventory(missing=("crowdsecurity/traefik-logs",)),
                False,
            ),
            "local scenario missing": (
                hub_inventory(missing=("apptolast/traefik-basicauth-bf",)),
                False,
            ),
            "extra scenario": (
                hub_inventory(extra_scenarios=("crowdsecurity/http-probing",)),
                False,
            ),
        }
        for label, (inventory, accepted) in cases.items():
            with self.subTest(label):
                self.assert_outcome(
                    self.run_inventory(
                        inventory, scenario_file_exists=True, check_mode=False
                    ),
                    accepted,
                )

    def test_check_mode_tolerates_only_what_the_apply_adds(self) -> None:
        before_first_apply = hub_inventory(
            missing=("crowdsecurity/traefik-logs", "apptolast/traefik-basicauth-bf")
        )
        cases = {
            "before the first apply": (before_first_apply, False, True),
            "converged host": (hub_inventory(), True, True),
            "dependency missing": (
                hub_inventory(
                    missing=(
                        "crowdsecurity/traefik-logs",
                        "crowdsecurity/sshd-logs",
                    )
                ),
                False,
                False,
            ),
            "extra scenario": (
                hub_inventory(
                    missing=("crowdsecurity/traefik-logs",),
                    extra_scenarios=("crowdsecurity/http-probing",),
                ),
                True,
                False,
            ),
            "scenario file present but not listed": (
                hub_inventory(missing=("apptolast/traefik-basicauth-bf",)),
                True,
                False,
            ),
        }
        for label, (inventory, file_exists, accepted) in cases.items():
            with self.subTest(label):
                self.assert_outcome(
                    self.run_inventory(
                        inventory,
                        scenario_file_exists=file_exists,
                        check_mode=True,
                    ),
                    accepted,
                )

    def test_installed_digest_gate_skips_only_pending_standalone_items(
        self,
    ) -> None:
        when = tasks_by_name()[
            "Enforce installed CrowdSec Hub versions and content digests"
        ]["when"]
        probes = []
        cases = {
            "pending standalone in check mode": ("crowdsecurity/traefik-logs", False),
            "installed standalone": ("crowdsecurity/traefik-logs", True),
            "missing dependency": ("crowdsecurity/sshd-logs", False),
        }
        for index, (name, installed) in enumerate(cases.values()):
            probes.append(
                {
                    "name": f"Probe {index}",
                    "ansible.builtin.set_fact": {
                        f"probe_{index}": f"{{{{ {when} }}}}",
                    },
                    "vars": {
                        "item": {
                            "item": {"name": name},
                            "stdout": json.dumps({"installed": installed}),
                        }
                    },
                }
            )
        probes.append(
            {
                "name": "Report the probes",
                "ansible.builtin.assert": {
                    "that": [
                        "not probe_0 | bool",
                        "probe_1 | bool",
                        "probe_2 | bool",
                    ],
                },
            }
        )
        completed = run_task_definitions(probes, contract(), check_mode=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        # Outside check mode every item is enforced.
        applied = copy.deepcopy(probes)
        applied[-1]["ansible.builtin.assert"]["that"] = [
            "probe_0 | bool",
            "probe_1 | bool",
            "probe_2 | bool",
        ]
        completed = run_task_definitions(applied, contract())
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


def allowlist_item(value: str, expiration: str = PERMANENT) -> dict[str, str]:
    return {
        "value": value,
        "expiration": expiration,
        "created_at": "2026-09-20T01:32:13.279Z",
        "description": "test",
    }


class TraefikBasicAuthAllowlistPlanTests(unittest.TestCase):
    """The allowlist converges exactly to its source, or stops."""

    GATES = (
        "Decode the CrowdSec allowlists and their host-only source",
        "Select the managed CrowdSec allowlist entries",
        "Reject every unreviewed CrowdSec allowlist",
        "Require well-formed entries in the managed CrowdSec allowlist",
        "Refuse to guess an absent CrowdSec allowlist source",
        "Plan the exact CrowdSec allowlist convergence",
    )

    def run_plan(
        self,
        allowlists: Any,
        source: dict[str, Any],
        expectations: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        tasks = [load_task(GATES, name) for name in self.GATES]
        if expectations:
            tasks.append(
                {
                    "name": "Check the plan",
                    "ansible.builtin.assert": {"that": expectations},
                }
            )
        variables = {
            **contract(),
            "host_security_crowdsec_allowlists_raw": {
                "rc": 0,
                "stdout": json.dumps(allowlists),
            },
            "host_security_crowdsec_allowlist_source_raw": {
                "rc": 0,
                "stdout": json.dumps(source),
                "stderr": "",
            },
        }
        return run_task_definitions(tasks, variables)

    def test_exact_convergence_removes_stale_and_expiring_entries(self) -> None:
        completed = self.run_plan(
            [
                {
                    "name": "apptolast-trusted",
                    "description": "Operator trusted IPs - never ban",
                    "items": [
                        allowlist_item("1.1.1.1"),
                        allowlist_item("9.9.9.9"),
                        allowlist_item("8.8.8.0/24", "2026-10-01T00:00:00.000Z"),
                    ],
                }
            ],
            {"present": True, "entries": ["1.1.1.1", "8.8.8.0/24", "2606:4700::/48"]},
            [
                "host_security_crowdsec_allowlist_exists",
                (
                    "host_security_crowdsec_allowlist_remove | sort"
                    " == ['8.8.8.0/24', '9.9.9.9']"
                ),
                (
                    "host_security_crowdsec_allowlist_add | sort"
                    " == ['2606:4700::/48', '8.8.8.0/24']"
                ),
            ],
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_converged_allowlist_plans_nothing(self) -> None:
        for label, (allowlists, source) in {
            "converged": (
                [{"name": "apptolast-trusted", "items": [allowlist_item("1.1.1.1")]}],
                {"present": True, "entries": ["1.1.1.1"]},
            ),
            "no list and no source": (None, {"present": False, "entries": []}),
            "empty list and no source": (
                [{"name": "apptolast-trusted", "items": []}],
                {"present": False, "entries": []},
            ),
        }.items():
            with self.subTest(label):
                completed = self.run_plan(
                    allowlists,
                    source,
                    [
                        "host_security_crowdsec_allowlist_remove == []",
                        "host_security_crowdsec_allowlist_add == []",
                    ],
                )
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )

    def test_new_source_creates_the_list(self) -> None:
        completed = self.run_plan(
            [],
            {"present": True, "entries": ["1.1.1.1"]},
            [
                "not host_security_crowdsec_allowlist_exists",
                "host_security_crowdsec_allowlist_add == ['1.1.1.1']",
            ],
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        create = tasks_by_name()["Create the managed CrowdSec allowlist"]
        self.assertIn("not host_security_crowdsec_allowlist_exists", create["when"])

    def test_ambiguous_or_unreviewed_state_stops(self) -> None:
        cases = {
            "absent source with entries": (
                [{"name": "apptolast-trusted", "items": [allowlist_item("1.1.1.1")]}],
                {"present": False, "entries": []},
                "does not exist",
            ),
            "unreviewed allowlist": (
                [
                    {"name": "apptolast-trusted", "items": []},
                    {"name": "friends", "items": [allowlist_item("1.1.1.1")]},
                ],
                {"present": True, "entries": []},
                "(friends)",
            ),
            "console managed": (
                [
                    {
                        "name": "apptolast-trusted",
                        "console_managed": True,
                        "items": [],
                    }
                ],
                {"present": True, "entries": []},
                "CrowdSec console",
            ),
            "entry without expiration": (
                [{"name": "apptolast-trusted", "items": [{"value": "1.1.1.1"}]}],
                {"present": True, "entries": ["1.1.1.1"]},
                "without a value or an",
            ),
        }
        for label, (allowlists, source, message) in cases.items():
            with self.subTest(label):
                completed = self.run_plan(allowlists, source)
                output = completed.stdout + completed.stderr
                self.assertNotEqual(completed.returncode, 0, output)
                self.assertIn(message, output)
                self.assertNotIn("1.1.1.1", output)

    def test_the_preflight_pass_gates_before_any_change(self) -> None:
        # The first host-baseline apply on a host whose hand-made allowlist
        # holds an entry and whose host-only source does not exist yet stops
        # here, before any CrowdSec restart.
        completed = self.run_plan(
            [{"name": "apptolast-trusted", "items": [allowlist_item("1.1.1.1")]}],
            {"present": False, "entries": []},
        )
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0, output)
        self.assertIn("does not exist", output)
        self.assertNotIn("1.1.1.1", output)

    def test_a_silent_local_api_skips_only_the_preflight_gates(self) -> None:
        # A stopped CrowdSec must stay repairable: without an answer the
        # gates are skipped, never evaluated against nothing.
        tasks = [load_task(GATES, name) for name in self.GATES]
        completed = run_task_definitions(
            tasks,
            {
                **contract(),
                "host_security_crowdsec_allowlists_raw": {"rc": 1, "stdout": ""},
                "host_security_crowdsec_allowlist_source_raw": {
                    "rc": 0,
                    "stdout": json.dumps({"present": False, "entries": []}),
                    "stderr": "",
                },
            },
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("skipping", completed.stdout)

    def test_an_unsafe_source_stops_even_without_the_local_api(self) -> None:
        require = load_task(GATES, "Require a safe host-only CrowdSec allowlist source")
        self.assertNotIn("when", require)
        completed = run_task_definitions(
            [require],
            {
                **contract(),
                "host_security_crowdsec_allowlist_source_raw": {
                    "rc": 1,
                    "stdout": "",
                    "stderr": "ERROR: line 2: not globally routable\n",
                },
            },
        )
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0, output)
        self.assertIn("not globally routable", output)

    def test_final_state_must_equal_the_source(self) -> None:
        verify = load_task(
            TASKS, "Verify the managed CrowdSec allowlist equals its source"
        )
        source = {"present": True, "entries": ["1.1.1.1", "2606:4700::/48"]}
        cases = {
            "exact": (
                [
                    {
                        "name": "apptolast-trusted",
                        "items": [
                            allowlist_item("2606:4700::/48"),
                            allowlist_item("1.1.1.1"),
                        ],
                    }
                ],
                True,
            ),
            "cscli skipped a value": (
                [{"name": "apptolast-trusted", "items": [allowlist_item("1.1.1.1")]}],
                False,
            ),
            "an entry kept its expiry": (
                [
                    {
                        "name": "apptolast-trusted",
                        "items": [
                            allowlist_item("1.1.1.1"),
                            allowlist_item(
                                "2606:4700::/48", "2026-10-01T00:00:00.000Z"
                            ),
                        ],
                    }
                ],
                False,
            ),
            # Only the length clause catches it: the permanent entries alone
            # equal the source.
            "a temporary extra entry survived": (
                [
                    {
                        "name": "apptolast-trusted",
                        "items": [
                            allowlist_item("1.1.1.1"),
                            allowlist_item("2606:4700::/48"),
                            allowlist_item("9.9.9.9", "2099-01-01T00:00:00.000Z"),
                        ],
                    }
                ],
                False,
            ),
            "another list appeared": (
                [
                    {
                        "name": "apptolast-trusted",
                        "items": [
                            allowlist_item("1.1.1.1"),
                            allowlist_item("2606:4700::/48"),
                        ],
                    },
                    {"name": "other", "items": []},
                ],
                False,
            ),
        }
        for label, (final, accepted) in cases.items():
            with self.subTest(label):
                completed = run_task_definitions(
                    [verify],
                    {
                        **contract(),
                        "host_security_crowdsec_allowlist_source_state": source,
                        "host_security_crowdsec_allowlists_final_raw": {
                            "rc": 0,
                            "stdout": json.dumps(final),
                        },
                    },
                )
                output = completed.stdout + completed.stderr
                self.assertEqual(completed.returncode == 0, accepted, output)
                if not accepted:
                    self.assertIn("does not hold exactly the entries", output)
                    self.assertNotIn("1.1.1.1", output)
                    self.assertNotIn("9.9.9.9", output)


class AllowlistSourceTests(unittest.TestCase):
    """scripts/validate-crowdsec-allowlist.py reads its file fail-closed."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "crowdsec"
        self.directory.mkdir(mode=0o700)
        self.directory.chmod(0o700)
        self.source = self.directory / "trusted-ips"
        self.uid = os.getuid()
        self.gid = os.getgid()

    def write(self, content: str | bytes, mode: int = 0o600) -> None:
        data = content.encode("ascii") if isinstance(content, str) else content
        self.source.write_bytes(data)
        self.source.chmod(mode)

    def load(self) -> dict[str, Any]:
        return allowlist_source.load(self.source, self.uid, self.gid)

    def assert_rejected(self, fragment: str) -> str:
        with self.assertRaises(allowlist_source.AllowlistSourceError) as caught:
            self.load()
        self.assertIn(fragment, str(caught.exception))
        return str(caught.exception)

    def test_absent_directory_or_file_is_not_configured(self) -> None:
        self.assertEqual(self.load(), {"present": False, "entries": []})
        self.directory.rmdir()
        self.assertEqual(self.load(), {"present": False, "entries": []})

    def test_entries_are_returned_in_cscli_canonical_form(self) -> None:
        self.write(
            "# Owner, home line\n"
            "\n"
            "  1.1.1.1  \n"
            "9.9.9.0/24\n"
            "8.8.4.4/32\n"
            "2606:4700:4700:0000:0000:0000:0000:1111\n"
            "2606:4700::/48\n"
        )
        self.assertEqual(
            self.load(),
            {
                "present": True,
                "entries": [
                    "1.1.1.1",
                    "9.9.9.0/24",
                    "8.8.4.4",
                    "2606:4700:4700::1111",
                    "2606:4700::/48",
                ],
            },
        )

    def test_an_empty_source_is_present_with_no_entries(self) -> None:
        self.write("# nobody is trusted\n")
        self.assertEqual(self.load(), {"present": True, "entries": []})

    def test_unsafe_entries_are_rejected_without_echoing_them(self) -> None:
        cases = {
            "private": ("10.1.2.3", "not globally routable"),
            "documentation": ("203.0.113.5", "not globally routable"),
            "loopback v6": ("::1", "not globally routable"),
            "wide v4": ("9.9.0.0/16", "no wider than /24"),
            "wide v6": ("2606:4700::/32", "no wider than /48"),
            "everything": ("0.0.0.0/0", "no wider than /24"),
            "host bits": ("9.9.9.9/24", "not an IP address"),
            "garbage": ("owner-laptop", "not an IP address"),
            "inline comment": ("1.1.1.1 # me", "one entry per line"),
            "mapped": ("::ffff:1.1.1.1", "IPv4-mapped"),
        }
        for label, (entry, fragment) in cases.items():
            with self.subTest(label):
                self.write(f"1.0.0.1\n{entry}\n")
                message = self.assert_rejected(fragment)
                self.assertIn("line 2", message)
                self.assertNotIn(entry.split()[0], message)

    def test_duplicates_and_oversized_sources_are_rejected(self) -> None:
        self.write("1.1.1.1\n1.1.1.1/32\n")
        self.assert_rejected("duplicate entry")
        self.write("".join(f"1.1.1.{index}\n" for index in range(1, 18)))
        self.assert_rejected("more than 16 entries")
        self.write("#" * 5000 + "\n")
        self.assert_rejected("exceeds 4096 bytes")
        self.write(b"1.1.1.1\r\n")
        self.assert_rejected("unsafe bytes")
        self.write(b"1.1.1.1\n" + "# café\n".encode())
        self.assert_rejected("ASCII")

    def test_file_identity_is_checked_on_the_open_descriptor(self) -> None:
        self.write("1.1.1.1\n", mode=0o644)
        self.assert_rejected("regular 0600 file")
        self.write("1.1.1.1\n")
        with self.assertRaises(allowlist_source.AllowlistSourceError):
            allowlist_source.load(self.source, self.uid + 1, self.gid)
        link = self.directory / "second-name"
        os.link(self.source, link)
        self.assert_rejected("single link")
        link.unlink()
        self.assertEqual(self.load()["entries"], ["1.1.1.1"])

    def test_links_and_loose_directories_are_refused(self) -> None:
        real = self.directory / "real"
        real.write_text("1.1.1.1\n", encoding="ascii")
        real.chmod(0o600)
        self.source.symlink_to(real)
        self.assert_rejected("symbolic link")
        self.source.unlink()
        fifo = self.source
        os.mkfifo(fifo, 0o600)
        self.assert_rejected("regular 0600 file")
        fifo.unlink()
        self.directory.chmod(0o755)
        self.assert_rejected("0700 directory")
        self.directory.chmod(0o700)
        moved = Path(self.temporary.name) / "moved"
        self.directory.rename(moved)
        self.directory.symlink_to(moved)
        self.assert_rejected("symbolic link")

    def test_relative_paths_are_refused(self) -> None:
        with self.assertRaises(allowlist_source.AllowlistSourceError):
            allowlist_source.load(Path("trusted-ips"), self.uid, self.gid)

    def test_main_prints_json_or_a_reason_without_content(self) -> None:
        # main() checks the production owner, root; a directory that does not
        # exist yet is "not configured" for any owner.
        absent = Path(self.temporary.name) / "missing" / "trusted-ips"
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = allowlist_source.main([str(absent)])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()), {"entries": [], "present": False}
        )
        self.write("1.1.1.1\n")
        stderr = StringIO()
        with redirect_stderr(stderr):
            code = allowlist_source.main([str(self.source)])
        self.assertEqual(code, 1)
        self.assertIn("owned by 0:0", stderr.getvalue())
        self.assertNotIn("1.1.1.1", stderr.getvalue())
        stderr = StringIO()
        with redirect_stderr(stderr):
            code = allowlist_source.main(["relative/trusted-ips"])
        self.assertEqual(code, 1)
        self.assertIn("absolute", stderr.getvalue())

    def test_script_runs_under_the_host_interpreter(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SOURCE_SCRIPT),
                str(Path(self.temporary.name) / "missing" / "trusted-ips"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout), {"entries": [], "present": False}
        )
        self.assertEqual(stat.S_IMODE(SOURCE_SCRIPT.stat().st_mode) & 0o022, 0)

    def test_the_sensitive_path_guard_covers_the_validator(self) -> None:
        # The validator alone enforces public entries no wider than /24 or
        # /48, so relaxing it must get the same sensitive-path label as the
        # role, like scripts/validate-authorized-keys.py.
        guard = (
            PROJECT_ROOT / ".github/workflows/guard-sensitive-paths.yml"
        ).read_text(encoding="utf-8")
        lines = guard.split("<<'PATTERNS'\n", 1)[1].splitlines()
        patterns = [
            re.compile(line.strip())
            for line in lines[: [line.strip() for line in lines].index("PATTERNS")]
        ]
        for path in (
            SOURCE_SCRIPT.relative_to(PROJECT_ROOT).as_posix(),
            GATES,
            TASKS,
            CONTRACT_PATH.relative_to(PROJECT_ROOT).as_posix(),
        ):
            with self.subTest(path):
                self.assertTrue(any(pattern.search(path) for pattern in patterns))


class TraefikBasicAuthDocsTests(unittest.TestCase):
    """The runbooks fail loudly and never print an allowlisted address."""

    def test_the_allowlist_adoption_fails_loudly_and_counts_by_size(self) -> None:
        text = (PROJECT_ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
        section = text[
            text.index("### Direcciones que nunca se banean") : text.index(
                "### Si el propietario queda baneado"
            )
        ]
        block = section[section.index("sudo -v\n") :]
        block = block[: block.index("```")]
        # One password prompt before a pipeline with two sudo, and a cscli
        # or jq failure that cannot leave an empty file behind in silence.
        for fragment in (
            "set -o pipefail",
            "cscli allowlists inspect apptolast-trusted -o json",
            "FALLO",
            "sudo -- cscli allowlists list",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, block)
        self.assertLess(
            block.index("set -o pipefail"), block.index("cscli allowlists inspect")
        )
        self.assertIn("`Size`", section)
        self.assertNotIn("wc -l", section)

    def test_the_readme_uses_the_real_rate_limits(self) -> None:
        readme = " ".join((ROLE / "README.md").read_text(encoding="utf-8").split())
        self.assertNotIn("(30 a minute)", readme)
        self.assertNotIn("about 22 seconds", readme)
        for fragment in (
            "`ax-rl-ip` and `ax-rl-host`",
            "`update_frequency: 10s`",
            "3 000 to 5 000 a day per IP",
            "`pkg/leakybucket/bucket.go:82`",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, readme)


if __name__ == "__main__":
    unittest.main()
