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
import subprocess
import sys
import unittest
from collections.abc import Iterator
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
    "@@ -3,7 +3,7 @@ go 1.27.0",
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


def with_line(block: list[str], line: str, at: int = 1) -> list[str]:
    return [*block[:at], line, *block[at:]]


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
                "a plain diff between the sections",
                patch_bytes(GOMOD, PLAIN_DIFF, GOSUM),
            ),
            ("a plain diff after the last", patch_bytes(GOMOD, GOSUM, PLAIN_DIFF)),
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
            (
                "a second pair of another file",
                patch_bytes(with_line(GOMOD, "--- a/main.go", 9), GOSUM),
            ),
        ):
            with self.subTest(case=label):
                self.refused(data, "names a file its header does not")

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
        self.refused(b"", "must hold 1 to 65536 bytes")
        filler = [" " + "x" * 99] * 700
        self.refused(patch_bytes(GOMOD, filler, GOSUM), "must hold 1 to 65536 bytes")
        # The limit itself still passes.
        room = VALIDATOR.GOMOD_PATCH_MAX_BYTES - len(GOMOD_PATCH)
        self.accepted(patch_bytes(GOMOD, [" " + "x" * (room - 2)], GOSUM))
        self.refused(
            patch_bytes(GOMOD, [" " + "x" * (room - 1)], GOSUM),
            "must hold 1 to 65536 bytes",
        )

    def test_the_text_is_strict_utf8(self) -> None:
        self.refused(GOMOD_PATCH + b"\xff\xfe", "must be UTF-8 text")
        self.refused(
            GOMOD_PATCH.replace(b"example.com/c", b"example.com/\xc3"), "UTF-8"
        )

    def test_an_added_replace_or_exclude_is_refused_a_removed_one_is_not(self) -> None:
        for added in (
            "replace example.com/a => ../a",
            "replace (",
            "replace(",
            "\treplace example.com/a v1.0.0 => example.com/b v1.0.0",
            "exclude example.com/a v1.0.0",
            "\texclude example.com/a v1.0.0",
            # Inside an existing replace block the keyword is not repeated.
            "\texample.com/a v1.0.0 => example.com/b v1.0.0",
            "\texample.com/a => ../a",
        ):
            with self.subTest(added=added):
                self.refused(
                    patch_bytes(with_line(GOMOD, "+" + added, 7), GOSUM),
                    "adds a replace or exclude",
                )
        for neutral in (
            "-replace example.com/a => ../a",
            "-\texample.com/a => ../a",
            "-exclude example.com/a v1.0.0",
            " replace example.com/a => ../a",
            "+\treplace-me.example/x v1.0.0 // indirect",
            "+\texample.com/replace v1.0.0 // indirect",
            "+\texample.com/exclude v1.0.0 // indirect",
        ):
            with self.subTest(neutral=neutral):
                self.accepted(patch_bytes(with_line(GOMOD, neutral, 7), GOSUM))

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
        with self.assertRaises(VALIDATOR.AxLabError):
            VALIDATOR.validate_gomod_patch("images/ax/google-ax-375.patch")


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
                (patch_bytes(replacing, GOSUM), "adds a replace or exclude"),
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
                self.assertEqual(
                    json.loads(completed.stdout), json.loads(json.dumps(plan))
                )
        self.assertEqual(
            "modules_patch" in VALIDATOR.ax_plan(lab),
            "modules_patch" in document()["ax_lab"]["sources"]["ax"],
        )


if __name__ == "__main__":
    unittest.main()
