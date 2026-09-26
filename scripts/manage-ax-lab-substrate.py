#!/usr/bin/env python3
"""Keep the AX lab's pinned Substrate and AX images, and install Substrate.

config/ax-lab.yml pins every image by the digest of its manifest. The local
registry serves each one as <name>:<version>, the name that `ate-setup
--image-repo` looks up (cmd/ate-setup/differences.md, «Image sources», at the
pinned commit), and a root-only OCI image layout keeps a byte-exact copy of
all of them. Images move between the two only through the registry HTTP API,
never with `docker pull` and `docker push`, which store and re-serialise
manifests and so change their digests. Every manifest and every blob is
hashed on the way and must equal its descriptor.

Three image sets share the layout and the registry: Substrate's, the
default, AX's (--image-set ax) and the AX web panel's (--image-set web). This
host never builds the last two: AX's are only ever seeded from the manual
lab, the panel's from the OCI layout its CI workflow uploads, and both are
backed up and restored.

Subcommands:

image-status    Read-only: which pinned images the registry serves under
                their tag, and which the layout holds complete.
cluster-status  Read-only: the node's Substrate version label, the identity
                (never the data) of the objects ate-setup creates only once,
                and every workload of its three namespaces with its images
                and rollout state (never its environment).
export          Copy pinned images from a registry into the layout: the seed
                from the manual lab and the backup of the lab's registry.
import          Copy them from the layout into the registry: the restore.
seed-layout     Copy one pinned image from an external OCI image layout, such
                as the ax-web workflow's artifact, into the layout: the
                manifest must hash to the pin and every blob to its
                descriptor.
forget          Drop the layout's entry for <name>:<version> of an old pin, by
                its exact digest, so that a re-pin under the same version
                can be seeded, built or copied again.
build           The explicit fallback: build one missing image from the
                pinned checkout in the bounded, unprivileged toolbox, into
                the layout.
install         Run the pinned ate-setup image once: deploy ate-system.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

MODULE_PATH = "github.com/agent-substrate/substrate"
# The images ko builds from ./cmd/<name> for the lab: the five ate-setup
# installs, the gVisor worker image of the WorkerPool, and ate-setup itself.
IMAGE_NAMES = (
    "ateapi",
    "atecontroller",
    "atelet",
    "atenet",
    "podcertcontroller",
    "ateom-gvisor",
    "ate-setup",
)
INSTALLER = "ate-setup"
# AX at the pinned commit with the #375 patch. ko built the controller and the
# server with its default namer; the runner and the agents images were pushed
# under their own names. Nothing here builds any of them.
AX_MODULE_PATH = "github.com/google/ax"
AX_IMAGE_NAMES = ("ax-controller", "ax-server", "ax-task-runner", "ax-agents")
AX_KO_IMAGES = ("ax-controller", "ax-server")
# The AX web panel (images/ax-web): built by ko in CI with --bare, so its
# name is the repository's own, and seeded from that build's OCI layout.
WEB_MODULE_PATH = "apptolast.com/ax-web"
WEB_IMAGE_NAMES = ("ax-web",)
# Per image set: its Go module, its images and those ko named by md5.
IMAGE_SETS = {
    "substrate": (MODULE_PATH, IMAGE_NAMES, IMAGE_NAMES),
    "ax": (AX_MODULE_PATH, AX_IMAGE_NAMES, AX_KO_IMAGES),
    "web": (WEB_MODULE_PATH, WEB_IMAGE_NAMES, ()),
}
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
MANIFEST_TYPES = (OCI_MANIFEST, DOCKER_MANIFEST)
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
REF_NAME = "org.opencontainers.image.ref.name"
LAYOUT_FILE = b'{"imageLayoutVersion":"1.0.0"}'
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_INDEX_BYTES = 1024 * 1024
MAX_BLOB_BYTES = 4 * 1024 * 1024 * 1024
CHUNK = 1024 * 1024
DIGEST_RE = re.compile(r"sha256:([a-f0-9]{64})")
NAME_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
HOST_PORT_RE = re.compile(r"(127\.0\.0\.1|localhost):([1-9][0-9]{0,4})")
# The registry client speaks plain HTTP, so it only ever talks to loopback.
REQUEST_TIMEOUT_SECONDS = 120
# ate-setup's three namespaces (cmd/ate-setup/internal/steps/env.go and the
# kind overlay's otel collector, manifests/ate-install/kind/otel-collector.yaml).
NAMESPACES = ("ate-system", "otel-system", "podcertificate-controller-system")
# What ate-setup creates only when it is missing and never reconciles
# (cmd/ate-setup/internal/steps/prereqs.go lines 33-63, create.go lines
# 33-41, 204-287): generated key pools, Opaque Secrets holding the key `pool`,
# the root derived from actor-id-ca-pool, and the authentication ConfigMap.
# ate-api-server-envvars is reconciled on every run (prereqs.go line 51), so
# it is not here, and the shell installer's ate-api-server-secret-envvars is
# never created by ate-setup.
CREATE_ONCE = (
    ("ate-system", "Secret", "actor-id-jwt-pool"),
    ("ate-system", "Secret", "actor-id-ca-pool"),
    ("ate-system", "Secret", "actor-id-ca-certs"),
    ("podcertificate-controller-system", "Secret", "service-dns-ca-pool"),
    ("podcertificate-controller-system", "Secret", "pod-identity-ca-pool"),
    ("ate-system", "ConfigMap", "ate-api-authentication"),
)
# ate-setup re-applies these with force although their pod templates are
# immutable (a StatefulSet's volumeClaimTemplates, a Job's template): after
# any run each must be the same object at the same generation.
IMMUTABLE_TEMPLATES = (
    "ate-system/StatefulSet/postgres",
    "ate-system/Job/rustfs-bucket-init",
)
WORKLOAD_KINDS = "deployments.apps,statefulsets.apps,daemonsets.apps,jobs.batch"
# Only identities, rollout counters and images: never a pod template's
# environment, which carries upstream's static S3 key pair inline.
WORKLOAD_FIELDS = (
    ".kind",
    ".metadata.name",
    ".metadata.uid",
    ".metadata.generation",
    ".status.observedGeneration",
    ".spec.replicas",
    ".status.replicas",
    ".status.readyReplicas",
    ".status.updatedReplicas",
    ".status.availableReplicas",
    ".status.currentRevision",
    ".status.updateRevision",
    ".status.desiredNumberScheduled",
    ".status.updatedNumberScheduled",
    ".status.numberAvailable",
    ".status.succeeded",
    ".spec.template.spec.initContainers[*].image",
    ".spec.template.spec.containers[*].image",
)
WORKLOAD_JSONPATH = (
    "{range .items[*]}"
    + '{"\\t"}'.join("{" + path + "}" for path in WORKLOAD_FIELDS)
    + '{"\\n"}{end}'
)
CONTAINER_ID_RE = re.compile(r"[a-f0-9]{64}")
BUILD_CONTAINER_PREFIX = "ax-lab-build-"
INSTALL_CONTAINER = "ax-lab-ate-setup"
CONTAINER_LABELS = ("com.apptolast.managed-by=ansible",)
# Upstream installs the kind images for the host architecture
# (hack/install-ate-kind.sh line 30); the lab host is linux/amd64.
PLATFORM = "linux/amd64"
SWARM_STATES_UNDER_LOCK = {"active", "pending", "locked", "error"}


class SubstrateError(RuntimeError):
    """The pinned images or the install cannot be proven."""


class PathMissing(SubstrateError):
    """A directory on the way to a layout or cache does not exist."""


# --------------------------------------------------------------------------
# Arguments


def parse_pins(
    values: Sequence[str],
    *,
    allow_pending: bool = False,
    names: Sequence[str] = IMAGE_NAMES,
) -> dict:
    """NAME=sha256:<hex> pairs, each a known image of the set, each named once."""
    pins: dict[str, str | None] = {}
    for value in values:
        name, separator, digest = value.partition("=")
        if not separator or name not in names or name in pins:
            raise SubstrateError(f"--image {value!r} is not one known image")
        if allow_pending and digest == "pending":
            pins[name] = None
            continue
        if not DIGEST_RE.fullmatch(digest):
            raise SubstrateError(f"--image {name} needs a sha256 digest")
        pins[name] = digest
    if not pins:
        raise SubstrateError("at least one --image is required")
    return pins


def parse_registry(address: str) -> tuple[str, int]:
    match = HOST_PORT_RE.fullmatch(address)
    if match is None or not 1024 <= int(match[2]) <= 65535:
        raise SubstrateError(
            "the registry must be on loopback: 127.0.0.1:<port> or localhost:<port>"
        )
    return match[1], int(match[2])


def require_tag(tag: str) -> str:
    if not TAG_RE.fullmatch(tag):
        raise SubstrateError("the tag is not a valid image tag")
    return tag


def ko_md5_repository(name: str, module: str = MODULE_PATH) -> str:
    """ko's default repository name for ./cmd/<name> (packageWithMD5).

    ko v0.19.1 pkg/commands/options/publish.go, lines 109-113: the base of
    the import path, a dash and the md5 of the whole import path. The
    manual lab pushed with that namer, so its registry holds
    ateapi-752889f8b0bcdbee32172ac9fe056025 and so on, and for AX
    ax-controller-7ebf6094b73be08cb227c879d4802a93.
    """
    import_path = f"{module}/cmd/{name}"
    digest = hashlib.md5(import_path.encode(), usedforsecurity=False).hexdigest()
    return f"{name}-{digest}"


# --------------------------------------------------------------------------
# Manifests


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise SubstrateError(f"duplicate JSON key {key!r}")
        document[key] = value
    return document


def load_json(raw: bytes, context: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SubstrateError(f"{context} is not JSON") from error


def descriptor(value: Any, context: str) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("mediaType"), str)
        or not isinstance(value.get("digest"), str)
        or not DIGEST_RE.fullmatch(value["digest"])
        or type(value.get("size")) is not int
        or not 0 <= value["size"] <= MAX_BLOB_BYTES
    ):
        raise SubstrateError(f"{context} is not a sha256 descriptor")
    return value


def parse_manifest(raw: bytes, digest: str) -> tuple[str, list[dict[str, Any]]]:
    """A single-platform image manifest whose bytes hash to digest."""
    if len(raw) > MAX_MANIFEST_BYTES:
        raise SubstrateError(f"manifest {digest} is too large")
    if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
        raise SubstrateError(f"manifest bytes do not hash to {digest}")
    document = load_json(raw, f"manifest {digest}")
    if (
        not isinstance(document, dict)
        or document.get("schemaVersion") != 2
        or document.get("mediaType") not in MANIFEST_TYPES
        or "manifests" in document
    ):
        raise SubstrateError(f"{digest} is not a single-platform image manifest")
    layers = document.get("layers")
    if not isinstance(layers, list) or not layers:
        raise SubstrateError(f"manifest {digest} has no layers")
    blobs = [descriptor(document.get("config"), f"{digest} config")]
    blobs += [descriptor(layer, f"{digest} layer") for layer in layers]
    return document["mediaType"], blobs


# --------------------------------------------------------------------------
# Registry HTTP API (distribution spec), loopback only


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes = b""


class Registry:
    """A registry on loopback, spoken to over plain HTTP."""

    def __init__(self, address: str) -> None:
        self.host, self.port = parse_registry(address)

    def _connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(
            self.host, self.port, timeout=REQUEST_TIMEOUT_SECONDS
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: Any = None,
    ) -> Response:
        connection = self._connection()
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            result = Response(
                response.status,
                {key.lower(): value for key, value in response.getheaders()},
            )
            result.body = response.read(MAX_MANIFEST_BYTES + 1)
            return result
        except OSError as error:
            raise SubstrateError(
                f"cannot reach the registry at {self.host}:{self.port}: {error}"
            ) from error
        finally:
            connection.close()

    def head_manifest(self, repository: str, reference: str) -> str | None:
        response = self.request(
            "HEAD",
            f"/v2/{repository}/manifests/{reference}",
            headers={"Accept": ", ".join(MANIFEST_TYPES)},
        )
        if response.status == 404:
            return None
        digest = response.headers.get("docker-content-digest", "")
        if response.status != 200 or not DIGEST_RE.fullmatch(digest):
            raise SubstrateError(
                f"HEAD {repository}:{reference} answered {response.status}"
            )
        return digest

    def get_manifest(self, repository: str, reference: str) -> tuple[bytes, str]:
        response = self.request(
            "GET",
            f"/v2/{repository}/manifests/{reference}",
            headers={"Accept": ", ".join(MANIFEST_TYPES)},
        )
        if response.status != 200:
            raise SubstrateError(
                f"GET {repository}:{reference} answered {response.status}"
            )
        return response.body, response.headers.get("content-type", "")

    def blob_exists(self, repository: str, digest: str) -> bool:
        response = self.request("HEAD", f"/v2/{repository}/blobs/{digest}")
        if response.status not in (200, 404):
            raise SubstrateError(f"HEAD blob {digest} answered {response.status}")
        return response.status == 200

    def iter_blob(self, repository: str, blob: dict[str, Any]) -> Iterator[bytes]:
        """Stream one blob chunk by chunk, never holding it whole in memory.

        Its size is checked as it streams and its digest at the end, so a
        consumer must discard what it received when this raises:
        Layout.write_blob hashes on its own and unlinks its partial file.
        """
        connection = self._connection()
        try:
            try:
                connection.request("GET", f"/v2/{repository}/blobs/{blob['digest']}")
                response = connection.getresponse()
            except OSError as error:
                raise SubstrateError(
                    f"cannot reach the registry at {self.host}:{self.port}: {error}"
                ) from error
            if response.status != 200:
                raise SubstrateError(
                    f"GET blob {blob['digest']} answered {response.status}"
                )
            hasher = hashlib.sha256()
            count = 0
            while True:
                try:
                    chunk = response.read(CHUNK)
                except OSError as error:
                    raise SubstrateError(
                        f"reading blob {blob['digest']} failed: {error}"
                    ) from error
                if not chunk:
                    break
                count += len(chunk)
                if count > blob["size"]:
                    raise SubstrateError(
                        f"blob {blob['digest']} is larger than its size"
                    )
                hasher.update(chunk)
                yield chunk
            if (
                count != blob["size"]
                or "sha256:" + hasher.hexdigest() != blob["digest"]
            ):
                raise SubstrateError(
                    f"blob {blob['digest']} does not match its descriptor"
                )
        finally:
            connection.close()

    def upload_blob(
        self, repository: str, blob: dict[str, Any], chunks: Iterator[bytes]
    ) -> None:
        """POST, then one monolithic PUT that the registry checks by digest."""
        started = self.request("POST", f"/v2/{repository}/blobs/uploads/")
        location = started.headers.get("location", "")
        if started.status != 202 or not location:
            raise SubstrateError(f"upload of {blob['digest']} was not accepted")
        parts = urlsplit(location)
        if parts.netloc and parts.netloc not in (
            f"{self.host}:{self.port}",
            f"127.0.0.1:{self.port}",
            f"localhost:{self.port}",
        ):
            raise SubstrateError("the registry sent the upload to another host")
        if not parts.path.startswith(f"/v2/{repository}/blobs/uploads/"):
            raise SubstrateError("the registry sent an unexpected upload location")
        separator = "&" if parts.query else "?"
        query = f"?{parts.query}" if parts.query else ""
        target = f"{parts.path}{query}{separator}digest={blob['digest']}"
        finished = self.request(
            "PUT",
            target,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(blob["size"]),
            },
            body=IteratorReader(chunks),
        )
        if finished.status != 201:
            raise SubstrateError(
                f"upload of {blob['digest']} answered {finished.status}"
            )

    def put_manifest(
        self, repository: str, tag: str, raw: bytes, media_type: str
    ) -> str:
        response = self.request(
            "PUT",
            f"/v2/{repository}/manifests/{tag}",
            headers={"Content-Type": media_type, "Content-Length": str(len(raw))},
            body=raw,
        )
        digest = response.headers.get("docker-content-digest", "")
        if response.status != 201 or not DIGEST_RE.fullmatch(digest):
            raise SubstrateError(f"PUT {repository}:{tag} answered {response.status}")
        return digest


class IteratorReader:
    """A file-like body for http.client from an iterator of chunks.

    It keeps an offset into the current chunk, so each read copies only the
    bytes it returns, never the rest of the chunk.
    """

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self.chunks = iter(chunks)
        self.current = b""
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        parts = []
        wanted = size
        while size < 0 or wanted > 0:
            if self.offset >= len(self.current):
                try:
                    self.current = next(self.chunks)
                except StopIteration:
                    break
                self.offset = 0
                continue
            end = len(self.current)
            if size >= 0:
                end = min(end, self.offset + wanted)
                wanted -= end - self.offset
            parts.append(self.current[self.offset : end])
            self.offset = end
        return b"".join(parts)


# --------------------------------------------------------------------------
# OCI image layout, read and written without following links


def open_directory(path: Path, *, create_mode: int | None = None) -> int:
    """Walk path from / with O_NOFOLLOW, one component at a time.

    Every directory on the way must be owned by root or by this process and
    must not be writable by others unless it is sticky (like /tmp). With
    create_mode, a missing last component is created with that mode.
    """
    if not path.is_absolute() or ".." in path.parts:
        raise SubstrateError(f"{path} must be a normalized absolute path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor_fd = os.open("/", flags)
    try:
        parts = path.parts[1:]
        for index, part in enumerate(parts):
            try:
                child = os.open(part, flags, dir_fd=descriptor_fd)
            except FileNotFoundError as error:
                if create_mode is None or index != len(parts) - 1:
                    raise PathMissing(f"{path} does not exist") from error
                os.mkdir(part, create_mode, dir_fd=descriptor_fd)
                child = os.open(part, flags, dir_fd=descriptor_fd)
                os.fchmod(child, create_mode)
            except OSError as error:
                raise SubstrateError(
                    f"{path}: {part} is not a safe directory"
                ) from error
            os.close(descriptor_fd)
            descriptor_fd = child
            status = os.fstat(descriptor_fd)
            if status.st_uid not in (0, os.geteuid()):
                raise SubstrateError(f"{path}: {part} has another owner")
            if status.st_mode & 0o022 and not status.st_mode & stat.S_ISVTX:
                raise SubstrateError(f"{path}: {part} is writable by others")
        return descriptor_fd
    except BaseException:
        os.close(descriptor_fd)
        raise


def open_regular(
    name: str, dir_fd: int, *, strict: bool, max_size: int, context: str
) -> int:
    """TOCTOU-safe open: O_NOFOLLOW, then fstat on the open descriptor."""
    try:
        descriptor_fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd
        )
    except OSError as error:
        raise SubstrateError(f"{context}: cannot open safely: {error}") from error
    status = os.fstat(descriptor_fd)
    problem = None
    if not stat.S_ISREG(status.st_mode):
        problem = "is not a regular file"
    elif status.st_nlink != 1:
        problem = "has other hard links"
    elif status.st_uid != os.geteuid():
        problem = "has another owner"
    elif strict and (status.st_gid != os.getegid() or status.st_mode & 0o777 != 0o600):
        problem = "is not mode 0600 with this process's group"
    elif status.st_mode & 0o022:
        problem = "is writable by others"
    elif status.st_size > max_size:
        problem = "is too large"
    if problem is not None:
        os.close(descriptor_fd)
        raise SubstrateError(f"{context} {problem}")
    return descriptor_fd


def write_all(descriptor_fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor_fd, view) :]


def read_all(descriptor_fd: int) -> bytes:
    chunks = []
    while True:
        chunk = os.read(descriptor_fd, CHUNK)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


class Layout:
    """An OCI image layout: oci-layout, index.json and blobs/sha256/<hex>.

    strict is the backup this script writes: directories 0700 and files 0600,
    owned by this process. The relaxed form reads the layout ko writes inside
    the build container, where files keep ko's own modes.
    """

    def __init__(self, root: Path, *, strict: bool = True) -> None:
        self.root = root
        self.strict = strict
        self._fds: dict[str, int] = {}

    def close(self) -> None:
        for descriptor_fd in self._fds.values():
            os.close(descriptor_fd)
        self._fds.clear()

    def __enter__(self) -> Layout:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _check_directory(self, descriptor_fd: int, context: str) -> None:
        status = os.fstat(descriptor_fd)
        if status.st_uid != os.geteuid():
            raise SubstrateError(f"{context} has another owner")
        if self.strict and status.st_mode & 0o777 != 0o700:
            raise SubstrateError(f"{context} is not a 0700 directory")
        if status.st_mode & 0o022:
            raise SubstrateError(f"{context} is writable by others")

    def exists(self) -> bool:
        try:
            os.close(open_directory(self.root))
        except PathMissing:
            return False
        return True

    def open(self, *, create: bool = False) -> None:
        if self._fds:
            return
        mode = 0o700 if create else None
        root = open_directory(self.root, create_mode=mode)
        self._fds["root"] = root
        self._check_directory(root, str(self.root))
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        parent = root
        for name in ("blobs", "sha256"):
            try:
                child = os.open(name, flags, dir_fd=parent)
            except FileNotFoundError:
                if not create:
                    raise SubstrateError(f"{self.root} is not an OCI image layout")
                os.mkdir(name, 0o700, dir_fd=parent)
                child = os.open(name, flags, dir_fd=parent)
                os.fchmod(child, 0o700)
            self._fds[name] = child
            self._check_directory(child, f"{self.root}/{name}")
            parent = child
        if create:
            self._ensure_file("oci-layout", LAYOUT_FILE)
            self._ensure_file(
                "index.json",
                json.dumps(
                    {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": []},
                    sort_keys=True,
                ).encode(),
            )
        layout = self._read_small("oci-layout", MAX_INDEX_BYTES)
        if load_json(layout, "oci-layout").get("imageLayoutVersion") != "1.0.0":
            raise SubstrateError(f"{self.root} has another OCI layout version")

    def _ensure_file(self, name: str, content: bytes) -> None:
        try:
            os.close(
                open_regular(
                    name,
                    self._fds["root"],
                    strict=self.strict,
                    max_size=MAX_INDEX_BYTES,
                    context=f"{self.root}/{name}",
                )
            )
        except SubstrateError:
            try:
                os.stat(name, dir_fd=self._fds["root"], follow_symlinks=False)
            except FileNotFoundError:
                self._write_atomically(self._fds["root"], name, content)
                return
            raise

    def _read_small(self, name: str, max_size: int) -> bytes:
        descriptor_fd = open_regular(
            name,
            self._fds["root"],
            strict=self.strict,
            max_size=max_size,
            context=f"{self.root}/{name}",
        )
        try:
            return read_all(descriptor_fd)
        finally:
            os.close(descriptor_fd)

    @staticmethod
    def _write_atomically(dir_fd: int, name: str, content: bytes) -> None:
        temporary = f".{name}.{os.getpid()}.tmp"
        descriptor_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            os.fchmod(descriptor_fd, 0o600)
            write_all(descriptor_fd, content)
            os.fsync(descriptor_fd)
        finally:
            os.close(descriptor_fd)
        os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)

    def index(self) -> list[dict[str, Any]]:
        document = load_json(
            self._read_small("index.json", MAX_INDEX_BYTES), "index.json"
        )
        manifests = document.get("manifests") if isinstance(document, dict) else None
        if (
            not isinstance(manifests, list)
            or document.get("schemaVersion") != 2
            or not all(isinstance(entry, dict) for entry in manifests)
        ):
            raise SubstrateError(f"{self.root}/index.json is not an image index")
        return manifests

    def reference(self, ref_name: str) -> dict[str, Any] | None:
        matches = [
            entry
            for entry in self.index()
            if isinstance(entry, dict)
            and (entry.get("annotations") or {}).get(REF_NAME) == ref_name
        ]
        if len(matches) > 1:
            raise SubstrateError(f"{self.root} names {ref_name} more than once")
        return descriptor(matches[0], ref_name) if matches else None

    def add_reference(self, ref_name: str, entry: dict[str, Any]) -> None:
        manifests = self.index()
        current = self.reference(ref_name)
        if current is not None:
            if current["digest"] != entry["digest"]:
                raise SubstrateError(
                    f"{self.root} already names {ref_name} with another digest"
                )
            return
        manifests.append(
            {
                "mediaType": entry["mediaType"],
                "digest": entry["digest"],
                "size": entry["size"],
                "annotations": {REF_NAME: ref_name},
            }
        )
        self._write_index(manifests)

    def remove_reference(self, ref_name: str, digest: str) -> bool:
        """Drop the entry for ref_name only while it names exactly digest.

        Its blobs stay: they are content-addressed and may be shared, and a
        later copy of the same digest reuses them after verifying them.
        """
        current = self.reference(ref_name)
        if current is None:
            return False
        if current["digest"] != digest:
            raise SubstrateError(
                f"{self.root} names {ref_name} with {current['digest']}, not {digest}"
            )
        self._write_index(
            [
                entry
                for entry in self.index()
                if (entry.get("annotations") or {}).get(REF_NAME) != ref_name
            ]
        )
        return True

    def _write_index(self, manifests: list[Any]) -> None:
        manifests.sort(
            key=lambda item: (item.get("annotations") or {}).get(REF_NAME, "")
        )
        document = {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": manifests}
        self._write_atomically(
            self._fds["root"],
            "index.json",
            json.dumps(document, sort_keys=True).encode(),
        )

    def has_blob(self, digest: str) -> bool:
        try:
            os.stat(
                DIGEST_RE.fullmatch(digest)[1],
                dir_fd=self._fds["sha256"],
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        return True

    def iter_blob(self, blob: dict[str, Any]) -> Iterator[bytes]:
        """Stream one blob, verifying its size and digest at the end."""
        hex_digest = DIGEST_RE.fullmatch(blob["digest"])[1]
        descriptor_fd = open_regular(
            hex_digest,
            self._fds["sha256"],
            strict=self.strict,
            max_size=blob["size"],
            context=f"blob {blob['digest']}",
        )
        hasher = hashlib.sha256()
        count = 0
        try:
            while True:
                chunk = os.read(descriptor_fd, CHUNK)
                if not chunk:
                    break
                count += len(chunk)
                hasher.update(chunk)
                yield chunk
        finally:
            os.close(descriptor_fd)
        if count != blob["size"] or hasher.hexdigest() != hex_digest:
            raise SubstrateError(f"blob {blob['digest']} is corrupt in {self.root}")

    def read_blob(self, blob: dict[str, Any]) -> bytes:
        return b"".join(self.iter_blob(blob))

    def verify_blob(self, blob: dict[str, Any]) -> None:
        for _chunk in self.iter_blob(blob):
            pass

    def write_blob(self, blob: dict[str, Any], chunks: Iterator[bytes]) -> bool:
        """Write a verified blob; an existing one is verified, never replaced."""
        if self.has_blob(blob["digest"]):
            self.verify_blob(blob)
            return False
        hex_digest = DIGEST_RE.fullmatch(blob["digest"])[1]
        temporary = f".partial-{hex_digest}-{os.getpid()}"
        sha_fd = self._fds["sha256"]
        descriptor_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=sha_fd,
        )
        hasher = hashlib.sha256()
        count = 0
        try:
            os.fchmod(descriptor_fd, 0o600)
            for chunk in chunks:
                count += len(chunk)
                if count > blob["size"]:
                    raise SubstrateError(
                        f"blob {blob['digest']} is larger than its size"
                    )
                hasher.update(chunk)
                write_all(descriptor_fd, chunk)
            os.fsync(descriptor_fd)
            if count != blob["size"] or hasher.hexdigest() != hex_digest:
                raise SubstrateError(
                    f"blob {blob['digest']} does not match its descriptor"
                )
        except BaseException:
            os.close(descriptor_fd)
            os.unlink(temporary, dir_fd=sha_fd)
            raise
        os.close(descriptor_fd)
        os.rename(temporary, hex_digest, src_dir_fd=sha_fd, dst_dir_fd=sha_fd)
        os.fsync(sha_fd)
        return True

    def image(self, digest: str) -> tuple[bytes, str, list[dict[str, Any]]]:
        """The manifest blob with that digest, verified, and its blobs."""
        descriptor_fd = open_regular(
            DIGEST_RE.fullmatch(digest)[1],
            self._fds["sha256"],
            strict=self.strict,
            max_size=MAX_MANIFEST_BYTES,
            context=f"manifest {digest}",
        )
        try:
            raw = read_all(descriptor_fd)
        finally:
            os.close(descriptor_fd)
        media_type, blobs = parse_manifest(raw, digest)
        return raw, media_type, blobs

    def status(self, name: str, tag: str, digest: str) -> str:
        """complete or missing; anything else is corruption and raises."""
        if not self.exists():
            return "missing"
        self.open()
        entry = self.reference(f"{name}:{tag}")
        if entry is None:
            return "missing"
        if entry["digest"] != digest:
            raise SubstrateError(f"{self.root} names {name}:{tag} with another digest")
        _raw, media_type, blobs = self.image(digest)
        if entry["mediaType"] != media_type:
            raise SubstrateError(f"{self.root} records another type for {name}")
        for blob in blobs:
            self.verify_blob(blob)
        return "complete"


# --------------------------------------------------------------------------
# Copies


def copy_to_layout(
    manifest: bytes,
    media_type: str,
    blobs: list[dict[str, Any]],
    open_blob: Callable[[dict[str, Any]], Iterator[bytes]],
    layout: Layout,
    ref_name: str,
    digest: str,
) -> None:
    for blob in blobs:
        if not layout.has_blob(blob["digest"]):
            layout.write_blob(blob, open_blob(blob))
        else:
            layout.verify_blob(blob)
    layout.write_blob(
        {"mediaType": media_type, "digest": digest, "size": len(manifest)},
        iter([manifest]),
    )
    layout.add_reference(
        ref_name, {"mediaType": media_type, "digest": digest, "size": len(manifest)}
    )


def export_images(
    registry: Registry,
    layout: Layout,
    tag: str,
    pins: dict[str, str],
    source_naming: str,
    image_set: str = "substrate",
) -> dict[str, str]:
    """Registry to layout, byte for byte; the layout is created if missing.

    ko-md5 names the source repositories as ko's default namer did, which
    only applies to the images ko built.
    """
    module, _names, ko_images = IMAGE_SETS[image_set]
    if source_naming == "ko-md5":
        for name in pins:
            if name not in ko_images:
                raise SubstrateError(f"{name} is not a ko image: use base naming")
    layout.open(create=True)
    results = {}
    for name, digest in sorted(pins.items()):
        if layout.status(name, tag, digest) == "complete":
            results[name] = "present"
            continue
        repository = (
            ko_md5_repository(name, module) if source_naming == "ko-md5" else name
        )
        raw, content_type = registry.get_manifest(repository, digest)
        media_type, blobs = parse_manifest(raw, digest)
        if content_type.split(";")[0].strip() != media_type:
            raise SubstrateError(f"{repository}@{digest} is served as another type")
        copy_to_layout(
            raw,
            media_type,
            blobs,
            lambda blob, repository=repository: registry.iter_blob(repository, blob),
            layout,
            f"{name}:{tag}",
            digest,
        )
        if layout.status(name, tag, digest) != "complete":
            raise SubstrateError(f"{name} is not complete in the layout after export")
        results[name] = "exported"
    return results


def verify_registry_image(registry: Registry, name: str, tag: str, digest: str) -> None:
    """The tag names the digest, and the bytes served hash to it."""
    if registry.head_manifest(name, tag) != digest:
        raise SubstrateError(f"{name}:{tag} does not name {digest}")
    raw, _content_type = registry.get_manifest(name, tag)
    parse_manifest(raw, digest)


def import_images(
    registry: Registry, layout: Layout, tag: str, pins: dict[str, str]
) -> dict[str, str]:
    """Layout to registry, byte for byte; a moved tag is put back."""
    layout.open()
    results = {}
    for name, digest in sorted(pins.items()):
        if layout.status(name, tag, digest) != "complete":
            raise SubstrateError(f"{name} is not complete in the layout")
        if registry.head_manifest(name, tag) == digest:
            verify_registry_image(registry, name, tag, digest)
            results[name] = "present"
            continue
        raw, media_type, blobs = layout.image(digest)
        for blob in blobs:
            if not registry.blob_exists(name, blob["digest"]):
                registry.upload_blob(name, blob, layout.iter_blob(blob))
        if registry.put_manifest(name, tag, raw, media_type) != digest:
            raise SubstrateError(f"the registry stored {name}:{tag} as another digest")
        verify_registry_image(registry, name, tag, digest)
        results[name] = "imported"
    return results


def registry_state(registry: Registry, name: str, tag: str, digest: str) -> str:
    """pinned, missing, or moved (the tag names another manifest)."""
    served = registry.head_manifest(name, tag)
    if served is None:
        return "missing"
    return "pinned" if served == digest else "moved"


def image_status(
    registry: Registry | None, layout: Layout, tag: str, pins: dict[str, str]
) -> dict[str, Any]:
    """Per pinned image: its state in the registry (None when it is not
    read) and in the backup layout (complete or missing; corruption raises).
    """
    with layout:
        return {
            "registry": (
                None
                if registry is None
                else {
                    name: registry_state(registry, name, tag, digest)
                    for name, digest in sorted(pins.items())
                }
            ),
            "backup": {
                name: layout.status(name, tag, digest)
                for name, digest in sorted(pins.items())
            },
        }


def seed_layout(
    source: Layout, layout: Layout, tag: str, pins: dict[str, str]
) -> dict[str, str]:
    """External layout to backup layout, one pinned manifest at a time.

    The source's index.json must list the pinned digest with the manifest's
    own media type; the manifest bytes must hash to the pin, and every blob,
    streamed from the source into the backup, to its descriptor. Nothing
    else of the source is copied, and it is never written.
    """
    source.open()
    layout.open(create=True)
    results = {}
    for name, digest in sorted(pins.items()):
        if layout.status(name, tag, digest) == "complete":
            results[name] = "present"
            continue
        listed = [
            descriptor(entry, f"{source.root}/index.json")
            for entry in source.index()
            if entry.get("digest") == digest
        ]
        if not listed:
            raise SubstrateError(f"{source.root} does not list {digest}")
        raw, media_type, blobs = source.image(digest)
        for entry in listed:
            if entry["mediaType"] != media_type or entry["size"] != len(raw):
                raise SubstrateError(
                    f"{source.root}/index.json describes {digest} as another type"
                )
        copy_to_layout(
            raw, media_type, blobs, source.iter_blob, layout, f"{name}:{tag}", digest
        )
        if layout.status(name, tag, digest) != "complete":
            raise SubstrateError(f"{name} is not complete in the layout after seeding")
        results[name] = "seeded"
    return results


def forget_images(layout: Layout, tag: str, stale: dict[str, str]) -> dict[str, str]:
    """Drop <name>:<tag> from the layout, only where it names the given digest.

    The layout never names one <name>:<tag> with two digests, so a re-pin
    under the same version stops every read until the entry of the old pin
    is dropped, by its exact old digest (docs/AX.md, «Copia de seguridad»).
    """
    layout.open()
    return {
        name: (
            "forgotten"
            if layout.remove_reference(f"{name}:{tag}", digest)
            else "absent"
        )
        for name, digest in sorted(stale.items())
    }


# --------------------------------------------------------------------------
# Cluster reads


class Kubectl:
    def __init__(self, binary: str, kubeconfig: str, context: str, home: str) -> None:
        self.prefix = [
            binary,
            "--kubeconfig",
            kubeconfig,
            "--context",
            context,
            "--request-timeout",
            "10s",
        ]
        self.environment = {"HOME": home, "PATH": "/usr/bin:/bin"}

    def get(self, arguments: Sequence[str]) -> str:
        try:
            completed = subprocess.run(
                [*self.prefix, "get", *arguments],
                capture_output=True,
                text=True,
                check=False,
                env=self.environment,
            )
        except OSError as error:
            raise SubstrateError(f"cannot run kubectl: {error}") from error
        if completed.returncode != 0:
            detail = (completed.stderr.strip().splitlines() or [""])[0][:200]
            raise SubstrateError(f"kubectl get {arguments[0]} failed: {detail}")
        return completed.stdout


def optional_int(value: str) -> int | None:
    return int(value) if value.strip().isdigit() else None


def workload_ready(fields: dict[str, str]) -> bool:
    """kubectl rollout status's readiness rules, per kind; Jobs: succeeded."""
    kind = fields["kind"]
    if kind == "Job":
        return (optional_int(fields["succeeded"]) or 0) >= 1
    generation = optional_int(fields["generation"])
    observed = optional_int(fields["observedGeneration"])
    if generation is None or observed is None or observed < generation:
        return False
    if kind == "DaemonSet":
        desired = optional_int(fields["desiredNumberScheduled"])
        return (
            desired is not None
            and (optional_int(fields["updatedNumberScheduled"]) or 0) >= desired
            and (optional_int(fields["numberAvailable"]) or 0) >= desired
        )
    replicas = optional_int(fields["specReplicas"])
    replicas = 1 if replicas is None else replicas
    if kind == "Deployment":
        updated = optional_int(fields["updatedReplicas"]) or 0
        return (
            updated >= replicas
            and (optional_int(fields["replicas"]) or 0) <= updated
            and (optional_int(fields["availableReplicas"]) or 0) >= updated
        )
    if kind == "StatefulSet":
        return (optional_int(fields["readyReplicas"]) or 0) >= replicas and fields[
            "currentRevision"
        ] == fields["updateRevision"] != ""
    return False


FIELD_NAMES = (
    "kind",
    "name",
    "uid",
    "generation",
    "observedGeneration",
    "specReplicas",
    "replicas",
    "readyReplicas",
    "updatedReplicas",
    "availableReplicas",
    "currentRevision",
    "updateRevision",
    "desiredNumberScheduled",
    "updatedNumberScheduled",
    "numberAvailable",
    "succeeded",
    "initImages",
    "images",
)


def cluster_status(
    kubectl: Kubectl,
    node: str,
    image_repo: str,
    tag: str,
    pins: dict[str, str],
) -> dict[str, Any]:
    label = kubectl.get(
        [
            "node",
            node,
            "--output",
            "jsonpath={.metadata.labels.ate\\.dev/substrate-version}",
        ]
    ).strip()
    create_once: dict[str, Any] = {}
    for namespace, kind, name in CREATE_ONCE:
        read = kubectl.get(
            [
                kind.lower(),
                name,
                "--namespace",
                namespace,
                "--ignore-not-found",
                "--output",
                'jsonpath={.metadata.uid}{"\\t"}{.type}',
            ]
        )
        uid, _separator, object_type = read.partition("\t")
        create_once[f"{namespace}/{kind}/{name}"] = (
            {"uid": uid.strip(), "type": object_type.strip() or None}
            if uid.strip()
            else None
        )
    references = {
        f"{image_repo}/{name}:{tag}@{digest}": name for name, digest in pins.items()
    }
    workloads = []
    objects = {}
    for namespace in NAMESPACES:
        output = kubectl.get(
            [
                WORKLOAD_KINDS,
                "--namespace",
                namespace,
                "--output",
                f"jsonpath={WORKLOAD_JSONPATH}",
            ]
        )
        for line in output.splitlines():
            if not line.strip():
                continue
            values = line.split("\t")
            if len(values) != len(FIELD_NAMES):
                raise SubstrateError(f"unexpected workload line in {namespace}")
            fields = dict(zip(FIELD_NAMES, values, strict=True))
            entry: dict[str, Any] = {
                "kind": fields["kind"],
                "namespace": namespace,
                "name": fields["name"],
            }
            init_images = fields["initImages"].split()
            if init_images:
                entry["init_images"] = [references.get(i, i) for i in init_images]
            entry["images"] = [references.get(i, i) for i in fields["images"].split()]
            workloads.append(entry)
            objects[f"{namespace}/{fields['kind']}/{fields['name']}"] = {
                "uid": fields["uid"],
                "generation": optional_int(fields["generation"]),
                "ready": workload_ready(fields),
            }
    workloads.sort(key=lambda item: (item["namespace"], item["kind"], item["name"]))
    return {
        "node_label": label or None,
        "create_once": create_once,
        "workloads": workloads,
        "objects": objects,
        "immutable": {
            key: (
                f"{objects[key]['uid']}@{objects[key]['generation']}"
                if key in objects
                else None
            )
            for key in IMMUTABLE_TEMPLATES
        },
        "not_ready": sorted(
            key for key, value in objects.items() if not value["ready"]
        ),
    }


# --------------------------------------------------------------------------
# Bounded containers: the fallback build and the ate-setup run


class Docker:
    def run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["/usr/bin/docker", *argv],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise SubstrateError("cannot execute /usr/bin/docker") from error

    def checked(self, argv: Sequence[str]) -> str:
        completed = self.run(argv)
        if completed.returncode != 0:
            detail = (completed.stderr.strip().splitlines() or [""])[-1][:300]
            raise SubstrateError(f"docker {argv[0]} failed: {detail}")
        return completed.stdout

    def state(self, name: str) -> dict[str, Any] | None:
        completed = self.run(
            ["container", "inspect", "--format", "{{json .State}}", name]
        )
        if completed.returncode != 0:
            if f"No such container: {name}" in completed.stderr:
                return None
            raise SubstrateError(f"cannot inspect container {name}")
        state = load_json(completed.stdout.encode(), f"state of {name}")
        if not isinstance(state, dict):
            raise SubstrateError(f"cannot inspect container {name}")
        return state

    def swarm_state(self) -> str:
        return self.checked(["info", "--format", "{{.Swarm.LocalNodeState}}"]).strip()


@dataclass
class Host:
    """What the bounded runner reads from the host; replaced in tests."""

    proc: Path = Path("/proc")
    cgroup: Path = Path("/sys/fs/cgroup/system.slice")
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    def mem_available_mib(self) -> int:
        for line in (self.proc / "meminfo").read_text(encoding="ascii").splitlines():
            key, _separator, value = line.partition(":")
            if key == "MemAvailable":
                return int(value.split()[0]) // 1024
        raise SubstrateError("/proc/meminfo has no MemAvailable")

    @staticmethod
    def _full_avg10(path: Path) -> float | None:
        try:
            text = path.read_text(encoding="ascii")
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("full "):
                for part in line.split():
                    if part.startswith("avg10="):
                        return float(part.removeprefix("avg10="))
        return None

    def host_full_avg10(self) -> float | None:
        return self._full_avg10(self.proc / "pressure/memory")

    def container_metrics(self, container_id: str) -> dict[str, Any]:
        scope = self.cgroup / f"docker-{container_id}.scope"
        metrics: dict[str, Any] = {
            "full_avg10": self._full_avg10(scope / "memory.pressure")
        }
        try:
            metrics["memory_peak"] = int((scope / "memory.peak").read_text().strip())
        except (OSError, ValueError):
            metrics["memory_peak"] = None
        try:
            events = dict(
                line.split()
                for line in (scope / "memory.events").read_text().splitlines()
                if line.strip()
            )
            metrics["oom_kill"] = int(events.get("oom_kill", "0"))
        except (OSError, ValueError):
            metrics["oom_kill"] = None
        return metrics


@dataclass
class Outcome:
    exit_code: int | None = None
    oom_killed: bool = False
    aborted: str | None = None
    seconds: float = 0.0
    memory_peak_bytes: int | None = None
    oom_kill_events: int | None = None
    max_host_memory_full_avg10: float | None = None
    max_container_memory_full_avg10: float | None = None
    stdout: str = field(default="", repr=False)
    stderr: str = field(default="", repr=False)

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.oom_killed and self.aborted is None

    def metrics(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "oom_killed": self.oom_killed,
            "aborted": self.aborted,
            "seconds": round(self.seconds, 1),
            "memory_peak_bytes": self.memory_peak_bytes,
            "oom_kill_events": self.oom_kill_events,
            "max_host_memory_full_avg10": self.max_host_memory_full_avg10,
            "max_container_memory_full_avg10": self.max_container_memory_full_avg10,
        }


def maximum(current: float | None, value: float | None) -> float | None:
    if value is None:
        return current
    return value if current is None else max(current, value)


def run_bounded(
    docker: Docker,
    host: Host,
    name: str,
    argv: Sequence[str],
    *,
    timeout_seconds: int,
    mem_available_floor_mib: int,
    poll_seconds: float = 5.0,
) -> Outcome:
    """Start argv detached, watch it, kill it on a bound, then remove it.

    There is no --rm: the container is kept until its exit code and
    OOMKilled have been read, then removed with `docker rm`. The kernel
    enforces the memory cap; the runner only adds a timeout and a floor on
    the host's MemAvailable. Pressure is recorded, never acted on. Should
    anything fail while it runs, the container is killed before the error
    goes on, so it never outlives its bounds, and kept with its logs.
    """
    outcome = Outcome()
    started = host.clock()
    created = docker.run(["run", "--detach", *argv])
    if created.returncode != 0:
        # The name was proven free just before, so a container under it is
        # this run's own: created, or even started, before the daemon
        # reported the failure. --force removes it in either state.
        if docker.state(name) is not None:
            docker.run(["rm", "--force", name])
        detail = (created.stderr.strip().splitlines() or [""])[-1][:300]
        raise SubstrateError(f"docker run of {name} failed: {detail}")
    try:
        # `docker run --detach` prints the full ID of what it started.
        container_id = created.stdout.strip()
        if not CONTAINER_ID_RE.fullmatch(container_id):
            raise SubstrateError(f"docker run of {name} printed no container ID")
        while True:
            metrics = host.container_metrics(container_id)
            outcome.memory_peak_bytes = (
                metrics["memory_peak"] or outcome.memory_peak_bytes
            )
            if metrics["oom_kill"] is not None:
                outcome.oom_kill_events = metrics["oom_kill"]
            outcome.max_container_memory_full_avg10 = maximum(
                outcome.max_container_memory_full_avg10, metrics["full_avg10"]
            )
            outcome.max_host_memory_full_avg10 = maximum(
                outcome.max_host_memory_full_avg10, host.host_full_avg10()
            )
            state = docker.state(name)
            if state is None:
                raise SubstrateError(f"container {name} disappeared while running")
            if not state.get("Running"):
                break
            if host.clock() - started > timeout_seconds:
                outcome.aborted = "timeout"
            elif host.mem_available_mib() < mem_available_floor_mib:
                outcome.aborted = "mem_available_floor"
            if outcome.aborted is not None:
                docker.run(["kill", name])
                for _attempt in range(60):
                    if not (docker.state(name) or {}).get("Running"):
                        break
                    host.sleep(1.0)
                else:
                    raise SubstrateError(f"container {name} survived docker kill")
                break
            host.sleep(poll_seconds)
        outcome.seconds = host.clock() - started
        state = docker.state(name) or {}
    except SubstrateError as error:
        stop_after_error(docker, host, name)
        raise SubstrateError(
            f"{error}; docker kill was sent to {name}, which is kept for "
            "review: read its logs, then remove it with docker rm (docs/AX.md, "
            "«Contenedores transitorios»)"
        ) from error
    except BaseException:
        stop_after_error(docker, host, name)
        raise
    outcome.exit_code = state.get("ExitCode")
    outcome.oom_killed = bool(state.get("OOMKilled"))
    logs = docker.run(["logs", "--tail", "200", name])
    outcome.stdout, outcome.stderr = logs.stdout, logs.stderr
    docker.checked(["rm", name])
    return outcome


def stop_after_error(docker: Docker, host: Host, name: str) -> None:
    """Best effort after an unexpected error: kill the container and wait.

    It is kept, stopped, with its logs: refuse_leftover and the role's
    inspection stop every later run until someone reads them and removes
    it. A failure here must not hide the error that got us here.
    """
    with contextlib.suppress(SubstrateError, OSError):
        docker.run(["kill", name])
        for _attempt in range(60):
            if not (docker.state(name) or {}).get("Running"):
                return
            host.sleep(1.0)


def refuse_leftover(docker: Docker, name: str) -> None:
    state = docker.state(name)
    if state is None:
        return
    if state.get("Running"):
        raise SubstrateError(
            f"container {name} is still running under an earlier run, which "
            "an interrupted controller leaves going: wait until it exits (its "
            "own timeout bounds it) and run again; if it is left stopped, read "
            "its logs and remove it with docker rm first (docs/AX.md, "
            "«Contenedores transitorios»)"
        )
    raise SubstrateError(
        f"container {name} is left from an earlier run: read its logs, "
        "then remove it with docker rm, and run again (docs/AX.md, "
        "«Contenedores transitorios»)"
    )


def resources(
    memory_mib: int, reservation_mib: int, cpu_millicores: int, pids: int
) -> list[str]:
    for value in (memory_mib, reservation_mib, cpu_millicores, pids):
        if type(value) is not int or value < 1:
            raise SubstrateError("container limits must be positive integers")
    if reservation_mib > memory_mib:
        raise SubstrateError("the memory reservation exceeds the limit")
    return [
        "--memory",
        f"{memory_mib}m",
        "--memory-swap",
        f"{memory_mib}m",
        "--memory-reservation",
        f"{reservation_mib}m",
        "--cpus",
        f"{cpu_millicores // 1000}.{cpu_millicores % 1000:03d}",
        "--pids-limit",
        str(pids),
    ]


def hardening(name: str, role: str) -> list[str]:
    return [
        "--name",
        name,
        "--label",
        CONTAINER_LABELS[0],
        "--label",
        f"com.apptolast.ax-lab={role}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--read-only",
        "--user",
        "0:0",
    ]


@dataclass
class BuildLimits:
    memory_mib: int
    reservation_mib: int
    cpu_millicores: int
    pids: int
    timeout_seconds: int
    min_mem_available_mib: int
    mem_available_floor_mib: int


def build_argv(
    *,
    name: str,
    toolbox_image: str,
    source: Path,
    cache: Path,
    version: str,
    limits: BuildLimits,
) -> list[str]:
    """The toolbox run: upstream's own `make build-images` for one image.

    ko writes the image to an OCI layout (--push=false) instead of pushing
    it, with no SBOM; everything else is Makefile:72-75 at the pinned commit
    with the manual lab's inputs: KO_DOCKER_REPO on the make command line
    (Makefile:19 exports its own otherwise), the pinned VERSION, the host
    platform. The checkout is read-only; the Go caches, the temporary files
    and HOME live in the cache directory, never on a tmpfs, which the memory
    cgroup would charge. No docker.sock, no host network, no capability.
    """
    if limits.cpu_millicores % 1000:
        raise SubstrateError("the build CPU limit must be whole CPUs")
    ko_flags = f"--push=false --oci-layout-path=/cache/out/{name} --sbom=none"
    return [
        *hardening(BUILD_CONTAINER_PREFIX + name, "build"),
        *resources(
            limits.memory_mib,
            limits.reservation_mib,
            limits.cpu_millicores,
            limits.pids,
        ),
        "--network",
        "bridge",
        "--mount",
        f"type=bind,source={source},target=/src/substrate,readonly",
        "--mount",
        f"type=bind,source={cache},target=/cache",
        "--workdir",
        "/src/substrate",
        "--env",
        f"GOMAXPROCS={limits.cpu_millicores // 1000}",
        "--env",
        "GOFLAGS=-p=1",
        "--env",
        "GOGC=40",
        "--env",
        "GOTOOLCHAIN=local",
        "--env",
        "GOCACHE=/cache/go-build",
        "--env",
        "GOMODCACHE=/cache/go-mod",
        "--env",
        "TMPDIR=/cache/tmp",
        "--env",
        "HOME=/cache/home",
        "--env",
        f"KO_DEFAULTPLATFORMS={PLATFORM}",
        # git refuses a checkout owned by another uid; command-scope config
        # trusts exactly this one for hack/run-tool.sh and Go's VCS stamp.
        "--env",
        "GIT_CONFIG_COUNT=1",
        "--env",
        "GIT_CONFIG_KEY_0=safe.directory",
        "--env",
        "GIT_CONFIG_VALUE_0=/src/substrate",
        toolbox_image,
        "make",
        "build-images",
        "KO_DOCKER_REPO=localhost:5001",
        f"VERSION={version}",
        f"IMAGES=./cmd/{name}",
        f"KO_FLAGS={ko_flags}",
    ]


def prepare_cache(cache: Path, name: str) -> None:
    """The cache subdirectories, 0700, and an empty scratch layout path."""
    cache_fd = open_directory(cache)
    try:
        status = os.fstat(cache_fd)
        if status.st_uid != os.geteuid() or status.st_mode & 0o077:
            raise SubstrateError(f"{cache} is not a 0700 directory of this process")
        for child in ("go-build", "go-mod", "tmp", "home", "out"):
            try:
                os.mkdir(child, 0o700, dir_fd=cache_fd)
            except FileExistsError:
                pass
        out_fd = os.open(
            "out", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cache_fd
        )
        try:
            try:
                os.stat(name, dir_fd=out_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            shutil.rmtree(name, dir_fd=out_fd)
        finally:
            os.close(out_fd)
    finally:
        os.close(cache_fd)


def built_digest(stdout: str, name: str) -> str:
    """ko prints <layout path>@<digest> of the image as its last line."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    match = (
        re.fullmatch(
            rf"/cache/out/{re.escape(name)}@(sha256:[a-f0-9]{{64}})", lines[-1]
        )
        if lines
        else None
    )
    if match is None:
        raise SubstrateError(f"ko did not print the digest of {name}")
    return match[1]


def build_image(
    docker: Docker,
    host: Host,
    *,
    name: str,
    digest: str | None,
    toolbox_image: str,
    source: Path,
    cache: Path,
    layout: Layout,
    version: str,
    limits: BuildLimits,
    node_container: str,
) -> dict[str, Any]:
    layout.open(create=True)
    if digest is not None and layout.status(name, version, digest) == "complete":
        return {"name": name, "digest": digest, "result": "present"}
    refuse_leftover(docker, BUILD_CONTAINER_PREFIX + name)
    if (docker.state(node_container) or {}).get("Running"):
        raise SubstrateError(
            f"{node_container} is running: the build borrows its budget, so stop "
            "the lab first (docs/AX.md)"
        )
    available = host.mem_available_mib()
    if available < limits.min_mem_available_mib:
        raise SubstrateError(
            f"MemAvailable is {available} MiB, below the "
            f"{limits.min_mem_available_mib} MiB the build needs"
        )
    prepare_cache(cache, name)
    outcome = run_bounded(
        docker,
        host,
        BUILD_CONTAINER_PREFIX + name,
        build_argv(
            name=name,
            toolbox_image=toolbox_image,
            source=source,
            cache=cache,
            version=version,
            limits=limits,
        ),
        timeout_seconds=limits.timeout_seconds,
        mem_available_floor_mib=limits.mem_available_floor_mib,
    )
    result: dict[str, Any] = {"name": name, **outcome.metrics()}
    if not outcome.succeeded:
        raise BoundedFailure(result, outcome.stderr)
    built = built_digest(outcome.stdout, name)
    result["digest"] = built
    if digest is not None and built != digest:
        raise BoundedFailure(
            {**result, "result": "digest differs from the pin"},
            f"built {built}, pinned {digest}",
        )
    with Layout(cache / "out" / name, strict=False) as scratch:
        scratch.open()
        raw, media_type, blobs = scratch.image(built)
        copy_to_layout(
            raw,
            media_type,
            blobs,
            scratch.iter_blob,
            layout,
            f"{name}:{version}",
            built,
        )
    if layout.status(name, version, built) != "complete":
        raise SubstrateError(f"{name} is not complete in the layout after the build")
    out_fd = open_directory(cache / "out")
    try:
        shutil.rmtree(name, dir_fd=out_fd)
    finally:
        os.close(out_fd)
    result["result"] = "built"
    return result


class BoundedFailure(SubstrateError):
    def __init__(self, result: dict[str, Any], detail: str) -> None:
        super().__init__(f"{result.get('name', 'container')} failed: {detail[-2000:]}")
        self.result = result


@dataclass
class InstallSettings:
    registry_port: int
    tag: str
    installer_digest: str
    source: Path
    kubeconfig: Path
    context: str
    router: str
    rollout_timeout_seconds: int
    memory_mib: int
    reservation_mib: int
    cpu_millicores: int
    pids: int


def install_argv(settings: InstallSettings) -> list[str]:
    """ate-setup in prebuilt mode (cmd/ate-setup/commands.md, lines 34-71).

    --image-repo installs the images already in the registry and never
    invokes ko, go or git; the manifests still come from the checkout,
    mounted read-only as the working directory, where ate-setup looks for
    go.mod (internal/config/shellenv.go lines 70-85). --no-dev-env skips
    .ate-dev-env.sh. Host networking reaches the API on 127.0.0.1:6443 and
    the registry on localhost:<port>, the name the node's containerd mirrors.
    """
    if settings.router != "envoy":
        raise SubstrateError("only the envoy atenet router is reviewed")
    repository = f"localhost:{settings.registry_port}"
    return [
        *hardening(INSTALL_CONTAINER, "install"),
        *resources(
            settings.memory_mib,
            settings.reservation_mib,
            settings.cpu_millicores,
            settings.pids,
        ),
        "--network",
        "host",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=16m",
        "--mount",
        f"type=bind,source={settings.source},target=/src/substrate,readonly",
        "--mount",
        f"type=bind,source={settings.kubeconfig},target=/kubeconfig,readonly",
        "--workdir",
        "/src/substrate",
        "--env",
        "HOME=/tmp",
        f"{repository}/{INSTALLER}@{settings.installer_digest}",
        "--kind",
        "--no-dev-env",
        "--kubeconfig",
        "/kubeconfig",
        "--context",
        settings.context,
        "--atenet-router",
        settings.router,
        "--rollout-timeout",
        f"{settings.rollout_timeout_seconds}s",
        "--image-repo",
        repository,
        "--image-tag",
        settings.tag,
        "deploy",
        "ate-system",
    ]


def install(
    docker: Docker,
    host: Host,
    registry: Registry,
    settings: InstallSettings,
    pins: dict[str, str],
    *,
    timeout_seconds: int,
    min_mem_available_mib: int,
    mem_available_floor_mib: int,
) -> dict[str, Any]:
    refuse_leftover(docker, INSTALL_CONTAINER)
    # ate-setup resolves each tag with a HEAD of its own and accepts one
    # digest for all images at most (internal/images/prebuilt.go): check each
    # tag against its own pin as close to the run as possible.
    for name, digest in sorted(pins.items()):
        verify_registry_image(registry, name, settings.tag, digest)
    if registry.head_manifest(INSTALLER, settings.installer_digest) is None:
        raise SubstrateError("the registry does not hold the pinned ate-setup image")
    available = host.mem_available_mib()
    if available < min_mem_available_mib:
        raise SubstrateError(
            f"MemAvailable is {available} MiB, below {min_mem_available_mib} MiB"
        )
    outcome = run_bounded(
        docker,
        host,
        INSTALL_CONTAINER,
        install_argv(settings),
        timeout_seconds=timeout_seconds,
        mem_available_floor_mib=mem_available_floor_mib,
    )
    result = {"name": INSTALLER, **outcome.metrics()}
    if not outcome.succeeded:
        raise BoundedFailure(result, outcome.stderr or outcome.stdout)
    return result


# --------------------------------------------------------------------------
# Lock


def ensure_host_lock(operation: str, docker: Docker) -> None:
    """Hold the host-global lock for a mutation, as the validation lock does.

    Under a proven lock (an Ansible run or `host_global_operation_lock.py
    run`) the proof is checked. Otherwise Docker's Swarm state classifies the
    host, as scripts/host-global-docker-validation-lock.sh does: an active
    Swarm node re-executes this command under the lock, and a host without
    Swarm, such as a CI runner, has no production to serialise against.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from host_global_operation_lock import ensure_mutation_lock

    command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
    if os.environ.get("DOCKERSWARM_IAC_LOCK_SCOPE") is not None:
        ensure_mutation_lock(operation, command, caller=Path(__file__))
        return
    state = docker.swarm_state()
    if state == "inactive":
        return
    if state not in SWARM_STATES_UNDER_LOCK:
        raise SubstrateError("Docker returned an unknown local Swarm state")
    ensure_mutation_lock(operation, command, caller=Path(__file__))


# --------------------------------------------------------------------------
# Command line


def absolute(value: str) -> Path:
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise argparse.ArgumentTypeError(f"{value} must be a normalized absolute path")
    return Path(value)


def positive(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"{value} must be a positive integer")
    return int(value)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = root.add_subparsers(dest="command", required=True)

    status = commands.add_parser("image-status")
    status.add_argument("--image-set", choices=sorted(IMAGE_SETS), default="substrate")
    status.add_argument("--registry")
    status.add_argument("--layout", type=absolute, required=True)
    status.add_argument("--tag", required=True)
    status.add_argument("--image", action="append", default=[])

    cluster = commands.add_parser("cluster-status")
    cluster.add_argument("--kubectl", type=absolute, required=True)
    cluster.add_argument("--kubeconfig", type=absolute, required=True)
    cluster.add_argument("--home", type=absolute, required=True)
    cluster.add_argument("--context", required=True)
    cluster.add_argument("--node", required=True)
    cluster.add_argument("--registry-port", type=positive, required=True)
    cluster.add_argument("--tag", required=True)
    cluster.add_argument("--image", action="append", default=[])

    for name in ("export", "import"):
        copy = commands.add_parser(name)
        copy.add_argument(
            "--image-set", choices=sorted(IMAGE_SETS), default="substrate"
        )
        copy.add_argument("--registry", required=True)
        copy.add_argument("--layout", type=absolute, required=True)
        copy.add_argument("--tag", required=True)
        copy.add_argument("--image", action="append", default=[])
        if name == "export":
            copy.add_argument(
                "--source-naming", choices=("base", "ko-md5"), default="base"
            )

    seed = commands.add_parser("seed-layout")
    seed.add_argument("--image-set", choices=sorted(IMAGE_SETS), default="web")
    seed.add_argument("--source", type=absolute, required=True)
    seed.add_argument("--layout", type=absolute, required=True)
    seed.add_argument("--tag", required=True)
    seed.add_argument("--image", action="append", default=[])

    forget = commands.add_parser("forget")
    forget.add_argument("--image-set", choices=sorted(IMAGE_SETS), default="substrate")
    forget.add_argument("--layout", type=absolute, required=True)
    forget.add_argument("--tag", required=True)
    forget.add_argument("--image", action="append", default=[])

    build = commands.add_parser("build")
    build.add_argument("--image", required=True)
    build.add_argument("--toolbox-image", required=True)
    build.add_argument("--source", type=absolute, required=True)
    build.add_argument("--cache", type=absolute, required=True)
    build.add_argument("--layout", type=absolute, required=True)
    build.add_argument("--version", required=True)
    build.add_argument("--node-container", required=True)
    for option in (
        "--memory-mib",
        "--memory-reservation-mib",
        "--cpu-millicores",
        "--pids-limit",
        "--timeout-seconds",
        "--min-mem-available-mib",
        "--mem-available-floor-mib",
    ):
        build.add_argument(option, type=positive, required=True)

    run = commands.add_parser("install")
    run.add_argument("--registry-port", type=positive, required=True)
    run.add_argument("--tag", required=True)
    run.add_argument("--installer", required=True)
    run.add_argument("--image", action="append", default=[])
    run.add_argument("--source", type=absolute, required=True)
    run.add_argument("--kubeconfig", type=absolute, required=True)
    run.add_argument("--context", required=True)
    run.add_argument("--atenet-router", required=True)
    for option in (
        "--rollout-timeout-seconds",
        "--memory-mib",
        "--memory-reservation-mib",
        "--cpu-millicores",
        "--pids-limit",
        "--timeout-seconds",
        "--min-mem-available-mib",
        "--mem-available-floor-mib",
    ):
        run.add_argument(option, type=positive, required=True)
    return root


TOOLBOX_RE = re.compile(r"docker\.io/library/golang:[0-9.]+@sha256:[a-f0-9]{64}")


def dispatch(
    arguments: argparse.Namespace,
    docker: Docker | None = None,
    host: Host | None = None,
) -> dict[str, Any]:
    docker = docker or Docker()
    host = host or Host()
    command = arguments.command
    if command == "image-status":
        registry = None if arguments.registry is None else Registry(arguments.registry)
        return image_status(
            registry,
            Layout(arguments.layout),
            require_tag(arguments.tag),
            parse_pins(arguments.image, names=IMAGE_SETS[arguments.image_set][1]),
        )
    if command == "cluster-status":
        return cluster_status(
            Kubectl(
                str(arguments.kubectl),
                str(arguments.kubeconfig),
                arguments.context,
                str(arguments.home),
            ),
            arguments.node,
            f"localhost:{arguments.registry_port}",
            require_tag(arguments.tag),
            parse_pins(arguments.image),
        )
    if command in ("export", "import"):
        registry = Registry(arguments.registry)
        pins = parse_pins(arguments.image, names=IMAGE_SETS[arguments.image_set][1])
        tag = require_tag(arguments.tag)
        with Layout(arguments.layout) as layout:
            if command == "export":
                return export_images(
                    registry,
                    layout,
                    tag,
                    pins,
                    arguments.source_naming,
                    arguments.image_set,
                )
            return import_images(registry, layout, tag, pins)
    if command == "seed-layout":
        if arguments.source == arguments.layout:
            raise SubstrateError("--source must be another layout than --layout")
        pins = parse_pins(arguments.image, names=IMAGE_SETS[arguments.image_set][1])
        tag = require_tag(arguments.tag)
        with (
            Layout(arguments.source, strict=False) as source,
            Layout(arguments.layout) as layout,
        ):
            return seed_layout(source, layout, tag, pins)
    if command == "forget":
        with Layout(arguments.layout) as layout:
            return forget_images(
                layout,
                require_tag(arguments.tag),
                parse_pins(arguments.image, names=IMAGE_SETS[arguments.image_set][1]),
            )
    if command == "build":
        pins = parse_pins([arguments.image], allow_pending=True)
        ((name, digest),) = pins.items()
        if not TOOLBOX_RE.fullmatch(arguments.toolbox_image):
            raise SubstrateError(
                "the toolbox must be the official golang image by digest"
            )
        with Layout(arguments.layout) as layout:
            return build_image(
                docker,
                host,
                name=name,
                digest=digest,
                toolbox_image=arguments.toolbox_image,
                source=arguments.source,
                cache=arguments.cache,
                layout=layout,
                version=require_tag(arguments.version),
                limits=BuildLimits(
                    memory_mib=arguments.memory_mib,
                    reservation_mib=arguments.memory_reservation_mib,
                    cpu_millicores=arguments.cpu_millicores,
                    pids=arguments.pids_limit,
                    timeout_seconds=arguments.timeout_seconds,
                    min_mem_available_mib=arguments.min_mem_available_mib,
                    mem_available_floor_mib=arguments.mem_available_floor_mib,
                ),
                node_container=arguments.node_container,
            )
    if command == "install":
        installer = parse_pins([arguments.installer])
        if list(installer) != [INSTALLER]:
            raise SubstrateError("--installer must be ate-setup=<digest>")
        pins = parse_pins(arguments.image)
        if INSTALLER in pins:
            raise SubstrateError("ate-setup is the installer, not an installed image")
        settings = InstallSettings(
            registry_port=arguments.registry_port,
            tag=require_tag(arguments.tag),
            installer_digest=installer[INSTALLER],
            source=arguments.source,
            kubeconfig=arguments.kubeconfig,
            context=arguments.context,
            router=arguments.atenet_router,
            rollout_timeout_seconds=arguments.rollout_timeout_seconds,
            memory_mib=arguments.memory_mib,
            reservation_mib=arguments.memory_reservation_mib,
            cpu_millicores=arguments.cpu_millicores,
            pids=arguments.pids_limit,
        )
        return install(
            docker,
            host,
            Registry(f"127.0.0.1:{arguments.registry_port}"),
            settings,
            pins,
            timeout_seconds=arguments.timeout_seconds,
            min_mem_available_mib=arguments.min_mem_available_mib,
            mem_available_floor_mib=arguments.mem_available_floor_mib,
        )
    raise SubstrateError(f"unknown command {command}")


MUTATING_COMMANDS = {"export", "import", "seed-layout", "forget", "build", "install"}
# Commands that run a bounded container: a termination signal becomes an
# error, so the runner kills its container before this process exits.
CONTAINER_COMMANDS = {"build", "install"}


class Terminated(SubstrateError):
    """This process received SIGTERM or SIGHUP."""


def raise_terminated(signal_number: int, _frame: object) -> None:
    raise Terminated(f"received signal {signal.Signals(signal_number).name}")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command in MUTATING_COMMANDS:
            ensure_host_lock(f"ax-lab-substrate-{arguments.command}", Docker())
        if arguments.command in CONTAINER_COMMANDS:
            for signal_number in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(signal_number, raise_terminated)
        result = dispatch(arguments)
    except BoundedFailure as failure:
        print(json.dumps(failure.result, sort_keys=True))
        print(f"ERROR: {failure}", file=sys.stderr)
        return 1
    except (SubstrateError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
