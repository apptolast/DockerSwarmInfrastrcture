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
        self.assertIn("Desde el `forget` hasta el último `changed=0`", text)
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
            "La cola de la Oficina, vacía",
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
                'mapfile -t OLD_SUB < <(pins "${WORK}/sub-old.json" images)',
            ],
        )
        for fragment in (
            'plan . --substrate-plan >"${WORK}/sub-new.json"',
            'plan . --ax-plan >"${WORK}/ax-new.json"',
            'mapfile -t NEW_SUB < <(pins "${WORK}/sub-new.json" images)',
            'mapfile -t NEW_KO < <(pins "${WORK}/ax-new.json" ko_images)',
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

LOCK_LAUNCHER = r"""
import importlib.util
import json
import os
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
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

# The admin the window runs as is made root by sudo. Nothing here is root, so
# sudo only drops itself, and what names root (the owner of an installed file,
# the owner a find looks for) names this user.
PREAMBLE = r"""
umask 077
cd "${T}" || exit 1
export RELOCK_TEST_REPO TMPDIR="${T}/work"
ME=$(id -un)
sudo() {
  if [[ "$1" == -- ]]; then
    shift
  fi
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
}
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
DEFINITIONS = "image_state() {"
STEP_2 = "df -h /var/backups"
STEP_4 = 'seed_one substrate "${IN}/substrate" "${REH}/images"'
STEP_5 = 'forget_one substrate "${LAYOUT}" "${TAG_SUB}" "${old}"'
STEP_6 = 'seed_one substrate "${IN}/substrate" "${LAYOUT}"'
STEP_7 = '--image-set web --layout "${LAYOUT}"'
STEP_8 = '"${IN}/ax/ax" "${CLI_BACKUP}"'
ROLLBACK = 'forget_one substrate "${LAYOUT}" "${TAG_SUB}" "${new}"'
RECOVERY = "scripts/host_global_operation_lock.py recover"
CLI_CHAIN = 'sha256sum "${IN}/ax/ax"'


def block_with(anchor: str) -> str:
    found = [block for block in bash_blocks(window()) if anchor in block]
    if len(found) != 1:
        raise AssertionError(f"{anchor!r} must be in one block, not {len(found)}")
    return found[0]


def make_directories(path: Path) -> None:
    """mkdir -p with mode 0700 on every component (a umask may be 0007)."""
    for directory in reversed([path, *path.parents]):
        if not directory.exists():
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)


class Sandbox:
    """A host of the lab in a temporary directory, and the window run on it."""

    # Two Substrate images and one of the two ko images move: enough to leave a
    # window stopped half way in each set.
    SUBSTRATE_MOVED = manager.IMAGE_NAMES[:2]
    KO_MOVED = manager.AX_KO_IMAGES[:1]
    CLI_OLD = b"the CLI the copy holds\n"
    CLI_NEW = b'#!/bin/sh\n[ "$1" = help ] && touch witness; exit 0\n'

    def __init__(self, case: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory()
        case.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        for name in ("scripts", "run", "work", "backups/ax-lab/binaries"):
            make_directories(self.root / name)
        launchers = {
            "host_global_operation_lock.py": LOCK_LAUNCHER,
            "manage-ax-lab-substrate.py": MANAGER_LAUNCHER,
        }
        for name, source in launchers.items():
            (self.root / "scripts" / name).write_text(source, encoding="utf-8")
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
            "TAG_SUB": TAG,
            "TAG_AX": AX_TAG,
            "WEB_TAG": WEB_TAG,
            "WEB_PIN": self.pin(old, "ax-web"),
            "CLI_OLD": hashlib.sha256(self.CLI_OLD).hexdigest(),
            "CLI_NEW": hashlib.sha256(self.CLI_NEW).hexdigest(),
        }
        lines = [f"{key}={shlex.quote(value)}" for key, value in scalars.items()]
        lines += [
            f"{key}=({' '.join(shlex.quote(value) for value in values)})"
            for key, values in arrays.items()
        ]
        return "\n".join(lines) + "\n"

    def local(self, text: str) -> str:
        """The block, with the host's paths moved into the sandbox."""
        return text.replace("/var/backups/dockerswarm", f"{self.root}/backups").replace(
            "/run/lock/dockerswarm-*.marker", f"{self.root}/run/*.marker"
        )

    def run(self, *blocks: str) -> types.SimpleNamespace:
        """Paste the blocks, one after the other, into one shell."""
        script = "".join(
            (
                PREAMBLE,
                self.local(block_with(HELPERS)),
                self.local(block_with(DEFINITIONS)),
                self.variables(),
                *(self.local(block) + "\n" for block in blocks),
                'echo "@@HALT=[${HALT}]"\n',
            )
        )
        env = {**os.environ, "T": str(self.root), "RELOCK_TEST_REPO": str(ROOT)}
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

    # What the host holds.
    def extract(self) -> None:
        """Step 3's extraction: the artifacts of the run on the PR head."""
        make_directories(self.inputs / "ax")
        write_build_output(
            self.inputs / "substrate",
            {name: self.new[name] for name in self.SUBSTRATE_MOVED},
            TAG,
        )
        for name in self.KO_MOVED:
            write_ko_layout(self.inputs / "ax" / f"ko-{name}", self.new[name])
        (self.inputs / "ax/ax").write_bytes(self.CLI_NEW)
        (self.inputs / "ax/ax").chmod(0o644)

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
