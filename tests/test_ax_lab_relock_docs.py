"""The relock window of the AX lab in docs/AX.md.

The section «Imágenes compiladas en la CI y ventana de resiembra» is a
procedure, not code, so what can be proved offline is its shape: the commands
come in the order that keeps the production copy recoverable, nothing reads
the live copy with the new contract before the old entries are forgotten,
every manager call names its image set, the PR of the pins is merged inside
the window and never before the seed, and no value of any pin is written in
it (the window takes them from the validated plans).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

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
    blocks = re.findall(r"^( *)```bash\n(.*?)^\1```$", text, flags=re.DOTALL | re.M)
    return [textwrap.dedent(body) for _indent, body in blocks]


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
                'sudo -- "${IN}/ax/ax" --help',
                # The rehearsal into a scratch layout, proved complete.
                f'{MANAGER} seed-layout --image-set substrate --source "${{IN}}/substrate"'
                ' --layout "${REH}/images"',
                f'{MANAGER} seed-layout --image-set ax --source "${{IN}}/ax/ko-${{name}}"'
                ' --layout "${REH}/images"',
                f'{MANAGER} image-status --image-set substrate --layout "${{REH}}/images"',
                # Only then the forget, by the old digests, on the live copy.
                f'{MANAGER} forget --image-set substrate --layout "${{LAYOUT}}"',
                f'{MANAGER} forget --image-set ax --layout "${{LAYOUT}}"',
                # The real seed, then the live copy is read with the new pins.
                f'{MANAGER} seed-layout --image-set substrate --source "${{IN}}/substrate"'
                ' --layout "${LAYOUT}"',
                f'{MANAGER} seed-layout --image-set ax --source "${{IN}}/ax/ko-${{name}}"'
                ' --layout "${LAYOUT}"',
                f'{MANAGER} image-status --image-set substrate --layout "${{LAYOUT}}"',
                # The CLI goes in last of the copy, under the lock.
                f"{LOCK} /usr/bin/install -o root -g root -m 0600"
                ' "${IN}/ax/ax" "${CLI_BACKUP}"',
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
        before = text[: text.index(f"{MANAGER} forget --image-set substrate")]
        # The playbook (its --check reads the copy too) and every manager call
        # on the live layout wait for the forget; the snapshot and the
        # rehearsal use other layouts.
        self.assertNotIn("deploy-ansible.sh", before)
        self.assertNotIn('--layout "${LAYOUT}"', before)
        self.assertNotIn(" --registry ", before.split("### Pasos")[1])

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
                f'{MANAGER} seed-layout --image-set substrate --source "${{IN}}/substrate"'
                ' --layout "${LAYOUT}"',
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
            text.index("cp -a"),
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
                'sudo -- "${IN}/ax/ax" --help',
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
                '[[ "$(sudo -- sha256sum "${cli}" | cut -d\' \' -f1)" =='
                ' "${CLI_NEW}" ]]',
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
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, step_zero)

    def test_the_rehearsal_precedes_the_forget_and_is_asserted(self) -> None:
        text = flat(window())
        positions(
            self,
            text,
            [
                'sudo -- install -d -o root -g root -m 0700 "${REH}"',
                'expect_all "${#MOVED_SUB[@]}" seeded "${OUT}"',
                'expect_complete "${#MOVED_SUB[@]}" "${OUT}"',
                'sudo -- rm -rf "${REH:?}"',
                'expect_all "${#STALE_SUB[@]}" forgotten "${OUT}"',
                'expect_all "${#MOVED_SUB[@]}" seeded "${OUT}"',
                'expect_complete 7 "${OUT}"',
            ],
        )

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
                f'{MANAGER} forget --image-set substrate --layout "${{LAYOUT}}"'
                ' --tag "${TAG_SUB}" "${new}"',
                f'{MANAGER} seed-layout --image-set substrate --source "${{SNAP_LAYOUT}}"',
                f'{MANAGER} forget --image-set ax --layout "${{LAYOUT}}"'
                ' --tag "${TAG_AX}" "${new}"',
                f'{MANAGER} seed-layout --image-set ax --source "${{SNAP_LAYOUT}}"',
                'expect_complete 7 "${OUT}"',
                'expect_complete 4 "${OUT}"',
                f"{LOCK} /usr/bin/install -o root -g root -m 0600"
                ' "${SNAP_CLI}" "${CLI_BACKUP}"',
            ],
        )
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
