#!/usr/bin/env python3
"""Read the host-only source of the managed CrowdSec allowlist.

The IP addresses that CrowdSec must never ban are personal data and this
repository is public, so they live only in a root-only file on the host
(`host_security_crowdsec_allowlist_source` in config/host-security.yml). The
file is optional: when it is absent this script reports `present: false`.

Format: one IPv4 or IPv6 address or network per line. Blank lines and lines
whose first non-blank character is `#` are ignored. Every entry must be
globally routable, and a network may be no wider than /24 (IPv4) or /48
(IPv6): a wider entry would exempt strangers from every ban, SSH included.

The file is opened without following a link, relative to its directory, which
is itself opened without following a link; both are checked on the open
descriptor (type, exact owner and mode, one link, bounded size) before a
single byte is read. On success the script prints
`{"present": bool, "entries": [...]}` on stdout, with every entry in the
canonical form cscli stores: a bare address for a single host, CIDR
otherwise. On failure it prints a reason on stderr that never contains the
file's content, so a caller may show it without leaking an address.
"""

from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import os
import stat
import sys
from pathlib import Path

MAX_SOURCE_BYTES = 4096
MAX_ENTRIES = 16
MIN_PREFIX = {4: 24, 6: 48}
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600


class AllowlistSourceError(RuntimeError):
    """The allowlist source is unsafe or malformed."""


def _open_directory(directory: Path, owner_uid: int, owner_gid: int) -> int | None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(directory, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AllowlistSourceError(
            "the source directory is not a directory or is a symbolic link"
        ) from exc
    status = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != owner_uid
        or status.st_gid != owner_gid
        or stat.S_IMODE(status.st_mode) != DIRECTORY_MODE
    ):
        os.close(descriptor)
        raise AllowlistSourceError(
            f"the source directory must be a {DIRECTORY_MODE:04o} directory "
            f"owned by {owner_uid}:{owner_gid}"
        )
    return descriptor


def read_source(
    path: Path,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> bytes | None:
    """Return the raw source, or None when it does not exist."""
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise AllowlistSourceError("the source path must be absolute")
    directory = _open_directory(path.parent, owner_uid, owner_gid)
    if directory is None:
        return None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            descriptor = os.open(path.name, flags, dir_fd=directory)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise AllowlistSourceError("the source is a symbolic link") from exc
            raise AllowlistSourceError("the source cannot be opened") from exc
    finally:
        os.close(directory)
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != owner_uid
            or status.st_gid != owner_gid
            or stat.S_IMODE(status.st_mode) != FILE_MODE
            or status.st_nlink != 1
        ):
            raise AllowlistSourceError(
                f"the source must be a regular {FILE_MODE:04o} file owned by "
                f"{owner_uid}:{owner_gid} with a single link"
            )
        if status.st_size > MAX_SOURCE_BYTES:
            raise AllowlistSourceError(f"the source exceeds {MAX_SOURCE_BYTES} bytes")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(MAX_SOURCE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > MAX_SOURCE_BYTES:
        raise AllowlistSourceError(f"the source exceeds {MAX_SOURCE_BYTES} bytes")
    return raw


def canonical_entry(token: str, line_number: int) -> str:
    """Validate one entry and return the value cscli should store."""
    try:
        network = ipaddress.ip_network(token, strict=True)
    except ValueError as exc:
        raise AllowlistSourceError(
            f"line {line_number}: not an IP address or a network without host bits"
        ) from exc
    if (
        isinstance(network, ipaddress.IPv6Network)
        and network.network_address.ipv4_mapped is not None
    ):
        raise AllowlistSourceError(
            f"line {line_number}: write an IPv4-mapped address as plain IPv4"
        )
    if network.prefixlen < MIN_PREFIX[network.version]:
        raise AllowlistSourceError(
            f"line {line_number}: an IPv{network.version} network may be no "
            f"wider than /{MIN_PREFIX[network.version]}"
        )
    if not network.is_global:
        raise AllowlistSourceError(
            f"line {line_number}: the entry is not globally routable"
        )
    if network.prefixlen == network.max_prefixlen:
        return str(network.network_address)
    return str(network)


def parse_source(raw: bytes) -> list[str]:
    """Return the canonical entries of a source, in file order."""
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AllowlistSourceError("the source must be ASCII") from exc
    if "\r" in text or "\x00" in text:
        raise AllowlistSourceError("the source contains unsafe bytes")
    entries: list[str] = []
    for line_number, line in enumerate(text.split("\n"), start=1):
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        if any(character.isspace() for character in token):
            raise AllowlistSourceError(
                f"line {line_number}: one entry per line, without comments after it"
            )
        entry = canonical_entry(token, line_number)
        if entry in entries:
            raise AllowlistSourceError(f"line {line_number}: duplicate entry")
        entries.append(entry)
    if len(entries) > MAX_ENTRIES:
        raise AllowlistSourceError(f"the source holds more than {MAX_ENTRIES} entries")
    return entries


def load(
    path: Path,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> dict[str, object]:
    raw = read_source(path, owner_uid, owner_gid)
    if raw is None:
        return {"present": False, "entries": []}
    return {"present": True, "entries": parse_source(raw)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    arguments = parser.parse_args(argv)
    try:
        document = load(arguments.source)
    except AllowlistSourceError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(document, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
