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


if __name__ == "__main__":
    unittest.main()
