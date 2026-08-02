#!/usr/bin/env python3
"""CAS state machine for fail-closed Terraform operation leases in R2."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

CLOUDFLARE_ACCOUNT_ID = "becc1c00820a0f6779e9b278ab150abf"
REPOSITORY_CONTRACT = "DockerSwarmInfrastructure/terraform-operation-lease/v1"
RECOVERY_SIGNER_IDENTITY = "apptolast-terraform-lease-recovery"
RECOVERY_SIGNATURE_NAMESPACE = "terraform-operation-lease-recovery"
RECOVERY_SIGNERS_PATH = Path("infra/terraform/lease-recovery.allowed-signers")
ROOT_KEYS = {
    "cloudflare/apptolast-dns": "cloudflare/apptolast-dns/terraform.tfstate",
    "netcup/perimeter": "netcup/perimeter/terraform.tfstate",
}
OPERATIONS = {"apply", "migration"}
PHASES = {
    "acquired",
    "prestate_verified",
    "writer_started",
    "writer_finished",
    "snapshot_verified",
    "poststate_verified",
}
NEXT_PHASE = {
    "acquired": "prestate_verified",
    "prestate_verified": "writer_started",
    "writer_started": "writer_finished",
    "writer_finished": "snapshot_verified",
    "snapshot_verified": "poststate_verified",
}
PREWRITE_PHASES = {"acquired", "prestate_verified"}
POSTWRITE_PHASES = PHASES - PREWRITE_PHASES
REMOTE_KEYS = {
    "schema",
    "repository_contract",
    "root",
    "operation_id",
    "owner_token_sha256",
    "operation",
    "backend_identity_sha256s",
    "backend_registry_sha256",
    "source_commit",
    "plan_sha256",
    "status",
    "phase",
    "generation",
    "transition_nonce",
    "previous_document_sha256",
    "acquired_at",
    "updated_at",
}
TOKEN_KEYS = REMOTE_KEYS | {
    "coordinator_bucket",
    "coordinator_key",
    "owner_token",
    "remote_etag",
    "remote_document_sha256",
    "local_state",
}
RECOVERY_KEYS = {
    "schema",
    "repository_contract",
    "action",
    "root",
    "operation_id",
    "generation",
    "held_document_sha256",
    "controller_stopped_or_revoked",
    "reason",
    "approved_at",
    "signer_identity",
}
MAX_RESPONSE_BYTES = 64 * 1024
MAX_RECOVERY_AGE = timedelta(hours=24)


class LeaseError(RuntimeError):
    """The distributed lease cannot be proven safe."""


class NoRedirectHandler(HTTPRedirectHandler):
    """Refuse every redirect so signed requests never leave the pinned origin."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


URL_OPENER = build_opener(ProxyHandler({}), NoRedirectHandler())


def canonical_json(document: Any) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_utc(value: Any, field: str) -> datetime:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value) is None
    ):
        raise LeaseError(f"{field} is not a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LeaseError(f"{field} is not a valid UTC timestamp") from exc
    return parsed


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def is_commit(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value) is not None


def sign(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def signature_key(secret: str, date: str) -> bytes:
    date_key = sign(f"AWS4{secret}".encode(), date)
    region_key = sign(date_key, "auto")
    service_key = sign(region_key, "s3")
    return sign(service_key, "aws4_request")


def credentials() -> tuple[str, str]:
    access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    if (
        not access_key
        or any(character.isspace() for character in access_key)
        or not secret_key
    ):
        raise LeaseError("explicit R2 access and secret keys are required")
    return access_key, secret_key


def backend_identity(metadata_path: Path, root: str) -> tuple[str, str, str]:
    if metadata_path.is_symlink() or not metadata_path.is_file():
        raise LeaseError("backend metadata must be a regular, non-symlink file")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LeaseError("backend metadata is not valid JSON") from exc
    backend = metadata.get("backend") if isinstance(metadata, dict) else None
    config = backend.get("config") if isinstance(backend, dict) else None
    if (
        not isinstance(backend, dict)
        or backend.get("type") != "s3"
        or not isinstance(config, dict)
    ):
        raise LeaseError("operation leases require an initialized S3 backend")
    bucket = config.get("bucket")
    key = config.get("key")
    endpoints = config.get("endpoints")
    if isinstance(endpoints, list) and len(endpoints) == 1:
        endpoints = endpoints[0]
    endpoint = endpoints.get("s3") if isinstance(endpoints, dict) else None
    expected_endpoint = f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
    if (
        not isinstance(bucket, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket) is None
        or key != ROOT_KEYS.get(root)
        or not isinstance(endpoint, str)
        or endpoint.rstrip("/") != expected_endpoint
    ):
        raise LeaseError("backend identity is outside the reviewed lease scope")
    return bucket, endpoint.rstrip("/"), f"operation-leases/{root}.json"


def request(
    *,
    method: str,
    endpoint: str,
    bucket: str,
    key: str,
    body: bytes = b"",
    condition_name: str | None = None,
    condition_value: str | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
        raise LeaseError("R2 endpoint is not an origin-only HTTPS URL")
    access_key, secret_key = credentials()
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = now.strftime("%Y%m%d")
    payload_hash = sha256_bytes(body)
    canonical_path = f"/{quote(bucket, safe='-_.~')}/{quote(key, safe='/-_.~')}"
    signed = {
        "host": parsed.netloc,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": timestamp,
    }
    if condition_name is not None:
        if condition_value is None:
            raise LeaseError("conditional request has no value")
        signed[condition_name.lower()] = condition_value
    signed_headers = ";".join(sorted(signed))
    canonical_headers = "".join(
        f"{name}:{signed[name].strip()}\n" for name in sorted(signed)
    )
    canonical_request = "\n".join(
        (method, canonical_path, "", canonical_headers, signed_headers, payload_hash)
    )
    credential_scope = f"{short_date}/auto/s3/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            timestamp,
            credential_scope,
            sha256_bytes(canonical_request.encode()),
        )
    )
    signature = hmac.new(
        signature_key(secret_key, short_date),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "Authorization": (
            "AWS4-HMAC-SHA256 "
            f"Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        **signed,
    }
    http_request = Request(
        f"{endpoint}{canonical_path}",
        data=body if method == "PUT" else None,
        headers=headers,
        method=method,
    )
    try:
        with URL_OPENER.open(http_request, timeout=20) as response:
            if response.geturl() != http_request.full_url:
                raise LeaseError("R2 lease request was redirected")
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                raise LeaseError("R2 lease response exceeds the safety limit")
            return (
                response.status,
                response_body,
                {name.lower(): value for name, value in response.headers.items()},
            )
    except HTTPError as exc:
        response_body = exc.read(MAX_RESPONSE_BYTES + 1)
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise LeaseError("R2 lease error response exceeds the safety limit")
        return (
            exc.code,
            response_body,
            {name.lower(): value for name, value in exc.headers.items()},
        )
    except URLError as exc:
        raise LeaseError("R2 lease request has an ambiguous network outcome") from exc


def validate_remote_document(
    document: Any,
    raw: bytes,
    *,
    root: str,
    bucket: str,
    lease_key: str,
) -> dict[str, Any]:
    if (
        re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket) is None
        or lease_key != f"operation-leases/{root}.json"
    ):
        raise LeaseError("lease coordinator context is outside the reviewed scope")
    if not isinstance(document, dict) or set(document) != REMOTE_KEYS:
        raise LeaseError("lease coordinator document has an invalid field set")
    if canonical_json(document) != raw:
        raise LeaseError("lease coordinator document is not canonical JSON")
    backend_identities = document["backend_identity_sha256s"]
    if (
        document["schema"] != 1
        or document["repository_contract"] != REPOSITORY_CONTRACT
        or document["root"] != root
        or not is_sha256(document["operation_id"])
        or not is_sha256(document["owner_token_sha256"])
        or document["operation"] not in OPERATIONS
        or not isinstance(backend_identities, list)
        or len(backend_identities) not in (1, 2)
        or any(not is_sha256(identity) for identity in backend_identities)
        or len(set(backend_identities)) != len(backend_identities)
        or not is_sha256(document["backend_registry_sha256"])
        or not is_commit(document["source_commit"])
        or (
            document["plan_sha256"] is not None
            and not is_sha256(document["plan_sha256"])
        )
        or document["status"] not in {"held", "quarantined", "released"}
        or document["phase"] not in PHASES
        or not isinstance(document["generation"], int)
        or isinstance(document["generation"], bool)
        or document["generation"] < 1
        or not is_sha256(document["transition_nonce"])
        or (
            document["previous_document_sha256"] is not None
            and not is_sha256(document["previous_document_sha256"])
        )
    ):
        raise LeaseError("lease coordinator document contract is invalid")
    if (document["generation"] == 1) != (document["previous_document_sha256"] is None):
        raise LeaseError("lease hash chain does not match its generation")
    parse_utc(document["acquired_at"], "acquired_at")
    acquired_at = parse_utc(document["acquired_at"], "acquired_at")
    updated_at = parse_utc(document["updated_at"], "updated_at")
    if updated_at < acquired_at:
        raise LeaseError("lease updated_at predates acquired_at")
    if document["operation"] == "apply":
        if len(backend_identities) != 1 or document["plan_sha256"] is None:
            raise LeaseError("apply lease binding is incomplete")
    elif len(backend_identities) != 2 or document["plan_sha256"] is not None:
        raise LeaseError("migration lease binding is incomplete")
    if (
        document["status"] == "quarantined"
        and document["phase"] not in POSTWRITE_PHASES
    ):
        raise LeaseError("only a post-writer lease may be quarantined")
    return document


def parse_remote(
    body: bytes,
    *,
    root: str,
    bucket: str,
    lease_key: str,
) -> dict[str, Any]:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LeaseError("lease coordinator object is invalid JSON") from exc
    return validate_remote_document(
        document,
        body,
        root=root,
        bucket=bucket,
        lease_key=lease_key,
    )


def remote_from_token(token: dict[str, Any]) -> dict[str, Any]:
    return {key: token[key] for key in REMOTE_KEYS}


def validate_token(document: Any, raw: bytes) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != TOKEN_KEYS:
        raise LeaseError("lease token has an invalid field set")
    remote = remote_from_token(document)
    validate_remote_document(
        remote,
        canonical_json(remote),
        root=document.get("root"),
        bucket=document.get("coordinator_bucket", ""),
        lease_key=document.get("coordinator_key", ""),
    )
    if (
        not isinstance(document["coordinator_bucket"], str)
        or re.fullmatch(
            r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]",
            document["coordinator_bucket"],
        )
        is None
        or document["coordinator_key"] != f"operation-leases/{document['root']}.json"
        or not is_sha256(document["owner_token"])
        or sha256_bytes(document["owner_token"].encode())
        != document["owner_token_sha256"]
        or document["local_state"] not in {"acquiring", "active"}
        or (
            document["local_state"] == "acquiring"
            and document["remote_etag"] is not None
        )
        or (
            document["local_state"] == "active"
            and (
                not isinstance(document["remote_etag"], str)
                or not document["remote_etag"]
            )
        )
        or not is_sha256(document["remote_document_sha256"])
        or document["remote_document_sha256"] != sha256_bytes(canonical_json(remote))
        or canonical_json(document) != raw
    ):
        raise LeaseError("lease token contract is invalid")
    return document


def read_token(path: Path) -> tuple[dict[str, Any], bytes]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LeaseError("lease token must be a regular, non-symlink file") from exc
    try:
        file_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_status.st_mode)
            or stat.S_IMODE(file_status.st_mode) != 0o600
        ):
            raise LeaseError("lease token must be a regular mode-0600 file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(MAX_RESPONSE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise LeaseError("lease token exceeds the safety limit")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LeaseError("lease token is invalid JSON") from exc
    return validate_token(document, raw), raw


def fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_new_token(path: Path, document: dict[str, Any]) -> None:
    raw = canonical_json(document)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    fsync_parent(path)


def replace_token(path: Path, document: dict[str, Any]) -> None:
    read_token(path)
    raw = canonical_json(document)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_parent(path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def unlink_token(path: Path) -> None:
    path.unlink()
    fsync_parent(path)


def activate_token(
    path: Path,
    token: dict[str, Any],
    etag: str,
) -> dict[str, Any]:
    if not etag:
        raise LeaseError("R2 did not return a coordinator ETag")
    active = {**token, "local_state": "active", "remote_etag": etag}
    replace_token(path, active)
    return active


def assert_current(
    metadata: Path,
    root: str,
    token_file: Path,
) -> tuple[str, str, str, dict[str, Any], dict[str, Any], str]:
    bucket, endpoint, lease_key = backend_identity(metadata, root)
    token, _ = read_token(token_file)
    expected_remote = remote_from_token(token)
    if (
        token["root"] != root
        or token["coordinator_bucket"] != bucket
        or token["coordinator_key"] != lease_key
    ):
        raise LeaseError("lease token belongs to another coordinator")
    status, body, headers = request(
        method="GET", endpoint=endpoint, bucket=bucket, key=lease_key
    )
    etag = headers.get("etag", "")
    if (
        status != 200
        or body != canonical_json(expected_remote)
        or sha256_bytes(body) != token["remote_document_sha256"]
        or not etag
    ):
        raise LeaseError("distributed Terraform operation lease is not current")
    if token["local_state"] == "acquiring":
        token = activate_token(token_file, token, etag)
    elif token["remote_etag"] != etag:
        raise LeaseError("lease ETag differs from the owner token")
    return bucket, endpoint, lease_key, token, expected_remote, etag


def assert_held(
    metadata: Path,
    root: str,
    token_file: Path,
) -> tuple[str, str, str, dict[str, Any], dict[str, Any], str]:
    result = assert_current(metadata, root, token_file)
    if result[4]["status"] != "held":
        raise LeaseError("distributed Terraform operation lease is not held")
    return result


def new_remote_document(
    args: argparse.Namespace,
    *,
    generation: int,
    previous_document_sha256: str | None,
) -> tuple[dict[str, Any], str]:
    backend_identities = list(args.backend_identity_sha256)
    if any(not is_sha256(value) for value in backend_identities):
        raise LeaseError("backend identity binding is invalid")
    if len(set(backend_identities)) != len(backend_identities):
        raise LeaseError("backend identity bindings must be distinct")
    if args.operation == "apply":
        if len(backend_identities) != 1 or not is_sha256(args.plan_sha256):
            raise LeaseError("apply requires one backend identity and a plan SHA-256")
        plan_sha256: str | None = args.plan_sha256
    else:
        if len(backend_identities) != 2 or args.plan_sha256 is not None:
            raise LeaseError("migration requires two identities and no plan SHA-256")
        plan_sha256 = None
    if not is_commit(args.source_commit) or not is_sha256(args.registry_sha256):
        raise LeaseError("source commit or backend registry binding is invalid")
    owner_token = os.urandom(32).hex()
    acquired_at = utc_now()
    remote = {
        "schema": 1,
        "repository_contract": REPOSITORY_CONTRACT,
        "root": args.root,
        "operation_id": os.urandom(32).hex(),
        "owner_token_sha256": sha256_bytes(owner_token.encode()),
        "operation": args.operation,
        "backend_identity_sha256s": backend_identities,
        "backend_registry_sha256": args.registry_sha256,
        "source_commit": args.source_commit,
        "plan_sha256": plan_sha256,
        "status": "held",
        "phase": "acquired",
        "generation": generation,
        "transition_nonce": os.urandom(32).hex(),
        "previous_document_sha256": previous_document_sha256,
        "acquired_at": acquired_at,
        "updated_at": acquired_at,
    }
    return remote, owner_token


def acquire(args: argparse.Namespace) -> None:
    if args.token_file.exists() or args.token_file.is_symlink():
        raise LeaseError("refusing to overwrite an existing lease token")
    bucket, endpoint, lease_key = backend_identity(args.metadata, args.root)
    status, existing_body, existing_headers = request(
        method="GET", endpoint=endpoint, bucket=bucket, key=lease_key
    )
    if status == 404:
        generation = 1
        previous_sha = None
        condition_name = "if-none-match"
        condition_value = "*"
    elif status == 200 and existing_headers.get("etag"):
        previous = parse_remote(
            existing_body, root=args.root, bucket=bucket, lease_key=lease_key
        )
        if previous["status"] != "released":
            raise LeaseError("distributed Terraform operation lease is already active")
        generation = previous["generation"] + 1
        previous_sha = sha256_bytes(existing_body)
        condition_name = "if-match"
        condition_value = existing_headers["etag"]
    else:
        raise LeaseError("distributed Terraform operation lease is unavailable")
    remote, owner_token = new_remote_document(
        args,
        generation=generation,
        previous_document_sha256=previous_sha,
    )
    remote_bytes = canonical_json(remote)
    token = {
        **remote,
        "coordinator_bucket": bucket,
        "coordinator_key": lease_key,
        "owner_token": owner_token,
        "remote_etag": None,
        "remote_document_sha256": sha256_bytes(remote_bytes),
        "local_state": "acquiring",
    }
    write_new_token(args.token_file, token)
    try:
        put_status, _, put_headers = request(
            method="PUT",
            endpoint=endpoint,
            bucket=bucket,
            key=lease_key,
            body=remote_bytes,
            condition_name=condition_name,
            condition_value=condition_value,
        )
    except LeaseError as put_error:
        try:
            check_status, check_body, check_headers = request(
                method="GET", endpoint=endpoint, bucket=bucket, key=lease_key
            )
        except LeaseError as check_error:
            raise LeaseError(
                "lease acquisition outcome is unknown; owner token retained"
            ) from check_error
        if (
            check_status == 200
            and check_body == remote_bytes
            and check_headers.get("etag")
        ):
            activate_token(args.token_file, token, check_headers["etag"])
            return
        raise LeaseError(
            "lease acquisition outcome is unknown; owner token retained"
        ) from put_error
    if put_status == 412:
        unlink_token(args.token_file)
        raise LeaseError("distributed Terraform operation lease lost the CAS race")
    if put_status not in (200, 204):
        raise LeaseError(
            f"lease acquisition HTTP {put_status} is ambiguous; owner token retained"
        )
    etag = put_headers.get("etag", "")
    if not etag:
        check_status, check_body, check_headers = request(
            method="GET", endpoint=endpoint, bucket=bucket, key=lease_key
        )
        if check_status != 200 or check_body != remote_bytes:
            raise LeaseError("acquired lease cannot be reconciled; token retained")
        etag = check_headers.get("etag", "")
    activate_token(args.token_file, token, etag)
    assert_held(args.metadata, args.root, args.token_file)


def cas_transition(
    args: argparse.Namespace,
    *,
    status: str,
    phase: str,
) -> dict[str, Any]:
    bucket, endpoint, lease_key, token, current, etag = assert_current(
        args.metadata, args.root, args.token_file
    )
    updated = {
        **current,
        "status": status,
        "phase": phase,
        "generation": current["generation"] + 1,
        "transition_nonce": os.urandom(32).hex(),
        "previous_document_sha256": sha256_bytes(canonical_json(current)),
        "updated_at": utc_now(),
    }
    updated_bytes = canonical_json(updated)
    try:
        put_status, _, put_headers = request(
            method="PUT",
            endpoint=endpoint,
            bucket=bucket,
            key=lease_key,
            body=updated_bytes,
            condition_name="if-match",
            condition_value=etag,
        )
    except LeaseError as put_error:
        try:
            check_status, check_body, check_headers = request(
                method="GET", endpoint=endpoint, bucket=bucket, key=lease_key
            )
        except LeaseError as check_error:
            raise LeaseError(
                "lease transition outcome is unknown; token retained"
            ) from check_error
        if (
            check_status == 200
            and check_body == updated_bytes
            and check_headers.get("etag")
        ):
            put_status = 200
            put_headers = check_headers
        else:
            raise LeaseError(
                "lease transition outcome is unknown; token retained"
            ) from put_error
    if put_status not in (200, 204):
        raise LeaseError(f"conditional lease transition failed (HTTP {put_status})")
    new_etag = put_headers.get("etag", "")
    if not new_etag:
        check_status, check_body, check_headers = request(
            method="GET", endpoint=endpoint, bucket=bucket, key=lease_key
        )
        if check_status != 200 or check_body != updated_bytes:
            raise LeaseError("lease transition cannot be reconciled")
        new_etag = check_headers.get("etag", "")
    new_token = {
        **token,
        **updated,
        "remote_etag": new_etag,
        "remote_document_sha256": sha256_bytes(updated_bytes),
        "local_state": "active",
    }
    replace_token(args.token_file, new_token)
    return updated


def transition(args: argparse.Namespace) -> None:
    _, _, _, _, current, _ = assert_held(args.metadata, args.root, args.token_file)
    if NEXT_PHASE.get(current["phase"]) != args.phase:
        raise LeaseError("requested lease phase is not the unique next transition")
    cas_transition(args, status="held", phase=args.phase)


def quarantine(args: argparse.Namespace) -> None:
    _, _, _, _, current, _ = assert_held(args.metadata, args.root, args.token_file)
    if current["phase"] not in POSTWRITE_PHASES:
        raise LeaseError("a pre-writer lease must be released, not quarantined")
    cas_transition(args, status="quarantined", phase=current["phase"])


def release(args: argparse.Namespace) -> None:
    _, _, _, _, current, _ = assert_held(args.metadata, args.root, args.token_file)
    if args.pre_write:
        if current["phase"] not in PREWRITE_PHASES:
            raise LeaseError("pre-write release is forbidden after writer_started")
    elif current["phase"] != "poststate_verified":
        raise LeaseError("normal release requires poststate_verified")
    cas_transition(args, status="released", phase=current["phase"])
    unlink_token(args.token_file)


def read_regular_file(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LeaseError(f"{label} must be a regular, non-symlink file") from exc
    try:
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            raise LeaseError(f"{label} must be a regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            value = stream.read(MAX_RESPONSE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(value) > MAX_RESPONSE_BYTES:
        raise LeaseError(f"{label} exceeds the safety limit")
    return value


def verify_recovery_record(
    args: argparse.Namespace,
    remote: dict[str, Any],
    remote_body: bytes,
) -> None:
    record_bytes = read_regular_file(args.recovery_record, "recovery record")
    try:
        record = json.loads(record_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LeaseError("recovery record is invalid JSON") from exc
    if (
        not isinstance(record, dict)
        or set(record) != RECOVERY_KEYS
        or canonical_json(record) != record_bytes
        or record["schema"] != 1
        or record["repository_contract"] != REPOSITORY_CONTRACT
        or record["action"] != "release-quarantined-controller"
        or record["root"] != args.root
        or record["operation_id"] != remote["operation_id"]
        or record["generation"] != remote["generation"]
        or record["held_document_sha256"] != sha256_bytes(remote_body)
        or record["controller_stopped_or_revoked"] is not True
        or not isinstance(record["reason"], str)
        or not record["reason"].strip()
        or len(record["reason"]) > 500
        or any(ord(character) < 0x20 for character in record["reason"])
        or record["signer_identity"] != RECOVERY_SIGNER_IDENTITY
    ):
        raise LeaseError("signed recovery record does not bind the current lease")
    approved_at = parse_utc(record["approved_at"], "approved_at")
    now = datetime.now(timezone.utc)
    if approved_at > now or now - approved_at > MAX_RECOVERY_AGE:
        raise LeaseError("signed recovery approval is not current")
    expected_confirmation = (
        f"RECOVER:{args.root}:{remote['operation_id']}:"
        f"{remote['generation']}:{sha256_bytes(remote_body)}"
    )
    if args.confirm != expected_confirmation:
        raise LeaseError(f"recovery confirmation mismatch: {expected_confirmation}")
    signature_path = Path(f"{args.recovery_record}.sig")
    read_regular_file(signature_path, "recovery signature")
    project_dir = args.project_dir.resolve(strict=True)
    committed_registry = subprocess.run(
        [
            "git",
            "-C",
            str(project_dir),
            "show",
            f"{remote['source_commit']}:{RECOVERY_SIGNERS_PATH.as_posix()}",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if committed_registry.returncode != 0 or not committed_registry.stdout:
        raise LeaseError("lease source commit has no approved recovery signer registry")
    registry_descriptor, registry_name = tempfile.mkstemp(
        prefix=".terraform-recovery-signers.",
    )
    registry_path = Path(registry_name)
    try:
        os.fchmod(registry_descriptor, 0o600)
        with os.fdopen(registry_descriptor, "wb") as stream:
            registry_descriptor = -1
            stream.write(committed_registry.stdout)
            stream.flush()
            os.fsync(stream.fileno())
        verified = subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "verify",
                "-f",
                str(registry_path),
                "-I",
                RECOVERY_SIGNER_IDENTITY,
                "-n",
                RECOVERY_SIGNATURE_NAMESPACE,
                "-s",
                str(signature_path),
            ],
            input=record_bytes,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        if registry_descriptor >= 0:
            os.close(registry_descriptor)
        registry_path.unlink(missing_ok=True)
    if verified.returncode != 0:
        raise LeaseError("recovery approval signature is invalid or unapproved")


def recover(args: argparse.Namespace) -> None:
    bucket, endpoint, lease_key = backend_identity(args.metadata, args.root)
    status, body, headers = request(
        method="GET", endpoint=endpoint, bucket=bucket, key=lease_key
    )
    if status != 200 or not headers.get("etag"):
        raise LeaseError("recovery target is absent or has no ETag")
    current = parse_remote(body, root=args.root, bucket=bucket, lease_key=lease_key)
    if current["status"] not in {"held", "quarantined"}:
        raise LeaseError("only an active or quarantined lease can be recovered")
    verify_recovery_record(args, current, body)
    released = {
        **current,
        "status": "released",
        "generation": current["generation"] + 1,
        "transition_nonce": os.urandom(32).hex(),
        "previous_document_sha256": sha256_bytes(body),
        "updated_at": utc_now(),
    }
    released_bytes = canonical_json(released)
    try:
        put_status, _, _ = request(
            method="PUT",
            endpoint=endpoint,
            bucket=bucket,
            key=lease_key,
            body=released_bytes,
            condition_name="if-match",
            condition_value=headers["etag"],
        )
    except LeaseError as put_error:
        try:
            check_status, check_body, _ = request(
                method="GET", endpoint=endpoint, bucket=bucket, key=lease_key
            )
        except LeaseError as check_error:
            raise LeaseError("recovery outcome is unknown") from check_error
        if check_status == 200 and check_body == released_bytes:
            return
        raise LeaseError("recovery outcome is unknown") from put_error
    if put_status not in (200, 204):
        raise LeaseError(f"conditional recovery failed (HTTP {put_status})")
    check_status, check_body, _ = request(
        method="GET", endpoint=endpoint, bucket=bucket, key=lease_key
    )
    if check_status != 200 or check_body != released_bytes:
        raise LeaseError("recovered lease tombstone is not current")


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    subparsers = argument_parser.add_subparsers(dest="command", required=True)
    for command in (
        "acquire",
        "assert-held",
        "transition",
        "quarantine",
        "release",
    ):
        operation = subparsers.add_parser(command)
        operation.add_argument("--metadata", type=Path, required=True)
        operation.add_argument("--root", choices=sorted(ROOT_KEYS), required=True)
        operation.add_argument("--token-file", type=Path, required=True)
        if command == "acquire":
            operation.add_argument("--source-commit", required=True)
            operation.add_argument(
                "--operation", choices=sorted(OPERATIONS), required=True
            )
            operation.add_argument(
                "--backend-identity-sha256",
                action="append",
                required=True,
            )
            operation.add_argument("--registry-sha256", required=True)
            operation.add_argument("--plan-sha256")
        elif command == "transition":
            operation.add_argument("--phase", choices=sorted(PHASES), required=True)
        elif command == "release":
            operation.add_argument("--pre-write", action="store_true")
    recovery = subparsers.add_parser("recover")
    recovery.add_argument("--metadata", type=Path, required=True)
    recovery.add_argument("--root", choices=sorted(ROOT_KEYS), required=True)
    recovery.add_argument("--recovery-record", type=Path, required=True)
    recovery.add_argument("--project-dir", type=Path, required=True)
    recovery.add_argument("--confirm", required=True)
    return argument_parser


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "acquire":
            acquire(args)
        elif args.command == "assert-held":
            assert_held(args.metadata, args.root, args.token_file)
        elif args.command == "transition":
            transition(args)
        elif args.command == "quarantine":
            quarantine(args)
        elif args.command == "release":
            release(args)
        else:
            recover(args)
    except (LeaseError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
