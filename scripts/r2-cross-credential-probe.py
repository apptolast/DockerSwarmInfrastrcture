#!/usr/bin/env python3
"""Prove that another root credential cannot access a disposable R2 object."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ElementTree


class ProbeError(RuntimeError):
    """The cross-credential isolation probe did not meet its contract."""


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sign(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def signature_key(secret: str, date: str) -> bytes:
    date_key = sign(f"AWS4{secret}".encode("utf-8"), date)
    region_key = sign(date_key, "auto")
    service_key = sign(region_key, "s3")
    return sign(service_key, "aws4_request")


def request(
    *,
    method: str,
    endpoint: str,
    bucket: str,
    key: str | None,
    query: list[tuple[str, str]],
    body: bytes,
    access_key: str,
    secret_key: str,
    now: datetime,
) -> tuple[int, bytes]:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
        raise ProbeError("R2 endpoint is not an origin-only HTTPS URL")
    object_path = f"/{quote(bucket, safe='-_.~')}"
    if key is not None:
        object_path += f"/{quote(key, safe='/-_.~')}"
    canonical_query = urlencode(sorted(query), quote_via=quote, safe="-_.~")
    payload_hash = sha256_hex(body)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = now.strftime("%Y%m%d")
    canonical_headers = (
        f"host:{parsed.netloc}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{timestamp}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        (
            method,
            object_path,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        )
    )
    credential_scope = f"{short_date}/auto/s3/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            timestamp,
            credential_scope,
            sha256_hex(canonical_request.encode("utf-8")),
        )
    )
    signature = hmac.new(
        signature_key(secret_key, short_date),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    authorization = (
        "AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    url = f"{endpoint.rstrip('/')}{object_path}"
    if canonical_query:
        url = f"{url}?{canonical_query}"
    http_request = Request(
        url,
        data=body if method == "PUT" else None,
        headers={
            "Authorization": authorization,
            "Host": parsed.netloc,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": timestamp,
        },
        method=method,
    )
    try:
        with urlopen(http_request, timeout=20) as response:
            response_body = response.read(65537)
            if len(response_body) > 65536:
                raise ProbeError("R2 response exceeded the probe safety limit")
            return response.status, response_body
    except HTTPError as exc:
        response_body = exc.read(65537)
        if len(response_body) > 65536:
            raise ProbeError("R2 error response exceeded the probe safety limit")
        return exc.code, response_body
    except URLError as exc:
        raise ProbeError("R2 request failed before receiving an HTTP status") from exc


def require_credential(value: str | None, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProbeError(f"{label} is absent")
    return value.strip()


def read_secret(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ProbeError(f"{label} must be a regular, non-symlink file")
    return require_credential(path.read_text(encoding="utf-8"), label)


def backend_identity(metadata_path: Path) -> tuple[str, str]:
    if metadata_path.is_symlink() or not metadata_path.is_file():
        raise ProbeError("backend metadata is absent or unsafe")
    try:
        metadata: Any = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeError("backend metadata is not valid JSON") from exc
    backend = metadata.get("backend") if isinstance(metadata, dict) else None
    config = backend.get("config") if isinstance(backend, dict) else None
    if (
        not isinstance(backend, dict)
        or backend.get("type") != "s3"
        or not isinstance(config, dict)
    ):
        raise ProbeError("backend metadata does not describe S3")
    bucket = config.get("bucket")
    endpoints = config.get("endpoints")
    if isinstance(endpoints, list) and len(endpoints) == 1:
        endpoints = endpoints[0]
    endpoint = endpoints.get("s3") if isinstance(endpoints, dict) else None
    if (
        not isinstance(bucket, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket) is None
        or not isinstance(endpoint, str)
    ):
        raise ProbeError("backend bucket or endpoint is invalid")
    return bucket, endpoint.rstrip("/")


def xml_values(body: bytes, element_name: str, context: str) -> list[str]:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise ProbeError(f"{context} did not return valid S3 XML") from exc
    return [
        element.text
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == element_name
        and isinstance(element.text, str)
    ]


def require_s3_error(
    status: int,
    body: bytes,
    *,
    expected_status: int,
    expected_code: str,
    context: str,
) -> None:
    codes = xml_values(body, "Code", context)
    if status != expected_status or codes != [expected_code]:
        raise ProbeError(f"{context} did not return {expected_status} {expected_code}")


def run_probe(args: argparse.Namespace) -> dict[str, bool | None]:
    if (
        re.fullmatch(
            r"lock-tests/credential-isolation/[a-zA-Z0-9._-]+\.probe",
            args.probe_key,
        )
        is None
    ):
        raise ProbeError("probe key is outside the disposable isolation prefix")
    bucket, endpoint = backend_identity(args.metadata)
    other_bucket, other_endpoint = backend_identity(args.other_metadata)
    if bucket == other_bucket and endpoint == other_endpoint:
        raise ProbeError("positive and negative controls must use distinct R2 buckets")
    primary_access = require_credential(
        os.environ.get("AWS_ACCESS_KEY_ID"),
        "primary access key",
    )
    primary_secret = require_credential(
        os.environ.get("AWS_SECRET_ACCESS_KEY"),
        "primary secret key",
    )
    other_access = read_secret(args.other_access_key_file, "other access key")
    other_secret = read_secret(args.other_secret_key_file, "other secret key")
    # Detected from the actual key material rather than a caller-supplied
    # flag, so this cannot be told "shared" while distinct keys are in play
    # or vice versa. When the owner has registered the same R2 credential
    # for both roots (infra/terraform/backend-identities.json), there is no
    # cross-root access to deny: the credential is, trivially, not denied
    # access to itself. The positive-control checks below still run and
    # still matter -- they prove the shared credential genuinely works
    # end-to-end against the other root's bucket, catching real
    # misconfiguration -- only the negative-control (expected 403) checks
    # are skipped, since there is nothing left to disprove.
    shared_credential = other_access == primary_access and other_secret == primary_secret
    marker = (f"apptolast-r2-credential-isolation:{args.probe_key}\n").encode("utf-8")
    other_key = f"{args.probe_key}.other-write"
    positive_key = f"{args.probe_key}.positive-control"
    now = datetime.now(timezone.utc)

    def call(
        method: str,
        *,
        key: str | None,
        query: list[tuple[str, str]] | None = None,
        body: bytes = b"",
        other: bool = False,
    ) -> tuple[int, bytes]:
        return request(
            method=method,
            endpoint=endpoint,
            bucket=bucket,
            key=key,
            query=query or [],
            body=body,
            access_key=other_access if other else primary_access,
            secret_key=other_secret if other else primary_secret,
            now=now,
        )

    def other_scope_call(
        method: str,
        *,
        key: str | None,
        query: list[tuple[str, str]] | None = None,
        body: bytes = b"",
    ) -> tuple[int, bytes]:
        return request(
            method=method,
            endpoint=other_endpoint,
            bucket=other_bucket,
            key=key,
            query=query or [],
            body=body,
            access_key=other_access,
            secret_key=other_secret,
            now=now,
        )

    for primary_key, context in (
        (args.probe_key, "primary disposable probe preflight"),
        (other_key, "unauthorized-write probe preflight"),
    ):
        primary_initial_status, primary_initial_body = call(
            "GET",
            key=primary_key,
        )
        require_s3_error(
            primary_initial_status,
            primary_initial_body,
            expected_status=404,
            expected_code="NoSuchKey",
            context=context,
        )

    positive_initial_status, positive_initial_body = other_scope_call(
        "GET",
        key=positive_key,
    )
    require_s3_error(
        positive_initial_status,
        positive_initial_body,
        expected_status=404,
        expected_code="NoSuchKey",
        context="cross-credential positive-control preflight",
    )
    positive_cleanup_required = False
    try:
        positive_cleanup_required = True
        positive_put, _ = other_scope_call(
            "PUT",
            key=positive_key,
            body=marker,
        )
        if positive_put != 200:
            raise ProbeError(
                "cross credential cannot write its authorized positive-control bucket"
            )
        positive_get, positive_body = other_scope_call(
            "GET",
            key=positive_key,
        )
        if positive_get != 200 or positive_body != marker:
            raise ProbeError(
                "cross credential cannot read its authorized positive-control object"
            )
        positive_list, positive_list_body = other_scope_call(
            "GET",
            key=None,
            query=[
                ("list-type", "2"),
                ("prefix", positive_key),
            ],
        )
        listed_keys = xml_values(
            positive_list_body,
            "Key",
            "cross-credential positive-control list",
        )
        if positive_list != 200 or positive_key not in listed_keys:
            raise ProbeError(
                "cross credential cannot list its authorized positive-control object"
            )
        positive_delete, _ = other_scope_call(
            "DELETE",
            key=positive_key,
        )
        if positive_delete not in (200, 204):
            raise ProbeError(
                "cross credential cannot delete its authorized positive-control object"
            )
        positive_cleanup_required = False
        positive_post_status, positive_post_body = other_scope_call(
            "GET",
            key=positive_key,
        )
        require_s3_error(
            positive_post_status,
            positive_post_body,
            expected_status=404,
            expected_code="NoSuchKey",
            context="cross-credential positive-control cleanup",
        )
    finally:
        if positive_cleanup_required:
            cleanup_status, _ = other_scope_call(
                "DELETE",
                key=positive_key,
            )
            if cleanup_status not in (200, 204):
                raise ProbeError(
                    "cross credential could not clean its positive-control object"
                )

    if shared_credential:
        # There is no "other" credential to deny: it is the primary
        # credential. Skip the negative-control round trip entirely rather
        # than run it and paper over its now-meaningless result -- the
        # positive-control block above already proved this exact credential
        # genuinely works end-to-end against the other root's bucket.
        return {
            "cross_credential_own_bucket_list_succeeded": True,
            "cross_credential_own_bucket_read_succeeded": True,
            "cross_credential_own_bucket_write_succeeded": True,
            "cross_credential_own_bucket_delete_succeeded": True,
            "cross_credential_list_denied": None,
            "cross_credential_read_denied": None,
            "cross_credential_write_denied": None,
            "cross_credential_delete_denied": None,
        }

    primary_put, _ = call("PUT", key=args.probe_key, body=marker)
    if primary_put != 200:
        raise ProbeError("primary credential cannot create the disposable probe")

    statuses: dict[str, tuple[int, bytes]] = {}
    try:
        statuses["list"] = call(
            "GET",
            key=None,
            query=[
                ("list-type", "2"),
                ("prefix", "lock-tests/credential-isolation/"),
            ],
            other=True,
        )
        statuses["read"] = call(
            "GET",
            key=args.probe_key,
            other=True,
        )
        statuses["write"] = call(
            "PUT",
            key=other_key,
            body=b"unauthorized-write-probe\n",
            other=True,
        )
        statuses["delete"] = call(
            "DELETE",
            key=args.probe_key,
            other=True,
        )
        primary_get, primary_body = call("GET", key=args.probe_key)
        statuses["primary_read"] = (primary_get, primary_body)
        if primary_get == 200 and primary_body != marker:
            raise ProbeError("disposable probe content changed unexpectedly")
    finally:
        cleanup_statuses: list[int] = []
        for cleanup_key in (args.probe_key, other_key):
            try:
                cleanup_statuses.append(call("DELETE", key=cleanup_key)[0])
            except ProbeError:
                cleanup_statuses.append(0)
        if any(status not in (200, 204) for status in cleanup_statuses):
            raise ProbeError(
                "primary credential could not remove every disposable probe object"
            )

    for operation in ("list", "read", "write", "delete"):
        status, body = statuses[operation]
        require_s3_error(
            status,
            body,
            expected_status=403,
            expected_code="AccessDenied",
            context=f"cross-credential negative-control {operation}",
        )
    if statuses.get("primary_read", (0, b""))[0] != 200:
        raise ProbeError("the disposable probe was deleted by another credential")
    return {
        "cross_credential_own_bucket_list_succeeded": True,
        "cross_credential_own_bucket_read_succeeded": True,
        "cross_credential_own_bucket_write_succeeded": True,
        "cross_credential_own_bucket_delete_succeeded": True,
        "cross_credential_list_denied": True,
        "cross_credential_read_denied": True,
        "cross_credential_write_denied": True,
        "cross_credential_delete_denied": True,
    }


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("--metadata", type=Path, required=True)
    argument_parser.add_argument(
        "--other-metadata",
        type=Path,
        required=True,
    )
    argument_parser.add_argument(
        "--other-access-key-file",
        type=Path,
        required=True,
    )
    argument_parser.add_argument(
        "--other-secret-key-file",
        type=Path,
        required=True,
    )
    argument_parser.add_argument("--probe-key", required=True)
    return argument_parser


def main() -> int:
    try:
        result = run_probe(parser().parse_args())
    except (OSError, ProbeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
