"""Contract of the memory bounds host_baseline sets for the operator's sessions.

On 2026-10-13 the interactive sessions of `admin` reached 7.4 GiB of the 16 GiB
of a host without swap, 3.7 GiB of it files in /tmp (a tmpfs is RAM). The page
cache of Traefik thrashed, its 5 s healthcheck failed three times in a row and
Swarm replaced it every few minutes: every site was down. These tests cover the
validator rules, the role's gates (run through the reviewed-task harness
against synthetic reads, never against the host), the two rendered drop-ins and
the runbook.
"""

from __future__ import annotations

import base64
import copy
import json
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
TASKS = f"{ROLE}/tasks/resource-control.yml"
SLICE_TEMPLATE = PROJECT_ROOT / ROLE / "templates/operator-slice-memory.conf.j2"
TMP_TEMPLATE = PROJECT_ROOT / ROLE / "templates/tmp-mount-size.conf.j2"
CONTRACT = PROJECT_ROOT / "config/host-security.yml"

BOUNDS_GATE = "Verify the operator memory bounds are whole M or G sizes"
MEASURE = "Measure the memory the operator slice cannot give back"
REFUSE = "Refuse a memory ceiling the operator slice already approaches"
TMP_GATE = "Verify the /tmp drop-in only changes the size of the packaged mount"
SLICE_GATE = "Verify the operator slice candidate"
KERNEL_GATE = "Verify the kernel enforces the reviewed ceilings"
SLICE_INSTALL = "Install the reviewed memory ceiling of the operator slice"
TMP_INSTALL = "Install the reviewed size of the /tmp tmpfs"
OVERRIDES = "Remove live-only memory overrides that would shadow the reviewed ceiling"

GIB = 1024**3
BOUNDS = {
    "host_baseline_operator_slice_memory_high": "4G",
    "host_baseline_operator_slice_memory_max": "5G",
    "host_baseline_tmp_size": "2G",
}
# The Options= line of /usr/lib/systemd/system/tmp.mount on the host
# (systemd 259.5): `%%` is the systemd specifier for a literal percent.
PACKAGED_OPTIONS = (
    "Options=mode=1777,strictatime,nosuid,nodev,size=50%%,nr_inodes=1m,"
    "x-systemd.graceful-option=usrquota"
)
PACKAGED_UNIT = (
    "[Unit]\nDescription=Temporary Directory /tmp\n\n[Mount]\nWhat=tmpfs\n"
    f"Where=/tmp\nType=tmpfs\n{PACKAGED_OPTIONS}\n"
)


def encoded(contents: str) -> dict[str, str]:
    """A registered slurp result."""
    return {"content": base64.b64encode(contents.encode("utf-8")).decode("ascii")}


def render(template: Path, **overrides: str) -> str:
    return (
        Environment(
            autoescape=False,
            keep_trailing_newline=True,
            undefined=StrictUndefined,
        )
        .from_string(template.read_text(encoding="utf-8"))
        .render(**{**BOUNDS, **overrides})
    )


def memory_stat(anon: int, shmem: int) -> dict[str, str]:
    """A memory.stat with the decoys a real one has: anon_thp and shmem_thp."""
    return encoded(
        "\n".join(
            [
                f"anon {anon}",
                "file 3000000000",
                "kernel 123456",
                f"shmem {shmem}",
                "file_mapped 2",
                "anon_thp 777777777",
                "shmem_thp 888888888",
                "file_thp 0",
                "",
            ]
        )
    )


class OperatorMemoryValidatorTests(unittest.TestCase):
    """scripts/validate-host-security.py rejects every unsafe bound."""

    NOW = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
    HIGH = "host_security_operator_slice_memory_high"
    MAX = "host_security_operator_slice_memory_max"
    TMP = "host_security_tmp_size"

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

    def test_the_reviewed_bounds_are_the_live_ones(self) -> None:
        self.assertEqual(self.contract[self.HIGH], "4G")
        self.assertEqual(self.contract[self.MAX], "5G")
        self.assertEqual(self.contract[self.TMP], "2G")
        host_security_validator.validate(self.contract, self.NOW)

    def test_every_bound_is_required_and_a_whole_m_or_g_size(self) -> None:
        for key in (self.HIGH, self.MAX, self.TMP):
            for label, value in (
                ("missing", None),
                ("integer", 4096),
                ("list", ["4G"]),
                ("with a B", "4GB"),
                ("lower case", "4g"),
                ("zero", "0G"),
                ("leading zero", "04G"),
                ("fraction", "1.5G"),
                ("percentage", "50%"),
                ("infinity", "infinity"),
                ("bare unit", "G"),
                ("empty", ""),
                ("bytes", "2147483648"),
                ("too many digits", "10000M"),
            ):
                with self.subTest(key=key, value=label):
                    self.assert_rejected(
                        {key: value}, f"{key} must be a whole number of M or G"
                    )

    def test_memory_high_stays_below_memory_max(self) -> None:
        self.assert_rejected({self.HIGH: "5G"}, "must stay below")
        self.assert_rejected({self.HIGH: "6G"}, "must stay below")
        self.assert_accepted({self.HIGH: "5119M"})

    def test_the_ceiling_would_have_prevented_the_2026_10_13_outage(self) -> None:
        # The outage began with the slice at 7.4 GiB.
        self.assert_rejected({self.MAX: "7G"}, "must not exceed 6G")
        self.assert_rejected({self.MAX: "6145M"}, "must not exceed 6G")
        self.assert_accepted({self.MAX: "6G", self.TMP: "3G"})

    def test_memory_high_cannot_throttle_every_session_at_start(self) -> None:
        self.assert_rejected({self.HIGH: "1023M"}, "must be at least 1G")
        self.assert_accepted({self.HIGH: "1G", self.MAX: "2G", self.TMP: "1G"})

    def test_tmp_leaves_the_processes_half_of_the_ceiling(self) -> None:
        # tmpfs pages are charged to the cgroup that wrote them and cannot be
        # reclaimed without swap: a /tmp that could fill the slice would leave
        # the OOM killer ending every process while the memory stays used.
        self.assert_rejected({self.TMP: "2561M"}, "at least half")
        self.assert_rejected({self.TMP: "3G"}, "at least half")
        self.assert_accepted({self.TMP: "2560M"})


class OperatorMemoryRoleTests(AnsibleTaskAssertions, unittest.TestCase):
    """The role's gates, run against synthetic reads."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tasks = load_tasks(TASKS)

    def slice_reads(self, anon: int, shmem: int) -> dict[str, Any]:
        return {**BOUNDS, "host_baseline_operator_slice_stat": memory_stat(anon, shmem)}

    def run_refusal(self, anon: int, shmem: int, check_mode: bool = False):
        return run_task_definitions(
            [self.tasks[MEASURE], self.tasks[REFUSE]],
            self.slice_reads(anon, shmem),
            check_mode=check_mode,
        )

    # -- the bounds themselves ----------------------------------------------

    def test_the_bounds_gate_accepts_the_reviewed_values(self) -> None:
        self.assert_task_accepts(TASKS, BOUNDS_GATE, BOUNDS)

    def test_the_bounds_gate_accepts_the_edges_of_the_validator(self) -> None:
        for high, maximum, tmp in (
            ("1G", "6G", "3G"),
            ("1024M", "6144M", "3072M"),
            ("5119M", "5G", "2560M"),
        ):
            with self.subTest(high=high, maximum=maximum, tmp=tmp):
                self.assert_task_accepts(
                    TASKS,
                    BOUNDS_GATE,
                    {
                        "host_baseline_operator_slice_memory_high": high,
                        "host_baseline_operator_slice_memory_max": maximum,
                        "host_baseline_tmp_size": tmp,
                    },
                )

    def test_the_bounds_gate_repeats_every_validator_rule(self) -> None:
        # deploy-ansible.sh never runs the validator, so the role must refuse
        # what the validator refuses.
        rejected = {
            "high equals max": ("5G", "5G", "2G"),
            "high above max": ("6G", "5G", "2G"),
            "max above 6G": ("4G", "7G", "2G"),
            "max just above 6G": ("4G", "6145M", "2G"),
            "high below 1G": ("512M", "5G", "2G"),
            "tmp above half of max": ("4G", "5G", "2561M"),
            "a B suffix": ("4GB", "5G", "2G"),
            "lower case": ("4g", "5G", "2G"),
            "zero": ("0G", "5G", "2G"),
            "a fraction": ("4G", "5.5G", "2G"),
            "a percentage": ("4G", "5G", "50%"),
            "infinity": ("4G", "infinity", "2G"),
            "empty": ("4G", "5G", ""),
        }
        for label, (high, maximum, tmp) in rejected.items():
            with self.subTest(label):
                self.assert_task_rejects(
                    TASKS,
                    BOUNDS_GATE,
                    {
                        "host_baseline_operator_slice_memory_high": high,
                        "host_baseline_operator_slice_memory_max": maximum,
                        "host_baseline_tmp_size": tmp,
                    },
                    "must be whole numbers of M or G",
                )

    # -- never end a live session -------------------------------------------

    def test_only_anon_and_shmem_count_as_unreclaimable(self) -> None:
        check = {
            "name": "Check the measured sum",
            "ansible.builtin.assert": {
                "that": [
                    "host_baseline_operator_slice_unreclaimable | int"
                    f" == {3 * GIB + 1 * GIB}"
                ]
            },
        }
        completed = run_task_definitions(
            [self.tasks[MEASURE], check], self.slice_reads(3 * GIB, 1 * GIB)
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_the_refusal_gate_accepts_a_slice_well_below_the_ceiling(self) -> None:
        completed = self.run_refusal(2 * GIB, 1 * GIB)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_the_refusal_gate_stops_at_exactly_ninety_percent(self) -> None:
        ceiling = 5 * GIB
        # 90 % of 5G is 4 831 838 208 bytes: one byte less passes, that many
        # fails, so applying never leaves less than a tenth for the kernel.
        edge = ceiling * 9 // 10
        self.assertEqual(edge * 10, ceiling * 9)
        passing = self.run_refusal(edge - 1, 0)
        self.assertEqual(passing.returncode, 0, passing.stdout + passing.stderr)
        failing = self.run_refusal(edge, 0)
        self.assertNotEqual(failing.returncode, 0, failing.stdout)
        # Not "nothing was changed": the tasks before this one already ran.
        self.assertIn("this control changed nothing", failing.stdout + failing.stderr)

    def test_the_refusal_gate_would_have_stopped_the_session_state_of_today(
        self,
    ) -> None:
        # 3 374 MiB of processes and 1 445 MiB of /tmp, measured the night of
        # the outage: a 5G MemoryMax would have made the OOM killer end live
        # sessions, so the apply must refuse and say what to do.
        failing = self.run_refusal(3374 * 1024**2, 1445 * 1024**2)
        output = failing.stdout + failing.stderr
        self.assertNotEqual(failing.returncode, 0, output)
        for expected in (
            "cannot reclaim",
            "this control changed nothing",
            "marker is retained after a failed apply",
            "Recuperar un marker abandonado",
            "move scratch files from /tmp to disk",
        ):
            self.assertIn(expected, output)

    def test_the_refusal_gate_also_runs_under_check_mode(self) -> None:
        # It only reads, and a --check that said "converges" would be a lie.
        failing = self.run_refusal(5 * GIB, 0, check_mode=True)
        self.assertNotEqual(failing.returncode, 0, failing.stdout)

    # -- the rendered drop-ins ----------------------------------------------

    def test_the_slice_drop_in_is_two_plain_settings(self) -> None:
        rendered = render(SLICE_TEMPLATE)
        settings = [line for line in rendered.splitlines() if not line.startswith("#")]
        self.assertEqual(settings, ["[Slice]", "MemoryHigh=4G", "MemoryMax=5G"])
        self.assertTrue(rendered.endswith("\n"))

    def test_the_slice_gate_accepts_only_the_reviewed_drop_in(self) -> None:
        reviewed = render(SLICE_TEMPLATE)
        self.assert_task_accepts(
            TASKS,
            SLICE_GATE,
            {**BOUNDS, "host_baseline_operator_slice_candidate": reviewed},
        )
        rejected = {
            "extra setting": reviewed + "MemorySwapMax=0\n",
            "missing MemoryMax": reviewed.replace("MemoryMax=5G\n", ""),
            "another ceiling": reviewed.replace("MemoryMax=5G", "MemoryMax=7G"),
            "another section": reviewed.replace("[Slice]", "[Service]"),
            "no section": reviewed.replace("[Slice]\n", ""),
        }
        for label, candidate in rejected.items():
            with self.subTest(label):
                self.assert_task_rejects(
                    TASKS,
                    SLICE_GATE,
                    {**BOUNDS, "host_baseline_operator_slice_candidate": candidate},
                    "operator slice drop-in is not exactly",
                )

    def test_the_tmp_drop_in_repeats_every_packaged_option(self) -> None:
        rendered = render(TMP_TEMPLATE)
        settings = [line for line in rendered.splitlines() if not line.startswith("#")]
        self.assertEqual(
            settings,
            [
                "[Mount]",
                "Options=mode=1777,strictatime,nosuid,nodev,size=2G,"
                "nr_inodes=1m,x-systemd.graceful-option=usrquota",
            ],
        )

    def test_the_tmp_gate_accepts_only_a_size_change_of_the_packaged_mount(
        self,
    ) -> None:
        variables = {
            **BOUNDS,
            # Ansible resolves fail_msg even when the conditions hold.
            "host_baseline_tmp_packaged_unit": "/usr/lib/systemd/system/tmp.mount",
            "host_baseline_tmp_packaged": encoded(PACKAGED_UNIT),
            "host_baseline_tmp_candidate": render(TMP_TEMPLATE),
        }
        self.assert_task_accepts(TASKS, TMP_GATE, variables)
        rejected = {
            "the package adds an option": {
                "host_baseline_tmp_packaged": encoded(
                    PACKAGED_UNIT.replace("nosuid,", "nosuid,noexec,")
                )
            },
            "the package drops an option": {
                "host_baseline_tmp_packaged": encoded(
                    PACKAGED_UNIT.replace("nodev,", "")
                )
            },
            "the package changes the mode": {
                "host_baseline_tmp_packaged": encoded(
                    PACKAGED_UNIT.replace("mode=1777", "mode=0755")
                )
            },
            "the drop-in loses an option": {
                "host_baseline_tmp_candidate": render(TMP_TEMPLATE).replace(
                    "nosuid,", ""
                )
            },
            "the drop-in sets another size": {
                "host_baseline_tmp_candidate": render(TMP_TEMPLATE).replace(
                    "size=2G", "size=7G"
                )
            },
            "the drop-in adds a setting": {
                "host_baseline_tmp_candidate": render(TMP_TEMPLATE) + "Where=/var/tmp\n"
            },
            "the drop-in has no section": {
                "host_baseline_tmp_candidate": render(TMP_TEMPLATE).replace(
                    "[Mount]\n", ""
                )
            },
        }
        for label, change in rejected.items():
            with self.subTest(label):
                self.assert_task_rejects(
                    TASKS,
                    TMP_GATE,
                    {**variables, **change},
                    "pin an outdated copy",
                )

    # -- what the kernel really enforces ------------------------------------

    @staticmethod
    def kernel(high: str, maximum: str) -> dict[str, Any]:
        return {
            "host_baseline_operator_slice_kernel": {
                "results": [encoded(f"{high}\n"), encoded(f"{maximum}\n")]
            }
        }

    def test_the_kernel_gate_accepts_only_the_reviewed_ceilings(self) -> None:
        self.assert_task_accepts(
            TASKS, KERNEL_GATE, {**BOUNDS, **self.kernel("4294967296", "5368709120")}
        )
        rejected = {
            "no ceiling at all": ("max", "max"),
            "a live-only override still wins": ("6442450944", "7516192768"),
            "only MemoryHigh applied": ("4294967296", "max"),
            "only MemoryMax applied": ("max", "5368709120"),
            "the values swapped": ("5368709120", "4294967296"),
        }
        for label, (high, maximum) in rejected.items():
            with self.subTest(label):
                self.assert_task_rejects(
                    TASKS,
                    KERNEL_GATE,
                    {**BOUNDS, **self.kernel(high, maximum)},
                    "does not enforce the reviewed memory.high and memory.max",
                )

    def test_check_mode_skips_the_kernel_verification(self) -> None:
        # --check changes nothing, so the kernel still has the old ceilings.
        completed = run_task_definitions(
            [self.tasks[KERNEL_GATE]],
            {**BOUNDS, **self.kernel("max", "max")},
            check_mode=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.count("skipping: [localhost]"), 1)

    # -- expressions no gate runs -------------------------------------------

    def test_the_tmp_report_fires_only_while_the_size_is_not_applied(self) -> None:
        # The debug task cannot run in the harness, so its condition is
        # exercised as an assert: it holds exactly when the task would run.
        report = self.tasks["Report a /tmp that still has the packaged size"]
        condition = {
            "name": "The report condition holds",
            "ansible.builtin.assert": {"that": report["when"]},
        }
        packaged = "rw,nosuid,nodev,nr_inodes=1048576,inode64,usrquota\n"
        applied = "rw,nosuid,nodev,size=2097152k,nr_inodes=1048576,inode64,usrquota\n"
        other = "rw,nosuid,nodev,size=4194304k,nr_inodes=1048576,inode64,usrquota\n"
        for label, options, reported in (
            ("the packaged size", packaged, True),
            ("the reviewed size", applied, False),
            ("another size", other, True),
        ):
            with self.subTest(label):
                completed = run_task_definitions(
                    [condition],
                    {**BOUNDS, "host_baseline_tmp_live_options": {"stdout": options}},
                )
                self.assertEqual(completed.returncode == 0, reported, completed.stdout)

    def test_the_override_loop_names_the_four_set_property_files(self) -> None:
        defaults = yaml.safe_load(
            (PROJECT_ROOT / ROLE / "defaults/main.yml").read_text(encoding="utf-8")
        )
        expected = sorted(
            f"{directory}/50-{name}.conf"
            for directory in (
                "/run/systemd/system.control/user-1001.slice.d",
                "/etc/systemd/system.control/user-1001.slice.d",
            )
            for name in ("MemoryHigh", "MemoryMax")
        )
        expand = {
            "name": "Expand the override loop",
            "ansible.builtin.set_fact": {"expanded": self.tasks[OVERRIDES]["loop"]},
        }
        check = {
            "name": "Check the expanded paths",
            "ansible.builtin.assert": {
                "that": [
                    "expanded | length == 4",
                    "expanded | map('join', '/50-') "
                    "| map('regex_replace', '$', '.conf') | sort | list == "
                    + json.dumps(expected),
                ]
            },
        }
        completed = run_task_definitions(
            [expand, check],
            {
                "host_baseline_admin_uid": 1001,
                "host_baseline_operator_slice_override_dirs": defaults[
                    "host_baseline_operator_slice_override_dirs"
                ],
                "host_baseline_operator_slice_override_names": defaults[
                    "host_baseline_operator_slice_override_names"
                ],
            },
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        # The label keeps the path readable and secret-free in the apply log.
        label = self.tasks[OVERRIDES]["loop_control"]["label"]
        self.assertEqual(label, "{{ item.0 }}/50-{{ item.1 }}.conf")


class OperatorMemoryRoleShapeTests(unittest.TestCase):
    """Where the control sits, what it may run and what it must never do."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (PROJECT_ROOT / TASKS).read_text(encoding="utf-8")
        cls.tasks = yaml.safe_load(cls.text)
        cls.by_name = {task["name"]: task for task in cls.tasks}

    def test_the_role_applies_the_control_after_every_older_one(self) -> None:
        # A failure here must never block a control that already worked: it
        # comes after crowdsec-docker.yml, the last control before it. Later
        # controls (AIDE) go after it for the same reason.
        main = yaml.safe_load(
            (PROJECT_ROOT / ROLE / "tasks/main.yml").read_text(encoding="utf-8")
        )
        imports = [task["ansible.builtin.import_tasks"] for task in main]
        self.assertEqual(
            imports.index("resource-control.yml"),
            imports.index("crowdsec-docker.yml") + 1,
        )
        self.assertEqual(imports[-1], "aide.yml")

    def test_the_only_command_the_role_runs_is_a_read(self) -> None:
        commands = [
            task["ansible.builtin.command"]["argv"]
            for task in self.tasks
            if "ansible.builtin.command" in task
        ]
        self.assertEqual(
            commands,
            [
                [
                    "/usr/bin/findmnt",
                    "--noheadings",
                    "--output",
                    "OPTIONS",
                    "--mountpoint",
                    "/tmp",
                ]
            ],
        )
        for task in self.tasks:
            for module in (
                "ansible.builtin.shell",
                "ansible.builtin.raw",
                "ansible.builtin.script",
            ):
                self.assertNotIn(module, task)

    def test_the_role_never_ends_a_process_or_removes_user_files(self) -> None:
        # Every task uses one of these modules. None of them can end a process
        # (no service, systemd_service, command except the findmnt read, shell,
        # raw or script) and `file` may only remove the set-property overrides.
        allowed = {
            "ansible.builtin.assert",
            "ansible.builtin.command",
            "ansible.builtin.debug",
            "ansible.builtin.file",
            "ansible.builtin.meta",
            "ansible.builtin.set_fact",
            "ansible.builtin.slurp",
            "ansible.builtin.template",
        }
        keywords = {
            "name",
            "when",
            "register",
            "loop",
            "loop_control",
            "notify",
            "vars",
            "changed_when",
            "check_mode",
            "no_log",
        }
        for task in self.tasks:
            modules = set(task) - keywords
            with self.subTest(task=task["name"]):
                self.assertEqual(len(modules), 1, modules)
                self.assertLessEqual(modules, allowed)
        removals = [
            task
            for task in self.tasks
            if "ansible.builtin.file" in task
            and task["ansible.builtin.file"].get("state") == "absent"
        ]
        self.assertEqual([task["name"] for task in removals], [OVERRIDES])

    def test_the_overrides_removed_are_exactly_the_set_property_files(self) -> None:
        defaults = yaml.safe_load(
            (PROJECT_ROOT / ROLE / "defaults/main.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            defaults["host_baseline_operator_slice_override_dirs"],
            [
                "/run/systemd/system.control/user-{{ host_baseline_admin_uid }}"
                ".slice.d",
                "/etc/systemd/system.control/user-{{ host_baseline_admin_uid }}"
                ".slice.d",
            ],
        )
        self.assertEqual(
            defaults["host_baseline_operator_slice_override_names"],
            ["MemoryHigh", "MemoryMax"],
        )
        self.assertEqual(
            self.by_name[OVERRIDES]["ansible.builtin.file"]["path"],
            "{{ item.0 }}/50-{{ item.1 }}.conf",
        )

    def test_both_drop_ins_are_installed_without_when_and_reload_systemd(self) -> None:
        handlers = yaml.safe_load(
            (PROJECT_ROOT / ROLE / "handlers/main.yml").read_text(encoding="utf-8")
        )
        reload_topics = [
            handler.get("listen")
            for handler in handlers
            if handler.get("ansible.builtin.systemd_service", {}).get("daemon_reload")
        ]
        self.assertEqual(reload_topics, ["Host baseline reload systemd"])
        for name in (SLICE_INSTALL, TMP_INSTALL, OVERRIDES):
            with self.subTest(name):
                task = self.by_name[name]
                # Without `when`, so that `--check --diff` shows the change.
                self.assertNotIn("when", task)
                self.assertEqual(task["notify"], "Host baseline reload systemd")
        for name in (SLICE_INSTALL, TMP_INSTALL):
            template = self.by_name[name]["ansible.builtin.template"]
            self.assertEqual(
                (template["owner"], template["group"], template["mode"]),
                ("root", "root", "0644"),
            )

    def test_the_handlers_run_before_anything_is_verified(self) -> None:
        names = [task["name"] for task in self.tasks]
        flush = names.index("Apply the unit changes before verification")
        self.assertEqual(self.tasks[flush]["ansible.builtin.meta"], "flush_handlers")
        self.assertLess(names.index(SLICE_INSTALL), flush)
        self.assertLess(names.index(TMP_INSTALL), flush)
        self.assertLess(names.index(OVERRIDES), flush)
        self.assertLess(names.index(REFUSE), names.index(SLICE_INSTALL))
        self.assertLess(flush, names.index(KERNEL_GATE))

    def test_the_live_tmp_is_reported_not_remounted(self) -> None:
        report = self.by_name["Report a /tmp that still has the packaged size"]
        self.assertIn("ansible.builtin.debug", report)
        self.assertIn("size=", " ".join(report["when"]))
        # No task may remount: none runs `mount`, and the only command is a read
        # (test_the_only_command_the_role_runs_is_a_read).
        self.assertNotIn("remount", " ".join(report["when"]))

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
            f"{ROLE}/templates/operator-slice-memory.conf.j2",
            f"{ROLE}/templates/tmp-mount-size.conf.j2",
            f"{ROLE}/handlers/main.yml",
            f"{ROLE}/defaults/main.yml",
            "scripts/validate-host-security.py",
            "config/host-security.yml",
        ):
            with self.subTest(path):
                self.assertTrue(any(pattern.search(path) for pattern in patterns))


class OperatorMemoryDocsTests(unittest.TestCase):
    """The runbook says what the code does, with the numbers of the contract."""

    @staticmethod
    def read(path: str) -> str:
        return (PROJECT_ROOT / path).read_text(encoding="utf-8")

    @staticmethod
    def flat(text: str) -> str:
        """The text with every run of whitespace, wrapped lines included, as one."""
        return " ".join(text.split())

    @staticmethod
    def section(text: str, heading: str) -> str:
        start = text.index(heading + "\n")
        following = re.search(r"^## ", text[start + len(heading) :], re.MULTILINE)
        end = start + len(heading) + following.start() if following else len(text)
        return text[start:end]

    def test_the_runbook_states_the_reviewed_values(self) -> None:
        contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
        runbook = self.flat(
            self.section(
                self.read("docs/OPERATIONS.md"), "## Memoria de las sesiones de IA"
            )
        )
        high = contract["host_security_operator_slice_memory_high"]
        maximum = contract["host_security_operator_slice_memory_max"]
        tmp = contract["host_security_tmp_size"]
        for expected in (
            f"MemoryHigh={high}",
            f"MemoryMax={maximum}",
            f"size={tmp}",
            f"remount,size={tmp} /tmp",
            "CLAUDE_CODE_TMPDIR",
            "required_swap_mib: 0",
            "Health check exceeded timeout (5s)",
            "Riesgo aceptado",
            "corren en este mismo slice",
            "Recuperar un marker abandonado",
        ):
            with self.subTest(expected):
                self.assertIn(expected, runbook)

    def test_the_runbook_never_tells_anyone_to_cap_below_current_use(self) -> None:
        runbook = self.flat(
            self.section(
                self.read("docs/OPERATIONS.md"), "## Memoria de las sesiones de IA"
            )
        )
        self.assertIn("Nunca por debajo de lo que ya retiene", runbook)
        # The check comes before the set-property it guards.
        self.assertLess(
            runbook.index("memory.stat"), runbook.index("systemctl set-property")
        )

    def test_the_other_documents_record_the_outage_and_the_gap(self) -> None:
        expectations = {
            "docs/CAPACITY.md": ("### Sesiones del operador", "MemoryMax=5G"),
            "docs/DEPLOYMENT_STATUS.md": (
                "### Caída por memoria de las sesiones de IA (2026-10-13)",
                "systemctl set-property --runtime user-1001.slice",
                "### Memoria de las sesiones de IA",
                "### Caída por AIDE (2026-10-14)",
                "3 001 595",
                "### AIDE",
            ),
            "docs/KNOWN_ISSUES.md": (
                "## Traefik se reinicia cada pocos minutos con el host sin memoria",
                "Health check exceeded timeout (5s)",
                "## AIDE consume 4 GiB y 2 h de CPU cada noche",
                "sudo systemctl stop dailyaidecheck.service",
            ),
            "CHANGELOG.md": ("### Memoria de las sesiones de IA",),
            f"{ROLE}/README.md": ("### Memory of the operator's sessions",),
        }
        for path, needles in expectations.items():
            text = self.read(path)
            for needle in needles:
                with self.subTest(path=path, needle=needle):
                    self.assertIn(self.flat(needle), self.flat(text))

    def test_the_temporary_runtime_drift_is_recorded_as_such(self) -> None:
        status = self.read("docs/DEPLOYMENT_STATUS.md")
        temporary = self.section(status, "## Estado temporal fuera del repositorio")
        temporary = self.flat(temporary)
        self.assertIn("MemoryHigh=6G MemoryMax=7G", temporary)
        self.assertIn("tmpfs-offload", temporary)
        # The AIDE timer was stopped by hand during the 2026-10-14 outage and
        # must come back through a reviewed decision, never by forgetting it.
        self.assertIn("sudo systemctl stop dailyaidecheck.timer", temporary)
        self.assertIn("sudo systemctl disable dailyaidecheck.timer", temporary)
        self.assertIn("sudo systemctl enable --now dailyaidecheck.timer", temporary)

    def test_the_precheck_threshold_is_ninety_percent_of_the_reviewed_ceiling(
        self,
    ) -> None:
        contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
        maximum_gib = int(contract["host_security_operator_slice_memory_max"][:-1])
        self.assertTrue(
            contract["host_security_operator_slice_memory_max"].endswith("G")
        )
        threshold = f"{maximum_gib * 0.9:.1f}".replace(".", ",") + " GiB"
        runbook = self.flat(
            self.section(
                self.read("docs/OPERATIONS.md"), "## Memoria de las sesiones de IA"
            )
        )
        self.assertIn(f"Debe ser menor que {threshold}", runbook)
        # The very measure the role gates on, in the command the owner runs.
        self.assertIn("anon|shmem", runbook)

    def test_the_marker_advice_matches_what_deploy_ansible_does(self) -> None:
        # The runbook and the refusal message say a failed apply keeps the
        # operation marker. If the script stops doing that, so must they.
        script = self.read("scripts/deploy-ansible.sh")
        self.assertIn(
            "Ansible failed; operation marker retained for reviewed recovery",
            script,
        )
        self.assertIn(
            "\n## Recuperar un marker abandonado\n", self.read("docs/OPERATIONS.md")
        )


if __name__ == "__main__":
    unittest.main()
