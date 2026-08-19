#!/usr/bin/env python3
"""Verify the one reviewed runtime tag against an approved immutable digest."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

MAX_JSON_BYTES = 1024 * 1024
EXPECTED_OS = "linux"
EXPECTED_ARCHITECTURE = "amd64"
TRACKED_REPOSITORIES = {
    "docker.io/hgarciaalberto/personal-website:latest": (
        "hgarciaalberto/personal-website"
    ),
}
DIGEST_RE = re.compile(r"sha256:[a-f0-9]{64}")
ALLOWED_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
}


class TrackedImageError(RuntimeError):
    """The inspected image does not satisfy the tracked-tag contract."""


def resolve_tracked_reference(
    reference: str,
    approved_reference: str,
    document: Any,
) -> str:
    expected_repository = TRACKED_REPOSITORIES.get(reference)
    if expected_repository is None:
        raise TrackedImageError("image reference is not approved for tracking")
    if (
        not isinstance(approved_reference, str)
        or re.fullmatch(
            re.escape(reference) + r"@sha256:[a-f0-9]{64}",
            approved_reference,
        )
        is None
    ):
        raise TrackedImageError("approved runtime reference is invalid")
    if not isinstance(document, dict):
        raise TrackedImageError("registry descriptor must be an object")
    media_type = document.get("mediaType")
    digest = document.get("digest")
    size = document.get("size")
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise TrackedImageError("registry descriptor media type is unsupported")
    if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
        raise TrackedImageError("registry descriptor digest is invalid")
    if type(size) is not int or size <= 0:
        raise TrackedImageError("registry descriptor size is invalid")
    resolved_reference = f"{reference}@{digest}"
    if resolved_reference != approved_reference:
        raise TrackedImageError(
            "registry digest differs from the reviewed runtime approval"
        )
    return resolved_reference


def verify_tracked_image(resolved_reference: str, document: Any) -> None:
    matches = [
        (reference, repository)
        for reference, repository in TRACKED_REPOSITORIES.items()
        if resolved_reference.startswith(f"{reference}@")
    ]
    if len(matches) != 1:
        raise TrackedImageError("resolved image reference is not approved")
    reference, expected_repository = matches[0]
    digest = resolved_reference.removeprefix(f"{reference}@")
    if DIGEST_RE.fullmatch(digest) is None:
        raise TrackedImageError("resolved image digest is invalid")
    if not isinstance(document, list) or len(document) != 1:
        raise TrackedImageError("Docker inspect must contain exactly one image")
    image = document[0]
    if not isinstance(image, dict):
        raise TrackedImageError("Docker inspect image must be an object")
    if (
        image.get("Os") != EXPECTED_OS
        or image.get("Architecture") != EXPECTED_ARCHITECTURE
    ):
        raise TrackedImageError("tracked image platform is not linux/amd64")
    image_id = image.get("Id")
    if not isinstance(image_id, str) or DIGEST_RE.fullmatch(image_id) is None:
        raise TrackedImageError("tracked image has no valid local content ID")

    raw_repo_digests = image.get("RepoDigests")
    if not isinstance(raw_repo_digests, list):
        raise TrackedImageError("tracked image has no repository digest list")
    expected_repo_digest = f"{expected_repository}@{digest}"
    normalized_repo_digests: set[str] = set()
    for raw_digest in raw_repo_digests:
        if not isinstance(raw_digest, str):
            raise TrackedImageError("tracked image repository digest is invalid")
        normalized_repo_digests.add(raw_digest.removeprefix("docker.io/"))
    if expected_repo_digest not in normalized_repo_digests:
        raise TrackedImageError(
            "local image does not contain the resolved repository digest"
        )


def read_json_document() -> Any:
    raw = sys.stdin.buffer.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise TrackedImageError("JSON input exceeds the size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrackedImageError("input is not valid JSON") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--reference", required=True)
    resolve.add_argument("--approved-reference", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--resolved-reference", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        document = read_json_document()
        if args.command == "resolve":
            print(
                resolve_tracked_reference(
                    args.reference,
                    args.approved_reference,
                    document,
                )
            )
        else:
            verify_tracked_image(args.resolved_reference, document)
    except TrackedImageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
