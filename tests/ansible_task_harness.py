"""Run one reviewed Ansible task against synthetic facts, never Docker.

The task is copied unchanged from its role task file into a disposable
playbook, so the Jinja expression under test is exactly the one production
runs. Only side-effect-free modules are accepted: a gate that reads Docker
is fed the registered result it would have received instead.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ANSIBLE_PLAYBOOK = REPOSITORY_ROOT / ".venv/bin/ansible-playbook"
SIDE_EFFECT_FREE_MODULES = {"ansible.builtin.assert", "ansible.builtin.set_fact"}


def load_task(task_file: str, task_name: str) -> dict[str, Any]:
    """Return the single task called task_name, refusing side effects."""
    tasks = yaml.safe_load((REPOSITORY_ROOT / task_file).read_text(encoding="utf-8"))
    matches = [
        task
        for task in tasks
        if isinstance(task, dict) and task.get("name") == task_name
    ]
    if len(matches) != 1:
        raise AssertionError(f"{task_file}: expected one task named {task_name!r}")
    task = matches[0]
    if len(SIDE_EFFECT_FREE_MODULES.intersection(task)) != 1:
        raise AssertionError(f"{task_name!r} is not a side-effect-free task")
    return task


def run_task(
    task_file: str,
    task_name: str,
    variables: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    task = load_task(task_file, task_name)
    with tempfile.TemporaryDirectory() as temporary:
        playbook = Path(temporary) / "task.yml"
        playbook.write_text(
            yaml.safe_dump(
                [
                    {
                        "name": "Exercise one reviewed task",
                        "hosts": "localhost",
                        "connection": "local",
                        "gather_facts": False,
                        "become": False,
                        "vars": variables,
                        "tasks": [task],
                    }
                ],
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return subprocess.run(
            [str(ANSIBLE_PLAYBOOK), "-i", "localhost,", str(playbook)],
            cwd=REPOSITORY_ROOT / "ansible",
            text=True,
            capture_output=True,
            check=False,
        )


class AnsibleTaskAssertions:
    """unittest mixin: a reviewed gate accepts or rejects synthetic facts."""

    def assert_task_accepts(
        self,
        task_file: str,
        task_name: str,
        variables: dict[str, Any],
    ) -> None:
        completed = run_task(task_file, task_name, variables)
        self.assertEqual(  # type: ignore[attr-defined]
            completed.returncode, 0, completed.stdout + completed.stderr
        )

    def assert_task_rejects(
        self,
        task_file: str,
        task_name: str,
        variables: dict[str, Any],
        expected_message: str,
    ) -> None:
        completed = run_task(task_file, task_name, variables)
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0, output)  # type: ignore[attr-defined]
        self.assertIn(expected_message, output)  # type: ignore[attr-defined]
