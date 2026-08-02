from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import backup_safety  # noqa: E402

STAMP = "20260723T225340Z"
PACKAGE = f"apptolast-data-{STAMP}.tar.zst.gpg"
KEY = f"apptolast-data-{STAMP}.RECOVERY-KEY.txt"
TAG = f"backup-{STAMP}"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_release(directory: Path, payload: bytes = b"encrypted-fixture") -> None:
    part = f"{PACKAGE}.part-000.bin"
    (directory / part).write_bytes(payload)
    (directory / KEY).write_bytes(base64.b64encode(b"k" * 48) + b"\n")
    (directory / "PARTS_SHA256SUMS").write_text(
        f"{digest(payload)}  {part}\n", encoding="utf-8"
    )
    (directory / "ORIGINAL_SHA256SUMS").write_text(
        f"{digest(payload)}  {PACKAGE}\n", encoding="utf-8"
    )
    manifest = {
        "schemaVersion": 1,
        "tag": TAG,
        "package": PACKAGE,
        "packageSize": len(payload),
        "packageSha256": digest(payload),
        "recoveryKeyAsset": KEY,
        "partCount": 1,
        "partSize": "1GiB",
    }
    (directory / "RELEASE-MANIFEST.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (directory / "README-RELEASE.txt").write_text("fixture\n", encoding="utf-8")


def write_tree_checksums(root: Path) -> None:
    lines = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            lines.append(
                f"{backup_safety.sha256_file(path)}  {path.relative_to(root).as_posix()}"
            )
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def tar_bytes(entries: list[tuple[str, bytes, str]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o700
                archive.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "../../outside"
                archive.addfile(info)
            else:
                info.size = len(content)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


class ReleaseValidationTests(unittest.TestCase):
    def test_valid_release_and_atomic_reassembly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            payload = b"fixture-payload"
            write_release(directory, payload)
            manifest = backup_safety.validate_release_directory(directory)
            self.assertEqual(manifest["tag"], TAG)
            result = backup_safety.reassemble_release(directory)
            self.assertEqual(result.name, PACKAGE)
            self.assertEqual(result.read_bytes(), payload)

    def test_unknown_asset_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_release(directory)
            (directory / "unexpected.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(backup_safety.SafetyError):
                backup_safety.validate_release_directory(directory)

    def test_manifest_path_and_extra_field_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_release(directory)
            manifest_path = directory / "RELEASE-MANIFEST.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["package"] = "../../outside"
            manifest["unexpected"] = True
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(backup_safety.SafetyError):
                backup_safety.load_manifest(manifest_path)


class TreeValidationTests(unittest.TestCase):
    def test_exact_coverage_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_bytes(b"a")
            write_tree_checksums(root)
            self.assertEqual(
                backup_safety.verify_checksum_tree(root, root / "SHA256SUMS"), 1
            )
            (root / "uncovered").write_bytes(b"x")
            with self.assertRaises(backup_safety.SafetyError):
                backup_safety.verify_checksum_tree(root, root / "SHA256SUMS")

    def test_checksum_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checksum = root / "SHA256SUMS"
            checksum.write_text(f"{'0' * 64}  ../outside\n", encoding="utf-8")
            with self.assertRaises(backup_safety.SafetyError):
                backup_safety.parse_checksums(checksum)


class TarValidationTests(unittest.TestCase):
    def test_safe_tar_extracts_and_verifies(self) -> None:
        root = f"apptolast-data-{STAMP}"
        content = b"hello"
        sums = f"{digest(content)}  data.txt\n".encode()
        payload = tar_bytes(
            [
                (root, b"", "dir"),
                (f"{root}/data.txt", content, "file"),
                (f"{root}/SHA256SUMS", sums, "file"),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            extracted_root = backup_safety.process_tar_stream(
                io.BytesIO(payload), destination
            )
            self.assertEqual(extracted_root, root)
            self.assertEqual(
                backup_safety.verify_checksum_tree(
                    destination / root, destination / root / "SHA256SUMS"
                ),
                1,
            )

    def test_tar_traversal_is_rejected(self) -> None:
        root = f"apptolast-data-{STAMP}"
        payload = tar_bytes(
            [
                (root, b"", "dir"),
                (f"{root}/../outside", b"x", "file"),
                (f"{root}/SHA256SUMS", b"x", "file"),
            ]
        )
        with self.assertRaises(backup_safety.SafetyError):
            backup_safety.process_tar_stream(io.BytesIO(payload), None)

    def test_tar_symlink_is_rejected(self) -> None:
        root = f"apptolast-data-{STAMP}"
        payload = tar_bytes(
            [
                (root, b"", "dir"),
                (f"{root}/link", b"", "symlink"),
                (f"{root}/SHA256SUMS", b"x", "file"),
            ]
        )
        with self.assertRaises(backup_safety.SafetyError):
            backup_safety.process_tar_stream(io.BytesIO(payload), None)


if __name__ == "__main__":
    unittest.main()
