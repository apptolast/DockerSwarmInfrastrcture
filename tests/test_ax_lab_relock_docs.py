"""The relock window of the AX lab in docs/AX.md.

The section «Imágenes compiladas en la CI y ventana de resiembra» is a
procedure, not code, so what can be proved offline is its shape: the commands
come in the order that keeps the production copy recoverable, nothing reads
the live copy with the new contract before the old entries are forgotten,
every manager call names its image set, the PR of the pins is merged inside
the window and never before the seed, and no value of any pin is written in
it (the window takes them from the validated plans).

Its shape also has to stop the window and never ask for a refusal: on the host
a locked command that exits non-zero leaves the direct marker and the next
locked command is refused. So a locked call is never followed by `|| echo`,
every guard goes through the stop helpers, the CLI is checked by one `&&`
chain, and the last class runs the blocks of the window, as they are written,
against the real manager and the real direct lock on private paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
import zipfile
from pathlib import Path
from typing import Any

from test_manage_ax_lab_substrate import (
    AX_TAG,
    LAB_TAGS,
    RELOCKED,
    TAG,
    WEB_TAG,
    make_image,
    manager,
    sha,
    write_backup,
    write_build_output,
    write_ko_layout,
)

ROOT = Path(__file__).resolve().parents[1]
HEADING = "## Imágenes compiladas en la CI y ventana de resiembra\n"
MANAGER = "scripts/manage-ax-lab-substrate.py"
LOCK = "scripts/host_global_operation_lock.py run --operation ax-lab-seed --"
DEPLOY = "./scripts/deploy-ansible.sh --playbook ax-lab --local"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def window() -> str:
    docs = read("docs/AX.md")
    if docs.count(HEADING) != 1:
        raise AssertionError(f"docs/AX.md must hold {HEADING.strip()!r} once")
    return docs.split(HEADING, 1)[1].split("\n## ", 1)[0]


def flat(text: str) -> str:
    """The text as the existing docs tests read it: one line, no `\\` breaks."""
    return " ".join(text.replace("\\\n", " ").split())


def positions(case: unittest.TestCase, text: str, fragments: list[str]) -> list[int]:
    """Where each fragment is, each one found after the end of the previous."""
    found = []
    start = 0
    for fragment in fragments:
        index = text.find(fragment, start)
        case.assertGreaterEqual(
            index,
            0,
            f"{fragment!r} is missing or comes before the previous command",
        )
        found.append(index)
        start = index + len(fragment)
    return found


def bash_blocks(text: str) -> list[str]:
    blocks = re.findall(
        r"^( *)```bash\n(.*?)^\1```$", text, flags=re.DOTALL | re.MULTILINE
    )
    return [textwrap.dedent(body) for _indent, body in blocks]


def statements(block: str) -> list[str]:
    """The commands of a bash block, one per logical line (`\\` joined)."""
    joined = re.sub(r"\\\n\s*", " ", block)
    lines = (line.strip() for line in joined.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


# What takes the host-global lock: the manager's mutating commands (its
# MUTATING_COMMANDS; on the host it re-executes itself under the lock) and
# anything run through host_global_operation_lock.py.
LOCKED_MANAGER = re.compile(
    rf"{re.escape(MANAGER)}\s+(export|import|seed-layout|forget|build|install)\b"
)
# A retained marker is the whole point of a failed locked call: what follows it
# must not turn that failure into a success.
MASKING_OR = re.compile(r"\|\|\s*(echo|printf|true|:|exit\s+0|return\s+0)(\s|;|$)")
# A check that decides whether the window goes on.
GUARD = re.compile(r"^(\[\[|\[ |test )")


def is_locked(statement: str) -> bool:
    return (
        "host_global_operation_lock.py run" in statement
        or LOCKED_MANAGER.search(statement) is not None
    )


def function_body(blocks: list[str], name: str) -> str:
    """The text of `name() { ... }`, from its header to the closing brace."""
    found = []
    for block in blocks:
        match = re.search(
            rf"^{name}\(\) \{{.*?^\}}$", block, flags=re.DOTALL | re.MULTILINE
        )
        if match:
            found.append(match.group(0))
    if len(found) != 1:
        raise AssertionError(f"{name}() must be defined once in the window")
    return found[0]


class WindowOrderTests(unittest.TestCase):
    def test_the_commands_run_in_the_order_that_keeps_the_copy_recoverable(
        self,
    ) -> None:
        text = flat(window())
        positions(
            self,
            text,
            [
                # The snapshot of the live copy and the CLI, under the lock,
                # then proved readable by the manager for the three sets.
                f"{LOCK} /usr/bin/cp -a " + '"${LAYOUT}" "${SNAP_LAYOUT}"',
                f"{LOCK} /usr/bin/cp -a " + '"${CLI_BACKUP}" "${SNAP_CLI}"',
                f'{MANAGER} image-status --image-set substrate --layout "${{SNAP_LAYOUT}}"',
                f'{MANAGER} image-status --image-set ax --layout "${{SNAP_LAYOUT}}"',
                f'{MANAGER} image-status --image-set web --layout "${{SNAP_LAYOUT}}"',
                # The artifacts, extracted as root, and the CLI checked.
                "/usr/bin/python3 -I -m zipfile -e",
                'sudo -- chmod 0700 "${IN}/ax/ax"',
                'sudo -- "${IN}/ax/ax" help',
                # The rehearsal into a scratch layout, proved complete.
                'seed_one substrate "${IN}/substrate" "${REH}/images"',
                'seed_one ax "${IN}/ax/ko-${name}" "${REH}/images"',
                f'{MANAGER} image-status --image-set substrate --layout "${{REH}}/images"',
                # Only then the forget, by the old digests, on the live copy.
                'forget_one substrate "${LAYOUT}"',
                'forget_one ax "${LAYOUT}"',
                # The real seed, then the live copy is read with the new pins.
                'seed_one substrate "${IN}/substrate" "${LAYOUT}"',
                'seed_one ax "${IN}/ax/ko-${name}" "${LAYOUT}"',
                f'{MANAGER} image-status --image-set substrate --layout "${{LAYOUT}}"',
                # The CLI goes in last of the copy, under the lock.
                (
                    f"{LOCK} /usr/bin/install -o root -g root -m 0600"
                    ' "${IN}/ax/ax" "${CLI_BACKUP}"'
                ),
                # The merge, then the playbook: check, apply, check again.
                "git checkout --detach origin/main",
                f"{DEPLOY} --check",
                f"{DEPLOY} --confirm-production",
                f"{DEPLOY} --check",
            ],
        )

    def test_nothing_reads_the_live_copy_before_the_old_entries_are_forgotten(
        self,
    ) -> None:
        text = flat(window())
        before = text[: text.index('forget_one substrate "${LAYOUT}"')]
        # The playbook (its --check reads the copy too) and every manager call
        # on the live layout wait for the forget; the snapshot and the
        # rehearsal use other layouts. The per-image functions take the layout
        # as an argument, so no call of theirs names the live one before then.
        self.assertNotIn("deploy-ansible.sh", before)
        self.assertNotIn('--layout "${LAYOUT}"', before)
        self.assertNotIn(" --registry ", before.split("### Pasos")[1])
        # What the forget itself reads is the live copy with the OLD pins: the
        # ones that describe what it holds. Never a new pin.
        step_five = text.split("5. El `forget`", 1)[1].split("6. La siembra", 1)[0]
        self.assertIn("STALE_SUB", step_five)
        self.assertIn("STALE_KO", step_five)
        for new in ("NEW_", "MOVED_", "${IN}"):
            self.assertNotIn(new, step_five)

    def test_the_pins_pr_is_merged_inside_the_window_after_the_seed(self) -> None:
        text = flat(window())
        self.assertIn(
            "se fusiona dentro de la ventana, después de la siembra, nunca antes",
            text,
        )
        first_seed, merge, first_check = positions(
            self,
            text,
            [
                'seed_one substrate "${IN}/substrate" "${LAYOUT}"',
                "git checkout --detach origin/main",
                DEPLOY,
            ],
        )
        self.assertLess(first_seed, merge)
        self.assertLess(merge, first_check)
        # The seed runs from a detached checkout of the PR head, with the
        # artifacts of a workflow_dispatch run on that head.
        self.assertLess(
            text.index('git checkout --detach "${PR_HEAD:?}"'),
            text.index("/usr/bin/cp -a"),
        )
        self.assertIn("`workflow_dispatch` de `ax-lab-images.yml` sobre ese head", text)

    def test_the_freeze_covers_ax_web_releases_and_every_ax_lab_run(self) -> None:
        text = flat(window())
        self.assertIn("Desde el paso 0 hasta el último `changed=0`", text)
        self.assertNotIn("Desde el `forget` hasta el último `changed=0`", text)
        self.assertIn("ningún release de `ax-web` se fusiona", text)
        self.assertIn("ningún `ax-lab` corre (`--check` incluido)", text)

    def test_the_cli_is_checked_before_it_is_installed(self) -> None:
        text = flat(window())
        positions(
            self,
            text,
            [
                'sudo -- sha256sum "${IN}/ax/ax"',
                'sudo -- chmod 0700 "${IN}/ax/ax"',
                'sudo -- "${IN}/ax/ax" help',
                "getconf GNU_LIBC_VERSION",
                f"{LOCK} /usr/bin/install",
            ],
        )


class WindowSafetyTests(unittest.TestCase):
    def test_every_manager_call_runs_as_root_and_names_its_image_set(self) -> None:
        text = flat(window())
        calls = re.findall(rf"(.{{0,40}}){re.escape(MANAGER)} (\S+)( \S+)?", text)
        commands = [command for _before, command, _after in calls]
        for command in ("seed-layout", "forget", "image-status"):
            self.assertIn(command, commands)
        for before, command, after in calls:
            with self.subTest(command=command):
                self.assertTrue(before.endswith("sudo -- /usr/bin/python3 "), before)
                if command in ("seed-layout", "forget", "image-status"):
                    self.assertEqual(after.strip(), "--image-set")
        # The web panel's set is only ever read: its image is not re-pinned.
        self.assertIsNone(re.search(r"(seed-layout|forget) --image-set web", text))

    def test_the_snapshot_never_hard_links_the_copy(self) -> None:
        text = flat(window())
        self.assertNotIn("cp -al", text.replace("`cp -al`", ""))
        self.assertIn("/usr/bin/cp -a", text)

    def test_the_artifacts_are_extracted_as_root_into_root_only_directories(
        self,
    ) -> None:
        text = flat(window())
        self.assertIn(
            'sudo -- install -d -o root -g root -m 0700 "${IN}" "${IN}/substrate"'
            ' "${IN}/ax"',
            text,
        )
        self.assertIn(
            'sudo -- /usr/bin/python3 -I -m zipfile -e "${SUBSTRATE_ZIP:?}"'
            ' "${IN}/substrate"',
            text,
        )
        self.assertIn(
            'sudo -- /usr/bin/python3 -I -m zipfile -e "${AX_ZIP:?}" "${IN}/ax"', text
        )
        self.assertIn(r"\( -perm /022 -o ! -user root \) -print", text)

    def test_every_assertion_helper_the_steps_use_is_defined(self) -> None:
        text = window()
        for helper in ("expect_all", "expect_complete", "expect_pinned"):
            with self.subTest(helper=helper):
                self.assertIn(f"{helper}() {{", text)
                self.assertIn(f"{helper} ", text.split(f"{helper}() {{", 1)[1])

    def test_the_pin_sets_are_compared_in_the_c_locale(self) -> None:
        # `sorted` sorts in the C locale, so `comm` has to compare in it too: in
        # another locale it can see unsorted input and file an unchanged pin as
        # stale, which the forget would then drop.
        text = flat(window())
        calls = re.findall(r"(.{0,12})\bcomm -", text)
        self.assertEqual(len(calls), 4)
        for before in calls:
            with self.subTest(before=before):
                self.assertTrue(before.endswith("LC_ALL=C "), before)
        self.assertIn("sorted() { printf '%s\\n' \"$@\" | LC_ALL=C sort; }", text)

    def test_the_final_verification_asserts_the_cli_on_both_paths(self) -> None:
        text = flat(window())
        positions(
            self,
            text,
            [
                'for cli in /opt/dockerswarm/ax-lab/bin/ax "${CLI_BACKUP}"; do',
                (
                    '[[ "$(sudo -- sha256sum "${cli}" | cut -d\' \' -f1)" =='
                    ' "${CLI_NEW}" ]]'
                ),
                f"{DEPLOY} --check",
            ],
        )

    def test_a_repeated_seed_is_judged_by_image_status_not_by_its_own_count(
        self,
    ) -> None:
        text = flat(window())
        self.assertIn("se juzga con el `image-status` del paso 7", text)
        self.assertIn("`present`", text)

    def test_the_section_writes_no_value_of_any_pin(self) -> None:
        text = window()
        self.assertIsNone(re.search(r"\b[0-9a-f]{40,}\b", text), "a hash or digest")
        self.assertIsNone(re.search(r"sha256:[0-9a-f]{8}", text), "a digest")
        self.assertIsNone(re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text), "an address")
        self.assertIn("ningún digest", text.replace("\n", " "))

    def test_every_bash_block_of_the_window_parses(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is not installed")
        blocks = bash_blocks(window())
        self.assertGreaterEqual(len(blocks), 12)
        for number, block in enumerate(blocks):
            with self.subTest(block=number):
                result = subprocess.run(
                    [bash, "-n"],
                    input=block,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_every_bash_block_of_the_window_passes_shellcheck(self) -> None:
        # The blocks are pasted into one tmux shell, so a variable assigned in
        # one and read by a later one is not unused (SC2034, the only code
        # left out).
        for number, block in enumerate(bash_blocks(window())):
            with self.subTest(block=number):
                result = subprocess.run(
                    [
                        "shellcheck",
                        "--severity=style",
                        "--shell=bash",
                        "--exclude=SC2034",
                        "-",
                    ],
                    input=block,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout)


class WindowStopTests(unittest.TestCase):
    """What stops the window, and what never provokes a refusal.

    On the host the manager re-executes every mutating command under
    host_global_operation_lock.py run, which leaves the direct marker when the
    command exits non-zero and refuses the next locked command until the
    marker is recovered. These tests keep the text from asking for a refusal,
    and from going on after a check that failed.
    """

    def test_no_locked_command_is_followed_by_a_masking_or(self) -> None:
        locked = []
        for number, block in enumerate(bash_blocks(window())):
            for statement in statements(block):
                if not is_locked(statement):
                    continue
                locked.append(statement)
                with self.subTest(block=number, statement=statement):
                    self.assertIsNone(MASKING_OR.search(statement), statement)
        # The scan found the commands it is about: the manager's two verbs,
        # the snapshot and the CLI copy.
        text = " ".join(locked)
        for fragment in ("seed-layout", "forget", "cp -a", "/usr/bin/install"):
            self.assertIn(fragment, text)

    def test_the_masking_or_is_what_the_scan_rejects(self) -> None:
        for refused in (
            f"sudo -- /usr/bin/python3 {MANAGER} forget --image-set ax || echo gone",
            f"sudo -- /usr/bin/python3 {MANAGER} seed-layout --image-set ax || true",
            f"sudo -- /usr/bin/python3 {MANAGER} forget --image-set ax || :",
            f"sudo -- /usr/bin/python3 {LOCK} /usr/bin/cp -a a b || printf x",
        ):
            with self.subTest(refused=refused):
                self.assertTrue(is_locked(refused))
                self.assertIsNotNone(MASKING_OR.search(refused))
        for allowed in (
            f"sudo -- /usr/bin/python3 {MANAGER} forget --image-set ax || stop x",
            f"sudo -- /usr/bin/python3 {MANAGER} forget --image-set ax || return",
        ):
            with self.subTest(allowed=allowed):
                self.assertIsNone(MASKING_OR.search(allowed))
        # image-status takes no lock: reading it is not a locked call.
        self.assertFalse(is_locked(f"sudo -- /usr/bin/python3 {MANAGER} image-status"))

    def test_the_stop_helpers_are_defined_before_the_first_guard(self) -> None:
        blocks = bash_blocks(window())
        defined = set()
        guards = 0
        for number, block in enumerate(blocks):
            for statement in statements(block):
                for helper in ("stop", "must", "out"):
                    if statement.startswith(f"{helper}() {{"):
                        defined.add(helper)
                if statement.startswith(("stop() {", "must() {", "out() {")):
                    continue
                guard = GUARD.match(statement) is not None
                guarded = statement.startswith(("must ", "out "))
                if not (guard or guarded):
                    continue
                guards += 1
                with self.subTest(block=number, statement=statement):
                    self.assertEqual(defined, {"stop", "must", "out"}, statement)
        self.assertGreater(guards, 25)

    def test_every_standalone_guard_stops_the_window(self) -> None:
        for number, block in enumerate(bash_blocks(window())):
            for statement in statements(block):
                if GUARD.match(statement) is None:
                    continue
                with self.subTest(block=number, statement=statement):
                    # `[[ ... ]] || stop '...'`, as one statement. A guard
                    # inside `if`, `elif` or a `&&` chain does not start the
                    # statement and decides something else.
                    self.assertRegex(statement, r"\|\|\s*stop\b[^|]*$")

    def test_must_never_runs_in_a_subshell(self) -> None:
        # The last stage of a pipe and a command substitution are subshells:
        # the STOP raised there would be lost with them, and the next line
        # would not be held back. A process substitution keeps `must` in the
        # shell that holds HALT.
        seen = 0
        for number, block in enumerate(bash_blocks(window())):
            for statement in statements(block):
                seen += statement.startswith("must ")
                with self.subTest(block=number, statement=statement):
                    self.assertIsNone(re.search(r"[^|]\|\s*(must|out)\s", statement))
                    self.assertIsNone(re.search(r"\$\((must|out|stop)\b", statement))
        self.assertGreater(seen, 25)

    def test_the_helpers_do_not_use_set_e_and_say_why(self) -> None:
        text = window()
        for block in bash_blocks(text):
            self.assertNotRegex(block, r"(?m)^\s*set\s+-\S*e")
            self.assertNotIn("trap ", block)
        self.assertIn("`set -e`", flat(text))
        self.assertIn("mataría la shell de `tmux`", flat(text))

    def test_the_helpers_depend_on_nothing_a_later_step_deletes(self) -> None:
        # `out` runs in the rollback too, after step 13 removed ${WORK}: its
        # file belongs to the helpers, which are pasted before anything else.
        helpers = block_with(HELPERS)
        self.assertNotIn("WORK", helpers)
        self.assertIn("OUTFILE=$(mktemp)", helpers)
        for block in bash_blocks(window()):
            if "rm -rf" in block and "${WORK" in block:
                self.assertNotIn("OUTFILE", block)

    def test_the_helper_functions_report_through_stop(self) -> None:
        blocks = bash_blocks(window())
        for helper in ("expect_all", "expect_complete", "expect_pinned"):
            with self.subTest(helper=helper):
                self.assertIn("|| stop", function_body(blocks, helper))
        for helper in ("seed_one", "forget_one"):
            with self.subTest(helper=helper):
                self.assertIn("stop ", function_body(blocks, helper))

    def test_every_manager_mutation_is_decided_by_a_read_first(self) -> None:
        blocks = bash_blocks(window())
        # The two verbs appear only inside their per-image functions, and each
        # of those reads the image with image-status (no lock, no marker)
        # before it asks for the lock.
        callers = {"seed-layout": "seed_one", "forget": "forget_one"}
        for number, block in enumerate(blocks):
            for statement in statements(block):
                match = LOCKED_MANAGER.search(statement)
                if match is None:
                    continue
                verb = match.group(1)
                with self.subTest(block=number, verb=verb):
                    self.assertIn(verb, callers, statement)
                    self.assertIn(
                        statement,
                        statements(function_body(blocks, callers[verb])),
                    )
        for helper in callers.values():
            lines = statements(function_body(blocks, helper))
            with self.subTest(helper=helper):
                read = next(i for i, line in enumerate(lines) if "image_state " in line)
                locked = next(i for i, line in enumerate(lines) if is_locked(line))
                self.assertLess(read, locked)
        probe = function_body(blocks, "image_state")
        self.assertIn(f"{MANAGER} image-status", flat(probe))
        self.assertNotRegex(flat(probe), r"seed-layout|forget")
        self.assertIn("with another digest", probe)

    def test_the_cli_is_verified_by_one_chain_before_anything_runs_it(self) -> None:
        chains = [
            statement
            for block in bash_blocks(window())
            for statement in statements(block)
            if 'sha256sum "${IN}/ax/ax"' in statement
        ]
        self.assertEqual(len(chains), 1)
        chain = chains[0]
        positions(
            self,
            chain,
            [
                'sha256sum "${IN}/ax/ax"',
                "&&",
                'find "${IN}"',
                "&&",
                'sudo -- chmod 0700 "${IN}/ax/ax"',
                "&&",
                "ldd",
                "'not found'",
                "&&",
                'sudo -- "${IN}/ax/ax" help >/dev/null',
                "|| stop",
            ],
        )
        # One statement, joined by && only: a `;` or another line would let a
        # failed check fall through to the next command.
        self.assertNotIn(";", chain)
        self.assertNotIn("||", chain.rsplit("|| stop", 1)[0])
        self.assertIn("! sudo -- ldd", chain)
        # `--help` exits 1 on the pinned CLI, so it checks nothing; `help`
        # exits 0 when the binary loads.
        for block in bash_blocks(window()):
            self.assertNotIn("--help", block)
        # Nothing else of the window executes the artifact.
        runs = [
            statement
            for block in bash_blocks(window())
            for statement in statements(block)
            if 'sudo -- "${IN}/ax/ax"' in statement and statement != chain
        ]
        self.assertEqual(runs, [])

    def test_a_snapshot_never_lands_on_an_earlier_attempt(self) -> None:
        text = flat(window())
        positions(
            self,
            text,
            [
                'sudo -- test ! -e "${RELOCK}"',
                'sudo -- install -d -o root -g root -m 0700 "${RELOCK}"',
                f'{LOCK} /usr/bin/cp -a "${{LAYOUT}}" "${{SNAP_LAYOUT}}"',
            ],
        )
        # What an earlier attempt left is never deleted (it can hold the only
        # copy from before the window) but set aside, and the rehearsal
        # directory, which is scratch, is cleared before it is reused.
        self.assertIn("no se borra", text)
        self.assertIn('sudo -- mv "${RELOCK}"', text)
        step_four = text.split("4. El ensayo", 1)[1].split("5. El `forget`", 1)[0]
        self.assertLess(
            step_four.index('sudo -- rm -rf "${REH:?}"'),
            step_four.index('install -d -o root -g root -m 0700 "${REH}"'),
        )

    def test_the_cli_copy_is_decided_by_its_sha256_first(self) -> None:
        installs = 0
        for number, block in enumerate(bash_blocks(window())):
            lines = statements(block)
            for index, statement in enumerate(lines):
                if "/usr/bin/install -o root -g root -m 0600" not in statement:
                    continue
                installs += 1
                with self.subTest(block=number, statement=statement):
                    # `if [[ <sha256 of the copy> == <wanted> ]]; then ...
                    # else <install> fi`: the install is the else branch.
                    head = max(i for i in range(index) if lines[i].startswith("if "))
                    self.assertIn('sha256sum "${CLI_BACKUP}"', lines[head])
                    self.assertEqual(lines[index - 1], "else")
        self.assertEqual(installs, 2)

    def test_a_retained_marker_is_explained_where_it_applies(self) -> None:
        text = flat(window())
        marker = text.split("### Un marker retenido", 1)[1].split("\n### ", 1)[0]
        for fragment in (
            "/run/lock/dockerswarm-direct.marker",
            "/run/lock/dockerswarm-ansible.marker",
            "Stale marker recovery",
            "host_global_operation_lock.py recover",
            "ansible-operation-lock.py recover --operation-id",
            "--apply --confirm 'CONFIRMACION_EXACTA_MOSTRADA'",
            "byte a byte",
            "antes de reiniciar",
            "un `deploy-ansible.sh` fallido",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, marker.replace("\n", " "))
        # The abort criteria and each step that takes the lock point at it.
        steps = text.split("### Pasos de la ventana de resiembra", 1)[1]
        steps = steps.split("### Un marker retenido", 1)[0]
        starts = {
            2: "2. La instantánea",
            4: "4. El ensayo",
            5: "5. El `forget`",
            6: "6. La siembra real",
            8: "8. La copia de la CLI",
            10: "10. `--check`",
            11: "11. El apply",
            12: "12. Verificar",
            13: "13. Cerrar.",
        }
        # Every step that takes the lock, or the wrapper's, says what a failure
        # leaves behind (the last one, 13, only deletes what the steps made).
        for number, start in starts.items():
            if number == 13:
                continue
            end = starts[next(n for n in sorted(starts) if n > number)]
            part = steps.split(start, 1)[1].split(end, 1)[0]
            with self.subTest(step=number):
                self.assertIn("«Un marker retenido»", part)
        criteria = text.split("### Criterios de aborto", 1)[1]
        criteria = criteria.split("### Marcha atrás", 1)[0]
        self.assertIn("/run/lock/dockerswarm-direct.marker", criteria)
        self.assertIn("/run/lock/dockerswarm-ansible.marker", criteria)
        self.assertIn("«Un marker retenido»", criteria)


class WindowProcedureTests(unittest.TestCase):
    def test_step_zero_holds_the_preconditions(self) -> None:
        text = flat(window())
        step_zero = text.split("### Precondiciones de la ventana de resiembra", 1)[1]
        step_zero = step_zero.split("### Pasos de la ventana de resiembra", 1)[0]
        for fragment in (
            "`sudo ax get tasks -a default`",
            "`sudo ax tunnel list`",
            "'ax-tarea-*' 'ax-cli-*'",
            "La cola de la Oficina, en pausa y leída de vuelta",
            "avisada la persona que la use",
            "`tmux`",
            "`memory.peak`",
            "grep MemAvailable /proc/meminfo",
            "`ax-web-state`",
            "`ax-redis-data`",
            "`0700` bajo `/var/backups/dockerswarm/`",
            "`ax-lab-images.yml` ya en `main`",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, step_zero)

    def test_step_zero_copies_the_office_state_and_says_why_not_redis(self) -> None:
        text = flat(window())
        step_zero = text.split("### Precondiciones de la ventana de resiembra", 1)[1]
        step_zero = step_zero.split("### Pasos de la ventana de resiembra", 1)[0]
        # The path comes from the cluster, the copy runs under the lock into a
        # root-only directory, and it is checked to hold something.
        positions(
            self,
            step_zero,
            [
                "get pvc ax-web-state",
                "{.spec.volumeName}",
                "{.spec.hostPath.path}{.spec.local.path}",
                "|| stop 'el PV de ax-web-state no trae ruta'",
                '-m 0700 "${STATE}" "${STATE}/data"',
                (
                    f"{LOCK} /usr/bin/docker cp -a "
                    '"kind-control-plane:${PV_PATH}/." "${STATE}/data"'
                ),
                'must test -n "$(sudo -- ls -A "${STATE}/data")"',
            ],
        )
        # The Redis volume is not copied, and the reason is written down.
        redis = step_zero.split("El volumen de Redis (`ax-redis-data`) no se copia")
        self.assertEqual(len(redis), 2)
        for reason in ("sin Tasks", "no cambia ni la imagen de Redis ni su volumen"):
            self.assertIn(reason, redis[1])
        self.assertNotIn(
            "copia del estado del PVC de la Oficina (`ax-web-state`) y", step_zero
        )

    def test_the_images_workflow_must_already_be_on_main(self) -> None:
        text = flat(window())
        for fragment in (
            (
                "GitHub solo ofrece `workflow_dispatch` para un workflow que ya "
                "está en la rama por defecto"
            ),
            "`ax-lab-images.yml` tiene que estar ya en `main`",
            "el PR del mecanismo, fusionado",
            "«Run workflow» ofrece la rama del PR de pines",
            "antes de la congelación",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

    def test_the_rehearsal_precedes_the_forget_and_is_asserted(self) -> None:
        text = flat(window())
        positions(
            self,
            text,
            [
                'sudo -- install -d -o root -g root -m 0700 "${REH}"',
                'seed_one substrate "${IN}/substrate" "${REH}/images"',
                'expect_complete "${#MOVED_SUB[@]}" "${OUT}"',
                'sudo -- rm -rf "${REH:?}"',
                'forget_one substrate "${LAYOUT}"',
                'seed_one substrate "${IN}/substrate" "${LAYOUT}"',
                'expect_complete 7 "${OUT}"',
            ],
        )
        # What each per-image call asserts about its own answer.
        blocks = bash_blocks(window())
        self.assertIn("expect_all 1 forgotten", function_body(blocks, "forget_one"))
        self.assertIn("expect_all 1 seeded", function_body(blocks, "seed_one"))

    def test_the_old_digests_come_from_the_previous_revision_and_the_new_from_the_plans(
        self,
    ) -> None:
        text = flat(window())
        positions(
            self,
            text,
            [
                "/opt/dockerswarm/deployments/ax-lab.yml",
                'git archive "${PREV}"',
                'plan "${WORK}/prev" --substrate-plan >"${WORK}/sub-old.json"',
                'load_plans "${WORK}"',
            ],
        )
        for fragment in (
            'plan . --substrate-plan >"${WORK}/sub-new.json"',
            'plan . --ax-plan >"${WORK}/ax-new.json"',
            # load_plans reads each array from its own plan of the directory.
            'mapfile -t NEW_SUB < <(pins "$1/sub-new.json" images)',
            'mapfile -t OLD_SUB < <(pins "$1/sub-old.json" images)',
            'mapfile -t NEW_KO < <(pins "$1/ax-new.json" ko_images)',
            'mapfile -t OLD_KO < <(pins "$1/ax-old.json" ko_images)',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

    def test_the_abort_criteria_and_the_rollback_are_per_image(self) -> None:
        text = flat(window())
        criteria = text.split("### Criterios de aborto", 1)[1]
        criteria = criteria.split("### Marcha atrás de la ventana de resiembra", 1)[0]
        self.assertIn("tiempo de espera de rollout del rol", criteria)
        self.assertIn("Es marcha atrás y no un reintento", criteria)
        rollback = text.split("### Marcha atrás de la ventana de resiembra", 1)[1]
        positions(
            self,
            rollback,
            [
                'forget_one substrate "${LAYOUT}" "${TAG_SUB}" "${new}"',
                'seed_one substrate "${SNAP_LAYOUT}" "${LAYOUT}" "${TAG_SUB}" "${old}"',
                'forget_one ax "${LAYOUT}" "${TAG_AX}" "${new}"',
                'seed_one ax "${SNAP_LAYOUT}" "${LAYOUT}" "${TAG_AX}" "${old}"',
                'expect_complete 7 "${OUT}"',
                'expect_complete 4 "${OUT}"',
                (
                    f"{LOCK} /usr/bin/install -o root -g root -m 0600"
                    ' "${SNAP_CLI}" "${CLI_BACKUP}"'
                ),
                # Both variants end off the unmerged head of the pins PR.
                'git checkout --detach "${PREV:?}"',
                'test -z "$(git status --porcelain --untracked-files=normal)"',
            ],
        )
        self.assertIn("ningún apply desde el head del PR de pines", rollback)
        self.assertIn("ni desde el commit de fusión", rollback)
        self.assertIn("cuenta como hecha", rollback)
        self.assertIn("Antes del apply", rollback)
        self.assertIn("Después del apply", rollback)
        self.assertIn("se fusiona ese mismo día un PR que lo revierta", rollback)
        self.assertIn(
            "desde el `forget` hasta que la marcha atrás termina no corre ningún apply"
            " de `ax-lab` ni de `ax-web` desde `main`",
            rollback,
        )

    def test_the_accepted_risk_and_branch_k_are_stated_plainly(self) -> None:
        text = flat(window())
        for fragment in (
            "se ejecutan por primera vez en producción",
            "La única red de seguridad es la marcha atrás desde la instantánea",
            "el propietario aceptó este riesgo",
            "`ax-task-runner` y `ax-agents` son imágenes de buildx",
            "con Go 1.27.1 y con los módulos de antes de la subida",
            "los hallazgos de `govulncheck` de su binario siguen abiertos",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)


# --------------------------------------------------------------------------
# The window run on a host with the lock of the host
#
# The tests above prove the shape of the text. Whether it provokes a refusal
# has to be run. The blocks of the window are pasted, as they are, into a bash
# that has a sudo shim, the real manager (scripts/manage-ax-lab-substrate.py,
# with only the Swarm check that sends it under the lock replaced) and the
# real host_global_operation_lock.py run_direct, on private lock and marker
# paths: a command that exits non-zero leaves the marker and the next locked
# command is refused, as on the host (where the Swarm is active).
#
# Docker, the lab's kubectl and the `ax` helper are shims that answer from
# files under world/, so the reads of the window (the Oficina, the transient
# containers, the cgroups, the pods) run for real against a state the test
# sets, and an unexpected call fails loudly instead of reaching the machine
# the test runs on. The step-1 and recovery tests also need a git repository:
# an `origin` and a clone in the sandbox, with a fake validator that prints the
# plans each commit holds.

LOCK_LAUNCHER = r"""
import importlib.util
import json
import os
import sys
from pathlib import Path

HOME = Path(os.environ["RELOCK_TEST_HOME"])
spec = importlib.util.spec_from_file_location(
    "hglock",
    Path(os.environ["RELOCK_TEST_REPO"]) / "scripts/host_global_operation_lock.py",
)
hglock = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hglock
spec.loader.exec_module(hglock)


def record(code):
    with (HOME / "run/log.jsonl").open("a") as log:
        log.write(json.dumps({"argv": sys.argv[1:], "code": code}) + "\n")


arguments = hglock.parse_arguments(sys.argv[1:])
try:
    if arguments.action == "run":
        command = arguments.command
        if command[:1] == ["/usr/bin/install"]:
            # Nothing here is root: install keeps the user's own owner.
            for option in ("-o", "-g"):
                while option in command:
                    index = command.index(option)
                    del command[index : index + 2]
        code = hglock.run_direct(
            arguments.operation,
            command,
            lock_path=HOME / "run/lock",
            marker_path=HOME / "run/direct.marker",
        )
    else:
        print(
            hglock.recover_direct_marker(
                apply=arguments.apply,
                confirmation=arguments.confirm,
                lock_path=HOME / "run/lock",
                marker_path=HOME / "run/direct.marker",
                archive_directory=HOME / "run/archive",
            )
        )
        code = 0
except (hglock.HostGlobalLockError, OSError) as error:
    print(f"ERROR: {error}", file=sys.stderr)
    code = 126
record(code)
raise SystemExit(code)
"""

MANAGER_LAUNCHER = r"""
import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "manage_ax_lab_substrate",
    Path(os.environ["RELOCK_TEST_REPO"]) / "scripts/manage-ax-lab-substrate.py",
)
manager = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = manager
spec.loader.exec_module(manager)


def ensure_host_lock(operation, docker):
    # What the host does: an active Swarm re-executes under the lock.
    if os.environ.get("RELOCK_TEST_LOCKED") == "1":
        return
    os.execv(
        sys.executable,
        [
            sys.executable,
            str(HERE / "host_global_operation_lock.py"),
            "run",
            "--operation",
            operation,
            "--",
            "/usr/bin/env",
            "RELOCK_TEST_LOCKED=1",
            sys.executable,
            __file__,
            *sys.argv[1:],
        ],
    )


manager.ensure_host_lock = ensure_host_lock
raise SystemExit(manager.main())
"""

# What the validator prints for each commit: the plans that commit holds.
VALIDATOR = r"""#!/usr/bin/env python3
import sys
from pathlib import Path

plans = Path(__file__).resolve().parent.parent / "plans"
names = {"--substrate-plan": "substrate.json", "--ax-plan": "ax.json"}
sys.stdout.write((plans / names[sys.argv[1]]).read_text())
"""

# Docker on this host, as far as the window asks it: the containers file, the
# Oficina's volume, the ID of each container (which names its cgroup).
DOCKER_SHIM = r"""#!/usr/bin/env python3
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path

WORLD = Path(os.environ["RELOCK_TEST_HOME"]) / "world"
args = sys.argv[1:]


def containers():
    path = WORLD / "containers"
    lines = path.read_text().splitlines() if path.exists() else []
    return dict(line.split("\t", 1) for line in lines if line)


if args[:1] == ["ps"]:
    filters = [
        args[index + 1][len("name=") :]
        for index, arg in enumerate(args[:-1])
        if arg == "--filter" and args[index + 1].startswith("name=")
    ]
    states = [
        args[index + 1][len("status=") :]
        for index, arg in enumerate(args[:-1])
        if arg == "--filter" and args[index + 1].startswith("status=")
    ]
    for name, status in containers().items():
        if not any(re.search(pattern, name) for pattern in filters):
            continue
        if states and not all(status.lower().startswith(state) for state in states):
            continue
        print(f"{name} {status}" if "{{.Status}}" in " ".join(args) else name)
elif args[:1] == ["inspect"]:
    print(hashlib.sha256(args[-1].encode()).hexdigest())
elif args[:1] == ["logs"]:
    print(f"log of {args[-1]}")
elif args[:1] == ["rm"]:
    found = containers()
    if args[-1] not in found:
        sys.exit(f"Error: No such container: {args[-1]}")
    del found[args[-1]]
    (WORLD / "containers").write_text(
        "".join(f"{name}\t{status}\n" for name, status in found.items())
    )
elif args[:2] == ["exec", "kind-control-plane"] and args[2:3] == ["cat"]:
    sys.stdout.write(Path(args[3]).read_text())
elif args[:2] == ["cp", "-a"] and args[2].startswith("kind-control-plane:"):
    source = Path(args[2].split(":", 1)[1].removesuffix("/."))
    shutil.copytree(source, args[3], dirs_exist_ok=True)
else:
    sys.exit(f"unexpected docker call: {args}")
"""

# The lab's kubectl, as far as the window asks it. Its answers are files.
KUBECTL_SHIM = r"""#!/usr/bin/env python3
import os
import sys
from pathlib import Path

WORLD = Path(os.environ["RELOCK_TEST_HOME"]) / "world"
args = [arg for arg in sys.argv[1:]]
for flag in ("--kubeconfig", "--context", "--request-timeout"):
    while flag in args:
        index = args.index(flag)
        del args[index : index + 2]
if "--request-timeout" in " ".join(sys.argv) and "rollout" in args:
    sys.exit("a watch with --request-timeout is cut at 10 s: refused")
if args[:3] == ["--namespace", "ax-web", "get"] and args[3:5] == ["pvc", "ax-web-state"]:
    sys.stdout.write("pvc-office")
elif args[:2] == ["get", "pv"] and args[2] == "pvc-office":
    sys.stdout.write(str(WORLD / "pv"))
elif args[:2] == ["get", "pods"] and "--all-namespaces" in args:
    sys.stdout.write((WORLD / "pods.json").read_text())
elif args[:3] == ["--namespace", "ax-workers", "get"] and args[3] == "pods":
    sys.stdout.write((WORLD / "worker-pods.json").read_text())
elif args[:3] == ["--namespace", "ax-workers", "rollout"]:
    assert args[3:6] == ["status", "deployment/ax", "--timeout=300s"], args
    code = int((WORLD / "rollout.rc").read_text())
    print("rolled out" if code == 0 else "timed out waiting for the rollout")
    sys.exit(code)
else:
    sys.exit(f"unexpected kubectl call: {args}")
"""

# `sudo ax`, the CLI of the lab with its own scope: tasks and tunnels.
AX_SHIM = r"""#!/usr/bin/env python3
import os
import sys
from pathlib import Path

WORLD = Path(os.environ["RELOCK_TEST_HOME"]) / "world"
args = sys.argv[1:]
if args == ["get", "tasks", "-a", "default"]:
    if (WORLD / "ax-server-down").exists():
        sys.exit("Error: dial tcp: connect: connection refused")
    print("NAME   ATESPACE   PHASE   ACTOR   WORKER-IP   AGE")
    sys.stdout.write((WORLD / "tasks").read_text())
elif args == ["tunnel", "list"]:
    sys.stdout.write((WORLD / "tunnels").read_text())
else:
    sys.exit(f"unexpected ax call: {args}")
"""

# The wrapper of the playbooks and `ax-tarea`: they only record that they ran.
DEPLOY_SHIM = r"""#!/usr/bin/env bash
home=${RELOCK_TEST_HOME}
printf '%s\n' "$*" >>"${home}/run/deploy.log"
exit "$(cat "${home}/world/deploy.rc")"
"""

TAREA_SHIM = r"""#!/usr/bin/env bash
home=${RELOCK_TEST_HOME}
printf '%s %s %s\n' "${TURNOS:-}" "${RAMA:-}" "$*" >>"${home}/run/tarea.log"
exit "$(cat "${home}/world/tarea.rc")"
"""

# The admin the window runs as is made root by sudo. Nothing here is root, so
# sudo only drops itself, and what names root (the owner of an installed file,
# the owner a find looks for) names this user. On the host sudo runs root with
# the umask 0027 whatever the admin's one (0007), which the shim reproduces.
PREAMBLE = r"""
umask "${UMASK:-077}"
cd "${CWD}" || exit 1
export RELOCK_TEST_REPO RELOCK_TEST_HOME TMPDIR="${T}/work"
ME=$(id -un)
sudo() {
  if [[ "$1" == -- ]]; then
    shift
  fi
  (
    umask "${SUDO_UMASK:-0027}"
    local args=() arg
    case "$1" in
    install)
      shift
      while (($#)); do
        case "$1" in
        -o | -g) shift 2 ;;
        *) args+=("$1"); shift ;;
        esac
      done
      /usr/bin/install "${args[@]}"
      ;;
    find)
      shift
      for arg in "$@"; do
        args+=("${arg/#root/${ME}}")
      done
      command find "${args[@]}"
      ;;
    *) "$@" ;;
    esac
  )
}
systemctl() { cat "${T}/world/scopes"; }
"""

# The rollback loop of the window as it was before the guards: a forget that
# the manager refuses (the entry names the old digest) and a `|| echo` after
# it. Kept to show the harness can see the refusal it is there to prevent.
OLD_ROLLBACK_LOOP = r"""
for old in "${STALE_SUB[@]}"; do
  name=${old#--image=}; name=${name%%=*}
  new=$(printf '%s\n' "${MOVED_SUB[@]}" | grep -F -- "--image=${name}=")
  sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py forget \
    --image-set substrate --layout "${LAYOUT}" --tag "${TAG_SUB}" \
    "${new}" || echo "${name}: ausente o ya restaurada"
  sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py seed-layout \
    --image-set substrate --source "${SNAP_LAYOUT}" --layout "${LAYOUT}" \
    --tag "${TAG_SUB}" "${old}"
done
"""

HELPERS = "stop() {"
PLAN_FUNCTIONS = "load_plans() {"
DEFINITIONS = "image_state() {"
READS = "office_idle() {"
STEP_1 = "PR_HEAD='PEGAR"
STEP_1_FACTS = 'ax-lab.yml\nload_facts "${WORK}" "${OLD_CFG}"'
STEP_2 = "df -h /var/backups"
STEP_3 = 'zipfile -e "${SUBSTRATE_ZIP:?}"'
STEP_4 = 'seed_one substrate "${IN}/substrate" "${REH}/images"'
STEP_5 = 'forget_one substrate "${LAYOUT}" "${TAG_SUB}" "${old}"'
STEP_6 = 'seed_one substrate "${IN}/substrate" "${LAYOUT}"'
STEP_7 = '"${NEW_KO[@]}" "${BUILDX[@]}"\nexpect_complete 4'
STEP_8 = '"${IN}/ax/ax" "${CLI_BACKUP}"'
STEP_9 = "git checkout --detach origin/main"
STEP_11_READS = 'record_is "${PREV:?}"\noffice_idle'
STEP_11_BASELINE = 'lab_memory >"${WORK}/memory.txt"'
STEP_12_VERIFY = '"${NEW_KO[@]}" "${BUILDX[@]}"\nexpect_pinned 4'
STEP_12_GATE = 'worker_gate "${WORKER_NEW}"'
STEP_12_COMPARE = 'must diff <(sudo -- cat "${RELOCK}/baseline/memory.txt"'
RECOVER = 'sudo -- test -f "${RELOCK}/window.env"'
ROLLBACK = 'forget_one substrate "${LAYOUT}" "${TAG_SUB}" "${new}"'
ROLLBACK_PREV = 'record_is "${PREV:?}" ${MERGE:+"${MERGE}"}'
TRANSIENT = "LEFTOVER='"
RECOVERY = "scripts/host_global_operation_lock.py recover"
CLI_CHAIN = 'sha256sum "${IN}/ax/ax"'
PVC_COPY = 'must test -n "$(sudo -- ls -A "${STATE}/data")"'


def block_with(anchor: str, *, without: str | None = None) -> str:
    found = [
        block
        for block in bash_blocks(window())
        if anchor in block and (without is None or without not in block)
    ]
    if len(found) != 1:
        raise AssertionError(f"{anchor!r} must be in one block, not {len(found)}")
    return found[0]


def make_directories(path: Path) -> None:
    """mkdir -p with mode 0700 on every component (a umask may be 0007)."""
    for directory in reversed([path, *path.parents]):
        if not directory.exists():
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)


def sha1(label: str) -> str:
    """A commit-shaped value, assembled here (a literal would be scanned)."""
    return hashlib.sha256(label.encode()).hexdigest()[:40]


class Sandbox:
    """A host of the lab in a temporary directory, and the window run on it.

    With `repo`, the operational clone is a git repository with an origin, a
    commit PREV, a branch of the pins PR and a validator that prints the plans
    each commit holds; without it, the variables of the first two steps are
    injected, as the tests that start from the snapshot have always done.
    """

    # Two Substrate images and one of the two ko images move: enough to leave a
    # window stopped half way in each set.
    SUBSTRATE_MOVED = manager.IMAGE_NAMES[:2]
    KO_MOVED = manager.AX_KO_IMAGES[:1]
    CLI_OLD = b"the CLI the copy holds\n"
    CLI_NEW = (
        b'#!/bin/sh\n[ "$1" = help ] && touch "${RELOCK_TEST_HOME}/witness"; exit 0\n'
    )
    PLACEHOLDER = "PR_HEAD='PEGAR_AQUÍ_EL_COMMIT_COMPLETO_DEL_HEAD_DEL_PR'"

    def __init__(self, case: unittest.TestCase, *, repo: bool = False) -> None:
        temporary = tempfile.TemporaryDirectory()
        case.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        for name in (
            "run",
            "work",
            "bin",
            "sbin",
            "deployments",
            "backups/ax-lab/binaries",
            "world/pv",
        ):
            make_directories(self.root / name)
        for name, source in {
            "docker": DOCKER_SHIM,
            "kubectl": KUBECTL_SHIM,
        }.items():
            self.executable(self.root / "bin" / name, source)
        self.executable(self.root / "sbin/ax", AX_SHIM)
        self.executable(self.root / "sbin/ax-tarea", TAREA_SHIM)
        (self.root / "run/lock").touch(mode=0o600)
        (self.root / "run/lock").chmod(0o600)
        self.live = self.root / "backups/ax-lab/images"
        self.cli = self.root / "backups/ax-lab/binaries/ax"
        self.relock = self.root / "backups/ax-lab-relock"
        self.inputs = self.relock / "in"
        # The lab as it is before the window, and the images the pins PR moves.
        self.old = {name: make_image(f"{name}-old") for name in LAB_TAGS}
        self.new = {name: make_image(f"{name}-new") for name in RELOCKED}
        write_backup(
            self.live,
            {f"{name}:{tag}": self.old[name] for name, tag in LAB_TAGS.items()},
        )
        self.cli.write_bytes(self.CLI_OLD)
        self.cli.chmod(0o600)
        self.repo = repo
        self.git_env = {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "window",
            "GIT_AUTHOR_EMAIL": "window@example.test",
            "GIT_COMMITTER_NAME": "window",
            "GIT_COMMITTER_EMAIL": "window@example.test",
        }
        self.commits = 0
        if repo:
            self.cwd = self.root / "repo"
            self.init_repo()
        else:
            self.cwd = self.root
            self.write_scripts(self.root / "scripts")
            self.prev = sha1("the commit the record names")
            self.pr_head = sha1("the head of the pins PR")
            self.merge = sha1("the commit the PR is merged as")
            self.write_work()
        self.reset_world()
        self.write_record(self.prev)

    @staticmethod
    def executable(path: Path, source: str) -> None:
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)

    # What the repository holds, and the facts the window derives from it.
    def write_scripts(self, scripts: Path) -> None:
        make_directories(scripts)
        for name, source in {
            "host_global_operation_lock.py": LOCK_LAUNCHER,
            "manage-ax-lab-substrate.py": MANAGER_LAUNCHER,
        }.items():
            (scripts / name).write_text(source, encoding="utf-8")
        self.executable(scripts / "deploy-ansible.sh", DEPLOY_SHIM)

    def digest(self, which: str, name: str) -> str:
        moved = which == "new" and name in (*self.SUBSTRATE_MOVED, *self.KO_MOVED)
        return (self.new if moved else self.old)[name]["digest"]

    def plan_documents(self, which: str) -> dict[str, Any]:
        cli = self.CLI_NEW if which == "new" else self.CLI_OLD
        return {
            "substrate": {
                "images": {
                    name: self.digest(which, name) for name in manager.IMAGE_NAMES
                },
                "version": TAG,
            },
            "ax": {
                "ko_images": {
                    name: self.digest(which, name) for name in manager.AX_KO_IMAGES
                },
                "tag": AX_TAG,
                "cli_sha256": hashlib.sha256(cli).hexdigest(),
            },
        }

    def contract(self) -> str:
        pins = {
            name: self.old[name]["digest"] for name in ("ax-task-runner", "ax-agents")
        }
        lines = [
            "ax_lab:",
            "  ax:",
            f"    tag: {AX_TAG}",
            "    images:",
            *(f"      {name}: {digest}" for name, digest in pins.items()),
            "  web:",
            "    image:",
            f"      tag: '{WEB_TAG}'",
            f"      digest: {self.old['ax-web']['digest']}",
        ]
        return "\n".join(lines) + "\n"

    def write_work(self) -> None:
        """What step 1 leaves in ${WORK}, for the tests that start after it."""
        work = self.root / "work"
        for which, label in (("old", "old"), ("new", "new")):
            documents = self.plan_documents(which)
            (work / f"sub-{label}.json").write_text(json.dumps(documents["substrate"]))
            (work / f"ax-{label}.json").write_text(json.dumps(documents["ax"]))
        make_directories(work / "prev/config")
        (work / "prev/config/ax-lab.yml").write_text(self.contract())

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        self.commits += 1
        when = f"2026-10-12T00:{self.commits:02d}:00Z"
        done = subprocess.run(
            ["git", *arguments],
            cwd=cwd or self.cwd,
            env={
                **os.environ,
                **self.git_env,
                "GIT_AUTHOR_DATE": when,
                "GIT_COMMITTER_DATE": when,
            },
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def commit(self, message: str, cwd: Path | None = None) -> str:
        self.git("add", "--all", cwd=cwd)
        self.git("commit", "--quiet", "-m", message, cwd=cwd)
        return self.git("rev-parse", "HEAD", cwd=cwd)

    def write_plans(self, which: str, directory: Path | None = None) -> None:
        plans = (directory or self.cwd) / "plans"
        make_directories(plans)
        documents = self.plan_documents(which)
        (plans / "substrate.json").write_text(json.dumps(documents["substrate"]))
        (plans / "ax.json").write_text(json.dumps(documents["ax"]))

    def init_repo(self) -> None:
        origin = self.root / "origin.git"
        self.git(
            "init",
            "--quiet",
            "--bare",
            "--initial-branch=main",
            str(origin),
            cwd=self.root,
        )
        self.git("clone", "--quiet", str(origin), str(self.cwd), cwd=self.root)
        scripts = self.cwd / "scripts"
        self.write_scripts(scripts)
        self.executable(scripts / "validate-ax-lab.py", VALIDATOR)
        make_directories(self.cwd / "config")
        (self.cwd / "config/ax-lab.yml").write_text(self.contract())
        self.write_plans("old")
        (self.cwd / ".git/info/exclude").write_text(".venv\n__pycache__/\n")
        make_directories(self.cwd / ".venv/bin")
        (self.cwd / ".venv/bin/python").symlink_to(sys.executable)
        self.prev = self.commit("the lab as the last apply left it")
        self.git("push", "--quiet", "origin", "main")
        self.git("checkout", "--quiet", "-b", "pr")
        self.write_plans("new")
        self.pr_head = self.commit("the pins of the PR")
        self.git("push", "--quiet", "origin", "pr")
        self.git("checkout", "--quiet", "--detach", self.prev)
        self.merge = ""

    def merge_pr(self) -> str:
        """The PR merged into main, as the maintainer does it (a merge commit)."""
        maintainer = self.root / "maintainer"
        self.git(
            "clone",
            "--quiet",
            str(self.root / "origin.git"),
            str(maintainer),
            cwd=self.root,
        )
        self.git(
            "merge",
            "--quiet",
            "--no-ff",
            "-m",
            "merge the pins",
            "origin/pr",
            cwd=maintainer,
        )
        self.merge = self.git("rev-parse", "HEAD", cwd=maintainer)
        self.git("push", "--quiet", "origin", "HEAD:main", cwd=maintainer)
        return self.merge

    # What the host holds.
    def write_record(self, revision: str) -> None:
        """/opt/dockerswarm/deployments/ax-lab.yml, as deployment_metadata writes it."""
        (self.root / "deployments/ax-lab.yml").write_text(
            '---\nschema_version: 1\ncomponent: "ax-lab"\n'
            f'revision: "{revision}"\nprofile: "production"\n'
        )

    def reset_world(self) -> None:
        """The lab idle: the Oficina paused, nothing running, memory to spare."""
        world = self.root / "world"
        (world / "containers").write_text("")
        (world / "tasks").write_text("")
        (world / "tunnels").write_text("No active tunnels found in ~/.ax/tunnels.\n")
        (world / "scopes").write_text("")
        (world / "rollout.rc").write_text("0")
        (world / "deploy.rc").write_text("0")
        (world / "tarea.rc").write_text("0")
        self.write_office(paused=True, schedules=[], statuses=["hecho", "fallido"])
        self.write_pods([], [])
        (self.root / "meminfo").write_text(
            "MemTotal: 16000000 kB\nMemAvailable: 4000000 kB\n"
        )
        for name in ("kind-control-plane", "kind-registry"):
            self.write_cgroup(name)

    def write_office(
        self, *, paused: bool, schedules: list[bool], statuses: list[str]
    ) -> None:
        pv = self.root / "world/pv"
        office = {
            "schema": 1,
            "settings": {"queue_paused": paused},
            "schedules": [
                {"id": f"s{n}", "enabled": on} for n, on in enumerate(schedules)
            ],
        }
        jobs = {
            "schema": 1,
            "jobs": [{"id": f"j{n}", "status": s} for n, s in enumerate(statuses)],
        }
        (pv / "office.json").write_text(json.dumps(office))
        (pv / "jobs.json").write_text(json.dumps(jobs))

    def write_pods(
        self, pods: list[tuple[str, str, int]], workers: list[dict[str, Any]]
    ) -> None:
        """Every pod as (namespace, name, restarts); the worker pods as is."""
        items = [
            {
                "metadata": {"namespace": namespace, "name": name},
                "status": {"containerStatuses": [{"restartCount": restarts}]},
            }
            for namespace, name, restarts in pods
        ]
        (self.root / "world/pods.json").write_text(json.dumps({"items": items}))
        (self.root / "world/worker-pods.json").write_text(
            json.dumps({"items": workers})
        )

    def write_cgroup(
        self,
        name: str,
        events: dict[str, int] | None = None,
        local: dict[str, int] | None = None,
    ) -> None:
        """The memory files of a container's cgroup (memory.peak is never read)."""
        identifier = hashlib.sha256(name.encode()).hexdigest()
        scope = self.root / "cgroup" / f"docker-{identifier}.scope"
        make_directories(scope)
        zero = {"oom": 0, "oom_kill": 0, "oom_group_kill": 0}
        for file, counters in (
            ("memory.events", {**zero, **(events or {})}),
            ("memory.events.local", {**zero, **(local or {})}),
        ):
            (scope / file).write_text(
                "low 0\nmax 7\n" + "".join(f"{k} {v}\n" for k, v in counters.items())
            )
        (scope / "memory.stat").write_text("anon 1380769792\nfile 1080438784\n")
        (scope / "memory.current").write_text("2552602624\n")

    # What the window computes in its first step.
    @staticmethod
    def pin(images: dict[str, Any], name: str) -> str:
        return f"--image={name}={images[name]['digest']}"

    def variables(self) -> str:
        old, new = self.old, self.new
        moved = self.SUBSTRATE_MOVED
        arrays = {
            "OLD_SUB": [self.pin(old, name) for name in manager.IMAGE_NAMES],
            "NEW_SUB": [
                self.pin(new if name in moved else old, name)
                for name in manager.IMAGE_NAMES
            ],
            "STALE_SUB": [self.pin(old, name) for name in moved],
            "MOVED_SUB": [self.pin(new, name) for name in moved],
            "OLD_KO": [self.pin(old, name) for name in manager.AX_KO_IMAGES],
            "NEW_KO": [
                self.pin(new if name in self.KO_MOVED else old, name)
                for name in manager.AX_KO_IMAGES
            ],
            "STALE_KO": [self.pin(old, name) for name in self.KO_MOVED],
            "MOVED_KO": [self.pin(new, name) for name in self.KO_MOVED],
            "BUILDX": [self.pin(old, name) for name in ("ax-task-runner", "ax-agents")],
        }
        scalars = {
            "WORK": str(self.root / "work"),
            "OLD_CFG": str(self.root / "work/prev/config/ax-lab.yml"),
            "PR_HEAD": self.pr_head,
            "PREV": self.prev,
            "TAG_SUB": TAG,
            "TAG_AX": AX_TAG,
            "WEB_TAG": WEB_TAG,
            "WEB_PIN": self.pin(old, "ax-web"),
            "CLI_OLD": hashlib.sha256(self.CLI_OLD).hexdigest(),
            "CLI_NEW": hashlib.sha256(self.CLI_NEW).hexdigest(),
            "WORKER_OLD": old["ateom-gvisor"]["digest"],
            "WORKER_NEW": old["ateom-gvisor"]["digest"],
        }
        lines = [f"{key}={shlex.quote(value)}" for key, value in scalars.items()]
        lines += [
            f"{key}=({' '.join(shlex.quote(value) for value in values)})"
            for key, values in arrays.items()
        ]
        return "\n".join(lines) + "\n"

    def local(self, text: str) -> str:
        """The block, with the host's paths moved into the sandbox."""
        root = str(self.root)
        for host, here in (
            ("/var/backups/dockerswarm", f"{root}/backups"),
            ("/run/lock/dockerswarm-*.marker", f"{root}/run/*.marker"),
            ("/opt/dockerswarm/deployments", f"{root}/deployments"),
            ("/opt/dockerswarm/ax-lab/bin", f"{root}/bin"),
            ("/usr/local/sbin/ax", f"{root}/sbin/ax"),
            ("/usr/bin/docker", f"{root}/bin/docker"),
            ("/proc/meminfo", f"{root}/meminfo"),
            ("/run/lock/dockerswarm-iac.lock", f"{root}/run/lock"),
            ("/sys/fs/cgroup/system.slice", f"{root}/cgroup"),
            (self.PLACEHOLDER, f"PR_HEAD={self.pr_head}"),
        ):
            text = text.replace(host, here)
        return text

    def definitions(self) -> list[str]:
        """The step-0 blocks every paste starts with, after the helpers."""
        return [
            self.local(block_with(anchor))
            for anchor in (HELPERS, PLAN_FUNCTIONS, DEFINITIONS, READS)
        ]

    def run(
        self,
        *blocks: str,
        inject: bool = True,
        umask: str = "077",
        sudo_umask: str = "0027",
        prelude: str = "",
    ) -> types.SimpleNamespace:
        """Paste the blocks, one after the other, into one shell."""
        script = "".join(
            (
                PREAMBLE,
                *self.definitions(),
                self.variables() if inject else "",
                prelude,
                *(self.local(block) + "\n" for block in blocks),
                'echo "@@HALT=[${HALT}]"\n',
            )
        )
        env = {
            **os.environ,
            **self.git_env,
            "T": str(self.root),
            "CWD": str(self.cwd),
            "UMASK": umask,
            "SUDO_UMASK": sudo_umask,
            "RELOCK_TEST_REPO": str(ROOT),
            "RELOCK_TEST_HOME": str(self.root),
        }
        done = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", script],
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=600,
        )
        halt = re.search(r"@@HALT=\[(.*?)\]", done.stdout, flags=re.DOTALL)
        return types.SimpleNamespace(
            code=done.returncode,
            output=done.stdout + done.stderr,
            halt=halt.group(1) if halt else None,
        )

    def extract(self) -> None:
        """Step 3's extraction: the artifacts of the run on the PR head."""
        make_directories(self.inputs / "ax")
        self.write_artifacts(self.inputs)
        (self.inputs / "ax/ax").chmod(0o644)

    def write_artifacts(self, directory: Path) -> None:
        """What the two artifacts of the CI hold, under `directory`."""
        make_directories(directory / "ax")
        write_build_output(
            directory / "substrate",
            {name: self.new[name] for name in self.SUBSTRATE_MOVED},
            TAG,
        )
        for name in self.KO_MOVED:
            write_ko_layout(directory / "ax" / f"ko-{name}", self.new[name])
        (directory / "ax/ax").write_bytes(self.CLI_NEW)
        (directory / "ax/pins.json").write_text("{}")

    def zips(self) -> dict[str, str]:
        """The two downloads of the run, as the zip files the window extracts."""
        staging = self.root / "staging"
        self.write_artifacts(staging)
        downloads = self.root / "downloads"
        make_directories(downloads)
        paths = {}
        for variable, directory in (("SUBSTRATE_ZIP", "substrate"), ("AX_ZIP", "ax")):
            base = staging / directory
            archive = downloads / f"{directory}.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                for path in sorted(base.rglob("*")):
                    if path.is_file():
                        bundle.write(path, path.relative_to(base).as_posix())
            paths[variable] = str(archive)
        return paths

    def source(self, name: str) -> Path:
        if name in self.KO_MOVED:
            return self.inputs / "ax" / f"ko-{name}"
        return self.inputs / "substrate"

    def moved(self) -> tuple[str, ...]:
        return (*self.SUBSTRATE_MOVED, *self.KO_MOVED)

    def named(self, name: str) -> str | None:
        with manager.Layout(self.live) as layout:
            layout.open()
            entry = layout.reference(f"{name}:{LAB_TAGS[name]}")
        return None if entry is None else entry["digest"]

    def forget(self, name: str) -> None:
        """What a window stopped after some of its forgets leaves."""
        digest = self.old[name]["digest"]
        with manager.Layout(self.live) as layout:
            manager.forget_images(layout, LAB_TAGS[name], {name: digest})

    def seed(self, name: str) -> None:
        """What a window stopped after some of its seeds leaves."""
        digest = self.new[name]["digest"]
        with (
            manager.Layout(self.source(name), strict=False) as source,
            manager.Layout(self.live) as layout,
        ):
            manager.seed_layout(source, layout, LAB_TAGS[name], {name: digest})

    def assert_live(self, case: unittest.TestCase, which: str, names=None) -> None:
        """Every image that moves is named with its old or its new digest."""
        images = self.old if which == "old" else self.new
        for name in names or self.moved():
            with case.subTest(image=name, live=which):
                case.assertEqual(self.named(name), images[name]["digest"])

    # What the lock did.
    def calls(self) -> list[dict[str, Any]]:
        log = self.root / "run/log.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    @property
    def marker(self) -> bool:
        return (self.root / "run/direct.marker").exists()

    def assert_clean(
        self, case: unittest.TestCase, result: types.SimpleNamespace
    ) -> None:
        """Nothing stopped, nothing was refused, no marker was left."""
        case.assertEqual(result.halt, "", result.output)
        case.assertNotIn("STOP", result.output)
        case.assertFalse(self.marker, result.output)
        refused = [call for call in self.calls() if call["code"] != 0]
        case.assertEqual(refused, [], result.output)


STEP_STARTS = {
    1: "1. En el clon operativo",
    2: "2. La instantánea",
    3: "3. Extraer los artefactos",
    4: "4. El ensayo",
    5: "5. El `forget`",
    6: "6. La siembra real",
    7: "7. Comprobar la copia",
    8: "8. La copia de la CLI",
    9: "9. Fusionar el PR de pines",
    10: "10. `--check`",
    11: "11. El apply",
    12: "12. Verificar",
    13: "13. Cerrar.",
}
SECTIONS = (
    "### Reglas de la ventana de resiembra",
    "### Precondiciones de la ventana de resiembra",
    "### Pasos de la ventana de resiembra",
    "### Lo que se ve durante el apply",
    "### Recuperar la shell",
    "### Un marker retenido",
    "### Criterios de aborto",
    "### Marcha atrás de la ventana de resiembra",
)


def section(heading: str, text: str | None = None) -> str:
    """The raw text of one `###` section of the window."""
    text = window() if text is None else text
    return text.split(heading + "\n", 1)[1].split("\n### ", 1)[0]


def step_text(number: int) -> str:
    """The raw text of one numbered step (0 is the preconditions)."""
    if number == 0:
        return section("### Precondiciones de la ventana de resiembra")
    steps = section("### Pasos de la ventana de resiembra", window() + "\n### ")
    steps = steps.split("\n### ", 1)[0]
    # Step 13 runs to the end of the `###` section: the later `###` headings
    # (what you will see, recovery) are not part of it.
    start = steps.index(STEP_STARTS[number])
    if number == 13:
        return steps[start:]
    return steps[start : steps.index(STEP_STARTS[number + 1])]


def blocks_of(text: str) -> list[str]:
    return bash_blocks(text)


def one_block(text: str, anchor: str) -> str:
    found = [block for block in bash_blocks(text) if anchor in block]
    if len(found) != 1:
        raise AssertionError(f"{anchor!r} must be in one block, not {len(found)}")
    return found[0]


MARKERS_GUARD = "must test -z \"$(compgen -G '/run/lock/dockerswarm-*.marker')\""


class WindowAuditDocsTests(unittest.TestCase):
    """What the audits of the window found, held in the text."""

    def test_the_sections_come_in_the_order_a_reader_needs_them(self) -> None:
        text = window()
        positions(self, text, [f"{heading}\n" for heading in SECTIONS])

    # -- Fresh-shell recovery --------------------------------------------------
    def test_pr_head_has_a_documented_assignment_before_any_use(self) -> None:
        blocks = bash_blocks(window())
        assignments = [
            (number, statement)
            for number, block in enumerate(blocks)
            for statement in statements(block)
            if re.match(r"PR_HEAD=", statement)
        ]
        # One assignment of its own, with a quoted placeholder (an unquoted
        # <...> is a bash redirection), and the recovery's `PR_HEAD=''`.
        placed = [item for item in assignments if "PEGAR" in item[1]]
        self.assertEqual(len(placed), 1, assignments)
        number, statement = placed[0]
        self.assertRegex(statement, r"^PR_HEAD='[^'$<>]+'$")
        self.assertIn(Sandbox.PLACEHOLDER, step_text(1))
        # Nothing reads it before that assignment, and the guard that follows
        # it is the regex of a full commit, as a stop.
        for earlier in blocks[:number]:
            if "() {" not in earlier:  # the definitions only name it in a function
                self.assertNotIn("${PR_HEAD", earlier)
        lines = statements(blocks[number])
        guard = lines[lines.index(statement) + 1]
        self.assertRegex(
            guard, r'^\[\[ "\$\{PR_HEAD\}" =~ \^\[0-9a-f\]\{40\}\$ \]\]\s+\|\| stop '
        )
        self.assertIn("se asigna aquí, una sola vez", flat(step_text(1)))

    def test_step_two_persists_the_facts_root_only_outside_tmpfs(self) -> None:
        defs = block_with(PLAN_FUNCTIONS)
        # RELOCK is under /var/backups: /tmp is a tmpfs on this host.
        self.assertRegex(defs, r"(?m)^RELOCK=/var/backups/dockerswarm/ax-lab-relock$")
        self.assertNotIn("/tmp", defs)
        save = function_body([defs], "save_window")
        for fragment in (
            'printf \'PR_HEAD=%s\\nPREV=%s\\n\' "${PR_HEAD:?}" "${PREV:?}"',
            "MERGE=%s",
            'must sudo -- install -m 0600 -o root -g root "${WORK}/window.env"',
            '"${RELOCK}/window.env"',
        ):
            self.assertIn(fragment, flat(save))
        block = block_with(STEP_2)
        lines = [" ".join(line.split()) for line in statements(block)]
        wanted = [
            'must sudo -- install -d -o root -g root -m 0700 "${RELOCK}" "${RELOCK}/plans"',
            "save_window",
            "for file in sub-old sub-new ax-old ax-new; do",
            'must sudo -- install -m 0600 -o root -g root "${WORK}/${file}.json" "${RELOCK}/plans/"',
            'must sudo -- install -m 0600 -o root -g root "${OLD_CFG}" "${RELOCK}/plans/ax-lab-old.yml"',
            f'must sudo -- /usr/bin/python3 {LOCK} /usr/bin/cp -a "${{LAYOUT}}" "${{SNAP_LAYOUT}}"',
        ]
        index = -1
        for fragment in wanted:
            found = [
                i for i, line in enumerate(lines) if line == fragment and i > index
            ]
            self.assertTrue(found, f"{fragment!r} is missing or out of order")
            index = found[0]
        # Persisted before the snapshot is taken, so that a failed copy leaves
        # them with the half snapshot (set aside with it).
        self.assertLess(
            lines.index("save_window"),
            next(i for i, line in enumerate(lines) if "cp -a" in line),
        )
        # The merge commit joins them after step 9, once the plans were compared.
        nine = statements(block_with(STEP_9))
        self.assertLess(
            nine.index("MERGE=$(git rev-parse HEAD)"),
            nine.index("save_window"),
        )
        self.assertEqual(
            len([line for line in nine if line.startswith("must diff")]), 2
        )
        self.assertLess(
            max(i for i, line in enumerate(nine) if line.startswith("must diff")),
            nine.index("save_window"),
        )
        prose = flat(step_text(2))
        for fragment in ("`window.env`", "`plans/`", "de root", "fuera de `/tmp`"):
            self.assertIn(fragment, prose)

    def test_the_recovery_rebuilds_from_relock_alone(self) -> None:
        section_text = section("### Recuperar la shell")
        block = one_block(section_text, RECOVER)
        code = "\n".join(statements(block))
        # The facts and the plans come from RELOCK, read as root.
        for fragment in (
            'sudo -- cat "${RELOCK}/window.env"',
            'sudo -- cat "${RELOCK}/plans/${file}.json"',
            'sudo -- cat "${RELOCK}/plans/ax-lab-old.yml"',
        ):
            self.assertIn(fragment, code)
        # Through the functions that step 1 uses, not a copy of them.
        self.assertIn('load_plans "${WORK}"', code)
        self.assertIn('load_facts "${WORK}" "${OLD_CFG}"', code)
        self.assertNotRegex(code, r"\(\) \{")
        self.assertEqual(code.count("comm -"), 0)
        # window.env is parsed, never executed.
        for forbidden in ("source ", "eval ", "\n. "):
            self.assertNotIn(forbidden, "\n" + code)
        self.assertIn("PR_HEAD | PREV | MERGE)", code)
        # What the rest of the window created or left is not read: not the
        # extracted artifacts, the rehearsal, the copy of the office, the
        # record of the last apply (rewritten after each good apply), nor the
        # WORK of the lost shell, which is a new one made by the block itself.
        for forbidden in ("${IN}", "${REH}", "${STATE}", "${RECORD}", "deployments"):
            self.assertNotIn(forbidden, code)
        self.assertLess(code.index("WORK=$(mktemp -d)"), code.index("${WORK}/"))
        prose = flat(section_text)
        for fragment in (
            "Tampoco se reconstruyen del registro del último apply",
            "`deployment_metadata` lo reescribe con cada apply correcto",
            "vive en `/tmp`, que aquí es un tmpfs",
            "Antes del paso 2 no hay nada que recuperar",
            "los ayudantes y los tres bloques de definiciones del paso 0",
        ):
            self.assertIn(fragment, prose)

    def test_the_block_that_deletes_work_deletes_nothing_the_recovery_reads(
        self,
    ) -> None:
        blocks = bash_blocks(window())
        deleting = [b for b in blocks if re.search(r"\brm -rf\b.*\$\{WORK", b)]
        self.assertEqual(len(deleting), 1)
        deleted = re.findall(r'rm -rf "(\$\{[A-Z]+):?\??\}"', deleting[0])
        self.assertEqual(sorted(deleted), ["${IN", "${WORK"])
        defs = block_with(PLAN_FUNCTIONS)
        relock = re.search(r"(?m)^RELOCK=(\S+)$", defs).group(1)
        deleted_paths = [f"{relock}/in"]  # IN=${RELOCK}/in, WORK is a scratch dir
        self.assertIn("IN=${RELOCK}/in", defs)
        recovery = one_block(section("### Recuperar la shell"), RECOVER)
        reads = re.findall(r'"\$\{RELOCK\}(/[^"]*)"', recovery)
        self.assertTrue(reads)
        for read in reads:
            for gone in deleted_paths:
                self.assertFalse(
                    (relock + read).startswith(gone + "/") or relock + read == gone,
                    read,
                )
        # Nothing deletes RELOCK itself in a block: the owner's acceptance does,
        # in prose, once the rollback is no longer possible.
        for block in blocks:
            self.assertNotRegex(block, r'rm -rf "\$\{RELOCK')
        prose = flat(step_text(13))
        for fragment in (
            "`${IN}` y `${WORK}` y nada más",
            "`window.env`, `plans/` y `baseline/`",
            "se borra `${RELOCK}` entera",
        ):
            self.assertIn(fragment, prose)

    # -- Freeze, locks, record -------------------------------------------------
    def test_the_freeze_starts_at_step_zero_and_names_every_user_of_the_lock(
        self,
    ) -> None:
        rules = flat(section("### Reglas de la ventana de resiembra"))
        for fragment in (
            "Desde el paso 0 hasta el último `changed=0`",
            "No empieza en el `forget`",
            "todos los playbooks",
            "`validate-iac.sh` y `lint.sh`",
            "los tests del repositorio que corren bajo ese lock",
            "las sondas de la Oficina (operaciones `ax-office-*`)",
            "`lock_free` mira con `fuser` que nadie tiene abierto `/run/lock/dockerswarm-iac.lock`",
            "toda sesión de agente del host",
            "en este host corren varias a la vez",
            "El clon operativo es solo de la ventana",
            "«source HEAD changed during deployment»",
            "un único registro de la última invocación",
            "impide el `changed=0`",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, rules)
        # What the text says about the lock is what the repo does.
        for path in ("scripts/validate-iac.sh", "scripts/lint.sh"):
            self.assertIn("host-global-docker-validation-lock.sh", read(path))
        self.assertIn(
            "source HEAD changed during deployment",
            read("scripts/deploy-ansible.sh"),
        )

    def test_a_record_that_moved_stops_steps_five_and_eleven_and_the_prev_apply(
        self,
    ) -> None:
        defs = block_with(PLAN_FUNCTIONS)
        record_is = flat(function_body([defs], "record_is"))
        self.assertIn(
            'sed -n \'s/^revision: "\\([a-f0-9]\\{40\\}\\)"$/\\1/p\' "${RECORD}"',
            record_is,
        )
        self.assertIn("un apply ajeno movió el registro", record_is)
        self.assertIn("RECORD=/opt/dockerswarm/deployments/ax-lab.yml", defs)
        five = statements(block_with(STEP_5))
        self.assertLess(
            five.index('record_is "${PREV:?}"'),
            next(i for i, line in enumerate(five) if line.startswith("for old")),
        )
        eleven = statements(block_with(STEP_11_READS))
        self.assertLess(
            eleven.index('record_is "${PREV:?}"'), eleven.index("office_idle")
        )
        # Before the PREV apply it names PREV, or the merge commit after a good
        # apply, and nothing else.
        guard = statements(block_with(ROLLBACK_PREV))
        self.assertIn('record_is "${PREV:?}" ${MERGE:+"${MERGE}"}', guard)
        rollback = flat(section("### Marcha atrás de la ventana de resiembra"))
        positions(
            self,
            rollback,
            [
                'record_is "${PREV:?}" ${MERGE:+"${MERGE}"}',
                "--check",
                "--confirm-production",
            ],
        )
        self.assertIn("tras un apply correcto nombra el commit de fusión", rollback)

    def test_every_step_that_takes_the_lock_starts_by_refusing_a_marker(self) -> None:
        for number, anchor in {
            2: STEP_2,
            4: STEP_4,
            5: STEP_5,
            6: STEP_6,
            8: STEP_8,
        }.items():
            with self.subTest(step=number):
                lines = statements(block_with(anchor))
                guard = lines.index(MARKERS_GUARD)
                first_locked = next(
                    i
                    for i, line in enumerate(lines)
                    if is_locked(line)
                    or line.startswith(
                        ("forget_one", "seed_one", "for old", "for pin", "if [")
                    )
                )
                self.assertLess(guard, first_locked)
        # And the reads that open the window and the rollback.
        for anchor in (
            block_with("office_idle\nlab_idle", without="record_is"),
            block_with(ROLLBACK),
            block_with(STEP_11_READS),
        ):
            self.assertIn(MARKERS_GUARD, statements(anchor))

    # -- Oficina ---------------------------------------------------------------
    def test_the_oficina_is_paused_read_back_and_read_again_before_the_apply(
        self,
    ) -> None:
        rules = flat(section("### Reglas de la ventana de resiembra"))
        for fragment in (
            "en pausa durante toda la ventana, no solo vacía al empezar",
            "`POST /api/queue/pause`",
            "«Pausar cola» en la cabecera",
            "«Pausar la cola» en Ajustes",
            "`queue_paused`",
            "ningún trabajo en `preparando`, `en_curso` ni `limpiando`",
            "justo antes del paso 11 y del apply de la marcha atrás",
            "Un trabajo en marcha en ese momento es un `STOP`",
            "La persona que usa la Oficina no encola nada",
            "se reanuda al final del paso 13, o cuando la marcha atrás está verificada",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, rules.replace("La cola se\n", "La cola se "))
        idle = flat(function_body([block_with(READS)], "office_idle"))
        for fragment in (
            '"${PV_PATH}/office.json"',
            '"${PV_PATH}/jobs.json"',
            ".settings.queue_paused == true",
            "select(.enabled)",
            '"preparando", "en_curso", "limpiando"',
            "kind-control-plane cat",
        ):
            self.assertIn(fragment, idle)
        # The reads: at step 0 (before the copy of the office volume is taken),
        # right before the apply, and before the PREV apply.
        zero = flat(step_text(0))
        self.assertLess(
            zero.index("office_idle lab_idle host_memory"),
            zero.index(
                "must sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run"
            ),
        )
        self.assertIn("tomada después de pausar la cola", zero)
        eleven = step_text(11)
        self.assertLess(
            eleven.index("office_idle"),
            eleven.index("--confirm-production"),
        )
        guard = statements(block_with(ROLLBACK_PREV))
        self.assertLess(guard.index("office_idle"), len(guard) - 1)
        # The queue goes on only at the end, and is read back.
        thirteen = statements(one_block(step_text(13), "queue_paused == false"))
        self.assertIn("office_path", thirteen)
        self.assertIn("la cola de la Oficina sigue en pausa", " ".join(thirteen))
        rollback = flat(section("### Marcha atrás de la ventana de resiembra"))
        self.assertIn("La cola de la Oficina se reanuda solo entonces", rollback)

    def test_the_oficina_reads_are_what_the_oficina_writes(self) -> None:
        # The key and the three running statuses are the Oficina's own.
        model = read("images/ax-web/internal/office/model.go")
        self.assertIn('QueuePaused bool `json:"queue_paused"`', model)
        statuses = dict(re.findall(r'Status(\w+)\s*=\s*"(\w+)"', model))
        for name in ("Preparing", "Running", "Cleaning"):
            self.assertIn(
                statuses[name], function_body([block_with(READS)], "office_idle")
            )
        self.assertIn(
            'mux.HandleFunc("POST /api/queue/pause", s.pauseQueue)',
            read("images/ax-web/internal/web/api.go"),
        )
        dispatch = read("images/ax-web/internal/office/dispatch.go")
        self.assertIn("o.st.Settings.QueuePaused", dispatch)
        self.assertIn("office.json", read("images/ax-web/internal/office/store.go"))
        self.assertIn(
            "Pausar cola", read("images/ax-web/internal/web/static/js/25-shell.js")
        )
        self.assertIn(
            "Pausar la cola",
            read("images/ax-web/internal/web/static/js/50-settings.js"),
        )

    # -- Step 10, the worker, ate-setup, the baseline --------------------------
    def test_step_ten_lists_what_the_role_prints_and_no_more(self) -> None:
        text = flat(step_text(10))
        for fragment in (
            "`the existing node has its reviewed preboot sysctl mask`",
            (
                "`then, only where they differ: proxy_arp, proxy_ndp, the registry"
                " mirror and the local-registry-hosting ConfigMap inside the node`"
            ),
            "las tareas `debug` que el rol ejecuta solo en `--check`",
            "`restore into the registry: <imagen>` por cada pin de `MOVED_SUB`",
            "`install Substrate with ate-setup: state, workloads`",
            "`restore into the registry: <imagen>` por cada pin de `MOVED_KO`",
            (
                "`AX is read once the lab node runs and Substrate matches its pinned"
                " install`"
            ),
            "`the web panel is read once AX matches its pinned install`",
            "`ax-web-edge runs as reviewed`",
            "`changed=3`",
            "suponen que de `PREV` a la fusión no cambia nada de lo que el rol instala",
            "se leen contra esa lista, no se toman por un fallo",
            "«Install the AX CLI from its backup only when it differs»",
            "`deployment_metadata`",
            (
                "Ni una línea `back up from the registry` ni `in neither backup nor"
                " registry`"
            ),
            (
                "el diff de AX, el WorkerPool, el `--route-timeout` del router y los"
                " workers obsoletos se evalúan solo dentro del apply"
            ),
            "abortar a partir de aquí exige además el PR que lo revierta",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)
        # The claim the audit disproved is gone, here and in the abort criteria.
        whole = flat(window())
        self.assertNotIn("anuncia el despliegue de `ax-controller`", whole)
        self.assertNotIn("el rol anuncia el despliegue", whole)
        # Every line is one the role prints.
        role = {
            name: read(f"ansible/roles/ax_lab/tasks/{name}.yml")
            for name in ("main", "inspect", "web_read", "ax_host", "ax_read")
        }
        joined = lambda name: " ".join(role[name].replace('"', " ").split())  # noqa: E731
        self.assertIn(
            "then, only where they differ: proxy_arp, proxy_ndp, the registry",
            joined("inspect"),
        )
        self.assertIn(
            "mirror and the local-registry-hosting ConfigMap inside the node",
            joined("inspect"),
        )
        self.assertIn(
            "the existing node has its reviewed preboot sysctl mask",
            read("ansible/roles/ax_lab/tasks/node_core_pattern_read.yml"),
        )
        self.assertIn("restore into the registry: ", role["main"])
        self.assertIn("install Substrate with ate-setup: ", role["main"])
        self.assertIn(
            "AX is read once the lab node runs and Substrate matches its",
            joined("main"),
        )
        self.assertIn("pinned install", joined("main"))
        self.assertIn(
            "the web panel is read once AX matches its pinned install", role["web_read"]
        )
        self.assertIn('" runs as reviewed"', role["web_read"])
        self.assertIn(
            "Install the AX CLI from its backup only when it differs", role["ax_host"]
        )
        # The `debug` tasks of the roles the playbook runs: the four plans of
        # main.yml and the node's, which --check prints, and the report of the
        # office that web.yml prints in the apply. Any other would be a line
        # step 10 does not list.
        debugs = []
        for role_name in (
            "capacity_preflight",
            "operation_lock_guard",
            "deployment_metadata",
            "ax_lab",
        ):
            for path in sorted((ROOT / "ansible/roles" / role_name).rglob("*.yml")):
                text_of = path.read_text(encoding="utf-8")
                debugs += [
                    (path.name, match)
                    for match in re.findall(
                        r"ansible\.builtin\.debug:\n(?:\s+msg:.*\n)", text_of
                    )
                ]
        self.assertEqual(
            sorted(name for name, _ in debugs),
            ["main.yml"] * 4 + ["node_core_pattern_read.yml", "web.yml"],
        )
        # AX is read only once Substrate matches, in --check: that is why the
        # AX diff, the router and the stale workers are not in it.
        self.assertIn(
            "not ansible_check_mode or ax_lab_substrate_drift == []", role["ax_read"]
        )
        # The apply is what changes the worker: the WorkerPool carries its image.
        self.assertIn(
            'ax_lab.substrate.images["ateom-gvisor"]',
            read("ansible/roles/ax_lab/templates/ax/ax-workers.yaml.j2"),
        )
        # The criteria say the same, without the old claim.
        criteria = flat(section("### Criterios de aborto"))
        for fragment in (
            "una línea `the apply stops` o `back up from the registry`",
            "que las de `MOVED_SUB` y `MOVED_KO`",
            "abortar aquí exige además el PR que lo revierta",
            "y que el `git diff --stat` del paso 9 no explica",
        ):
            self.assertIn(fragment, criteria)
        # The assumption behind `changed=3` is read at step 9, and only read.
        nine = statements(block_with(STEP_9))
        self.assertEqual(nine[-1], 'git diff --stat "${PREV}" "${MERGE}" -- ansible')

    def test_the_worker_is_gated_and_smoke_tested_before_the_freeze_ends(self) -> None:
        defs = block_with(READS)
        gate = flat(function_body([defs], "worker_gate"))
        for fragment in (
            '"${KUBECTL_WAIT[@]}" --namespace ax-workers rollout status deployment/ax --timeout=300s',
            "--selector ate.dev/worker-pool=ax",
            "select(.metadata.deletionTimestamp == null)",
            "length == 1",
            'endswith("@" + $digest)',
            '.type == "Ready"',
        ):
            self.assertIn(fragment, gate)
        # A watch through KUBECTL is cut at 10 s by its own --request-timeout.
        wait = re.search(r"KUBECTL_WAIT=\((.*?)\)\n", defs, flags=re.DOTALL).group(1)
        self.assertNotIn("--request-timeout", wait)
        self.assertIn(
            "--request-timeout",
            re.search(r"KUBECTL=\((.*?)\)\n", defs, flags=re.DOTALL).group(1),
        )
        smoke = block_with(STEP_12_GATE)
        lines = statements(smoke)
        self.assertEqual(lines[0], 'worker_gate "${WORKER_NEW}"')
        command = lines[1]
        for fragment in (
            "must sudo -- /usr/bin/env TURNOS=4 RAMA=master /usr/local/sbin/ax-tarea",
            "https://github.com/octocat/Hello-World",
            "No modifiques nada",
            "claude",
        ):
            self.assertIn(fragment, command)
        self.assertEqual(lines[-1], "lab_idle")
        twelve = flat(step_text(12))
        positions(
            self,
            twelve,
            [
                'expect_pinned 4 "${OUT}"',
                'worker_gate "${WORKER_NEW}"',
                "/usr/local/sbin/ax-tarea",
                "lab_idle",
                f"{DEPLOY} --check",
                "oom_lines",
            ],
        )
        for fragment in (
            "el rollout del despliegue `ax` de `ax-workers` (hasta 300 s)",
            "exactamente un pod de worker que no se esté terminando",
            "con el digest nuevo de `ateom-gvisor` y listo",
            "una prueba de humo de solo lectura, como la del cambio 4",
            "como mucho 4 turnos, `TURNOS=4`",
            "`/etc/dockerswarm/ax/claude-oauth-token`",
            "El worker viejo puede seguir en `Terminating` mucho tiempo",
            "no es un error",
            "`readyReplicas`, que durante el relevo cuenta el pod viejo y pasa al instante",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, twelve)
        criteria = flat(section("### Criterios de aborto"))
        for fragment in (
            "no termina en 300 s",
            "o la prueba de humo falla",
            "Un worker viejo en `Terminating` no es un fallo",
        ):
            self.assertIn(fragment, criteria)
        # What the commands name is what the lab has.
        contract = read("config/ax-lab.yml")
        self.assertIn("namespace: ax-workers\n      name: ax\n", contract)
        self.assertIn(
            "ate.dev/worker-pool", read("ansible/roles/ax_lab/tasks/workers.yml")
        )
        tarea = read("ansible/roles/ax_lab/templates/ax/ax-tarea.sh.j2")
        for fragment in (
            "TURNOS=${TURNOS:-20}",
            "RAMA=${RAMA:-main}",
            "claude-oauth-token",
        ):
            self.assertIn(fragment, tarea)
        self.assertIn("credential_directory: /etc/dockerswarm/ax", contract)

    def test_ate_setup_memory_is_measured_or_accepted_and_its_oom_is_read(self) -> None:
        zero = flat(step_text(0))
        for fragment in (
            "medido en un clúster kind desechable con los pines nuevos",
            "como mucho 134 MiB (140 509 184 bytes)",
            "un margen de al menos 58 MiB, el 30 % del tope",
            "`memory_peak_bytes`",
            "la aceptación explícita del propietario",
            "la aceptación cubre los dos",
            "«Pico de `ate-setup`»",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, zero)
        # The threshold is the cap less 30 %, in MiB and in bytes.
        cap = 192
        self.assertIn(f"memory_limit_mib: {cap}\n", read("config/ax-lab.yml"))
        limit = 134
        self.assertGreaterEqual((cap - limit) / cap, 0.30)
        self.assertLess((cap - (limit + 1)) / cap, 0.30)
        self.assertEqual(limit * 1024 * 1024, 140_509_184)
        # The stale text is gone: it runs under 192 MiB, and never ran there.
        install = flat(section("### Instalación de Substrate", read("docs/AX.md")))
        self.assertIn("192 MiB sin swap (96 MiB reservados)", install)
        self.assertNotIn("256 MiB sin swap", install)
        self.assertIn("Su consumo real no se ha medido nunca", install)
        criteria = flat(section("### Criterios de aborto"))
        for fragment in (
            "tarea fallida «Install Substrate with the pinned ate-setup only on drift»",
            "campo `stdout`",
            "`exit_code`, `oom_killed`, `memory_peak_bytes`, `oom_kill_events`,",
            "`aborted` (`timeout` o `mem_available_floor`",
            "201 326 592 bytes del tope de 192 MiB",
            "el rol no imprime ese resultado ni lo guarda",
            "Un fallo así no deja contenedor",
            "`installing` en `state/substrate.json`",
            "No se reintenta a ciegas",
            "vuelve a ejecutar `ate-setup`, el anterior, bajo el mismo tope de 192 MiB",
            "no hay un tercer intento en la ventana",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, criteria)
        self.assertEqual(cap * 1024 * 1024, 201_326_592)
        # The keys the text names are the keys the manager prints.
        printed = manager.Outcome().metrics()
        for key in (
            "exit_code",
            "oom_killed",
            "memory_peak_bytes",
            "oom_kill_events",
            "seconds",
            "aborted",
        ):
            self.assertIn(key, printed)
        self.assertIn("`ERROR: ate-setup failed:", criteria)

    def test_the_baseline_is_taken_before_the_apply_and_compared_after(self) -> None:
        defs = block_with(READS)
        memory = flat(function_body([defs], "lab_memory"))
        for fragment in (
            "kind-control-plane kind-registry",
            "memory.events memory.events.local",
            "'^anon '",
            "memory.stat",
            "memory.current",
        ):
            self.assertIn(fragment, memory)
        # memory.peak is saturated by page cache: no block reads it.
        for block in bash_blocks(window()):
            self.assertNotIn("memory.peak", block)
        replaced = re.findall(r"REPLACED\+?='([^']*)'", defs)
        regex = "".join(replaced)
        for name in (
            "ate-api-server",
            "ate-controller",
            "atelet",
            "atenet-router",
            "atenet-egress",
            "podcertificate-controller",
            "ax-controller",
            "ax-server",
            "ax-workers/",
        ):
            self.assertIn(name, regex)
        for kept in ("postgres", "rustfs", "ax-redis", "ax-web", "kube-system"):
            self.assertNotRegex(regex, rf"{kept}\b")
        baseline = statements(block_with(STEP_11_BASELINE))
        self.assertIn(
            "lab_memory >\"${WORK}/memory.txt\" || stop 'la línea base de memoria'",
            baseline,
        )
        eleven = flat(step_text(11))
        positions(
            self,
            eleven,
            [
                "office_idle",
                'lab_memory >"${WORK}/memory.txt"',
                f"{DEPLOY} --confirm-production",
            ],
        )
        for fragment in (
            "los contadores de `memory.events` y de `memory.events.local` del nodo y del registro",
            "`anon` de `memory.stat` y `memory.current`",
            "la tabla de reinicios de los pods que el apply no sustituye y la lista de los que sí",
            "`memory.events` jerárquico ya lea `oom_kill` distinto de cero",
            "el criterio es que no se mueva del valor que se anota ahora",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, eleven)
        criteria = flat(section("### Criterios de aborto"))
        for fragment in (
            "se mueva del valor de la línea base en `memory.events` o en `memory.events.local`",
            "el criterio es que no se mueva, no que sea cero",
            "el `memory.peak` no informa, está saturado por la caché",
        ):
            self.assertIn(fragment, criteria)
        zero = flat(step_text(0))
        self.assertIn("está saturado por la caché de página", zero)
        compare = flat(block_with(STEP_12_COMPARE, without="worker_gate"))
        for fragment in (
            'baseline/memory.txt" | oom_lines',
            "<(lab_memory | oom_lines)",
            'baseline/restarts.txt")',
            "<(pod_table unreplaced)",
        ):
            self.assertIn(fragment, compare)

    def test_what_you_will_see_and_how_to_watch_it(self) -> None:
        seen = section("### Lo que se ve durante el apply")
        text = flat(seen)
        for fragment in (
            "tres pods a la vez",
            "`atelet`, un DaemonSet que sustituye de uno en uno, desaparece entre 10 y 60 s, y hasta 330 s",
            "`ax-controller` no tiene sonda",
            "«starting AX task worker»",
            "`atenet-router` se sustituye dos veces",
            "`--route-timeout=1h`",
            "el viejo se queda `Terminating` mucho tiempo",
            "`readyReplicas` del WorkerPool puede valer 2 un momento",
            "puede reiniciarse una o dos veces mientras espera sus certificados de pod",
            "En total, de 8 a 15 minutos (sin medir)",
            "Lo que decide el aborto es que el rol falle, no el reloj",
            "por debajo de unos 10 minutos por Deployment no se interrumpe nada",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)
        watch = bash_blocks(seen)
        self.assertEqual(len(watch), 1)
        command = flat(watch[0])
        self.assertIn("get pods --all-namespaces --watch", command)
        self.assertTrue(command.startswith("sudo -- /usr/bin/env HOME="))
        # Only reads: nothing that changes the cluster or the host.
        for verb in (
            " apply ",
            " delete ",
            " patch ",
            " rm ",
            " exec ",
            " edit ",
            " scale ",
        ):
            self.assertNotIn(verb, " " + command + " ")
        self.assertIn("get events --all-namespaces --watch", text)

    # -- Rollback --------------------------------------------------------------
    def test_the_rollback_after_the_apply_mirrors_steps_ten_to_twelve(self) -> None:
        rollback = section("### Marcha atrás de la ventana de resiembra")
        text = flat(rollback)
        positions(
            self,
            text,
            [
                'git checkout --detach "${PREV:?}"',
                'record_is "${PREV:?}" ${MERGE:+"${MERGE}"}',
                "office_idle lab_scopes host_memory",
                f"{DEPLOY} --check",
                f"{DEPLOY} --confirm-production",
                '--registry localhost:5001 --layout "${LAYOUT}" --tag "${TAG_SUB}" "${OLD_SUB[@]}"',
                'expect_pinned 7 "${OUT}"',
                '"${OLD_KO[@]}" "${BUILDX[@]}"',
                'expect_pinned 4 "${OUT}"',
                'for cli in /opt/dockerswarm/ax-lab/bin/ax "${CLI_BACKUP}"; do',
                '== "${CLI_OLD}" ]]',
                'worker_gate "${WORKER_OLD}"',
                f"{DEPLOY} --check",
                'baseline/memory.txt" | oom_lines',
                'baseline/restarts.txt")',
            ],
        )
        for fragment in (
            "`restore into the registry: <imagen>` por cada pin de `STALE_SUB` y de `STALE_KO`",
            "`install Substrate with ate-setup: state, workloads`",
            "`ax-web-edge runs as reviewed` y `changed=3`",
            "los dos ficheros de `deployment_metadata`",
            "Tras uno que falló, que es el motivo habitual de volver atrás, es un subconjunto y algo más",
            "`deployment_metadata` no llegó a correr",
            "(`changed=1`, solo la CLI",
            "Lo que nunca es aceptable, en ningún caso, es `in neither backup nor registry`, `back up from the registry`",
            "vuelve a ejecutar `ate-setup`, el anterior",
            "sustituye otra vez el worker",
            "nunca se avanza hacia el head del PR ni hacia el commit de fusión",
            "tiene que informar `changed=0`",
            "no se avanza: se recupera el marker, se pasa la guarda de contenedores transitorios",
            "si vuelve a fallar, es un `STOP`",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)
        # A check after a failed apply is a subset: "any other line stops" is
        # what the text no longer says.
        self.assertNotIn("Cualquier otra línea es una causa de parar", text)
        # The verification is the one of step 12, with the old pins.
        code = "\n".join(statements(one_block(rollback, 'worker_gate "${WORKER_OLD}"')))
        self.assertNotIn("NEW_", code)
        self.assertNotIn("CLI_NEW", code)
        # The before-apply variant ends on a --check from PREV with nothing to do.
        before = statements(block_with('record_is "${PREV:?}"\nmust ./scripts'))
        self.assertEqual(before[-1], f"must {DEPLOY} --check")
        self.assertIn("`changed=0`", text)

    def test_the_transient_container_guard_is_where_a_retained_marker_blocks(
        self,
    ) -> None:
        rollback_block = statements(block_with(ROLLBACK))
        self.assertLess(
            rollback_block.index(MARKERS_GUARD), rollback_block.index("no_transient")
        )
        self.assertLess(
            rollback_block.index("no_transient"),
            next(
                i for i, line in enumerate(rollback_block) if line.startswith("for old")
            ),
        )
        rollback = flat(section("### Marcha atrás de la ventana de resiembra"))
        for fragment in (
            "ningún contenedor transitorio del gestor (`ax-lab-ate-setup`, `ax-lab-build-*`)",
            "`no_transient` lo comprueba entre la recuperación del marker y el bucle",
            "dejaría un segundo marker",
        ):
            self.assertIn(fragment, rollback)
        marker = flat(section("### Un marker retenido"))
        for fragment in (
            "`ax-lab-ate-setup` y `ax-lab-build-*` parados, con sus logs",
            "el rol se niega a correr mientras exista uno",
            "6 480 s la instalación y 3 600 s una compilación",
            "se espera a que termine",
            "se leen sus logs y se borra con el lock host-global y la operación `ax-lab-transient`",
            "no hay lista",
            "`HALT=` levanta su `STOP`",
            "que está parado",
            "al cabo de un minuto",
            "la recuperación con éxito no prueba que el apply esté muerto",
            "ni la monitorización ni una alerta",
            "después de cada `STOP` se lista a mano `ls /run/lock/dockerswarm-*.marker`",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, marker)
        guard = flat(function_body([block_with(READS)], "no_transient"))
        for fragment in (
            "/usr/bin/docker ps --all",
            "--filter 'name=^ax-lab-ate-setup$'",
            "--filter 'name=^ax-lab-build-'",
            "contenedor transitorio del gestor",
        ):
            self.assertIn(fragment, guard)
        # The names are the manager's and the role's, and the operation is a
        # name the lock accepts.
        self.assertEqual(manager.INSTALL_CONTAINER, "ax-lab-ate-setup")
        self.assertEqual(manager.BUILD_CONTAINER_PREFIX, "ax-lab-build-")
        inspect = read("ansible/roles/ax_lab/tasks/inspect.yml")
        self.assertIn('"ax-lab-build-"', inspect)
        self.assertIn('["ax-lab-ate-setup"]', inspect)
        self.assertRegex("ax-lab-transient", r"^[a-z][-a-z0-9_.:]{1,127}$")
        self.assertIn(
            'SAFE_OPERATION = re.compile(r"^[a-z][-a-z0-9_.:]{1,127}$")',
            read("scripts/host_global_operation_lock.py"),
        )

    def test_the_abort_criteria_state_the_real_waits_and_not_to_interrupt(self) -> None:
        criteria = flat(section("### Criterios de aborto"))
        for fragment in (
            "hasta 600 s por rollout (`substrate.install.rollout_timeout_seconds`)",
            "6 480 s en total, más los 300 s que le añade Ansible",
            "treinta intentos de `kubectl rollout status --timeout=10s` más diez segundos de pausa",
            "unos 10 minutos por Deployment",
            "el router, lo mismo (10 minutos)",
            "el WorkerPool, treinta lecturas con diez segundos de pausa (5 minutos",
            "la salud de Substrate, sesenta lecturas con diez segundos (10 minutos)",
            "Lo que aborta es que el rol falle, no el reloj",
            "Tampoco se interrumpe el apply con `Ctrl-C` ni cerrando la terminal",
            "tiempo de espera de rollout del rol",
            "Es marcha atrás y no un reintento",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, criteria)
        # The numbers are the role's and the contract's.
        install = yaml_load(read("config/ax-lab.yml"))["ax_lab"]["substrate"]["install"]
        self.assertEqual(install["rollout_timeout_seconds"], 600)
        self.assertEqual(install["timeout_seconds"], 6480)
        self.assertEqual(10 * 600 + 180 + 300, install["timeout_seconds"])
        rollout = task(
            "ax.yml", "Wait for each AX control plane Deployment to roll out"
        )
        self.assertEqual((rollout["retries"], rollout["delay"]), (30, 10))
        self.assertIn("--timeout=10s", rollout["ansible.builtin.command"]["argv"])
        pool = task("ax.yml", "Wait for the WorkerPool to be ready")
        self.assertEqual((pool["retries"], pool["delay"]), (30, 10))
        router = task(
            "ax.yml", "Wait for the atenet router to roll out with its route timeout"
        )
        self.assertEqual((router["retries"], router["delay"]), (30, 10))
        health = task("substrate.yml", "Wait for every Substrate workload to be ready")
        self.assertEqual((health["retries"], health["delay"]), (60, 10))
        self.assertEqual(
            task(
                "substrate.yml",
                "Install Substrate with the pinned ate-setup only on drift",
            )["async"],
            "{{ ax_lab.substrate.install.timeout_seconds + 300 }}",
        )

    def test_the_corrupt_blob_fallback_and_the_end_of_the_rollback_freeze(self) -> None:
        rollback = section("### Marcha atrás de la ventana de resiembra")
        text = flat(rollback)
        fallback = statements(one_block(rollback, "ASIDE=${LAYOUT}.aparte-"))
        self.assertEqual(len([line for line in fallback if is_locked(line)]), 2)
        positions(
            self,
            " ".join(fallback),
            [
                '/usr/bin/mv "${LAYOUT}" "${ASIDE}"',
                '/usr/bin/cp -a "${SNAP_LAYOUT}" "${LAYOUT}"',
                "--image-set substrate",
                "--image-set ax",
                "--image-set web",
            ],
        )
        for fragment in (
            "Un blob corrupto en la copia viva",
            "`seed-layout` verifica el blob que ya existe y no lo repara",
            "se aparta la copia viva",
            "se restaura la instantánea completa",
            "se comprueban los tres conjuntos con los pines anteriores",
            "Una entrada nueva corrupta en la copia viva",
            "`forget` no lee los blobs",
            "solo donde el nombre apunta a ese digest exacto",
            "sin la lectura previa de `forget_one`",
            # The freeze ends with an apply from the revert and a clean check,
            # and the check from main before it is expected to differ.
            "La congelación termina con un apply desde `R`",
            "un `--check` posterior que informe `changed=0`",
            "informa `changed=2`, solo los dos ficheros de `deployment_metadata`",
            "es lo esperado y se acepta, y cualquier otro `changed` no",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)
        # forget outside forget_one is prose, with the same shape as the rest.
        self.assertIn(
            "sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py forget --image-set <conjunto>",
            text,
        )

    def test_every_function_the_window_pastes_is_defined_once_and_used(self) -> None:
        blocks = bash_blocks(window())
        body = "\n".join(blocks)
        for name in (
            "cfg",
            "plan",
            "pins",
            "sorted",
            "load_plans",
            "load_facts",
            "save_window",
            "record_is",
            "office_path",
            "office_idle",
            "lab_scopes",
            "lab_idle",
            "host_memory",
            "lock_free",
            "no_transient",
            "lab_memory",
            "oom_lines",
            "pod_table",
            "worker_gate",
        ):
            with self.subTest(function=name):
                self.assertEqual(len(re.findall(rf"(?m)^{name}\(\) \{{", body)), 1)
                uses = re.findall(rf"(?m)^\s*(?:.*[ (|&])?{name}\b(?!\(\))", body)
                self.assertGreater(len(uses), 1, name)
        # The definitions are in step 0, before any step runs them.
        steps_at = window().index("### Pasos de la ventana de resiembra")
        for anchor in (PLAN_FUNCTIONS, DEFINITIONS, READS):
            self.assertLess(window().index(anchor), steps_at)
        self.assertIn(
            "en tres bloques que solo definen y no ejecutan nada",
            flat(step_text(0)),
        )


def yaml_load(text: str) -> Any:
    import yaml

    return yaml.safe_load(text)


def task(file: str, name: str) -> dict[str, Any]:
    tasks = yaml_load(read(f"ansible/roles/ax_lab/tasks/{file}"))
    found = [item for item in tasks if item.get("name") == name]
    if len(found) != 1:
        raise AssertionError(f"{name!r} must be one task of {file}")
    return found[0]


EXECUTION_TOOLS = ("bash", "jq", "sha256sum", "find", "grep")


@unittest.skipUnless(sys.platform.startswith("linux"), "the lock needs /proc")
@unittest.skipUnless(
    all(shutil.which(tool) for tool in EXECUTION_TOOLS)
    and all(
        Path(path).exists()
        for path in ("/usr/bin/python3", "/usr/bin/install", "/usr/bin/cp")
    ),
    "the window's tools are not installed",
)
class WindowExecutionTests(unittest.TestCase):
    """The blocks of the window, run against the real manager and lock."""

    def snapshot(self, host: Sandbox) -> None:
        result = host.run(block_with(STEP_2))
        host.assert_clean(self, result)
        self.assertTrue((host.relock / "images/index.json").is_file())
        self.assertEqual((host.relock / "ax").read_bytes(), host.CLI_OLD)
        host.extract()

    def apply(self, host: Sandbox, *, rehearse: bool = True) -> None:
        """Steps 4 to 8, as the window pastes them."""
        steps = [STEP_4, STEP_5, STEP_6, STEP_7, STEP_8]
        result = host.run(*(block_with(step) for step in steps[0 if rehearse else 1 :]))
        host.assert_clean(self, result)
        host.assert_live(self, "new")
        self.assertEqual(host.cli.read_bytes(), host.CLI_NEW)

    def test_the_apply_path_asks_for_no_refusal_and_repeats_with_no_lock_at_all(
        self,
    ) -> None:
        host = Sandbox(self)
        self.snapshot(host)
        self.apply(host)
        asked = len(host.calls())
        self.assertGreater(asked, 0)
        # Everything is done: the guards read, and nothing asks for the lock.
        # (The rehearsal is scratch and is seeded again each time it runs.)
        self.apply(host, rehearse=False)
        self.assertEqual(len(host.calls()), asked)
        # The images that do not move were never touched.
        for name in LAB_TAGS:
            if name not in host.moved():
                with self.subTest(image=name):
                    self.assertEqual(host.named(name), host.old[name]["digest"])

    def test_a_window_stopped_half_way_resumes_without_a_refusal(self) -> None:
        names = Sandbox.SUBSTRATE_MOVED + Sandbox.KO_MOVED
        # Stopped after the first forget, after all of them, and half way
        # through the seeds: the states the repetition of steps 5 and 6 meets.
        states = {
            "one forgotten": ([names[0]], []),
            "all forgotten": (names, []),
            "half seeded": (names, [names[0], names[2]]),
            "all seeded": (names, names),
        }
        for label, (forgotten, seeded) in states.items():
            with self.subTest(state=label):
                host = Sandbox(self)
                self.snapshot(host)
                for name in forgotten:
                    host.forget(name)
                for name in seeded:
                    host.seed(name)
                self.apply(host)

    def test_the_rollback_restores_every_state_and_repeats_without_a_refusal(
        self,
    ) -> None:
        names = Sandbox.SUBSTRATE_MOVED + Sandbox.KO_MOVED
        states = {
            "nothing applied": ([], []),
            "one forgotten": ([names[0]], []),
            "all forgotten": (names, []),
            "half seeded": (names, [names[1], names[2]]),
            "all seeded": (names, names),
        }
        for label, (forgotten, seeded) in states.items():
            with self.subTest(state=label):
                host = Sandbox(self)
                self.snapshot(host)
                for name in forgotten:
                    host.forget(name)
                for name in seeded:
                    host.seed(name)
                if seeded == names:
                    # What the CLI step leaves on top.
                    host.cli.write_bytes(host.CLI_NEW)
                result = host.run(block_with(ROLLBACK))
                host.assert_clean(self, result)
                host.assert_live(self, "old")
                self.assertEqual(host.cli.read_bytes(), host.CLI_OLD)
                done = len(host.calls())
                # The rollback can be repeated, and then there is nothing left
                # to ask the lock for.
                again = host.run(block_with(ROLLBACK))
                host.assert_clean(self, again)
                host.assert_live(self, "old")
                self.assertEqual(len(host.calls()), done)

    def test_the_rollback_after_the_whole_apply_path_ends_where_it_started(
        self,
    ) -> None:
        host = Sandbox(self)
        self.snapshot(host)
        self.apply(host)
        result = host.run(block_with(ROLLBACK))
        host.assert_clean(self, result)
        host.assert_live(self, "old")
        self.assertEqual(host.cli.read_bytes(), host.CLI_OLD)

    def test_the_rollback_loop_before_the_guards_locked_itself_out(self) -> None:
        # The control: the loop the window had, on the state a window stopped
        # before the forget leaves. The manager refuses the forget (the entry
        # names the old digest), the wrapper keeps the marker, `|| echo` hides
        # it, and the seed that follows is refused. This is the failure the
        # guards exist for; if this stopped happening, the tests above would
        # no longer be telling anything.
        host = Sandbox(self)
        self.snapshot(host)
        result = host.run(OLD_ROLLBACK_LOOP)
        self.assertTrue(host.marker, result.output)
        codes = [call["code"] for call in host.calls()]
        self.assertIn(1, codes)
        self.assertIn(126, codes)
        self.assertIn("an active or stale operation marker exists", result.output)

    def test_a_failed_seed_stops_the_window_and_the_documented_recovery_frees_it(
        self,
    ) -> None:
        host = Sandbox(self)
        self.snapshot(host)
        # A blob of the second Substrate image is damaged in the artifact.
        victim = host.SUBSTRATE_MOVED[1]
        blob = next(iter(host.new[victim]["blobs"]))
        path = host.inputs / "substrate/blobs/sha256" / blob.split(":")[1]
        good = path.read_bytes()
        path.write_bytes(b"x" * len(good))
        # The seed, then the next steps pasted anyway.
        result = host.run(block_with(STEP_5), block_with(STEP_6), block_with(STEP_8))
        self.assertTrue(result.halt, result.output)
        self.assertIn("STOP:", result.output)
        self.assertTrue(host.marker, result.output)
        # The reason is shown, not swallowed with the output.
        self.assertIn("ERROR:", result.output)
        # One failed locked call, and no refusal: nothing asked for the lock
        # after it, and the CLI was not installed by the step pasted after.
        codes = [call["code"] for call in host.calls()]
        self.assertEqual(sum(code == 1 for code in codes), 1, codes)
        self.assertNotIn(126, codes)
        self.assertEqual(host.cli.read_bytes(), host.CLI_OLD)
        self.assertIn("STOP previo", result.output)
        # The documented recovery: the dry run prints the confirmation, and
        # the apply carries it byte for byte.
        recover = [
            statement
            for statement in statements(block_with(RECOVERY))
            if "recover" in statement
        ]
        dry, apply = recover
        dry_run = host.run(dry)
        confirmation = re.search(
            r"^RECOVER_DIRECT_OPERATION:\S+$", dry_run.output, re.MULTILINE
        )
        self.assertIsNotNone(confirmation, dry_run.output)
        self.assertTrue(host.marker)
        applied = host.run(
            apply.replace("CONFIRMACION_EXACTA_MOSTRADA", confirmation.group(0))
        )
        self.assertFalse(host.marker, applied.output)
        self.assertEqual(len(list((host.root / "run/archive").iterdir())), 1)
        # The artifact is fixed, the stop is lifted and the step repeats: the
        # guards ask only for what is missing.
        path.write_bytes(good)
        again = host.run("HALT=", block_with(STEP_6), block_with(STEP_7))
        host.assert_live(self, "new")
        self.assertEqual(again.halt, "", again.output)
        self.assertFalse(host.marker)

    def test_the_cli_chain_never_runs_a_binary_that_failed_a_check(self) -> None:
        chain = next(
            statement
            for statement in statements(block_with(CLI_CHAIN))
            if CLI_CHAIN in statement
        )
        # Per case: what is wrong, and the mode the binary has afterwards (the
        # chain only changes it once the first two checks passed).
        cases = {
            "healthy": (None, 0o700),
            "another sha256": ("CLI_NEW=0000\n", 0o644),
            "writable by others": ('chmod 0666 "${IN}/ax/ax"\n', 0o666),
            "not found by ldd": ('PATH="${T}/shims:${PATH}"\n', 0o700),
        }
        for label, (fault, mode) in cases.items():
            with self.subTest(case=label):
                host = Sandbox(self)
                make_directories(host.inputs / "ax")
                binary = host.inputs / "ax/ax"
                binary.write_bytes(host.CLI_NEW)
                binary.chmod(0o644)
                make_directories(host.root / "shims")
                (host.root / "shims/ldd").write_text(
                    "#!/bin/sh\necho '\\tlibfoo.so => not found'\n"
                )
                (host.root / "shims/ldd").chmod(0o755)
                result = host.run((fault or "") + chain)
                # The binary touches the witness only when run with `help`.
                ran = (host.root / "witness").exists()
                if fault is None:
                    self.assertEqual(result.halt, "", result.output)
                    self.assertTrue(ran, result.output)
                else:
                    self.assertTrue(result.halt, result.output)
                    self.assertIn("STOP:", result.output)
                    self.assertFalse(ran, result.output)
                self.assertEqual(stat.S_IMODE(binary.stat().st_mode), mode)

    def test_an_earlier_snapshot_and_a_leftover_rehearsal_are_not_trampled(
        self,
    ) -> None:
        host = Sandbox(self)
        self.snapshot(host)
        before = sorted(path.name for path in (host.relock / "images").iterdir())
        asked = len(host.calls())
        # Repeating step 2 stops on the snapshot that is there: it neither
        # nests the copy inside it nor reads the old one as the new.
        again = host.run(block_with(STEP_2))
        self.assertTrue(again.halt, again.output)
        self.assertIn("existe", again.output)
        self.assertEqual(
            sorted(path.name for path in (host.relock / "images").iterdir()), before
        )
        self.assertFalse((host.relock / "images/images").exists())
        self.assertEqual(len(host.calls()), asked)
        # A rehearsal an aborted step 4 left is cleared, not read.
        stale = host.relock / "rehearsal"
        make_directories(stale)
        (stale / "leftover").write_text("an earlier attempt\n")
        result = host.run(block_with(STEP_4))
        host.assert_clean(self, result)
        self.assertFalse((stale / "leftover").exists())


DUMP = """
{
  declare -p OLD_SUB NEW_SUB STALE_SUB MOVED_SUB OLD_KO NEW_KO STALE_KO MOVED_KO
  declare -p BUILDX TAG_SUB TAG_AX CLI_OLD CLI_NEW WEB_TAG WEB_PIN
  declare -p WORKER_OLD WORKER_NEW PR_HEAD PREV
  echo "MERGE=${MERGE:-}"
} >"${T}/%s"
"""


def needs_repo() -> bool:
    try:
        import yaml  # noqa: F401
    except ImportError:
        return False
    return shutil.which("git") is not None


@unittest.skipUnless(sys.platform.startswith("linux"), "the lock needs /proc")
@unittest.skipUnless(
    all(shutil.which(tool) for tool in EXECUTION_TOOLS)
    and all(
        Path(path).exists()
        for path in (
            "/usr/bin/python3",
            "/usr/bin/install",
            "/usr/bin/cp",
            "/usr/bin/mv",
        )
    ),
    "the window's tools are not installed",
)
class WindowReadsExecutionTests(unittest.TestCase):
    """The reads and guards of the window, run against a state the test sets."""

    def start_block(self) -> str:
        return block_with("office_idle\nlab_idle", without="record_is")

    def test_the_idle_lab_passes_every_read(self) -> None:
        host = Sandbox(self)
        result = host.run(self.start_block())
        host.assert_clean(self, result)

    def test_each_thing_that_is_not_idle_stops_the_window(self) -> None:
        cases = {
            "the queue is not paused": (
                lambda h: h.write_office(
                    paused=False, schedules=[], statuses=["hecho"]
                ),
                "no está en pausa",
            ),
            "a schedule is enabled": (
                lambda h: h.write_office(
                    paused=True, schedules=[False, True], statuses=[]
                ),
                "hay un horario activo",
            ),
            "a job is preparing": (
                lambda h: h.write_office(
                    paused=True, schedules=[], statuses=["preparando"]
                ),
                "trabajo en marcha",
            ),
            "a job is running": (
                lambda h: h.write_office(
                    paused=True, schedules=[], statuses=["hecho", "en_curso"]
                ),
                "trabajo en marcha",
            ),
            "a job is being cleaned up": (
                lambda h: h.write_office(
                    paused=True, schedules=[], statuses=["limpiando"]
                ),
                "trabajo en marcha",
            ),
            "AX has a Task": (
                lambda h: (h.root / "world/tasks").write_text(
                    "t1  default  Running  a  b  5m\n"
                ),
                "AX tiene Tasks",
            ),
            "AX has a tunnel": (
                lambda h: (h.root / "world/tunnels").write_text("PID  LOCAL  REMOTE\n"),
                "AX tiene túneles",
            ),
            "an ax-tarea is running": (
                lambda h: (h.root / "world/scopes").write_text(
                    "ax-tarea-1.scope loaded active running ax-tarea\n"
                ),
                "ax-tarea o un ax en marcha",
            ),
            "MemAvailable is under 3 GiB": (
                lambda h: (h.root / "meminfo").write_text("MemAvailable: 3145727 kB\n"),
                "faltan 3 GiB",
            ),
            "a marker is retained": (
                lambda h: (h.root / "run/direct.marker").write_text("{}"),
                "test -z",
            ),
        }
        for label, (prepare, message) in cases.items():
            with self.subTest(case=label):
                host = Sandbox(self)
                prepare(host)
                result = host.run(self.start_block())
                self.assertTrue(result.halt, result.output)
                self.assertIn("STOP:", result.output)
                self.assertIn(message, result.output)

    def test_a_holder_of_the_global_lock_stops_the_reads(self) -> None:
        host = Sandbox(self)
        host.assert_clean(self, host.run("lock_free"))
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl, sys, time\n"
                    "handle = open(sys.argv[1])\n"
                    "fcntl.flock(handle, fcntl.LOCK_EX)\n"
                    "print('held', flush=True)\n"
                    "time.sleep(120)\n"
                ),
                str(host.root / "run/lock"),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        result = host.run(self.start_block())
        self.assertTrue(result.halt, result.output)
        self.assertIn("alguien tiene el lock host-global", result.output)
        holder.kill()
        holder.wait()
        host.assert_clean(self, host.run(self.start_block()))

    def test_three_gib_exactly_is_enough(self) -> None:
        host = Sandbox(self)
        (host.root / "meminfo").write_text("MemAvailable: 3145728 kB\n")
        host.assert_clean(self, host.run(self.start_block()))

    def test_the_queue_is_read_from_the_volume_the_cluster_names(self) -> None:
        # The path comes from the PV, not from a guess: a PV without a path
        # stops, and so does a volume with no office.json.
        host = Sandbox(self)
        (host.root / "world/pv/office.json").unlink()
        result = host.run(self.start_block())
        self.assertTrue(result.halt, result.output)

    def test_the_record_must_name_what_the_window_saved(self) -> None:
        host = Sandbox(self)
        ok = host.run('record_is "${PREV:?}"')
        host.assert_clean(self, ok)
        host.write_record(sha1("an apply the window did not make"))
        moved = host.run('record_is "${PREV:?}"\nmust echo not reached')
        self.assertIn("un apply ajeno movió el registro", moved.output)
        # The `must` that follows is held back by the STOP, not run.
        self.assertIn("STOP previo, no se ejecuta: echo not reached", moved.output)
        self.assertEqual(moved.output.count("not reached"), 1)
        # After a good apply it names the merge commit, which the rollback
        # accepts, and nothing else.
        host.write_record(host.merge)
        self.assertEqual(
            host.run(
                'record_is "${PREV:?}" "${MERGE:?}"', prelude=f"MERGE={host.merge}\n"
            ).halt,
            "",
        )
        host.write_record(sha1("a third commit"))
        third = host.run(
            'record_is "${PREV:?}" "${MERGE:?}"', prelude=f"MERGE={host.merge}\n"
        )
        self.assertTrue(third.halt)
        # Without a MERGE (the window never merged) the line names PREV alone.
        host.write_record(host.merge)
        alone = host.run(block_with(ROLLBACK_PREV))
        self.assertTrue(alone.halt, alone.output)
        self.assertFalse((host.root / "run/deploy.log").exists())

    def test_a_transient_container_stops_the_window_until_it_is_gone(self) -> None:
        for status, label in (
            ("Up 3 minutes", "running"),
            ("Exited (137) 2 minutes ago", "stopped"),
        ):
            for name in (
                "ax-lab-ate-setup",
                "ax-lab-build-atelet",
                "ax-lab-build-ate-setup",
            ):
                with self.subTest(name=name, state=label):
                    host = Sandbox(self)
                    (host.root / "world/containers").write_text(f"{name}\t{status}\n")
                    result = host.run("no_transient")
                    self.assertTrue(result.halt, result.output)
                    self.assertIn("contenedor transitorio del gestor", result.output)
                    self.assertIn(name, result.output)
        host = Sandbox(self)
        (host.root / "world/containers").write_text(
            "kind-registry\tUp 6 days\nax-web-edge\tUp 6 days\n"
            "ax-lab-ate-setup-old\tUp 1 minute\nax-lab-builder\tUp 1 minute\n"
        )
        host.assert_clean(self, host.run("no_transient"))

    def test_the_rollback_stops_before_asking_the_lock_when_a_container_is_left(
        self,
    ) -> None:
        host = Sandbox(self)
        for status in ("Up 3 minutes", "Exited (137) 2 minutes ago"):
            (host.root / "world/containers").write_text(f"ax-lab-ate-setup\t{status}\n")
            result = host.run(block_with(ROLLBACK))
            with self.subTest(status=status):
                self.assertTrue(result.halt, result.output)
                self.assertIn("ax-lab-ate-setup", result.output)
                # No locked command was even tried, so no marker, no refusal.
                self.assertEqual(host.calls(), [])
                self.assertFalse(host.marker)
        # Without it the same block goes through (nothing to restore).
        (host.root / "world/containers").write_text("")
        host.assert_clean(self, host.run(block_with(ROLLBACK)))

    def test_a_stopped_container_is_removed_under_the_lock_and_a_running_one_is_not(
        self,
    ) -> None:
        removal = block_with(TRANSIENT)
        host = Sandbox(self)
        (host.root / "world/containers").write_text("ax-lab-ate-setup\tUp 3 minutes\n")
        running = host.run(
            removal.replace(
                TRANSIENT + "NOMBRE_QUE_IMPRIMIÓ_LA_GUARDA'",
                "LEFTOVER='ax-lab-ate-setup'",
            )
        )
        self.assertTrue(running.halt, running.output)
        self.assertIn("no está parado: se espera", running.output)
        # Nothing asked the lock: a docker rm of a running container would have
        # failed and left the direct marker.
        self.assertEqual(host.calls(), [])
        self.assertFalse(host.marker)
        self.assertIn("ax-lab-ate-setup", (host.root / "world/containers").read_text())
        # A name that is not the manager's is refused before anything runs.
        other = host.run(
            removal.replace("NOMBRE_QUE_IMPRIMIÓ_LA_GUARDA", "kind-registry")
        )
        self.assertIn("no es un contenedor transitorio del gestor", other.output)
        self.assertEqual(host.calls(), [])
        # Stopped: the logs, the removal under the lock with its operation, and
        # the guard passes after it.
        (host.root / "world/containers").write_text(
            "ax-lab-ate-setup\tExited (137) 2 minutes ago\n"
        )
        stopped = host.run(
            removal.replace("NOMBRE_QUE_IMPRIMIÓ_LA_GUARDA", "ax-lab-ate-setup")
        )
        # The guard at the top of the block prints the name and stops, which is
        # what it is there for; `HALT=` is the decision, and the end passes.
        self.assertIn(
            "STOP: contenedor transitorio del gestor: ax-lab-ate-setup", stopped.output
        )
        self.assertEqual(stopped.halt, "", stopped.output)
        self.assertFalse(host.marker)
        self.assertEqual([call["code"] for call in host.calls()], [0])
        self.assertIn("log of ax-lab-ate-setup", stopped.output)
        self.assertEqual(
            [call["argv"][:3] for call in host.calls()],
            [["run", "--operation", "ax-lab-transient"]],
        )
        self.assertEqual((host.root / "world/containers").read_text(), "")

    def test_the_baseline_and_its_comparison_catch_a_moved_counter_and_a_restart(
        self,
    ) -> None:
        pods = [
            ("ate-system", "ate-api-server-aaa-1", 4),
            ("ate-system", "ate-api-server-aaa-2", 4),
            ("ate-system", "ate-controller-bbb-1", 3),
            ("ate-system", "atelet-67253354-xyz", 4),
            ("ate-system", "atenet-router-ccc-1", 5),
            ("ate-system", "atenet-egress-ddd-1", 4),
            ("ate-system", "postgres-0", 4),
            ("ate-system", "rustfs-eee-1", 3),
            ("ate-system", "rustfs-bucket-init-fff", 0),
            ("ax-system", "ax-controller-ggg-1", 2),
            ("ax-system", "ax-server-hhh-1", 2),
            ("ax-system", "ax-redis-iii-1", 2),
            ("ax-web", "ax-web-jjj-1", 0),
            ("ax-workers", "ax-kkk-1", 1),
            ("kube-system", "coredns-lll-1", 2),
            ("kube-system", "kube-scheduler-kind-control-plane", 4),
            ("podcertificate-controller-system", "podcertificate-controller-mmm-1", 2),
        ]
        host = Sandbox(self)
        host.write_pods(pods, [])
        # What the incident of 2026-10-09 left: the hierarchical counter reads
        # non-zero and the local one zero.
        for name in ("kind-control-plane", "kind-registry"):
            host.write_cgroup(name, events={"oom_kill": 65, "oom_group_kill": 29})
        make_directories(host.relock)
        base = host.run(block_with(STEP_11_BASELINE))
        host.assert_clean(self, base)
        saved = host.relock / "baseline"
        self.assertEqual(stat.S_IMODE((saved / "memory.txt").stat().st_mode), 0o600)
        memory = (saved / "memory.txt").read_text().splitlines()
        self.assertIn("kind-control-plane memory.events oom_kill 65", memory)
        self.assertIn("kind-control-plane memory.events.local oom_kill 0", memory)
        self.assertIn("kind-registry memory.stat anon 1380769792", memory)
        self.assertIn("kind-control-plane memory.current 2552602624", memory)
        self.assertFalse([line for line in memory if "memory.peak" in line])
        restarts = (saved / "restarts.txt").read_text().splitlines()
        self.assertEqual(
            restarts,
            sorted(
                f"{ns}/{name} {count}"
                for ns, name, count in pods
                if not name.startswith(
                    (
                        "ate-api-server",
                        "ate-controller",
                        "atelet",
                        "atenet-router",
                        "atenet-egress",
                        "ax-controller",
                        "ax-server",
                        "podcertificate-controller",
                    )
                )
                and ns != "ax-workers"
            ),
        )
        replaced = (saved / "replaced.txt").read_text().splitlines()
        self.assertEqual(len(replaced), 10)
        self.assertIn("ax-workers/ax-kkk-1 1", replaced)
        self.assertNotIn("ate-system/postgres-0 4", replaced)
        compare = block_with(STEP_12_COMPARE, without="worker_gate")
        same = host.run(compare)
        host.assert_clean(self, same)
        self.assertEqual(
            (host.root / "run/deploy.log").read_text().strip(),
            "--playbook ax-lab --local --check",
        )
        # The replaced pods come back with other names and no restarts: that is
        # the apply working, and it is not compared.
        host.write_pods(
            [
                (ns, name + "-new", 0)
                if (ns, name, count) in pods[:6]
                else (ns, name, count)
                for ns, name, count in pods
            ],
            [],
        )
        host.assert_clean(self, host.run(compare))
        # A pod the apply does not replace restarted: stop.
        host.write_pods(
            [(ns, name, count + (name == "rustfs-eee-1")) for ns, name, count in pods],
            [],
        )
        again = host.run(compare)
        self.assertTrue(again.halt, again.output)
        self.assertIn("rustfs-eee-1 3", again.output)
        host.write_pods(pods, [])
        # A counter that moved: the hierarchical one, the local one, in the
        # node and in the registry.
        for name, events, local in (
            ("kind-control-plane", {"oom_kill": 66, "oom_group_kill": 29}, {}),
            ("kind-control-plane", {"oom_kill": 65, "oom_group_kill": 29}, {"oom": 1}),
            ("kind-registry", {"oom_kill": 65, "oom_group_kill": 30}, {}),
            ("kind-registry", {"oom_kill": 65, "oom_group_kill": 29}, {"oom_kill": 1}),
        ):
            with self.subTest(moved=(name, events, local)):
                host.write_cgroup(name, events=events, local=local)
                moved = host.run(compare)
                self.assertTrue(moved.halt, moved.output)
                self.assertIn("oom", moved.output)
                host.write_cgroup(name, events={"oom_kill": 65, "oom_group_kill": 29})
        # The value read in the baseline is the reference, whatever it is:
        # page cache moving memory.current or anon never stops the window.
        scope = (
            host.root
            / "cgroup"
            / f"docker-{hashlib.sha256(b'kind-control-plane').hexdigest()}.scope"
        )
        (scope / "memory.current").write_text("3758096384\n")
        (scope / "memory.stat").write_text("anon 1\nfile 2\n")
        host.assert_clean(self, host.run(compare))

    def test_the_worker_gate_wants_one_ready_pod_with_the_new_digest(self) -> None:
        def pod(
            digest: str, *, ready: bool = True, deleting: bool = False
        ) -> dict[str, Any]:
            document: dict[str, Any] = {
                "metadata": {"name": "ax-1"},
                "spec": {
                    "containers": [
                        {"image": f"localhost:5001/ateom-gvisor:{TAG}@{digest}"}
                    ]
                },
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "True" if ready else "False"}
                    ]
                },
            }
            if deleting:
                document["metadata"]["deletionTimestamp"] = "2026-10-12T12:00:00Z"
            return document

        host = Sandbox(self)
        new = host.old["ateom-gvisor"]["digest"]
        old = sha(b"the old worker")
        gate = block_with(STEP_12_GATE)
        cases = {
            "one ready pod with the digest": ([pod(new)], 0, True),
            "the old one still terminating": (
                [pod(new), pod(old, deleting=True)],
                0,
                True,
            ),
            "two live pods": ([pod(new), pod(new)], 0, False),
            "the old digest": ([pod(old)], 0, False),
            "not ready": ([pod(new, ready=False)], 0, False),
            "no pod": ([], 0, False),
            "the rollout does not finish": ([pod(new)], 1, False),
        }
        for label, (pods, code, passes) in cases.items():
            with self.subTest(case=label):
                host.write_pods([], pods)
                (host.root / "world/rollout.rc").write_text(str(code))
                (host.root / "run/tarea.log").unlink(missing_ok=True)
                result = host.run(gate)
                if passes:
                    host.assert_clean(self, result)
                    # The read-only smoke Task ran, once, with its four turns.
                    log = (host.root / "run/tarea.log").read_text().splitlines()
                    self.assertEqual(len(log), 1)
                    self.assertTrue(
                        log[0].startswith(
                            "4 master https://github.com/octocat/Hello-World "
                        )
                    )
                    self.assertTrue(log[0].endswith(" claude"))
                else:
                    self.assertTrue(result.halt, result.output)
                    # Nothing went on to create a Task.
                    self.assertFalse((host.root / "run/tarea.log").exists())

    def test_a_failed_smoke_stops_before_the_last_check(self) -> None:
        host = Sandbox(self)
        host.write_pods(
            [],
            [
                {
                    "metadata": {"name": "ax-1"},
                    "spec": {
                        "containers": [
                            {"image": "x@" + host.old["ateom-gvisor"]["digest"]}
                        ]
                    },
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                }
            ],
        )
        (host.root / "world/tarea.rc").write_text("1")
        result = host.run(
            block_with(STEP_12_GATE), block_with(STEP_12_COMPARE, without="worker_gate")
        )
        self.assertTrue(result.halt, result.output)
        self.assertIn("ax-tarea", result.output)
        self.assertFalse((host.root / "run/deploy.log").exists())

    def test_the_watch_through_the_default_client_is_what_the_shim_refuses(
        self,
    ) -> None:
        # The lab's kubectl cuts a watch at 10 s with --request-timeout, so the
        # rollout wait must not go through KUBECTL: the shim refuses it.
        host = Sandbox(self)
        host.write_pods([], [])
        refused = host.run(
            '"${KUBECTL[@]}" --namespace ax-workers rollout status deployment/ax --timeout=300s'
        )
        self.assertIn("refused", refused.output)
        allowed = host.run(
            '"${KUBECTL_WAIT[@]}" --namespace ax-workers rollout status deployment/ax --timeout=300s'
        )
        self.assertNotIn("refused", allowed.output)

    def test_the_office_volume_is_copied_after_the_queue_is_paused_and_read(
        self,
    ) -> None:
        host = Sandbox(self)
        copy = block_with(PVC_COPY)
        result = host.run(self.start_block(), copy)
        host.assert_clean(self, result)
        states = sorted((host.root / "backups").glob("ax-web-state-*"))
        self.assertEqual(len(states), 1)
        self.assertEqual(
            json.loads((states[0] / "data/office.json").read_text())["settings"][
                "queue_paused"
            ],
            True,
        )
        self.assertEqual(stat.S_IMODE(states[0].stat().st_mode), 0o700)
        # Not paused: the reads stop the window, and nothing is copied.
        host = Sandbox(self)
        host.write_office(paused=False, schedules=[], statuses=[])
        result = host.run(self.start_block(), copy)
        self.assertTrue(result.halt)
        self.assertEqual(list((host.root / "backups").glob("ax-web-state-*")), [])
        self.assertEqual(host.calls(), [])

    def test_the_rollback_does_not_need_ax_server_and_the_forward_guards_do(
        self,
    ) -> None:
        # If the new ax-server is what crash-loops, `ax get tasks` fails: the
        # rollback must not stop there, and the reads before the apply must.
        host = Sandbox(self)
        host.write_record(host.merge)
        (host.root / "world/ax-server-down").write_text("")
        forward = host.run(self.start_block())
        self.assertTrue(forward.halt, forward.output)
        self.assertIn("connection refused", forward.output)
        guards = block_with(ROLLBACK_PREV)
        result = host.run(guards, prelude=f"MERGE={host.merge}\n")
        host.assert_clean(self, result)
        self.assertEqual(
            (host.root / "run/deploy.log").read_text().splitlines(),
            ["--playbook ax-lab --local --check"],
        )
        # What does not need it still stops: a scope of ax-tarea, a tunnel,
        # a job of the Oficina, the memory of the host.
        for label, prepare in {
            "a tunnel": lambda: (host.root / "world/tunnels").write_text("PID LOCAL\n"),
            "a scope": lambda: (host.root / "world/scopes").write_text(
                "ax-cli-1.scope loaded active running\n"
            ),
            "a job": lambda: host.write_office(
                paused=True, schedules=[], statuses=["en_curso"]
            ),
            "memory": lambda: (host.root / "meminfo").write_text(
                "MemAvailable: 1 kB\n"
            ),
        }.items():
            with self.subTest(case=label):
                host.reset_world()
                (host.root / "world/ax-server-down").write_text("")
                (host.root / "run/deploy.log").unlink(missing_ok=True)
                prepare()
                result = host.run(guards, prelude=f"MERGE={host.merge}\n")
                self.assertTrue(result.halt, result.output)
                self.assertFalse((host.root / "run/deploy.log").exists())

    def test_the_rollback_after_the_apply_runs_its_commands_in_order_and_guarded(
        self,
    ) -> None:
        host = Sandbox(self)
        prelude = f"MERGE={host.merge}\n"
        host.write_record(host.merge)
        guards = block_with(ROLLBACK_PREV)
        apply = one_block(
            section("### Marcha atrás de la ventana de resiembra"),
            "--confirm-production",
        )
        # The merge commit names the record after a good apply: it goes on.
        first = host.run(guards, apply, prelude=prelude)
        host.assert_clean(self, first)
        self.assertEqual(
            (host.root / "run/deploy.log").read_text().splitlines(),
            [
                "--playbook ax-lab --local --check",
                "--playbook ax-lab --local --confirm-production",
            ],
        )
        # A job running, the queue not paused, an apply of someone else: stop
        # before the check, and the apply that follows is held back.
        for label, prepare in {
            "a job running": lambda: host.write_office(
                paused=True, schedules=[], statuses=["en_curso"]
            ),
            "queue not paused": lambda: host.write_office(
                paused=False, schedules=[], statuses=[]
            ),
            "a third commit in the record": lambda: host.write_record(sha1("a third")),
        }.items():
            with self.subTest(case=label):
                host.reset_world()
                host.write_record(host.merge)
                prepare()
                (host.root / "run/deploy.log").unlink(missing_ok=True)
                result = host.run(guards, apply, prelude=prelude)
                self.assertTrue(result.halt, result.output)
                self.assertFalse((host.root / "run/deploy.log").exists())
        # Before the apply nothing else is named: the record names PREV alone.
        host.reset_world()
        host.write_record(host.prev)
        before = block_with('record_is "${PREV:?}"\nmust ./scripts')
        clean = host.run(before)
        host.assert_clean(self, clean)
        host.write_record(host.merge)
        refused = host.run(before)
        self.assertTrue(refused.halt, refused.output)


@unittest.skipUnless(sys.platform.startswith("linux"), "the lock needs /proc")
@unittest.skipUnless(
    all(shutil.which(tool) for tool in (*EXECUTION_TOOLS, "git"))
    and all(
        Path(path).exists()
        for path in (
            "/usr/bin/python3",
            "/usr/bin/install",
            "/usr/bin/cp",
            "/usr/bin/mv",
        )
    )
    and needs_repo(),
    "the window's tools, git or PyYAML are not installed",
)
class WindowRepositoryExecutionTests(unittest.TestCase):
    """Steps 1 to 9 run in a git repository, and a shell that is lost."""

    def step_one(self, host: Sandbox, *, prelude: str = "") -> types.SimpleNamespace:
        return host.run(
            block_with(STEP_1),
            block_with(STEP_1_FACTS),
            DUMP % "dump-one",
            inject=False,
            prelude=prelude,
        )

    @staticmethod
    def dumped(host: Sandbox, name: str) -> dict[str, list[str]]:
        """The variables a DUMP wrote: each name and its values."""
        found: dict[str, list[str]] = {}
        for line in (host.root / name).read_text().splitlines():
            match = re.match(r"declare -\S+ (\w+)=(.*)$", line) or re.match(
                r"(\w+)=(.*)$", line
            )
            if match:
                value = match.group(2)
                found[match.group(1)] = re.findall(r'"([^"]*)"', value) or [
                    value.strip("'\"")
                ]
        return found

    def test_step_one_computes_the_pins_of_the_moved_images(self) -> None:
        host = Sandbox(self, repo=True)
        result = self.step_one(host)
        host.assert_clean(self, result)
        facts = self.dumped(host, "dump-one")
        pins = lambda images, names: sorted(  # noqa: E731
            host.pin(images, name) for name in names
        )
        self.assertEqual(facts["STALE_SUB"], pins(host.old, host.SUBSTRATE_MOVED))
        self.assertEqual(facts["MOVED_SUB"], pins(host.new, host.SUBSTRATE_MOVED))
        self.assertEqual(facts["STALE_KO"], pins(host.old, host.KO_MOVED))
        self.assertEqual(facts["MOVED_KO"], pins(host.new, host.KO_MOVED))
        self.assertEqual(len(facts["OLD_SUB"]), 7)
        self.assertEqual(len(facts["NEW_KO"]), 2)
        self.assertEqual(facts["PREV"], [host.prev])
        self.assertEqual(facts["PR_HEAD"], [host.pr_head])
        self.assertEqual(facts["TAG_SUB"], [TAG])
        self.assertEqual(facts["TAG_AX"], [AX_TAG])
        self.assertEqual(facts["CLI_OLD"], [hashlib.sha256(host.CLI_OLD).hexdigest()])
        self.assertEqual(facts["CLI_NEW"], [hashlib.sha256(host.CLI_NEW).hexdigest()])
        self.assertEqual(
            facts["BUILDX"],
            [host.pin(host.old, "ax-task-runner"), host.pin(host.old, "ax-agents")],
        )
        self.assertEqual(facts["WEB_TAG"], [WEB_TAG])
        self.assertEqual(facts["WEB_PIN"], [host.pin(host.old, "ax-web")])
        self.assertEqual(facts["WORKER_OLD"], [host.old["ateom-gvisor"]["digest"]])
        # The clone is on the head of the PR, detached and clean.
        self.assertEqual(host.git("rev-parse", "HEAD"), host.pr_head)
        self.assertEqual(
            host.git("status", "--porcelain", "--untracked-files=normal"), ""
        )

    def test_step_one_stops_where_the_audit_found_it_stops(self) -> None:
        # The record names the commit of the PR itself: the plans do not move.
        host = Sandbox(self, repo=True)
        host.write_record(host.pr_head)
        result = self.step_one(host)
        self.assertIn(
            "STOP: los pines de Substrate no cambian por pares", result.output
        )
        # The record names the merge commit (after a good apply): not an
        # ancestor of the head of the PR, so it stops at the check of PREV.
        host = Sandbox(self, repo=True)
        host.write_record(host.merge_pr())
        result = self.step_one(host)
        self.assertTrue(result.halt, result.output)
        self.assertIn("STOP: git merge-base --is-ancestor", result.output)
        # The placeholder was not replaced.
        host = Sandbox(self, repo=True)
        text = block_with(STEP_1).replace(
            Sandbox.PLACEHOLDER, "PR_HEAD='no es un commit'"
        )
        result = host.run(text, inject=False)
        self.assertIn("STOP: PR_HEAD no es un commit completo", result.output)
        # A dirty clone.
        host = Sandbox(self, repo=True)
        (host.cwd / "stray").write_text("x")
        result = self.step_one(host)
        self.assertTrue(result.halt, result.output)
        self.assertIn("?? stray", result.output)

    def test_step_one_stops_when_a_pin_that_must_stay_changes(self) -> None:
        for label, edit, message in (
            (
                "a buildx image",
                lambda text, host: text.replace(
                    host.old["ax-agents"]["digest"], host.old["ax-web"]["digest"], 1
                ),
                "ax-agents cambia, y la ventana no la toca",
            ),
            (
                "the panel",
                lambda text, host: text.replace(
                    host.old["ax-web"]["digest"], host.old["ax-agents"]["digest"], 1
                ),
                "cambia el digest del panel",
            ),
        ):
            with self.subTest(case=label):
                host = Sandbox(self, repo=True)
                host.git("checkout", "--quiet", "pr")
                config = host.cwd / "config/ax-lab.yml"
                config.write_text(edit(config.read_text(), host))
                host.pr_head = host.commit("a pin the window must not move")
                host.git("push", "--quiet", "origin", "pr")
                host.git("checkout", "--quiet", "--detach", host.prev)
                result = self.step_one(host)
                self.assertIn(f"STOP: {message}", result.output)

    def run_window(self, host: Sandbox, umask: str) -> types.SimpleNamespace:
        """Steps 1 to 9, as a shell that stays up pastes them."""
        host.merge_pr()
        blocks = [
            block_with(STEP_1),
            block_with(STEP_1_FACTS),
            block_with(STEP_2),
            block_with(STEP_3),
            block_with(STEP_4),
            block_with(STEP_5),
            block_with(STEP_6),
            block_with(STEP_7),
            block_with(STEP_8),
            block_with(STEP_9),
            DUMP % "dump-a",
        ]
        prelude = "".join(
            f"{key}={shlex.quote(path)}\n" for key, path in host.zips().items()
        )
        return host.run(*blocks, inject=False, umask=umask, prelude=prelude)

    def test_a_fresh_shell_recovers_the_rollback_after_a_successful_apply(self) -> None:
        host = Sandbox(self, repo=True)
        first = self.run_window(host, umask="0007")
        host.assert_clean(self, first)
        host.assert_live(self, "new")
        self.assertEqual(host.cli.read_bytes(), host.CLI_NEW)
        # The facts are on disk, root-only and outside /tmp.
        saved = host.relock / "window.env"
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertEqual(
            saved.read_text().splitlines(),
            [f"PR_HEAD={host.pr_head}", f"PREV={host.prev}", f"MERGE={host.merge}"],
        )
        self.assertEqual(stat.S_IMODE((host.relock / "plans").stat().st_mode), 0o700)
        self.assertEqual(
            sorted(path.name for path in (host.relock / "plans").iterdir()),
            [
                "ax-lab-old.yml",
                "ax-new.json",
                "ax-old.json",
                "sub-new.json",
                "sub-old.json",
            ],
        )
        for path in (host.relock / "plans").iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        # The apply happened (the record now names the merge commit, which is
        # what deployment_metadata writes) and the shell, its variables and
        # its /tmp are lost.
        host.write_record(host.merge)
        (host.root / "bin/ax").write_bytes(host.CLI_NEW)
        shutil.rmtree(host.root / "work")
        make_directories(host.root / "work")
        # A new bash that pastes only step 0, the recovery and the rollback.
        rollback = section("### Marcha atrás de la ventana de resiembra")
        second = host.run(
            block_with(RECOVER),
            DUMP % "dump-b",
            block_with(ROLLBACK),
            # And on to the apply from PREV: the clone goes back to it, and the
            # record that names the merge commit is accepted because the
            # recovery brought MERGE back from window.env.
            one_block(rollback, 'git checkout --detach "${PREV:?}"'),
            block_with(ROLLBACK_PREV),
            one_block(rollback, "--confirm-production"),
            inject=False,
        )
        host.assert_clean(self, second)
        host.assert_live(self, "old")
        self.assertEqual(host.cli.read_bytes(), host.CLI_OLD)
        self.assertEqual(host.git("rev-parse", "HEAD"), host.prev)
        self.assertEqual(
            (host.root / "run/deploy.log").read_text().splitlines(),
            [
                "--playbook ax-lab --local --check",
                "--playbook ax-lab --local --confirm-production",
            ],
        )
        # It rebuilt exactly what the first shell had.
        self.assertEqual(
            (host.root / "dump-a").read_text(), (host.root / "dump-b").read_text()
        )
        self.assertIn(f"MERGE={host.merge}", (host.root / "dump-b").read_text())
        # Without the recovery the rollback has nothing to go on: it stops.
        control = Sandbox(self)
        control.assert_clean(self, control.run(block_with(STEP_2)))
        control.extract()
        control.run(block_with(STEP_5), block_with(STEP_6), block_with(STEP_8))
        control.assert_live(self, "new")
        lost = control.run(block_with(ROLLBACK), inject=False)
        self.assertTrue(lost.halt, lost.output)
        control.assert_live(self, "new")

    def test_the_artifacts_extracted_under_the_umask_of_sudo_pass_the_checks(
        self,
    ) -> None:
        host = Sandbox(self, repo=True)
        result = self.run_window(host, umask="0007")
        host.assert_clean(self, result)
        # What a root under sudo's 0027 extracts has no write bit for anyone
        # else: the find of step 3 stays quiet and the manager read it.
        modes = {
            path: stat.S_IMODE(path.stat().st_mode)
            for path in [host.inputs, *host.inputs.rglob("*")]
        }
        self.assertTrue(modes)
        for path, mode in modes.items():
            with self.subTest(path=path.relative_to(host.inputs).as_posix()):
                self.assertEqual(mode & 0o022, 0)
        extracted = host.inputs / "substrate/index.json"
        self.assertEqual(stat.S_IMODE(extracted.stat().st_mode), 0o640)
        self.assertEqual(
            stat.S_IMODE((host.inputs / "substrate/blobs").stat().st_mode), 0o750
        )
        # The control: a sudo that kept the admin's 0007 extracts group-writable
        # files, and the check stops the window before anything runs the CLI.
        other = Sandbox(self, repo=True)
        prelude = "".join(
            f"{key}={shlex.quote(path)}\n" for key, path in other.zips().items()
        )
        steps = [
            block_with(anchor) for anchor in (STEP_1, STEP_1_FACTS, STEP_2, STEP_3)
        ]
        loose = other.run(
            *steps, inject=False, umask="0007", sudo_umask="0007", prelude=prelude
        )
        self.assertTrue(loose.halt, loose.output)
        self.assertIn("la CLI no pasa la comprobación", loose.output)
        self.assertFalse((other.root / "witness").exists())


class StaleFallbackTextTests(unittest.TestCase):
    def test_the_fallback_text_says_the_host_never_patches_once_relocked(self) -> None:
        docs = read("docs/AX.md")
        fallback = docs.split("### Compilación de reserva\n", 1)[1]
        fallback = flat(fallback.split("\n### ", 1)[0])
        for fragment in (
            "`sources.substrate.modules_patch`",
            "el host nunca parchea ni vendoriza Substrate",
            "`ax_lab_substrate_fallback_builds` que no esté vacía",
            "re-fijar una imagen pasa por `forget` más `seed-layout`",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, fallback)
        backup = flat(
            docs.split("### Copia de seguridad\n", 1)[1].split("\n### ", 1)[0]
        )
        self.assertIn("un re-pin pasa siempre por `forget` más `seed-layout`", backup)

    def test_the_branch_k_bullet_keeps_the_runner_and_agents_findings(self) -> None:
        docs = read("docs/AX.md")
        bullet = docs.split("### Lo que no se puede codificar fielmente\n", 1)[1]
        bullet = flat(bullet.split("\n### ", 1)[0])
        self.assertIn(
            "`ax-task-runner` y `ax-agents` se quedan sembradas tal como están, con Go "
            "1.27.1 y con los módulos de antes de la subida",
            bullet,
        )
        self.assertIn("siguen abiertos", bullet)

    def test_the_changelog_and_the_ax_readme_know_the_images_workflow(self) -> None:
        unreleased = read("CHANGELOG.md").split("\n## [0.1.0]", 1)[0]
        for fragment in (
            "ax-lab-images.yml",
            "ax-lab-substrate-oci-layout",
            "ax-lab-ax-artifacts",
            "Imágenes compiladas en la CI y ventana de resiembra",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, flat(unreleased))
        self.assertIn("ax-lab-images.yml", read("images/ax/README.md"))


if __name__ == "__main__":
    unittest.main()
