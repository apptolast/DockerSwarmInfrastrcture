"""Adversarial tests for the host-global Ansible operation lock."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER = PROJECT_ROOT / "scripts/ansible-operation-lock.py"
LOCKED_RUNNER = PROJECT_ROOT / "scripts/run-locked-command.py"
WRAPPER = PROJECT_ROOT / "scripts/deploy-ansible.sh"
GUARD = PROJECT_ROOT / "ansible/roles/operation_lock_guard/tasks/main.yml"


def load_helper():
    spec = importlib.util.spec_from_file_location(
        "ansible_operation_lock",
        HELPER,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot import operation-lock helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = load_helper()


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_locked_command",
        LOCKED_RUNNER,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot import locked-command runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load_runner()


class AnsibleOperationLockTests(unittest.TestCase):
    operation_id = "a" * 64
    source_revision = "b" * 40
    contract_sha256 = "c" * 64

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.lock_path = self.root / "operation.lock"
        self.marker_path = self.root / "operation.marker"
        self.archive_directory = self.root / "archive"
        self.holders: list[subprocess.Popen[str]] = []

    def tearDown(self) -> None:
        for holder in self.holders:
            if holder.poll() is None:
                holder.kill()
            holder.communicate(timeout=5)
        self.temporary.cleanup()

    def command(
        self,
        operation_id: str | None = None,
        marker_path: Path | None = None,
    ) -> list[str]:
        return [
            sys.executable,
            os.fspath(HELPER),
            "hold",
            "--lock-path",
            os.fspath(self.lock_path),
            "--marker-path",
            os.fspath(marker_path or self.marker_path),
            "--owner-uid",
            str(os.geteuid()),
            "--owner-gid",
            str(os.getegid()),
            "--operation-id",
            operation_id or self.operation_id,
            "--source-revision",
            self.source_revision,
            "--contract-sha256",
            self.contract_sha256,
            "--playbook",
            "platform",
            "--profile",
            "production",
            "--mode",
            "apply",
            "--controller",
            "test@controller",
        ]

    def start_holder(
        self,
        operation_id: str | None = None,
    ) -> subprocess.Popen[str]:
        holder = subprocess.Popen(
            self.command(operation_id),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.holders.append(holder)
        assert holder.stdout is not None
        handshake = holder.stdout.readline().strip()
        self.assertEqual(
            handshake,
            f"LOCKED:{operation_id or self.operation_id}",
            holder.stderr.read() if holder.poll() is not None else "",
        )
        return holder

    def prove_command(self, operation_id: str | None = None) -> list[str]:
        command = self.command(operation_id)
        command[2] = "prove"
        return command

    def recovery_args(
        self,
        *,
        apply: bool,
        confirm: str = "",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            operation_id=self.operation_id,
            lock_path=self.lock_path,
            marker_path=self.marker_path,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
            archive_directory=self.archive_directory,
            apply=apply,
            confirm=confirm,
        )

    def test_hold_prove_release_and_second_holder_exclusion(self) -> None:
        first = self.start_holder()
        proof = subprocess.run(
            self.prove_command(),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proof.stdout.strip(), f"PROVEN:{self.operation_id}")

        second = subprocess.run(
            self.command("d" * 64),
            input="",
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("another host-global", second.stderr)

        assert first.stdin is not None
        assert first.stdout is not None
        first.stdin.write(f"RELEASE:{self.operation_id}\n")
        first.stdin.flush()
        self.assertEqual(
            first.stdout.readline().strip(),
            f"RELEASED:{self.operation_id}",
        )
        self.assertEqual(first.wait(timeout=5), 0)
        self.assertFalse(self.marker_path.exists())

        later = self.start_holder("e" * 64)
        assert later.stdin is not None
        later.stdin.write(f"RELEASE:{'e' * 64}\n")
        later.stdin.flush()
        self.assertEqual(later.wait(timeout=5), 0)

    def test_bootstrap_and_normal_scopes_share_one_active_mutex(self) -> None:
        first = self.start_holder()
        peer_marker = self.root / "bootstrap.marker"
        second = subprocess.run(
            self.command("d" * 64, peer_marker),
            input="",
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("another host-global", second.stderr)
        self.assertFalse(peer_marker.exists())
        assert first.stdin is not None
        first.stdin.write(f"RELEASE:{self.operation_id}\n")
        first.stdin.flush()
        self.assertEqual(first.wait(timeout=5), 0)

    def test_stale_peer_scope_marker_blocks_a_new_operation(self) -> None:
        peer_marker = self.root / "bootstrap.marker"
        peer_marker.write_text("{}\n", encoding="ascii")
        with (
            mock.patch.object(helper, "DEFAULT_MARKER_PATH", self.marker_path),
            mock.patch.object(
                helper,
                "BOOTSTRAP_MARKER_PATH",
                peer_marker,
            ),
        ):
            with self.assertRaisesRegex(
                helper.OperationLockError,
                "peer operation marker",
            ):
                helper.refuse_peer_production_marker(self.marker_path)

    def test_holder_death_retains_marker_and_requires_exact_recovery(
        self,
    ) -> None:
        holder = self.start_holder()
        holder.kill()
        self.assertNotEqual(holder.wait(timeout=5), 0)
        self.assertTrue(self.marker_path.exists())

        blocked = subprocess.run(
            self.command("d" * 64),
            input="",
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("stale operation marker", blocked.stderr)

        raw = self.marker_path.read_bytes()
        confirmation = (
            f"RECOVER_ANSIBLE_LOCK:{self.operation_id}:"
            f"{helper.hashlib.sha256(raw).hexdigest()}:CONTROLLER_STOPPED"
        )
        with self.assertRaises(helper.OperationLockError):
            helper.recover_lock(
                self.recovery_args(apply=True, confirm="wrong"),
                require_root=False,
            )
        helper.recover_lock(
            self.recovery_args(apply=True, confirm=confirmation),
            require_root=False,
        )
        self.assertFalse(self.marker_path.exists())
        archives = list(self.archive_directory.iterdir())
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), raw)

    def test_wrong_nonce_and_unheld_lock_are_rejected(self) -> None:
        holder = self.start_holder()
        wrong = subprocess.run(
            self.prove_command("d" * 64),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("nonce differs", wrong.stderr)

        wrong_mode_command = self.prove_command()
        wrong_mode_command[wrong_mode_command.index("--mode") + 1] = "check"
        wrong_mode = subprocess.run(
            wrong_mode_command,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(wrong_mode.returncode, 0)
        self.assertIn("mode differs", wrong_mode.stderr)

        holder.kill()
        holder.wait(timeout=5)
        unheld = subprocess.run(
            self.prove_command(),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(unheld.returncode, 0)
        self.assertIn("not actively held", unheld.stderr)

    def test_symlink_hardlink_mode_and_path_aliases_fail_closed(self) -> None:
        self.lock_path.symlink_to(self.root / "missing")
        symlink = subprocess.run(
            self.command(),
            input="",
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(symlink.returncode, 0)
        self.lock_path.unlink()

        self.lock_path.write_text("", encoding="ascii")
        self.lock_path.chmod(0o600)
        hardlink = self.root / "second-link"
        os.link(self.lock_path, hardlink)
        linked = subprocess.run(
            self.command(),
            input="",
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(linked.returncode, 0)
        hardlink.unlink()

        self.lock_path.chmod(0o644)
        wrong_mode = subprocess.run(
            self.command(),
            input="",
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(wrong_mode.returncode, 0)

        with self.assertRaises(helper.OperationLockError):
            helper.require_paths(
                self.lock_path,
                self.lock_path,
                os.geteuid(),
                os.getegid(),
            )

    def test_production_bootstrap_lock_has_immutable_root_identity(self) -> None:
        helper.require_paths(
            helper.BOOTSTRAP_LOCK_PATH,
            helper.BOOTSTRAP_MARKER_PATH,
            0,
            0,
        )
        with self.assertRaises(helper.OperationLockError):
            helper.require_paths(
                helper.BOOTSTRAP_LOCK_PATH,
                helper.BOOTSTRAP_MARKER_PATH,
                1001,
                1001,
            )
        helper.require_metadata(
            self.operation_id,
            self.source_revision,
            self.contract_sha256,
            "bootstrap-host",
            "fresh-host",
            "apply",
            "test@controller",
        )
        with self.assertRaises(helper.OperationLockError):
            helper.require_metadata(
                self.operation_id,
                self.source_revision,
                self.contract_sha256,
                "platform",
                "fresh-host",
                "apply",
                "test@controller",
            )

    def test_recovery_does_not_create_an_absent_lock(self) -> None:
        self.assertFalse(self.lock_path.exists())
        with self.assertRaises(helper.OperationLockError):
            helper.recover_lock(
                self.recovery_args(apply=False),
                require_root=False,
            )
        self.assertFalse(self.lock_path.exists())

    def test_recovery_rejects_a_visible_mutating_process(self) -> None:
        holder = self.start_holder()
        holder.kill()
        holder.wait(timeout=5)
        mutator = subprocess.Popen(
            ["bash", "-c", "exec -a apt sleep 30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(50):
                if any(name == "apt" for _pid, name in helper.mutating_processes()):
                    break
                time.sleep(0.02)
            with self.assertRaisesRegex(
                helper.OperationLockError,
                "mutating host processes are still active",
            ):
                helper.recover_lock(
                    self.recovery_args(apply=False),
                    require_root=False,
                )
        finally:
            mutator.terminate()
            mutator.wait(timeout=5)

    def test_wrapper_and_every_mutating_playbook_require_the_guard(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("Deployment operation ID:", wrapper)
        self.assertIn("operation_lock_guard_operation_id=", wrapper)
        self.assertIn("marker retained", wrapper)
        self.assertIn("ServerAliveInterval=15", wrapper)
        self.assertIn("ControlMaster=no", wrapper)
        self.assertIn("StrictHostKeyChecking=yes", wrapper)
        self.assertIn("run-locked-command.py", wrapper)
        self.assertIn(
            '"${SUDO_BIN}"\n    --preserve-env=ANSIBLE_CONFIG\n'
            '    --\n    "${locked_runner_command[@]}"',
            wrapper,
        )
        self.assertNotIn(
            '"${SUDO_BIN}"\n      --preserve-env=ANSIBLE_CONFIG\n'
            '      --\n      "${ANSIBLE_PLAYBOOK}"',
            wrapper,
        )
        self.assertGreaterEqual(wrapper.count("status --porcelain"), 2)
        ansible_configuration = (PROJECT_ROOT / "ansible/ansible.cfg").read_text(
            encoding="utf-8"
        )
        self.assertIn("ControlMaster=no", ansible_configuration)
        self.assertNotIn("ControlPersist", ansible_configuration)

        guard = GUARD.read_text(encoding="utf-8")
        self.assertIn("ansible-operation-lock.py", guard)
        self.assertIn("check_mode: false", guard)
        for name in (
            "platform",
            "host-baseline",
            "preflight-images",
            "edge",
            "workloads",
            "observability",
            "backup",
            "site",
        ):
            playbook = (PROJECT_ROOT / f"ansible/playbooks/{name}.yml").read_text(
                encoding="utf-8"
            )
            self.assertLess(
                playbook.index("role: operation_lock_guard"),
                playbook.index("role: deployment_metadata"),
            )
        for name in (
            "bootstrap-host",
            "bootstrap-host-security",
            "bootstrap-host-ssh-final",
        ):
            playbook = (PROJECT_ROOT / f"ansible/playbooks/{name}.yml").read_text(
                encoding="utf-8"
            )
            self.assertIn("operation_lock_guard", playbook)
            self.assertIn("fresh-host-bootstrap", playbook)

    def test_locked_runner_supports_a_real_tty_prompt(self) -> None:
        watcher = subprocess.Popen(["sleep", "30"])
        try:
            prompt = (
                "import sys; "
                "writer=open('/dev/tty','w'); reader=open('/dev/tty','r'); "
                "writer.write('Secret: '); writer.flush(); "
                "value=reader.readline().strip(); "
                "print('PROMPT_OK' if value == 'test-value' else 'PROMPT_BAD'); "
                "sys.exit(0 if value == 'test-value' else 9)"
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    os.fspath(LOCKED_RUNNER),
                    "--watch-pid",
                    str(watcher.pid),
                    "--",
                    sys.executable,
                    "-c",
                    prompt,
                ],
                input="test-value\n",
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(
                completed.returncode,
                0,
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
            )
            self.assertIn("PROMPT_OK", completed.stdout)
        finally:
            watcher.terminate()
            watcher.wait(timeout=5)

    def test_holder_loss_kills_the_complete_guarded_process_group(self) -> None:
        watcher = subprocess.Popen(["sleep", "0.4"])
        child_pid_path = self.root / "child.pid"
        command = (
            "import pathlib, subprocess; "
            "child=subprocess.Popen(['sleep','30']); "
            f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); "
            "child.wait()"
        )
        completed = subprocess.run(
            [
                sys.executable,
                os.fspath(LOCKED_RUNNER),
                "--watch-pid",
                str(watcher.pid),
                "--",
                sys.executable,
                "-c",
                command,
            ],
            input="",
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        watcher.wait(timeout=5)
        self.assertEqual(completed.returncode, 125, completed.stderr)
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        for _ in range(100):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail("guarded descendant survived operation-lock loss")

    def test_locked_runner_accepts_non_pollable_devnull_stdin(self) -> None:
        watcher = subprocess.Popen(["sleep", "30"])
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    os.fspath(LOCKED_RUNNER),
                    "--watch-pid",
                    str(watcher.pid),
                    "--",
                    sys.executable,
                    "-c",
                    "print('NONINTERACTIVE_OK')",
                ],
                stdin=subprocess.DEVNULL,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("NONINTERACTIVE_OK", completed.stdout)
        finally:
            watcher.terminate()
            watcher.wait(timeout=5)

    def test_post_fork_relay_failure_reaps_the_guarded_group(self) -> None:
        watcher = subprocess.Popen(["sleep", "30"])

        class FailingSelector:
            def __init__(self) -> None:
                self.registrations = 0

            def register(self, *_args: object) -> None:
                self.registrations += 1
                if self.registrations == 1:
                    time.sleep(0.2)
                    raise OSError("synthetic selector failure")

            def close(self) -> None:
                return

        try:
            with mock.patch.object(
                runner.selectors,
                "DefaultSelector",
                FailingSelector,
            ):
                with self.assertRaises(OSError):
                    runner.run(["sleep", "30"], watcher.pid)
        finally:
            watcher.terminate()
            watcher.wait(timeout=5)

    def test_successful_root_cannot_leave_an_escaped_descendant(self) -> None:
        watcher = subprocess.Popen(["sleep", "30"])
        child_pid_path = self.root / "escaped-child.pid"
        command = (
            "import pathlib, subprocess; "
            "child=subprocess.Popen("
            "['sleep','30'], start_new_session=True); "
            f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))"
        )
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    os.fspath(LOCKED_RUNNER),
                    "--watch-pid",
                    str(watcher.pid),
                    "--",
                    sys.executable,
                    "-c",
                    command,
                ],
                stdin=subprocess.DEVNULL,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("left live descendants", completed.stderr)
            child_pid = int(child_pid_path.read_text(encoding="ascii"))
            for _ in range(100):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                self.fail("escaped descendant survived successful root exit")
        finally:
            watcher.terminate()
            watcher.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
