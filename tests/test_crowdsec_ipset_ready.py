"""Tests for the Docker firewall dependency on CrowdSec ipsets."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from jinja2 import Environment, StrictUndefined


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (
    PROJECT_ROOT
    / "ansible/roles/host_baseline/templates/crowdsec-ipset-ready.sh.j2"
)
DROP_IN = (
    PROJECT_ROOT
    / "ansible/roles/host_baseline/templates/docker-firewall-crowdsec.conf.j2"
)
SERVICE_UNIT = (
    PROJECT_ROOT
    / "ansible/roles/platform/files/dockerswarm-docker-firewall.service"
)


class CrowdSecIPSetReadinessTests(unittest.TestCase):
    def render_helper(self, base: Path, timeout: int) -> tuple[Path, dict[str, str]]:
        rendered = Environment(
            autoescape=False,
            keep_trailing_newline=True,
            undefined=StrictUndefined,
        ).from_string(TEMPLATE.read_text(encoding="utf-8")).render(
            host_baseline_crowdsec_ipset_wait_timeout_seconds=timeout,
        )
        ipset = base / "ipset-mock"
        ipset.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$MOCK_IPSETS\"\n",
            encoding="utf-8",
        )
        ipset.chmod(0o755)
        script = base / "wait-for-ipsets"
        script.write_text(
            rendered.replace("/usr/sbin/ipset", str(ipset)),
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script, {**os.environ}

    def test_accepts_both_crowdsec_ipset_families(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.render_helper(Path(temporary), timeout=1)
            environment["MOCK_IPSETS"] = (
                "crowdsec-blacklists-0\ncrowdsec6-blacklists-0"
            )
            subprocess.run([str(script)], check=True, env=environment)

    def test_rejects_missing_ipv6_ipsets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.render_helper(Path(temporary), timeout=0)
            environment["MOCK_IPSETS"] = "crowdsec-blacklists-0"
            completed = subprocess.run(
                [str(script)], check=False, capture_output=True, text=True, env=environment
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Timed out waiting for CrowdSec", completed.stderr)

    def test_drop_in_waits_before_applying_firewall_rules(self) -> None:
        rendered = Environment(
            autoescape=False,
            keep_trailing_newline=True,
            undefined=StrictUndefined,
        ).from_string(DROP_IN.read_text(encoding="utf-8")).render(
            host_baseline_crowdsec_ipset_wait_script="/usr/local/sbin/wait-for-ipsets",
            host_baseline_crowdsec_order_script="/usr/local/sbin/order-rules",
        )
        self.assertLess(
            rendered.index("ExecStartPre=/usr/local/sbin/wait-for-ipsets"),
            rendered.index("ExecStartPost=/usr/local/sbin/order-rules"),
        )

    def test_firewall_unit_keeps_the_raw_socket_capability(self) -> None:
        self.assertIn(
            "CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW",
            SERVICE_UNIT.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "CapabilityBoundingSet=CAP_NET_RAW",
            DROP_IN.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
