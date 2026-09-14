"""Adversarial tests for the host-global Ansible operation lock."""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

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
        # The host scan is covered by
        # test_recovery_rejects_a_visible_mutating_process. Here it would read
        # the real /proc, where unrelated `docker service` clients (the image
        # watcher runs one per cycle) make the result depend on timing.
        with mock.patch.object(
            helper, "mutating_processes", return_value=[]
        ) as scan:
            with self.assertRaisesRegex(
                helper.OperationLockError, "confirmation differs"
            ):
                helper.recover_lock(
                    self.recovery_args(apply=True, confirm="wrong"),
                    require_root=False,
                )
            helper.recover_lock(
                self.recovery_args(apply=True, confirm=confirmation),
                require_root=False,
            )
        self.assertEqual(scan.call_count, 2)
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
            "organizationweb",
            "autoupdater",
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


class MutatingProcessClassificationTests(unittest.TestCase):
    """Classify synthetic argv lists; the real /proc is never scanned."""

    def assert_mutating(self, arguments: list[str]) -> None:
        self.assertTrue(helper.is_mutating_process(arguments), arguments)

    def assert_not_mutating(self, arguments: list[str]) -> None:
        self.assertFalse(helper.is_mutating_process(arguments), arguments)

    def assert_parsed_swarm_writer(self, arguments: list[str]) -> None:
        """Prove the exact parse, not the fail-closed fallback, matched."""
        try:
            invocation = helper.swarm_cli_invocation(arguments)
        except helper.AmbiguousCommandLine as error:
            self.fail(f"{arguments} fell back to fail-closed: {error}")
        self.assertIsNotNone(invocation, arguments)
        self.assertIn(invocation[0], helper.SWARM_WRITER_SUBCOMMANDS, arguments)
        self.assertEqual(
            invocation, arguments[len(arguments) - len(invocation) :], arguments
        )
        self.assert_mutating(arguments)

    def test_existing_classifications_are_kept(self) -> None:
        for arguments in (
            ["apt-get", "install", "-y", "docker-ce"],
            ["/usr/bin/dpkg", "--configure", "-a"],
            ["/usr/bin/python3", "/usr/bin/unattended-upgrade"],
            ["/usr/bin/python3", "/root/.ansible/tmp/x/AnsiballZ_command.py"],
            ["python3", "/srv/.venv/bin/ansible-playbook", "site.yml"],
            ["docker", "service", "update", "portfolio_pablo"],
            ["/usr/bin/docker", "stack", "deploy", "-c", "stack.yml", "edge"],
            ["docker", "swarm", "update"],
            ["docker", "node", "update", "--availability", "drain", "self"],
            # Reads through a Swarm writer command family keep counting.
            ["docker", "service", "inspect", "portfolio_pablo"],
        ):
            self.assert_mutating(arguments)
        for arguments in (
            [],
            ["docker"],
            ["docker", "ps"],
            ["docker", "manifest", "inspect", "nginx:1"],
            ["docker", "login", "--username", "bot", "--password-stdin"],
            ["/usr/local/bin/shepherd"],
            ["sleep", "3600"],
            ["dockerd", "--host", "fd://", "service"],
        ):
            self.assert_not_mutating(arguments)

    def test_docker_global_options_before_a_swarm_writer_are_skipped(
        self,
    ) -> None:
        for arguments in (
            ["docker", "--config", "/x", "service", "update", "s"],
            ["docker", "--config=/x", "service", "update", "--rollback", "s"],
            ["docker", "-c", "/x", "node", "update", "self"],
            ["docker", "-c/x", "node", "update", "self"],
            [
                "docker",
                "-H",
                "unix:///var/run/docker.sock",
                "service",
                "update",
                "--rollback",
                "s",
            ],
            ["docker", "--host=unix:///var/run/docker.sock", "swarm", "update"],
            ["docker", "-Hunix:///var/run/docker.sock", "service", "rm", "s"],
            ["docker", "--context", "c", "stack", "deploy"],
            ["docker", "--context=c", "stack", "rm", "edge"],
            ["docker", "-l", "debug", "service", "scale", "s=0"],
            ["docker", "--log-level=info", "--debug", "stack", "rm", "x"],
            [
                "/usr/bin/docker",
                "--tls",
                "--tlsverify",
                "--tlscacert",
                "/ca.pem",
                "--tlscert=/cert.pem",
                "--tlskey",
                "/key.pem",
                "-D",
                "service",
                "update",
                "s",
            ],
            ["docker", "--tlsverify=false", "service", "update", "s"],
            ["docker", "--", "service", "update", "s"],
        ):
            self.assert_parsed_swarm_writer(arguments)

    def test_timeout_wrappers_around_a_swarm_writer_are_skipped(self) -> None:
        image = "--image=apptolast/portfolio@sha256:" + "d" * 64
        for arguments in (
            # The exact Shepherd v1.8.1 invocation.
            [
                "timeout",
                "900",
                "docker",
                "service",
                "update",
                "portfolio_pablo",
                "--detach=false",
                "--with-registry-auth",
                image,
            ],
            ["timeout", "900", "docker", "--config", "/x", "service", "update"],
            ["timeout", "900", "docker", "service", "update", "--rollback", "s"],
            [
                "/usr/bin/timeout",
                "-s",
                "KILL",
                "-k",
                "10s",
                "900",
                "docker",
                "service",
                "update",
                "s",
            ],
            ["timeout", "-sKILL", "-k5", "15m", "docker", "stack", "deploy"],
            [
                "timeout",
                "--signal=TERM",
                "--kill-after=5",
                "--preserve-status",
                "--foreground",
                "-v",
                "1.5m",
                "/usr/bin/docker",
                "node",
                "update",
            ],
            ["timeout", "--", "900", "docker", "swarm", "update"],
            ["timeout", "-t", "900", "-s", "KILL", "docker", "service", "rm"],
            ["busybox", "timeout", "900", "docker", "service", "update", "s"],
            ["timeout", "900", "timeout", "800", "docker", "node", "update"],
        ):
            self.assert_parsed_swarm_writer(arguments)

    def test_unparsed_options_fail_closed_before_a_swarm_writer(self) -> None:
        for arguments in (
            ["docker", "--unreviewed", "service", "update", "s"],
            ["timeout", "--odd", "900", "docker", "service", "rm"],
            ["timeout", "docker", "service", "update", "s"],
            ["timeout", "soon", "docker", "service", "update", "s"],
        ):
            with self.assertRaises(helper.AmbiguousCommandLine, msg=arguments):
                helper.swarm_cli_invocation(arguments)
        self.assert_mutating(["docker", "--unreviewed", "service", "update", "s"])
        self.assert_mutating(["timeout", "--odd", "900", "docker", "service", "rm"])
        self.assert_mutating(["timeout", "docker", "service", "update", "s"])
        self.assert_not_mutating(["docker", "--unreviewed", "manifest", "inspect"])

    def test_exact_parse_returns_the_subcommand_after_the_options(self) -> None:
        self.assertEqual(
            helper.swarm_cli_invocation(
                ["timeout", "-k", "5", "900", "docker", "-H", "x", "ps"]
            ),
            ["ps"],
        )
        self.assertEqual(
            helper.swarm_cli_invocation(["docker", "-c", "/x", "service", "ls"]),
            ["service", "ls"],
        )
        self.assertIsNone(helper.swarm_cli_invocation(["timeout", "9", "sleep"]))

    def test_reads_and_non_docker_commands_stay_non_mutating(self) -> None:
        for arguments in (
            ["timeout", "900", "docker", "manifest", "inspect", "nginx:1"],
            ["docker", "--config", "/x", "manifest", "inspect", "nginx:1"],
            ["timeout", "900", "docker", "--config=/x", "manifest", "inspect"],
            ["docker", "--config", "/x", "login", "--password-stdin"],
            ["timeout", "-s", "KILL", "900", "docker", "ps"],
            # An option value is never mistaken for the subcommand.
            ["docker", "-H", "service", "ps"],
            ["docker", "--context", "stack", "info"],
            ["docker", "--config"],
            ["timeout", "900", "sleep", "service"],
            ["timeout", "900"],
            ["busybox", "sleep", "60"],
        ):
            self.assert_not_mutating(arguments)


class WatcherUpdateGuardContractTests(unittest.TestCase):
    """Structure of the read-only image-watcher check in the lock guard."""

    PROVE = "Prove the host-global deployment lock before mutation"
    GUARD_BLOCK = "Refuse to mutate while a watched service is mid-update"
    WAIT = "Wait for any in-progress update of each watched service"

    def setUp(self) -> None:
        self.tasks = yaml.safe_load(GUARD.read_text(encoding="utf-8"))
        names = [task["name"] for task in self.tasks]
        self.guard = self.tasks[names.index(self.PROVE) + 1]

    def flattened(self, tasks: list[dict]) -> list[dict]:
        result: list[dict] = []
        for task in tasks:
            result.append(task)
            result.extend(self.flattened(task.get("block", [])))
        return result

    def test_guard_follows_the_prove_task_and_runs_only_in_apply(self) -> None:
        self.assertEqual(self.guard["name"], self.GUARD_BLOCK)
        self.assertEqual(self.guard["when"], 'operation_lock_guard_mode == "apply"')
        names = [task["name"] for task in self.tasks]
        self.assertEqual(
            names.index("Derive the strict nested operation-lock proof environment"),
            names.index(self.GUARD_BLOCK) + 1,
        )

    def test_guard_is_read_only_and_cannot_be_overridden(self) -> None:
        defaults = (
            PROJECT_ROOT / "ansible/roles/operation_lock_guard/defaults/main.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("watch", defaults)
        self.assertNotIn("autoupdate", defaults)
        commands = 0
        for task in self.flattened([self.guard]):
            for forbidden in (
                "ignore_errors",
                "failed_when",
                "vars",
                "rescue",
                "always",
                "ansible.builtin.shell",
            ):
                self.assertNotIn(forbidden, task, task["name"])
            if task["name"] != self.WAIT:
                for forbidden in ("until", "retries", "delay"):
                    self.assertNotIn(forbidden, task, task["name"])
            if "block" in task:
                continue
            if "ansible.builtin.assert" in task:
                continue
            self.assertIs(task["check_mode"], False, task["name"])
            self.assertIs(task["changed_when"], False, task["name"])
            if "ansible.builtin.command" in task:
                commands += 1
                argv = task["ansible.builtin.command"]["argv"]
                self.assertEqual(argv[0], "/usr/bin/docker")
                self.assertIn(argv[1], {"info", "service"})
                if argv[1] == "service":
                    self.assertIn(argv[2], {"ls", "inspect"})
            else:
                self.assertEqual(
                    set(task) - {"name", "register", "changed_when", "check_mode"},
                    {"ansible.builtin.stat"},
                )
                self.assertEqual(
                    task["ansible.builtin.stat"]["path"], "/usr/bin/docker"
                )
        self.assertEqual(commands, 3)
        listing = next(
            task
            for task in self.flattened([self.guard])
            if task["name"] == "List the services the image watcher may update"
        )
        self.assertEqual(
            listing["ansible.builtin.command"]["argv"][3:],
            ["--quiet", "--filter", "label=apptolast.autoupdate=true"],
        )

    def test_only_in_progress_update_states_block(self) -> None:
        asserts = [
            task
            for task in self.flattened([self.guard])
            if "ansible.builtin.assert" in task
        ]
        state_gate = next(
            task
            for task in asserts
            if task["name"]
            == "Reject a watched-service update that is still in progress"
        )
        expression = " ".join(state_gate["ansible.builtin.assert"]["that"])
        blocked = re.findall(r"not in (\[[^\]]*\])", expression)
        self.assertEqual(len(blocked), 1, expression)
        self.assertEqual(
            set(ast.literal_eval(blocked[0])), {"updating", "rollback_started"}
        )
        for terminal in ("paused", "rollback_paused", "rollback_completed"):
            self.assertNotIn(terminal, expression)
        fail_msg = state_gate["ansible.builtin.assert"]["fail_msg"]
        self.assertIn("retry", fail_msg)
        # Swarm cannot tell a watcher update from one a previous apply left.
        self.assertIn("previous apply", fail_msg)
        self.assertNotIn("The image watcher is updating", fail_msg)
        self.assertEqual(
            state_gate["loop"], "{{ operation_lock_guard_watched_states.results }}"
        )

        swarm_gate = next(
            task
            for task in asserts
            if task["name"]
            == "Require a Swarm state that proves whether the watcher can act"
        )
        self.assertEqual(
            swarm_gate["ansible.builtin.assert"]["that"],
            ['operation_lock_guard_swarm_state.stdout in ["inactive", "active"]'],
        )
        active_block = next(
            task
            for task in self.flattened([self.guard])
            if task["name"] == "Check the watched services on an active Swarm"
        )
        self.assertEqual(
            active_block["when"],
            [
                "operation_lock_guard_docker_cli.stat.exists",
                'operation_lock_guard_swarm_state.stdout == "active"',
            ],
        )

    def test_wait_is_bounded_and_stops_on_the_same_in_progress_states(
        self,
    ) -> None:
        wait = next(
            task for task in self.flattened([self.guard]) if task["name"] == self.WAIT
        )
        # Covers a 120 s monitor plus a 2 min start_period, then fails.
        self.assertGreaterEqual(wait["retries"] * wait["delay"], 300)
        self.assertLessEqual(wait["retries"] * wait["delay"], 600)
        until = wait["until"]
        self.assertIn("operation_lock_guard_watched_states.rc != 0", until)
        blocked = re.findall(r"not in (\[[^\]]*\])", until)
        self.assertEqual(len(blocked), 1, until)
        self.assertEqual(
            set(ast.literal_eval(blocked[0])), {"updating", "rollback_started"}
        )

    def test_unreachable_or_locked_swarm_fails_closed_with_a_manual_exit(
        self,
    ) -> None:
        # Decision: no platform-only tolerance and no bypass variable. The
        # guard fails closed everywhere and the manual exits are documented.
        for task in self.flattened([self.guard]):
            conditions = task.get("when", [])
            if isinstance(conditions, str):
                conditions = [conditions]
            for condition in conditions:
                self.assertNotIn("platform", condition, task["name"])
                self.assertNotIn("playbook", condition, task["name"])
        operations = (PROJECT_ROOT / "docs/OPERATIONS.md").read_text(
            encoding="utf-8"
        )
        for heading in (
            "### Apply rechazado por un update en curso",
            "### Swarm parado o bloqueado",
        ):
            self.assertIn(heading, operations)
        self.assertIn("sudo -- systemctl start docker.service", operations)
        self.assertIn("UpdateStatus.StartedAt", operations)


if __name__ == "__main__":
    unittest.main()
