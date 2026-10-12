"""Contract of the relock plumbing of the AX lab validator.

The optional fields scripts/validate-ax-lab.py accepts once the lab is
relocked: images.runner_toolbox and the go.mod/go.sum-only modules patch of
each source, the rules that tie them together with the toolbox and the
fallback builds, and the plans that carry them. The validator reads its
inputs relative to the repository, which a test never re-points, so the
relocked state is exercised on mocked documents and patch bytes served from
memory; the plain config is only ever validated as it stands.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from test_ax_lab_ax_contract import document
from test_ax_lab_contract import ROOT, load_script

VALIDATOR = load_script("validate_ax_lab_relock", "scripts/validate-ax-lab.py")
AX_MODULES_PATCH = "images/ax/google-ax-modules.patch"
SUBSTRATE_MODULES_PATCH = "images/ax/substrate-modules.patch"
# Made up: the patches are served from memory under these hashes.
AX_SHA256 = "c" * 64
SUBSTRATE_SHA256 = "d" * 64
TREE = "a" * 40
GOLANG_1_27_1 = "docker.io/library/golang:1.27.1@sha256:" + "1" * 64
INDEX = "index " + "1" * 40 + ".." + "2" * 40 + " 100644"
# The shape of a `git diff --cached --full-index` of go.mod and go.sum after
# `go get` and `go mod tidy`, with neutral module names.
GOMOD = [
    "diff --git a/go.mod b/go.mod",
    INDEX,
    "--- a/go.mod",
    "+++ b/go.mod",
    "@@ -3,5 +3,5 @@ go 1.27.0",
    " ",
    " require (",
    "-\texample.com/a v1.0.0 // indirect",
    "+\texample.com/a v1.1.0 // indirect",
    " \texample.com/b v1.0.0 // indirect",
    " )",
]
GOSUM = [
    "diff --git a/go.sum b/go.sum",
    INDEX,
    "--- a/go.sum",
    "+++ b/go.sum",
    "@@ -1,4 +1,4 @@",
    " example.com/b v1.0.0 h1:" + "A" * 43 + "=",
    "-example.com/a v1.0.0 h1:" + "B" * 43 + "=",
    "-example.com/a v1.0.0/go.mod h1:" + "C" * 43 + "=",
    "+example.com/a v1.1.0 h1:" + "D" * 43 + "=",
    "+example.com/a v1.1.0/go.mod h1:" + "E" * 43 + "=",
    " example.com/c v1.0.0 h1:" + "A" * 43 + "=",
]


# What `git apply` also takes: no `diff --git` line, a plain pair of headers.
PLAIN_DIFF = [
    "--- a/main.go",
    "+++ b/main.go",
    "@@ -1,3 +1,3 @@",
    " package main",
    " ",
    "-x",
    "+y",
]


def patch_bytes(*blocks: list[str]) -> bytes:
    return ("\n".join(line for block in blocks for line in block) + "\n").encode()


def with_lines(block: list[str], lines: list[str], at: int = 1) -> list[str]:
    return [*block[:at], *lines, *block[at:]]


def with_line(block: list[str], line: str, at: int = 1) -> list[str]:
    return with_lines(block, [line], at)


def counted(block: list[str]) -> list[str]:
    """The block with its hunk header counting the lines the hunk now holds."""
    body = [line for line in block[5:] if line[:1] in (" ", "+", "-")]
    old = sum(not line.startswith("+") for line in body)
    new = sum(not line.startswith("-") for line in body)
    header = re.sub(
        r"^@@ -(\d+),\d+ \+(\d+),\d+ @@",
        rf"@@ -\g<1>,{old} +\g<2>,{new} @@",
        block[4],
    )
    return [*block[:4], header, *block[5:]]


GOMOD_PATCH = patch_bytes(GOMOD, GOSUM)


@contextlib.contextmanager
def patched_files(
    texts: dict[str, bytes] | None = None, hashes: dict[str, str] | None = None
) -> Iterator[None]:
    """Serve the two modules patches the validator reads, from memory.

    Every other path goes to the real function, captured before the patch.
    """
    real_hash = VALIDATOR.file_sha256
    real_text = VALIDATOR.read_patch_text
    served_hashes = {
        AX_MODULES_PATCH: AX_SHA256,
        SUBSTRATE_MODULES_PATCH: SUBSTRATE_SHA256,
        **(hashes or {}),
    }
    served_texts = {
        AX_MODULES_PATCH: GOMOD_PATCH,
        SUBSTRATE_MODULES_PATCH: GOMOD_PATCH,
        **(texts or {}),
    }

    def file_sha256(path: str) -> str:
        return served_hashes[path] if path in served_hashes else real_hash(path)

    def read_patch_text(path: str) -> bytes:
        return served_texts[path] if path in served_texts else real_text(path)

    with (
        mock.patch.object(VALIDATOR, "file_sha256", side_effect=file_sha256),
        mock.patch.object(VALIDATOR, "read_patch_text", side_effect=read_patch_text),
    ):
        yield


def ax_entry(**changes: Any) -> dict[str, Any]:
    return {"path": AX_MODULES_PATCH, "sha256": AX_SHA256, **changes}


def substrate_entry(**changes: Any) -> dict[str, Any]:
    return {
        "path": SUBSTRATE_MODULES_PATCH,
        "sha256": SUBSTRATE_SHA256,
        "tree": TREE,
        **changes,
    }


def legacy_document() -> dict[str, Any]:
    """The config before the relock, whatever the file says today."""
    mutated = document()
    lab = mutated["ax_lab"]
    lab["images"].pop("runner_toolbox", None)
    lab["images"]["toolbox"] = GOLANG_1_27_1
    for source in lab["sources"].values():
        source.pop("modules_patch", None)
    return mutated


def relocked_document() -> dict[str, Any]:
    """The relocked state: Go 1.27.2 for the toolbox, 1.27.1 for the runner."""
    mutated = legacy_document()
    lab = mutated["ax_lab"]
    lab["images"]["toolbox"] = lab["images"]["web_toolbox"]
    lab["images"]["runner_toolbox"] = GOLANG_1_27_1
    lab["sources"]["ax"]["modules_patch"] = ax_entry()
    lab["sources"]["substrate"]["modules_patch"] = substrate_entry()
    return mutated


def combination(ax: bool, substrate: bool, runner: bool) -> dict[str, Any]:
    """The legacy config with some of the three relock fields set."""
    mutated = legacy_document()
    lab = mutated["ax_lab"]
    if runner:
        lab["images"]["runner_toolbox"] = GOLANG_1_27_1
    if ax:
        lab["sources"]["ax"]["modules_patch"] = ax_entry()
    if substrate:
        lab["sources"]["substrate"]["modules_patch"] = substrate_entry()
    if ax and substrate and runner:
        lab["images"]["toolbox"] = lab["images"]["web_toolbox"]
    return mutated


class RelockCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.reserved = VALIDATOR.reserved_sysctl_keys()

    def validated(self, mutated: dict[str, Any], **served: Any) -> dict[str, Any]:
        with patched_files(**served):
            return VALIDATOR.validate_catalog(copy.deepcopy(mutated), self.reserved)

    def rejected(self, mutated: dict[str, Any], message: str, **served: Any) -> None:
        with (
            patched_files(**served),
            self.assertRaisesRegex(VALIDATOR.AxLabError, message),
        ):
            VALIDATOR.validate_catalog(copy.deepcopy(mutated), self.reserved)

    def sources_rejected(
        self, sources: dict[str, Any], message: str, **served: Any
    ) -> None:
        with (
            patched_files(**served),
            self.assertRaisesRegex(VALIDATOR.AxLabError, message),
        ):
            VALIDATOR.validate_sources(sources)


class KeysBetweenTests(unittest.TestCase):
    def test_required_keys_may_come_with_or_without_the_optional_ones(self) -> None:
        for value in ({"a": 1}, {"a": 1, "b": 2}, {"a": 1, "b": 2, "c": 3}):
            with self.subTest(keys=sorted(value)):
                self.assertIs(
                    VALIDATOR.keys_between(value, {"a"}, {"b", "c"}, "ctx"), value
                )

    def test_a_missing_required_key_or_an_unknown_one_is_refused(self) -> None:
        for value in (
            {},
            {"b": 2},
            {"a": 1, "d": 4},
            {"a": 1, "b": 2, "d": 4},
            {"b": 2, "c": 3},
        ):
            with self.subTest(keys=sorted(value)):
                with self.assertRaisesRegex(VALIDATOR.AxLabError, "ctx: unexpected"):
                    VALIDATOR.keys_between(value, {"a"}, {"b", "c"}, "ctx")

    def test_a_value_that_is_not_a_mapping_is_refused(self) -> None:
        for value in (None, "a", ["a"], 1, {"a"}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(VALIDATOR.AxLabError, "ctx: unexpected"):
                    VALIDATOR.keys_between(value, {"a"}, {"b"}, "ctx")

    def test_the_message_is_the_one_of_exact_keys(self) -> None:
        for value in ({}, {"a": 1, "d": 4}, None):
            with self.subTest(value=value):
                with self.assertRaises(VALIDATOR.AxLabError) as exact:
                    VALIDATOR.exact_keys(value, {"a"}, "ctx")
                with self.assertRaises(VALIDATOR.AxLabError) as between:
                    VALIDATOR.keys_between(value, {"a"}, set(), "ctx")
                self.assertEqual(str(between.exception), str(exact.exception))
                self.assertEqual(
                    str(between.exception), "ctx: unexpected or missing keys"
                )

    def test_without_optional_keys_it_is_exact_keys(self) -> None:
        VALIDATOR.keys_between({"a": 1}, {"a"}, set(), "ctx")
        with self.assertRaisesRegex(VALIDATOR.AxLabError, "ctx: unexpected"):
            VALIDATOR.keys_between({"a": 1, "b": 2}, {"a"}, set(), "ctx")


class RunnerToolboxTests(RelockCase):
    def images(self, **changes: Any) -> dict[str, Any]:
        images = legacy_document()["ax_lab"]["images"]
        images.update(changes)
        return images

    def test_the_key_is_optional(self) -> None:
        references = VALIDATOR.validate_images(self.images())
        self.assertNotIn("runner_toolbox", references)
        references = VALIDATOR.validate_images(
            self.images(runner_toolbox=GOLANG_1_27_1)
        )
        self.assertEqual(references["runner_toolbox"]["tag"], "1.27.1")
        self.assertEqual(references["toolbox"]["tag"], "1.27.1")

    def test_it_may_equal_the_toolbox(self) -> None:
        images = self.images()
        images["runner_toolbox"] = images["toolbox"]
        VALIDATOR.validate_images(images)

    def test_it_is_a_pinned_golang_image_like_the_others(self) -> None:
        for label, value, message in (
            (
                "other repository",
                "docker.io/library/debian:1.27.1@sha256:" + "a" * 64,
                "image runner_toolbox must use docker.io/library/golang",
            ),
            (
                "moving tag",
                "docker.io/library/golang:latest@sha256:" + "a" * 64,
                "image runner_toolbox must not use the moving latest tag",
            ),
            (
                "no digest",
                "docker.io/library/golang:1.27.1",
                "image runner_toolbox must be repository:tag@sha256",
            ),
            (
                "short digest",
                "docker.io/library/golang:1.27.1@sha256:" + "a" * 63,
                "image runner_toolbox must be repository:tag@sha256",
            ),
            (
                "uppercase digest",
                "docker.io/library/golang:1.27.1@sha256:" + "A" * 64,
                "image runner_toolbox must be repository:tag@sha256",
            ),
            ("none", None, "image runner_toolbox must be repository:tag@sha256"),
            ("integer", 127, "image runner_toolbox must be repository:tag@sha256"),
        ):
            with self.subTest(case=label):
                with self.assertRaisesRegex(VALIDATOR.AxLabError, message):
                    VALIDATOR.validate_images(self.images(runner_toolbox=value))

    def test_an_unknown_key_or_a_missing_mandatory_one_is_still_refused(self) -> None:
        for images in (
            self.images(extra=GOLANG_1_27_1),
            self.images(runner_toolbox=GOLANG_1_27_1, extra=GOLANG_1_27_1),
        ):
            with self.subTest(keys=sorted(images)):
                with self.assertRaisesRegex(VALIDATOR.AxLabError, "images: unexpected"):
                    VALIDATOR.validate_images(images)
        for name in ("kind_node", "registry", "redis", "toolbox", "web_toolbox"):
            images = self.images(runner_toolbox=GOLANG_1_27_1)
            del images[name]
            with self.subTest(missing=name):
                with self.assertRaisesRegex(VALIDATOR.AxLabError, "images: unexpected"):
                    VALIDATOR.validate_images(images)

    def test_the_runner_toolbox_alone_is_legal_without_the_patches(self) -> None:
        mutated = legacy_document()
        mutated["ax_lab"]["images"]["runner_toolbox"] = GOLANG_1_27_1
        self.validated(mutated)


class GomodPatchTests(RelockCase):
    def accepted(self, data: bytes) -> None:
        VALIDATOR.validate_gomod_patch_text(data, "patch")

    def refused(self, data: bytes, message: str) -> None:
        with self.assertRaisesRegex(VALIDATOR.AxLabError, message):
            VALIDATOR.validate_gomod_patch_text(data, "patch")

    def test_a_go_mod_then_go_sum_patch_is_accepted(self) -> None:
        self.accepted(GOMOD_PATCH)

    def test_the_no_newline_marker_and_a_missing_final_newline_are_accepted(
        self,
    ) -> None:
        marker = with_line(GOSUM, "\\ No newline at end of file", len(GOSUM))
        self.accepted(patch_bytes(GOMOD, marker))
        self.accepted(GOMOD_PATCH.removesuffix(b"\n"))

    def test_only_go_mod_then_go_sum_may_be_patched(self) -> None:
        def renamed(block: list[str], name: str) -> list[str]:
            return [
                line.replace("go.mod", name).replace("go.sum", name) for line in block
            ]

        for label, data in (
            ("main.go", patch_bytes(renamed(GOMOD, "main.go"), GOSUM)),
            (
                "vendor/modules.txt",
                patch_bytes(GOMOD, renamed(GOSUM, "vendor/modules.txt")),
            ),
            ("go.work", patch_bytes(renamed(GOMOD, "go.work"), GOSUM)),
            ("go.mod only", patch_bytes(GOMOD)),
            ("go.sum only", patch_bytes(GOSUM)),
            ("reversed", patch_bytes(GOSUM, GOMOD)),
            ("duplicate", patch_bytes(GOMOD, GOMOD, GOSUM)),
            ("a third file", patch_bytes(GOMOD, GOSUM, renamed(GOMOD, "go.work"))),
        ):
            with self.subTest(case=label):
                self.refused(data, "must patch go.mod and then go.sum, only")

    def test_nothing_may_come_before_the_first_git_header(self) -> None:
        # `git apply` reads a plain `---`/`+++` pair with no `diff --git`
        # line before it as the patch of another file.
        for label, data in (
            ("a plain diff", patch_bytes(PLAIN_DIFF, GOMOD, GOSUM)),
            ("no header line", patch_bytes(GOMOD[1:], GOSUM[1:])),
            ("a context line", patch_bytes([" context"], GOMOD, GOSUM)),
            ("a blank line", patch_bytes([""], GOMOD, GOSUM)),
            ("only a newline", b"\n"),
        ):
            with self.subTest(case=label):
                self.refused(data, "must start with a git file header")

    def test_every_file_header_names_the_file_of_its_section(self) -> None:
        def named(block: list[str], old: str, new: str) -> list[str]:
            return [*block[:2], old, new, *block[4:]]

        for label, data in (
            (
                "a created file",
                patch_bytes(named(GOMOD, "--- /dev/null", "+++ b/go.mod"), GOSUM),
            ),
            (
                "a deleted file",
                patch_bytes(named(GOMOD, "--- a/go.mod", "+++ /dev/null"), GOSUM),
            ),
            (
                "another old name",
                patch_bytes(named(GOMOD, "--- a/main.go", "+++ b/go.mod"), GOSUM),
            ),
            (
                "another new name",
                patch_bytes(named(GOMOD, "--- a/go.mod", "+++ b/main.go"), GOSUM),
            ),
            (
                "the name of the other section",
                patch_bytes(named(GOMOD, "--- a/go.sum", "+++ b/go.sum"), GOSUM),
            ),
            (
                "the old name for the new one",
                patch_bytes(named(GOMOD, "--- a/go.mod", "+++ a/go.mod"), GOSUM),
            ),
            (
                "the new name for the old one",
                patch_bytes(GOMOD, named(GOSUM, "--- b/go.sum", "+++ b/go.sum")),
            ),
        ):
            with self.subTest(case=label):
                self.refused(data, "names a file its header does not")

    def test_no_second_file_header_follows_the_first_one(self) -> None:
        # Not another file, not the section's own pair again: git reads a
        # repeated pair followed by a hunk as a second patch of the same file.
        own = GOMOD[2:4]
        for label, data in (
            (
                "a plain diff between the sections",
                patch_bytes(GOMOD, PLAIN_DIFF, GOSUM),
            ),
            ("a plain diff after the last", patch_bytes(GOMOD, GOSUM, PLAIN_DIFF)),
            (
                "a second pair of another file",
                patch_bytes(with_line(GOMOD, "--- a/main.go", 9), GOSUM),
            ),
            (
                "a second pair of its own in the hunk",
                patch_bytes(with_lines(GOMOD, own, 9), GOSUM),
            ),
            (
                "its own pair and hunk again after the hunk",
                patch_bytes([*GOMOD, *GOMOD[2:]], GOSUM),
            ),
            ("a second index line", patch_bytes(with_line(GOMOD, INDEX, 9), GOSUM)),
            (
                "its own old header alone",
                patch_bytes(GOMOD, counted(with_line(GOSUM, "--- a/go.sum", 10))),
            ),
            (
                "its own new header alone",
                patch_bytes(GOMOD, counted(with_line(GOSUM, "+++ b/go.sum", 10))),
            ),
        ):
            with self.subTest(case=label):
                self.refused(data, "carries a second file header")

    def test_no_rename_copy_mode_creation_deletion_or_binary_marker(self) -> None:
        for marker in (
            "rename from go.mod",
            "rename to go.mod",
            "copy from go.mod",
            "copy to go.mod",
            "similarity index 100%",
            "dissimilarity index 5%",
            "new file mode 100644",
            "deleted file mode 100644",
            "old mode 100644",
            "new mode 100755",
            "GIT binary patch",
            "Binary files a/go.sum and b/go.sum differ",
        ):
            with self.subTest(marker=marker):
                self.refused(
                    patch_bytes(with_line(GOMOD, marker), GOSUM), "not a plain go.mod"
                )
                self.refused(
                    patch_bytes(GOMOD, with_line(GOSUM, marker, 2)),
                    "not a plain go.mod",
                )

    def test_a_blank_or_unmarked_line_is_refused(self) -> None:
        for line in ("", "go 1.27.0", "From: someone", "\tnot a diff line"):
            with self.subTest(line=line):
                self.refused(
                    patch_bytes(with_line(GOMOD, line, 5), GOSUM), "not a plain go.mod"
                )

    def test_the_size_is_bounded(self) -> None:
        def padded(width: int, rows: int = 1) -> bytes:
            filler = [" " + "x" * width] * rows
            return patch_bytes(counted(with_lines(GOMOD, filler, 5)), GOSUM)

        self.refused(b"", "must hold 1 to 65536 bytes")
        self.refused(padded(99, 700), "must hold 1 to 65536 bytes")
        # The limit itself still passes.
        room = VALIDATOR.GOMOD_PATCH_MAX_BYTES - len(GOMOD_PATCH)
        self.accepted(padded(room - 2))
        self.refused(padded(room - 1), "must hold 1 to 65536 bytes")

    def test_the_text_is_strict_utf8(self) -> None:
        self.refused(GOMOD_PATCH + b"\xff\xfe", "must be UTF-8 text")
        self.refused(
            GOMOD_PATCH.replace(b"example.com/c", b"example.com/\xc3"), "UTF-8"
        )

    def test_no_carriage_return_line_separator_or_byte_order_mark_is_accepted(
        self,
    ) -> None:
        # Git cuts the patch into lines at \n alone. Whatever else a text tool
        # may cut at stays out, whether it sits in a header, in a hunk line or
        # before the patch, so that the validator and git read the same lines.
        message = "must hold no carriage return, line separator or byte order mark"
        self.refused(GOMOD_PATCH.replace(b"\n", b"\r\n"), message)
        self.refused(b"\xef\xbb\xbf" + GOMOD_PATCH, message)
        for char in (
            "\r",
            "\v",
            "\f",
            "\x1c",
            "\x1d",
            "\x1e",
            "\x85",
            "\u2028",
            "\u2029",
            "\ufeff",
        ):
            added = with_line(GOMOD, "+\texample.com/z v1.0.0" + char + "// z", 7)
            for label, data in (
                ("before the patch", char.encode() + GOMOD_PATCH),
                (
                    "for the line feed between two headers",
                    GOMOD_PATCH.replace(
                        b"--- a/go.mod\n+++", f"--- a/go.mod{char}+++".encode()
                    ),
                ),
                ("in the middle of an added line", patch_bytes(counted(added), GOSUM)),
                (
                    "at the end of a context line",
                    GOMOD_PATCH.replace(b" )\n", f" ){char}\n".encode(), 1),
                ),
            ):
                with self.subTest(char=repr(char), where=label):
                    self.refused(data, message)
        # A tab is the one blank a go.mod line is indented with.
        added = with_line(GOMOD, "+\texample.com/z\tv1.0.0\t// z", 7)
        self.accepted(patch_bytes(counted(added), GOSUM))

    def test_a_stray_line_between_the_headers_is_refused(self) -> None:
        shape = re.escape("a file is its diff --git, index, --- and +++ lines, then")
        for place, at in (
            ("diff and index", 1),
            ("index and ---", 2),
            ("--- and +++", 3),
        ):
            for stray in (
                "+x",
                "-x",
                " x",
                "@@ -1 +1 @@",
                "\\ No newline at end of file",
                INDEX,
            ):
                with self.subTest(place=place, stray=stray):
                    self.refused(patch_bytes(with_line(GOMOD, stray, at), GOSUM), shape)
                    self.refused(patch_bytes(GOMOD, with_line(GOSUM, stray, at)), shape)
        for label, block, at in (
            ("no index line", GOMOD, 1),
            ("no --- line", GOMOD, 2),
            ("no +++ line", GOSUM, 3),
        ):
            gone = [line for number, line in enumerate(block) if number != at]
            with self.subTest(case=label):
                self.refused(
                    patch_bytes(gone, GOSUM)
                    if block is GOMOD
                    else patch_bytes(GOMOD, gone),
                    shape,
                )

    def test_the_index_line_is_git_s_for_a_file_whose_mode_did_not_change(self) -> None:
        shape = re.escape("a file is its diff --git, index, --- and +++ lines, then")
        abbreviated = "index 1234567..89abcde 100644"
        self.accepted(
            patch_bytes(with_line(GOMOD[:1], abbreviated, 1), GOMOD[2:], GOSUM)
        )
        for index in (
            "index 123456..789abcd 100644",
            "index " + "1" * 65 + ".." + "2" * 40 + " 100644",
            "index " + "A" * 40 + ".." + "2" * 40 + " 100644",
            "index " + "1" * 40 + ".." + "2" * 40,
            "index " + "1" * 40 + ".." + "2" * 40 + " 100755",
            "index " + "1" * 40 + ".." + "2" * 40 + " 120000",
            "index " + "1" * 40 + "..." + "2" * 40 + " 100644",
            "index " + "1" * 40 + ".." + "2" * 40 + " 100644 ",
            "index ",
        ):
            with self.subTest(index=index):
                self.refused(patch_bytes([GOMOD[0], index, *GOMOD[2:]], GOSUM), shape)

    def test_a_hunk_holds_exactly_the_lines_its_header_counts(self) -> None:
        more = "more lines than its hunk header counts"
        fewer = "fewer lines than its hunk header counts"
        later = [
            "@@ -9,2 +9,2 @@",
            " example.com/d v1.0.0 h1:x=",
            "-example.com/e h1:y=",
            "+example.com/e h1:z=",
        ]
        two = [*GOSUM, *later]
        self.accepted(patch_bytes(GOMOD, two))

        def counting(counts: str, block: list[str] = GOSUM) -> list[str]:
            return [*block[:4], f"@@ {counts} @@", *block[5:]]

        for label, block, message in (
            ("a line before the first hunk", with_line(GOSUM, "+x", 4), more),
            ("a removed line before the first hunk", with_line(GOSUM, "-x", 4), more),
            ("a context line before the first hunk", with_line(GOSUM, " x", 4), more),
            ("a line in the middle of a hunk", with_line(GOSUM, "+x", 7), more),
            ("a line between two hunks", with_line(two, "+x", len(GOSUM)), more),
            ("a line after the last hunk", [*two, "+x"], more),
            ("a context line after the last hunk", [*two, " x"], more),
            (
                "a line missing from the hunk",
                [line for line in GOSUM if "h1:" + "D" * 43 not in line],
                fewer,
            ),
            ("a header counting more old lines", counting("-1,5 +1,4"), fewer),
            ("a header counting more new lines", counting("-1,4 +1,5"), fewer),
            ("a header counting fewer old lines", counting("-1,3 +1,4"), more),
            ("a header counting fewer new lines", counting("-1,4 +1,3"), more),
            (
                "a hunk begun inside a hunk",
                with_line(GOSUM, "@@ -9,1 +9,1 @@", 8),
                fewer,
            ),
            ("a line missing from the last hunk", two[:-1], fewer),
            ("a file without a hunk", GOSUM[:4], "a file without a hunk"),
        ):
            with self.subTest(case=label):
                self.refused(patch_bytes(GOMOD, block), message)

    def test_a_hunk_header_may_leave_out_a_count_of_one_or_name_a_function(
        self,
    ) -> None:
        old, new = GOSUM[6], GOSUM[8]
        for counts in ("-7 +7", "-7,1 +7,1", "-7,1 +7", "-7 +7,1"):
            with self.subTest(counts=counts):
                self.accepted(
                    patch_bytes(GOMOD, [*GOSUM[:4], f"@@ {counts} @@", old, new])
                )
        # No old line at all: a pure addition.
        added = [
            *GOSUM[:4],
            "@@ -6,0 +7,2 @@",
            "+example.com/f h1:x=",
            "+example.com/g h1:y=",
        ]
        self.accepted(patch_bytes(GOMOD, added))
        for context in ("", " require (", " go.uber.org/atomic v1.11.0 h1:x="):
            with self.subTest(context=context):
                header = [*GOSUM[:4], "@@ -1,4 +1,4 @@" + context, *GOSUM[5:]]
                self.accepted(patch_bytes(GOMOD, header))
        for header in (
            "@@ -1,4 +1,4",
            "@@ -1,4 +1,4 @",
            "@@ -1,4 +1,4 @@x",
            "@@ -1,4 +1,4 @@@",
            "@@@ -1,4 +1,4 @@@",
            "@@ -a +b @@",
            "@@ -1,4 @@",
            "@@ +1,4 -1,4 @@",
            "@@ -1,4 +1,4",
            "@ -1,4 +1,4 @@",
            "@",
        ):
            with self.subTest(header=header):
                self.refused(
                    patch_bytes(GOMOD, [*GOSUM[:4], header, *GOSUM[5:]]),
                    "not a hunk header",
                )

    def test_a_no_newline_marker_follows_a_line_of_a_hunk(self) -> None:
        marker = "\\ No newline at end of file"
        needs = "needs a line before it"
        for label, block in (
            ("right after the file header", with_line(GOSUM, marker, 4)),
            ("right after the hunk header", with_line(GOSUM, marker, 5)),
            ("after another marker", [*GOSUM, marker, marker]),
            (
                "after the hunk header of a later hunk",
                [*GOSUM, "@@ -9,1 +9,1 @@", marker],
            ),
        ):
            with self.subTest(case=label):
                self.refused(patch_bytes(GOMOD, block), needs)
        # After the old line and after the new line of a hunk.
        both = [*GOSUM[:4], "@@ -7 +7 @@", GOSUM[6], marker, GOSUM[8], marker]
        self.accepted(patch_bytes(GOMOD, both))

    def test_an_added_directive_that_changes_the_build_is_refused(self) -> None:
        message = (
            "adds a replace, exclude, go, toolchain, godebug, tool, ignore or retract"
        )
        for added in (
            "replace example.com/a => ../a",
            "replace (",
            "replace(",
            "replace",
            "\treplace example.com/a v1.0.0 => example.com/b v1.0.0",
            "exclude example.com/a v1.0.0",
            "\texclude example.com/a v1.0.0",
            "go 1.20",
            "go\t1.20",
            "go",
            "go(",
            "\tgo 1.27.2",
            "toolchain go1.99.0",
            "toolchain",
            "godebug default=go1.20",
            "godebug (",
            "tool example.com/x/cmd/y",
            "tool (",
            "ignore ./vendor",
            "ignore (",
            "retract v1.0.0",
            "retract [v1.0.0, v1.1.0]",
            "retract (",
            # A blank of Unicode in front, which Go does not skip but git takes.
            "\u00a0go 1.20",
            "\u2003exclude example.com/a v1.0.0",
            # Inside an existing replace block the keyword is not repeated.
            "\texample.com/a v1.0.0 => example.com/b v1.0.0",
            "\texample.com/a => ../a",
        ):
            with self.subTest(added=added):
                self.refused(
                    patch_bytes(counted(with_line(GOMOD, "+" + added, 7)), GOSUM),
                    message,
                )
        with self.subTest(file="go.sum"):
            self.refused(
                patch_bytes(GOMOD, counted(with_line(GOSUM, "+go 1.20", 5))), message
            )
        hash_ = " h1:" + "F" * 43 + "="
        for neutral in (
            "-replace example.com/a => ../a",
            "-\texample.com/a => ../a",
            "-exclude example.com/a v1.0.0",
            "-go 1.20",
            "-toolchain go1.99.0",
            " replace example.com/a => ../a",
            " go 1.27.0",
            "+\treplace-me.example/x v1.0.0 // indirect",
            "+\texample.com/replace v1.0.0 // indirect",
            "+\texample.com/exclude v1.0.0 // indirect",
            # A module path that starts with a directive's name, or holds it.
            "+\tgo.uber.org/atomic v1.11.0 // indirect",
            "+\tgolang.org/x/mod v0.1.0 // indirect",
            "+\tgo-yaml.example/x v1.0.0",
            "+\ttoolchain.example/x v1.0.0",
            "+\tgodebug.example/x v1.0.0 // indirect",
            "+\ttool.example/x v1.0.0",
            "+\tignore.example/x v1.0.0",
            "+\tretract.example/x v1.0.0",
            "+\texample.com/go v1.0.0",
            "+\texample.com/tool v1.0.0 // indirect",
            # Only the first word of the line is a directive.
            "+\texample.com/a v1.1.0 // go 1.20, replace and exclude",
        ):
            with self.subTest(neutral=neutral):
                self.accepted(patch_bytes(counted(with_line(GOMOD, neutral, 7)), GOSUM))
        for neutral in (
            "+go.uber.org/atomic v1.11.0" + hash_,
            "+go.uber.org/atomic v1.11.0/go.mod" + hash_,
            "+golang.org/x/mod v0.1.0" + hash_,
            "+toolchain.example/x v1.0.0" + hash_,
            "+tool.example/x v1.0.0/go.mod" + hash_,
        ):
            with self.subTest(neutral=neutral):
                self.accepted(patch_bytes(GOMOD, counted(with_line(GOSUM, neutral, 5))))

    def test_the_file_function_reads_through_read_patch_text(self) -> None:
        with mock.patch.object(
            VALIDATOR, "read_patch_text", return_value=GOMOD_PATCH
        ) as read:
            VALIDATOR.validate_gomod_patch("images/ax/some.patch")
        read.assert_called_once_with("images/ax/some.patch")
        with mock.patch.object(
            VALIDATOR, "read_patch_text", return_value=patch_bytes(GOMOD)
        ):
            with self.assertRaisesRegex(VALIDATOR.AxLabError, "go.mod and then go.sum"):
                VALIDATOR.validate_gomod_patch("images/ax/some.patch")

    def test_a_missing_file_is_a_contract_error(self) -> None:
        with self.assertRaisesRegex(VALIDATOR.AxLabError, "cannot read images/ax/no"):
            VALIDATOR.read_patch_text("images/ax/no-such-file.patch")
        with self.assertRaisesRegex(VALIDATOR.AxLabError, "cannot read images/ax/no"):
            VALIDATOR.validate_gomod_patch("images/ax/no-such-file.patch")

    def test_the_vendored_375_patch_is_not_a_modules_patch(self) -> None:
        # The error names the files, so a renamed patch ("cannot read ...")
        # or any other refusal does not pass for it.
        self.assertTrue((ROOT / "images/ax/google-ax-375.patch").is_file())
        with self.assertRaisesRegex(
            VALIDATOR.AxLabError,
            "^images/ax/google-ax-375.patch must patch go.mod and then go.sum, only$",
        ):
            VALIDATOR.validate_gomod_patch("images/ax/google-ax-375.patch")


# A toy module in a real repository: what git itself makes of a patch, and
# whether it applies. No commit is made, so no identity is needed.
TOY_MOD = (
    "module example.com/toy\n\ngo 1.27.0\n\nrequire (\n"
    "\texample.com/a v1.0.0 // indirect\n"
    "\tgo.uber.org/atomic v1.11.0 // indirect\n)\n"
)
TOY_SUM = (
    "example.com/a v1.0.0 h1:" + "A" * 43 + "=\n"
    "example.com/a v1.0.0/go.mod h1:" + "B" * 43 + "=\n"
    "go.uber.org/atomic v1.11.0 h1:" + "C" * 43 + "=\n"
)
TOY_BUMPED_SUM = TOY_SUM.replace("a v1.0.0", "a v1.1.0")


class GitAgreementTests(unittest.TestCase):
    """What the validator refuses, a real git would apply or read otherwise."""

    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # Built from nothing, so that no GIT_DIR of a hook or user config leaks in.
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }

    def git(
        self, directory: Path, *args: str, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=directory,
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    def repository(self) -> Path:
        """A repository whose index holds the toy go.mod and go.sum."""
        directory = Path(tempfile.mkdtemp(dir=self.home))
        self.assertEqual(self.git(directory, "init", "-q").returncode, 0)
        (directory / "go.mod").write_text(TOY_MOD)
        (directory / "go.sum").write_text(TOY_SUM)
        self.assertEqual(self.git(directory, "add", "go.mod", "go.sum").returncode, 0)
        return directory

    def patch_of(self, mod: str, total: str = TOY_BUMPED_SUM) -> str:
        """What `git diff --full-index` writes for the toy files changed so."""
        directory = self.repository()
        (directory / "go.mod").write_text(mod)
        (directory / "go.sum").write_text(total)
        diff = self.git(directory, "diff", "--full-index")
        self.assertEqual(diff.returncode, 0, diff.stderr)
        self.assertTrue(diff.stdout.startswith("diff --git a/go.mod b/go.mod\n"))
        return diff.stdout

    def applies_as(self, patch: str, mod: str) -> None:
        """Git applies the patch to the toy files and go.mod becomes `mod`."""
        directory = self.repository()
        done = self.git(directory, "apply", "--index", "-", stdin=patch)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((directory / "go.mod").read_text(), mod)

    def read_by_git(self, patch: str) -> list[str] | None:
        """The files git reads in the patch, one per section it finds."""
        done = self.git(self.home, "apply", "--numstat", "-", stdin=patch)
        if done.returncode != 0:
            return None
        return [line.split("\t")[2] for line in done.stdout.splitlines()]

    def files_of(self, patch: str) -> list[str]:
        files = self.read_by_git(patch)
        self.assertIsNotNone(files, "git does not read the patch")
        return files or []

    def test_the_patch_of_a_bump_is_what_git_and_the_validator_both_accept(
        self,
    ) -> None:
        bumped = TOY_MOD.replace("a v1.0.0", "a v1.1.0")
        patch = self.patch_of(bumped)
        self.applies_as(patch, bumped)
        self.assertEqual(self.files_of(patch), ["go.mod", "go.sum"])
        VALIDATOR.validate_gomod_patch_text(patch.encode(), "toy")

    def test_a_module_that_starts_like_a_directive_is_not_one(self) -> None:
        # Bumps of go.uber.org/atomic, and a new requirement: a module may be
        # added, as the relock patches do for what `go get` raised.
        for label, mod, total in (
            (
                "go.uber.org/atomic",
                TOY_MOD.replace("atomic v1.11.0", "atomic v1.12.0"),
                TOY_SUM.replace("atomic v1.11.0", "atomic v1.12.0"),
            ),
            (
                "a new requirement",
                TOY_MOD.replace(")\n", "\ttoolchain.example/x v1.0.0\n)\n"),
                TOY_SUM + "toolchain.example/x v1.0.0 h1:" + "D" * 43 + "=\n",
            ),
        ):
            with self.subTest(case=label):
                patch = self.patch_of(mod, total)
                self.applies_as(patch, mod)
                VALIDATOR.validate_gomod_patch_text(patch.encode(), "toy")

    def test_every_refused_directive_would_otherwise_apply(self) -> None:
        message = "adds a replace, exclude, go, toolchain, godebug, tool"
        for label, mod in (
            ("replace", TOY_MOD + "replace example.com/a => ../a\n"),
            (
                "a replace block",
                TOY_MOD
                + "replace (\n\texample.com/a v1.0.0 => example.com/b v1.0.0\n)\n",
            ),
            ("exclude", TOY_MOD + "exclude example.com/a v1.0.0\n"),
            ("go", TOY_MOD.replace("go 1.27.0", "go 1.20")),
            ("go and a tab", TOY_MOD.replace("go 1.27.0", "go\t1.20")),
            ("toolchain", TOY_MOD + "toolchain go1.99.0\n"),
            ("godebug", TOY_MOD + "godebug default=go1.20\n"),
            ("a godebug block", TOY_MOD + "godebug (\n\tdefault=go1.20\n)\n"),
            ("tool", TOY_MOD + "tool example.com/x/cmd/y\n"),
            ("ignore", TOY_MOD + "ignore ./vendor\n"),
            ("retract", TOY_MOD + "retract v1.0.0\n"),
        ):
            with self.subTest(directive=label):
                patch = self.patch_of(mod)
                self.applies_as(patch, mod)
                self.assertEqual(self.files_of(patch), ["go.mod", "go.sum"])
                with self.assertRaisesRegex(VALIDATOR.AxLabError, message):
                    VALIDATOR.validate_gomod_patch_text(patch.encode(), "toy")

    def test_a_stray_line_makes_git_read_another_patch(self) -> None:
        # Between the index and the --- line git takes the section for a patch
        # of go.mod of its own (or refuses the patch): it never reads the two
        # files the validator took it for.
        bumped = TOY_MOD.replace("a v1.0.0", "a v1.1.0")
        lines = self.patch_of(bumped).split("\n")
        stray = "\n".join([*lines[:2], "+x", *lines[2:]])
        self.assertNotEqual(self.read_by_git(stray), ["go.mod", "go.sum"])
        with self.assertRaisesRegex(VALIDATOR.AxLabError, "then hunks"):
            VALIDATOR.validate_gomod_patch_text(stray.encode(), "toy")
        # The pair of headers and the hunk of go.mod again before go.sum.
        sum_section = lines.index("diff --git a/go.sum b/go.sum")
        again = "\n".join([*lines[:sum_section], *lines[2:]])
        self.assertNotEqual(self.read_by_git(again), ["go.mod", "go.sum"])
        with self.assertRaisesRegex(VALIDATOR.AxLabError, "second file header"):
            VALIDATOR.validate_gomod_patch_text(again.encode(), "toy")

    def test_whatever_the_validator_accepts_git_reads_as_go_mod_and_go_sum(
        self,
    ) -> None:
        # The real patch with each of its lines taken out, repeated, or
        # preceded by a stray line of each kind.
        bumped = TOY_MOD.replace("a v1.0.0", "a v1.1.0")
        real = self.patch_of(bumped).rstrip("\n").split("\n")
        marker = "\\ No newline at end of file"
        strays = ("+x", "-x", " x", "@@ -1 +1 @@", "index", marker)
        mutants = []
        for number in range(len(real)):
            mutants.append(("taken out", [*real[:number], *real[number + 1 :]]))
            mutants.append(("repeated", [*real[: number + 1], *real[number:]]))
            mutants.extend(
                (stray, [*real[:number], stray, *real[number:]]) for stray in strays
            )
        accepted = []
        for label, mutant in mutants:
            text = "\n".join(mutant) + "\n"
            try:
                VALIDATOR.validate_gomod_patch_text(text.encode(), "toy")
            except VALIDATOR.AxLabError:
                continue
            accepted.append(label)
            with self.subTest(mutant=mutant):
                self.assertEqual(self.files_of(text), ["go.mod", "go.sum"])
        # Only a no-newline marker after a line of a hunk gets through.
        self.assertEqual(set(accepted), {marker})


class ModulesPatchSourceTests(RelockCase):
    def sources(self) -> dict[str, Any]:
        return relocked_document()["ax_lab"]["sources"]

    def test_without_the_key_the_sources_validate_as_before(self) -> None:
        sources = legacy_document()["ax_lab"]["sources"]
        VALIDATOR.validate_sources(sources)

    def test_well_formed_entries_are_accepted(self) -> None:
        with patched_files():
            VALIDATOR.validate_sources(self.sources())
        for name in ("ax", "substrate"):
            sources = self.sources()
            del sources[name]["modules_patch"]
            with self.subTest(only=name), patched_files():
                VALIDATOR.validate_sources(sources)

    def test_the_ax_entry_is_pinned_by_path_and_hash(self) -> None:
        base = "the ax modules patch"
        keys = "ax modules patch: unexpected or missing keys"
        first = self.sources()["ax"]["patch"]["sha256"]
        for label, entry, message in (
            ("other path", ax_entry(path="images/ax/other.patch"), base + " must be"),
            ("the 375 patch", ax_entry(sha256=first), "must not be the #375 patch"),
            ("another hash", ax_entry(sha256="e" * 64), "does not hash to its"),
            ("uppercase hash", ax_entry(sha256="C" * 64), "64 lowercase hex"),
            ("short hash", ax_entry(sha256="c" * 63), "64 lowercase hex"),
            ("not text", ax_entry(sha256=7), "64 lowercase hex"),
            ("a tree", ax_entry(tree=TREE), keys),
            ("no hash", {"path": AX_MODULES_PATCH}, keys),
            ("no path", {"sha256": AX_SHA256}, keys),
            ("empty", {}, keys),
            ("none", None, keys),
            ("text", AX_MODULES_PATCH, keys),
        ):
            with self.subTest(case=label):
                sources = self.sources()
                sources["ax"]["modules_patch"] = entry
                self.sources_rejected(sources, message)

    def test_the_substrate_entry_is_pinned_by_path_hash_and_tree(self) -> None:
        keys = "substrate modules patch: unexpected or missing keys"
        tree = "the Substrate modules patch tree must be a 40-hex git tree id"
        no_tree = {"path": SUBSTRATE_MODULES_PATCH, "sha256": SUBSTRATE_SHA256}
        for label, entry, message in (
            (
                "other path",
                substrate_entry(path=AX_MODULES_PATCH),
                "must be the vendored",
            ),
            ("another hash", substrate_entry(sha256="e" * 64), "does not hash to its"),
            ("uppercase hash", substrate_entry(sha256="D" * 64), "64 lowercase hex"),
            ("short hash", substrate_entry(sha256="d" * 63), "64 lowercase hex"),
            ("no tree", no_tree, keys),
            ("tree and more", substrate_entry(extra=1), keys),
            ("empty tree", substrate_entry(tree=""), tree),
            ("short tree", substrate_entry(tree="a" * 39), tree),
            ("long tree", substrate_entry(tree="a" * 41), tree),
            ("abbreviated tree", substrate_entry(tree="a" * 7), tree),
            ("uppercase tree", substrate_entry(tree="A" * 40), tree),
            ("non-hex tree", substrate_entry(tree="g" * 40), tree),
            ("sha256 as tree", substrate_entry(tree="a" * 64), tree),
            ("integer tree", substrate_entry(tree=1), tree),
            ("none tree", substrate_entry(tree=None), tree),
            ("empty", {}, keys),
            ("none", None, keys),
            ("text", SUBSTRATE_MODULES_PATCH, keys),
        ):
            with self.subTest(case=label):
                sources = self.sources()
                sources["substrate"]["modules_patch"] = entry
                self.sources_rejected(sources, message)

    def test_the_patch_file_must_be_a_go_mod_and_go_sum_patch(self) -> None:
        other = [line.replace("go.mod", "main.go") for line in GOMOD]
        replacing = with_line(GOMOD, "+replace example.com/a => ../a", 7)
        for name, path in (
            ("ax", AX_MODULES_PATCH),
            ("substrate", SUBSTRATE_MODULES_PATCH),
        ):
            for data, message in (
                (patch_bytes(other, GOSUM), "go.mod and then go.sum"),
                (
                    patch_bytes(replacing, GOSUM),
                    "adds a replace, exclude, go, toolchain",
                ),
                (b"", "must hold 1 to 65536 bytes"),
            ):
                with self.subTest(source=name, message=message):
                    self.sources_rejected(self.sources(), message, texts={path: data})

    def test_the_patch_key_stays_the_ax_one(self) -> None:
        # The existing contract: Substrate has no `patch`, and AX must have it.
        sources = self.sources()
        sources["substrate"]["patch"] = {}
        self.sources_rejected(sources, "substrate: unexpected")
        sources = self.sources()
        del sources["ax"]["patch"]
        self.sources_rejected(sources, "ax: unexpected")
        sources = self.sources()
        sources["substrate"]["modules_patch"]["extra"] = 1
        self.sources_rejected(sources, "substrate modules patch: unexpected")
        sources = self.sources()
        sources["ax"]["modules_patch2"] = {}
        self.sources_rejected(sources, "ax: unexpected")


class RelockStateTests(RelockCase):
    def test_the_repository_config_validates_as_it_stands(self) -> None:
        VALIDATOR.validate_catalog(document(), self.reserved)

    def test_the_legacy_state_is_accepted(self) -> None:
        lab = self.validated(legacy_document())
        self.assertEqual(lab["images"]["toolbox"], GOLANG_1_27_1)
        self.assertNotIn("runner_toolbox", lab["images"])

    def test_the_relocked_state_is_accepted(self) -> None:
        lab = self.validated(relocked_document())
        self.assertEqual(lab["images"]["toolbox"], lab["images"]["web_toolbox"])
        self.assertEqual(lab["images"]["runner_toolbox"], GOLANG_1_27_1)
        self.assertEqual(lab["sources"]["ax"]["modules_patch"], ax_entry())
        self.assertEqual(
            lab["sources"]["substrate"]["modules_patch"], substrate_entry()
        )

    def test_the_three_fields_come_together_or_not_at_all(self) -> None:
        legal = {
            (False, False, False): "the legacy state",
            (False, False, True): "the runner toolbox alone",
            (True, True, True): "the relocked state",
        }
        for ax in (False, True):
            for substrate in (False, True):
                for runner in (False, True):
                    fields = (ax, substrate, runner)
                    with self.subTest(ax=ax, substrate=substrate, runner=runner):
                        if fields in legal:
                            self.validated(combination(*fields))
                        else:
                            self.rejected(
                                combination(*fields),
                                "^images.runner_toolbox, sources.ax.modules_patch and "
                                "sources.substrate.modules_patch must be set together$",
                            )

    def test_the_toolbox_follows_the_state(self) -> None:
        relocked = relocked_document()
        relocked["ax_lab"]["images"]["toolbox"] = GOLANG_1_27_1
        self.rejected(relocked, "^the toolbox must be golang 1.27.2$")
        for mutated in (legacy_document(), combination(False, False, True)):
            mutated["ax_lab"]["images"]["toolbox"] = mutated["ax_lab"]["images"][
                "web_toolbox"
            ]
            self.rejected(mutated, "^the toolbox must be golang 1.27.1$")

    def test_the_runner_toolbox_is_the_release_that_built_the_deployed_binary(
        self,
    ) -> None:
        for mutated in (relocked_document(), combination(False, False, True)):
            images = mutated["ax_lab"]["images"]
            images["runner_toolbox"] = images["web_toolbox"]
            self.rejected(mutated, "^the runner toolbox must be golang 1.27.1$")

    def test_the_web_toolbox_stays_pinned_to_its_release(self) -> None:
        relocked = relocked_document()
        relocked["ax_lab"]["images"]["web_toolbox"] = GOLANG_1_27_1
        self.rejected(relocked, "^the web toolbox must be golang 1.27.2$")

    def test_the_host_never_builds_a_relocked_substrate(self) -> None:
        closed = (
            "^ax_lab_substrate_fallback_builds must be empty while "
            "sources.substrate.modules_patch is set: the host never patches or "
            "vendors the Substrate checkout$"
        )
        for names in (["ate-setup"], ["ateapi"], ["ateapi", "atelet"]):
            relocked = relocked_document()
            relocked["ax_lab_substrate_fallback_builds"] = names
            with self.subTest(relocked=names):
                self.rejected(relocked, closed)
            legacy = legacy_document()
            legacy["ax_lab_substrate_fallback_builds"] = names
            with self.subTest(legacy=names):
                self.validated(legacy)
        relocked = relocked_document()
        relocked["ax_lab_substrate_fallback_builds"] = []
        self.validated(relocked)

    def test_the_older_fallback_messages_keep_winning(self) -> None:
        for mutated in (legacy_document(), relocked_document()):
            pending = copy.deepcopy(mutated)
            pending["ax_lab"]["substrate"]["images"]["ate-setup"] = None
            pending["ax_lab_substrate_fallback_builds"] = ["ate-setup"]
            self.rejected(pending, "no pinned digest to reproduce")
            unknown = copy.deepcopy(mutated)
            unknown["ax_lab_substrate_fallback_builds"] = ["ateom-microvm"]
            self.rejected(unknown, "not a Substrate image")
            twice = copy.deepcopy(mutated)
            twice["ax_lab_substrate_fallback_builds"] = ["ateapi", "ateapi"]
            self.rejected(twice, "distinct images")

    def test_the_fallback_check_takes_the_state_as_a_flag(self) -> None:
        substrate = legacy_document()["ax_lab"]["substrate"]
        VALIDATOR.validate_fallback_builds(["ateapi"], substrate)
        VALIDATOR.validate_fallback_builds(["ateapi"], substrate, relocked=False)
        VALIDATOR.validate_fallback_builds([], substrate, relocked=True)
        with self.assertRaisesRegex(VALIDATOR.AxLabError, "must be empty while"):
            VALIDATOR.validate_fallback_builds(["ateapi"], substrate, relocked=True)


class PlanTests(RelockCase):
    def lab(self, mutated: dict[str, Any]) -> dict[str, Any]:
        return self.validated(mutated)

    def test_the_legacy_plans_carry_no_modules_patch(self) -> None:
        lab = self.lab(legacy_document())
        self.assertNotIn("modules_patch", VALIDATOR.substrate_plan(lab))
        self.assertNotIn("modules_patch", VALIDATOR.ax_plan(lab))

    def test_the_ax_plan_always_names_the_runner_toolbox_and_the_tag(self) -> None:
        lab = self.lab(legacy_document())
        plan = VALIDATOR.ax_plan(lab)
        self.assertEqual(plan["runner_toolbox_image"], plan["toolbox_image"])
        self.assertEqual(plan["tag"], lab["ax"]["tag"])
        self.assertEqual(plan["patch"], lab["sources"]["ax"]["patch"])
        alone = self.lab(combination(False, False, True))
        plan = VALIDATOR.ax_plan(alone)
        self.assertEqual(plan["runner_toolbox_image"], GOLANG_1_27_1)
        self.assertEqual(plan["toolbox_image"], GOLANG_1_27_1)
        self.assertNotIn("modules_patch", plan)

    def test_the_relocked_plans_carry_their_modules_patch(self) -> None:
        legacy = self.lab(legacy_document())
        lab = self.lab(relocked_document())
        plan = VALIDATOR.ax_plan(lab)
        self.assertEqual(plan["modules_patch"], ax_entry())
        self.assertEqual(plan["runner_toolbox_image"], GOLANG_1_27_1)
        self.assertEqual(plan["toolbox_image"], lab["images"]["web_toolbox"])
        self.assertEqual(set(plan) - set(VALIDATOR.ax_plan(legacy)), {"modules_patch"})
        plan = VALIDATOR.substrate_plan(lab)
        self.assertEqual(plan["modules_patch"], substrate_entry())
        self.assertEqual(plan["toolbox_image"], lab["images"]["web_toolbox"])
        self.assertEqual(
            set(plan) - set(VALIDATOR.substrate_plan(legacy)), {"modules_patch"}
        )

    def test_the_command_line_prints_the_plans_as_json(self) -> None:
        lab = VALIDATOR.validate_catalog(document(), self.reserved)
        sources = document()["ax_lab"]["sources"]
        # The shape is spelled out, not read back from the functions that
        # make it; the relock adds one key, once the config sets it.
        keys = {
            "--ax-plan": {
                "commit",
                "repository",
                "patch",
                "substrate_commit",
                "toolbox_image",
                "runner_toolbox_image",
                "tag",
                "ko_base_image",
                "ko_images",
                "ko_manifests",
                "cli_sha256",
                "task_runner_binary_sha256",
                *({"modules_patch"} & set(sources["ax"])),
            },
            "--substrate-plan": {
                "version",
                "commit",
                "repository",
                "toolbox_image",
                "registry_image",
                "build",
                "images",
                *({"modules_patch"} & set(sources["substrate"])),
            },
        }
        for flag, plan in (
            ("--ax-plan", VALIDATOR.ax_plan(lab)),
            ("--substrate-plan", VALIDATOR.substrate_plan(lab)),
        ):
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts/validate-ax-lab.py"), flag],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            with self.subTest(flag=flag):
                self.assertEqual(completed.returncode, 0, completed.stderr)
                printed = json.loads(completed.stdout)
                self.assertEqual(set(printed), keys[flag])
                self.assertEqual(printed, json.loads(json.dumps(plan)))


if __name__ == "__main__":
    unittest.main()
