"""Contract of the exclusions and limits host_baseline sets for the nightly AIDE.

On 2026-10-14 `aide --update` ran for 4 h 26 min with 3.4 GiB (4.1-4.4 GiB and
~2 h of CPU on every earlier night) over a reference database from 2026-07-21
that was never renewed: 3 167 981 entries, 3 001 595 of them "added" (container
layers, caches, working copies). On a host without swap it left Traefik without
page cache and every site went down. These tests cover the validator rules, the
role's gates (run through the reviewed-task harness against synthetic reads,
never against the host), the two rendered files, the intent of the exclusions
with the real regexes, and the runbook.
"""

from __future__ import annotations

import copy
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined

from ansible_task_harness import AnsibleTaskAssertions, run_task_definitions
from test_host_bootstrap_contract import (
    PROJECT_ROOT,
    host_security_validator,
    load_tasks,
)

ROLE = "ansible/roles/host_baseline"
TASKS = f"{ROLE}/tasks/aide.yml"
EXCLUDES_TEMPLATE = PROJECT_ROOT / ROLE / "templates/aide-dockerswarm-excludes.j2"
LIMITS_TEMPLATE = PROJECT_ROOT / ROLE / "templates/aide-service-limits.conf.j2"
CONTRACT = PROJECT_ROOT / "config/host-security.yml"

BOUNDS_GATE = "Verify the AIDE exclusions and limits"
INSTALLED_GATE = "Verify AIDE is installed"
CANDIDATE_GATE = "Verify the AIDE exclusions candidate"
UNIT_GATE = "Verify systemd enforces the reviewed AIDE limits"
EXCLUDES_INSTALL = "Install the AIDE exclusions"
LIMITS_INSTALL = "Install the AIDE resource limits"
CONFIG_CHECK = "Check the merged AIDE configuration with aide itself"

LIVE = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
EXCLUDED = LIVE["host_security_aide_excluded_paths"]
PROTECTED = LIVE["host_security_aide_protected_paths"]
VARIABLES = {
    "host_baseline_aide_excluded_paths": EXCLUDED,
    "host_baseline_aide_protected_paths": PROTECTED,
    "host_baseline_aide_memory_high": "1536M",
    "host_baseline_aide_memory_max": "2G",
    "host_baseline_aide_runtime_max": "3h",
}


def render(template: Path, **overrides: Any) -> str:
    return (
        Environment(
            autoescape=False,
            keep_trailing_newline=True,
            trim_blocks=True,
            undefined=StrictUndefined,
        )
        .from_string(template.read_text(encoding="utf-8"))
        .render(**{**VARIABLES, **overrides})
    )


def rules(text: str) -> list[str]:
    return [line for line in text.splitlines() if line and not line.startswith("#")]


class AideValidatorTests(unittest.TestCase):
    """scripts/validate-host-security.py rejects every unsafe AIDE setting."""

    NOW = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
    EXCLUDED = "host_security_aide_excluded_paths"
    PROTECTED = "host_security_aide_protected_paths"
    HIGH = "host_security_aide_memory_high"
    MAX = "host_security_aide_memory_max"
    RUNTIME = "host_security_aide_runtime_max"

    def setUp(self) -> None:
        self.contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
        self.contract["host_security_ubuntu_snapshot"] = "20260819T000000Z"

    def assert_rejected(self, change: dict[str, Any], message: str) -> None:
        contract = copy.deepcopy(self.contract)
        for key, value in change.items():
            if value is None:
                contract.pop(key)
            else:
                contract[key] = value
        with self.assertRaisesRegex(
            host_security_validator.HostSecurityContractError, message
        ):
            host_security_validator.validate(contract, self.NOW)

    def assert_accepted(self, change: dict[str, Any]) -> None:
        contract = copy.deepcopy(self.contract)
        contract.update(change)
        host_security_validator.validate(contract, self.NOW)

    def test_the_reviewed_settings_are_the_live_ones(self) -> None:
        self.assertEqual(self.contract[self.HIGH], "1536M")
        self.assertEqual(self.contract[self.MAX], "2G")
        self.assertEqual(self.contract[self.RUNTIME], "3h")
        host_security_validator.validate(self.contract, self.NOW)

    def test_both_lists_are_required_and_not_empty(self) -> None:
        for key in (self.EXCLUDED, self.PROTECTED):
            for label, value in (
                ("missing", None),
                ("empty", []),
                ("a string", "/tmp/[^/]+$"),
                ("a mapping", {"a": "b"}),
            ):
                with self.subTest(key=key, value=label):
                    self.assert_rejected(
                        {key: value}, f"{key} must be a non-empty list"
                    )

    def test_every_rule_is_a_path_without_whitespace_that_ends_in_dollar(self) -> None:
        # A space would start the rule's restriction field; without `$` the
        # rule would also match every sibling that merely starts like it.
        for label, rule in {
            "no dollar": "/var/lib/docker",
            "a space": "/var/lib/my dir$",
            "a tab": "/var/lib/my\tdir$",
            "a newline": "/var/lib/a\\n$\n-/etc$",
            "relative": "var/lib/docker$",
            "only a dollar": "$",
            "empty": "",
            "an integer": 42,
        }.items():
            with self.subTest(label):
                self.assert_rejected(
                    {self.EXCLUDED: [*EXCLUDED, rule]},
                    "not a path without whitespace ending in",
                )

    def test_an_invalid_regex_is_rejected_before_aide_reads_it(self) -> None:
        self.assert_rejected({self.EXCLUDED: [*EXCLUDED, "/tmp/($"]}, "invalid regex")

    def test_no_rule_is_declared_twice(self) -> None:
        self.assert_rejected(
            {self.EXCLUDED: [*EXCLUDED, EXCLUDED[0]]}, "more than once"
        )

    def test_no_rule_may_stop_watching_a_protected_path(self) -> None:
        for label, rule in {
            "all of /etc": "/etc$",
            "an ssh directory": "/home/admin/\\.ssh$",
            "any home dotfile": "/home/admin/\\.[^/]+$",
            "every child of /home/admin": "/home/admin/[^/]+$",
            "the binaries": "/usr/(bin|sbin)/[^/]+$",
            "/tmp itself": "/tmp$",
            "everything": "/.*$",
            "the AIDE database": "/var/lib/aide$",
        }.items():
            with self.subTest(label):
                self.assert_rejected(
                    {self.EXCLUDED: [*EXCLUDED, rule]}, "would stop watching"
                )

    def test_the_noise_that_held_the_entries_stays_excluded(self) -> None:
        for noise in (
            "/var/lib/containerd",
            "/var/lib/docker",
            "/home/admin/.cache",
            "/tmp/claude-1001",
        ):
            kept = [rule for rule in EXCLUDED if not re.match(rule, noise)]
            with self.subTest(noise):
                self.assert_rejected(
                    {self.EXCLUDED: kept}, f"no AIDE exclusion covers {noise}"
                )

    def test_the_protected_paths_are_absolute(self) -> None:
        self.assert_rejected(
            {self.PROTECTED: [*PROTECTED, "etc/passwd"]}, "absolute paths"
        )
        self.assert_rejected({self.PROTECTED: [*PROTECTED, 7]}, "absolute paths")

    def test_memory_high_stays_below_memory_max(self) -> None:
        self.assert_rejected({self.HIGH: "2G"}, "must stay below")
        self.assert_rejected({self.HIGH: "3G"}, "must stay below")
        self.assert_accepted({self.HIGH: "2047M"})

    def test_the_ceiling_would_have_prevented_the_2026_10_14_outage(self) -> None:
        # AIDE peaked at 4.4 GiB and was at 3.4 GiB when it was stopped.
        self.assert_rejected({self.MAX: "5G"}, "must not exceed 4G")
        self.assert_rejected({self.MAX: "4097M"}, "must not exceed 4G")
        self.assert_accepted({self.MAX: "4G"})

    def test_memory_high_cannot_starve_aide_of_its_own_database(self) -> None:
        self.assert_rejected({self.HIGH: "511M"}, "at least 512M")
        self.assert_accepted({self.HIGH: "512M"})

    def test_every_memory_value_is_a_whole_m_or_g_size(self) -> None:
        for key in (self.HIGH, self.MAX):
            for label, value in (
                ("missing", None),
                ("integer", 2048),
                ("a B", "2GB"),
                ("lower case", "2g"),
                ("zero", "0G"),
                ("fraction", "1.5G"),
                ("infinity", "infinity"),
            ):
                with self.subTest(key=key, value=label):
                    self.assert_rejected(
                        {key: value}, f"{key} must be a whole number of M or G"
                    )

    def test_the_run_must_end(self) -> None:
        self.assert_accepted({self.RUNTIME: "1h"})
        self.assert_accepted({self.RUNTIME: "4h"})
        for label, value in (
            ("missing", None),
            ("five hours", "5h"),
            ("zero", "0h"),
            ("minutes", "90m"),
            ("infinity", "infinity"),
            ("an integer", 3),
        ):
            with self.subTest(label):
                self.assert_rejected({self.RUNTIME: value}, "must be 1h to 4h")


class AideIntentTests(unittest.TestCase):
    """The reviewed regexes do what the runbook says, checked with real regexes.

    AIDE anchors every expression at the start of the full path (`^` implicit),
    which is what `re.match` does. The same paths were checked on the host with
    `aide -p` against AIDE 0.19.2 (docs/OPERATIONS.md, "AIDE").
    """

    @staticmethod
    def excluded(path: str) -> bool:
        return any(re.match(rule, path) for rule in EXCLUDED)

    def test_regenerable_noise_is_excluded(self) -> None:
        for path in (
            "/var/lib/containerd",
            "/var/lib/docker",
            "/var/lib/apptolast-tmp-archive",
            "/home/admin/.cache",
            "/home/admin/.npm",
            "/home/admin/.nvm",
            "/home/admin/.claude",
            "/home/admin/.claude-scratch-archive",
            "/home/admin/.codex",
            "/home/admin/infraestructure",
            "/home/admin/scratch-axweb",
            "/home/admin/scratch-archive-20260926",
            "/home/admin/codex-php-observer-gates-934p2ch5",
            "/opt/dockerswarm/ax-lab/cache",
            "/opt/dockerswarm/ax-lab/src",
            "/tmp/claude-1001",
            "/tmp/tmp.E2ED8dtIu3",
        ):
            with self.subTest(path):
                self.assertTrue(self.excluded(path))

    def test_what_is_worth_watching_is_not(self) -> None:
        for path in PROTECTED:
            with self.subTest(path):
                self.assertFalse(self.excluded(path))

    def test_the_dollar_keeps_lookalike_names_watched(self) -> None:
        # Non-recursive rules only match the directory itself. A sibling that
        # starts the same way, or a deeper path, is never matched by accident.
        for path in (
            "/var/lib/containerd-other",
            "/var/lib/docker.bak",
            "/home/admin/.cache2",
            "/home/admin/.claudex",
            "/home/admin/infraestructure-old",
            "/home/admin/scratch",
            "/home/admin/.ssh",
            "/opt/dockerswarm/ax-lab/bin",
            "/opt/dockerswarm/ax-lab/cache2",
            "/tmp",
            "/tmp/a/b",
            "/srv/dockerswarm/services",
        ):
            with self.subTest(path):
                self.assertFalse(self.excluded(path))

    def test_the_children_are_left_to_aide_not_to_the_regex(self) -> None:
        # `-regex` is non-recursive: AIDE does not descend into a matching
        # directory (aide.conf(5), 0.19), so a rule for the directory is enough
        # and no rule may name a child. The regex deliberately does not match
        # them, which is why the host check used `aide -p`.
        self.assertFalse(self.excluded("/var/lib/docker/overlay2"))
        self.assertFalse(self.excluded("/home/admin/.cache/ax-build"))


class AideRoleTests(AnsibleTaskAssertions, unittest.TestCase):
    """The role's gates, run against synthetic reads."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tasks = load_tasks(TASKS)

    def bounds(self, **change: Any) -> dict[str, Any]:
        return {**VARIABLES, **change}

    def test_the_bounds_gate_accepts_the_reviewed_values(self) -> None:
        self.assert_task_accepts(TASKS, BOUNDS_GATE, VARIABLES)

    def test_the_bounds_gate_accepts_the_edges_of_the_validator(self) -> None:
        for high, maximum, runtime in (
            ("512M", "4G", "1h"),
            ("512M", "4096M", "4h"),
            ("2047M", "2G", "3h"),
        ):
            with self.subTest(high=high, maximum=maximum, runtime=runtime):
                self.assert_task_accepts(
                    TASKS,
                    BOUNDS_GATE,
                    self.bounds(
                        host_baseline_aide_memory_high=high,
                        host_baseline_aide_memory_max=maximum,
                        host_baseline_aide_runtime_max=runtime,
                    ),
                )

    def test_the_bounds_gate_repeats_every_validator_rule(self) -> None:
        # deploy-ansible.sh never runs the validator, so the role must refuse
        # what the validator refuses.
        rejected = {
            "empty exclusions": {"host_baseline_aide_excluded_paths": []},
            "no dollar": {"host_baseline_aide_excluded_paths": [*EXCLUDED, "/var/x"]},
            "a space": {"host_baseline_aide_excluded_paths": [*EXCLUDED, "/var/a b$"]},
            "a rule twice": {
                "host_baseline_aide_excluded_paths": [*EXCLUDED, EXCLUDED[0]]
            },
            "reaches /etc": {"host_baseline_aide_excluded_paths": [*EXCLUDED, "/etc$"]},
            "reaches .ssh": {
                "host_baseline_aide_excluded_paths": [
                    *EXCLUDED,
                    "/home/admin/\\.ssh$",
                ]
            },
            "reaches every home dotfile": {
                "host_baseline_aide_excluded_paths": [
                    *EXCLUDED,
                    "/home/admin/\\.[^/]+$",
                ]
            },
            "reaches /tmp itself": {
                "host_baseline_aide_excluded_paths": [*EXCLUDED, "/tmp$"]
            },
            "no protected paths": {"host_baseline_aide_protected_paths": []},
            "high equals max": {"host_baseline_aide_memory_high": "2G"},
            "high above max": {"host_baseline_aide_memory_high": "3G"},
            "max above 4G": {"host_baseline_aide_memory_max": "5G"},
            "high below 512M": {"host_baseline_aide_memory_high": "511M"},
            "a B suffix": {"host_baseline_aide_memory_max": "2GB"},
            "infinity": {"host_baseline_aide_memory_max": "infinity"},
            "five hours": {"host_baseline_aide_runtime_max": "5h"},
            "minutes": {"host_baseline_aide_runtime_max": "90m"},
            "no limit": {"host_baseline_aide_runtime_max": "infinity"},
        }
        for label, change in rejected.items():
            with self.subTest(label):
                self.assert_task_rejects(
                    TASKS,
                    BOUNDS_GATE,
                    self.bounds(**change),
                    "The AIDE exclusions must be unique paths",
                )

    def test_the_installed_gate_stops_when_host_security_has_not_run(self) -> None:
        variables = {"host_baseline_aide_rules_dir": "/etc/aide/aide.conf.d"}
        self.assert_task_accepts(
            TASKS,
            INSTALLED_GATE,
            {
                **variables,
                "host_baseline_aide_rules_dir_state": {
                    "stat": {"exists": True, "isdir": True}
                },
            },
        )
        for label, state in {
            "missing": {"stat": {"exists": False}},
            "a file": {"stat": {"exists": True, "isdir": False}},
        }.items():
            with self.subTest(label):
                self.assert_task_rejects(
                    TASKS,
                    INSTALLED_GATE,
                    {**variables, "host_baseline_aide_rules_dir_state": state},
                    "host_security installs",
                )

    # -- the rendered exclusions --------------------------------------------

    def test_the_exclusions_are_one_non_recursive_negative_rule_per_path(self) -> None:
        rendered = render(EXCLUDES_TEMPLATE)
        self.assertEqual(rules(rendered), [f"-{path}" for path in EXCLUDED])
        self.assertTrue(rendered.endswith("\n"))
        # Never a positive rule, a recursive `!`, or a restriction field.
        for line in rules(rendered):
            with self.subTest(line):
                self.assertRegex(line, r"^-/[^\s]*\$$")

    def test_the_candidate_gate_accepts_only_the_reviewed_exclusions(self) -> None:
        reviewed = render(EXCLUDES_TEMPLATE)
        self.assert_task_accepts(
            TASKS,
            CANDIDATE_GATE,
            {**VARIABLES, "host_baseline_aide_candidate": reviewed},
        )
        first = f"-{EXCLUDED[0]}\n"
        rejected = {
            "a rule is missing": reviewed.replace(first, ""),
            "an extra rule": reviewed + "-/etc$\n",
            "a positive rule": reviewed.replace(first, f"{EXCLUDED[0]}\n"),
            "a recursive rule": reviewed.replace(first, f"!{EXCLUDED[0]}\n"),
            "a restriction field": reviewed.replace(first, f"-{EXCLUDED[0]} d\n"),
            "the order changes": "".join(reversed(reviewed.splitlines(keepends=True))),
        }
        for label, candidate in rejected.items():
            with self.subTest(label):
                self.assert_task_rejects(
                    TASKS,
                    CANDIDATE_GATE,
                    {**VARIABLES, "host_baseline_aide_candidate": candidate},
                    "one non-recursive negative rule per reviewed path",
                )

    # -- the rendered limits ------------------------------------------------

    def test_the_limits_drop_in_is_seven_plain_settings(self) -> None:
        rendered = render(LIMITS_TEMPLATE)
        self.assertEqual(
            rules(rendered),
            [
                "[Service]",
                "Nice=19",
                "IOSchedulingClass=idle",
                "CPUWeight=20",
                "MemoryHigh=1536M",
                "MemoryMax=2G",
                "RuntimeMaxSec=3h",
                "OOMScoreAdjust=1000",
            ],
        )

    # -- what systemd really enforces ---------------------------------------

    @staticmethod
    def status(**change: str) -> dict[str, Any]:
        reviewed = {
            "MemoryHigh": "1610612736",
            "MemoryMax": "2147483648",
            "RuntimeMaxUSec": "3h",
            "Nice": "19",
            "IOSchedulingClass": "3",
            "CPUWeight": "20",
            "OOMScoreAdjust": "1000",
        }
        return {"host_baseline_aide_unit": {"status": {**reviewed, **change}}}

    def unit_variables(self, **change: str) -> dict[str, Any]:
        return {
            **VARIABLES,
            "host_baseline_aide_unit_dropin": (
                "/etc/systemd/system/dailyaidecheck.service.d/"
                "60-dockerswarm-limits.conf"
            ),
            **self.status(**change),
        }

    def test_the_unit_gate_accepts_only_the_reviewed_limits(self) -> None:
        self.assert_task_accepts(TASKS, UNIT_GATE, self.unit_variables())
        rejected = {
            "no memory ceiling": {"MemoryMax": "infinity"},
            "no memory throttle": {"MemoryHigh": "infinity"},
            "a larger ceiling": {"MemoryMax": "4294967296"},
            "no run time limit": {"RuntimeMaxUSec": "infinity"},
            "another run time": {"RuntimeMaxUSec": "4h"},
            "normal priority": {"Nice": "0"},
            "best-effort I/O": {"IOSchedulingClass": "2"},
            "default CPU weight": {"CPUWeight": "100"},
            "not the first to die": {"OOMScoreAdjust": "0"},
        }
        for label, change in rejected.items():
            with self.subTest(label):
                self.assert_task_rejects(
                    TASKS,
                    UNIT_GATE,
                    self.unit_variables(**change),
                    "systemd does not enforce the reviewed limits",
                )

    def test_check_mode_skips_the_unit_verification(self) -> None:
        # --check changes nothing, so the unit still has no drop-in.
        completed = run_task_definitions(
            [self.tasks[UNIT_GATE]],
            self.unit_variables(MemoryMax="infinity"),
            check_mode=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.count("skipping: [localhost]"), 1)


class AideRoleShapeTests(unittest.TestCase):
    """Where the control sits, what it may run and what it must never do."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (PROJECT_ROOT / TASKS).read_text(encoding="utf-8")
        cls.tasks = yaml.safe_load(cls.text)
        cls.by_name = {task["name"]: task for task in cls.tasks}

    def test_the_role_applies_the_control_last(self) -> None:
        # A failure here must never block a control that already worked.
        main = yaml.safe_load(
            (PROJECT_ROOT / ROLE / "tasks/main.yml").read_text(encoding="utf-8")
        )
        imports = [task["ansible.builtin.import_tasks"] for task in main]
        self.assertEqual(imports[-1], "aide.yml")
        self.assertEqual(imports[-2], "resource-control.yml")

    def test_the_only_command_is_aide_checking_its_own_configuration(self) -> None:
        commands = [task for task in self.tasks if "ansible.builtin.command" in task]
        self.assertEqual([task["name"] for task in commands], [CONFIG_CHECK])
        command = commands[0]
        self.assertEqual(
            command["ansible.builtin.command"]["argv"],
            [
                "/usr/bin/aide",
                "--config={{ host_baseline_aide_config }}",
                "--config-check",
            ],
        )
        # A read: it never reports a change and it also runs under --check.
        self.assertIs(command["changed_when"], False)
        self.assertIs(command["check_mode"], False)

    def test_the_role_neither_initialises_nor_updates_the_database(self) -> None:
        # Renewing the reference database accepts the host as it is today: a
        # security decision for the owner (docs/OPERATIONS.md, "AIDE"). The
        # same goes for turning the timer back on. No argument of any module
        # may name them; only the failure messages explain them to a human.
        forbidden = (
            "--init",
            "--update",
            "--dry-init",
            "aideinit",
            "aide.db",
            ".timer",
            "enable",
        )
        for task in self.tasks:
            for module, arguments in task.items():
                if not module.startswith("ansible.builtin."):
                    continue
                items = (
                    arguments.items()
                    if isinstance(arguments, dict)
                    else [("", arguments)]
                )
                for key, value in items:
                    if key in ("fail_msg", "msg"):
                        continue
                    for word in forbidden:
                        with self.subTest(task=task["name"], key=key, word=word):
                            self.assertNotIn(word, str(value))

    def test_every_task_uses_a_known_module_and_the_status_read_changes_nothing(
        self,
    ) -> None:
        allowed = {
            "ansible.builtin.assert",
            "ansible.builtin.command",
            "ansible.builtin.file",
            "ansible.builtin.meta",
            "ansible.builtin.set_fact",
            "ansible.builtin.stat",
            "ansible.builtin.systemd_service",
            "ansible.builtin.template",
        }
        keywords = {
            "name",
            "when",
            "register",
            "notify",
            "changed_when",
            "check_mode",
        }
        for task in self.tasks:
            modules = set(task) - keywords
            with self.subTest(task=task["name"]):
                self.assertEqual(len(modules), 1, modules)
                self.assertLessEqual(modules, allowed)
        # With only `name` the module cannot start, stop, enable or reload.
        status = self.by_name["Read what systemd enforces on the AIDE unit"]
        self.assertEqual(
            status["ansible.builtin.systemd_service"],
            {"name": "dailyaidecheck.service"},
        )

    def test_both_files_are_installed_without_when_and_not_executable(self) -> None:
        # Without `when`, so that `--check --diff` shows the change. A file of
        # aide.conf.d with the x bit is executed and its output is taken as
        # configuration (README.Debian), so these must never be executable.
        for name in (EXCLUDES_INSTALL, LIMITS_INSTALL):
            with self.subTest(name):
                task = self.by_name[name]
                self.assertNotIn("when", task)
                template = task["ansible.builtin.template"]
                self.assertEqual(
                    (template["owner"], template["group"], template["mode"]),
                    ("root", "root", "0644"),
                )
        self.assertEqual(
            self.by_name[LIMITS_INSTALL]["notify"], "Host baseline reload systemd"
        )
        self.assertNotIn("notify", self.by_name[EXCLUDES_INSTALL])

    def test_the_rules_file_is_read_by_debian_and_sorts_before_the_root_rule(
        self,
    ) -> None:
        defaults = yaml.safe_load(
            (PROJECT_ROOT / ROLE / "defaults/main.yml").read_text(encoding="utf-8")
        )
        name = Path(
            defaults["host_baseline_aide_rules_file"].replace(
                "{{ host_baseline_aide_rules_dir }}", "/etc/aide/aide.conf.d"
            )
        ).name
        # aide.conf: @@x_include /etc/aide/aide.conf.d ^[a-zA-Z0-9_-]+$
        self.assertRegex(name, r"^[a-zA-Z0-9_-]+$")
        # The root rule `/ 0 Full` of Debian is 99_aide_root: an exclusion
        # must be read before it.
        self.assertLess(name, "99_aide_root")
        self.assertEqual(name, "05_dockerswarm_excludes")

    def test_the_steps_run_in_a_safe_order(self) -> None:
        names = [task["name"] for task in self.tasks]
        order = [
            BOUNDS_GATE,
            INSTALLED_GATE,
            CANDIDATE_GATE,
            EXCLUDES_INSTALL,
            CONFIG_CHECK,
            LIMITS_INSTALL,
            "Apply the AIDE unit changes before verification",
            UNIT_GATE,
        ]
        positions = [names.index(name) for name in order]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(
            names.index("Read the AIDE rules directory"), names.index(INSTALLED_GATE)
        )

    def test_the_sensitive_path_guard_covers_the_control(self) -> None:
        guard = (
            PROJECT_ROOT / ".github/workflows/guard-sensitive-paths.yml"
        ).read_text(encoding="utf-8")
        lines = guard.split("<<'PATTERNS'\n", 1)[1].splitlines()
        patterns = [
            re.compile(line.strip())
            for line in lines[: [line.strip() for line in lines].index("PATTERNS")]
        ]
        for path in (
            TASKS,
            f"{ROLE}/templates/aide-dockerswarm-excludes.j2",
            f"{ROLE}/templates/aide-service-limits.conf.j2",
            f"{ROLE}/defaults/main.yml",
            "scripts/validate-host-security.py",
            "config/host-security.yml",
        ):
            with self.subTest(path):
                self.assertTrue(any(pattern.search(path) for pattern in patterns))


class AideDocsTests(unittest.TestCase):
    """The runbook says what the code does, with the numbers of the contract."""

    @staticmethod
    def read(path: str) -> str:
        return (PROJECT_ROOT / path).read_text(encoding="utf-8")

    @staticmethod
    def flat(text: str) -> str:
        return " ".join(text.split())

    def section(self, heading: str) -> str:
        text = self.read("docs/OPERATIONS.md")
        start = text.index(heading + "\n")
        following = re.search(r"^#{2,3} ", text[start + len(heading) :], re.MULTILINE)
        end = start + len(heading) + following.start() if following else len(text)
        return self.flat(text[start:end])

    def test_the_runbook_states_the_reviewed_values_and_the_steps(self) -> None:
        aide = self.section("### AIDE")
        for expected in (
            f"MemoryHigh={LIVE['host_security_aide_memory_high']}",
            f"MemoryMax={LIVE['host_security_aide_memory_max']}",
            f"RuntimeMaxSec={LIVE['host_security_aide_runtime_max']}",
            "05_dockerswarm_excludes",
            "--config-check",
            "-p d:/var/lib/docker",
            "sudo systemctl start dailyaidecheck.service",
            "/var/lib/aide/aide.db.new",
            "sudo systemctl enable --now dailyaidecheck.timer",
            "COPYNEWDB",
            "354 000",
        ):
            with self.subTest(expected):
                self.assertIn(expected, aide)

    def test_the_runbook_promotes_the_database_only_after_reading_the_report(
        self,
    ) -> None:
        aide = self.section("### AIDE")
        first_run = aide.index("sudo systemctl start dailyaidecheck.service")
        report = aide.index("/var/log/aide/aide.log")
        promote = aide.index("aide.db.new /var/lib/aide/aide.db")
        enable = aide.index("sudo systemctl enable --now dailyaidecheck.timer")
        self.assertLess(first_run, report)
        self.assertLess(report, promote)
        self.assertLess(promote, enable)

    def test_the_other_documents_record_the_fix_and_what_stays_manual(self) -> None:
        expectations = {
            "docs/KNOWN_ISSUES.md": ("## AIDE consume 4 GiB y 2 h de CPU cada noche",),
            "docs/DEPLOYMENT_STATUS.md": (
                "### AIDE",
                "sudo systemctl disable dailyaidecheck.timer",
                "sudo systemctl enable --now dailyaidecheck.timer",
            ),
            "CHANGELOG.md": ("### AIDE acotado y recortado",),
            f"{ROLE}/README.md": ("### AIDE, trimmed and bounded",),
        }
        for path, needles in expectations.items():
            text = self.flat(self.read(path))
            for needle in needles:
                with self.subTest(path=path, needle=needle):
                    self.assertIn(self.flat(needle), text)


if __name__ == "__main__":
    unittest.main()
