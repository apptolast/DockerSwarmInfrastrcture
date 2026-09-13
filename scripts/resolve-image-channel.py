#!/usr/bin/env python3
"""Resolve a reviewed image channel to its current immutable digest.

The set of resolvable channels comes only from `config/image-channels.yml`
(entries in channel mode, `repo:tag` without a digest). The registry digest
is an observation of the channel head, not an approval: the reviewed object
is the channel itself (owner decision recorded in CHANGELOG.md). What stays
fail-closed is the shape of the answer: a supported descriptor, a sha256
digest, a linux/amd64 platform and, after the pull, a local image that
exposes exactly that repository digest.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

MAX_JSON_BYTES = 1024 * 1024
EXPECTED_OS = "linux"
EXPECTED_ARCHITECTURE = "amd64"
DIGEST_RE = re.compile(r"sha256:[a-f0-9]{64}")
INDEX_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}
ALLOWED_MEDIA_TYPES = INDEX_MEDIA_TYPES | {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}


class ChannelImageError(RuntimeError):
    """The inspected image does not satisfy the image channel contract."""


def load_channel_module() -> Any:
    path = Path(__file__).with_name("validate-image-channels.py")
    spec = importlib.util.spec_from_file_location("validate_image_channels", path)
    if spec is None or spec.loader is None:
        raise ChannelImageError("cannot load the image channel validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_channel_references(root: Path) -> dict[str, str]:
    """Map every channel-mode reference to its familiar repository name."""
    module = load_channel_module()
    try:
        channel_map = module.load_channel_map(root)
    except module.ChannelError as exc:
        raise ChannelImageError(str(exc)) from exc
    references: dict[str, str] = {}
    for entries in channel_map["services"].values():
        for entry in entries.values():
            if entry["mode"] == "channel":
                references[entry["reference"]] = entry["repository_familiar"]
    return references


def resolve_channel_reference(
    reference: str,
    channel_references: dict[str, str],
    document: Any,
) -> str:
    if reference not in channel_references:
        raise ChannelImageError("image reference is not a reviewed channel")
    if not isinstance(document, dict):
        raise ChannelImageError("registry descriptor must be an object")
    media_type = document.get("mediaType")
    if (
        "mediaType" not in document
        and type(document.get("schemaVersion")) is int
        and document.get("schemaVersion") == 2
        and isinstance(document.get("manifests"), list)
    ):
        # The OCI image-spec makes mediaType optional in an image index, and
        # some publishers omit it (passbolt/passbolt, 2026-09-13). Only an
        # explicit schema 2 manifest list is read as an index; any other
        # document without mediaType stays unsupported.
        media_type = "application/vnd.oci.image.index.v1+json"
    digest = document.get("digest")
    size = document.get("size")
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise ChannelImageError("registry descriptor media type is unsupported")
    if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
        raise ChannelImageError("registry descriptor digest is invalid")
    if type(size) is not int or size <= 0:
        raise ChannelImageError("registry descriptor size is invalid")
    manifests = document.get("manifests")
    if manifests is not None:
        if media_type not in INDEX_MEDIA_TYPES or not isinstance(manifests, list):
            raise ChannelImageError("registry descriptor manifest list is invalid")
        platforms = [
            item.get("platform")
            for item in manifests
            if isinstance(item, dict) and isinstance(item.get("platform"), dict)
        ]
        if not any(
            platform.get("os") == EXPECTED_OS
            and platform.get("architecture") == EXPECTED_ARCHITECTURE
            for platform in platforms
        ):
            raise ChannelImageError("channel head has no linux/amd64 manifest")
    return f"{reference}@{digest}"


def verify_channel_image(
    resolved_reference: str,
    channel_references: dict[str, str],
    document: Any,
) -> None:
    matches = [
        (reference, repository)
        for reference, repository in channel_references.items()
        if resolved_reference.startswith(f"{reference}@")
    ]
    if len(matches) != 1:
        raise ChannelImageError("resolved image reference is not a reviewed channel")
    reference, expected_repository = matches[0]
    digest = resolved_reference.removeprefix(f"{reference}@")
    if DIGEST_RE.fullmatch(digest) is None:
        raise ChannelImageError("resolved image digest is invalid")
    if not isinstance(document, list) or len(document) != 1:
        raise ChannelImageError("Docker inspect must contain exactly one image")
    image = document[0]
    if not isinstance(image, dict):
        raise ChannelImageError("Docker inspect image must be an object")
    if (
        image.get("Os") != EXPECTED_OS
        or image.get("Architecture") != EXPECTED_ARCHITECTURE
    ):
        raise ChannelImageError("channel image platform is not linux/amd64")
    image_id = image.get("Id")
    if not isinstance(image_id, str) or DIGEST_RE.fullmatch(image_id) is None:
        raise ChannelImageError("channel image has no valid local content ID")

    raw_repo_digests = image.get("RepoDigests")
    if not isinstance(raw_repo_digests, list):
        raise ChannelImageError("channel image has no repository digest list")
    expected_repo_digest = f"{expected_repository}@{digest}"
    normalized_repo_digests: set[str] = set()
    for raw_digest in raw_repo_digests:
        if not isinstance(raw_digest, str):
            raise ChannelImageError("channel image repository digest is invalid")
        normalized_repo_digests.add(
            raw_digest.removeprefix("docker.io/library/").removeprefix("docker.io/")
        )
    if expected_repo_digest not in normalized_repo_digests:
        raise ChannelImageError(
            "local image does not contain the resolved repository digest"
        )


def read_json_document() -> Any:
    raw = sys.stdin.buffer.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ChannelImageError("JSON input exceeds the size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChannelImageError("input is not valid JSON") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--reference", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--resolved-reference", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        document = read_json_document()
        channel_references = load_channel_references(args.root)
        if args.command == "resolve":
            print(
                resolve_channel_reference(
                    args.reference,
                    channel_references,
                    document,
                )
            )
        else:
            verify_channel_image(
                args.resolved_reference,
                channel_references,
                document,
            )
    except ChannelImageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
