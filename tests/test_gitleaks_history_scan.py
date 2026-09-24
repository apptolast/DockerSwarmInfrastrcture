"""Tests for the fail-closed gitleaks history scan and non-paging git checks."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUARD = PROJECT_ROOT / "scripts/require-gitleaks-history-scan.sh"
FIXTURES = PROJECT_ROOT / "tests/fixtures"
# Both logs are real gitleaks v8.30.1 output from the pinned image: a scan of
# a regular clone with 165 commits, and the scan of a linked worktree whose
# common git directory was not mounted, which still exited 0.
CLEAN_LOG = FIXTURES / "gitleaks-history-clean.log"
UNMOUNTED_WORKTREE_LOG = FIXTURES / "gitleaks-history-unmounted-worktree.log"


def colored(level_color: str, level: str, message: str) -> str:
    return (
        f"\x1b[90m9:15AM\x1b[0m \x1b[{level_color}m{level}\x1b[0m "
        f"\x1b[1m{message}\x1b[0m\n"
    )


class GitleaksHistoryScanGuardTests(unittest.TestCase):
    def run_guard(self, log: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(GUARD), *args],
            input=log,
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_rejected(
        self,
        result: subprocess.CompletedProcess[str],
        message: str,
    ) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stderr)

    def test_a_complete_clean_scan_passes(self) -> None:
        result = self.run_guard(CLEAN_LOG.read_text(encoding="utf-8"), "165")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_the_unmounted_worktree_scan_that_exited_zero_is_rejected(
        self,
    ) -> None:
        log = UNMOUNTED_WORKTREE_LOG.read_text(encoding="utf-8")
        self.assertIn("0 commits scanned.", log)
        self.assertIn("no leaks found", log)
        self.assert_rejected(
            self.run_guard(log, "165"),
            "gitleaks logged a warning or error during the history scan",
        )

    def test_a_partial_scan_is_rejected_by_commit_count(self) -> None:
        self.assert_rejected(
            self.run_guard(CLEAN_LOG.read_text(encoding="utf-8"), "166"),
            "gitleaks scanned 165 commits; the repository has 166",
        )

    def test_warning_and_fatal_levels_are_rejected(self) -> None:
        clean = CLEAN_LOG.read_text(encoding="utf-8")
        for color, level in (("33", "WRN"), ("31", "FTL"), ("31", "PNC")):
            with self.subTest(level=level):
                self.assert_rejected(
                    self.run_guard(clean + colored(color, level, "x"), "165"),
                    "gitleaks logged a warning or error during the history scan",
                )

    def test_missing_or_repeated_counts_are_rejected(self) -> None:
        no_count = colored("32", "INF", "no leaks found")
        repeated = (
            colored("32", "INF", "165 commits scanned.")
            + colored("32", "INF", "165 commits scanned.")
            + no_count
        )
        for name, log in (("missing", no_count), ("repeated", repeated)):
            with self.subTest(case=name):
                self.assert_rejected(
                    self.run_guard(log, "165"),
                    "did not report exactly one commit count",
                )

    def test_a_scan_without_a_clean_result_is_rejected(self) -> None:
        log = colored("32", "INF", "165 commits scanned.")
        self.assert_rejected(
            self.run_guard(log, "165"),
            "gitleaks history scan did not report a clean result",
        )

    def test_pseudo_terminal_carriage_returns_are_tolerated(self) -> None:
        log = CLEAN_LOG.read_text(encoding="utf-8").replace("\n", "\r\n")
        result = self.run_guard(log, "165")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_expected_count_must_be_a_positive_integer(self) -> None:
        clean = CLEAN_LOG.read_text(encoding="utf-8")
        for argument in ("0", "-1", "165x", ""):
            with self.subTest(argument=argument):
                self.assert_rejected(
                    self.run_guard(clean, argument),
                    "expected commit count must be a positive integer",
                )
        self.assert_rejected(self.run_guard(clean), "usage:")


class ValidatorGitContractTests(unittest.TestCase):
    def test_lint_mounts_the_common_git_dir_and_checks_the_scan_log(
        self,
    ) -> None:
        lint = (PROJECT_ROOT / "scripts/lint.sh").read_text(encoding="utf-8")
        self.assertIn("--path-format=absolute --git-common-dir", lint)
        self.assertIn('--volume "${git_common_dir}:${git_common_dir}:ro"', lint)
        self.assertIn('rev-list --all --count', lint)
        self.assertIn('require-gitleaks-history-scan.sh" "${expected_commits}"', lint)

    def test_validators_never_page_git_output(self) -> None:
        for relative, commands in (
            (
                "scripts/lint.sh",
                (
                    "git --no-pager diff --check",
                    "git --no-pager diff --cached --check",
                ),
            ),
            ("scripts/validate-iac.sh", ("git --no-pager diff --check",)),
        ):
            content = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            for command in commands:
                self.assertIn(command, content, relative)
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("git diff"):
                    self.fail(f"{relative} pages git output: {stripped}")


if __name__ == "__main__":
    unittest.main()
