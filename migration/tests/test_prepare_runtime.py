from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
from unittest import mock
import sys
import tarfile
import tempfile
import unittest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import backup_safety  # noqa: E402
import prepare_runtime  # noqa: E402

STAMP = "20260723T225340Z"


def write_tar_gz(path: Path, entries: dict[str, bytes]) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        roots = sorted({name.split("/", 1)[0] for name in entries})
        for root in roots:
            info = tarfile.TarInfo(root)
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def write_checksums(stage: Path) -> None:
    lines = []
    for path in sorted(stage.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            lines.append(
                f"{backup_safety.sha256_file(path)}  {path.relative_to(stage).as_posix()}"
            )
    (stage / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_stage(parent: Path) -> Path:
    stage = parent / f"apptolast-data-{STAMP}"
    (stage / "databases").mkdir(parents=True)
    (stage / "filesystems").mkdir()
    for name in prepare_runtime.DATABASE_DUMPS:
        (stage / "databases" / f"{name}.dump").write_bytes(f"{name}-dump".encode())
    write_tar_gz(
        stage / "filesystems/n8n-home.tar.gz",
        {".n8n/config": json.dumps({"encryptionKey": "existing-key"}).encode()},
    )
    write_tar_gz(
        stage / "filesystems/passbolt-keys.tar.gz",
        {"gpg/serverkey_private.asc": b"key", "jwt/jwt.key": b"jwt"},
    )
    (stage / "filesystems/minecraft-data.tar.zst").write_bytes(b"zstd-data")
    (stage / "filesystems/minecraft-bind-mods.tar.zst").write_bytes(b"zstd-mods")
    (stage / "filesystems/traefik-acme.json").write_text("{}\n", encoding="utf-8")
    (stage / "filesystems/OPENCLAW-LEGACY-EXCLUDED.txt").write_text(
        "excluded\n", encoding="utf-8"
    )
    passbolt_secrets = stage / "kubernetes/passbolt/secrets.json"
    passbolt_secrets.parent.mkdir(parents=True)
    passbolt_secrets.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "data": {
                            "SECURITY_SALT": base64.b64encode(b"existing-salt").decode()
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    write_checksums(stage)
    return stage


def fake_extract_zstd(
    source: Path,
    destination: Path,
    roots: set[str],
    *,
    strip_root: bool,
) -> None:
    del roots, strip_root
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    (destination / "fixture").write_bytes(source.read_bytes())


class PrepareRuntimeTests(unittest.TestCase):
    def test_canonical_runtime_requires_root_for_numeric_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = create_stage(Path(temporary))
            with (
                mock.patch.object(prepare_runtime.os, "geteuid", return_value=1000),
                self.assertRaises(backup_safety.SafetyError),
            ):
                prepare_runtime.prepare(
                    stage,
                    prepare_runtime.DEFAULT_SERVICES_ROOT,
                )

    def test_database_bind_directories_receive_image_numeric_owners(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            expected = {
                "n8n/postgres": (999, 999),
                "passbolt/postgres": (70, 70),
                "shlink/postgres": (70, 70),
            }
            for relative in expected:
                path = runtime / relative
                path.mkdir(parents=True)
                (path / "fixture").write_bytes(b"data")

            with (
                mock.patch.object(prepare_runtime.os, "geteuid", return_value=0),
                mock.patch.object(prepare_runtime.os, "chown") as mocked_chown,
            ):
                prepare_runtime.apply_container_ownership(runtime)

            for relative, (uid, gid) in expected.items():
                path = runtime / relative
                mocked_chown.assert_any_call(
                    path,
                    uid,
                    gid,
                    follow_symlinks=False,
                )
                mocked_chown.assert_any_call(
                    path / "fixture",
                    uid,
                    gid,
                    follow_symlinks=False,
                )
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
                self.assertEqual(
                    (path / "fixture").stat().st_mode & 0o777,
                    0o600,
                )

    def test_only_exact_legacy_n8n_wheel_cache_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            for relative in prepare_runtime.LEGACY_N8N_STORAGE_WHEELS:
                path = home / "storage" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"wheel")
            prepare_runtime.remove_legacy_n8n_storage_wheels(home)
            self.assertFalse((home / "storage").exists())

    def test_unknown_n8n_storage_content_is_preserved_and_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            storage = home / "storage"
            storage.mkdir()
            (storage / "owner-data").write_bytes(b"keep")
            with self.assertRaises(backup_safety.SafetyError):
                prepare_runtime.remove_legacy_n8n_storage_wheels(home)
            self.assertEqual((storage / "owner-data").read_bytes(), b"keep")

    def test_matches_services_layout_and_keeps_values_out_of_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            stage = create_stage(base)
            runtime = base / "runtime"
            with mock.patch.object(
                prepare_runtime, "extract_zstd", side_effect=fake_extract_zstd
            ):
                prepare_runtime.prepare(stage, runtime)

            expected_paths = (
                "minecraft/data/fixture",
                "minecraft/mods/fixture",
                "n8n/home/config",
                "n8n/postgres",
                "passbolt/gpg/serverkey_private.asc",
                "passbolt/jwt/jwt.key",
                "passbolt/postgres",
                "shlink/postgres",
                "openclaw-clean/home",
                "recovery/databases/vectors.dump",
                "recovery/edge/traefik-acme.json",
            )
            for relative in expected_paths:
                self.assertTrue((runtime / relative).exists(), relative)
            self.assertFalse((runtime / "recovery/staging").exists())
            self.assertFalse((runtime / "traefik").exists())
            self.assertFalse((runtime / "observability").exists())
            self.assertEqual(list(runtime.rglob("*.env")), [])

            manifest_text = (runtime / "runtime-manifest.json").read_text(
                encoding="utf-8"
            )
            manifest = json.loads(manifest_text)
            self.assertFalse(manifest["openClawLegacyImported"])
            self.assertEqual(
                manifest["runtimeRootContract"],
                "/srv/dockerswarm/services",
            )
            self.assertEqual(
                manifest["secretDelivery"],
                "individual-docker-secret-source-files",
            )
            values = []
            for name in prepare_runtime.DIRECT_SECRET_NAMES:
                path = runtime / "secrets/files" / name
                value = path.read_text(encoding="ascii").strip()
                self.assertGreater(len(value), 40)
                values.append(value)
                if os.name == "posix":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for value in values:
                self.assertNotIn(value, manifest_text)

            self.assertEqual(
                (runtime / "secrets/files/n8n_encryption_key").read_text(
                    encoding="utf-8"
                ),
                "existing-key\n",
            )
            if os.name == "posix":
                self.assertEqual(
                    (runtime / "secrets/files/n8n_encryption_key").stat().st_mode
                    & 0o777,
                    0o600,
                )
            self.assertEqual(
                (runtime / "secrets/files/passbolt_security_salt").read_text(
                    encoding="utf-8"
                ),
                "existing-salt\n",
            )
            self.assertIn("openclaw_gateway_token", manifest["secretFiles"])
            self.assertIn("n8n_encryption_key", manifest["secretFiles"])
            expected_secret_files = set(prepare_runtime.DIRECT_SECRET_NAMES)
            for mapping in prepare_runtime.SECRET_FILE_NAMES.values():
                expected_secret_files.update(mapping.values())
            self.assertEqual(
                set(manifest["secretFiles"]),
                expected_secret_files,
            )
            self.assertEqual(
                manifest["secretIdentityKeyFile"],
                prepare_runtime.SECRET_IDENTITY_KEY_FILE,
            )
            identity_key_path = (
                runtime / "secrets/files" / prepare_runtime.SECRET_IDENTITY_KEY_FILE
            )
            self.assertEqual(
                identity_key_path.stat().st_size,
                prepare_runtime.SECRET_IDENTITY_KEY_SIZE,
            )
            self.assertNotIn(
                prepare_runtime.SECRET_IDENTITY_KEY_FILE,
                manifest["secretFiles"],
            )
            if os.name == "posix":
                self.assertEqual(
                    identity_key_path.stat().st_mode & 0o777,
                    0o600,
                )
            for name in expected_secret_files:
                self.assertTrue(
                    (runtime / "secrets/files" / name).is_file(),
                    name,
                )
            self.assertEqual(
                (runtime / "secrets/files/n8n_smtp_host").stat().st_size,
                0,
            )
            self.assertNotIn("existing-key", manifest_text)
            self.assertNotIn("existing-salt", manifest_text)

    def test_secret_values_are_written_literally_without_shell_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            secret_root = Path(temporary)
            unexpected = secret_root / "should-not-exist"
            dangerous = f'"; $(touch {unexpected}); ${{HOME}} #'
            written = prepare_runtime.write_approved_secret_files(
                secret_root,
                {
                    "n8n": {
                        "N8N_ENCRYPTION_KEY": "required-n8n-key",
                        "N8N_SMTP_PASS": dangerous,
                    },
                    "passbolt": {"SECURITY_SALT": "required-passbolt-salt"},
                },
            )
            self.assertIn("n8n_smtp_pass", written)
            self.assertEqual(
                (secret_root / "n8n_smtp_pass").read_text(encoding="utf-8"),
                dangerous + "\n",
            )
            self.assertFalse(unexpected.exists())

    def test_legacy_openclaw_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            stage = create_stage(base)
            (stage / "filesystems/openclaw-legacy-raw.tar.zst").write_bytes(b"legacy")
            write_checksums(stage)
            with self.assertRaises(backup_safety.SafetyError):
                prepare_runtime.prepare(stage, base / "runtime")

    def test_nonempty_runtime_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            stage = create_stage(base)
            runtime = base / "runtime"
            runtime.mkdir()
            (runtime / "owner-data").write_text("keep", encoding="utf-8")
            with self.assertRaises(backup_safety.SafetyError):
                prepare_runtime.prepare(stage, runtime)
            self.assertEqual(
                (runtime / "owner-data").read_text(encoding="utf-8"), "keep"
            )


if __name__ == "__main__":
    unittest.main()
