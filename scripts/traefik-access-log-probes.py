#!/usr/bin/env python3
"""Prove that Traefik wrote the edge role's login probes to its access log.

The edge role runs this on the host after it probed every basicAuth route
without credentials. It reads the JSON access log that CrowdSec tails from
the byte offset recorded before the probes (from the start if the file was
truncated by rotation meanwhile) and counts, for each HOST=ROUTER pair, the
401 lines whose RequestHost and RouterName match. It prints only those
counts as JSON, never a log line, and exits 0 only when every pair has at
least one line.

Usage: traefik-access-log-probes.py PATH OFFSET HOST=ROUTER [HOST=ROUTER...]
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

# Only the tail written since the probes matters; the bound keeps the read
# cheap even when the file holds a whole day of a flood.
MAX_READ_BYTES = 16 * 1024 * 1024
HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
ROUTER = re.compile(r"[a-z0-9-]+@[a-z]+")


class ProbeError(RuntimeError):
    """The arguments or the access log cannot prove the probes."""


def parse_pairs(raw_pairs: list[str]) -> list[tuple[str, str]]:
    if not raw_pairs:
        raise ProbeError("at least one HOST=ROUTER pair is required")
    pairs: list[tuple[str, str]] = []
    for raw in raw_pairs:
        host, separator, router = raw.partition("=")
        if separator != "=" or not HOST.fullmatch(host) or not ROUTER.fullmatch(router):
            raise ProbeError(f"invalid HOST=ROUTER pair: {raw!r}")
        if (host, router) in pairs:
            raise ProbeError(f"duplicate HOST=ROUTER pair: {raw!r}")
        pairs.append((host, router))
    return pairs


def read_tail(path: Path, offset: int) -> bytes:
    """Read what was appended since offset, without following a link."""
    if not path.is_absolute():
        raise ProbeError("the access log path must be absolute")
    if offset < 0:
        raise ProbeError("the offset must not be negative")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ProbeError(
            f"cannot open the access log without following links: {error.strerror}"
        ) from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise ProbeError("the access log must be a single-link regular file")
        size = status.st_size
        # A smaller file was truncated by logrotate after the offset was read.
        start = offset if offset <= size else 0
        skip_partial_line = False
        if size - start > MAX_READ_BYTES:
            start = size - MAX_READ_BYTES
            skip_partial_line = True
        os.lseek(descriptor, start, os.SEEK_SET)
        chunks = []
        remaining = size - start
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if skip_partial_line:
        _, _, data = data.partition(b"\n")
    return data


def count_probes(data: bytes, pairs: list[tuple[str, str]]) -> dict[str, int]:
    counts = {f"{host}={router}": 0 for host, router in pairs}
    for raw_line in data.splitlines():
        try:
            entry = json.loads(raw_line)
        except (UnicodeDecodeError, ValueError):
            # Only a torn last line or a foreign line can fail; neither counts.
            continue
        if not isinstance(entry, dict) or entry.get("DownstreamStatus") != 401:
            continue
        key = f"{entry.get('RequestHost')}={entry.get('RouterName')}"
        if key in counts:
            counts[key] += 1
    return counts


def main(argv: list[str]) -> int:
    try:
        if len(argv) < 3:
            raise ProbeError(
                "usage: traefik-access-log-probes.py PATH OFFSET HOST=ROUTER..."
            )
        if not argv[1].isdigit():
            raise ProbeError("the offset must be a non-negative integer")
        pairs = parse_pairs(argv[2:])
        counts = count_probes(read_tail(Path(argv[0]), int(argv[1])), pairs)
    except ProbeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(json.dumps(counts, sort_keys=True))
    missing = sorted(key for key, count in counts.items() if count == 0)
    if missing:
        print(
            "ERROR: the access log has no 401 line for " + ", ".join(missing),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
