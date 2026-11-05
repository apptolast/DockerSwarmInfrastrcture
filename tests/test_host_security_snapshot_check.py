"""A --check of host-baseline across a snapshot promotion.

A --check run does not write the APT snapshot pin, so APT keeps resolving
against the installed snapshot. On 2026-11-05 the first --check after
promoting 20261104T000000Z stopped at the bootstrap install with
`no available installation candidate for ca-certificates=20260816...`. The
role now simulates every package lock against the promoted snapshot in a
throwaway APT state instead. The gates run through the reviewed-task harness
against synthetic results, never against the host.
"""

from __future__ import annotations

import unittest
from typing import Any

from ansible_task_harness import run_task_definitions
from test_host_bootstrap_contract import load_tasks

TASKS = "ansible/roles/host_security/tasks/main.yml"
PIN = "Pin the reviewed Ubuntu archive snapshot"
PENDING = "Detect a promoted snapshot that only an apply adopts"
SIMULATE = "Simulate the package locks against the promoted snapshot"
REQUIRE = "Require the promoted snapshot to resolve every package lock"
BOOTSTRAP = "Install repository bootstrap dependencies"
INSTALL = "Install the reviewed host-security packages"
ENFORCE = "Enforce every locked host-security package version"
OPENSSH = "1:10.2p1-2ubuntu3.8"
UPGRADE = (
    "Inst openssh-server [1:10.2p1-2ubuntu3.6] (1:10.2p1-2ubuntu3.8 "
    "Ubuntu:26.04/resolute-updates, Ubuntu:26.04/resolute-security [amd64]) []"
)


def simulation(lines: list[str], rc: int = 0) -> dict[str, Any]:
    return {"rc": rc, "stdout_lines": lines, "stderr": ""}


def installed(version: str) -> dict[str, Any]:
    return {
        "results": [
            {"item": f"openssh-server={OPENSSH}", "stdout": version},
        ]
    }


class SnapshotCheckTests(unittest.TestCase):
    tasks: dict[str, dict[str, Any]]
    order: list[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.tasks = load_tasks(TASKS)
        cls.order = list(cls.tasks)

    def run_gate(
        self, name: str, variables: dict[str, Any], check_mode: bool = False
    ) -> tuple[int, str]:
        completed = run_task_definitions(
            [self.tasks[name]], variables, check_mode=check_mode
        )
        return completed.returncode, completed.stdout + completed.stderr

    def pending(self, changed: bool, check_mode: bool) -> bool:
        probe = {
            "name": "Report the pending flag",
            "ansible.builtin.assert": {
                "that": ["host_security_snapshot_pending | bool"],
                "quiet": True,
            },
        }
        completed = run_task_definitions(
            [self.tasks[PENDING], probe],
            {"host_security_snapshot_pin": {"changed": changed}},
            check_mode=check_mode,
        )
        return completed.returncode == 0

    def test_only_a_check_with_a_moved_pin_is_pending(self) -> None:
        self.assertTrue(self.pending(changed=True, check_mode=True))
        self.assertFalse(self.pending(changed=False, check_mode=True))
        self.assertFalse(self.pending(changed=True, check_mode=False))
        self.assertFalse(self.pending(changed=False, check_mode=False))

    def test_the_simulation_runs_before_any_install_and_only_when_pending(
        self,
    ) -> None:
        self.assertEqual(
            self.tasks[PIN]["register"], "host_security_snapshot_pin"
        )
        positions = [
            self.order.index(name)
            for name in (PIN, PENDING, SIMULATE, REQUIRE, BOOTSTRAP, INSTALL, ENFORCE)
        ]
        self.assertEqual(positions, sorted(positions))
        for name in (SIMULATE, REQUIRE):
            self.assertEqual(self.tasks[name]["when"], "host_security_snapshot_pending")
        self.assertEqual(
            self.tasks[BOOTSTRAP]["when"], "not host_security_snapshot_pending"
        )

    def test_the_simulation_never_touches_the_host_apt_state(self) -> None:
        task = self.tasks[SIMULATE]
        self.assertIs(task["check_mode"], False)
        self.assertIs(task["changed_when"], False)
        self.assertIs(task["failed_when"], False)
        script = task["vars"]["host_security_snapshot_simulation_script"]
        for fragment in (
            "set -euo pipefail",
            "mktemp -d /var/tmp/dockerswarm-snapshot-check.",
            "trap 'rm -rf -- \"$state\"' EXIT",
            '-o "APT::Snapshot=$snapshot"',
            '-o "Dir::State::Lists=$state/lists"',
            '-o "Dir::Cache=$state/cache"',
            "-o APT::Update::Error-Mode=any",
            '"${apt[@]}" -s install --no-install-recommends "$@"',
        ):
            self.assertIn(fragment, script)
        # /tmp is RAM on this host; the lists go to disk.
        self.assertNotIn(" /tmp", script)
        argv = task["ansible.builtin.command"]["argv"]
        self.assertIn("host_security_ubuntu_snapshot", argv)
        self.assertIn("host_security_repository_bootstrap_package_lock", argv)
        self.assertIn("host_security_package_lock", argv)

    def test_the_promoted_snapshot_must_resolve_without_removals(self) -> None:
        cases = {
            "resolves": (simulation([UPGRADE]), 0),
            "fails": (simulation([], rc=100), 2),
            "removes": (simulation([UPGRADE, "Remv openssh-sftp-server [1]"]), 2),
        }
        for label, (result, expected) in cases.items():
            with self.subTest(label):
                rc, output = self.run_gate(
                    REQUIRE,
                    {
                        "host_security_snapshot_pending": True,
                        "host_security_snapshot_simulation": result,
                    },
                )
                self.assertEqual(rc, expected, output)

    def test_an_old_version_passes_only_on_its_exact_simulated_upgrade(
        self,
    ) -> None:
        cases = {
            "already at the lock": (OPENSSH, False, [], 0),
            "pending upgrade to the lock": ("1:10.2p1-2ubuntu3.6", True, [UPGRADE], 0),
            "pending without that upgrade": ("1:10.2p1-2ubuntu3.6", True, [], 2),
            "pending from another version": (
                "1:10.2p1-2ubuntu3.5",
                True,
                [UPGRADE],
                2,
            ),
            "pending to another version": (
                "1:10.2p1-2ubuntu3.6",
                True,
                [UPGRADE.replace("ubuntu3.8 ", "ubuntu3.9 ")],
                2,
            ),
            "old version in an apply": ("1:10.2p1-2ubuntu3.6", False, [UPGRADE], 2),
        }
        for label, (version, pending, lines, expected) in cases.items():
            with self.subTest(label):
                rc, output = self.run_gate(
                    ENFORCE,
                    {
                        "host_security_snapshot_pending": pending,
                        "host_security_snapshot_simulation": (
                            simulation(lines) if pending else {"skipped": True}
                        ),
                        "host_security_installed_package_versions": installed(
                            version
                        ),
                    },
                )
                self.assertEqual(rc, expected, output)


if __name__ == "__main__":
    unittest.main()
