#!/usr/bin/env python3
"""Expose one internal observability UI on local loopback over SSH stdio."""

from __future__ import annotations

import argparse
import os
import re
import signal
import socket
import subprocess
import sys
import threading

REMOTE_HELPER = "/usr/local/libexec/dockerswarm-observability-forward"
SERVICE_PORTS = {
    "grafana": 3000,
    "prometheus": 9090,
    "alertmanager": 9093,
    "loki": 3100,
    "alloy": 12345,
}
REMOTE_HOST_RE = re.compile(r"[a-zA-Z0-9_.@:-]+")


class TunnelError(RuntimeError):
    """The local stdio transport cannot be started safely."""


def ssh_command(remote_host: str, service: str) -> list[str]:
    """Build the fixed remote command; OpenSSH TCP forwarding is not used."""
    if (
        not REMOTE_HOST_RE.fullmatch(remote_host)
        or remote_host.startswith("-")
    ):
        raise TunnelError("USER@HOST contains unsupported characters")
    if service not in SERVICE_PORTS:
        raise TunnelError(f"unsupported observability service: {service}")
    return [
        "ssh",
        "-T",
        "--",
        remote_host,
        "sudo",
        "-n",
        REMOTE_HELPER,
        "--stdio",
        service,
    ]


def upload(client: socket.socket, process: subprocess.Popen[bytes]) -> None:
    """Copy the local client request stream to the SSH command."""
    assert process.stdin is not None
    try:
        while chunk := client.recv(65536):
            process.stdin.write(chunk)
            process.stdin.flush()
    except (BrokenPipeError, ConnectionError, OSError):
        pass
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass


def proxy_connection(
    client: socket.socket,
    remote_host: str,
    service: str,
    children: set[subprocess.Popen[bytes]],
    children_lock: threading.Lock,
) -> None:
    """Give each local TCP connection one isolated SSH stdio command."""
    command = ssh_command(remote_host, service)
    with client:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
        )
        with children_lock:
            children.add(process)
        transfer = threading.Thread(
            target=upload,
            args=(client, process),
            daemon=True,
        )
        transfer.start()
        assert process.stdout is not None
        try:
            while chunk := os.read(process.stdout.fileno(), 65536):
                client.sendall(chunk)
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            transfer.join(timeout=1)
            with children_lock:
                children.discard(process)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bind local loopback and transport each connection through an "
            "SSH command's stdin/stdout."
        )
    )
    parser.add_argument("remote_host", metavar="USER@HOST")
    parser.add_argument("service", choices=sorted(SERVICE_PORTS))
    parser.add_argument("local_port", nargs="?", type=int)
    args = parser.parse_args()
    if args.local_port is None:
        args.local_port = SERVICE_PORTS[args.service]
    if not 1024 <= args.local_port <= 65535:
        parser.error("LOCAL_PORT must be between 1024 and 65535")
    try:
        ssh_command(args.remote_host, args.service)
    except TunnelError as exc:
        parser.error(str(exc))
    return args


def main() -> int:
    args = parse_args()
    stopped = threading.Event()
    children: set[subprocess.Popen[bytes]] = set()
    children_lock = threading.Lock()

    def request_stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", args.local_port))
            listener.listen(32)
            listener.settimeout(0.5)
            print(
                f"Open http://127.0.0.1:{args.local_port}; "
                "Ctrl-C closes local access.",
                flush=True,
            )
            while not stopped.is_set():
                try:
                    client, _address = listener.accept()
                except TimeoutError:
                    continue
                threading.Thread(
                    target=proxy_connection,
                    args=(
                        client,
                        args.remote_host,
                        args.service,
                        children,
                        children_lock,
                    ),
                    daemon=True,
                ).start()
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        with children_lock:
            running = list(children)
        for process in running:
            if process.poll() is None:
                process.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
