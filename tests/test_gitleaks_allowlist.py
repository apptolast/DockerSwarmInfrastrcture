"""Tests for the gitleaks allowlist of the go.sum lines in the modules patches.

scripts/lint.sh scans every ref of the repository (`--log-opts=--all`) with the
.gitleaks.toml of the branch under test, so the allowlist has to be on main
before a branch that carries the patches exists. It must stay narrow: only the
generic-api-key rule, only the two patch paths, only lines shaped like go.sum
(or a hunk header that ends with one). gitleaks runs Go's RE2, which reads
these expressions the way Python's `re` does.
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESCRIPTION_START = "go.sum checksum lines inside the go.mod and go.sum module patches"
HASH = "A" * 43 + "="
# Key-looking values, assembled at run time so that this file does not itself
# trip the scan it tests.
FAKE_SECRET = bytes.fromhex(
    "774a616c725855746e46454d492f4b374d44454e472f62507852666943597a46334b396142326344"
).decode()


def allowlist() -> dict:
    document = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    matching = [
        entry
        for entry in document["allowlists"]
        if entry.get("description", "").startswith(DESCRIPTION_START)
    ]
    assert len(matching) == 1, matching
    return matching[0]


class ModulesPatchAllowlistTests(unittest.TestCase):
    def test_it_covers_one_rule_and_requires_path_and_line_together(self) -> None:
        entry = allowlist()
        self.assertEqual(entry["targetRules"], ["generic-api-key"])
        self.assertEqual(entry["condition"], "AND")
        self.assertEqual(entry["regexTarget"], "line")
        self.assertEqual(len(entry["paths"]), 1)
        self.assertEqual(len(entry["regexes"]), 1)

    def test_the_path_is_only_the_two_modules_patches(self) -> None:
        path = re.compile(allowlist()["paths"][0])
        for accepted in (
            "images/ax/google-ax-modules.patch",
            "images/ax/substrate-modules.patch",
            "/repo/images/ax/google-ax-modules.patch",
            "/repo/images/ax/substrate-modules.patch",
        ):
            with self.subTest(accepted=accepted):
                self.assertIsNotNone(path.search(accepted))
        for rejected in (
            "images/ax/google-ax-375.patch",
            "images/ax/substrate-modules.patch.orig",
            "images/ax/other-modules.patch",
            "docs/substrate-modules.patch",
            "docs/AX.md",
            "tests/images/ax/substrate-modules.patch",
            "/home/images/ax/substrate-modules.patch",
        ):
            with self.subTest(rejected=rejected):
                self.assertIsNone(path.search(rejected))

    def test_the_line_is_only_a_go_sum_checksum(self) -> None:
        line = re.compile(allowlist()["regexes"][0])
        for accepted in (
            f" golang.org/x/oauth2 v0.36.0 h1:{HASH}",
            f"+golang.org/x/net v0.60.0 h1:{HASH}",
            f"-golang.org/x/net v0.58.0/go.mod h1:{HASH}",
            f"+go.uber.org/atomic v1.11.0 h1:{HASH}",
            f"+golang.org/x/net v0.60.0 h1:{HASH}\r",
            f"@@ -482,10 +482,10 @@ google.golang.org/api v0.274.0 h1:{HASH}",
        ):
            with self.subTest(accepted=accepted):
                self.assertIsNotNone(line.search(accepted))
        for rejected in (
            f'+{"to" + "ken"} = "{FAKE_SECRET}"',
            f'+{"aws_secret" + "_access_key"} = "{FAKE_SECRET}"',
            f"+golang.org/x/net v0.60.0 h1:{HASH} extra",
            f"+api_key v0.60.0 h1:{HASH} # commented",
            f"golang.org/x/net v0.60.0 h1:{HASH}",
            "+golang.org/x/net v0.60.0 h1:" + "A" * 42 + "=",
            "+golang.org/x/net v0.60.0 h1:" + "A" * 44 + "=",
            f"+golang.org/x/net notaversion h1:{HASH}",
        ):
            with self.subTest(rejected=rejected):
                self.assertIsNone(line.search(rejected))


if __name__ == "__main__":
    unittest.main()
