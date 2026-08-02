#!/usr/bin/env python3
"""Proxy one SSH command's stdio into an internal Swarm task namespace."""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import threading
from pathlib import Path

SERVICE_PORTS = {
    "grafana": 3000,
    "prometheus": 9090,
    "alertmanager": 9093,
    "loki": 3100,
    "alloy": 12345,
}
CONTAINER_RE = re.compile(r"[a-f0-9]{12,64}")


class ForwardError(RuntimeError):
    """The requested internal stdio transport cannot be created safely."""


def find_task_pid(service: str) -> int:
    """Resolve exactly one running task for the allowlisted service."""
    service_name = f"observability_{service}"
    completed = subprocess.run(
        [
            "/usr/bin/docker",
            "ps",
            "--filter",
            f"label=com.docker.swarm.service.name={service_name}",
            "--filter",
            "status=running",
            "--format",
            "{{.ID}}",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    container_ids = [
        value.strip()
        for value in completed.stdout.splitlines()
        if value.strip()
    ]
    if (
        completed.returncode != 0
        or len(container_ids) != 1
        or not CONTAINER_RE.fullmatch(container_ids[0])
    ):
        raise ForwardError(f"expected one running task for {service_name}")

    inspected = subprocess.run(
        [
            "/usr/bin/docker",
            "inspect",
            "--format",
            "{{.State.Pid}}",
            container_ids[0],
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    value = inspected.stdout.strip()
    if (
        inspected.returncode != 0
        or not value.isdigit()
        or int(value) <= 1
    ):
        raise ForwardError(f"cannot resolve the task namespace for {service}")
    pid = int(value)
    if not Path(f"/proc/{pid}/ns/net").exists():
        raise ForwardError(f"network namespace disappeared for {service}")
    return pid


def copy_stdin(upstream: socket.socket) -> None:
    """Copy SSH command stdin and half-close the internal connection."""
    try:
        while chunk := os.read(sys.stdin.fileno(), 65536):
            upstream.sendall(chunk)
    except (BrokenPipeError, ConnectionError, OSError):
        pass
    finally:
        try:
            upstream.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def proxy_stdio(service: str) -> None:
    """Enter the task netns and proxy only this command's stdin/stdout."""
    pid = find_task_pid(service)
    with Path(f"/proc/{pid}/ns/net").open("rb") as namespace:
        os.setns(namespace.fileno(), os.CLONE_NEWNET)

    with socket.create_connection(
        ("127.0.0.1", SERVICE_PORTS[service]),
        timeout=5,
    ) as upstream:
        upstream.settimeout(None)
        upload = threading.Thread(
            target=copy_stdin,
            args=(upstream,),
            daemon=True,
        )
        upload.start()
        try:
            while chunk := upstream.recv(65536):
                view = memoryview(chunk)
                while view:
                    written = os.write(sys.stdout.fileno(), view)
                    view = view[written:]
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            try:
                upstream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            upload.join(timeout=1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stdio",
        dest="service",
        choices=sorted(SERVICE_PORTS),
        required=True,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if os.geteuid() != 0:
            raise ForwardError("the namespace helper must run as root")
        proxy_stdio(args.service)
    except (OSError, ForwardError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
