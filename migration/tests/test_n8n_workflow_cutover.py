from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPOSITORY_ROOT / "migration/scripts/manage_n8n_workflows.py"
SPEC = importlib.util.spec_from_file_location(
    "manage_n8n_workflows",
    SCRIPT_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot import n8n workflow cutover helper")
workflow_manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workflow_manager)


class N8nWorkflowCutoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.expected = [
            {"id": "alpha", "activeVersionId": "version-alpha"},
            {"id": "beta", "activeVersionId": "version-beta"},
        ]

    def test_inventory_requires_exact_sorted_id_and_version_fields(self) -> None:
        self.assertEqual(
            workflow_manager.validate_inventory(self.expected),
            self.expected,
        )
        with self.assertRaises(workflow_manager.WorkflowActivationError):
            workflow_manager.validate_inventory(list(reversed(self.expected)))
        with self.assertRaises(workflow_manager.WorkflowActivationError):
            workflow_manager.validate_inventory(
                [{"id": "alpha", "activeVersionId": "v", "active": True}]
            )

    def test_publication_is_idempotent_for_an_exact_subset(self) -> None:
        self.assertEqual(
            workflow_manager.publication_plan(
                [self.expected[0]],
                self.expected,
            ),
            [self.expected[1]],
        )
        self.assertEqual(
            workflow_manager.publication_plan(self.expected, self.expected),
            [],
        )

    def test_publication_rejects_version_or_scope_drift(self) -> None:
        with self.assertRaises(workflow_manager.WorkflowActivationError):
            workflow_manager.publication_plan(
                [{"id": "alpha", "activeVersionId": "wrong"}],
                self.expected,
            )
        with self.assertRaises(workflow_manager.WorkflowActivationError):
            workflow_manager.publication_plan(
                [{"id": "outside", "activeVersionId": "version"}],
                self.expected,
            )

    def test_rollback_only_accepts_audited_workflows(self) -> None:
        self.assertEqual(
            workflow_manager.rollback_plan([self.expected[1]], self.expected),
            ["beta"],
        )
        with self.assertRaises(workflow_manager.WorkflowActivationError):
            workflow_manager.rollback_plan(
                [{"id": "outside", "activeVersionId": "version"}],
                self.expected,
            )

    def test_stack_smoke_requires_every_exact_service_at_one_replica(self) -> None:
        output = "\n".join(
            f"workloads_{name}\t1/1"
            for name in sorted(workflow_manager.EXPECTED_STACK_SERVICES)
        )
        observed = workflow_manager.parse_stack_replicas(output, "workloads")
        self.assertEqual(
            set(observed),
            workflow_manager.EXPECTED_STACK_SERVICES,
        )
        with self.assertRaises(workflow_manager.WorkflowActivationError):
            workflow_manager.parse_stack_replicas(
                output.replace("workloads_n8n\t1/1", "workloads_n8n\t0/1"),
                "workloads",
            )

    def test_cli_uses_exact_version_and_never_interpolates_identifiers(self) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.argv: list[str] = []

            def run(
                self,
                argv,
                *,
                check: bool = True,
                redact_output: bool = False,
            ) -> subprocess.CompletedProcess[str]:
                self.argv = list(argv)
                self.redact_output = redact_output
                return subprocess.CompletedProcess(self.argv, 0, "", "")

        runner = FakeRunner()
        workflow_manager.n8n_cli(
            runner,
            "a" * 64,
            "publish:workflow",
            "workflow id with spaces",
            "version id with spaces",
        )
        shell = runner.argv[7]
        self.assertNotIn("workflow id with spaces", shell)
        self.assertIn("publish:workflow", runner.argv)
        self.assertIn("--id=workflow id with spaces", runner.argv)
        self.assertIn("--versionId=version id with spaces", runner.argv)
        self.assertNotIn("--all", runner.argv)

    def test_publish_requires_named_google_oauth_consent(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "I_HAVE_CONFIRMED_GOOGLE_OAUTH_CONSENT",
            source,
        )
        self.assertNotIn("publish:workflow --all", source)
        self.assertIn('"unpublish:workflow"', source)

    def test_public_smoke_is_bound_to_the_reviewed_target_ip(self) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.argv: list[str] = []

            def run(
                self,
                argv,
                *,
                check: bool = True,
            ) -> subprocess.CompletedProcess[str]:
                self.argv = list(argv)
                return subprocess.CompletedProcess(self.argv, 0, "", "")

        runner = FakeRunner()
        workflow_manager.public_n8n_smoke(runner, "159.195.156.57")
        self.assertIn(
            "n8n.apptolast.com:443:159.195.156.57",
            runner.argv,
        )
        self.assertIn("https://n8n.apptolast.com/healthz", runner.argv)
        self.assertEqual(
            runner.argv[runner.argv.index("--noproxy") + 1],
            "*",
        )

    def test_restored_workflow_security_accepts_only_current_dependencies(
        self,
    ) -> None:
        inventory = [
            {
                "id": "workflow",
                "nodes": [
                    {
                        "type": "n8n-nodes-base.httpRequest",
                        "parameters": {
                            "url": "https://api.example.com/v1",
                        },
                    },
                    {
                        "type": "n8n-nodes-base.httpRequest",
                        "parameters": {
                            "url": "http://selenium:4444/status",
                        },
                    },
                ],
            }
        ]
        self.assertEqual(
            workflow_manager.validate_workflow_security(inventory),
            {
                "workflowCount": 1,
                "nodeCount": 2,
                "endpointCount": 2,
            },
        )

    def test_restored_workflow_security_rejects_runner_internal_network_access(
        self,
    ) -> None:
        for endpoint in (
            "http://selenium:4444/status",
            "redis://redis-coordinator:6379",
        ):
            with self.subTest(endpoint=endpoint):
                inventory = [
                    {
                        "id": "workflow",
                        "nodes": [
                            {
                                "type": "n8n-nodes-base.code",
                                "parameters": {
                                    "jsCode": ("return await fetch(" f"'{endpoint}');"),
                                },
                            },
                        ],
                    }
                ]
                with self.assertRaisesRegex(
                    workflow_manager.WorkflowActivationError,
                    "outside the isolated external task runner network",
                ):
                    workflow_manager.validate_workflow_security(inventory)

    def test_restored_workflow_security_rejects_environment_access(self) -> None:
        inventory = [
            {
                "id": "workflow",
                "nodes": [
                    {
                        "type": "n8n-nodes-base.code",
                        "parameters": {
                            "jsCode": "return [{json: {key: $env.API_KEY}}];",
                        },
                    }
                ],
            }
        ]
        with self.assertRaisesRegex(
            workflow_manager.WorkflowActivationError,
            "environment-variable access",
        ):
            workflow_manager.validate_workflow_security(inventory)

    def test_restored_workflow_security_rejects_community_node(self) -> None:
        inventory = [
            {
                "id": "workflow",
                "nodes": [
                    {
                        "type": "n8n-nodes-unverified.example",
                        "parameters": {},
                    }
                ],
            }
        ]
        with self.assertRaisesRegex(
            workflow_manager.WorkflowActivationError,
            "non-official package",
        ):
            workflow_manager.validate_workflow_security(inventory)

    def test_restored_workflow_security_rejects_legacy_internal_host(
        self,
    ) -> None:
        inventory = [
            {
                "id": "workflow",
                "nodes": [
                    {
                        "type": "n8n-nodes-base.httpRequest",
                        "parameters": {
                            "url": "http://legacy-selenium:4444/status",
                        },
                    }
                ],
            }
        ]
        with self.assertRaisesRegex(
            workflow_manager.WorkflowActivationError,
            "internal endpoints",
        ):
            workflow_manager.validate_workflow_security(inventory)

    def test_live_security_audit_queries_without_exposing_workflow_values(
        self,
    ) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.argv: list[str] = []

            def run(
                self,
                argv,
                *,
                check: bool = True,
            ) -> subprocess.CompletedProcess[str]:
                self.argv = list(argv)
                payload = [
                    {
                        "id": "workflow",
                        "nodes": [
                            {
                                "type": "n8n-nodes-base.noOp",
                                "parameters": {},
                            }
                        ],
                    }
                ]
                return subprocess.CompletedProcess(
                    self.argv,
                    0,
                    json.dumps(payload),
                    "",
                )

        runner = FakeRunner()
        self.assertEqual(
            workflow_manager.audit_restored_workflows(
                runner,
                "a" * 64,
            )["workflowCount"],
            1,
        )
        self.assertIn("SELECT COALESCE(json_agg", runner.argv[-1])

    def test_credential_endpoint_auditor_emits_only_safe_summary(self) -> None:
        credentials = [
            {
                "id": "credential",
                "data": {
                    "host": "legacy-database",
                    "password": "must-never-appear",
                },
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "credentials.json"
            path.write_text(json.dumps(credentials), encoding="utf-8")
            result = subprocess.run(
                [
                    "node",
                    "-e",
                    workflow_manager.CREDENTIAL_ENDPOINT_AUDIT_JS,
                    path,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("must-never-appear", result.stdout + result.stderr)
        summary = json.loads(result.stderr)
        self.assertEqual(summary["validationViolationCount"], 1)
        self.assertEqual(summary["probeViolationCount"], 0)

    def test_live_credential_audit_uses_private_ephemeral_export(self) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.argv: list[str] = []

            def run(
                self,
                argv,
                *,
                check: bool = True,
                redact_output: bool = False,
            ) -> subprocess.CompletedProcess[str]:
                self.argv = list(argv)
                self.redact_output = redact_output
                summary = {
                    "schemaVersion": 1,
                    "credentialCount": 2,
                    "endpointCount": 1,
                    "validationViolationCount": 0,
                    "probeViolationCount": 0,
                }
                return subprocess.CompletedProcess(
                    self.argv,
                    0,
                    json.dumps(summary),
                    "",
                )

        runner = FakeRunner()
        summary = workflow_manager.audit_restored_credentials(
            runner,
            "a" * 64,
        )
        self.assertEqual(summary["credentialCount"], 2)
        shell = runner.argv[7]
        self.assertIn("umask 077", shell)
        self.assertIn("export:credentials --all --decrypted", shell)
        self.assertIn("trap cleanup EXIT", shell)
        self.assertNotIn("must-never-appear", " ".join(runner.argv))
        self.assertTrue(runner.redact_output)

    def test_sensitive_command_failure_output_is_redacted(self) -> None:
        failure = subprocess.CalledProcessError(
            2,
            ["docker", "container", "exec"],
            output="decrypted-secret-value",
            stderr="another-decrypted-secret",
        )
        with (
            mock.patch.object(
                workflow_manager.subprocess,
                "run",
                side_effect=failure,
            ),
            self.assertRaisesRegex(
                workflow_manager.WorkflowActivationError,
                "sensitive command output redacted",
            ) as raised,
        ):
            workflow_manager.CommandRunner().run(
                ["docker", "container", "exec"],
                redact_output=True,
            )
        self.assertNotIn("decrypted-secret", str(raised.exception))

    def test_restart_failure_attempts_and_records_exact_rollback(self) -> None:
        inventory_path = Path("/private/n8n-active-workflows.json")
        with (
            mock.patch.object(
                workflow_manager,
                "stack_smoke",
                return_value=("n8n-container", "db-container"),
            ),
            mock.patch.object(
                workflow_manager,
                "current_inventory",
                side_effect=[[], self.expected],
            ),
            mock.patch.object(workflow_manager, "n8n_cli"),
            mock.patch.object(
                workflow_manager,
                "force_restart_and_wait",
                side_effect=workflow_manager.WorkflowActivationError("restart failed"),
            ),
            mock.patch.object(
                workflow_manager,
                "rollback_transition",
                return_value=[],
            ) as rollback,
            mock.patch.object(
                workflow_manager,
                "write_evidence",
                return_value=Path("/private/rollback-evidence.json"),
            ) as evidence,
        ):
            with self.assertRaisesRegex(
                workflow_manager.WorkflowActivationError,
                "automatic exact rollback was verified",
            ):
                workflow_manager.publish(
                    workflow_manager.CommandRunner(),
                    stack_name="workloads",
                    expected=self.expected,
                    inventory_path=inventory_path,
                    expected_ipv4="159.195.156.57",
                    timeout=300,
                )
        rollback.assert_called_once()
        evidence.assert_called_once()

    def test_keyboard_interrupt_attempts_and_records_exact_rollback(self) -> None:
        inventory_path = Path("/private/n8n-active-workflows.json")
        with (
            mock.patch.object(
                workflow_manager,
                "stack_smoke",
                return_value=("n8n-container", "db-container"),
            ),
            mock.patch.object(
                workflow_manager,
                "current_inventory",
                return_value=[],
            ),
            mock.patch.object(
                workflow_manager,
                "n8n_cli",
                side_effect=KeyboardInterrupt,
            ),
            mock.patch.object(
                workflow_manager,
                "rollback_transition",
                return_value=[],
            ) as rollback,
            mock.patch.object(
                workflow_manager,
                "write_evidence",
                return_value=Path("/private/rollback-evidence.json"),
            ) as evidence,
        ):
            with self.assertRaisesRegex(
                workflow_manager.WorkflowActivationError,
                "automatic exact rollback was verified",
            ):
                workflow_manager.publish(
                    workflow_manager.CommandRunner(),
                    stack_name="workloads",
                    expected=self.expected,
                    inventory_path=inventory_path,
                    expected_ipv4="159.195.156.57",
                    timeout=300,
                )
        rollback.assert_called_once()
        evidence.assert_called_once()

    def test_transaction_signal_handlers_raise_a_rollback_error(self) -> None:
        handlers: dict[int, object] = {}

        def fake_signal(signal_number, handler):
            handlers[signal_number] = handler
            return workflow_manager.signal.SIG_DFL

        with mock.patch.object(
            workflow_manager.signal, "signal", side_effect=fake_signal
        ):
            with self.assertRaisesRegex(
                workflow_manager.WorkflowActivationError,
                "transactional rollback is required",
            ):
                with workflow_manager.transactional_signal_handlers():
                    handler = handlers[workflow_manager.signal.SIGTERM]
                    assert callable(handler)
                    handler(workflow_manager.signal.SIGTERM, None)

    def test_post_restart_inventory_drift_attempts_exact_rollback(self) -> None:
        with (
            mock.patch.object(
                workflow_manager,
                "stack_smoke",
                return_value=("n8n-container", "db-container"),
            ),
            mock.patch.object(
                workflow_manager,
                "current_inventory",
                side_effect=[[], self.expected, []],
            ),
            mock.patch.object(workflow_manager, "n8n_cli"),
            mock.patch.object(
                workflow_manager,
                "force_restart_and_wait",
                return_value=("new-n8n", "new-db"),
            ),
            mock.patch.object(
                workflow_manager,
                "rollback_transition",
                return_value=[],
            ) as rollback,
            mock.patch.object(
                workflow_manager,
                "write_evidence",
                return_value=Path("/private/rollback-evidence.json"),
            ),
        ):
            with self.assertRaisesRegex(
                workflow_manager.WorkflowActivationError,
                "automatic exact rollback was verified",
            ):
                workflow_manager.publish(
                    workflow_manager.CommandRunner(),
                    stack_name="workloads",
                    expected=self.expected,
                    inventory_path=Path("/private/n8n-active-workflows.json"),
                    expected_ipv4="159.195.156.57",
                    timeout=300,
                )
        rollback.assert_called_once()

    def test_rollback_failure_is_reported_explicitly(self) -> None:
        with (
            mock.patch.object(
                workflow_manager,
                "stack_smoke",
                return_value=("n8n-container", "db-container"),
            ),
            mock.patch.object(
                workflow_manager,
                "current_inventory",
                side_effect=[[], self.expected],
            ),
            mock.patch.object(workflow_manager, "n8n_cli"),
            mock.patch.object(
                workflow_manager,
                "force_restart_and_wait",
                side_effect=workflow_manager.WorkflowActivationError("restart failed"),
            ),
            mock.patch.object(
                workflow_manager,
                "rollback_transition",
                side_effect=workflow_manager.WorkflowActivationError(
                    "rollback restart failed"
                ),
            ),
            mock.patch.object(
                workflow_manager,
                "quarantine_n8n_service",
                return_value=Path("/private/n8n-workflows-quarantine.json"),
            ) as quarantine,
        ):
            with self.assertRaisesRegex(
                workflow_manager.WorkflowActivationError,
                "scaled to zero",
            ):
                workflow_manager.publish(
                    workflow_manager.CommandRunner(),
                    stack_name="workloads",
                    expected=self.expected,
                    inventory_path=Path("/private/n8n-active-workflows.json"),
                    expected_ipv4="159.195.156.57",
                    timeout=300,
                )
        quarantine.assert_called_once()

    def test_signal_mid_multi_workflow_rollback_is_deferred_until_empty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_directory = Path(temporary)
            inventory_path = state_directory / workflow_manager.INVENTORY_FILENAME
            inventory_path.write_text(json.dumps(self.expected), encoding="utf-8")
            runner = mock.Mock()
            runner.run.return_value = subprocess.CompletedProcess(
                [],
                0,
                "",
                "",
            )
            cli_calls: list[str] = []

            def interrupted_unpublish(
                _runner,
                _container,
                _command,
                workflow_id,
                _version_id=None,
            ):
                cli_calls.append(workflow_id)
                if len(cli_calls) == 1:
                    os.kill(os.getpid(), signal.SIGTERM)

            with (
                mock.patch.object(workflow_manager, "parse_stack_replicas"),
                mock.patch.object(
                    workflow_manager,
                    "unique_running_container",
                    side_effect=lambda _runner, service: (
                        "database-container"
                        if service.endswith("_n8n-db")
                        else "n8n-container"
                    ),
                ),
                mock.patch.object(
                    workflow_manager,
                    "current_inventory",
                    side_effect=[
                        self.expected,
                        self.expected,
                        [],
                        [],
                    ],
                ),
                mock.patch.object(
                    workflow_manager,
                    "n8n_cli",
                    side_effect=interrupted_unpublish,
                ),
                mock.patch.object(
                    workflow_manager,
                    "force_restart_and_wait",
                    return_value=("new-n8n", "database-container"),
                ),
                self.assertRaisesRegex(
                    workflow_manager.WorkflowActivationError,
                    "rollback completed after a deferred signal",
                ),
            ):
                workflow_manager.rollback(
                    runner,
                    stack_name="workloads",
                    expected=self.expected,
                    inventory_path=inventory_path,
                    expected_ipv4="159.195.156.57",
                    timeout=300,
                )
            self.assertEqual(cli_calls, ["alpha", "beta"])
            evidence = list(state_directory.glob("n8n-workflows-unpublished-*.json"))
            self.assertEqual(len(evidence), 1)
            self.assertEqual(
                json.loads(evidence[0].read_text(encoding="utf-8"))["activeInventory"],
                [],
            )

    def test_partial_unpublish_failure_retries_then_quarantines(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = subprocess.CompletedProcess([], 0, "", "")
        remaining = [self.expected[1]]

        def unpublish(
            _runner,
            _container,
            _command,
            workflow_id,
            _version_id=None,
        ):
            if workflow_id == "beta":
                raise workflow_manager.WorkflowActivationError(
                    "simulated persistent CLI failure"
                )

        with (
            mock.patch.object(workflow_manager, "parse_stack_replicas"),
            mock.patch.object(
                workflow_manager,
                "unique_running_container",
                side_effect=lambda _runner, service: (
                    "database-container"
                    if service.endswith("_n8n-db")
                    else "n8n-container"
                ),
            ),
            mock.patch.object(
                workflow_manager,
                "current_inventory",
                side_effect=[
                    self.expected,
                    self.expected,
                    remaining,
                    remaining,
                    remaining,
                    remaining,
                ],
            ),
            mock.patch.object(
                workflow_manager,
                "n8n_cli",
                side_effect=unpublish,
            ) as cli,
            mock.patch.object(
                workflow_manager,
                "quarantine_n8n_service",
                return_value=Path("/private/n8n-workflows-quarantine.json"),
            ) as quarantine,
            self.assertRaisesRegex(
                workflow_manager.WorkflowActivationError,
                "scaled to zero",
            ),
        ):
            workflow_manager.rollback(
                runner,
                stack_name="workloads",
                expected=self.expected,
                inventory_path=Path("/private/n8n-active-workflows.json"),
                expected_ipv4="159.195.156.57",
                timeout=300,
            )
        self.assertEqual(cli.call_count, 4)
        quarantine.assert_called_once()
        self.assertEqual(
            quarantine.call_args.kwargs["observed"],
            remaining,
        )

    def test_signal_during_automatic_rollback_is_deferred_and_evidenced(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_directory = Path(temporary)
            inventory_path = state_directory / workflow_manager.INVENTORY_FILENAME
            inventory_path.write_text(json.dumps(self.expected), encoding="utf-8")

            def interrupted_rollback(*_args, **_kwargs):
                os.kill(os.getpid(), signal.SIGTERM)
                return []

            with (
                mock.patch.object(
                    workflow_manager,
                    "stack_smoke",
                    return_value=("n8n-container", "db-container"),
                ),
                mock.patch.object(
                    workflow_manager,
                    "current_inventory",
                    return_value=[],
                ),
                mock.patch.object(
                    workflow_manager,
                    "n8n_cli",
                    side_effect=workflow_manager.WorkflowActivationError(
                        "publication command failed"
                    ),
                ),
                mock.patch.object(
                    workflow_manager,
                    "rollback_transition",
                    side_effect=interrupted_rollback,
                ),
                mock.patch.object(
                    workflow_manager,
                    "quarantine_n8n_service",
                ) as quarantine,
                self.assertRaisesRegex(
                    workflow_manager.WorkflowActivationError,
                    "deferredSignal=15",
                ),
            ):
                workflow_manager.publish(
                    workflow_manager.CommandRunner(),
                    stack_name="workloads",
                    expected=self.expected,
                    inventory_path=inventory_path,
                    expected_ipv4="159.195.156.57",
                    timeout=300,
                )
            quarantine.assert_not_called()
            evidence = list(
                state_directory.glob("n8n-workflows-automatic-unpublished-*.json")
            )
            self.assertEqual(len(evidence), 1)

    def test_quarantine_marker_precedes_verified_scale_to_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_directory = Path(temporary)
            inventory_path = state_directory / workflow_manager.INVENTORY_FILENAME
            inventory_path.write_text(json.dumps(self.expected), encoding="utf-8")
            runner = mock.Mock()
            runner.run.side_effect = [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "0\n", ""),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
            marker = workflow_manager.quarantine_n8n_service(
                runner,
                stack_name="workloads",
                inventory_path=inventory_path,
                observed=[self.expected[1]],
                cause=workflow_manager.WorkflowActivationError("failure"),
            )
            self.assertEqual(
                marker, state_directory / workflow_manager.QUARANTINE_FILENAME
            )
            document = json.loads(marker.read_text(encoding="utf-8"))
            self.assertTrue(document["requiresReviewedRecovery"])
            self.assertEqual(document["observedActiveInventory"], [self.expected[1]])
            commands = [call.args[0] for call in runner.run.call_args_list]
            self.assertIn("workloads_n8n=0", commands[0])
            self.assertEqual(commands[1][1:3], ["service", "inspect"])
            self.assertEqual(commands[2][1:3], ["service", "ps"])


if __name__ == "__main__":
    unittest.main()
