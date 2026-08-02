from __future__ import annotations

import grp
import os
import pwd
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

MIGRATION_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = MIGRATION_ROOT / "scripts" / "install_traefik_acme.sh"


class InstallTraefikAcmeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = INSTALL_SCRIPT.read_text(encoding="utf-8")

    @staticmethod
    def unused_id(lookup) -> int:
        for identifier in range(60000, 1_000_000):
            try:
                lookup(identifier)
            except KeyError:
                return identifier
        raise AssertionError("No hay UID/GID libre para la prueba")

    def test_numeric_owner_does_not_require_passwd_or_group_entries(self) -> None:
        self.assertNotIn('-o "${traefik_uid}"', self.script)
        self.assertNotIn('-g "${traefik_gid}"', self.script)
        ownership_commands = re.findall(
            r'chown -- "\+\$\{traefik_uid\}:\+\$\{traefik_gid\}" '
            r'"\$\{(?:target_directory|temporary)\}"',
            self.script,
        )
        self.assertEqual(len(ownership_commands), 2)

        unused_uid = self.unused_id(pwd.getpwuid)
        unused_gid = self.unused_id(grp.getgrgid)
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            fake_bin = temporary_root / "bin"
            fake_bin.mkdir()
            argument_log = temporary_root / "chown-arguments"
            fake_chown = fake_bin / "chown"
            fake_chown.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" >"${CHOWN_ARGUMENT_LOG}"\n',
                encoding="utf-8",
            )
            fake_chown.chmod(0o700)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "CHOWN_ARGUMENT_LOG": str(argument_log),
                    "traefik_uid": str(unused_uid),
                    "traefik_gid": str(unused_gid),
                    "target_directory": str(temporary_root / "traefik"),
                }
            )
            subprocess.run(
                ["bash", "-c", ownership_commands[0]],
                env=environment,
                check=True,
            )
            self.assertEqual(
                argument_log.read_text(encoding="utf-8").splitlines(),
                [
                    "--",
                    f"+{unused_uid}:+{unused_gid}",
                    str(temporary_root / "traefik"),
                ],
            )

    def test_destination_remains_no_clobber_and_mode_checked(self) -> None:
        self.assertIn("mv --no-clobber", self.script)
        self.assertIn('"750:${traefik_uid}:${traefik_gid}"', self.script)
        self.assertIn('"600:${traefik_uid}:${traefik_gid}"', self.script)


if __name__ == "__main__":
    unittest.main()
