#!/usr/bin/env python3
"""Expose a minimal read-only Docker Engine API over a private Unix socket."""

from __future__ import annotations

import argparse
import os
import re
import select
import signal
import socket
import stat
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DEFAULT_LISTEN = Path(
    "/run/dockerswarm-observability/docker-readonly.sock"
)
DEFAULT_UPSTREAM = Path("/var/run/docker.sock")
MAX_HEADER_BYTES = 65536
MAX_TARGET_BYTES = 16384
CONTAINER_ID_RE = re.compile(r"/containers/[a-f0-9]{12,64}")
API_VERSION_RE = re.compile(r"^/v[0-9]+\.[0-9]+(?=/)")
HEADER_NAME_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
QUERY_KEYS = {
    "/containers/json": {
        "all",
        "before",
        "filters",
        "limit",
        "since",
        "size",
    },
    "/networks": {"filters"},
}
LOG_QUERY_KEYS = {
    "details",
    "follow",
    "since",
    "stderr",
    "stdout",
    "tail",
    "timestamps",
    "until",
}


class RequestError(RuntimeError):
    """A Docker API request is malformed or outside the read-only allowlist."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def normalize_path(path: str) -> str:
    """Remove only Docker's conventional numeric API-version prefix."""
    return API_VERSION_RE.sub("", path, count=1)


def validate_request(method: str, target: str) -> str:
    """Return the normalized path if an Engine API request is allowlisted."""
    if method not in {"GET", "HEAD"}:
        raise RequestError(403, "only GET and HEAD requests are permitted")
    try:
        target_bytes = target.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RequestError(400, "request target must be ASCII") from exc
    if len(target_bytes) > MAX_TARGET_BYTES:
        raise RequestError(414, "request target is too long")
    try:
        parsed = urlsplit(target)
    except ValueError as exc:
        raise RequestError(400, "invalid request target") from exc
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise RequestError(400, "only origin-form request targets are accepted")
    normalized = normalize_path(parsed.path)
    try:
        query = parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=32,
        )
    except ValueError as exc:
        raise RequestError(400, "invalid query string") from exc

    allowed_query_keys: set[str] | None = None
    if normalized in {"/_ping", "/version"}:
        allowed_query_keys = set()
    elif normalized in QUERY_KEYS:
        allowed_query_keys = QUERY_KEYS[normalized]
    elif CONTAINER_ID_RE.fullmatch(normalized.removesuffix("/json")) and (
        normalized.endswith("/json")
    ):
        allowed_query_keys = {"size"}
    elif CONTAINER_ID_RE.fullmatch(normalized.removesuffix("/logs")) and (
        normalized.endswith("/logs")
    ):
        allowed_query_keys = LOG_QUERY_KEYS

    if allowed_query_keys is None:
        raise RequestError(403, "Docker API endpoint is not allowlisted")
    if not set(query).issubset(allowed_query_keys):
        raise RequestError(403, "Docker API query parameter is not allowlisted")
    if any(len(values) != 1 for values in query.values()):
        raise RequestError(400, "duplicate Docker API query parameter")
    return normalized


def parse_headers(raw: bytes) -> tuple[str, str, str, list[tuple[str, str]]]:
    """Parse one bounded HTTP request header without accepting a body."""
    try:
        text = raw.decode("iso-8859-1")
    except UnicodeDecodeError as exc:
        raise RequestError(400, "invalid HTTP request encoding") from exc
    lines = text.split("\r\n")
    if len(lines) < 2 or lines[-2:] != ["", ""]:
        raise RequestError(400, "HTTP headers must use CRLF")
    request_parts = lines[0].split(" ")
    if len(request_parts) != 3:
        raise RequestError(400, "invalid HTTP request line")
    method, target, version = request_parts
    if version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise RequestError(400, "unsupported HTTP version")

    headers: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in lines[1:-2]:
        if not line or line[0].isspace() or ":" not in line:
            raise RequestError(400, "invalid HTTP header")
        name, value = line.split(":", 1)
        lower_name = name.lower()
        if (
            not HEADER_NAME_RE.fullmatch(name)
            or lower_name in seen
            or "\x00" in value
        ):
            raise RequestError(400, "invalid or duplicate HTTP header")
        seen.add(lower_name)
        headers.append((name, value.strip()))

    header_map = {name.lower(): value for name, value in headers}
    if "transfer-encoding" in header_map or "upgrade" in header_map:
        raise RequestError(400, "request framing or protocol upgrade denied")
    if header_map.get("content-length", "0") != "0":
        raise RequestError(400, "request bodies are denied")
    validate_request(method, target)
    return method, target, version, headers


def read_request(client: socket.socket) -> bytes:
    """Read exactly one bounded header and reject bodies or pipelining."""
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = client.recv(4096)
        if not chunk:
            raise RequestError(400, "client closed before request headers")
        buffer.extend(chunk)
        if len(buffer) > MAX_HEADER_BYTES:
            raise RequestError(431, "request headers are too large")
    marker = buffer.index(b"\r\n\r\n") + 4
    if buffer[marker:]:
        raise RequestError(400, "request body or pipelining is denied")
    return bytes(buffer[:marker])


def sanitized_request(raw: bytes) -> tuple[bytes, str]:
    """Validate a request and force the upstream to close after one response."""
    method, target, version, headers = parse_headers(raw)
    normalized = validate_request(method, target)
    output = [f"{method} {target} {version}\r\n".encode("ascii")]
    for name, value in headers:
        lower_name = name.lower()
        if (
            lower_name in HOP_BY_HOP_HEADERS
            or lower_name in {"content-length", "host"}
        ):
            continue
        output.append(f"{name}: {value}\r\n".encode("iso-8859-1"))
    output.extend(
        [
            b"Host: docker\r\n",
            b"Connection: close\r\n",
            b"\r\n",
        ]
    )
    return b"".join(output), normalized


def send_error(client: socket.socket, status: int, message: str) -> None:
    """Return a small non-sensitive HTTP error and close the connection."""
    reason = {
        400: "Bad Request",
        403: "Forbidden",
        414: "URI Too Long",
        431: "Request Header Fields Too Large",
        502: "Bad Gateway",
    }.get(status, "Error")
    body = f"{reason}: {message}\n".encode()
    response = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body
    try:
        client.sendall(response)
    except OSError:
        pass


def relay_response(client: socket.socket, upstream: socket.socket) -> None:
    """Relay one response and detect a client disconnect during log streaming."""
    while True:
        readable, _, exceptional = select.select(
            [client, upstream],
            [],
            [client, upstream],
            30,
        )
        if exceptional:
            return
        if client in readable:
            try:
                unexpected = client.recv(1)
            except OSError:
                return
            if not unexpected:
                return
            return
        if upstream in readable:
            chunk = upstream.recv(65536)
            if not chunk:
                return
            client.sendall(chunk)


def handle_client(client: socket.socket, upstream_path: Path) -> None:
    """Validate and proxy one connection to the real daemon socket."""
    with client:
        client.settimeout(15)
        try:
            request, _normalized = sanitized_request(read_request(client))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
                upstream.settimeout(15)
                upstream.connect(str(upstream_path))
                upstream.sendall(request)
                upstream.settimeout(None)
                client.settimeout(None)
                relay_response(client, upstream)
        except RequestError as exc:
            print(f"denied Docker API request: {exc}", file=sys.stderr)
            send_error(client, exc.status, str(exc))
        except OSError as exc:
            print(f"Docker API proxy failure: {exc}", file=sys.stderr)
            send_error(client, 502, "upstream Docker API unavailable")


def prepare_listener(path: Path) -> socket.socket:
    """Create a root-only Unix listener without replacing arbitrary files."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        current_mode = path.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(current_mode):
            raise RuntimeError(f"refusing to replace non-socket path: {path}")
        path.unlink()
    os.umask(0o077)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(64)
    listener.settimeout(0.5)
    return listener


def serve(listen_path: Path, upstream_path: Path) -> int:
    """Run the restricted proxy until SIGINT or SIGTERM."""
    if not stat.S_ISSOCK(upstream_path.stat().st_mode):
        raise RuntimeError(f"Docker upstream is not a Unix socket: {upstream_path}")
    stopped = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    listener = prepare_listener(listen_path)
    try:
        with listener:
            while not stopped.is_set():
                try:
                    client, _ = listener.accept()
                except TimeoutError:
                    continue
                threading.Thread(
                    target=handle_client,
                    args=(client, upstream_path),
                    daemon=True,
                ).start()
    finally:
        try:
            if stat.S_ISSOCK(listen_path.lstat().st_mode):
                listen_path.unlink()
        except FileNotFoundError:
            pass
    return 0


def probe(listen_path: Path) -> int:
    """Verify that the restricted listener can reach Docker's read-only ping."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(listen_path))
        client.sendall(
            b"GET /_ping HTTP/1.1\r\n"
            b"Host: docker\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        response = bytearray()
        while chunk := client.recv(4096):
            response.extend(chunk)
    if not response.startswith(b"HTTP/1.1 200"):
        raise RuntimeError("read-only Docker API proxy ping failed")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", type=Path, default=DEFAULT_LISTEN)
    parser.add_argument("--upstream", type=Path, default=DEFAULT_UPSTREAM)
    parser.add_argument("--probe", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.probe:
            return probe(args.listen)
        return serve(args.listen, args.upstream)
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
