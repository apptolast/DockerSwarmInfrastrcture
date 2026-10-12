"""The relock window's manager commands under the real direct lock.

On the production host every `forget` and `seed-layout` runs under
scripts/host_global_operation_lock.py run_direct, which keeps its marker when
the wrapped command exits non-zero. These tests drive the manager through that
real function, on a lock and a marker in a private directory, and pin what the
window can rely on: a refused command leaves the marker, every later mutating
command is refused before it starts, `image-status` takes no lock, and the
documented recovery lets the next command run.

What is not exercised: the `os.execv` hop of ensure_mutation_lock, the peer
markers of /run/lock and the root-owned production paths, none of which a
private path reaches. The wrapped command cannot be the manager's own entry
point: its proof reads /run/lock. The driver below keeps the manager's own
ensure_host_lock and points only the proof at the private paths, and makes the
step that would take the production lock fail instead of taking it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import traceback
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import test_manage_ax_lab_substrate as base
from test_host_global_operation_lock import host_lock

if TYPE_CHECKING:
    from collections.abc import Iterator

manager = base.manager
TAG = base.TAG
SCRIPTS = base.ROOT / "scripts"

# The command run_direct wraps: the manager's main, as the re-executed command
# runs it, with the lock and the marker of the test and a record of what it
# did. The marker proof of the manager's ensure_host_lock is the real one.
DRIVER = """\
import importlib.util
import json
import sys
from pathlib import Path

scripts, lock, marker, record, *argv = sys.argv[1:]


def note(event, **fields):
    with open(record, "a") as handle:
        handle.write(json.dumps({"event": event, **fields}) + "\\n")


note("started")
sys.path.insert(0, scripts)
import host_global_operation_lock as host_lock

prove = host_lock.prove_direct_environment
ensure = host_lock.ensure_mutation_lock


def prove_private(operation):
    prove(operation, lock_path=Path(lock), marker_path=Path(marker))


def ensure_noted(operation, command, *, caller):
    note("lock", operation=operation)
    ensure(operation, command, caller=caller)
    note("proven")


def never_the_production_lock(_caller):
    raise host_lock.HostGlobalLockError("no proof: the test never locks the host")


class FakeDocker:
    def swarm_state(self):
        return "active"


host_lock.prove_direct_environment = prove_private
host_lock.ensure_mutation_lock = ensure_noted
host_lock.locate_helper = never_the_production_lock
spec = importlib.util.spec_from_file_location(
    "manage_ax_lab_substrate", Path(scripts) / "manage-ax-lab-substrate.py"
)
manager = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = manager
spec.loader.exec_module(manager)
manager.Docker = FakeDocker
raise SystemExit(manager.main(argv))
"""


@contextmanager
def redirected(output: Path) -> Iterator[None]:
    """Standard input from nothing, and output and errors into a file.

    The descriptors, not only the Python streams: run_direct's children and the
    PTY runner write to the descriptors they inherit.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(descriptor) for descriptor in (0, 1, 2)]
    sink = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    nothing = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(nothing, 0)
        os.dup2(sink, 1)
        os.dup2(sink, 2)
        with (
            mock.patch.object(sys, "stdin", sys.__stdin__),
            mock.patch.object(sys, "stdout", sys.__stdout__),
            mock.patch.object(sys, "stderr", sys.__stderr__),
        ):
            yield
            sys.__stdout__.flush()
            sys.__stderr__.flush()
    finally:
        for descriptor, original in zip((0, 1, 2), saved, strict=True):
            os.dup2(original, descriptor)
            os.close(original)
        os.close(sink)
        os.close(nothing)


def without_proof(environment: dict[str, str]) -> dict[str, str]:
    """The environment of a command that is not under any lock, even when this
    test runs inside one (validate-iac does)."""
    proof = (
        {host_lock.PROOF_SCOPE_ENV}
        | set(host_lock.DIRECT_PROOF_FIELDS.values())
        | set(host_lock.ANSIBLE_PROOF_FIELDS.values())
    )
    return {key: value for key, value in environment.items() if key not in proof}


def printed(output: str) -> Any:
    """The JSON line a command printed, out of a terminal's echo."""
    return json.loads(output[output.index("{") : output.rindex("}") + 1])


def kinds(events: list[dict[str, Any]]) -> list[str]:
    return [event["event"] for event in events]


class WindowLockingTests(base.RelockCase):
    """A lab backup, a build output and the private lock the window runs under."""

    def setUp(self) -> None:
        super().setUp()
        self.write_old_backup()
        base.write_build_output(
            self.source, {name: self.new[name] for name in manager.IMAGE_NAMES}, TAG
        )
        # `cp -a` of the layout before any forget, as the window takes it.
        shutil.copytree(self.backup, self.snapshot, symlinks=True)
        self.names = sorted(manager.IMAGE_NAMES)
        self.old_pins = base.digests(self.old, manager.IMAGE_NAMES)
        self.new_pins = base.digests(self.new, manager.IMAGE_NAMES)
        self.locks = self.directory / "lock"
        self.locks.mkdir()
        self.locks.chmod(0o700)
        self.lock = self.locks / "iac.lock"
        self.marker = self.locks / "direct.marker"
        self.records = self.directory / "records"
        self.records.mkdir()
        self.driver = self.records / "window-driver.py"
        self.driver.write_text(DRIVER)
        self.calls = 0

    def argv(
        self, command: str, pins: dict[str, str], *, source: Path | None = None
    ) -> list[str]:
        arguments = [command, "--image-set", "substrate"]
        if source is not None:
            arguments += ["--source", str(source)]
        arguments += ["--layout", str(self.backup), "--tag", TAG]
        for name, digest in sorted(pins.items()):
            arguments += ["--image", f"{name}={digest}"]
        return arguments

    def driver_command(self, argv: list[str]) -> tuple[list[str], Path]:
        self.calls += 1
        record = self.records / f"record-{self.calls}.jsonl"
        command = [
            sys.executable,
            str(self.driver),
            str(SCRIPTS),
            str(self.lock),
            str(self.marker),
            str(record),
            *argv,
        ]
        return command, record

    @staticmethod
    def events(record: Path) -> list[dict[str, Any]]:
        if not record.exists():
            return []
        return [json.loads(line) for line in record.read_text().splitlines()]

    def supervise(self, operation: str, command: list[str]) -> int:
        """host_global_operation_lock.main's `run`, for the private paths."""
        try:
            return host_lock.run_direct(
                operation, command, lock_path=self.lock, marker_path=self.marker
            )
        except (host_lock.HostGlobalLockError, OSError) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 126

    def under_lock(self, *argv: str) -> types.SimpleNamespace:
        """What `sudo manage-ax-lab-substrate.py <argv>` does on an active Swarm
        node: the host lock's run_direct around the manager, in a supervisor
        process of its own that exits, as the real one does, when it is done."""
        operation = f"ax-lab-substrate-{argv[0]}"
        command, record = self.driver_command(list(argv))
        output = self.records / f"output-{self.calls}.txt"
        with redirected(output):
            supervisor = os.fork()
            if supervisor == 0:
                code = 70
                try:
                    code = self.supervise(operation, command)
                except BaseException:
                    traceback.print_exc()
                finally:
                    sys.stderr.flush()
                    os._exit(code)
            _pid, status = os.waitpid(supervisor, 0)
        return types.SimpleNamespace(
            code=os.waitstatus_to_exitcode(status),
            output=output.read_text().replace("\r", ""),
            events=self.events(record),
            operation=operation,
        )

    def without_lock(self, *argv: str) -> types.SimpleNamespace:
        """A command run as it is when it is not in MUTATING_COMMANDS: no
        run_direct, and no proof in the environment."""
        command, record = self.driver_command(list(argv))
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=without_proof(dict(os.environ)),
            timeout=120,
            check=False,
        )
        return types.SimpleNamespace(
            code=done.returncode,
            stdout=done.stdout,
            stderr=done.stderr,
            events=self.events(record),
        )

    def lock_entries(self) -> list[str]:
        return sorted(path.name for path in self.locks.iterdir())

    def move_one_image_through_the_lock(self) -> str:
        """The first image of a window, moved to its new pin by the two
        commands that rewrite the backup. Nothing is retained: each exits 0."""
        moved = self.names[0]
        forgot = self.under_lock(*self.argv("forget", {moved: self.old_pins[moved]}))
        self.assertEqual(
            (forgot.code, printed(forgot.output)), (0, {moved: "forgotten"})
        )
        # The manager asked for the lock under the name it is run under, and
        # proved it from inside the lock's own process tree.
        self.assertEqual(kinds(forgot.events), ["started", "lock", "proven"])
        self.assertEqual(forgot.events[1]["operation"], forgot.operation)
        self.assertFalse(self.marker.exists())
        seeded = self.under_lock(
            *self.argv("seed-layout", {moved: self.new_pins[moved]}, source=self.source)
        )
        self.assertEqual((seeded.code, printed(seeded.output)), (0, {moved: "seeded"}))
        self.assertEqual(seeded.events[1]["operation"], seeded.operation)
        self.assertFalse(self.marker.exists())
        return moved

    def refuse_a_forget(self) -> str:
        """The rollback's forget of an image the backup still names with the
        old pin: the manager refuses it, as the window's rollback relies on."""
        name = self.names[1]
        new, old = self.new_pins[name], self.old_pins[name]
        before = self.index_bytes()
        refused = self.under_lock(*self.argv("forget", {name: new}))
        self.assertEqual(refused.code, 1)
        self.assertIn(f"names {name}:{TAG} with {old}, not {new}", refused.output)
        # It was the manager that refused, inside the lock, and it wrote nothing.
        self.assertEqual(kinds(refused.events), ["started", "lock", "proven"])
        self.assertEqual(self.index_bytes(), before)
        return name

    def test_a_refused_forget_keeps_the_marker_and_every_next_mutation_is_refused(
        self,
    ) -> None:
        moved = self.move_one_image_through_the_lock()
        untouched = self.refuse_a_forget()
        # The wrapper kept the marker, as for any non-zero exit.
        self.assertTrue(self.marker.is_file())
        self.assertEqual(
            json.loads(self.marker.read_text())["operation"],
            "ax-lab-substrate-forget",
        )
        # So the next mutating commands of the window never start, whatever
        # they would have done: the forget that drops the moved image's new
        # entry, and the seed of the old pin that the rollback runs after it.
        before = self.index_bytes()
        repeats = {
            "forget": self.argv("forget", {moved: self.new_pins[moved]}),
            "seed-layout": self.argv(
                "seed-layout",
                {untouched: self.old_pins[untouched]},
                source=self.snapshot,
            ),
        }
        for command, arguments in repeats.items():
            with self.subTest(command=command):
                run = self.under_lock(*arguments)
                self.assertEqual(run.code, 126)
                self.assertRegex(run.output, "marker exists")
                self.assertIn(
                    "direct lock holder failed before acquisition", run.output
                )
                self.assertEqual(run.events, [])
                self.assertEqual(self.index_bytes(), before)
        with self.assertRaisesRegex(
            host_lock.HostGlobalLockError, "stale operation marker exists"
        ):
            host_lock.open_and_acquire_global_lock(self.lock, self.marker, create=False)
        self.assertEqual(self.named(moved, TAG), self.new_pins[moved])

    def test_a_refused_seed_layout_keeps_the_marker_and_its_repeat_is_refused(
        self,
    ) -> None:
        name = self.names[0]
        old = self.old_pins[name]
        forgot = self.under_lock(*self.argv("forget", {name: old}))
        self.assertEqual(forgot.code, 0, forgot.output)
        # The build output is not where the old pin comes from.
        wrong = self.under_lock(
            *self.argv("seed-layout", {name: old}, source=self.source)
        )
        self.assertEqual(wrong.code, 1)
        self.assertIn(f"{self.source} does not list {old}", wrong.output)
        self.assertEqual(kinds(wrong.events), ["started", "lock", "proven"])
        self.assertTrue(self.marker.is_file())
        self.assertEqual(
            json.loads(self.marker.read_text())["operation"],
            "ax-lab-substrate-seed-layout",
        )
        # The window's advice, to run the command again: with the right source
        # it would seed, and it does not start.
        right = self.under_lock(
            *self.argv("seed-layout", {name: old}, source=self.snapshot)
        )
        self.assertEqual(right.code, 126)
        self.assertRegex(right.output, "marker exists")
        self.assertEqual(right.events, [])
        self.assertIsNone(self.named(name, TAG))

    def test_image_status_takes_no_lock_and_the_marker_does_not_stop_it(self) -> None:
        moved = self.move_one_image_through_the_lock()
        untouched = self.refuse_a_forget()
        marker, entries = self.marker.read_bytes(), self.lock_entries()
        old, new = self.old_pins, self.new_pins
        complete = self.without_lock(
            *self.argv("image-status", {untouched: old[untouched]})
        )
        self.assertEqual(complete.code, 0, complete.stderr)
        self.assertEqual(
            json.loads(complete.stdout),
            {"backup": {untouched: "complete"}, "registry": None},
        )
        self.assertEqual(kinds(complete.events), ["started"])
        # What the rollback reads to decide per image: the new pin of an image
        # the backup names with the old pin, and the other way round, stop the
        # read. Refused without a lock, it leaves no marker.
        for name, pin in ((untouched, new[untouched]), (moved, old[moved])):
            with self.subTest(refused=name):
                run = self.without_lock(*self.argv("image-status", {name: pin}))
                self.assertEqual(run.code, 1)
                self.assertIn(f"names {name}:{TAG} with another digest", run.stderr)
                self.assertEqual(run.stdout, "")
                self.assertEqual(kinds(run.events), ["started"])
        self.assertEqual(self.marker.read_bytes(), marker)
        self.assertEqual(self.lock_entries(), entries)

    def test_a_refused_read_leaves_no_lock_and_no_marker_unlike_a_refused_forget(
        self,
    ) -> None:
        name = self.names[0]
        pin = {name: self.new_pins[name]}
        read = self.without_lock(*self.argv("image-status", pin))
        self.assertEqual(read.code, 1)
        self.assertIn(f"names {name}:{TAG} with another digest", read.stderr)
        self.assertEqual(self.lock_entries(), [])
        # The same state refused by a mutation takes the lock and keeps it.
        forget = self.under_lock(*self.argv("forget", pin))
        self.assertEqual(forget.code, 1)
        self.assertEqual(self.lock_entries(), ["direct.marker", "iac.lock"])

    def test_recovering_the_marker_is_what_lets_the_next_mutation_run(self) -> None:
        moved = self.move_one_image_through_the_lock()
        self.refuse_a_forget()
        arguments = self.argv("forget", {moved: self.new_pins[moved]})
        self.assertEqual(self.under_lock(*arguments).code, 126)
        # The supervisor of the refused command is gone, as it is once the
        # window's command returned, so the marker's own recovery applies.
        paths = {"lock_path": self.lock, "marker_path": self.marker}
        archive = self.directory / "archive"
        confirmation = host_lock.recover_direct_marker(
            apply=False, confirmation=None, archive_directory=archive, **paths
        )
        self.assertTrue(self.marker.is_file())
        host_lock.recover_direct_marker(
            apply=True, confirmation=confirmation, archive_directory=archive, **paths
        )
        self.assertFalse(self.marker.exists())
        self.assertEqual(len(list(archive.iterdir())), 1)
        run = self.under_lock(*arguments)
        self.assertEqual((run.code, printed(run.output)), (0, {moved: "forgotten"}))
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
