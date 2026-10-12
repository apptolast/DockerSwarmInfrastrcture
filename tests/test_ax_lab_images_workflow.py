"""Contract of the AX lab images workflow and of its edits to the other one.

ax-lab-images.yml builds what config/ax-lab.yml pins of the lab's Go images
with the steps ax-lab-reproducibility.yml proves them with, keeps them as
run artifacts only after every pin and every proof passed, and uploads
nothing from a pull request. These checks are static, apart from running the
no-op branches and the gates of the shell steps on synthetic plans: nothing
here needs Docker, a network or a runner.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
IMAGES = WORKFLOWS / "ax-lab-images.yml"
REPRODUCIBILITY = WORKFLOWS / "ax-lab-reproducibility.yml"
PATHS = [
    "config/ax-lab.yml",
    "images/ax/**",
    "scripts/manage-ax-lab-substrate.py",
    "scripts/validate-ax-lab.py",
    ".github/workflows/ax-lab-images.yml",
]
USES = r"^[a-z-]+/[a-z-]+@[a-f0-9]{40}$"
ACTIONS = {"actions/checkout", "actions/setup-python", "actions/upload-artifact"}
# Words that would mean a registry push, a credential or a download of what
# another run kept. Two other tests scan the reproducibility workflow's whole
# text for them, and this one scans both workflows.
FORBIDDEN = (
    "docker push",
    "docker login",
    "secrets.",
    "GITHUB_TOKEN",
    "ko publish",
    "--push=true",
)
UPLOAD_IF = "github.event_name != 'pull_request'"
# The job of the images workflow that runs the same steps as a job of the
# reproducibility workflow, and the steps that must be alike to the byte: the
# same inputs and the same commands are what one keeps and the other proves.
SIBLINGS = {"substrate": "reproduce", "ax": "reproduce-ax"}
SHARED = {
    "substrate": (
        "Check out this repository",
        "Install the locked Python runtime",
        "Bootstrap locked tooling",
        "Read the Substrate build plan from the validated contract",
        "Check out Agent Substrate at the pinned commit",
        "Require the pristine pinned Substrate checkout",
        "Apply the Substrate modules patch and vendor the modules",
        "Build every pinned image with the role's bounded build",
    ),
    "ax": (
        "Check out this repository",
        "Install the locked Python runtime",
        "Bootstrap locked tooling",
        "Read the AX plan from the validated contract",
        "Check out AX at the pinned commit with its tags",
        "Check out Agent Substrate at the pinned commit",
        "Require the pristine pinned checkouts",
        "Apply the vendored patch exactly as the manual lab staged it",
        "Apply the AX modules patch on top of the vendored one",
        "Rebuild the ax CLI",
        "Rebuild the ko images of ax-controller and ax-server",
    ),
}
# What the synthetic builds of the gate tests produced.
BUILT = {
    "ateapi": "sha256:" + "1" * 64,
    "atenet": "sha256:" + "2" * 64,
}
BUILD = "Build every pinned image with the role's bounded build"
SUBSTRATE_VENDOR = "Apply the Substrate modules patch and vendor the modules"
SUBSTRATE_GATE = "Require every image pinned and every digest reproduced"
SUBSTRATE_PROOF = "Prove the kept layout with seed-layout and forget"
AX_MODULES = "Apply the AX modules patch on top of the vendored one"
AX_GATE = "Require the rebuilt CLI and ko images to match their pins"
AX_PROOF = "Prove the kept files with seed-layout and forget"
GLIBC = "Require the ax CLI to need no newer glibc than the host has"
KO = "Rebuild the ko images of ax-controller and ax-server"
COMPARE = "Require the rebuilt binaries and ko images to match their pins"


class Workflow:
    """A workflow file, parsed once, with its steps by job and name."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.text = path.read_text(encoding="utf-8")
        self.document = yaml.safe_load(self.text)

    def steps(self, job: str) -> list[dict[str, Any]]:
        return self.document["jobs"][job]["steps"]

    def names(self, job: str) -> list[str]:
        return [step["name"] for step in self.steps(job)]

    def step(self, job: str, name: str) -> dict[str, Any]:
        matches = [step for step in self.steps(job) if step["name"] == name]
        assert len(matches) == 1, f"{self.path.name}: {job}: {name!r}"
        return matches[0]

    def run(self, job: str, name: str) -> str:
        return self.step(job, name)["run"]


def flat(text: str) -> str:
    """The script with its backslash continuations joined into one line."""
    return " ".join(text.replace("\\\n", " ").split())


def manager_calls(text: str) -> list[tuple[str, str]]:
    """Every `"${manager[@]}" <command> <arguments>` of a script."""
    return re.findall(r'"\$\{manager\[@\]\}" ([a-z-]+)([^|;>]*)', flat(text))


def assert_order(test: unittest.TestCase, names: list[str], *ordered: str) -> None:
    positions = [names.index(name) for name in ordered]
    test.assertEqual(positions, sorted(positions), ordered)


# --------------------------------------------------------------------------
# The images workflow


class ImagesWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.images = Workflow(IMAGES)

    def test_triggers_and_least_privilege(self) -> None:
        document = self.images.document
        on = document["on"]
        self.assertEqual(on["pull_request"], {"paths": PATHS})
        self.assertEqual(on["push"], {"branches": ["main"], "paths": PATHS})
        self.assertEqual(set(on), {"pull_request", "push", "workflow_dispatch"})
        self.assertIsNone(on["workflow_dispatch"])
        self.assertEqual(document["permissions"], {"contents": "read"})
        self.assertEqual(list(document["jobs"]), ["substrate", "ax"])
        for job in document["jobs"].values():
            self.assertNotIn("permissions", job)
            self.assertIsInstance(job["timeout-minutes"], int)
        self.assertTrue(document["concurrency"]["cancel-in-progress"])
        self.assertIn("ax-lab-images", document["concurrency"]["group"])

    def test_only_the_three_known_actions_each_pinned_by_commit(self) -> None:
        uploads = set()
        counts = {}
        for job in self.images.document["jobs"]:
            uses = [step for step in self.images.steps(job) if "uses" in step]
            counts[job] = len(uses)
            for step in uses:
                with self.subTest(job=job, uses=step["uses"]):
                    self.assertRegex(step["uses"], USES)
                    action = step["uses"].split("@")[0]
                    self.assertIn(action, ACTIONS)
                    if action == "actions/checkout":
                        self.assertIs(step["with"]["persist-credentials"], False)
                    if action == "actions/upload-artifact":
                        uploads.add(step["uses"])
        # One pinned upload action, whatever its commit is.
        self.assertEqual(len(uploads), 1)
        self.assertEqual(counts, {"substrate": 4, "ax": 5})

    def test_nothing_is_pushed_and_no_credential_is_used(self) -> None:
        for forbidden in (*FORBIDDEN, "download-artifact"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.images.text)

    def test_files_are_kept_only_after_every_pin_and_proof_passed(self) -> None:
        jobs = {
            "substrate": (
                SUBSTRATE_GATE,
                "Stage the OCI layout of the Substrate images",
                SUBSTRATE_PROOF,
                "Keep the OCI layout of the Substrate images",
                "ax-lab-substrate-oci-layout",
            ),
            "ax": (
                AX_GATE,
                "Stage the ax CLI, the ko image layouts and their manifests",
                AX_PROOF,
                "Keep the ax CLI, the ko image layouts and their manifests",
                "ax-lab-ax-artifacts",
            ),
        }
        for job, (gate, stage, proof, upload, artifact) in jobs.items():
            with self.subTest(job=job):
                order = self.images.names(job)
                assert_order(self, order, gate, stage, proof, upload)
                self.assertEqual(order[-1], upload)
                step = self.images.step(job, upload)
                # Neither always() nor failure(): a failed step above stops
                # it, and a pull request proves everything and keeps nothing.
                self.assertEqual(step.get("if"), UPLOAD_IF)
                self.assertEqual(step["with"]["name"], artifact)
                self.assertEqual(step["with"]["if-no-files-found"], "error")
                self.assertIs(type(step["with"]["retention-days"]), int)
                self.assertIn("/keep/", step["with"]["path"])
                for name in (stage, proof):
                    self.assertNotIn("if", self.images.step(job, name))
        assert_order(
            self,
            self.images.names("ax"),
            GLIBC,
            AX_GATE,
        )

    def test_values_are_printed_before_any_gate_and_even_after_a_failure(self) -> None:
        substrate = self.images.names("substrate")
        assert_order(
            self,
            substrate,
            SUBSTRATE_VENDOR,
            BUILD,
            "Print every digest built",
            SUBSTRATE_GATE,
        )
        self.assertEqual(
            self.images.step("substrate", "Print every digest built")["if"],
            "always()",
        )
        # The gate runs after a failed build too, so a stale pin is listed
        # with every other value and the tree is still compared.
        self.assertEqual(
            self.images.step("substrate", SUBSTRATE_GATE)["if"], "always()"
        )
        ax = self.images.names("ax")
        printing = "Print every digest and hash built"
        assert_order(self, ax, KO, printing, GLIBC, AX_GATE)
        step = self.images.step("ax", printing)
        self.assertEqual(step["if"], "always()")
        # The bytes of a manifest are written as base64 beside their sha256,
        # so images/ax/manifests/<name>.json can be rebuilt from a red log.
        self.assertIn("base64 -w0", step["run"])
        self.assertIn("sha256sum", step["run"])
        self.assertNotIn('cat "${out}/ko-', step["run"])

    def test_the_substrate_tree_is_compared_after_the_digests_are_printed(self) -> None:
        vendor = self.images.run("substrate", SUBSTRATE_VENDOR)
        self.assertNotIn('[[ "${tree}"', vendor)
        self.assertIn("substrate-trees.json", vendor)
        gate = self.images.run("substrate", SUBSTRATE_GATE)
        self.assertLess(
            gate.index("jq --exit-status"), gate.index(".modules_patch.tree")
        )
        self.assertLess(
            gate.index(".modules_patch.tree"),
            gate.index('[[ "${built}" == "${pinned}" ]]'),
        )
        self.assertIn('exit "${status}"', gate)

    def test_the_ko_digests_are_exact_only_once_the_modules_patch_is_set(self) -> None:
        gate = self.images.run("ax", AX_GATE)
        # Present or absent in the plan, never an empty path that reads as
        # absent.
        branch = 'if jq -e .modules_patch "${plan}" >/dev/null; then'
        self.assertIn(branch, gate)
        self.assertLess(
            gate.index(branch), gate.index('[[ "${built}" == "${pinned}" ]]')
        )
        # The CLI is gated whatever the config says.
        self.assertLess(
            gate.index('[[ "${cli}" == "$(jq -r .cli_sha256 "${plan}")" ]]'),
            gate.index(branch),
        )

    def test_the_cli_must_not_need_a_newer_glibc_than_the_host_has(self) -> None:
        # One constant, declared at the top of its step.
        self.assertEqual(
            len(re.findall(r"^\s*host_glibc=2\.43$", self.images.text, re.M)), 1
        )
        self.assertEqual(self.images.text.count("2.43"), 1)
        step = self.images.step("ax", GLIBC)
        self.assertEqual(step["if"], "always()")
        run = step["run"]
        self.assertLess(run.index("host_glibc=2.43"), run.index("objdump -T"))
        self.assertIn("sort -V", run)
        self.assertIn('"${host_glibc}"', run)
        # A missing tool or a file that is no ELF never reads as "no symbols".
        self.assertIn("readelf -h", run)
        self.assertIn("command -v objdump", run)

    def test_the_seed_proof_runs_the_window_with_every_result_asserted(self) -> None:
        for job, proof, images in (
            ("substrate", SUBSTRATE_PROOF, 7),
            ("ax", AX_PROOF, 2),
        ):
            run = self.images.run(job, proof)
            calls = manager_calls(run)
            with self.subTest(job=job):
                self.assertEqual(
                    {command for command, _ in calls},
                    {"seed-layout", "forget", "image-status"},
                )
                self.assertGreaterEqual(len(calls), 12)
                for command, arguments in calls:
                    with self.subTest(command=command):
                        # seed-layout defaults to the web panel's image set.
                        self.assertRegex(arguments, r"^ --image-set (substrate|ax) ")
                        self.assertNotIn("--registry", arguments)
                self.assertIn(f'[[ "${{#pins[@]}}" -eq {images} ]]', run)
                # The sequence of the window, and every answer asserted.
                for fragment in (
                    "expect seeded",
                    "expect forgotten",
                    "expect_status complete",
                    "expect_status missing",
                    "--exit-status",
                    "sudo cp -a",
                    '--source "${root}/snapshot"',
                    '--source "${root}/in',
                    'grep -qF "with another digest"',
                    'grep -qF ", not ',
                    "-m zipfile -c",
                    "-I -m zipfile -e",
                    "install -d -o root -g root -m 0700",
                ):
                    self.assertIn(fragment, run)
                # A held name refuses another digest, in a layout of its own
                # for the re-pin and in the one that holds every image.
                self.assertEqual(run.count('grep -qF "with another digest"'), 2)
                # The manager refuses what another user owns: root's, not
                # the runner's temporary directory.
                self.assertIn("root=/var/lib/ax-lab-ci/seed", run)
                self.assertNotIn("RUNNER_TEMP}/seed", run)

    def test_every_manager_call_is_the_sudo_python_invocation(self) -> None:
        mentions = 0
        for job in ("substrate", "ax"):
            for step in self.images.steps(job):
                run = step.get("run", "")
                for match in re.finditer(r"manage-ax-lab-substrate\.py", run):
                    mentions += 1
                    with self.subTest(job=job, step=step["name"]):
                        before = run[: match.start()]
                        self.assertTrue(
                            before.endswith("sudo -- /usr/bin/python3 scripts/"),
                            before[-60:],
                        )
        self.assertGreaterEqual(mentions, 3)
        build = self.images.run("substrate", BUILD)
        self.assertIn("manage-ax-lab-substrate.py build", build)

    def test_the_manifests_and_the_cli_are_staged_byte_for_byte(self) -> None:
        stage = "Stage the ax CLI, the ko image layouts and their manifests"
        run = self.images.run("ax", stage)
        for fragment in (
            'cp "${out}/ax" "${keep}/ax"',
            'cp -R "${out}/ko-${name}" "${keep}/ko-${name}"',
            '"${keep}/manifests/${name}.json"',
            "sudo chmod -R a+rX",
            "pins.json",
        ):
            self.assertIn(fragment, run)

    def test_the_plan_keys_are_the_validators(self) -> None:
        for job in ("substrate", "ax"):
            for step in self.images.steps(job):
                with self.subTest(job=job, step=step["name"]):
                    # Only modules_patch.{path,sha256,tree}, never a patch.tree.
                    self.assertNotRegex(
                        step.get("run", ""), r"\.modules_patch\.(?!path|sha256|tree)"
                    )
                    self.assertNotIn(".patch.tree", step.get("run", ""))
        ax = "\n".join(step.get("run", "") for step in self.images.steps("ax"))
        substrate = "\n".join(
            step.get("run", "") for step in self.images.steps("substrate")
        )
        self.assertIn(".modules_patch.tree", substrate)
        # AX has no tree pin, and its runner binary is the other workflow's.
        self.assertNotIn("modules_patch.tree", ax)
        # A tag that is missing from the plan fails instead of reading "null".
        self.assertIn('jq -er .tag "${plan}"', ax)
        self.assertNotIn("runner_toolbox_image", self.images.text)
        self.assertNotIn("ax-task-runner", ax)

    def test_only_the_two_workflows_that_keep_files_upload_them(self) -> None:
        uploading = {
            path.name
            for path in WORKFLOWS.glob("*.yml")
            if "upload-artifact" in path.read_text(encoding="utf-8")
        }
        self.assertEqual(uploading, {"ax-web.yml", "ax-lab-images.yml"})


# --------------------------------------------------------------------------
# Both workflows run the same steps


class SharedStepTests(unittest.TestCase):
    def test_the_shared_steps_are_identical_in_both_workflows(self) -> None:
        images = Workflow(IMAGES)
        reproducibility = Workflow(REPRODUCIBILITY)
        for job, names in SHARED.items():
            sibling = SIBLINGS[job]
            for name in names:
                with self.subTest(job=job, step=name):
                    # Present by name in both, so a rename cannot make the
                    # comparison pass vacuously.
                    self.assertIn(name, images.names(job))
                    self.assertIn(name, reproducibility.names(sibling))
                    self.assertEqual(
                        images.step(job, name), reproducibility.step(sibling, name)
                    )


# --------------------------------------------------------------------------
# What the reproducibility workflow gained


class ReproducibilityEditsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = Workflow(REPRODUCIBILITY)

    def test_nothing_that_publishes_or_keeps_a_file_is_written_in_it(self) -> None:
        for forbidden in (*FORBIDDEN, "upload-artifact"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.workflow.text)
        uses = {
            job: sum("uses" in step for step in self.workflow.steps(job))
            for job in self.workflow.document["jobs"]
        }
        self.assertEqual(uses, {"reproduce": 3, "reproduce-ax": 4})

    def test_the_substrate_checkout_is_patched_and_vendored_before_the_build(
        self,
    ) -> None:
        names = self.workflow.names("reproduce")
        assert_order(
            self,
            names,
            "Require the pristine pinned Substrate checkout",
            SUBSTRATE_VENDOR,
            "Start a throwaway registry on the runner's loopback",
            BUILD,
        )
        step = self.workflow.step("reproduce", SUBSTRATE_VENDOR)
        self.assertNotIn("uses", step)
        self.assertNotIn("with", step)
        run = step["run"]
        for fragment in (
            "set -euo pipefail",
            'if ! jq -e .modules_patch "${plan}" >/dev/null; then',
            'jq -er .modules_patch.path "${plan}"',
            'jq -er .modules_patch.sha256 "${plan}"',
            'jq -er .modules_patch.tree "${plan}"',
            "No Substrate modules patch is configured.",
            "sha256sum --check --strict",
            "git -C substrate apply --numstat",
            '[[ "${paths}" == "go.mod go.sum" ]]',
            'git -C substrate apply --index "${GITHUB_WORKSPACE}/${patch}"',
            "go mod vendor",
            "go mod verify",
            "git -C substrate add --all",
            "git -C substrate write-tree",
            "${tree}:vendor",
            ".modules_patch.tree",
            "GOTOOLCHAIN=local",
            '--user "$(id -u):$(id -g)"',
            "/src/substrate",
            "substrate-trees.json",
            'tee -a "${GITHUB_STEP_SUMMARY}"',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, run)
        # The tree is recorded here and compared later: a difference must not
        # hide a digest. Nothing is committed, and the vendoring has no
        # GOFLAGS, as the measured scratch run had none.
        self.assertNotIn('[[ "${tree}"', run)
        self.assertNotIn("git -C substrate commit", run)
        self.assertNotIn("GOFLAGS", run)
        self.assertLess(
            run.index("No Substrate modules patch"), run.index("docker run")
        )

    def test_the_last_substrate_step_compares_the_tree_after_the_digests(self) -> None:
        last = self.workflow.steps("reproduce")[-1]
        self.assertEqual(last["name"], SUBSTRATE_GATE)
        self.assertEqual(last["if"], "always()")
        run = last["run"]
        table = run.index('echo "## Substrate images rebuilt from source"')
        digests = run.index("jq --exit-status")
        tree = run.index(".modules_patch.tree")
        comparison = run.index('[[ "${built}" == "${pinned}" ]]')
        self.assertLess(table, digests)
        self.assertLess(digests, tree)
        self.assertLess(tree, comparison)
        self.assertIn("substrate-trees.json", run)
        self.assertIn('exit "${status}"', run)

    def test_the_runner_has_its_own_toolbox_and_the_cli_the_new_one(self) -> None:
        names = self.workflow.names("reproduce-ax")
        runner = "Rebuild the task runner binary with its own toolbox"
        assert_order(
            self,
            names,
            "Record the unpatched test baseline",
            "Apply the vendored patch exactly as the manual lab staged it",
            runner,
            AX_MODULES,
            "Rebuild the ax CLI",
            KO,
            COMPARE,
            "Require no new test failure with the patch",
        )
        self.assertNotIn("Rebuild the ax CLI and the task runner binary", names)
        run = self.workflow.run("reproduce-ax", runner)
        self.assertIn('jq -r .runner_toolbox_image "${RUNNER_TEMP}/ax-plan.json"', run)
        self.assertNotIn(".toolbox_image", run.replace(".runner_toolbox_image", ""))
        self.assertIn("-o /out/ax-task-runner ./cmd/ax-task-runner", run)
        self.assertNotIn("-o /out/ax ./cmd/ax", run)
        self.assertIn("GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath", run)
        self.assertIn("--env GOTOOLCHAIN=local --env GOFLAGS=-mod=readonly", run)
        for name in (
            "Record the unpatched test baseline",
            "Rebuild the ax CLI",
            KO,
            "Require no new test failure with the patch",
        ):
            with self.subTest(step=name):
                other = self.workflow.run("reproduce-ax", name)
                self.assertIn(
                    'jq -r .toolbox_image "${RUNNER_TEMP}/ax-plan.json"', other
                )
                self.assertNotIn("runner_toolbox_image", other)
        cli = self.workflow.run("reproduce-ax", "Rebuild the ax CLI")
        self.assertIn('go build -trimpath -ldflags="-s -w" -o /out/ax ./cmd/ax', cli)
        self.assertNotIn("ax-task-runner", cli)

    def test_the_ax_modules_patch_goes_on_top_of_the_first_in_the_same_index(
        self,
    ) -> None:
        run = self.workflow.run("reproduce-ax", AX_MODULES)
        for fragment in (
            'if ! jq -e .modules_patch "${plan}" >/dev/null; then',
            'jq -er .modules_patch.path "${plan}"',
            'jq -er .modules_patch.sha256 "${plan}"',
            "No AX modules patch is configured.",
            "sha256sum --check --strict",
            "git -C ax apply --numstat",
            '[[ "${paths}" == "go.mod go.sum" ]]',
            'git -C ax apply --index "${GITHUB_WORKSPACE}/${modules}"',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, run)
        # Not pinned: the tree it writes is only printed.
        self.assertNotIn(".modules_patch.tree", run)
        self.assertNotIn('== "${pinned}"', run)

    def test_the_compare_step_prints_what_it_compares_before_it_compares(self) -> None:
        run = self.workflow.run("reproduce-ax", COMPARE)
        table = run.index('tee -a "${GITHUB_STEP_SUMMARY}"')
        for comparison in (
            '[[ "${cli}" == "${pinned_cli}" ]]',
            '[[ "${runner}" == "${pinned_runner}" ]]',
        ):
            self.assertLess(table, run.index(comparison))
        self.assertIn('jq -r .cli_sha256 "${plan}"', run)
        self.assertIn('jq -r .task_runner_binary_sha256 "${plan}"', run)
        self.assertIn('BASE_NAME = "org.opencontainers.image.base.name"', run)
        # The exact digest of the ko images is required only with the patch.
        self.assertIn('plan.get("modules_patch") and entry["digest"] != pin', run)


# --------------------------------------------------------------------------
# The shell of the steps


def placeholder(run: str) -> str:
    return re.sub(r"\$\{\{.*?\}\}", "EXPRESSION", run, flags=re.S)


class ShellSyntaxTests(unittest.TestCase):
    def test_every_run_block_of_both_workflows_parses_with_bash(self) -> None:
        checked = 0
        for path in (IMAGES, REPRODUCIBILITY):
            workflow = Workflow(path)
            for job in workflow.document["jobs"]:
                for step in workflow.steps(job):
                    if "run" not in step:
                        continue
                    with self.subTest(workflow=path.name, step=step["name"]):
                        completed = subprocess.run(
                            ["bash", "-n"],
                            input=placeholder(step["run"]),
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        self.assertEqual(completed.returncode, 0, completed.stderr)
                        checked += 1
        self.assertGreaterEqual(checked, 30)


@unittest.skipUnless(shutil.which("jq"), "jq is not installed")
class ShellBehaviourTests(unittest.TestCase):
    """The no-op branches and the gates, run on synthetic plans."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.images = Workflow(IMAGES)
        cls.reproducibility = Workflow(REPRODUCIBILITY)

    def run_step(
        self, workflow: Workflow, job: str, name: str, files: dict[str, Any]
    ) -> tuple[int, str, set[str]]:
        """Run a step with RUNNER_TEMP and the workspace in a fresh directory."""
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            for filename, content in files.items():
                (temp / filename).write_text(
                    content if isinstance(content, str) else json.dumps(content),
                    encoding="utf-8",
                )
            summary = temp / "summary.md"
            summary.write_text("", encoding="utf-8")
            completed = subprocess.run(
                ["bash", "-e", "-c", workflow.run(job, name)],
                cwd=directory,
                env={
                    "PATH": os.environ["PATH"],
                    "RUNNER_TEMP": directory,
                    "GITHUB_WORKSPACE": directory,
                    "GITHUB_STEP_SUMMARY": str(summary),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            output = completed.stdout + completed.stderr
            output += summary.read_text(encoding="utf-8")
            return completed.returncode, output, {p.name for p in temp.iterdir()}

    def test_without_a_modules_patch_both_modules_steps_do_nothing(self) -> None:
        cases = (
            (
                "substrate",
                SUBSTRATE_VENDOR,
                {"plan.json": {"toolbox_image": "x", "images": BUILT}},
                "No Substrate modules patch is configured.",
            ),
            (
                "ax",
                AX_MODULES,
                {"ax-plan.json": {"toolbox_image": "x", "patch": {"path": "p"}}},
                "No AX modules patch is configured.",
            ),
        )
        for workflow in (self.images, self.reproducibility):
            for images_job, name, files, message in cases:
                job = images_job if workflow is self.images else SIBLINGS[images_job]
                with self.subTest(workflow=workflow.path.name, step=name):
                    code, output, leftovers = self.run_step(workflow, job, name, files)
                    self.assertEqual(code, 0, output)
                    self.assertIn(message, output)
                    # Nothing recorded, nothing created: a no-op.
                    self.assertEqual(leftovers, {*files, "summary.md"})

    def gate(
        self, workflow: Workflow, job: str, plan: dict[str, Any], trees: Any
    ) -> tuple[int, str]:
        builds = "".join(
            json.dumps({"name": name, "digest": digest}) + "\n"
            for name, digest in BUILT.items()
        )
        files: dict[str, Any] = {"plan.json": plan, "builds.jsonl": builds}
        if trees is not None:
            files["substrate-trees.json"] = trees
        code, output, _ = self.run_step(workflow, job, SUBSTRATE_GATE, files)
        return code, output

    def test_the_gate_compares_the_tree_only_with_a_modules_patch(self) -> None:
        legacy = {"images": BUILT}
        relocked = {
            **legacy,
            "modules_patch": {"path": "p", "sha256": "c" * 64, "tree": "d" * 40},
        }
        stale = {"images": {**BUILT, "atenet": "sha256:" + "3" * 64}}
        for workflow, job in (
            (self.images, "substrate"),
            (self.reproducibility, "reproduce"),
        ):
            with self.subTest(workflow=workflow.path.name):
                self.assertEqual(self.gate(workflow, job, legacy, None)[0], 0)
                code, _ = self.gate(workflow, job, relocked, {"tree": "d" * 40})
                self.assertEqual(code, 0)
                code, output = self.gate(workflow, job, relocked, {"tree": "e" * 40})
                self.assertNotEqual(code, 0)
                # Pinned and built are both printed before it fails.
                self.assertIn("d" * 40, output)
                self.assertIn("e" * 40, output)
                # A patch without a pinned tree is a broken plan, not a skip.
                broken = {**legacy, "modules_patch": {"path": "p", "sha256": "c" * 64}}
                code, _ = self.gate(workflow, job, broken, {"tree": "d" * 40})
                self.assertNotEqual(code, 0)
                # A patch is configured and the vendor step recorded nothing.
                code, output = self.gate(workflow, job, relocked, None)
                self.assertNotEqual(code, 0)
                self.assertIn("built `none`", output)
                self.assertNotEqual(self.gate(workflow, job, stale, None)[0], 0)


if __name__ == "__main__":
    unittest.main()
