"""Unit tests for the direct R2 credential-isolation probe."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE_PATH = PROJECT_ROOT / "scripts/r2-cross-credential-probe.py"
spec = importlib.util.spec_from_file_location("r2_probe", PROBE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot import R2 credential probe")
r2_probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r2_probe)


def error_xml(code: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Error><Code>{code}</Code><Message>test</Message></Error>"
    ).encode("utf-8")


class R2CredentialProbeTests(unittest.TestCase):
    def arguments(self) -> SimpleNamespace:
        return SimpleNamespace(
            metadata=Path("/unused/primary.json"),
            other_metadata=Path("/unused/other.json"),
            other_access_key_file=Path("/unused/other-access"),
            other_secret_key_file=Path("/unused/other-secret"),
            probe_key="lock-tests/credential-isolation/unit.probe",
        )

    def successful_transport(self, denial_code: str = "AccessDenied"):
        objects: dict[tuple[str, str], bytes] = {}

        def transport(**request):
            bucket = request["bucket"]
            key = request["key"]
            method = request["method"]
            access_key = request["access_key"]
            body = request["body"]
            if bucket == "other-state":
                if access_key != "other-access":
                    return 401, error_xml("Unauthorized")
                object_key = (bucket, key)
                if method == "GET" and key is not None:
                    if object_key not in objects:
                        return 404, error_xml("NoSuchKey")
                    return 200, objects[object_key]
                if method == "GET" and key is None:
                    keys = "".join(
                        f"<Contents><Key>{stored_key}</Key></Contents>"
                        for stored_bucket, stored_key in objects
                        if stored_bucket == bucket
                    )
                    return 200, f"<ListBucketResult>{keys}</ListBucketResult>".encode()
                if method == "PUT":
                    objects[object_key] = body
                    return 200, b""
                if method == "DELETE":
                    objects.pop(object_key, None)
                    return 204, b""
            if bucket == "primary-state":
                if access_key == "other-access":
                    return 403, error_xml(denial_code)
                if access_key != "primary-access":
                    return 401, error_xml("Unauthorized")
                object_key = (bucket, key)
                if method == "PUT":
                    objects[object_key] = body
                    return 200, b""
                if method == "GET":
                    if object_key not in objects:
                        return 404, error_xml("NoSuchKey")
                    return 200, objects[object_key]
                if method == "DELETE":
                    objects.pop(object_key, None)
                    return 204, b""
            raise AssertionError(f"unexpected request: {request}")

        return transport

    def run_with_transport(self, transport):
        with (
            mock.patch.object(
                r2_probe,
                "backend_identity",
                side_effect=(
                    ("primary-state", "https://account.r2.example"),
                    ("other-state", "https://account.r2.example"),
                ),
            ),
            mock.patch.object(
                r2_probe,
                "read_secret",
                side_effect=("other-access", "other-secret"),
            ),
            mock.patch.object(r2_probe, "request", side_effect=transport),
            mock.patch.dict(
                os.environ,
                {
                    "AWS_ACCESS_KEY_ID": "primary-access",
                    "AWS_SECRET_ACCESS_KEY": "primary-secret",
                },
                clear=True,
            ),
        ):
            return r2_probe.run_probe(self.arguments())

    def test_positive_control_and_exact_access_denials_are_required(self) -> None:
        result = self.run_with_transport(self.successful_transport())
        self.assertEqual(len(result), 8)
        self.assertTrue(all(result.values()))

    def test_invalid_cross_credential_cannot_pass_as_isolated(self) -> None:
        def invalid_transport(**request):
            if (
                request["bucket"] == "primary-state"
                and request["access_key"] == "primary-access"
                and request["method"] == "GET"
            ):
                return 404, error_xml("NoSuchKey")
            if request["bucket"] == "other-state":
                return 401, error_xml("Unauthorized")
            raise AssertionError("negative controls must not run")

        with self.assertRaises(r2_probe.ProbeError):
            self.run_with_transport(invalid_transport)

    def test_shared_credential_skips_negative_controls_only(self) -> None:
        objects: dict[tuple[str, str], bytes] = {}

        def shared_transport(**request):
            bucket = request["bucket"]
            key = request["key"]
            method = request["method"]
            access_key = request["access_key"]
            body = request["body"]
            if access_key != "primary-access":
                raise AssertionError(f"unexpected credential in request: {request}")
            if bucket == "primary-state":
                # Legitimate primary-credential-against-primary-bucket
                # preflight/cleanup traffic, unrelated to the (skipped)
                # negative-control round trip -- always a plain 404 here,
                # since this fixture never populates the primary bucket.
                if method == "GET":
                    return 404, error_xml("NoSuchKey")
                raise AssertionError(f"unexpected primary-bucket request: {request}")
            if bucket != "other-state":
                raise AssertionError(f"unexpected bucket in request: {request}")
            object_key = (bucket, key)
            if method == "GET" and key is not None:
                if object_key not in objects:
                    return 404, error_xml("NoSuchKey")
                return 200, objects[object_key]
            if method == "GET" and key is None:
                keys = "".join(
                    f"<Contents><Key>{stored_key}</Key></Contents>"
                    for stored_bucket, stored_key in objects
                    if stored_bucket == bucket
                )
                return 200, f"<ListBucketResult>{keys}</ListBucketResult>".encode()
            if method == "PUT":
                objects[object_key] = body
                return 200, b""
            if method == "DELETE":
                objects.pop(object_key, None)
                return 204, b""
            raise AssertionError(f"unexpected request: {request}")

        with (
            mock.patch.object(
                r2_probe,
                "backend_identity",
                side_effect=(
                    ("primary-state", "https://account.r2.example"),
                    ("other-state", "https://account.r2.example"),
                ),
            ),
            mock.patch.object(
                r2_probe,
                "read_secret",
                side_effect=("primary-access", "primary-secret"),
            ),
            mock.patch.object(r2_probe, "request", side_effect=shared_transport),
            mock.patch.dict(
                os.environ,
                {
                    "AWS_ACCESS_KEY_ID": "primary-access",
                    "AWS_SECRET_ACCESS_KEY": "primary-secret",
                },
                clear=True,
            ),
        ):
            result = r2_probe.run_probe(self.arguments())
        self.assertEqual(
            result,
            {
                "cross_credential_own_bucket_list_succeeded": True,
                "cross_credential_own_bucket_read_succeeded": True,
                "cross_credential_own_bucket_write_succeeded": True,
                "cross_credential_own_bucket_delete_succeeded": True,
                "cross_credential_list_denied": None,
                "cross_credential_read_denied": None,
                "cross_credential_write_denied": None,
                "cross_credential_delete_denied": None,
            },
        )
        # No leftover objects: the positive-control round trip cleaned up
        # after itself, and no primary-bucket probe object was ever created.
        self.assertEqual(objects, {})

    def test_preexisting_disposable_object_is_never_overwritten(self) -> None:
        transport = self.successful_transport()
        primary_requests = 0

        def collision_transport(**request):
            nonlocal primary_requests
            if request["bucket"] == "primary-state":
                primary_requests += 1
                if primary_requests == 1:
                    return 200, b"preexisting-object"
            return transport(**request)

        with self.assertRaises(r2_probe.ProbeError):
            self.run_with_transport(collision_transport)
        self.assertEqual(primary_requests, 1)

    def test_signature_failure_is_not_accepted_as_permission_denial(self) -> None:
        with self.assertRaises(r2_probe.ProbeError):
            self.run_with_transport(self.successful_transport("SignatureDoesNotMatch"))


if __name__ == "__main__":
    unittest.main()
