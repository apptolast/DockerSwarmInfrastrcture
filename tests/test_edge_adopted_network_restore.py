"""Plain containers of an adopted edge network come back after a reboot.

At daemon start Docker may restore a Compose container of an adopted network
before Swarm has that network; the start fails with "network <name> not
found" and the restart policy never retries it, because it only applies
after a successful start. On 2026-10-18 that left monitor.apptolast.com at
502 for 17 days (docs/EDGE.md, «Arranque tras un reinicio»). These tests
cover scripts/restore-adopted-network-containers.py, its unit and the edge
role tasks that install, enable and run it.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from typing import Any

import yaml
from ansible_task_harness import REPOSITORY_ROOT
from jinja2 import Environment, StrictUndefined

SCRIPT = REPOSITORY_ROOT / "scripts/restore-adopted-network-containers.py"
DEPLOY = REPOSITORY_ROOT / "ansible/roles/edge/tasks/deploy.yml"
EDGE_DEFAULTS = REPOSITORY_ROOT / "ansible/roles/edge/defaults/main.yml"
EDGE_TEMPLATES = REPOSITORY_ROOT / "ansible/roles/edge/templates"
GROUP_VARS = REPOSITORY_ROOT / "ansible/group_vars/all.yml"
VALIDATE_EDGE = REPOSITORY_ROOT / "scripts/validate-edge.sh"
UNIT = "dockerswarm-edge-adopted-network-restore"
HELPER = "/usr/local/libexec/restore-adopted-network-containers.py"
NETWORK = "apptolast-edge-observatorio"
# The exact error Docker recorded on monitor-production-api-1 on 2026-10-18.
MISSING = (
    "failed to set up container networking: could not find a network "
    f"matching network mode {NETWORK}: network {NETWORK} not found"
)


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("restore_adopted", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


restore = load_script()


def container(
    name: str,
    *,
    status: str = "exited",
    policy: str = "unless-stopped",
    error: str = MISSING,
    swarm: bool = False,
) -> dict[str, Any]:
    labels = {"com.docker.compose.project": "monitor-production"}
    if swarm:
        labels["com.docker.swarm.task.id"] = "t1"
    return {
        "Name": "/" + name,
        "Config": {"Labels": labels},
        "State": {"Status": status, "Running": status == "running", "Error": error},
        "HostConfig": {"RestartPolicy": {"Name": policy}},
    }


def answer(code: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, "")


class FakeDocker:
    """Answers the CLI calls the script makes, from an in-memory state."""

    def __init__(
        self,
        containers: dict[str, dict[str, Any]],
        *,
        network_after: int = 0,
        failing_starts: Sequence[str] = (),
        vanished: Sequence[str] = (),
    ) -> None:
        self.containers = containers
        self.network_after = network_after
        self.failing_starts = set(failing_starts)
        self.vanished = set(vanished)
        self.calls: list[list[str]] = []

    def __call__(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        call = list(arguments)
        self.calls.append(call)
        if call[:2] == ["network", "inspect"]:
            if self.network_after > 0:
                self.network_after -= 1
                return answer(1)
            return answer(0, call[-1] + "\n")
        if call[0] == "ps":
            return answer(0, "".join(f"{key}\n" for key in self.containers))
        if call[:2] == ["container", "inspect"]:
            if call[2] in self.vanished:
                return answer(1)
            return answer(0, json.dumps([self.containers[call[2]]]))
        if call[0] == "start":
            if call[1] in self.failing_starts:
                return answer(1)
            self.containers[call[1]]["State"] = {"Status": "running", "Running": True}
            return answer(0, call[1] + "\n")
        raise AssertionError(f"unexpected docker call: {call}")

    def starts(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "start"]


def run_main(docker: FakeDocker, argv: Sequence[str]) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = restore.main(list(argv), run=docker)
    return code, stdout.getvalue(), stderr.getvalue()


class RestoreSelectionTests(unittest.TestCase):
    """Only a failed start for that very network is retried."""

    def test_the_2026_10_18_failure_is_started(self) -> None:
        docker = FakeDocker({"a1": container("monitor-production-api-1")})
        code, stdout, stderr = run_main(docker, ["--timeout", "5", NETWORK])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), {"started": ["monitor-production-api-1"]})
        self.assertEqual(docker.starts(), ["a1"])

    def test_the_listing_asks_only_for_stopped_members_of_the_network(self) -> None:
        docker = FakeDocker({})
        self.assertEqual(run_main(docker, [NETWORK])[0], 0)
        listing = next(call for call in docker.calls if call[0] == "ps")
        for expected in (
            "--all",
            f"network={NETWORK}",
            "status=exited",
            "status=created",
        ):
            self.assertIn(expected, listing)

    def test_anything_else_stays_as_it_is(self) -> None:
        docker = FakeDocker(
            {
                # Stopped on purpose: Docker records no start error.
                "s1": container("stopped-by-hand", error=""),
                # Never restarted by Docker, like the AX forwarder.
                "n1": container("ax-web-edge", policy="no"),
                "o1": container("on-failure", policy="on-failure"),
                # Swarm reschedules its own tasks.
                "w1": container("edge_traefik.1.x", swarm=True),
                "r1": container("running", status="running", error=""),
                # Another network missing is not this network's race.
                "x1": container(
                    "other-network", error="network apptolast-edge-ax not found"
                ),
                "e1": container("other-error", error="OCI runtime create failed"),
            }
        )
        code, stdout, stderr = run_main(docker, [NETWORK])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), {"started": []})
        self.assertEqual(docker.starts(), [])

    def test_always_is_retried_like_unless_stopped(self) -> None:
        docker = FakeDocker(
            {"a1": container("api", policy="always", status="created")}
        )
        self.assertEqual(run_main(docker, [NETWORK])[0], 0)
        self.assertEqual(docker.starts(), ["a1"])

    def test_a_second_run_finds_nothing_to_do(self) -> None:
        docker = FakeDocker({"a1": container("monitor-production-api-1")})
        self.assertEqual(run_main(docker, [NETWORK])[0], 0)
        code, stdout, _ = run_main(docker, [NETWORK])
        self.assertEqual((code, json.loads(stdout)), (0, {"started": []}))
        self.assertEqual(docker.starts(), ["a1"])

    def test_a_container_removed_after_the_listing_is_skipped(self) -> None:
        docker = FakeDocker({"gone": container("gone")}, vanished=["gone"])
        code, stdout, stderr = run_main(docker, [NETWORK])
        self.assertEqual((code, json.loads(stdout)), (0, {"started": []}), stderr)

    def test_a_failed_start_is_an_error_after_trying_the_rest(self) -> None:
        docker = FakeDocker(
            {"a1": container("first"), "b2": container("second")},
            failing_starts=["a1"],
        )
        code, stdout, stderr = run_main(docker, [NETWORK])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "ERROR: cannot start: first\n")
        self.assertEqual(docker.starts(), ["a1", "b2"])


class RestoreWaitTests(unittest.TestCase):
    """The network may appear seconds after docker.service is active."""

    def test_it_waits_for_the_network_before_listing(self) -> None:
        docker = FakeDocker({"a1": container("api")}, network_after=3)
        sleeps: list[float] = []
        started = restore.restore(
            docker, [NETWORK], 100.0, clock=lambda: 0.0, sleep=sleeps.append
        )
        self.assertEqual(started, ["api"])
        self.assertEqual(sleeps, [restore.POLL_SECONDS] * 3)
        inspections = [c for c in docker.calls if c[:2] == ["network", "inspect"]]
        self.assertEqual(len(inspections), 4)

    def test_a_network_that_never_appears_fails_the_unit(self) -> None:
        docker = FakeDocker({}, network_after=10**6)
        times = iter([0.0, 1.0, 2.0, 3.0])
        with self.assertRaisesRegex(restore.RestoreError, "did not appear in time"):
            restore.restore(
                docker,
                [NETWORK],
                2.0,
                clock=lambda: next(times),
                sleep=lambda _: None,
            )
        self.assertFalse(any(call[0] == "ps" for call in docker.calls))


class RestoreArgumentTests(unittest.TestCase):
    """Invalid arguments exit 2 before any Docker call."""

    def test_invalid_arguments(self) -> None:
        for argv in (
            [],
            ["--timeout", "0", NETWORK],
            ["--timeout", "3601", NETWORK],
            ["--timeout", "x", NETWORK],
            ["bad name"],
            [NETWORK, NETWORK],
        ):
            with self.subTest(argv=argv):
                docker = FakeDocker({})
                code, stdout, stderr = run_main(docker, argv)
                self.assertEqual(code, 2, stderr)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr.startswith("ERROR: "))
                self.assertEqual(docker.calls, [])

    def test_the_script_runs_under_the_host_interpreter(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--timeout", "0", NETWORK],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertTrue(completed.stderr.startswith("ERROR: "))
        self.assertEqual(SCRIPT.stat().st_mode & 0o022, 0)
        self.assertTrue(
            SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3\n")
        )


def flatten(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for task in tasks:
        flat.append(task)
        for section in ("block", "rescue", "always"):
            if isinstance(task.get(section), list):
                flat.extend(flatten(task[section]))
    return flat


def render_unit() -> str:
    group_vars = yaml.safe_load(GROUP_VARS.read_text(encoding="utf-8"))
    variables = {
        **yaml.safe_load(EDGE_DEFAULTS.read_text(encoding="utf-8")),
        "edge_adopted_attachable_networks": group_vars[
            "edge_adopted_attachable_networks"
        ],
    }
    # Ansible renders templates with trim_blocks.
    environment = Environment(
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
    )
    template = (EDGE_TEMPLATES / f"{UNIT}.service.j2").read_text(encoding="utf-8")
    return environment.from_string(template).render(**variables)


class RestoreUnitTests(unittest.TestCase):
    """The edge role installs, enables and runs the repair."""

    tasks: list[dict[str, Any]]
    names: list[str]
    by_name: dict[str, dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.tasks = flatten(yaml.safe_load(DEPLOY.read_text(encoding="utf-8")))
        cls.names = [task["name"] for task in cls.tasks]
        cls.by_name = {task["name"]: task for task in cls.tasks}

    def test_the_unit_runs_after_every_docker_start(self) -> None:
        unit = render_unit()
        self.assertIn("Type=oneshot\n", unit)
        self.assertNotIn("RemainAfterExit", unit)
        self.assertIn("After=docker.service\n", unit)
        self.assertIn("Requires=docker.service\n", unit)
        self.assertIn("WantedBy=docker.service\n", unit)
        self.assertIn(
            f"ExecStart=/usr/bin/python3 {HELPER} --timeout 300 "
            "apptolast-edge-observatorio apptolast-edge-satisfactory "
            "apptolast-edge-ax\n",
            unit,
        )
        # The helper's own wait ends before systemd gives up on the unit.
        self.assertIn("TimeoutStartSec=360\n", unit)
        for hardening in (
            "PrivateTmp=true",
            "ProtectSystem=full",
            "ProtectHome=read-only",
            "NoNewPrivileges=true",
        ):
            self.assertIn(hardening + "\n", unit)

    def test_the_networks_are_verified_before_the_repair(self) -> None:
        order = [
            "Verify every isolated edge network contract",
            "Install the adopted-network restore helper",
            "Install the adopted-network restore unit",
            "Enable the adopted-network restore unit",
            "Restore the containers of the adopted networks",
            "Deploy the edge stack",
        ]
        self.assertEqual([name for name in self.names if name in order], order)

    def test_the_tasks_install_enable_and_run_the_helper(self) -> None:
        helper = self.by_name["Install the adopted-network restore helper"][
            "ansible.builtin.copy"
        ]
        self.assertEqual(
            helper["src"],
            "{{ playbook_dir }}/../../scripts/restore-adopted-network-containers.py",
        )
        self.assertEqual(
            (helper["dest"], helper["owner"], helper["mode"]),
            ("{{ edge_adopted_network_restore_helper }}", "root", "0755"),
        )
        defaults = yaml.safe_load(EDGE_DEFAULTS.read_text(encoding="utf-8"))
        self.assertEqual(defaults["edge_adopted_network_restore_helper"], HELPER)
        self.assertEqual(defaults["edge_adopted_network_restore_unit"], UNIT)
        enable = self.by_name["Enable the adopted-network restore unit"][
            "ansible.builtin.systemd_service"
        ]
        self.assertTrue(enable["enabled"])
        # A started oneshot would report a change on every apply.
        self.assertNotIn("state", enable)
        self.assertEqual(
            enable["daemon_reload"],
            "{{ edge_adopted_network_restore_unit_file is changed }}",
        )
        run = self.by_name["Restore the containers of the adopted networks"]
        self.assertIn(
            "edge_adopted_attachable_networks", run["ansible.builtin.command"]["argv"]
        )
        self.assertIn(".started | length > 0", run["changed_when"])

    def test_the_runtime_validator_requires_the_enabled_unit(self) -> None:
        self.assertIn(
            f"systemctl is-enabled --quiet {UNIT}.service",
            VALIDATE_EDGE.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
