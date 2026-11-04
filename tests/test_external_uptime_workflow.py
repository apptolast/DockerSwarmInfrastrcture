"""The off-host availability check of the public names.

monitor.apptolast.com stayed at 502 from 2026-10-18 to 2026-11-04 and nobody
saw it, because the monitoring dashboard was both what failed and what
would have warned. .github/workflows/external-uptime.yml probes every public
name from a GitHub runner and keeps one issue open while any fails.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/external-uptime.yml"
PLATFORM = ROOT / "config/platform.yml"
CHECK = re.compile(
    r'"(https://([a-z0-9-]+)\.apptolast\.com/\S*) ([0-9]{3}(?:,[0-9]{3})*)"'
)


def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def checks() -> list[tuple[str, str, str]]:
    return CHECK.findall(WORKFLOW.read_text(encoding="utf-8"))


class ExternalUptimeWorkflowTests(unittest.TestCase):
    def test_it_runs_on_a_schedule_and_by_hand_never_on_pull_requests(self) -> None:
        triggers = workflow()["on"]
        self.assertEqual(set(triggers), {"schedule", "workflow_dispatch"})
        self.assertEqual(triggers["schedule"], [{"cron": "*/30 * * * *"}])

    def test_it_holds_only_the_permissions_its_calls_need(self) -> None:
        self.assertEqual(
            workflow()["permissions"], {"contents": "read", "issues": "write"}
        )

    def test_it_runs_no_repository_code_and_no_third_party_action(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("uses:", text)
        self.assertNotIn("actions/checkout", text)
        steps = workflow()["jobs"]["probe"]["steps"]
        self.assertEqual(
            [step["name"] for step in steps],
            [
                "Probar cada URL con su respuesta esperada",
                "Abrir, actualizar o cerrar el issue",
            ],
        )
        # Untrusted text reaches the shell only through the environment.
        self.assertNotIn("${{", steps[1]["run"])
        self.assertEqual(workflow()["jobs"]["probe"]["timeout-minutes"], 10)

    def test_every_live_public_name_is_probed_once(self) -> None:
        labels = [label for _, label, _ in checks()]
        self.assertEqual(len(labels), len(set(labels)))
        expected = {
            "monitor", "ax", "oficina", "kropia", "n8n", "minecraft-stats",
            "organizacion", "pablohurtadohg", "albertohidalgo", "passbolt",
            "racinggame", "satisfactory", "logs-satisfactory", "winnest",
            "generadorcodigosqr",
        }
        self.assertEqual(set(labels), expected)
        # Parked services answer 503 by design (config/platform.yml).
        parked = yaml.safe_load(PLATFORM.read_text(encoding="utf-8"))[
            "platform_parked_workloads"
        ]
        self.assertIn("openclaw", parked)
        self.assertNotIn("openclaw", labels)

    def test_the_ax_names_probe_the_panel_not_the_login(self) -> None:
        urls = {label: url for url, label, _ in checks()}
        self.assertEqual(urls["ax"], "https://ax.apptolast.com/healthz")
        self.assertEqual(urls["oficina"], "https://oficina.apptolast.com/healthz")

    def test_a_failure_edits_the_open_issue_instead_of_notifying_again(self) -> None:
        run = workflow()["jobs"]["probe"]["steps"][1]["run"]
        self.assertIn("gh issue edit", run)
        self.assertIn("gh issue create", run)
        self.assertIn("gh issue close", run)
        self.assertNotIn("gh issue comment", run)


if __name__ == "__main__":
    unittest.main()
