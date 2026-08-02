"""Functional tests for the atomic Docker ingress firewall renderer."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from jinja2 import Environment, StrictUndefined

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (
    PROJECT_ROOT
    / "ansible/roles/platform/templates/dockerswarm-docker-firewall.sh.j2"
)
LIVE_VALIDATOR = PROJECT_ROOT / "scripts/validate-firewall.sh"


IPTABLES_MOCK = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["MOCK_IPTABLES_STATE"])
log_path = Path(os.environ["MOCK_IPTABLES_LOG"])
arguments = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(arguments) + "\n")
state = json.loads(state_path.read_text(encoding="utf-8"))
normalized = arguments[1:] if arguments[:1] == ["-w"] else arguments

if normalized[:2] == ["-n", "-L"]:
    raise SystemExit(0)
if normalized[:1] == ["-N"]:
    state["chains"].append(normalized[1])
elif normalized[:1] == ["-A"]:
    if (
        "--ctorigdstport" in normalized
        and normalized[normalized.index("--ctorigdstport") + 1]
        == os.environ.get("MOCK_FAIL_PORT")
    ):
        raise SystemExit(42)
elif normalized[:2] == ["-S", "DOCKER-USER"]:
    for target in state["jumps"]:
        print(f"-A DOCKER-USER -j {target}")
elif normalized == ["-S"]:
    for chain in state["chains"]:
        print(f"-N {chain}")
elif normalized[:2] == ["-I", "DOCKER-USER"]:
    position = int(normalized[2]) - 1
    target = normalized[normalized.index("-j") + 1]
    state["jumps"].insert(position, target)
elif normalized[:2] == ["-D", "DOCKER-USER"]:
    target = normalized[normalized.index("-j") + 1]
    if target not in state["jumps"]:
        raise SystemExit(1)
    state["jumps"].remove(target)
elif normalized[:1] == ["-F"]:
    pass
elif normalized[:1] == ["-X"]:
    state["chains"].remove(normalized[1])
elif normalized[:1] == ["-E"]:
    previous, replacement = normalized[1:3]
    state["chains"][state["chains"].index(previous)] = replacement
    state["jumps"] = [
        replacement if target == previous else target
        for target in state["jumps"]
    ]
else:
    raise SystemExit(f"unexpected iptables call: {normalized!r}")
state_path.write_text(json.dumps(state), encoding="utf-8")
"""


class DockerFirewallTests(unittest.TestCase):
    def test_live_validator_gates_minecraft_published_port(self) -> None:
        source = LIVE_VALIDATOR.read_text(encoding="utf-8")
        self.assertIn(
            'readonly MINECRAFT_PUBLIC_ENABLED="${firewall_contract[5]}"',
            source,
        )
        self.assertIn(
            'if [[ "${MINECRAFT_PUBLIC_ENABLED}" == "true" ]]; then',
            source,
        )
        conditional = source.index(
            'if [[ "${MINECRAFT_PUBLIC_ENABLED}" == "true" ]]; then'
        )
        minecraft_port = source.index(
            '"workloads_minecraft|25565|25565|tcp|host"'
        )
        conditional_end = source.index("\nfi", minecraft_port)
        self.assertLess(conditional, minecraft_port)
        self.assertLess(minecraft_port, conditional_end)

    def render_script(self, base: Path) -> tuple[Path, dict[str, str]]:
        environment = Environment(
            autoescape=False,
            keep_trailing_newline=True,
            undefined=StrictUndefined,
        )
        rendered = environment.from_string(
            TEMPLATE.read_text(encoding="utf-8")
        ).render(
            platform_public_interface="eth0",
            platform_effective_public_tcp_ports=[80, 443],
            platform_public_ipv6_tcp_ports=[],
        )
        iptables = base / "iptables-mock"
        iptables.write_text(IPTABLES_MOCK, encoding="utf-8")
        iptables.chmod(0o755)
        ip6tables = base / "ip6tables-mock"
        ip6tables.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >>\"$MOCK_IP6TABLES_LOG\"\n"
            "exit 0\n",
            encoding="utf-8",
        )
        ip6tables.chmod(0o755)
        ip = base / "ip-mock"
        ip.write_text(
            textwrap.dedent(
                """\
                #!/bin/sh
                if [ "$1" = "-4" ] || [ "$1" = "-6" ]; then
                  printf '%s\\n' 'default via 192.0.2.1 dev eth0'
                fi
                """
            ),
            encoding="utf-8",
        )
        ip.chmod(0o755)
        script = base / "firewall"
        script.write_text(
            rendered.replace("/usr/sbin/ip6tables", str(ip6tables))
            .replace("/usr/sbin/iptables", str(iptables))
            .replace("/usr/sbin/ip", str(ip)),
            encoding="utf-8",
        )
        script.chmod(0o755)
        state = base / "state.json"
        state.write_text(
            json.dumps(
                {
                    "chains": [
                        "DOCKER-USER",
                        "CROWDSEC",
                        "DOCKERSWARM-INGRESS",
                    ],
                    "jumps": ["CROWDSEC", "DOCKERSWARM-INGRESS"],
                }
            ),
            encoding="utf-8",
        )
        log = base / "calls.jsonl"
        return script, {
            **os.environ,
            "MOCK_IPTABLES_STATE": str(state),
            "MOCK_IPTABLES_LOG": str(log),
            "MOCK_IP6TABLES_LOG": str(base / "ip6tables.log"),
        }

    def test_ports_are_separate_and_policy_switch_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            script, environment = self.render_script(base)
            subprocess.run(
                [str(script)],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            calls = [
                json.loads(line)
                for line in (base / "calls.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            ports = [
                call[call.index("--ctorigdstport") + 1]
                for call in calls
                if "--ctorigdstport" in call
            ]
            self.assertEqual(ports, ["80", "443"])
            self.assertNotIn("25565", ports)
            state = json.loads(
                (base / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                state["jumps"], ["CROWDSEC", "DOCKERSWARM-INGRESS"]
            )
            self.assertEqual(
                state["chains"].count("DOCKERSWARM-INGRESS"), 1
            )
            self.assertNotIn(
                "--ctorigdstport",
                (base / "ip6tables.log").read_text(encoding="utf-8"),
            )

    def test_build_failure_keeps_previous_policy_referenced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            script, environment = self.render_script(base)
            environment["MOCK_FAIL_PORT"] = "443"
            completed = subprocess.run(
                [str(script)],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertNotEqual(completed.returncode, 0)
            state = json.loads(
                (base / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                state["jumps"], ["CROWDSEC", "DOCKERSWARM-INGRESS"]
            )

    def test_close_mode_publishes_no_exception_before_drop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            script, environment = self.render_script(base)
            subprocess.run(
                [str(script), "--close"],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            calls = [
                json.loads(line)
                for line in (base / "calls.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertFalse(
                any("--ctorigdstport" in call for call in calls)
            )
            self.assertTrue(
                any(
                    call[-4:] == ["-i", "eth0", "-j", "DROP"]
                    for call in calls
                )
            )


if __name__ == "__main__":
    unittest.main()
