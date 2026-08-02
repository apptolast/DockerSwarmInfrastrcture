#!/usr/bin/env python3
"""Validate an exact, option-free OpenSSH public-key allowlist."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ALLOWED_KEY_TYPES = {
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}


class AuthorizedKeysError(RuntimeError):
    """The authorized-key allowlist is unsafe or malformed."""


def inspect_line(line: str, ssh_keygen: str) -> tuple[str, int]:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="dockerswarm-authorized-key-",
    ) as candidate:
        candidate.write(f"{line}\n")
        candidate.flush()
        completed = subprocess.run(
            [ssh_keygen, "-l", "-E", "sha256", "-f", candidate.name],
            check=False,
            capture_output=True,
            text=True,
        )
    if completed.returncode != 0:
        raise AuthorizedKeysError("ssh-keygen rejected an authorized key")
    fields = completed.stdout.strip().split()
    if (
        len(fields) < 2
        or not fields[0].isdigit()
        or not fields[1].startswith("SHA256:")
    ):
        raise AuthorizedKeysError("ssh-keygen returned no SHA-256 fingerprint")
    return fields[1], int(fields[0])


def validate(path: Path, ssh_keygen: str) -> list[str]:
    try:
        path_state = path.lstat()
    except OSError as exc:
        raise AuthorizedKeysError("cannot inspect authorized-key file") from exc
    if not stat.S_ISREG(path_state.st_mode):
        raise AuthorizedKeysError("authorized-key input must be a regular file")

    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AuthorizedKeysError("cannot read authorized-key file") from exc
    if "\r" in raw or "\x00" in raw:
        raise AuthorizedKeysError("authorized-key input contains unsafe bytes")
    lines = raw.splitlines()
    if not lines or any(not line or line != line.strip() for line in lines):
        raise AuthorizedKeysError(
            "authorized-key input has blank lines or surrounding whitespace"
        )

    identities: set[tuple[str, bytes]] = set()
    fingerprints: list[str] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 2:
            raise AuthorizedKeysError("authorized key has too few fields")
        key_type, encoded_key = fields[:2]
        if key_type not in ALLOWED_KEY_TYPES:
            raise AuthorizedKeysError(
                "authorized-key options or key type are not allowed"
            )
        try:
            decoded_key = base64.b64decode(encoded_key, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise AuthorizedKeysError("authorized key is not valid base64") from exc
        if len(decoded_key) < 32:
            raise AuthorizedKeysError("authorized key payload is truncated")
        identity = (key_type, decoded_key)
        if identity in identities:
            raise AuthorizedKeysError("duplicate authorized-key identity")
        identities.add(identity)
        fingerprint, key_bits = inspect_line(line, ssh_keygen)
        if key_type == "ssh-rsa" and key_bits < 2048:
            raise AuthorizedKeysError(
                "RSA authorized keys must be at least 2048 bits"
            )
        fingerprints.append(fingerprint)

    if len(set(fingerprints)) != len(fingerprints):
        raise AuthorizedKeysError("duplicate authorized-key fingerprint")
    return fingerprints


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--ssh-keygen", default="/usr/bin/ssh-keygen")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        fingerprints = validate(args.path, args.ssh_keygen)
    except AuthorizedKeysError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps(
                {
                    "count": len(fingerprints),
                    "fingerprints": fingerprints,
                    "allowlist_sha256": hashlib.sha256(
                        args.path.read_bytes()
                    ).hexdigest(),
                },
                sort_keys=True,
            )
        )
    else:
        print(f"Authorized-key allowlist passed: {len(fingerprints)} key(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
