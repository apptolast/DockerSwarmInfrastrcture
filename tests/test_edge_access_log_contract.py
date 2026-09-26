"""Traefik's access log on a host file, the one CrowdSec tails.

CrowdSec 1.7.8 must never read Traefik through the Docker API: a Docker
source that loses the daemon for about 15 minutes stops every acquisition,
SSH included. Traefik therefore writes its JSON access log to a pre-created
file in a root-owned host directory (docs/EDGE.md, «Log de acceso en
fichero»). These tests cover the edge role's preparation of that file, its
rotation, the deployed mounts and the runtime proof that Traefik writes the
login probes there, plus scripts/traefik-access-log-probes.py.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, ClassVar

import yaml
from ansible_task_harness import REPOSITORY_ROOT, run_task_definitions
from jinja2 import Environment, StrictUndefined

DEPLOY = REPOSITORY_ROOT / "ansible/roles/edge/tasks/deploy.yml"
EDGE_MAIN = "ansible/roles/edge/tasks/main.yml"
EDGE_DEFAULTS = REPOSITORY_ROOT / "ansible/roles/edge/defaults/main.yml"
EDGE_TEMPLATES = REPOSITORY_ROOT / "ansible/roles/edge/templates"
GROUP_VARS = REPOSITORY_ROOT / "ansible/group_vars/all.yml"
HOST_SECURITY = REPOSITORY_ROOT / "config/host-security.yml"
HOST_SECURITY_TASKS = REPOSITORY_ROOT / "ansible/roles/host_security/tasks/main.yml"
PROBE_SCRIPT = REPOSITORY_ROOT / "scripts/traefik-access-log-probes.py"
ACCESS_LOG_DIR = "/var/log/dockerswarm/edge"


def load_script(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probes = load_script("traefik_access_log_probes", PROBE_SCRIPT)


def flatten(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for task in tasks:
        flat.append(task)
        for section in ("block", "rescue", "always"):
            if isinstance(task.get(section), list):
                flat.extend(flatten(task[section]))
    return flat


def deploy_tasks() -> list[dict[str, Any]]:
    return flatten(yaml.safe_load(DEPLOY.read_text(encoding="utf-8")))


def by_name(tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {task["name"]: task for task in tasks}


def edge_variables() -> dict[str, Any]:
    defaults = yaml.safe_load(EDGE_DEFAULTS.read_text(encoding="utf-8"))
    group_vars = yaml.safe_load(GROUP_VARS.read_text(encoding="utf-8"))
    return {
        **defaults,
        "edge_install_root": "/opt/dockerswarm",
        "edge_traefik_access_log_dir": group_vars["edge_traefik_access_log_dir"],
    }


def render(template: str) -> str:
    variables = edge_variables()
    environment = Environment(
        undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True
    )
    # Resolve the defaults that reference other variables, as Ansible does.
    for key in (
        "edge_traefik_access_log_rotation_config",
        "edge_traefik_access_log_rotation_state",
    ):
        variables[key] = environment.from_string(variables[key]).render(**variables)
    return environment.from_string(
        (EDGE_TEMPLATES / template).read_text(encoding="utf-8")
    ).render(**variables)


class EdgeAccessLogPreparationTests(unittest.TestCase):
    """The edge role prepares one file Traefik may only append to."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tasks = deploy_tasks()
        cls.names = [task["name"] for task in cls.tasks]
        cls.by_name = by_name(cls.tasks)

    def test_the_file_and_its_rotation_exist_before_the_stack_deploys(self) -> None:
        order = [
            "Verify the final ACME storage identity",
            "Inspect the Traefik access log directories without following links",
            "Reject an unsafe Traefik access log directory",
            "Create the Traefik access log directories",
            "Inspect the Traefik access log without following links",
            "Reject an unsafe or drifted existing Traefik access log",
            "Create the Traefik access log",
            "Verify the Traefik access log after creation",
            "Verify the final Traefik access log identity",
            "Install the Traefik access log rotation policy",
            "Install the Traefik access log rotation units",
            "Enable the Traefik access log rotation timer",
            "Render the Traefik static configuration",
            "Deploy the edge stack",
            "Verify the deployed Traefik service identity",
            "Read the Traefik access log size before the login probes",
            "Prove every basicAuth route answers its Basic challenge",
            "Prove the host access log records every login probe",
        ]
        self.assertEqual([name for name in self.names if name in order], order)

    def test_the_directories_match_the_ones_host_security_creates(self) -> None:
        inspect = self.by_name[
            "Inspect the Traefik access log directories without following links"
        ]
        self.assertIs(inspect["ansible.builtin.stat"]["follow"], False)
        create = self.by_name["Create the Traefik access log directories"]
        self.assertEqual(create["loop"], inspect["loop"])
        directory = create["ansible.builtin.file"]
        self.assertEqual(
            {key: directory[key] for key in ("state", "owner", "group", "mode")},
            {"state": "directory", "owner": "root", "group": "root", "mode": "0755"},
        )
        host_security = by_name(
            yaml.safe_load(HOST_SECURITY_TASKS.read_text(encoding="utf-8"))
        )["Create the Traefik access log directories CrowdSec watches"]
        self.assertEqual(
            {
                key: host_security["ansible.builtin.file"][key]
                for key in ("state", "owner", "group", "mode")
            },
            {key: directory[key] for key in ("state", "owner", "group", "mode")},
        )
        # Both loops resolve to the same two directories.
        contract = yaml.safe_load(HOST_SECURITY.read_text(encoding="utf-8"))
        completed = run_task_definitions(
            [
                {
                    "name": "Compare the two roles",
                    "ansible.builtin.assert": {
                        "that": [
                            "edge_dirs == host_security_dirs",
                            (
                                "edge_dirs == ['/var/log/dockerswarm',"
                                " '/var/log/dockerswarm/edge']"
                            ),
                        ]
                    },
                    "vars": {
                        "edge_dirs": create["loop"],
                        "host_security_dirs": host_security["loop"],
                    },
                }
            ],
            {
                **edge_variables(),
                "host_security_crowdsec_traefik_access_log": contract[
                    "host_security_crowdsec_traefik_access_log"
                ],
            },
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_an_unsafe_directory_or_file_stops_the_deploy(self) -> None:
        directory_gate = self.by_name["Reject an unsafe Traefik access log directory"]
        for label, (stat_result, accepted) in {
            "absent": ({"exists": False}, True),
            "directory": ({"exists": True, "isdir": True, "islnk": False}, True),
            "link": ({"exists": True, "isdir": False, "islnk": True}, False),
        }.items():
            with self.subTest(label):
                completed = run_task_definitions(
                    [directory_gate],
                    {
                        "edge_traefik_access_log_dirs": {
                            "results": [{"item": ACCESS_LOG_DIR, "stat": stat_result}]
                        }
                    },
                )
                self.assertEqual(
                    completed.returncode == 0,
                    accepted,
                    completed.stdout + completed.stderr,
                )
        file_gate = self.by_name[
            "Reject an unsafe or drifted existing Traefik access log"
        ]
        reviewed = {
            "exists": True,
            "isreg": True,
            "islnk": False,
            "nlink": 1,
            "uid": 65532,
            "gid": 65532,
            "mode": "0600",
        }
        cases = {
            "absent": ({"exists": False}, True),
            "reviewed": (reviewed, True),
            "link": (dict(reviewed, isreg=False, islnk=True), False),
            "second link": (dict(reviewed, nlink=2), False),
            "root owned": (dict(reviewed, uid=0), False),
            "foreign group": (dict(reviewed, gid=0), False),
            "world readable": (dict(reviewed, mode="0644"), False),
            "directory": (dict(reviewed, isreg=False), False),
        }
        for label, (stat_result, accepted) in cases.items():
            with self.subTest(label):
                completed = run_task_definitions(
                    [file_gate],
                    {
                        "edge_traefik_access_log": {"stat": stat_result},
                        "edge_traefik_runtime_uid": 65532,
                        "edge_traefik_runtime_gid": 65532,
                    },
                )
                output = completed.stdout + completed.stderr
                self.assertEqual(completed.returncode == 0, accepted, output)
                if not accepted:
                    self.assertIn("Refusing to follow, replace, chmod or chown", output)

    def test_the_file_is_created_once_with_the_runtime_identity(self) -> None:
        create = self.by_name["Create the Traefik access log"]
        self.assertEqual(
            create["ansible.builtin.file"],
            {
                "path": "{{ edge_traefik_access_log_dir }}/access.log",
                "state": "touch",
                "owner": "{{ edge_traefik_runtime_uid }}",
                "group": "{{ edge_traefik_runtime_gid }}",
                "mode": "0600",
                "follow": False,
                "access_time": "preserve",
                "modification_time": "preserve",
            },
        )
        self.assertEqual(create["when"], "not edge_traefik_access_log.stat.exists")
        verify = " ".join(
            self.by_name["Verify the final Traefik access log identity"][
                "ansible.builtin.assert"
            ]["that"]
        )
        for invariant in ("isreg", "islnk", "nlink", "uid", "gid", '"0600"'):
            self.assertIn(invariant, verify)

    def test_the_directory_is_pinned_where_crowdsec_reads(self) -> None:
        group_vars = yaml.safe_load(GROUP_VARS.read_text(encoding="utf-8"))
        defaults = yaml.safe_load(EDGE_DEFAULTS.read_text(encoding="utf-8"))
        contract = yaml.safe_load(HOST_SECURITY.read_text(encoding="utf-8"))
        self.assertEqual(group_vars["edge_traefik_access_log_dir"], ACCESS_LOG_DIR)
        self.assertEqual(defaults["edge_traefik_access_log_dir"], ACCESS_LOG_DIR)
        self.assertEqual(
            contract["host_security_crowdsec_traefik_access_log"],
            f"{ACCESS_LOG_DIR}/access.log",
        )
        verify = yaml.safe_load(
            (REPOSITORY_ROOT / EDGE_MAIN).read_text(encoding="utf-8")
        )[1]
        self.assertEqual(verify["name"], "Verify the edge deployment inputs")
        self.assertIn(
            f'edge_traefik_access_log_dir == "{ACCESS_LOG_DIR}"',
            verify["ansible.builtin.assert"]["that"],
        )


class EdgeAccessLogRotationTests(unittest.TestCase):
    """The file is bounded without a signal to the Swarm task."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.by_name = by_name(deploy_tasks())

    def test_the_policy_rotates_by_copy_and_truncate(self) -> None:
        rendered = render("traefik-access.logrotate.j2")
        body = [
            line.strip()
            for line in rendered.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(body[0], f"{ACCESS_LOG_DIR}/access.log {{")
        self.assertEqual(body[-1], "}")
        directives = body[1:-1]
        self.assertEqual(
            directives,
            [
                "daily",
                "maxsize 100M",
                "rotate 14",
                "missingok",
                "notifempty",
                "copytruncate",
                "compress",
                "delaycompress",
                "nomail",
            ],
        )
        # No script: a postrotate signal would need the Docker API, and
        # `create` would replace the file Traefik holds open.
        for forbidden in ("postrotate", "prerotate", "create", "su ", "olddir"):
            self.assertFalse(
                any(line.startswith(forbidden) for line in directives), forbidden
            )
        install = self.by_name["Install the Traefik access log rotation policy"][
            "ansible.builtin.template"
        ]
        self.assertEqual(
            install["validate"], "/usr/sbin/logrotate --debug --state /dev/null %s"
        )
        self.assertEqual((install["owner"], install["mode"]), ("root", "0644"))

    def test_a_dedicated_timer_bounds_a_flood(self) -> None:
        service = render("dockerswarm-edge-access-log-rotate.service.j2")
        self.assertIn("Type=oneshot\n", service)
        self.assertIn(
            "ExecStart=/usr/sbin/logrotate --state "
            "/var/lib/logrotate/dockerswarm-edge-access.status "
            "/opt/dockerswarm/edge/config/traefik-access.logrotate\n",
            service,
        )
        for hardening in (
            "PrivateTmp=true",
            "ProtectSystem=full",
            "ProtectHome=true",
            "NoNewPrivileges=true",
        ):
            self.assertIn(hardening + "\n", service)
        timer = render("dockerswarm-edge-access-log-rotate.timer.j2")
        # Every 15 minutes: the distribution's daily logrotate.timer would
        # let a flood grow the file for a whole day.
        self.assertIn("OnCalendar=*:0/15\n", timer)
        self.assertIn("WantedBy=timers.target\n", timer)
        units = self.by_name["Install the Traefik access log rotation units"]
        self.assertEqual(
            units["loop"],
            [
                "{{ edge_traefik_access_log_rotation_unit }}.service",
                "{{ edge_traefik_access_log_rotation_unit }}.timer",
            ],
        )
        enable = self.by_name["Enable the Traefik access log rotation timer"][
            "ansible.builtin.systemd_service"
        ]
        self.assertEqual(
            (enable["name"], enable["enabled"], enable["state"]),
            ("{{ edge_traefik_access_log_rotation_unit }}.timer", True, "started"),
        )
        self.assertEqual(
            enable["daemon_reload"],
            "{{ edge_traefik_access_log_rotation_units is changed }}",
        )


class EdgeAccessLogProofTests(unittest.TestCase):
    """The deploy proves Traefik wrote the login probes to the host file."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.by_name = by_name(deploy_tasks())
        cls.proof = cls.by_name["Prove the host access log records every login probe"]
        cls.challenges = cls.by_name[
            "Prove every basicAuth route answers its Basic challenge"
        ]

    def test_the_probed_routers_are_the_ones_crowdsec_counts(self) -> None:
        contract = yaml.safe_load(HOST_SECURITY.read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(item["router"] for item in self.challenges["loop"]),
            sorted(contract["host_security_crowdsec_basicauth_routers"]),
        )

    def test_the_proof_passes_every_probe_and_the_prior_size(self) -> None:
        results = [{"item": item} for item in self.challenges["loop"]]
        completed = run_task_definitions(
            [
                {
                    "name": "Render the reviewed command",
                    "ansible.builtin.set_fact": {
                        "probe_command": self.proof["ansible.builtin.script"]["cmd"]
                    },
                },
                {
                    "name": "Show it",
                    "ansible.builtin.assert": {
                        "that": ["false"],
                        "fail_msg": "COMMAND<<{{ probe_command }}>>",
                    },
                },
            ],
            {
                **edge_variables(),
                "edge_basicauth_challenges": {"results": results},
                "edge_traefik_access_log_before_probes": {"stat": {"size": 4096}},
            },
        )
        output = completed.stdout + completed.stderr
        command = output.split("COMMAND<<", 1)[1].split(">>", 1)[0]
        argv = shlex.split(command)
        self.assertTrue(argv[0].endswith("/../../scripts/traefik-access-log-probes.py"))
        self.assertEqual(
            argv[1:],
            [
                f"{ACCESS_LOG_DIR}/access.log",
                "4096",
                "logs-satisfactory.apptolast.com=satisfactory-logs@file",
                "ax.apptolast.com=ax@file",
            ],
        )

    def test_the_proof_retries_and_never_changes_anything(self) -> None:
        self.assertEqual(self.proof["until"], "edge_traefik_access_log_probes.rc == 0")
        self.assertGreaterEqual(self.proof["retries"] * self.proof["delay"], 60)
        self.assertIs(self.proof["changed_when"], False)
        self.assertIs(self.proof["check_mode"], False)
        self.assertEqual(
            self.proof["ansible.builtin.script"]["executable"], "/usr/bin/python3"
        )
        size = self.by_name["Read the Traefik access log size before the login probes"]
        self.assertIs(size["ansible.builtin.stat"]["follow"], False)


def access_line(
    host: str, router: str, status: int = 401, client: str = "198.51.100.7"
) -> str:
    return json.dumps(
        {
            "ClientHost": client,
            "DownstreamStatus": status,
            "RequestHost": host,
            "RequestPath": "/",
            "RouterName": router,
        }
    )


class TraefikAccessLogProbeScriptTests(unittest.TestCase):
    """scripts/traefik-access-log-probes.py counts, and prints only counts."""

    PAIRS: ClassVar[tuple[str, ...]] = (
        "ax.apptolast.com=ax@file",
        "logs.example.com=logs@file",
    )

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "access.log"

    def write(self, lines: list[str]) -> int:
        data = "".join(line + "\n" for line in lines).encode()
        self.log.write_bytes(data)
        return len(data)

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = probes.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_only_lines_after_the_offset_count(self) -> None:
        offset = self.write([access_line("ax.apptolast.com", "ax@file")])
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(access_line("logs.example.com", "logs@file") + "\n")
        code, stdout, stderr = self.run_main(str(self.log), str(offset), *self.PAIRS)
        self.assertEqual(code, 1, stderr)
        self.assertEqual(
            json.loads(stdout),
            {"ax.apptolast.com=ax@file": 0, "logs.example.com=logs@file": 1},
        )
        self.assertIn("ax.apptolast.com=ax@file", stderr)

    def test_every_probe_found_succeeds_without_printing_the_log(self) -> None:
        self.write(
            [
                access_line("ax.apptolast.com", "ax@file"),
                access_line("logs.example.com", "logs@file"),
                access_line("logs.example.com", "logs@file"),
            ]
        )
        code, stdout, stderr = self.run_main(str(self.log), "0", *self.PAIRS)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            json.loads(stdout),
            {"ax.apptolast.com=ax@file": 1, "logs.example.com=logs@file": 2},
        )
        self.assertNotIn("198.51.100.7", stdout + stderr)

    def test_only_a_401_on_the_named_router_counts(self) -> None:
        self.write(
            [
                access_line("ax.apptolast.com", "ax@file", status=200),
                access_line("ax.apptolast.com", "ax-health@file"),
                access_line("other.apptolast.com", "ax@file"),
                "not json",
                '{"DownstreamStatus": 401',
                "[401]",
                access_line("logs.example.com", "logs@file"),
            ]
        )
        code, stdout, _ = self.run_main(str(self.log), "0", *self.PAIRS)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout)["ax.apptolast.com=ax@file"], 0)

    def test_a_file_truncated_by_rotation_is_read_from_its_start(self) -> None:
        self.write(
            [
                access_line("ax.apptolast.com", "ax@file"),
                access_line("logs.example.com", "logs@file"),
            ]
        )
        code, _stdout, stderr = self.run_main(str(self.log), "999999", *self.PAIRS)
        self.assertEqual(code, 0, stderr)

    def test_the_read_is_bounded_to_the_tail(self) -> None:
        self.write(
            [access_line("ax.apptolast.com", "ax@file")]
            + ["x" * 100] * 50
            + [access_line("logs.example.com", "logs@file")]
        )
        original = probes.MAX_READ_BYTES
        probes.MAX_READ_BYTES = 300
        self.addCleanup(setattr, probes, "MAX_READ_BYTES", original)
        code, stdout, _ = self.run_main(str(self.log), "0", *self.PAIRS)
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(stdout),
            {"ax.apptolast.com=ax@file": 0, "logs.example.com=logs@file": 1},
        )

    def test_links_and_bad_arguments_are_refused(self) -> None:
        self.write([access_line("ax.apptolast.com", "ax@file")])
        link = self.root / "link.log"
        link.symlink_to(self.log)
        second = self.root / "second.log"
        for label, argv in {
            "symlink": [str(link), "0", self.PAIRS[0]],
            "hard link": [str(self.log), "0", self.PAIRS[0]],
            "relative": ["access.log", "0", self.PAIRS[0]],
            "negative offset": [str(self.log), "-1", self.PAIRS[0]],
            "no pair": [str(self.log), "0"],
            "bad pair": [str(self.log), "0", "ax.apptolast.com"],
            "shell in pair": [str(self.log), "0", "a;b=ax@file"],
            "duplicate pair": [str(self.log), "0", self.PAIRS[0], self.PAIRS[0]],
            "missing file": [str(self.root / "none.log"), "0", self.PAIRS[0]],
        }.items():
            with self.subTest(label):
                # Only the hard link case holds a second name, so the symlink
                # case is refused for being a link, not for the link count.
                if label == "hard link":
                    os.link(self.log, second)
                try:
                    code, stdout, stderr = self.run_main(*argv)
                finally:
                    if second.exists():
                        second.unlink()
                self.assertEqual(code, 2, stdout + stderr)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr.startswith("ERROR: "))
        code, _, stderr = self.run_main(str(self.log), "0", self.PAIRS[0])
        self.assertEqual(code, 0, stderr)

    def test_the_script_runs_under_the_host_interpreter(self) -> None:
        self.write([access_line("ax.apptolast.com", "ax@file")])
        completed = subprocess.run(
            [sys.executable, str(PROBE_SCRIPT), str(self.log), "0", self.PAIRS[0]],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {self.PAIRS[0]: 1})
        self.assertEqual(PROBE_SCRIPT.stat().st_mode & 0o022, 0)


class EdgeAccessLogWindowDocsTests(unittest.TestCase):
    """STOP gate 10 and the docs name the window this change applies in."""

    @staticmethod
    def text(path: str) -> str:
        raw = (REPOSITORY_ROOT / path).read_text(encoding="utf-8")
        return " ".join(raw.split())

    def test_stop_gate_10_names_the_access_log_window(self) -> None:
        claude = self.text("CLAUDE.md")
        gate = claude[
            claude.index("### 10. Hand-modified Traefik") : claude.index(
                "## Commit and changelog conventions"
            )
        ]
        for fragment in (
            "only through «Ventana de aplicación de la ruta»",
            "Once that route window is recorded",
            "«Ventana del log de acceso»",
            "`Version.Index`",
            "`edge_probe`",
            "22:30\u201300:40 UTC",
            "`changed=0`",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, gate)
        for path in ("docs/EDGE.md", "docs/DEPLOYMENT_STATUS.md", "docs/OPERATIONS.md"):
            with self.subTest(path=path):
                self.assertIn("«Ventana del log de acceso»", self.text(path))
        edge = self.text("docs/EDGE.md")
        self.assertIn("Registrada esa ventana, cada apply posterior", edge)
        self.assertNotIn("que es la que exige la compuerta STOP 10", edge)

    def test_the_window_waits_for_the_last_recorded_edge_window(self) -> None:
        edge = self.text("docs/EDGE.md")
        window = edge[
            edge.index("### Ventana del log de acceso") : edge.index(
                "#### Rollback del log de acceso"
            )
        ]
        for fragment in (
            "Si falta una, esta ventana no empieza",
            "tras el apply repetido",
            "PR #80",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, window)
        status = self.text("docs/DEPLOYMENT_STATUS.md")
        step = status[status.index("3. `edge` en la «Ventana del log de acceso»") :]
        step = step[: step.index("4. Verificar")]
        self.assertIn("PR #80", step)
        self.assertIn("`Version.Index`", step)


if __name__ == "__main__":
    unittest.main()
