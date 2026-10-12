"""scripts/manage-ax-lab-substrate.py against a fake registry, Docker and host.

Nothing here reaches Docker, a cluster or the network beyond a registry
served by this process on 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import types
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/manage-ax-lab-substrate.py"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
TAG = "67253354"
# The repositories of the manual lab's registry, as listed by
# `curl http://127.0.0.1:5001/v2/_catalog` on 2026-09-25. ko's default namer
# calls each one <base name>-<md5 of the Go import path>, so they are derived
# here instead of copied.
MANUAL_LAB_REPOSITORIES = {
    name: name
    + "-"
    + hashlib.md5(
        f"github.com/agent-substrate/substrate/cmd/{name}".encode()
    ).hexdigest()
    for name in (
        "ateapi",
        "atecontroller",
        "atelet",
        "atenet",
        "podcertcontroller",
        "ateom-gvisor",
    )
}


def load_manager() -> Any:
    spec = importlib.util.spec_from_file_location("manage_ax_lab_substrate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


manager = load_manager()


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def make_image(seed: str, layers: int = 3) -> dict[str, Any]:
    """A single-platform OCI image: its blobs, manifest bytes and digest."""
    config = json.dumps({"architecture": "amd64", "os": "linux", "seed": seed}).encode()
    blobs = {sha(config): config}
    layer_descriptors = []
    for index in range(layers):
        layer = f"{seed}-layer-{index}".encode() * (1000 + index)
        blobs[sha(layer)] = layer
        layer_descriptors.append(
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "size": len(layer),
                "digest": sha(layer),
            }
        )
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "size": len(config),
                "digest": sha(config),
            },
            "layers": layer_descriptors,
            "annotations": {"org.opencontainers.image.base.name": "base"},
        },
        indent=3,
    ).encode()
    return {"manifest": manifest, "digest": sha(manifest), "blobs": blobs}


class FakeRegistry:
    """The distribution API subset the script speaks, in memory."""

    def __init__(self) -> None:
        self.manifests: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.blobs: dict[str, bytes] = {}
        self.repository_blobs: dict[str, set[str]] = {}
        self.uploads: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []
        self.tampered_blobs: dict[str, bytes] = {}
        self.tampered_manifests: dict[str, bytes] = {}
        registry = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                return

            def _send(
                self, status: int, body: bytes = b"", headers: dict | None = None
            ) -> None:
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _route(self) -> None:
                parts = urlsplit(self.path)
                registry.requests.append((self.command, parts.path))
                segments = parts.path.split("/")
                if segments[:2] != ["", "v2"]:
                    return self._send(404)
                if "manifests" in segments:
                    index = segments.index("manifests")
                    return self._manifest("/".join(segments[2:index]), segments[-1])
                if segments[-3:-1] == ["blobs", "uploads"] or segments[-2:] == [
                    "uploads",
                    "",
                ]:
                    index = segments.index("blobs")
                    return self._upload(
                        "/".join(segments[2:index]), segments[-1], parts.query
                    )
                if "blobs" in segments:
                    index = segments.index("blobs")
                    return self._blob("/".join(segments[2:index]), segments[-1])
                return self._send(404)

            def _manifest(self, repository: str, reference: str) -> None:
                if self.command == "PUT":
                    body = self.rfile.read(int(self.headers["Content-Length"]))
                    digest = sha(body)
                    media_type = self.headers["Content-Type"]
                    registry.manifests[(repository, reference)] = (body, media_type)
                    registry.manifests[(repository, digest)] = (body, media_type)
                    return self._send(201, headers={"Docker-Content-Digest": digest})
                stored = registry.manifests.get((repository, reference))
                if stored is None:
                    return self._send(404)
                body, media_type = stored
                served = registry.tampered_manifests.get(sha(body), body)
                return self._send(
                    200,
                    served,
                    {"Docker-Content-Digest": sha(body), "Content-Type": media_type},
                )

            def _blob(self, repository: str, digest: str) -> None:
                if digest not in registry.repository_blobs.get(repository, set()):
                    return self._send(404)
                body = registry.tampered_blobs.get(digest, registry.blobs[digest])
                return self._send(200, body, {"Docker-Content-Digest": digest})

            def _upload(self, repository: str, upload: str, query: str) -> None:
                if self.command == "POST":
                    identifier = str(uuid.uuid4())
                    registry.uploads[identifier] = repository
                    location = (
                        f"http://127.0.0.1:{registry.port}/v2/{repository}"
                        f"/blobs/uploads/{identifier}?_state=abc"
                    )
                    return self._send(202, headers={"Location": location})
                if self.command != "PUT" or registry.uploads.get(upload) != repository:
                    return self._send(404)
                digest = parse_qs(query)["digest"][0]
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if sha(body) != digest:
                    return self._send(400)
                registry.blobs[digest] = body
                registry.repository_blobs.setdefault(repository, set()).add(digest)
                return self._send(201, headers={"Docker-Content-Digest": digest})

            do_GET = do_HEAD = do_PUT = do_POST = _route

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def add(self, repository: str, image: dict[str, Any], tag: str = "latest") -> None:
        for digest, body in image["blobs"].items():
            self.blobs[digest] = body
            self.repository_blobs.setdefault(repository, set()).add(digest)
        for reference in (tag, image["digest"]):
            self.manifests[(repository, reference)] = (image["manifest"], OCI_MANIFEST)


class RegistryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.layout_path = self.directory / "backup"
        self.images = {name: make_image(name) for name in ("ateapi", "atelet")}
        self.pins = {name: image["digest"] for name, image in self.images.items()}
        self.source = FakeRegistry()
        self.target = FakeRegistry()
        for name, image in self.images.items():
            self.source.add(manager.ko_md5_repository(name), image)

    def tearDown(self) -> None:
        self.source.close()
        self.target.close()
        self.temporary.cleanup()

    def export(self, **pins: str) -> dict[str, str]:
        with manager.Layout(self.layout_path) as layout:
            return manager.export_images(
                manager.Registry(self.source.address),
                layout,
                TAG,
                pins or self.pins,
                "ko-md5",
            )

    def restore(self) -> dict[str, str]:
        with manager.Layout(self.layout_path) as layout:
            return manager.import_images(
                manager.Registry(self.target.address), layout, TAG, self.pins
            )


class SeedBackupRestoreTests(RegistryCase):
    def test_ko_md5_names_are_the_manual_lab_repositories(self) -> None:
        for name, repository in MANUAL_LAB_REPOSITORIES.items():
            with self.subTest(name=name):
                self.assertEqual(manager.ko_md5_repository(name), repository)

    def test_seed_and_restore_are_byte_exact(self) -> None:
        self.assertEqual(self.export(), {"ateapi": "exported", "atelet": "exported"})
        # The layout: root 0700, blobs 0600, one reference per image and tag.
        self.assertEqual(stat.S_IMODE(self.layout_path.stat().st_mode), 0o700)
        blobs = self.layout_path / "blobs/sha256"
        self.assertEqual(stat.S_IMODE(blobs.stat().st_mode), 0o700)
        for path in blobs.iterdir():
            with self.subTest(blob=path.name):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(sha(path.read_bytes()), "sha256:" + path.name)
        index = json.loads((self.layout_path / "index.json").read_text())
        self.assertEqual(
            [
                (entry["annotations"][manager.REF_NAME], entry["digest"])
                for entry in index["manifests"]
            ],
            [
                (f"ateapi:{TAG}", self.pins["ateapi"]),
                (f"atelet:{TAG}", self.pins["atelet"]),
            ],
        )
        self.assertEqual(self.restore(), {"ateapi": "imported", "atelet": "imported"})
        # The restored registry serves the pinned bytes under the base name.
        for name, image in self.images.items():
            with self.subTest(name=name):
                body, media_type = self.target.manifests[(name, TAG)]
                self.assertEqual(body, image["manifest"])
                self.assertEqual(media_type, OCI_MANIFEST)
                for digest, blob in image["blobs"].items():
                    self.assertEqual(self.target.blobs[digest], blob)
        # Idempotent: nothing is downloaded or uploaded a second time.
        self.source.requests.clear()
        self.target.requests.clear()
        self.assertEqual(self.export(), {"ateapi": "present", "atelet": "present"})
        self.assertEqual(self.source.requests, [])
        self.assertEqual(self.restore(), {"ateapi": "present", "atelet": "present"})
        self.assertFalse(
            [
                request
                for request in self.target.requests
                if request[0] in ("PUT", "POST")
            ]
        )

    def test_restore_puts_a_moved_tag_back_on_its_pin(self) -> None:
        self.export()
        self.restore()
        other = make_image("intruder")
        self.target.add("ateapi", other, tag=TAG)
        with manager.Layout(self.layout_path) as layout:
            status = manager.image_status(
                manager.Registry(self.target.address), layout, TAG, self.pins
            )
        self.assertEqual(status["registry"], {"ateapi": "moved", "atelet": "pinned"})
        self.assertEqual(self.restore()["ateapi"], "imported")
        self.assertEqual(
            self.target.manifests[("ateapi", TAG)][0], self.images["ateapi"]["manifest"]
        )

    def test_a_manifest_other_than_the_pin_is_never_copied(self) -> None:
        pinned = self.images["ateapi"]
        self.source.tampered_manifests[pinned["digest"]] = pinned["manifest"] + b" "
        with self.assertRaisesRegex(manager.SubstrateError, "do not hash to"):
            self.export(ateapi=pinned["digest"])
        with manager.Layout(self.layout_path) as layout:
            layout.open()
            self.assertIsNone(layout.reference(f"ateapi:{TAG}"))

    def test_a_corrupt_blob_is_never_written(self) -> None:
        pinned = self.images["atelet"]
        layer = next(iter(sorted(pinned["blobs"])))
        self.source.tampered_blobs[layer] = b"x" * len(pinned["blobs"][layer])
        with self.assertRaisesRegex(manager.SubstrateError, "does not match"):
            self.export(atelet=pinned["digest"])
        self.assertFalse((self.layout_path / "blobs/sha256" / layer[7:]).exists())

    def test_blobs_stream_chunk_by_chunk_and_are_verified_at_the_end(self) -> None:
        pinned = self.images["atelet"]
        layer = sorted(pinned["blobs"])[0]
        body = pinned["blobs"][layer]
        descriptor = {"digest": layer, "size": len(body)}
        registry = manager.Registry(self.source.address)
        repository = manager.ko_md5_repository("atelet")
        with mock.patch.object(manager, "CHUNK", 1000):
            chunks = list(registry.iter_blob(repository, descriptor))
            self.assertGreater(len(chunks), 1)
            self.assertEqual(b"".join(chunks), body)
            # A tampered blob is handed out as it arrives, never held whole,
            # and fails once it ends: the consumer discards what it wrote.
            self.source.tampered_blobs[layer] = b"x" * len(body)
            received = []
            with self.assertRaisesRegex(manager.SubstrateError, "does not match"):
                received.extend(registry.iter_blob(repository, descriptor))
            self.assertEqual(len(received), len(chunks))
            self.source.tampered_blobs[layer] = body + b"x"
            with self.assertRaisesRegex(manager.SubstrateError, "larger than"):
                list(registry.iter_blob(repository, descriptor))

    def test_the_upload_body_reads_across_chunks_without_copying_the_rest(
        self,
    ) -> None:
        reader = manager.IteratorReader(iter([b"abc", b"", b"defgh", b"i"]))
        self.assertEqual(reader.read(2), b"ab")
        self.assertEqual(reader.read(4), b"cdef")
        self.assertEqual(reader.read(0), b"")
        self.assertEqual(reader.read(-1), b"ghi")
        self.assertEqual(reader.read(5), b"")

    def test_forget_drops_only_the_named_old_pin_of_a_re_pinned_image(self) -> None:
        self.export()
        repinned = "sha256:" + "d" * 64
        # Until the entry of the old pin is gone, every read of the new
        # pin under the same version stops.
        with (
            manager.Layout(self.layout_path) as layout,
            self.assertRaisesRegex(manager.SubstrateError, "another digest"),
        ):
            layout.status("ateapi", TAG, repinned)
        with manager.Layout(self.layout_path) as layout:
            with self.assertRaisesRegex(
                manager.SubstrateError, f"names ateapi:{TAG} with sha256:.*, not"
            ):
                manager.forget_images(layout, TAG, {"ateapi": repinned})
            self.assertEqual(
                manager.forget_images(layout, TAG, {"ateapi": self.pins["ateapi"]}),
                {"ateapi": "forgotten"},
            )
            self.assertEqual(layout.status("ateapi", TAG, repinned), "missing")
            self.assertEqual(
                layout.status("atelet", TAG, self.pins["atelet"]), "complete"
            )
            self.assertEqual(
                manager.forget_images(layout, TAG, {"ateapi": self.pins["ateapi"]}),
                {"ateapi": "absent"},
            )
        # The blobs stay, content-addressed, and a new copy reuses them.
        self.assertTrue(
            (self.layout_path / "blobs/sha256" / self.pins["ateapi"][7:]).exists()
        )
        self.assertEqual(
            self.export(ateapi=self.pins["ateapi"]), {"ateapi": "exported"}
        )

    def test_a_corrupt_backup_fails_closed(self) -> None:
        self.export()
        layer = sorted(self.images["ateapi"]["blobs"])[1]
        path = self.layout_path / "blobs/sha256" / layer[7:]
        path.write_bytes(b"y" * path.stat().st_size)
        with (
            manager.Layout(self.layout_path) as layout,
            self.assertRaisesRegex(manager.SubstrateError, "is corrupt"),
        ):
            layout.status("ateapi", TAG, self.pins["ateapi"])
        with self.assertRaisesRegex(manager.SubstrateError, "is corrupt"):
            self.restore()
        self.assertNotIn(("ateapi", TAG), self.target.manifests)

    def test_layout_refuses_links_loose_modes_and_repointed_names(self) -> None:
        self.export()
        blobs = self.layout_path / "blobs/sha256"
        layer = sorted(self.images["atelet"]["blobs"])[0][7:]
        original = (blobs / layer).read_bytes()
        (blobs / layer).chmod(0o644)
        with (
            manager.Layout(self.layout_path) as layout,
            self.assertRaisesRegex(manager.SubstrateError, "is not mode 0600"),
        ):
            layout.status("atelet", TAG, self.pins["atelet"])
        (blobs / layer).unlink()
        (self.directory / "elsewhere").write_bytes(original)
        (blobs / layer).symlink_to(self.directory / "elsewhere")
        with (
            manager.Layout(self.layout_path) as layout,
            self.assertRaisesRegex(manager.SubstrateError, "cannot open safely"),
        ):
            layout.status("atelet", TAG, self.pins["atelet"])
        with manager.Layout(self.layout_path) as layout:
            layout.open()
            with self.assertRaisesRegex(manager.SubstrateError, "another digest"):
                layout.add_reference(
                    f"ateapi:{TAG}",
                    {
                        "mediaType": OCI_MANIFEST,
                        "digest": "sha256:" + "0" * 64,
                        "size": 1,
                    },
                )
        link = self.directory / "linked"
        link.symlink_to(self.layout_path)
        with self.assertRaisesRegex(manager.SubstrateError, "not a safe directory"):
            with manager.Layout(link) as layout:
                layout.open()

    def test_image_status_reads_without_writing(self) -> None:
        with manager.Layout(self.layout_path) as layout:
            status = manager.image_status(None, layout, TAG, self.pins)
        self.assertEqual(
            status,
            {"registry": None, "backup": {"ateapi": "missing", "atelet": "missing"}},
        )
        self.assertFalse(self.layout_path.exists())
        self.export(ateapi=self.pins["ateapi"])
        with manager.Layout(self.layout_path) as layout:
            status = manager.image_status(
                manager.Registry(self.target.address), layout, TAG, self.pins
            )
        self.assertEqual(
            status,
            {
                "registry": {"ateapi": "missing", "atelet": "missing"},
                "backup": {"ateapi": "complete", "atelet": "missing"},
            },
        )

    def test_only_single_platform_manifests_are_accepted(self) -> None:
        index = json.dumps(
            {"schemaVersion": 2, "mediaType": manager.OCI_INDEX, "manifests": []}
        ).encode()
        with self.assertRaisesRegex(manager.SubstrateError, "single-platform"):
            manager.parse_manifest(index, sha(index))
        duplicate = b'{"schemaVersion": 2, "schemaVersion": 2}'
        with self.assertRaisesRegex(manager.SubstrateError, "duplicate JSON key"):
            manager.parse_manifest(duplicate, sha(duplicate))


class AxImageSetTests(unittest.TestCase):
    """--image-set ax: seeded from the manual lab, backed up, restored."""

    AX_TAG = "f009cc8-issue375"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.layout_path = Path(self.temporary.name) / "backup"
        self.images = {name: make_image(name) for name in manager.AX_IMAGE_NAMES}
        self.pins = {name: image["digest"] for name, image in self.images.items()}
        self.source = FakeRegistry()
        self.target = FakeRegistry()
        # The manual lab: ko's md5 names for the two ko images, the image's
        # own name for the two buildx ones.
        for name, image in self.images.items():
            repository = (
                manager.ko_md5_repository(name, manager.AX_MODULE_PATH)
                if name in manager.AX_KO_IMAGES
                else name
            )
            self.source.add(repository, image)

    def tearDown(self) -> None:
        self.source.close()
        self.target.close()
        self.temporary.cleanup()

    def export(self, pins: dict[str, str], naming: str) -> dict[str, str]:
        with manager.Layout(self.layout_path) as layout:
            return manager.export_images(
                manager.Registry(self.source.address),
                layout,
                self.AX_TAG,
                pins,
                naming,
                "ax",
            )

    def test_ax_ko_names_are_the_manual_lab_repositories(self) -> None:
        # `curl http://127.0.0.1:5001/v2/_catalog` on the manual lab.
        self.assertEqual(
            {
                name: manager.ko_md5_repository(name, manager.AX_MODULE_PATH)
                for name in manager.AX_KO_IMAGES
            },
            {
                "ax-controller": "ax-controller-7ebf6094b73be08cb227c879d4802a93",
                "ax-server": "ax-server-340c3583cc4a989b584acf55b1619e8e",
            },
        )
        self.assertEqual(
            manager.IMAGE_SETS["ax"],
            (
                "github.com/google/ax",
                ("ax-controller", "ax-server", "ax-task-runner", "ax-agents"),
                ("ax-controller", "ax-server"),
            ),
        )

    def test_the_ax_seed_and_restore_are_byte_exact(self) -> None:
        ko = {name: self.pins[name] for name in manager.AX_KO_IMAGES}
        buildx = {name: digest for name, digest in self.pins.items() if name not in ko}
        self.assertEqual(
            self.export(ko, "ko-md5"),
            {"ax-controller": "exported", "ax-server": "exported"},
        )
        self.assertEqual(
            self.export(buildx, "base"),
            {"ax-agents": "exported", "ax-task-runner": "exported"},
        )
        with manager.Layout(self.layout_path) as layout:
            self.assertEqual(
                manager.import_images(
                    manager.Registry(self.target.address),
                    layout,
                    self.AX_TAG,
                    self.pins,
                ),
                dict.fromkeys(self.pins, "imported"),
            )
            status = manager.image_status(
                manager.Registry(self.target.address), layout, self.AX_TAG, self.pins
            )
        self.assertEqual(
            status,
            {
                "registry": dict.fromkeys(sorted(self.pins), "pinned"),
                "backup": dict.fromkeys(sorted(self.pins), "complete"),
            },
        )
        for name, image in self.images.items():
            with self.subTest(image=name):
                body, _media_type = self.target.manifests[(name, self.AX_TAG)]
                self.assertEqual(body, image["manifest"])

    def test_ko_naming_only_names_the_images_ko_built(self) -> None:
        for name in ("ax-task-runner", "ax-agents"):
            with (
                self.subTest(image=name),
                self.assertRaisesRegex(manager.SubstrateError, "not a ko image"),
            ):
                self.export({name: self.pins[name]}, "ko-md5")
        self.assertFalse(self.layout_path.exists())

    def test_each_image_set_names_only_its_own_images(self) -> None:
        digest = "sha256:" + "a" * 64
        self.assertEqual(
            manager.parse_pins([f"ax-agents={digest}"], names=manager.AX_IMAGE_NAMES),
            {"ax-agents": digest},
        )
        for values, names in (
            ([f"ax-agents={digest}"], manager.IMAGE_NAMES),
            ([f"ateapi={digest}"], manager.AX_IMAGE_NAMES),
            (["ax-agents=pending"], manager.AX_IMAGE_NAMES),
        ):
            with (
                self.subTest(values=values),
                self.assertRaises(manager.SubstrateError),
            ):
                manager.parse_pins(values, names=names)
        # Only the copies know the AX set; nothing ever builds or installs
        # an AX image on this host.
        root = manager.parser()
        for command in ("image-status", "export", "import", "forget"):
            arguments = (
                ["--registry", "127.0.0.1:5001"]
                if command in ("export", "import")
                else []
            )
            parsed = root.parse_args([command, "--image-set", "ax", *arguments, "--layout", "/x", "--tag", "t"])  # fmt: skip
            self.assertEqual(parsed.image_set, "ax")
        for command in ("build", "install", "cluster-status"):
            with (
                self.subTest(command=command),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                root.parse_args([command, "--image-set", "ax"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            root.parse_args(["image-status", "--image-set", "other", "--layout", "/x", "--tag", "t"])  # fmt: skip
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            code = manager.main(
                ["image-status", "--image-set", "ax", "--layout", "/nonexistent/x", "--tag", "t", "--image", f"ateapi={digest}"]
            )  # fmt: skip
        self.assertEqual(code, 1)
        self.assertIn("is not one known image", stderr.getvalue())


AX_TAG = AxImageSetTests.AX_TAG
WEB_TAG = "1.0.8"
# Every image of the lab's backup under its version: Substrate's, AX's and the
# panel's.
LAB_TAGS = {
    **dict.fromkeys(manager.IMAGE_NAMES, TAG),
    **dict.fromkeys(manager.AX_IMAGE_NAMES, AX_TAG),
    "ax-web": WEB_TAG,
}
# What a relock pins again under the same version: every Substrate image and
# the two AX images ko builds.
RELOCKED = (*manager.IMAGE_NAMES, *manager.AX_KO_IMAGES)


def write_backup(root: Path, images: dict[str, dict[str, Any]]) -> None:
    """A layout as this script writes its own: one image per <name>:<tag>."""
    with manager.Layout(root) as layout:
        layout.open(create=True)
        for ref_name, image in sorted(images.items()):
            files = {**image["blobs"], image["digest"]: image["manifest"]}
            for digest, body in files.items():
                layout.write_blob({"digest": digest, "size": len(body)}, iter([body]))
            layout.add_reference(
                ref_name,
                {
                    "mediaType": OCI_MANIFEST,
                    "digest": image["digest"],
                    "size": len(image["manifest"]),
                },
            )


def write_build_output(root: Path, images: dict[str, dict[str, Any]], tag: str) -> None:
    """The layout of the build command as the CI uploads it.

    One <name>:<tag> reference per image and the pins.json the staging step
    writes beside it, with the modes of a copy made for another user: the
    manager reads a source that nobody else can write, not only a 0600 one.
    """
    write_backup(root, {f"{name}:{tag}": image for name, image in images.items()})
    pins = {name: image["digest"] for name, image in images.items()}
    (root / "pins.json").write_text(json.dumps({"tag": tag, "images": pins}))
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)


def write_ko_layout(root: Path, image: dict[str, Any], *, listed: bool = True) -> None:
    """The OCI layout ko writes for one image, as the web panel's tests do."""
    # That module imports this one, so it is imported when first used.
    from test_ax_web_deploy_contract import write_layout

    write_layout(root, image, listed=listed)


def blob_path(root: Path, digest: str) -> Path:
    return root / "blobs/sha256" / digest.split(":")[1]


def blob_names(root: Path) -> set[str]:
    return {path.name for path in (root / "blobs/sha256").iterdir()}


def image_blob_names(*images: dict[str, Any]) -> set[str]:
    """The files that hold these images: their blobs and their manifests."""
    return {
        digest.split(":")[1]
        for image in images
        for digest in (*image["blobs"], image["digest"])
    }


def digests(
    images: dict[str, dict[str, Any]], names: tuple[str, ...]
) -> dict[str, str]:
    return {name: images[name]["digest"] for name in names}


class RelockCase(unittest.TestCase):
    """The backup layout a relock window changes, and the sources it reads.

    Each layout is a direct child of one temporary directory: the manager
    refuses any directory or file that group or others can write, and a bare
    mkdir under umask 0007 makes one.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.backup = self.directory / "images"
        self.source = self.directory / "source"
        self.snapshot = self.directory / "snapshot"
        # The images the lab holds, and the ones a relock pins in their place
        # under the same names and versions: other digests.
        self.old = {name: make_image(f"{name}-old") for name in LAB_TAGS}
        self.new = {name: make_image(f"{name}-new") for name in RELOCKED}

    def write_old_backup(self) -> None:
        """The backup as the lab has it: its twelve images under their tags."""
        write_backup(
            self.backup,
            {f"{name}:{LAB_TAGS[name]}": self.old[name] for name in LAB_TAGS},
        )

    def seed(self, source: Path, tag: str, pins: dict[str, str]) -> dict[str, str]:
        with (
            manager.Layout(source, strict=False) as from_layout,
            manager.Layout(self.backup) as layout,
        ):
            return manager.seed_layout(from_layout, layout, tag, pins)

    def forget(self, tag: str, pins: dict[str, str]) -> dict[str, str]:
        with manager.Layout(self.backup) as layout:
            return manager.forget_images(layout, tag, pins)

    def status(self, tag: str, pins: dict[str, str]) -> dict[str, str]:
        layout = manager.Layout(self.backup)
        return manager.image_status(None, layout, tag, pins)["backup"]

    def index(self, root: Path | None = None) -> list[dict[str, Any]]:
        index = json.loads(((root or self.backup) / "index.json").read_text())
        return index["manifests"]

    def index_bytes(self) -> bytes:
        return (self.backup / "index.json").read_bytes()

    def refs(self) -> dict[str, str]:
        """The digest the backup names each <name>:<tag> with."""
        return {
            entry["annotations"][manager.REF_NAME]: entry["digest"]
            for entry in self.index()
        }

    def named(self, name: str, tag: str) -> str | None:
        """The digest the backup names <name>:<tag> with, if it names it."""
        with manager.Layout(self.backup) as layout:
            layout.open()
            entry = layout.reference(f"{name}:{tag}")
        return None if entry is None else entry["digest"]


class SeedLayoutSourceTests(RelockCase):
    """seed-layout from the layouts the CI uploads: build output and ko's."""

    def test_a_build_output_layout_seeds_the_seven_substrate_images(self) -> None:
        images = {name: self.new[name] for name in manager.IMAGE_NAMES}
        unrelated = make_image("unrelated")
        write_build_output(self.source, {**images, "unrelated": unrelated}, TAG)
        pins = digests(self.new, manager.IMAGE_NAMES)
        names = sorted(pins)
        self.assertEqual(len(names), 7)
        self.assertEqual(
            self.seed(self.source, TAG, pins), dict.fromkeys(names, "seeded")
        )
        self.assertEqual(
            self.seed(self.source, TAG, pins), dict.fromkeys(names, "present")
        )
        self.assertEqual(self.status(TAG, pins), dict.fromkeys(names, "complete"))
        # Only the pinned images were copied: not the source's unrelated image,
        # not its pins.json.
        self.assertEqual(blob_names(self.backup), image_blob_names(*images.values()))
        self.assertEqual(
            sorted(path.name for path in self.backup.iterdir()),
            ["blobs", "index.json", "oci-layout"],
        )
        self.assertEqual(
            self.refs(), {f"{name}:{TAG}": digest for name, digest in pins.items()}
        )
        # Root-only like the rest of the backup, whatever the source's modes.
        self.assertEqual(stat.S_IMODE(self.backup.stat().st_mode), 0o700)
        for path in (self.backup / "blobs/sha256").iterdir():
            with self.subTest(blob=path.name):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_each_ko_layout_seeds_its_one_image_beside_the_substrate_entries(
        self,
    ) -> None:
        write_backup(
            self.backup,
            {f"{name}:{TAG}": self.old[name] for name in manager.IMAGE_NAMES},
        )
        substrate = digests(self.old, manager.IMAGE_NAMES)
        for name in manager.AX_KO_IMAGES:
            layout = self.directory / f"ko-{name}"
            write_ko_layout(layout, self.new[name])
            # The window reads the pin from the layout the image was built in.
            (entry,) = self.index(layout)
            self.assertEqual(entry["digest"], self.new[name]["digest"])
            self.assertEqual(
                self.seed(layout, AX_TAG, {name: entry["digest"]}), {name: "seeded"}
            )
        ko = digests(self.new, manager.AX_KO_IMAGES)
        self.assertEqual(
            self.status(AX_TAG, ko), dict.fromkeys(manager.AX_KO_IMAGES, "complete")
        )
        self.assertEqual(
            self.status(TAG, substrate), dict.fromkeys(sorted(substrate), "complete")
        )
        self.assertEqual(
            self.refs(),
            {
                **{f"{name}:{TAG}": digest for name, digest in substrate.items()},
                **{f"{name}:{AX_TAG}": digest for name, digest in ko.items()},
            },
        )

    def test_a_ko_layout_lists_only_its_own_image(self) -> None:
        controller, server = (self.new[name] for name in manager.AX_KO_IMAGES)
        pins = {"ax-controller": controller["digest"], "ax-server": server["digest"]}
        write_ko_layout(self.source, controller)
        # One call per ko layout: the second pin is not in the first's.
        with self.assertRaisesRegex(
            manager.SubstrateError, f"does not list {server['digest']}"
        ):
            self.seed(self.source, AX_TAG, pins)
        self.assertEqual(
            self.status(AX_TAG, {"ax-server": server["digest"]}),
            {"ax-server": "missing"},
        )
        # A manifest that is in the blobs but not in index.json is not listed.
        shutil.rmtree(self.source)
        shutil.rmtree(self.backup)
        write_ko_layout(self.source, controller, listed=False)
        with self.assertRaisesRegex(
            manager.SubstrateError, f"does not list {controller['digest']}"
        ):
            self.seed(self.source, AX_TAG, {"ax-controller": controller["digest"]})

    def test_a_source_that_cannot_prove_the_pin_is_refused_and_leaves_nothing(
        self,
    ) -> None:
        image = self.new["atelet"]
        digest = image["digest"]
        pin = {"atelet": digest}
        layer = list(image["blobs"])[-1]
        body = image["blobs"][layer]

        def edit_index(source: Path, **change: Any) -> None:
            document = json.loads((source / "index.json").read_text())
            for entry in document["manifests"]:
                if entry["digest"] == digest:
                    entry.update(change)
            (source / "index.json").write_text(json.dumps(document))

        def unlist(source: Path) -> None:
            document = json.loads((source / "index.json").read_text())
            document["manifests"] = []
            (source / "index.json").write_text(json.dumps(document))

        def replace(source: Path, blob: str, data: bytes) -> None:
            blob_path(source, blob).write_bytes(data)

        for label, pattern, mutate in (
            ("pin not listed", f"does not list {digest}", unlist),
            (
                "other media type",
                "describes .* as another type",
                lambda source: edit_index(source, mediaType=manager.DOCKER_MANIFEST),
            ),
            (
                "other size",
                "describes .* as another type",
                lambda source: edit_index(source, size=len(image["manifest"]) + 1),
            ),
            (
                "tampered layer",
                f"blob {layer} is corrupt in",
                lambda source: replace(source, layer, b"x" * len(body)),
            ),
            (
                "longer layer",
                f"blob {layer} is too large",
                lambda source: replace(source, layer, body + b"x"),
            ),
            (
                "swapped manifest",
                f"manifest bytes do not hash to {digest}",
                lambda source: replace(source, digest, self.old["atelet"]["manifest"]),
            ),
        ):
            with self.subTest(case=label):
                for path in (self.source, self.backup):
                    shutil.rmtree(path, ignore_errors=True)
                write_build_output(self.source, {"atelet": image}, TAG)
                mutate(self.source)
                with self.assertRaisesRegex(manager.SubstrateError, pattern):
                    self.seed(self.source, TAG, pin)
                self.assertEqual(self.status(TAG, pin), {"atelet": "missing"})
                self.assertEqual(self.index(), [])
                # What was copied before the refusal is verified blobs only.
                for path in (self.backup / "blobs/sha256").iterdir():
                    self.assertEqual(sha(path.read_bytes()), "sha256:" + path.name)


class ReseedTests(RelockCase):
    """Re-pinning under the same version: forget the old pin, seed the new."""

    def setUp(self) -> None:
        super().setUp()
        self.write_old_backup()
        write_build_output(
            self.source,
            {name: self.new[name] for name in manager.IMAGE_NAMES},
            TAG,
        )
        self.old_pins = digests(self.old, manager.IMAGE_NAMES)
        self.new_pins = digests(self.new, manager.IMAGE_NAMES)

    def complete(self, names: tuple[str, ...] | list[str]) -> dict[str, str]:
        return dict.fromkeys(sorted(names), "complete")

    def move_ko_images(self) -> None:
        """The window's AX step: forget the old pins, seed each from its layout."""
        old = digests(self.old, manager.AX_KO_IMAGES)
        self.assertEqual(
            self.forget(AX_TAG, old), dict.fromkeys(manager.AX_KO_IMAGES, "forgotten")
        )
        for name in manager.AX_KO_IMAGES:
            layout = self.directory / f"ko-{name}"
            write_ko_layout(layout, self.new[name])
            pin = {name: self.new[name]["digest"]}
            self.assertEqual(self.seed(layout, AX_TAG, pin), {name: "seeded"})

    def test_a_mismatching_digest_never_changes_an_entry(self) -> None:
        before = self.index_bytes()
        blobs = blob_names(self.backup)
        # The new pin cannot be seeded while the old one is named, in one call
        # for all the images or image by image.
        singles = [{name: pin} for name, pin in self.new_pins.items()]
        for pins in (self.new_pins, *singles):
            first = min(pins)
            with (
                self.subTest(seed=sorted(pins)),
                self.assertRaisesRegex(
                    manager.SubstrateError, f"names {first}:{TAG} with another digest"
                ),
            ):
                self.seed(self.source, TAG, pins)
            self.assertEqual(self.index_bytes(), before)
            self.assertEqual(blob_names(self.backup), blobs)
        # Nor forgotten: forget names the exact digest it drops.
        for name, pin in self.new_pins.items():
            with (
                self.subTest(forget=name),
                self.assertRaisesRegex(
                    manager.SubstrateError,
                    f"names {name}:{TAG} with {self.old_pins[name]}, not {pin}",
                ),
            ):
                self.forget(TAG, {name: pin})
            self.assertEqual(self.index_bytes(), before)
        self.assertEqual(
            self.status(TAG, self.old_pins), self.complete(manager.IMAGE_NAMES)
        )

    def test_a_re_pin_goes_through_forget_then_seed_and_keeps_the_old_blobs(
        self,
    ) -> None:
        names = sorted(manager.IMAGE_NAMES)
        with self.assertRaisesRegex(manager.SubstrateError, "another digest"):
            self.status(TAG, self.new_pins)
        # The literal old digests, as the previous config pinned them.
        self.assertEqual(
            self.forget(TAG, self.old_pins), dict.fromkeys(names, "forgotten")
        )
        self.assertEqual(
            self.status(TAG, self.new_pins), dict.fromkeys(names, "missing")
        )
        self.assertEqual(
            self.seed(self.source, TAG, self.new_pins), dict.fromkeys(names, "seeded")
        )
        self.assertEqual(self.status(TAG, self.new_pins), self.complete(names))
        # The old manifests and layers are still on disk: no blob is deleted.
        old_files = image_blob_names(*(self.old[name] for name in names))
        self.assertLessEqual(old_files, blob_names(self.backup))
        # The other image sets were never touched.
        ax = digests(self.old, manager.AX_IMAGE_NAMES)
        self.assertEqual(self.status(AX_TAG, ax), self.complete(manager.AX_IMAGE_NAMES))
        web = digests(self.old, ("ax-web",))
        self.assertEqual(self.status(WEB_TAG, web), self.complete(("ax-web",)))

    def test_only_the_re_pinned_ax_images_move_each_from_its_own_ko_layout(
        self,
    ) -> None:
        untouched = digests(self.old, ("ax-agents", "ax-task-runner"))
        before = {name: self.named(name, AX_TAG) for name in untouched}
        self.move_ko_images()
        self.assertEqual(
            self.status(
                AX_TAG, {**digests(self.new, manager.AX_KO_IMAGES), **untouched}
            ),
            self.complete(manager.AX_IMAGE_NAMES),
        )
        # The two images ko does not build still name their own pins.
        self.assertEqual({name: self.named(name, AX_TAG) for name in untouched}, before)
        self.assertEqual(before, untouched)
        ko_files = image_blob_names(*(self.old[name] for name in manager.AX_KO_IMAGES))
        self.assertLessEqual(ko_files, blob_names(self.backup))
        self.assertEqual(
            self.status(TAG, self.old_pins), self.complete(manager.IMAGE_NAMES)
        )

    def test_the_rollback_restores_each_image_from_a_copy_of_the_original(
        self,
    ) -> None:
        names = sorted(manager.IMAGE_NAMES)
        # `cp -a` of the layout before any forget: it keeps the modes, so it is
        # a strict layout read as a source, as the window's command reads it.
        shutil.copytree(self.backup, self.snapshot, symlinks=True)
        self.assertEqual(stat.S_IMODE(self.snapshot.stat().st_mode), 0o700)
        original = self.index()
        # A window that stopped part-way: most images moved to the new pin, one
        # was forgotten and never seeded, one was not touched yet.
        crashed, untouched = names[3], names[4]
        for name in names:
            if name != untouched:
                self.forget(TAG, {name: self.old_pins[name]})
            if name not in (crashed, untouched):
                self.seed(self.source, TAG, {name: self.new_pins[name]})
        outcomes = {}
        for name in names:
            new, old = self.new_pins[name], self.old_pins[name]
            before = self.index_bytes()
            try:
                forgot = self.forget(TAG, {name: new})[name]
            except manager.SubstrateError as error:
                # The entry names the old pin already: nothing to roll back.
                self.assertRegex(
                    str(error), f"names {name}:{TAG} with {old}, not {new}"
                )
                self.assertEqual(self.index_bytes(), before)
                forgot = "kept"
            outcomes[name] = (forgot, self.seed(self.snapshot, TAG, {name: old})[name])
        self.assertEqual(
            outcomes,
            {
                **dict.fromkeys(names, ("forgotten", "seeded")),
                crashed: ("absent", "seeded"),
                untouched: ("kept", "present"),
            },
        )
        self.assertEqual(self.status(TAG, self.old_pins), self.complete(names))
        self.assertEqual(self.index(), original)
        self.assertEqual(self.index(self.snapshot), original)
        # Rolled back, the new pin is the one that stops every read again, and
        # the blobs of the images that moved stay on disk like the old ones did.
        with self.assertRaisesRegex(manager.SubstrateError, "another digest"):
            self.status(TAG, self.new_pins)
        moved = (self.new[name] for name in names if name not in (crashed, untouched))
        self.assertLessEqual(image_blob_names(*moved), blob_names(self.backup))

    def test_the_rollback_of_the_ko_images_reads_the_copy_and_keeps_the_rest(
        self,
    ) -> None:
        shutil.copytree(self.backup, self.snapshot, symlinks=True)
        original = self.index()
        self.move_ko_images()
        for name in manager.AX_KO_IMAGES:
            new, old = self.new[name]["digest"], self.old[name]["digest"]
            self.assertEqual(self.forget(AX_TAG, {name: new}), {name: "forgotten"})
            self.assertEqual(
                self.seed(self.snapshot, AX_TAG, {name: old}), {name: "seeded"}
            )
        self.assertEqual(
            self.status(AX_TAG, digests(self.old, manager.AX_IMAGE_NAMES)),
            self.complete(manager.AX_IMAGE_NAMES),
        )
        self.assertEqual(self.index(), original)


class ArgumentTests(unittest.TestCase):
    def test_the_registry_is_only_ever_loopback(self) -> None:
        self.assertEqual(manager.parse_registry("127.0.0.1:5001"), ("127.0.0.1", 5001))
        self.assertEqual(manager.parse_registry("localhost:5001"), ("localhost", 5001))
        for address in (
            "10.0.0.1:5001",
            "registry.example.com:5001",
            "127.0.0.1:80",
            "127.0.0.1",
            "[::1]:5001",
            "127.0.0.1:5001/path",
        ):
            with (
                self.subTest(address=address),
                self.assertRaisesRegex(manager.SubstrateError, "loopback"),
            ):
                manager.parse_registry(address)

    def test_pins_name_known_images_by_sha256_once(self) -> None:
        digest = "sha256:" + "a" * 64
        self.assertEqual(manager.parse_pins([f"ateapi={digest}"]), {"ateapi": digest})
        self.assertEqual(
            manager.parse_pins(["ate-setup=pending"], allow_pending=True),
            {"ate-setup": None},
        )
        for values in (
            ["ate-setup=pending"],
            [f"other={digest}"],
            [f"ateapi={digest}", f"ateapi={digest}"],
            ["ateapi=sha256:" + "A" * 64],
            ["ateapi"],
            [],
        ):
            with (
                self.subTest(values=values),
                self.assertRaises(manager.SubstrateError),
            ):
                manager.parse_pins(values)

    def test_mutating_commands_take_the_host_lock(self) -> None:
        self.assertEqual(
            manager.MUTATING_COMMANDS,
            {"export", "import", "seed-layout", "forget", "build", "install"},
        )
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'ensure_host_lock(f"ax-lab-substrate-{arguments.command}"', source
        )


class FakeLockModule(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("host_global_operation_lock")
        self.calls: list[tuple[str, list[str]]] = []

    def ensure_mutation_lock(self, operation: str, command: list[str], *, caller: Path):
        self.calls.append((operation, command))


class LockTests(unittest.TestCase):
    def check(self, swarm: str, scope: str | None) -> FakeLockModule:
        fake = FakeLockModule()
        docker = mock.Mock()
        docker.swarm_state.return_value = swarm
        environment = {} if scope is None else {"DOCKERSWARM_IAC_LOCK_SCOPE": scope}
        with (
            mock.patch.dict(sys.modules, {"host_global_operation_lock": fake}),
            mock.patch.dict(os.environ, environment, clear=False),
        ):
            if scope is None:
                os.environ.pop("DOCKERSWARM_IAC_LOCK_SCOPE", None)
            manager.ensure_host_lock("ax-lab-substrate-import", docker)
        return fake

    def test_a_swarm_host_runs_every_mutation_under_the_lock(self) -> None:
        for swarm in ("active", "pending", "locked", "error"):
            with self.subTest(swarm=swarm):
                calls = self.check(swarm, None).calls
                self.assertEqual(
                    [call[0] for call in calls], ["ax-lab-substrate-import"]
                )
        # Under an Ansible run the proof is checked whatever the state.
        self.assertEqual(len(self.check("inactive", "ansible").calls), 1)
        # A host without Swarm, such as a CI runner, has nothing to serialise.
        self.assertEqual(self.check("inactive", None).calls, [])
        with self.assertRaisesRegex(manager.SubstrateError, "unknown local Swarm"):
            self.check("weird", None)


class ClusterStatusTests(unittest.TestCase):
    PINS = {"ateapi": "sha256:" + "1" * 64, "atenet": "sha256:" + "2" * 64}

    def kubectl(self, workloads: dict[str, str], create_once: dict[str, str]):
        calls = []

        class FakeKubectl(manager.Kubectl):
            def get(self, arguments):  # noqa: ANN001
                calls.append(list(arguments))
                if arguments[0] == "node":
                    return "67253354"
                if arguments[0] == manager.WORKLOAD_KINDS:
                    return workloads.get(arguments[2], "")
                return create_once.get(arguments[1], "")

        return FakeKubectl("/k", "/c", "kind-kind", "/h"), calls

    @staticmethod
    def line(
        kind: str, name: str, *, init: str = "", images: str, **status: str
    ) -> str:
        fields = {name: "" for name in manager.FIELD_NAMES}
        fields.update(kind=kind, name=name, uid=f"uid-{name}", initImages=init)
        fields.update(images=images, **status)
        return "\t".join(fields[key] for key in manager.FIELD_NAMES)

    def test_reads_identities_images_and_rollouts_only(self) -> None:
        # Never the environment of a pod template, never Secret data.
        self.assertFalse(any("env" in path for path in manager.WORKLOAD_FIELDS))
        self.assertNotIn(".data", manager.WORKLOAD_JSONPATH)
        self.assertNotIn(".env", manager.WORKLOAD_JSONPATH)
        ateapi = f"localhost:5001/ateapi:{TAG}@{self.PINS['ateapi']}"
        moved = f"localhost:5001/atenet:{TAG}@sha256:" + "9" * 64
        postgres = "postgres:18-alpine@sha256:" + "3" * 64
        workloads = {
            "ate-system": "\n".join(
                [
                    self.line(
                        "Deployment",
                        "ate-api-server",
                        images=ateapi,
                        generation="2",
                        observedGeneration="2",
                        specReplicas="2",
                        replicas="2",
                        updatedReplicas="2",
                        availableReplicas="2",
                    ),
                    self.line(
                        "StatefulSet",
                        "postgres",
                        init=postgres,
                        images=postgres,
                        generation="1",
                        observedGeneration="1",
                        specReplicas="1",
                        readyReplicas="0",
                        currentRevision="postgres-1",
                        updateRevision="postgres-1",
                    ),
                    self.line(
                        "Deployment",
                        "atenet-router",
                        images=f"{moved} envoy:x",
                        generation="1",
                        observedGeneration="1",
                        specReplicas="1",
                        replicas="1",
                        updatedReplicas="1",
                        availableReplicas="1",
                    ),
                ]
            )
            + "\n",
        }
        create_once = {
            "actor-id-jwt-pool": "u1\tOpaque",
            "service-dns-ca-pool": "u2\tOpaque",
            "ate-api-authentication": "u3\t",
        }
        kubectl, calls = self.kubectl(workloads, create_once)
        status = manager.cluster_status(
            kubectl, "kind-control-plane", "localhost:5001", TAG, self.PINS
        )
        self.assertEqual(status["node_label"], "67253354")
        self.assertEqual(
            status["workloads"],
            [
                {
                    "kind": "Deployment",
                    "namespace": "ate-system",
                    "name": "ate-api-server",
                    "images": ["ateapi"],
                },
                {
                    "kind": "Deployment",
                    "namespace": "ate-system",
                    "name": "atenet-router",
                    "images": [moved, "envoy:x"],
                },
                {
                    "kind": "StatefulSet",
                    "namespace": "ate-system",
                    "name": "postgres",
                    "init_images": [postgres],
                    "images": [postgres],
                },
            ],
        )
        self.assertEqual(status["not_ready"], ["ate-system/StatefulSet/postgres"])
        self.assertEqual(
            status["immutable"],
            {
                "ate-system/StatefulSet/postgres": "uid-postgres@1",
                "ate-system/Job/rustfs-bucket-init": None,
            },
        )
        self.assertEqual(
            status["objects"]["ate-system/StatefulSet/postgres"]["generation"], 1
        )
        self.assertEqual(
            status["create_once"],
            {
                "ate-system/Secret/actor-id-jwt-pool": {"uid": "u1", "type": "Opaque"},
                "ate-system/Secret/actor-id-ca-pool": None,
                "ate-system/Secret/actor-id-ca-certs": None,
                "podcertificate-controller-system/Secret/service-dns-ca-pool": {
                    "uid": "u2",
                    "type": "Opaque",
                },
                "podcertificate-controller-system/Secret/pod-identity-ca-pool": None,
                "ate-system/ConfigMap/ate-api-authentication": {
                    "uid": "u3",
                    "type": None,
                },
            },
        )
        # Secrets and the ConfigMap are read only by uid and type.
        for call in calls:
            if call[0] in ("secret", "configmap"):
                self.assertEqual(call[-1], 'jsonpath={.metadata.uid}{"\\t"}{.type}')
        # B1: the shell installer's Secret is not a create-once object here.
        self.assertNotIn(
            "ate-api-server-secret-envvars",
            [name for _ns, _kind, name in manager.CREATE_ONCE],
        )

    def test_readiness_follows_kubectl_rollout_status(self) -> None:
        def ready(kind: str, **fields: str) -> bool:
            values = {name: "" for name in manager.FIELD_NAMES}
            values.update(kind=kind, generation="3", observedGeneration="3")
            values.update(fields)
            return manager.workload_ready(values)

        deployment = dict(
            specReplicas="2", replicas="2", updatedReplicas="2", availableReplicas="2"
        )
        self.assertTrue(ready("Deployment", **deployment))
        self.assertFalse(
            ready("Deployment", **{**deployment, "availableReplicas": "1"})
        )
        self.assertFalse(ready("Deployment", **{**deployment, "replicas": "3"}))
        self.assertFalse(
            ready("Deployment", **{**deployment, "observedGeneration": "2"})
        )
        daemon = dict(
            desiredNumberScheduled="1", updatedNumberScheduled="1", numberAvailable="1"
        )
        self.assertTrue(ready("DaemonSet", **daemon))
        self.assertFalse(ready("DaemonSet", **{**daemon, "numberAvailable": "0"}))
        stateful = dict(
            specReplicas="1", readyReplicas="1", currentRevision="a", updateRevision="a"
        )
        self.assertTrue(ready("StatefulSet", **stateful))
        self.assertFalse(ready("StatefulSet", **{**stateful, "updateRevision": "b"}))
        self.assertTrue(ready("Job", succeeded="1"))
        self.assertFalse(ready("Job", succeeded=""))
        self.assertFalse(ready("CronJob"))


class FakeDocker:
    """`docker` for the bounded runner: containers are dictionaries."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.containers: dict[str, dict[str, Any]] = {}
        self.on_run = None
        self.stdout = ""
        self.stderr = ""
        self.fail_start = False
        self.runs_for = 0
        # What `docker run --detach` prints: the full ID it started.
        self.started_id = "c" * 64 + "\n"
        # The next inspections fail as a daemon that cannot answer would.
        self.inspect_failures = 0

    def run(self, argv):  # noqa: ANN001
        argv = list(argv)
        self.calls.append(argv)
        ok = manager.subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["run", "--detach"]:
            name = argv[argv.index("--name") + 1]
            if self.fail_start:
                self.containers[name] = {"Running": False, "ExitCode": 128}
                return manager.subprocess.CompletedProcess(argv, 125, "", "boom\n")
            self.containers[name] = {
                "Running": self.runs_for > 0,
                "ExitCode": 0,
                "OOMKilled": False,
                "polls": self.runs_for,
            }
            if self.on_run is not None:
                self.on_run(argv)
            return manager.subprocess.CompletedProcess(argv, 0, self.started_id, "")
        if argv[:2] == ["container", "inspect"]:
            name = argv[-1]
            if self.inspect_failures > 0:
                self.inspect_failures -= 1
                return manager.subprocess.CompletedProcess(
                    argv, 1, "", "Error response from daemon: connection refused\n"
                )
            state = self.containers.get(name)
            if state is None:
                return manager.subprocess.CompletedProcess(
                    argv, 1, "", f"Error: No such container: {name}\n"
                )
            if state.get("Running"):
                state["polls"] -= 1
                if state["polls"] < 0:
                    state["Running"] = False
            public = {key: value for key, value in state.items() if key != "polls"}
            return manager.subprocess.CompletedProcess(argv, 0, json.dumps(public), "")
        if argv[0] == "kill":
            state = self.containers.get(argv[1])
            if state is None or not state.get("Running"):
                return manager.subprocess.CompletedProcess(argv, 1, "", "not running\n")
            state.update(Running=False, ExitCode=137)
            return ok
        if argv[0] == "logs":
            return manager.subprocess.CompletedProcess(
                argv, 0, self.stdout, self.stderr
            )
        if argv[0] == "rm":
            state = self.containers[argv[-1]]
            if state.get("Running") and "--force" not in argv:
                return manager.subprocess.CompletedProcess(argv, 1, "", "running\n")
            self.containers.pop(argv[-1])
            return ok
        if argv[0] == "info":
            return manager.subprocess.CompletedProcess(argv, 0, "active\n", "")
        raise AssertionError(argv)

    def checked(self, argv):  # noqa: ANN001
        return manager.Docker.checked(self, argv)

    def state(self, name):  # noqa: ANN001
        return manager.Docker.state(self, name)


class FakeHost(manager.Host):
    """The real Host over a fake /proc and cgroup tree, with a fake clock.

    MemAvailable is only ever written to proc/meminfo, in kB as the kernel
    writes it, so every test reads it through Host's own parser.
    """

    def __init__(self, root: Path, available_mib: int = 8192) -> None:
        proc = root / "proc"
        (proc / "pressure").mkdir(parents=True)
        (proc / "pressure/memory").write_text(
            "some avg10=1.00 avg60=0 avg300=0 total=1\nfull avg10=0.50 avg60=0 avg300=0 total=1\n"
        )
        scope = root / "cgroup" / ("docker-" + "c" * 64 + ".scope")
        scope.mkdir(parents=True)
        (scope / "memory.peak").write_text("1048576\n")
        (scope / "memory.events").write_text(
            "low 0\nhigh 0\nmax 3\noom 0\noom_kill 0\n"
        )
        (scope / "memory.pressure").write_text(
            "full avg10=7.25 avg60=0 avg300=0 total=1\n"
        )
        self.now = 0.0
        super().__init__(
            proc=proc, cgroup=root / "cgroup", clock=self.tick, sleep=self.advance
        )
        self.available = available_mib

    @property
    def available(self) -> int:
        raise AttributeError("read MemAvailable through mem_available_mib()")

    @available.setter
    def available(self, mib: int) -> None:
        (self.proc / "meminfo").write_text(
            f"MemTotal:       16364744 kB\nMemFree:          812344 kB\n"
            f"MemAvailable:   {mib * 1024} kB\nBuffers:           10240 kB\n"
        )

    def tick(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


LIMITS = manager.BuildLimits(
    memory_mib=3072,
    reservation_mib=1536,
    cpu_millicores=2000,
    pids=1024,
    timeout_seconds=3600,
    min_mem_available_mib=3584,
    mem_available_floor_mib=512,
)
TOOLBOX = (
    "docker.io/library/golang:1.27.1@sha256:"
    + "3680233e3204827fbdc66088528ae6d4b3d034f51d03a99d454f6de034888244"
)


class BoundedContainerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.docker = FakeDocker()
        self.host = FakeHost(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_build_command_is_bounded_and_unprivileged(self) -> None:
        argv = manager.build_argv(
            name="ateapi",
            toolbox_image=TOOLBOX,
            source=Path("/opt/dockerswarm/ax-lab/src/substrate"),
            cache=Path("/opt/dockerswarm/ax-lab/cache"),
            version=TAG,
            limits=LIMITS,
        )
        self.assertEqual(
            argv,
            [
                "--name", "ax-lab-build-ateapi",
                "--label", "com.apptolast.managed-by=ansible",
                "--label", "com.apptolast.ax-lab=build",
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges=true",
                "--read-only",
                "--user", "0:0",
                "--memory", "3072m",
                "--memory-swap", "3072m",
                "--memory-reservation", "1536m",
                "--cpus", "2.000",
                "--pids-limit", "1024",
                "--network", "bridge",
                "--mount", "type=bind,source=/opt/dockerswarm/ax-lab/src/substrate,target=/src/substrate,readonly",
                "--mount", "type=bind,source=/opt/dockerswarm/ax-lab/cache,target=/cache",
                "--workdir", "/src/substrate",
                "--env", "GOMAXPROCS=2",
                "--env", "GOFLAGS=-p=1",
                "--env", "GOGC=40",
                "--env", "GOTOOLCHAIN=local",
                "--env", "GOCACHE=/cache/go-build",
                "--env", "GOMODCACHE=/cache/go-mod",
                "--env", "TMPDIR=/cache/tmp",
                "--env", "HOME=/cache/home",
                "--env", "KO_DEFAULTPLATFORMS=linux/amd64",
                "--env", "GIT_CONFIG_COUNT=1",
                "--env", "GIT_CONFIG_KEY_0=safe.directory",
                "--env", "GIT_CONFIG_VALUE_0=/src/substrate",
                TOOLBOX,
                "make", "build-images",
                "KO_DOCKER_REPO=localhost:5001",
                f"VERSION={TAG}",
                "IMAGES=./cmd/ateapi",
                "KO_FLAGS=--push=false --oci-layout-path=/cache/out/ateapi --sbom=none",
            ],
        )  # fmt: skip
        text = " ".join(argv)
        for forbidden in (
            "docker.sock",
            "--privileged",
            "--rm",
            "--network host",
            "--tmpfs",
            "--cap-add",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)
        with self.assertRaisesRegex(manager.SubstrateError, "whole CPUs"):
            manager.build_argv(
                name="ateapi",
                toolbox_image=TOOLBOX,
                source=Path("/s"),
                cache=Path("/c"),
                version=TAG,
                limits=manager.BuildLimits(**{**vars(LIMITS), "cpu_millicores": 1500}),
            )

    def test_install_runs_the_pinned_ate_setup_in_prebuilt_mode(self) -> None:
        settings = manager.InstallSettings(
            registry_port=5001,
            tag=TAG,
            installer_digest="sha256:" + "e" * 64,
            source=Path("/opt/dockerswarm/ax-lab/src/substrate"),
            kubeconfig=Path("/opt/dockerswarm/ax-lab/home/.kube/config"),
            context="kind-kind",
            router="envoy",
            rollout_timeout_seconds=600,
            memory_mib=256,
            reservation_mib=128,
            cpu_millicores=1000,
            pids=256,
        )
        argv = manager.install_argv(settings)
        self.assertEqual(
            argv,
            [
                "--name", "ax-lab-ate-setup",
                "--label", "com.apptolast.managed-by=ansible",
                "--label", "com.apptolast.ax-lab=install",
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges=true",
                "--read-only",
                "--user", "0:0",
                "--memory", "256m",
                "--memory-swap", "256m",
                "--memory-reservation", "128m",
                "--cpus", "1.000",
                "--pids-limit", "256",
                "--network", "host",
                "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m",
                "--mount", "type=bind,source=/opt/dockerswarm/ax-lab/src/substrate,target=/src/substrate,readonly",
                "--mount", "type=bind,source=/opt/dockerswarm/ax-lab/home/.kube/config,target=/kubeconfig,readonly",
                "--workdir", "/src/substrate",
                "--env", "HOME=/tmp",
                "localhost:5001/ate-setup@sha256:" + "e" * 64,
                "--kind", "--no-dev-env",
                "--kubeconfig", "/kubeconfig",
                "--context", "kind-kind",
                "--atenet-router", "envoy",
                "--rollout-timeout", "600s",
                "--image-repo", "localhost:5001",
                "--image-tag", TAG,
                "deploy", "ate-system",
            ],
        )  # fmt: skip
        for forbidden in ("--setup-csi", "delete", "demo", "sdsmint", "agentgateway"):
            self.assertNotIn(forbidden, " ".join(argv))
        with self.assertRaisesRegex(manager.SubstrateError, "envoy"):
            manager.install_argv(
                manager.InstallSettings(**{**vars(settings), "router": "agentgateway"})
            )

    def test_runner_reads_the_exit_then_removes_the_container(self) -> None:
        self.docker.runs_for = 2
        self.docker.stdout = "done\n"
        outcome = manager.run_bounded(
            self.docker, self.host, "ax-lab-build-ateapi", ["--name", "ax-lab-build-ateapi", "img"],
            timeout_seconds=100, mem_available_floor_mib=512,
        )  # fmt: skip
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.memory_peak_bytes, 1048576)
        self.assertEqual(outcome.oom_kill_events, 0)
        self.assertEqual(outcome.max_host_memory_full_avg10, 0.5)
        self.assertEqual(outcome.max_container_memory_full_avg10, 7.25)
        verbs = [call[0] for call in self.docker.calls]
        # No --rm: the state is read before `docker rm`.
        self.assertNotIn("--rm", self.docker.calls[0])
        self.assertLess(
            len(verbs) - 1 - verbs[::-1].index("container"), verbs.index("rm")
        )
        self.assertEqual(self.docker.calls[-1], ["rm", "ax-lab-build-ateapi"])
        self.assertNotIn("ax-lab-build-ateapi", self.docker.containers)

    def test_runner_kills_on_timeout_and_on_the_memory_floor(self) -> None:
        for label, available, expected in (
            ("timeout", 8192, "timeout"),
            ("floor", 400, "mem_available_floor"),
        ):
            with self.subTest(case=label):
                self.docker.runs_for = 10_000
                self.host.available = available
                outcome = manager.run_bounded(
                    self.docker, self.host, "c1", ["--name", "c1", "img"],
                    timeout_seconds=60, mem_available_floor_mib=512,
                )  # fmt: skip
                self.assertEqual(outcome.aborted, expected)
                self.assertFalse(outcome.succeeded)
                self.assertIn(["kill", "c1"], self.docker.calls)
                self.assertEqual(self.docker.calls[-1], ["rm", "c1"])

    def test_oom_kill_is_reported_from_the_exited_container(self) -> None:
        def killed(argv):  # noqa: ANN001
            self.docker.containers["c2"].update(ExitCode=137, OOMKilled=True)

        self.docker.on_run = killed
        outcome = manager.run_bounded(
            self.docker, self.host, "c2", ["--name", "c2", "img"],
            timeout_seconds=60, mem_available_floor_mib=512,
        )  # fmt: skip
        self.assertTrue(outcome.oom_killed)
        self.assertEqual(outcome.exit_code, 137)
        self.assertFalse(outcome.succeeded)

    def test_a_failed_start_removes_only_its_own_container(self) -> None:
        self.docker.fail_start = True
        with self.assertRaisesRegex(manager.SubstrateError, "docker run of c3 failed"):
            manager.run_bounded(
                self.docker, self.host, "c3", ["--name", "c3", "img"],
                timeout_seconds=60, mem_available_floor_mib=512,
            )  # fmt: skip
        # --force: the daemon may have started it before reporting the error.
        self.assertEqual(self.docker.calls[-1], ["rm", "--force", "c3"])
        self.assertNotIn("c3", self.docker.containers)

    def test_an_error_while_running_kills_the_container_and_keeps_it(self) -> None:
        """Nothing may leave a container running past the runner's bounds."""

        def fail_in_loop(argv) -> None:  # noqa: ANN001, ARG001
            self.docker.inspect_failures = 1

        def interrupt(_seconds: float) -> None:
            raise KeyboardInterrupt

        for label, arrange, raised, message in (
            ("daemon error mid-poll", lambda: setattr(self.docker, "on_run", fail_in_loop), manager.SubstrateError, "cannot inspect container c4; docker kill was sent to c4, which is kept for review"),
            ("no container ID", lambda: setattr(self.docker, "started_id", "\n"), manager.SubstrateError, "printed no container ID"),
            ("interrupted", lambda: setattr(self.host, "sleep", interrupt), KeyboardInterrupt, ""),
        ):  # fmt: skip
            with self.subTest(case=label):
                self.docker = FakeDocker()
                self.host = FakeHost(self.root / label.replace(" ", "-"))
                self.docker.runs_for = 10_000
                arrange()
                with self.assertRaises(raised) as caught:
                    manager.run_bounded(
                        self.docker, self.host, "c4", ["--name", "c4", "img"],
                        timeout_seconds=60, mem_available_floor_mib=512,
                    )  # fmt: skip
                self.assertIn(message, str(caught.exception))
                self.assertIn(["kill", "c4"], self.docker.calls)
                # Stopped, never removed: its logs wait for review, and the
                # next run refuses it.
                self.assertFalse(self.docker.containers["c4"]["Running"])
                self.assertNotIn("rm", [call[0] for call in self.docker.calls])
                with self.assertRaisesRegex(
                    manager.SubstrateError, "c4 is left from an earlier run"
                ):
                    manager.refuse_leftover(self.docker, "c4")

    def test_a_leftover_says_whether_it_still_runs(self) -> None:
        manager.refuse_leftover(self.docker, "c5")
        self.docker.containers["c5"] = {"Running": True, "polls": 10**6}
        with self.assertRaisesRegex(
            manager.SubstrateError, "c5 is still running under an earlier run.*wait"
        ):
            manager.refuse_leftover(self.docker, "c5")
        self.docker.containers["c5"] = {"Running": False, "polls": 0}
        with self.assertRaisesRegex(
            manager.SubstrateError, "c5 is left from an earlier run.*docker rm"
        ):
            manager.refuse_leftover(self.docker, "c5")

    def test_mem_available_is_read_in_kib_from_proc_meminfo(self) -> None:
        meminfo = self.root / "proc/meminfo"
        for kib, mib in ((3584 * 1024, 3584), (524287, 511), (4254720, 4155)):
            with self.subTest(kib=kib):
                meminfo.write_text(
                    f"MemTotal:       16364744 kB\nMemAvailable:   {kib} kB\n"
                )
                self.assertEqual(self.host.mem_available_mib(), mib)
                self.assertEqual(
                    manager.Host(proc=self.root / "proc").mem_available_mib(), mib
                )
        meminfo.write_text("MemTotal:       16364744 kB\nMemFree: 1 kB\n")
        with self.assertRaisesRegex(manager.SubstrateError, "no MemAvailable"):
            self.host.mem_available_mib()
        # Below the floor by one MiB read in kB: the runner kills.
        self.docker.runs_for = 10_000
        self.host.available = 511
        outcome = manager.run_bounded(
            self.docker, self.host, "c6", ["--name", "c6", "img"],
            timeout_seconds=60, mem_available_floor_mib=512,
        )  # fmt: skip
        self.assertEqual(outcome.aborted, "mem_available_floor")

    def test_a_termination_signal_becomes_an_error_for_container_commands(
        self,
    ) -> None:
        with self.assertRaisesRegex(manager.Terminated, "SIGTERM"):
            manager.raise_terminated(manager.signal.SIGTERM, None)
        self.assertTrue(issubclass(manager.Terminated, manager.SubstrateError))
        self.assertEqual(manager.CONTAINER_COMMANDS, {"build", "install"})
        digest = "sha256:" + "a" * 64
        for command, expected in (
            (["image-status", "--layout", "/l", "--tag", TAG, "--image", f"ateapi={digest}"], []),
            (["install", "--registry-port", "5001", "--tag", TAG, "--installer", f"ate-setup={digest}", "--source", "/s", "--kubeconfig", "/k", "--context", "kind-kind", "--atenet-router", "envoy", *[value for option in ("--rollout-timeout-seconds", "--memory-mib", "--memory-reservation-mib", "--cpu-millicores", "--pids-limit", "--timeout-seconds", "--min-mem-available-mib", "--mem-available-floor-mib") for value in (option, "1")]], [manager.signal.SIGTERM, manager.signal.SIGHUP]),
        ):  # fmt: skip
            with (
                self.subTest(command=command[0]),
                mock.patch.object(manager, "ensure_host_lock"),
                mock.patch.object(manager, "dispatch", return_value={}),
                mock.patch.object(manager.signal, "signal") as handlers,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(manager.main(command), 0)
                self.assertEqual(
                    [call.args for call in handlers.call_args_list],
                    [(number, manager.raise_terminated) for number in expected],
                )


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.cache.mkdir(mode=0o700)
        self.image = make_image("ate-setup")
        self.docker = FakeDocker()
        self.docker.on_run = self.fake_ko
        self.host = FakeHost(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def fake_ko(self, argv) -> None:  # noqa: ANN001
        """What ko writes: an OCI layout in /cache/out/<name>, and its ref."""
        name = argv[argv.index("--name") + 1].removeprefix("ax-lab-build-")
        with manager.Layout(self.cache / "out" / name) as scratch:
            scratch.open(create=True)
            for digest, body in self.image["blobs"].items():
                scratch.write_blob({"digest": digest, "size": len(body)}, iter([body]))
            scratch.write_blob(
                {"digest": self.image["digest"], "size": len(self.image["manifest"])},
                iter([self.image["manifest"]]),
            )
        self.docker.stdout = f"log line\n/cache/out/{name}@{self.image['digest']}\n"

    def build(self, digest: str | None, **overrides: Any) -> dict[str, Any]:
        with manager.Layout(self.root / "backup") as layout:
            return manager.build_image(
                self.docker,
                self.host,
                name="ate-setup",
                digest=digest,
                toolbox_image=TOOLBOX,
                source=Path("/src"),
                cache=self.cache,
                layout=layout,
                version=TAG,
                limits=overrides.pop("limits", LIMITS),
                node_container="kind-control-plane",
            )

    def test_a_build_lands_in_the_backup_only_with_its_pinned_digest(self) -> None:
        result = self.build(self.image["digest"])
        self.assertEqual(result["result"], "built")
        self.assertEqual(result["digest"], self.image["digest"])
        with manager.Layout(self.root / "backup") as layout:
            self.assertEqual(
                layout.status("ate-setup", TAG, self.image["digest"]), "complete"
            )
        # The scratch layout is gone; a second run builds nothing.
        self.assertFalse((self.cache / "out/ate-setup").exists())
        self.docker.calls.clear()
        self.assertEqual(self.build(self.image["digest"])["result"], "present")
        self.assertEqual(self.docker.calls, [])

    def test_a_digest_other_than_the_pin_is_not_kept(self) -> None:
        with self.assertRaisesRegex(manager.SubstrateError, "built sha256"):
            self.build("sha256:" + "f" * 64)
        self.assertFalse(
            (self.root / "backup/blobs/sha256" / self.image["digest"][7:]).exists()
        )

    def test_a_pending_pin_builds_and_reports_the_digest(self) -> None:
        self.assertEqual(self.build(None)["digest"], self.image["digest"])

    def test_the_build_refuses_a_running_node_low_memory_and_leftovers(self) -> None:
        self.docker.containers["kind-control-plane"] = {"Running": True, "polls": 10**6}
        with self.assertRaisesRegex(
            manager.SubstrateError, "kind-control-plane is running"
        ):
            self.build(self.image["digest"])
        self.docker.containers["kind-control-plane"] = {"Running": False, "polls": 0}
        self.host.available = 3000
        with self.assertRaisesRegex(manager.SubstrateError, "below the 3584 MiB"):
            self.build(self.image["digest"])
        self.host.available = 8192
        self.docker.containers["ax-lab-build-ate-setup"] = {
            "Running": False,
            "polls": 0,
        }
        with self.assertRaisesRegex(manager.SubstrateError, "left from an earlier run"):
            self.build(self.image["digest"])
        self.assertFalse(any(call[:1] == ["run"] for call in self.docker.calls))

    def test_the_cache_must_be_private(self) -> None:
        self.cache.chmod(0o755)
        with self.assertRaisesRegex(manager.SubstrateError, "0700"):
            self.build(self.image["digest"])


class InstallTests(RegistryCase):
    def settings(self) -> Any:
        return manager.InstallSettings(
            registry_port=self.target.port,
            tag=TAG,
            installer_digest=self.installer["digest"],
            source=Path("/src"),
            kubeconfig=Path("/kubeconfig"),
            context="kind-kind",
            router="envoy",
            rollout_timeout_seconds=600,
            memory_mib=256,
            reservation_mib=128,
            cpu_millicores=1000,
            pids=256,
        )

    def setUp(self) -> None:
        super().setUp()
        self.export()
        self.restore()
        self.installer = make_image("installer")
        self.target.add("ate-setup", self.installer, tag=TAG)
        self.docker = FakeDocker()
        self.host = FakeHost(self.directory)

    def run_install(self) -> dict[str, Any]:
        return manager.install(
            self.docker,
            self.host,
            manager.Registry(self.target.address),
            self.settings(),
            self.pins,
            timeout_seconds=5400,
            min_mem_available_mib=768,
            mem_available_floor_mib=512,
        )

    def test_install_checks_every_tag_right_before_running(self) -> None:
        self.assertEqual(self.run_install()["exit_code"], 0)
        run = next(call for call in self.docker.calls if call[:1] == ["run"])
        self.assertEqual(
            run[run.index("--kind") - 1],
            f"localhost:{self.target.port}/ate-setup@{self.installer['digest']}",
        )
        self.target.add("atelet", make_image("moved"), tag=TAG)
        self.docker.calls.clear()
        with self.assertRaisesRegex(manager.SubstrateError, "does not name"):
            self.run_install()
        self.assertEqual(self.docker.calls[-1][:2], ["container", "inspect"])
        self.assertFalse(any(call[:1] == ["run"] for call in self.docker.calls))

    def test_install_needs_the_pinned_installer_image(self) -> None:
        del self.target.manifests[("ate-setup", self.installer["digest"])]
        with self.assertRaisesRegex(manager.SubstrateError, "pinned ate-setup"):
            self.run_install()

    def test_a_failed_install_reports_its_metrics(self) -> None:
        self.docker.on_run = lambda argv: self.docker.containers[
            "ax-lab-ate-setup"
        ].update(ExitCode=1)
        self.docker.stderr = "Error: rollout timed out\n"
        with self.assertRaises(manager.BoundedFailure) as raised:
            self.run_install()
        self.assertEqual(raised.exception.result["exit_code"], 1)
        self.assertIn("rollout timed out", str(raised.exception))


class CommandLineTests(unittest.TestCase):
    def test_only_mutating_commands_take_the_lock_before_anything_runs(self) -> None:
        digest = "sha256:" + "a" * 64
        for command, extra, locked in (
            ("import", ["--registry", "127.0.0.1:5001"], True),
            ("export", ["--registry", "127.0.0.1:5001"], True),
            ("forget", [], True),
            ("image-status", [], False),
        ):
            with (
                self.subTest(command=command),
                mock.patch.object(manager, "ensure_host_lock") as lock,
                mock.patch.object(manager, "dispatch", return_value={}) as run,
                redirect_stdout(io.StringIO()),
            ):
                code = manager.main(
                    [
                        command,
                        *extra,
                        "--layout",
                        "/nonexistent/layout",
                        "--tag",
                        TAG,
                        "--image",
                        f"ateapi={digest}",
                    ]
                )
                self.assertEqual(code, 0)
                run.assert_called_once()
                if locked:
                    lock.assert_called_once()
                    self.assertEqual(
                        lock.call_args.args[0], f"ax-lab-substrate-{command}"
                    )
                else:
                    lock.assert_not_called()

    def test_errors_are_one_line_and_never_tracebacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = manager.main(
                    [
                        "image-status",
                        "--layout",
                        temporary + "/missing",
                        "--tag",
                        TAG,
                        "--image",
                        "ateapi=pending",
                    ]
                )
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertTrue(stderr.getvalue().startswith("ERROR: --image ateapi needs"))


class RelockCommandTests(RelockCase):
    """The window's commands through manager.main, with only the lock patched.

    Nothing below main is replaced: seed-layout for the Substrate and AX sets
    and forget run here for the first time, so that production does not.
    """

    def run_main(self, *argv: str) -> types.SimpleNamespace:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(manager, "ensure_host_lock") as lock,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = manager.main(list(argv))
        # One JSON line when the command succeeds, and no partial result when
        # it does not.
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1 if code == 0 else 0, stderr.getvalue())
        return types.SimpleNamespace(
            code=code,
            lock=lock,
            result=json.loads(lines[0]) if lines else None,
            stderr=stderr.getvalue(),
        )

    @staticmethod
    def images(pins: dict[str, str]) -> list[str]:
        return [
            argument
            for name, digest in sorted(pins.items())
            for argument in ("--image", f"{name}={digest}")
        ]

    def seed_command(
        self, image_set: str, source: Path, tag: str, pins: dict[str, str]
    ) -> types.SimpleNamespace:
        return self.run_main(
            "seed-layout", "--image-set", image_set, "--source", str(source),
            "--layout", str(self.backup), "--tag", tag, *self.images(pins),
        )  # fmt: skip

    def forget_command(
        self, image_set: str, tag: str, pins: dict[str, str]
    ) -> types.SimpleNamespace:
        return self.run_main(
            "forget", "--image-set", image_set, "--layout", str(self.backup),
            "--tag", tag, *self.images(pins),
        )  # fmt: skip

    def status_command(
        self, image_set: str, tag: str, pins: dict[str, str]
    ) -> types.SimpleNamespace:
        return self.run_main(
            "image-status", "--image-set", image_set, "--layout", str(self.backup),
            "--tag", tag, *self.images(pins),
        )  # fmt: skip

    def assert_locked(self, run: types.SimpleNamespace, command: str) -> None:
        run.lock.assert_called_once()
        self.assertEqual(run.lock.call_args.args[0], f"ax-lab-substrate-{command}")

    def assert_every(
        self, run: types.SimpleNamespace, outcome: str, count: int
    ) -> None:
        """The check the window makes: `length == <count> and every one <outcome>`."""
        self.assertEqual(run.code, 0, run.stderr)
        self.assertEqual(len(run.result), count)
        self.assertEqual(set(run.result.values()), {outcome})

    def test_the_cli_seeds_a_build_output_layout_into_an_empty_backup(self) -> None:
        write_build_output(
            self.source, {name: self.new[name] for name in manager.IMAGE_NAMES}, TAG
        )
        pins = digests(self.new, manager.IMAGE_NAMES)
        first = self.seed_command("substrate", self.source, TAG, pins)
        self.assert_every(first, "seeded", 7)
        self.assert_locked(first, "seed-layout")
        second = self.seed_command("substrate", self.source, TAG, pins)
        self.assert_every(second, "present", 7)
        self.assert_locked(second, "seed-layout")
        # Reading the layout takes no lock.
        status = self.status_command("substrate", TAG, pins)
        status.lock.assert_not_called()
        self.assertEqual(
            status.result,
            {"backup": dict.fromkeys(sorted(pins), "complete"), "registry": None},
        )

    def test_the_cli_re_pins_the_substrate_set_by_the_exact_old_digests(self) -> None:
        self.write_old_backup()
        write_build_output(
            self.source, {name: self.new[name] for name in manager.IMAGE_NAMES}, TAG
        )
        old = digests(self.old, manager.IMAGE_NAMES)
        new = digests(self.new, manager.IMAGE_NAMES)
        forgotten = self.forget_command("substrate", TAG, old)
        self.assert_every(forgotten, "forgotten", 7)
        self.assert_locked(forgotten, "forget")
        seeded = self.seed_command("substrate", self.source, TAG, new)
        self.assert_every(seeded, "seeded", 7)
        self.assert_locked(seeded, "seed-layout")
        status = self.status_command("substrate", TAG, new)
        self.assertEqual(
            status.result["backup"], dict.fromkeys(sorted(new), "complete")
        )
        # Run again, the forget of the old digests stops: the entries name the
        # new pins now, and a forget never drops one it was not told.
        before = self.index_bytes()
        again = self.forget_command("substrate", TAG, old)
        first = min(old)
        self.assertEqual(again.code, 1)
        self.assertIn(
            f"names {first}:{TAG} with {new[first]}, not {old[first]}", again.stderr
        )
        self.assertEqual(self.index_bytes(), before)

    def test_the_cli_re_pins_the_ax_ko_images_one_layout_at_a_time(self) -> None:
        self.write_old_backup()
        old = digests(self.old, manager.AX_KO_IMAGES)
        forgotten = self.forget_command("ax", AX_TAG, old)
        self.assert_every(forgotten, "forgotten", 2)
        self.assert_locked(forgotten, "forget")
        for name in manager.AX_KO_IMAGES:
            layout = self.directory / f"ko-{name}"
            write_ko_layout(layout, self.new[name])
            (entry,) = self.index(layout)
            seeded = self.seed_command("ax", layout, AX_TAG, {name: entry["digest"]})
            self.assertEqual(seeded.result, {name: "seeded"})
            self.assert_locked(seeded, "seed-layout")
        pins = {
            **digests(self.new, manager.AX_KO_IMAGES),
            **digests(self.old, ("ax-agents", "ax-task-runner")),
        }
        status = self.status_command("ax", AX_TAG, pins)
        self.assertEqual(
            status.result["backup"], dict.fromkeys(manager.AX_IMAGE_NAMES, "complete")
        )

    def test_a_name_outside_the_chosen_set_is_refused_and_nothing_changes(
        self,
    ) -> None:
        self.write_old_backup()
        before = self.index_bytes()
        for command, image_set, name in (
            ("seed-layout", "substrate", "ax-controller"),
            ("seed-layout", "ax", "ateapi"),
            ("seed-layout", "web", "ax-server"),
            ("forget", "ax", "ateapi"),
            ("forget", "substrate", "ax-agents"),
            ("forget", "web", "ateapi"),
        ):
            with self.subTest(command=command, image_set=image_set, name=name):
                pins = {name: self.old[name]["digest"]}
                if command == "forget":
                    run = self.forget_command(image_set, TAG, pins)
                else:
                    run = self.seed_command(image_set, self.source, TAG, pins)
                self.assertEqual(run.code, 1)
                self.assertIn("is not one known image", run.stderr)
                self.assertEqual(self.index_bytes(), before)

    def test_seed_layout_without_an_image_set_is_the_web_set(self) -> None:
        # tests/test_ax_web_deploy_contract.py seeds the panel's image this way.
        root = manager.parser()
        for command, extra, expected in (
            ("seed-layout", ["--source", "/s"], "web"),
            ("forget", [], "substrate"),
            ("image-status", [], "substrate"),
            ("export", ["--registry", "localhost:5001"], "substrate"),
            ("import", ["--registry", "localhost:5001"], "substrate"),
        ):
            with self.subTest(command=command):
                parsed = root.parse_args(
                    [command, *extra, "--layout", "/x", "--tag", "t"]
                )
                self.assertEqual(parsed.image_set, expected)
        web = self.old["ax-web"]
        write_ko_layout(self.source, web)
        argv = [
            "seed-layout", "--source", str(self.source),
            "--layout", str(self.backup), "--tag", WEB_TAG,
        ]  # fmt: skip
        seeded = self.run_main(*argv, "--image", f"ax-web={web['digest']}")
        self.assertEqual(seeded.result, {"ax-web": "seeded"})
        self.assert_locked(seeded, "seed-layout")
        other = self.old["ateapi"]["digest"]
        refused = self.run_main(*argv, "--image", f"ateapi={other}")
        self.assertEqual(refused.code, 1)
        self.assertIn("is not one known image", refused.stderr)

    def test_a_forget_that_fails_partway_prints_nothing_and_is_finished_per_image(
        self,
    ) -> None:
        # Why the window forgets and seeds one image per command: a forget of
        # several stops at the first entry that names another digest, after it
        # dropped the names that sort before it, and prints no result.
        self.write_old_backup()
        write_build_output(
            self.source, {name: self.new[name] for name in manager.IMAGE_NAMES}, TAG
        )
        shutil.copytree(self.backup, self.snapshot, symlinks=True)
        names = sorted(manager.IMAGE_NAMES)
        old = digests(self.old, manager.IMAGE_NAMES)
        new = digests(self.new, manager.IMAGE_NAMES)
        # A window moved every image and its rollback began with the third.
        restored = names[2]
        self.forget(TAG, old)
        self.seed(self.source, TAG, new)
        self.forget(TAG, {restored: new[restored]})
        self.seed(self.snapshot, TAG, {restored: old[restored]})
        run = self.forget_command("substrate", TAG, new)
        self.assertEqual(run.code, 1)
        self.assertIn(
            f"names {restored}:{TAG} with {old[restored]}, not {new[restored]}",
            run.stderr,
        )
        self.assertEqual(
            {name: self.named(name, TAG) for name in names},
            {
                **dict.fromkeys(names[:2]),
                restored: old[restored],
                **{name: new[name] for name in names[3:]},
            },
        )
        # One image at a time, every outcome is known and the rerun is safe.
        outcomes = {}
        for name in names:
            forgot = self.forget_command("substrate", TAG, {name: new[name]})
            if forgot.code == 0:
                forgot_outcome = forgot.result[name]
            else:
                self.assertIn(f"names {name}:{TAG} with {old[name]}", forgot.stderr)
                forgot_outcome = "kept"
            seeded = self.seed_command(
                "substrate", self.snapshot, TAG, {name: old[name]}
            )
            outcomes[name] = (forgot_outcome, seeded.result[name])
        self.assertEqual(
            outcomes,
            {
                **dict.fromkeys(names, ("forgotten", "seeded")),
                **dict.fromkeys(names[:2], ("absent", "seeded")),
                restored: ("kept", "present"),
            },
        )
        status = self.status_command("substrate", TAG, old)
        self.assertEqual(status.result["backup"], dict.fromkeys(names, "complete"))


if __name__ == "__main__":
    unittest.main()
