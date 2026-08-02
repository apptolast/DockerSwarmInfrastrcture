"""Adversarial offline tests for the shared host mutation lock."""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "scripts" / "host_global_operation_lock.py"
SPEC = importlib.util.spec_from_file_location(
    "host_global_operation_lock",
    MODULE_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load host-global operation-lock helper")
host_lock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host_lock)


class DirectHolder:
    def __init__(
        self,
        lock_path: Path,
        marker_path: Path,
        operation: str = "offline-test",
        supervisor_pid: int | None = None,
    ) -> None:
        self.lock_path = lock_path
        self.marker_path = marker_path
        self.operation = operation
        self.operation_id = "a" * 64
        self.supervisor_pid = supervisor_pid or os.getpid()
        self.control_read, self.control_write = os.pipe()
        self.status_read, self.status_write = os.pipe()
        self.pid = os.fork()
        if self.pid == 0:
            try:
                os.close(self.control_write)
                os.close(self.status_read)
                result = host_lock.hold_direct_lock(
                    operation=self.operation,
                    operation_id=self.operation_id,
                    supervisor_pid=self.supervisor_pid,
                    control_descriptor=self.control_read,
                    status_descriptor=self.status_write,
                    lock_path=self.lock_path,
                    marker_path=self.marker_path,
                )
            except BaseException:
                result = 1
            finally:
                os.close(self.control_read)
                os.close(self.status_write)
            os._exit(result)
        os.close(self.control_read)
        os.close(self.status_write)
        self.ready = host_lock.wait_status(self.status_read)

    def release(self) -> tuple[str, int]:
        os.write(
            self.control_write,
            f"RELEASE:{self.operation_id}\n".encode("ascii"),
        )
        status = host_lock.wait_status(self.status_read)
        os.close(self.control_write)
        os.close(self.status_read)
        waited, wait_status = os.waitpid(self.pid, 0)
        if waited != self.pid:
            raise AssertionError("unexpected holder PID")
        return status, wait_status


class AnsibleHolder:
    operation_id = "b" * 64
    source_revision = "c" * 40
    contract_sha256 = "d" * 64
    playbook = "backup"
    profile = "production"
    mode = "apply"
    controller = "test@controller"

    def __init__(self, lock_path: Path, marker_path: Path) -> None:
        self.marker_path = marker_path
        helper_path = PROJECT_ROOT / "scripts" / "ansible-operation-lock.py"
        self.process = subprocess.Popen(
            [
                sys.executable,
                os.fspath(helper_path),
                "hold",
                "--lock-path",
                os.fspath(lock_path),
                "--marker-path",
                os.fspath(marker_path),
                "--owner-uid",
                str(os.geteuid()),
                "--owner-gid",
                str(os.getegid()),
                "--operation-id",
                self.operation_id,
                "--source-revision",
                self.source_revision,
                "--contract-sha256",
                self.contract_sha256,
                "--playbook",
                self.playbook,
                "--profile",
                self.profile,
                "--mode",
                self.mode,
                "--controller",
                self.controller,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self.process.stdout is not None
        self.ready = self.process.stdout.readline().strip()

    def proof_environment(self) -> dict[str, str]:
        return {
            host_lock.PROOF_SCOPE_ENV: "ansible",
            host_lock.ANSIBLE_PROOF_FIELDS["operation_id"]: self.operation_id,
            host_lock.ANSIBLE_PROOF_FIELDS["source_revision"]: self.source_revision,
            host_lock.ANSIBLE_PROOF_FIELDS["contract_sha256"]: self.contract_sha256,
            host_lock.ANSIBLE_PROOF_FIELDS["playbook"]: self.playbook,
            host_lock.ANSIBLE_PROOF_FIELDS["profile"]: self.profile,
            host_lock.ANSIBLE_PROOF_FIELDS["mode"]: self.mode,
            host_lock.ANSIBLE_PROOF_FIELDS["controller"]: self.controller,
        }

    def release(self) -> int:
        assert self.process.stdin is not None
        self.process.stdin.write(f"RELEASE:{self.operation_id}\n")
        self.process.stdin.flush()
        self.process.stdin.close()
        result = self.process.wait(timeout=5)
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self.process.stdout.close()
        self.process.stderr.close()
        return result


class HostGlobalOperationLockTests(unittest.TestCase):
    def private_paths(self, root: Path) -> tuple[Path, Path]:
        root.chmod(0o700)
        return root / "iac.lock", root / "direct.marker"

    def test_overlapping_holder_is_rejected_on_the_same_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path, marker_path = self.private_paths(Path(temporary))
            holder = DirectHolder(
                lock_path,
                marker_path,
                supervisor_pid=9_999_999,
            )
            self.assertEqual(holder.ready, f"LOCKED:{holder.operation_id}")
            with self.assertRaisesRegex(
                host_lock.HostGlobalLockError,
                "another host-global Ansible operation is active",
            ):
                host_lock.open_and_acquire_global_lock(
                    lock_path,
                    marker_path,
                    create=True,
                )
            released, status = holder.release()
            self.assertEqual(released, f"RELEASED:{holder.operation_id}")
            self.assertEqual(status, 0)
            self.assertFalse(marker_path.exists())

    def test_inode_replacement_cannot_bypass_the_stale_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path, marker_path = self.private_paths(root)
            holder = DirectHolder(
                lock_path,
                marker_path,
                supervisor_pid=9_999_999,
            )
            self.assertEqual(holder.ready, f"LOCKED:{holder.operation_id}")
            original_inode = lock_path.stat().st_ino
            lock_path.unlink()
            lock_path.write_bytes(b"replacement")
            lock_path.chmod(0o600)
            self.assertNotEqual(original_inode, lock_path.stat().st_ino)
            with self.assertRaisesRegex(
                host_lock.HostGlobalLockError,
                "operation marker exists",
            ):
                host_lock.open_and_acquire_global_lock(
                    lock_path,
                    marker_path,
                    create=False,
                )
            _released, status = holder.release()
            self.assertNotEqual(status, 0)
            self.assertTrue(marker_path.is_file())

            archive = root / "archive"
            confirmation = host_lock.recover_direct_marker(
                apply=False,
                confirmation=None,
                lock_path=lock_path,
                marker_path=marker_path,
                archive_directory=archive,
            )
            archived = host_lock.recover_direct_marker(
                apply=True,
                confirmation=confirmation,
                lock_path=lock_path,
                marker_path=marker_path,
                archive_directory=archive,
            )
            self.assertFalse(marker_path.exists())
            self.assertTrue(Path(archived).is_file())

    def test_direct_child_requires_live_holder_marker_and_supervisor_tree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path, marker_path = self.private_paths(Path(temporary))
            holder = DirectHolder(lock_path, marker_path)
            proof = {
                host_lock.PROOF_SCOPE_ENV: "direct",
                host_lock.DIRECT_PROOF_FIELDS["operation_id"]: holder.operation_id,
                host_lock.DIRECT_PROOF_FIELDS["operation"]: holder.operation,
                host_lock.DIRECT_PROOF_FIELDS["holder_pid"]: str(holder.pid),
                host_lock.DIRECT_PROOF_FIELDS["supervisor_pid"]: str(
                    holder.supervisor_pid
                ),
            }
            with mock.patch.dict(os.environ, proof, clear=False):
                host_lock.prove_direct_environment(
                    holder.operation,
                    lock_path=lock_path,
                    marker_path=marker_path,
                )
            released, status = holder.release()
            self.assertEqual(released, f"RELEASED:{holder.operation_id}")
            self.assertEqual(status, 0)

    def test_verified_direct_tree_can_nest_a_different_operation_name(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path, marker_path = self.private_paths(Path(temporary))
            holder = DirectHolder(
                lock_path,
                marker_path,
                operation="outer-cutover",
            )
            proof = {
                host_lock.PROOF_SCOPE_ENV: "direct",
                host_lock.DIRECT_PROOF_FIELDS["operation_id"]: holder.operation_id,
                host_lock.DIRECT_PROOF_FIELDS["operation"]: holder.operation,
                host_lock.DIRECT_PROOF_FIELDS["holder_pid"]: str(holder.pid),
                host_lock.DIRECT_PROOF_FIELDS["supervisor_pid"]: str(
                    holder.supervisor_pid
                ),
            }
            with mock.patch.dict(os.environ, proof, clear=False):
                host_lock.prove_direct_environment(
                    "nested-dns-or-runtime-step",
                    lock_path=lock_path,
                    marker_path=marker_path,
                )
            released, status = holder.release()
            self.assertEqual(released, f"RELEASED:{holder.operation_id}")
            self.assertEqual(status, 0)

    def test_partial_or_boolean_only_environment_never_bypasses(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                host_lock.DIRECT_PROOF_FIELDS["operation_id"]: "b" * 64,
                "DOCKERSWARM_IAC_LOCK_HELD": "1",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(
                host_lock.HostGlobalLockError,
                "partial lock proof",
            ):
                host_lock.prove_or_none("offline-test")

    def test_nonzero_child_retains_marker_and_live_supervisor_blocks_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path, marker_path = self.private_paths(root)
            result = host_lock.run_direct(
                "offline-nonzero",
                ["/bin/sh", "-c", "exit 7"],
                lock_path=lock_path,
                marker_path=marker_path,
            )
            self.assertEqual(result, 7)
            self.assertTrue(marker_path.is_file())
            with self.assertRaisesRegex(
                host_lock.HostGlobalLockError,
                "operation marker exists",
            ):
                host_lock.open_and_acquire_global_lock(
                    lock_path,
                    marker_path,
                    create=False,
                )
            with self.assertRaisesRegex(
                host_lock.HostGlobalLockError,
                "supervisor is still present",
            ):
                host_lock.recover_direct_marker(
                    apply=False,
                    confirmation=None,
                    lock_path=lock_path,
                    marker_path=marker_path,
                    archive_directory=root / "archive",
                )

    def test_successful_child_is_the_only_automatic_marker_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path, marker_path = self.private_paths(Path(temporary))
            result = host_lock.run_direct(
                "offline-success",
                ["/bin/true"],
                lock_path=lock_path,
                marker_path=marker_path,
            )
            self.assertEqual(result, 0)
            self.assertFalse(marker_path.exists())

    def test_direct_and_ansible_operations_exclude_on_the_same_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path, direct_marker = self.private_paths(root)
            ansible_marker = root / "ansible.marker"
            holder = AnsibleHolder(lock_path, ansible_marker)
            self.assertEqual(holder.ready, f"LOCKED:{holder.operation_id}")
            try:
                with self.assertRaisesRegex(
                    host_lock.HostGlobalLockError,
                    "another host-global Ansible operation is active",
                ):
                    host_lock.open_and_acquire_global_lock(
                        lock_path,
                        direct_marker,
                        create=True,
                    )
            finally:
                self.assertEqual(holder.release(), 0)
            self.assertFalse(ansible_marker.exists())

    def test_copied_ansible_environment_outside_its_tree_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path, _direct_marker = self.private_paths(root)
            ansible_marker = root / "ansible.marker"
            holder = AnsibleHolder(lock_path, ansible_marker)
            self.assertEqual(holder.ready, f"LOCKED:{holder.operation_id}")
            try:
                with (
                    mock.patch.dict(
                        os.environ,
                        holder.proof_environment(),
                        clear=True,
                    ),
                    self.assertRaisesRegex(
                        host_lock.HostGlobalLockError,
                        "outside a live Ansible execution tree",
                    ),
                ):
                    host_lock.prove_ansible_environment(
                        "ansible",
                        lock_path=lock_path,
                        marker_path=ansible_marker,
                        owner_uid=os.geteuid(),
                        owner_gid=os.getegid(),
                    )
            finally:
                self.assertEqual(holder.release(), 0)

    def test_live_ansible_execution_tree_can_use_the_exact_nested_proof(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path, _direct_marker = self.private_paths(root)
            ansible_marker = root / "ansible.marker"
            holder = AnsibleHolder(lock_path, ansible_marker)
            self.assertEqual(holder.ready, f"LOCKED:{holder.operation_id}")
            driver_directory = root / ".ansible/tmp/ansible-tmp-offline"
            driver_directory.mkdir(parents=True)
            driver = driver_directory / "AnsiballZ_command.py"
            driver.write_text(
                "\n".join(
                    [
                        "import importlib.util",
                        "from pathlib import Path",
                        "import sys",
                        f"module_path = Path({str(MODULE_PATH)!r})",
                        "spec = importlib.util.spec_from_file_location("
                        "'offline_host_lock', module_path)",
                        "module = importlib.util.module_from_spec(spec)",
                        "spec.loader.exec_module(module)",
                        "module.prove_ansible_environment(",
                        "    'ansible',",
                        "    lock_path=Path(sys.argv[1]),",
                        "    marker_path=Path(sys.argv[2]),",
                        "    owner_uid=int(sys.argv[3]),",
                        "    owner_gid=int(sys.argv[4]),",
                        ")",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        os.fspath(driver),
                        os.fspath(lock_path),
                        os.fspath(ansible_marker),
                        str(os.geteuid()),
                        str(os.getegid()),
                    ],
                    env={**os.environ, **holder.proof_environment()},
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
            finally:
                self.assertEqual(holder.release(), 0)

    def test_signal_cleanup_longer_than_two_seconds_finishes_but_marker_stays(
        self,
    ) -> None:
        for signal_number, expected_exit in (
            (signal.SIGINT, 130),
            (signal.SIGTERM, 143),
        ):
            with self.subTest(signal=signal_number):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    lock_path, marker_path = self.private_paths(root)
                    ready = root / "ready"
                    cleanup_started = root / "cleanup-started"
                    cleanup_finished = root / "cleanup-finished"
                    action = root / "transaction.py"
                    action.write_text(
                        "\n".join(
                            [
                                "from pathlib import Path",
                                "import signal",
                                "import time",
                                f"ready = Path({str(ready)!r})",
                                f"started = Path({str(cleanup_started)!r})",
                                f"finished = Path({str(cleanup_finished)!r})",
                                "def cleanup(_signal, _frame):",
                                "    started.write_text('started')",
                                "    time.sleep(2.5)",
                                "    finished.write_text('finished')",
                                "    raise SystemExit(0)",
                                "signal.signal(signal.SIGINT, cleanup)",
                                "signal.signal(signal.SIGTERM, cleanup)",
                                "ready.write_text('ready')",
                                "while True:",
                                "    time.sleep(0.05)",
                            ]
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    supervisor = os.fork()
                    if supervisor == 0:
                        try:
                            result = host_lock.run_direct(
                                "offline-signal",
                                [sys.executable, os.fspath(action)],
                                lock_path=lock_path,
                                marker_path=marker_path,
                            )
                        except BaseException:
                            result = 126
                        os._exit(result)
                    for _ in range(100):
                        if ready.exists():
                            break
                        time.sleep(0.05)
                    self.assertTrue(ready.exists())
                    os.kill(supervisor, signal_number)
                    waited, status = os.waitpid(supervisor, 0)
                    self.assertEqual(waited, supervisor)
                    self.assertTrue(os.WIFEXITED(status))
                    self.assertEqual(os.WEXITSTATUS(status), expected_exit)
                    self.assertTrue(cleanup_started.is_file())
                    self.assertTrue(cleanup_finished.is_file())
                    self.assertTrue(marker_path.is_file())

    def test_backup_signal_grace_covers_sequential_recovery_bound(self) -> None:
        self.assertGreaterEqual(host_lock.BACKUP_SIGNAL_GRACE_SECONDS, 2100)
        self.assertLessEqual(host_lock.BACKUP_SIGNAL_GRACE_SECONDS, 3600)
        for operation in (
            "backupctl-application",
            "backupctl-rehearse",
            "backupctl-restore",
            "backupctl-swarm-state",
            "backupctl-verify",
        ):
            self.assertEqual(
                host_lock.OPERATION_SIGNAL_GRACE_SECONDS[operation],
                host_lock.BACKUP_SIGNAL_GRACE_SECONDS,
            )

    def test_every_reviewed_direct_entrypoint_has_a_fail_closed_route(
        self,
    ) -> None:
        shell_operations = {
            "scripts/install-cloudflare-secret.sh": "cloudflare-secret-install",
            "scripts/backup-provision-secrets.sh": "backup-provision-secrets",
            "scripts/install-daemon-config.sh": "docker-daemon-config-install",
            "scripts/install-logrotate-config.sh": "logrotate-config-install",
            "scripts/smoke-test.sh": "swarm-smoke-test",
            "migration/scripts/restore_databases.sh": "migration-restore-${phase}",
            "migration/scripts/finalize_restore.sh": "migration-finalize-restore",
            "migration/scripts/install_traefik_acme.sh": (
                "migration-install-traefik-acme"
            ),
        }
        for relative, operation in shell_operations.items():
            content = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("host_global_operation_lock.py", content, relative)
            self.assertIn("prove --operation", content, relative)
            self.assertIn("run --operation", content, relative)
            self.assertIn(operation, content, relative)

        python_operations = {
            "backup/backupctl.py": "backupctl-{arguments.command}",
            "scripts/gc-docker-configs.py": "docker-config-gc",
            "scripts/install-workload-secrets.py": "workload-secret-install",
            "scripts/install-observability-secrets.py": (
                "observability-secret-install"
            ),
            "scripts/provision-observability-db-users.py": (
                "observability-db-provision"
            ),
            "scripts/manage-n8n-runner-image.py": "n8n-runner-image-reconcile",
            "scripts/reconcile-authorized-keys.py": "authorized-keys-reconcile",
            "migration/scripts/prepare_runtime.py": "migration-prepare-runtime",
            "migration/scripts/upgrade_secret_identity_contract.py": (
                "migration-secret-identity-{args.action}"
            ),
            "migration/scripts/manage_n8n_workflows.py": (
                "n8n-workflows-{args.action}"
            ),
        }
        for relative, operation in python_operations.items():
            content = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("ensure_mutation_lock(", content, relative)
            self.assertIn(operation, content, relative)

    def test_docker_runtime_validations_lock_active_swarm_mutations(
        self,
    ) -> None:
        helper = (
            PROJECT_ROOT / "scripts/host-global-docker-validation-lock.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("DOCKERSWARM_IAC_LOCK_SCOPE", helper)
        self.assertIn("prove --operation", helper)
        self.assertIn("run --operation", helper)
        self.assertIn("Swarm.LocalNodeState", helper)
        self.assertIn("inactive)", helper)
        self.assertIn("active|pending|locked|error)", helper)

        routed_operations = {
            "scripts/lint.sh": "repository-lint-docker",
            "scripts/validate-iac.sh": "iac-docker-validation",
            "scripts/validate-observability.sh": ("observability-docker-validation"),
            "scripts/smoke-observability-runtime.sh": ("observability-runtime-smoke"),
            "scripts/validate-traefik-config.sh": ("traefik-docker-validation"),
        }
        for relative, operation in routed_operations.items():
            content = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            self.assertIn(
                "host-global-docker-validation-lock.sh",
                content,
                relative,
            )
            self.assertIn("ensure_docker_validation_lock", content, relative)
            self.assertIn(operation, content, relative)

    def test_ansible_installs_helpers_and_exports_only_strict_proof(self) -> None:
        guard = (
            PROJECT_ROOT / "ansible/roles/operation_lock_guard/tasks/main.yml"
        ).read_text(encoding="utf-8")
        for field in (
            "DOCKERSWARM_IAC_LOCK_SCOPE",
            "DOCKERSWARM_IAC_OPERATION_ID",
            "DOCKERSWARM_IAC_SOURCE_REVISION",
            "DOCKERSWARM_IAC_CONTRACT_SHA256",
            "DOCKERSWARM_IAC_PLAYBOOK",
            "DOCKERSWARM_IAC_PROFILE",
            "DOCKERSWARM_IAC_MODE",
            "DOCKERSWARM_IAC_CONTROLLER",
        ):
            self.assertIn(field, guard)
        self.assertNotIn("DOCKERSWARM_IAC_LOCK_HELD", guard)

        for relative in (
            "ansible/roles/platform/tasks/main.yml",
            "ansible/roles/backup/tasks/main.yml",
            "ansible/roles/observability/tasks/deploy.yml",
            "ansible/roles/workloads/tasks/render.yml",
            "ansible/roles/workloads_runner_image/tasks/main.yml",
        ):
            content = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            for component in (
                "ansible-operation-lock.py",
                "host_global_operation_lock.py",
                "run-locked-command.py",
            ):
                self.assertIn(component, content, relative)
        backup_tasks = (PROJECT_ROOT / "ansible/roles/backup/tasks/main.yml").read_text(
            encoding="utf-8"
        )
        observability_tasks = (
            PROJECT_ROOT / "ansible/roles/observability/tasks/deploy.yml"
        ).read_text(encoding="utf-8")
        self.assertGreaterEqual(
            backup_tasks.count('environment: "{{ operation_lock_guard_environment }}"'),
            2,
        )
        self.assertIn(
            'environment: "{{ operation_lock_guard_environment }}"',
            observability_tasks,
        )


if __name__ == "__main__":
    unittest.main()
