"""Functional tests for the guarded Swarm auto-unlock lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER = (
    PROJECT_ROOT
    / "ansible/roles/platform/files/dockerswarm-swarm-unlock"
)
UNIT = (
    PROJECT_ROOT
    / "ansible/roles/platform/files/dockerswarm-swarm-unlock.service"
)
DOCKER_OVERRIDE = (
    PROJECT_ROOT
    / "ansible/roles/platform/files/docker-firewall-systemd.conf"
)

DOCKER_MOCK = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["MOCK_DOCKER_STATE"])
log_path = Path(os.environ["MOCK_DOCKER_LOG"])
state = json.loads(state_path.read_text(encoding="utf-8"))
arguments = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(arguments) + "\n")

if arguments[:1] == ["info"]:
    if state["initial"] == "active":
        print("active true")
    elif state["unlocked"]:
        print("active true")
    elif state["initial"] == "locked":
        print("locked false")
    else:
        print(f"{state['initial']} false")
elif arguments == ["swarm", "unlock"]:
    value = sys.stdin.read().rstrip("\n")
    Path(os.environ["MOCK_UNLOCK_STDIN"]).write_text(value, encoding="utf-8")
    state["unlocked"] = True
    state_path.write_text(json.dumps(state), encoding="utf-8")
else:
    raise SystemExit(f"unexpected Docker arguments: {arguments!r}")
"""


class SwarmUnlockLifecycleTests(unittest.TestCase):
    def prepare(
        self,
        base: Path,
        initial_state: str,
    ) -> tuple[Path, dict[str, str], str]:
        unlock_key = "SWMKEY-1-" + ("a" * 48)
        key_path = base / "swarm-unlock-key"
        key_path.write_text(f"{unlock_key}\n", encoding="utf-8")
        key_path.chmod(0o600)

        docker = base / "docker-mock"
        docker.write_text(DOCKER_MOCK, encoding="utf-8")
        docker.chmod(0o755)
        stat_mock = base / "stat-mock"
        stat_mock.write_text(
            "#!/bin/sh\nprintf '%s\\n' '0:0:600:regular file'\n",
            encoding="utf-8",
        )
        stat_mock.chmod(0o755)

        script = base / "swarm-unlock"
        script.write_text(
            HELPER.read_text(encoding="utf-8")
            .replace(
                "/etc/dockerswarm/backup/swarm-unlock-key",
                str(key_path),
            )
            .replace("/usr/bin/docker", str(docker))
            .replace("/usr/bin/stat", str(stat_mock)),
            encoding="utf-8",
        )
        script.chmod(0o700)
        state_path = base / "state.json"
        state_path.write_text(
            json.dumps({"initial": initial_state, "unlocked": False}),
            encoding="utf-8",
        )
        environment = {
            **os.environ,
            "MOCK_DOCKER_STATE": str(state_path),
            "MOCK_DOCKER_LOG": str(base / "docker.log"),
            "MOCK_UNLOCK_STDIN": str(base / "unlock.stdin"),
        }
        return script, environment, unlock_key

    def test_locked_manager_uses_stdin_and_converges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            script, environment, unlock_key = self.prepare(base, "locked")
            subprocess.run(
                [str(script)],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(
                (base / "unlock.stdin").read_text(encoding="utf-8"),
                unlock_key,
            )
            calls = (base / "docker.log").read_text(encoding="utf-8")
            self.assertNotIn(unlock_key, calls)
            self.assertIn('["swarm", "unlock"]', calls)

    def test_active_manager_does_not_submit_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            script, environment, _unlock_key = self.prepare(base, "active")
            subprocess.run(
                [str(script)],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertFalse((base / "unlock.stdin").exists())

    def test_unexpected_swarm_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            script, environment, _unlock_key = self.prepare(base, "inactive")
            completed = subprocess.run(
                [str(script)],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse((base / "unlock.stdin").exists())

    def test_systemd_binds_unlock_to_every_docker_start(self) -> None:
        unit = UNIT.read_text(encoding="utf-8")
        docker_override = DOCKER_OVERRIDE.read_text(encoding="utf-8")
        self.assertIn("After=docker.service", unit)
        self.assertIn("PartOf=docker.service", unit)
        self.assertIn("ConditionPathExists=", unit)
        self.assertIn("dockerswarm-swarm-unlock.service", docker_override)

    def test_docker_start_is_fail_closed_on_ingress_policy_failure(self) -> None:
        docker_override = DOCKER_OVERRIDE.read_text(encoding="utf-8")
        platform_tasks = (
            PROJECT_ROOT / "ansible/roles/platform/tasks/main.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "ExecStartPost=/usr/local/sbin/dockerswarm-docker-firewall",
            docker_override,
        )
        self.assertIn(
            "ExecStopPost=/usr/local/sbin/dockerswarm-docker-firewall --close",
            docker_override,
        )
        self.assertNotIn(
            "Wants=dockerswarm-docker-firewall.service",
            docker_override,
        )
        self.assertNotIn(
            "name: dockerswarm-docker-firewall.service\n"
            "        enabled: true",
            platform_tasks,
        )


if __name__ == "__main__":
    unittest.main()
