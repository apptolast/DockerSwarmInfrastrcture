"""Offline tests for the fail-closed R2 Terraform operation lease."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEASE_PATH = PROJECT_ROOT / "scripts/r2-operation-lease.py"
spec = importlib.util.spec_from_file_location("r2_operation_lease", LEASE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot import R2 operation lease")
r2_lease = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r2_lease)


class CASTransport:
    """Strongly consistent in-memory object with S3 conditional PUTs."""

    def __init__(self) -> None:
        self.body: bytes | None = None
        self.etag: str | None = None
        self.version = 0
        self.ambiguous_puts = 0
        self.requests: list[dict[str, object]] = []

    def __call__(self, **request):
        self.requests.append(request)
        method = request["method"]
        if method == "GET":
            if self.body is None:
                return 404, b"", {}
            return 200, self.body, {"etag": self.etag}
        if method != "PUT":
            raise AssertionError(f"unexpected method: {method}")
        condition = request.get("condition_name")
        condition_value = request.get("condition_value")
        if condition == "if-none-match":
            if condition_value != "*":
                raise AssertionError("invalid If-None-Match value")
            if self.body is not None:
                return 412, b"", {}
        elif condition == "if-match":
            if self.body is None or condition_value != self.etag:
                return 412, b"", {}
        else:
            raise AssertionError("every coordinator PUT must be conditional")
        self.version += 1
        self.body = request["body"]
        self.etag = f'"lease-{self.version}"'
        if self.ambiguous_puts:
            self.ambiguous_puts -= 1
            raise r2_lease.LeaseError("simulated response loss")
        return 200, b"", {"etag": self.etag}


class R2OperationLeaseTests(unittest.TestCase):
    root = "cloudflare/apptolast-dns"
    backend_identity = "1" * 64
    destination_identity = "2" * 64
    registry_sha = "3" * 64
    plan_sha = "4" * 64

    def metadata(self, directory: Path) -> Path:
        path = directory / "terraform.tfstate"
        path.write_text(
            json.dumps(
                {
                    "backend": {
                        "type": "s3",
                        "config": {
                            "bucket": "apptolast-dns-state",
                            "key": ("cloudflare/apptolast-dns/terraform.tfstate"),
                            "endpoints": {
                                "s3": (
                                    "https://"
                                    f"{r2_lease.CLOUDFLARE_ACCOUNT_ID}"
                                    ".r2.cloudflarestorage.com"
                                )
                            },
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        return path

    def args(
        self,
        metadata: Path,
        token_file: Path,
        *,
        operation: str = "apply",
    ) -> SimpleNamespace:
        identities = [self.backend_identity]
        plan_sha256: str | None = self.plan_sha
        if operation == "migration":
            identities.append(self.destination_identity)
            plan_sha256 = None
        return SimpleNamespace(
            metadata=metadata,
            root=self.root,
            token_file=token_file,
            source_commit="a" * 40,
            operation=operation,
            backend_identity_sha256=identities,
            registry_sha256=self.registry_sha,
            plan_sha256=plan_sha256,
            phase=None,
            pre_write=False,
        )

    def transition(
        self,
        args: SimpleNamespace,
        phase: str,
    ) -> None:
        args.phase = phase
        r2_lease.transition(args)

    def test_contention_full_state_machine_and_persistent_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata = self.metadata(directory)
            first_token = directory / "first.json"
            second_token = directory / "second.json"
            transport = CASTransport()
            first = self.args(metadata, first_token)
            second = self.args(metadata, second_token)
            with mock.patch.object(r2_lease, "request", side_effect=transport):
                r2_lease.acquire(first)
                self.assertEqual(first_token.stat().st_mode & 0o777, 0o600)
                r2_lease.assert_held(metadata, self.root, first_token)
                with self.assertRaises(r2_lease.LeaseError):
                    r2_lease.acquire(second)
                self.assertFalse(second_token.exists())
                with self.assertRaises(r2_lease.LeaseError):
                    r2_lease.release(first)
                for phase in (
                    "prestate_verified",
                    "writer_started",
                    "writer_finished",
                    "snapshot_verified",
                    "poststate_verified",
                ):
                    self.transition(first, phase)
                r2_lease.release(first)
            self.assertFalse(first_token.exists())
            self.assertIsNotNone(transport.body)
            released = json.loads(transport.body)
            self.assertEqual(released["status"], "released")
            self.assertEqual(released["phase"], "poststate_verified")
            self.assertGreater(released["generation"], 1)
            self.assertIsNotNone(released["previous_document_sha256"])

    def test_aba_and_old_owner_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata = self.metadata(directory)
            current_token = directory / "current.json"
            stale_token = directory / "stale.json"
            replacement_token = directory / "replacement.json"
            transport = CASTransport()
            current = self.args(metadata, current_token)
            with mock.patch.object(r2_lease, "request", side_effect=transport):
                r2_lease.acquire(current)
                stale_token.write_bytes(current_token.read_bytes())
                stale_token.chmod(0o600)
                self.transition(current, "prestate_verified")
                with self.assertRaises(r2_lease.LeaseError):
                    r2_lease.assert_held(metadata, self.root, stale_token)
                current.pre_write = True
                r2_lease.release(current)
                replacement = self.args(metadata, replacement_token)
                r2_lease.acquire(replacement)
                with self.assertRaises(r2_lease.LeaseError):
                    r2_lease.assert_held(metadata, self.root, stale_token)
            replacement_document = json.loads(replacement_token.read_bytes())
            stale_document = json.loads(stale_token.read_bytes())
            self.assertGreater(
                replacement_document["generation"],
                stale_document["generation"],
            )
            self.assertNotEqual(
                replacement_document["operation_id"],
                stale_document["operation_id"],
            )

    def test_ambiguous_acquire_and_transition_reconcile_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata = self.metadata(directory)
            token_file = directory / "lease.json"
            transport = CASTransport()
            transport.ambiguous_puts = 1
            args = self.args(metadata, token_file)
            with mock.patch.object(r2_lease, "request", side_effect=transport):
                r2_lease.acquire(args)
                transport.ambiguous_puts = 1
                self.transition(args, "prestate_verified")
                r2_lease.assert_held(metadata, self.root, token_file)
            put_requests = [
                request for request in transport.requests if request["method"] == "PUT"
            ]
            self.assertEqual(len(put_requests), 2)
            self.assertEqual(
                [request["condition_name"] for request in put_requests],
                ["if-none-match", "if-match"],
            )

    def test_unknown_acquire_outcome_retains_owner_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata = self.metadata(directory)
            token_file = directory / "lease.json"
            calls = 0

            def unavailable(**request):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return 404, b"", {}
                raise r2_lease.LeaseError("network unavailable")

            args = self.args(metadata, token_file)
            with mock.patch.object(r2_lease, "request", side_effect=unavailable):
                with self.assertRaisesRegex(
                    r2_lease.LeaseError,
                    "owner token retained",
                ):
                    r2_lease.acquire(args)
            self.assertTrue(token_file.exists())
            self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)

    def test_conditional_header_is_covered_by_sigv4(self) -> None:
        captured = {}
        endpoint = (
            f"https://{r2_lease.CLOUDFLARE_ACCOUNT_ID}" ".r2.cloudflarestorage.com"
        )

        class Response:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def geturl(self):
                return captured["request"].full_url

            def read(self, _limit):
                return b""

        def open_request(request, timeout):
            self.assertEqual(timeout, 20)
            captured["request"] = request
            return Response()

        with (
            mock.patch.object(
                r2_lease.URL_OPENER,
                "open",
                side_effect=open_request,
            ),
            mock.patch.dict(
                os.environ,
                {
                    "AWS_ACCESS_KEY_ID": "explicit-access",
                    "AWS_SECRET_ACCESS_KEY": "explicit-secret",
                },
                clear=True,
            ),
        ):
            r2_lease.request(
                method="PUT",
                endpoint=endpoint,
                bucket="apptolast-dns-state",
                key="operation-leases/cloudflare/apptolast-dns.json",
                body=b"{}\n",
                condition_name="if-match",
                condition_value='"current"',
            )
        headers = {
            key.lower(): value for key, value in captured["request"].header_items()
        }
        self.assertEqual(headers["if-match"], '"current"')
        self.assertIn(
            "SignedHeaders=host;if-match;x-amz-content-sha256;x-amz-date",
            headers["authorization"],
        )

    def test_postwriter_failure_quarantines_and_cannot_owner_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata = self.metadata(directory)
            token_file = directory / "lease.json"
            transport = CASTransport()
            args = self.args(metadata, token_file)
            with mock.patch.object(r2_lease, "request", side_effect=transport):
                r2_lease.acquire(args)
                self.transition(args, "prestate_verified")
                self.transition(args, "writer_started")
                r2_lease.quarantine(args)
                with self.assertRaises(r2_lease.LeaseError):
                    r2_lease.release(args)
            self.assertTrue(token_file.exists())
            self.assertEqual(json.loads(transport.body)["status"], "quarantined")

    def test_sigkill_token_loss_leaves_fail_closed_remote_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata = self.metadata(directory)
            lost_token = directory / "lost.json"
            contender_token = directory / "contender.json"
            transport = CASTransport()
            owner = self.args(metadata, lost_token)
            contender = self.args(metadata, contender_token)
            with mock.patch.object(r2_lease, "request", side_effect=transport):
                r2_lease.acquire(owner)
                lost_token.unlink()
                with self.assertRaisesRegex(
                    r2_lease.LeaseError,
                    "already active",
                ):
                    r2_lease.acquire(contender)
            self.assertFalse(contender_token.exists())
            self.assertEqual(json.loads(transport.body)["status"], "held")

    def test_migration_requires_two_distinct_backend_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata = self.metadata(directory)
            transport = CASTransport()
            valid = self.args(
                metadata,
                directory / "migration.json",
                operation="migration",
            )
            invalid = self.args(
                metadata,
                directory / "invalid.json",
                operation="migration",
            )
            invalid.backend_identity_sha256 = [self.backend_identity] * 2
            with mock.patch.object(r2_lease, "request", side_effect=transport):
                r2_lease.acquire(valid)
            self.assertEqual(
                json.loads(transport.body)["backend_identity_sha256s"],
                [self.backend_identity, self.destination_identity],
            )
            empty_transport = CASTransport()
            with mock.patch.object(
                r2_lease,
                "request",
                side_effect=empty_transport,
            ):
                with self.assertRaises(r2_lease.LeaseError):
                    r2_lease.acquire(invalid)

    @unittest.skipUnless(
        subprocess.run(
            ["sh", "-c", "command -v git && command -v ssh-keygen"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0,
        "git and ssh-keygen are required",
    )
    def test_signed_breakglass_recovery_is_exact_and_conditional(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            project = directory / "project"
            project.mkdir()
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            registry = project / r2_lease.RECOVERY_SIGNERS_PATH
            registry.parent.mkdir(parents=True)
            key = directory / "recovery-key"
            subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(key),
                ],
                check=True,
            )
            public_key = Path(f"{key}.pub").read_text(encoding="utf-8").strip()
            registry.write_text(
                f"{r2_lease.RECOVERY_SIGNER_IDENTITY} {public_key}\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(project), "add", str(registry)],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(project),
                    "-c",
                    "user.name=Lease Test",
                    "-c",
                    "user.email=lease-test@example.invalid",
                    "commit",
                    "-qm",
                    "add recovery signer",
                ],
                check=True,
            )
            source_commit = subprocess.run(
                ["git", "-C", str(project), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            metadata = self.metadata(directory)
            token_file = directory / "lease.json"
            transport = CASTransport()
            args = self.args(metadata, token_file)
            args.source_commit = source_commit
            with mock.patch.object(r2_lease, "request", side_effect=transport):
                r2_lease.acquire(args)
                self.transition(args, "prestate_verified")
                self.transition(args, "writer_started")
                r2_lease.quarantine(args)
            remote = json.loads(transport.body)
            remote_sha = r2_lease.sha256_bytes(transport.body)
            record = {
                "schema": 1,
                "repository_contract": r2_lease.REPOSITORY_CONTRACT,
                "action": "release-quarantined-controller",
                "root": self.root,
                "operation_id": remote["operation_id"],
                "generation": remote["generation"],
                "held_document_sha256": remote_sha,
                "controller_stopped_or_revoked": True,
                "reason": "controller process verified stopped after recovery",
                "approved_at": (
                    datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
                "signer_identity": r2_lease.RECOVERY_SIGNER_IDENTITY,
            }
            record_path = directory / "recovery.json"
            record_path.write_bytes(r2_lease.canonical_json(record))
            subprocess.run(
                [
                    "ssh-keygen",
                    "-Y",
                    "sign",
                    "-f",
                    str(key),
                    "-n",
                    r2_lease.RECOVERY_SIGNATURE_NAMESPACE,
                    str(record_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            recovery = SimpleNamespace(
                metadata=metadata,
                root=self.root,
                recovery_record=record_path,
                project_dir=project,
                confirm=(
                    f"RECOVER:{self.root}:{remote['operation_id']}:"
                    f"{remote['generation']}:{remote_sha}"
                ),
            )
            with mock.patch.object(r2_lease, "request", side_effect=transport):
                r2_lease.recover(recovery)
            self.assertEqual(json.loads(transport.body)["status"], "released")
            last_put = [
                request for request in transport.requests if request["method"] == "PUT"
            ][-1]
            self.assertEqual(last_put["condition_name"], "if-match")


if __name__ == "__main__":
    unittest.main()
