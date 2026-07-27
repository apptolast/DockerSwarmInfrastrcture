"""Functional and static tests for fail-closed Terraform workflows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAFETY_PATH = PROJECT_ROOT / "scripts/terraform-safety.py"
PLAN_WRAPPER = PROJECT_ROOT / "scripts/plan-terraform.sh"
APPLY_WRAPPER = PROJECT_ROOT / "scripts/apply-terraform.sh"
MIGRATE_WRAPPER = PROJECT_ROOT / "scripts/migrate-terraform-state.sh"
SNAPSHOT_WRAPPER = PROJECT_ROOT / "scripts/snapshot-terraform-state.sh"
LOCKING_HARNESS = PROJECT_ROOT / "scripts/test-terraform-r2-locking.sh"
PRIMARY_ACCESS_KEY = "primary-r2-access-key"
CROSS_ACCESS_KEY = "cross-r2-access-key"
BACKEND_REGISTRY_SHA256 = "a" * 64

spec = importlib.util.spec_from_file_location("terraform_safety", SAFETY_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot import Terraform safety helper")
terraform_safety = importlib.util.module_from_spec(spec)
spec.loader.exec_module(terraform_safety)


def passing_check(
    address: str,
    *,
    kind: str = "check",
    instances: list[str] | None = None,
) -> dict[str, object]:
    if kind == "check":
        address_document = {
            "kind": "check",
            "name": address.removeprefix("check."),
            "to_display": address,
        }
    elif kind == "var":
        address_document = {
            "kind": "var",
            "name": address.removeprefix("var."),
            "to_display": address,
        }
    else:
        resource_type, resource_name = address.split(".", maxsplit=1)
        address_document = {
            "kind": "resource",
            "mode": "managed",
            "name": resource_name,
            "to_display": address,
            "type": resource_type,
        }
    return {
        "address": address_document,
        "status": "pass",
        "instances": [
            {
                "address": {"to_display": instance},
                "status": "pass",
            }
            for instance in (instances if instances is not None else [address])
        ],
    }


def passing_raw_check(
    address: str,
    *,
    kind: str,
    instances: list[str] | None = None,
) -> dict[str, object]:
    resolved_instances = instances if instances is not None else [address]
    return {
        "config_addr": address,
        "object_kind": kind,
        "objects": (
            None
            if kind == "resource" and not resolved_instances
            else [
                {
                    "object_addr": instance,
                    "status": "pass",
                }
                for instance in resolved_instances
            ]
        ),
        "status": "pass",
    }


def clean_plan_ui_attestation() -> dict[str, object]:
    return {
        "schema": terraform_safety.CONTRACT_VERSION,
        "operation": "plan",
        "terraform_version": terraform_safety.TERRAFORM_VERSION,
        "ui_version": "1.3",
        "event_count": 3,
        "diagnostic_count": 0,
        "events_sha256": "d" * 64,
        "stderr_sha256": terraform_safety.sha256_bytes(b""),
        "change_summary": {
            "action_invocation": 0,
            "add": 0,
            "change": 1,
            "import": 0,
            "remove": 0,
        },
    }


def dns_state(
    identity: dict[str, object] | None = None,
    serial: int = 7,
    *,
    adoption_only: bool = False,
) -> dict[str, object]:
    expected = terraform_safety.ROOT_IDENTITIES["cloudflare/apptolast-dns"]
    return {
        "version": 4,
        "terraform_version": terraform_safety.TERRAFORM_VERSION,
        "serial": serial,
        "lineage": "8d1020b2-c16d-4c03-95ef-87be9ced54de",
        "outputs": {},
        "check_results": [
            {
                "config_addr": "check.platform_contract",
                "object_kind": "check",
                "objects": [
                    {
                        "object_addr": "check.platform_contract",
                        "status": "pass",
                    }
                ],
                "status": "pass",
            }
        ],
        "resources": [
            {
                "mode": "managed",
                "type": "terraform_data",
                "name": "root_identity",
                "provider": "terraform.io/builtin/terraform",
                "instances": [
                    {
                        "schema_version": 0,
                        "attributes": {
                            "input": identity or expected,
                            "output": identity or expected,
                        },
                    }
                ],
            },
            {
                "mode": "managed",
                "type": "cloudflare_dns_record",
                "name": "managed",
                "provider": ('provider["registry.terraform.io/cloudflare/cloudflare"]'),
                "instances": [
                    {
                        "index_key": key,
                        "schema_version": 1,
                        "attributes": {
                            "id": key,
                            "zone_id": terraform_safety.CLOUDFLARE_ZONE_ID,
                            "name": f"{key}.apptolast.com",
                            "content": (
                                terraform_safety.DNS_LEGACY_IPV4
                                if key == "minecraft"
                                or (adoption_only and key != "edge")
                                else terraform_safety.DNS_PLATFORM_IPV4
                            ),
                            "type": "A",
                            "proxied": False,
                            "ttl": 300,
                        },
                    }
                    for key in terraform_safety.DNS_KEYS
                ],
            },
        ],
    }


def dns_plan(
    actions: list[str] | None = None,
    *,
    adoption_only: bool = False,
    legacy_to_target: bool = False,
) -> dict[str, object]:
    resources = [
        {
            "address": "terraform_data.root_identity",
            "mode": "managed",
            "type": "terraform_data",
            "name": "root_identity",
            "values": {
                "input": terraform_safety.ROOT_IDENTITIES["cloudflare/apptolast-dns"]
            },
        },
        *[
            {
                "address": f"cloudflare_dns_record.managed[{json.dumps(key)}]",
                "mode": "managed",
                "type": "cloudflare_dns_record",
                "name": "managed",
                "index": key,
                "values": {
                    "zone_id": terraform_safety.CLOUDFLARE_ZONE_ID,
                    "name": f"{key}.apptolast.com",
                    "content": (
                        terraform_safety.DNS_LEGACY_IPV4
                        if key == "minecraft" or (adoption_only and key != "edge")
                        else terraform_safety.DNS_PLATFORM_IPV4
                    ),
                    "type": "A",
                    "proxied": False,
                    "ttl": 300,
                },
            }
            for key in terraform_safety.DNS_KEYS
        ],
    ]
    return {
        "format_version": "1.2",
        "terraform_version": terraform_safety.TERRAFORM_VERSION,
        "variables": {"adoption_only": {"value": adoption_only}},
        "timestamp": "2026-07-26T12:00:00Z",
        "applyable": True,
        "complete": True,
        "errored": False,
        "checks": [passing_check("check.platform_contract")],
        "planned_values": {"root_module": {"resources": resources}},
        "resource_changes": [
            {
                "address": 'cloudflare_dns_record.managed["n8n"]',
                "change": {
                    "actions": actions or ["update"],
                    "before": {
                        "content": (
                            terraform_safety.DNS_LEGACY_IPV4
                            if legacy_to_target
                            else (
                                terraform_safety.DNS_LEGACY_IPV4
                                if adoption_only
                                else terraform_safety.DNS_PLATFORM_IPV4
                            )
                        )
                    },
                    "after": {
                        "content": (
                            terraform_safety.DNS_LEGACY_IPV4
                            if adoption_only
                            else terraform_safety.DNS_PLATFORM_IPV4
                        )
                    },
                },
            }
        ],
    }


def s3_metadata(use_lockfile: bool = True) -> dict[str, object]:
    return {
        "version": 3,
        "serial": 1,
        "lineage": "local-metadata",
        "backend": {
            "type": "s3",
            "config": {
                "bucket": "apptolast-dns-state",
                "key": "cloudflare/apptolast-dns/terraform.tfstate",
                "region": "auto",
                "endpoints": {
                    "s3": (
                        "https://"
                        f"{terraform_safety.CLOUDFLARE_ACCOUNT_ID}"
                        ".r2.cloudflarestorage.com"
                    )
                },
                "skip_credentials_validation": True,
                "skip_metadata_api_check": True,
                "skip_region_validation": True,
                "skip_requesting_account_id": True,
                "skip_s3_checksum": True,
                "use_lockfile": use_lockfile,
                "use_path_style": True,
                "access_key": None,
                "secret_key": None,
                "token": None,
            },
        },
    }


def backend_registry() -> dict[str, dict[str, object]]:
    return {
        "cloudflare/apptolast-dns": {
            "production": {
                "backend_type": "s3",
                "bucket": "apptolast-dns-state",
                "key": "cloudflare/apptolast-dns/terraform.tfstate",
                "access_key_id_sha256": terraform_safety.sha256_bytes(
                    PRIMARY_ACCESS_KEY.encode("utf-8")
                ),
            },
            "pending_destination": None,
        },
        "cloudflare/state-bootstrap": {
            "production": {
                "backend_type": "local",
                "path": "/secure/apptolast/terraform-bootstrap.tfstate",
            },
            "pending_destination": None,
        },
        "netcup/perimeter": {
            "production": {
                "backend_type": "s3",
                "bucket": "apptolast-netcup-state",
                "key": "netcup/perimeter/terraform.tfstate",
                "access_key_id_sha256": terraform_safety.sha256_bytes(
                    CROSS_ACCESS_KEY.encode("utf-8")
                ),
            },
            "pending_destination": None,
        },
    }


class TerraformSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        registry_patch = mock.patch.object(
            terraform_safety,
            "load_backend_identities",
            return_value=(backend_registry(), BACKEND_REGISTRY_SHA256),
        )
        signature_patch = mock.patch.object(
            terraform_safety,
            "verify_plan_signature",
            return_value={
                "signature_sha256": "3" * 64,
                "allowed_signers_sha256": "4" * 64,
                "signer_identity": terraform_safety.PLAN_SIGNER,
            },
        )
        registry_patch.start()
        signature_patch.start()
        self.addCleanup(registry_patch.stop)
        self.addCleanup(signature_patch.stop)

    def test_exact_state_identity_and_inventory_are_required(self) -> None:
        attestation = terraform_safety.attest_state(
            "cloudflare/apptolast-dns",
            dns_state(),
        )
        self.assertEqual(attestation["serial"], 7)
        self.assertEqual(len(attestation["inventory"]), 11)
        same_contract_different_bytes = dns_state()
        same_contract_different_bytes["resources"][0]["instances"][0]["attributes"][
            "id"
        ] = "provider-generated-root-id"
        changed_attestation = terraform_safety.attest_state(
            "cloudflare/apptolast-dns",
            same_contract_different_bytes,
        )
        self.assertEqual(
            attestation["managed_contract_sha256"],
            changed_attestation["managed_contract_sha256"],
        )
        self.assertNotEqual(
            attestation["state_sha256"],
            changed_attestation["state_sha256"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            stderr = Path(temporary) / "state-pull.stderr"
            stderr.write_bytes(b"")
            state_bytes = terraform_safety.canonical_json(dns_state())
            self.assertEqual(
                terraform_safety.verify_attested_state_bytes(
                    "cloudflare/apptolast-dns",
                    state_bytes,
                    attestation,
                    stderr,
                ),
                state_bytes,
            )
            changed_state = dns_state(serial=8)
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.verify_attested_state_bytes(
                    "cloudflare/apptolast-dns",
                    terraform_safety.canonical_json(changed_state),
                    attestation,
                    stderr,
                )
            stderr.write_bytes(b"warning")
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.verify_attested_state_bytes(
                    "cloudflare/apptolast-dns",
                    state_bytes,
                    attestation,
                    stderr,
                )

        wrong = dict(terraform_safety.ROOT_IDENTITIES["cloudflare/state-bootstrap"])
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.attest_state(
                "cloudflare/apptolast-dns",
                dns_state(wrong),
            )

        evolving = dns_state()
        evolving["resources"][1]["instances"].pop()
        terraform_safety.attest_state(
            "cloudflare/apptolast-dns",
            evolving,
        )
        unexpected = dns_state()
        unexpected["resources"].append(
            {
                "mode": "managed",
                "type": "cloudflare_dns_record",
                "name": "unreviewed",
                "instances": [{"attributes": {"id": "unexpected"}}],
            }
        )
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.attest_state(
                "cloudflare/apptolast-dns",
                unexpected,
            )

    def test_snapshot_recipient_must_match_versioned_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            registry_path = project / terraform_safety.SNAPSHOT_RECIPIENTS_PATH
            registry_path.parent.mkdir(parents=True)
            recipient = project / "recipients.txt"
            recipient.write_text("age1approvedrecipient\n", encoding="utf-8")
            recipient_hash = terraform_safety.sha256_bytes(recipient.read_bytes())
            registry_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "recipient_file_sha256": recipient_hash,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            attestation = terraform_safety.attest_snapshot_recipient(
                recipient,
                project,
            )
            self.assertEqual(
                attestation["recipient_file_sha256"],
                recipient_hash,
            )
            recipient.write_text("age1unapprovedrecipient\n", encoding="utf-8")
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.attest_snapshot_recipient(recipient, project)

    def test_structural_recovery_state_rejects_empty_or_non_state_json(
        self,
    ) -> None:
        partial = {
            "version": 4,
            "lineage": "partial-initialize-lineage",
            "serial": 1,
            "outputs": {},
            "resources": [
                {
                    "mode": "managed",
                    "type": "cloudflare_dns_record",
                    "name": "partially_imported",
                    "instances": [],
                }
            ],
        }
        terraform_safety.validate_structural_state(partial)
        for invalid in (
            {},
            {"version": 4, "lineage": "", "serial": 0},
            {
                "version": 4,
                "lineage": "lineage",
                "serial": 0,
                "outputs": {},
                "resources": "not-a-list",
            },
        ):
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.validate_structural_state(invalid)

    def test_remote_backend_requires_true_locking_and_current_proof(self) -> None:
        now = datetime(2026, 7, 26, 12, tzinfo=timezone.utc)
        metadata = s3_metadata()
        primary_access_key = PRIMARY_ACCESS_KEY
        cross_access_key = CROSS_ACCESS_KEY
        primary_identity = terraform_safety.sha256_bytes(
            primary_access_key.encode("utf-8")
        )
        locking_scope = {
            "backend_type": "s3",
            "bucket": "apptolast-dns-state",
            "endpoint": (
                "https://"
                f"{terraform_safety.CLOUDFLARE_ACCOUNT_ID}"
                ".r2.cloudflarestorage.com"
            ),
            "root": "cloudflare/apptolast-dns",
            "backend_registry_role": "production",
            "backend_registry_sha256": BACKEND_REGISTRY_SHA256,
            "primary_access_key_id_sha256": primary_identity,
            "terraform_version": terraform_safety.TERRAFORM_VERSION,
        }
        scope_sha = terraform_safety.sha256_bytes(
            terraform_safety.canonical_json(locking_scope)
        )
        positive_scope = {
            "backend_type": "s3",
            "bucket": "apptolast-netcup-state",
            "endpoint": (
                "https://"
                f"{terraform_safety.CLOUDFLARE_ACCOUNT_ID}"
                ".r2.cloudflarestorage.com"
            ),
            "root": "netcup/perimeter",
            "backend_registry_role": "production",
            "backend_registry_sha256": BACKEND_REGISTRY_SHA256,
            "primary_access_key_id_sha256": (
                terraform_safety.sha256_bytes(cross_access_key.encode("utf-8"))
            ),
            "terraform_version": terraform_safety.TERRAFORM_VERSION,
        }
        proof = {
            "schema": 1,
            "terraform_version": terraform_safety.TERRAFORM_VERSION,
            "root": "cloudflare/apptolast-dns",
            "backend_registry_role": "production",
            "cross_root": "netcup/perimeter",
            "locking_scope_sha256": scope_sha,
            "locking_contract_sha256": terraform_safety.locking_contract(PROJECT_ROOT)[
                "sha256"
            ],
            "primary_access_key_id_sha256": primary_identity,
            "cross_access_key_id_sha256": terraform_safety.sha256_bytes(
                cross_access_key.encode("utf-8")
            ),
            "cross_credential_positive_scope_sha256": (
                terraform_safety.sha256_bytes(
                    terraform_safety.canonical_json(positive_scope)
                )
            ),
            "source_commit": "9" * 40,
            "signer_identity": terraform_safety.LOCK_PROOF_SIGNER,
            "tested_at": "2026-07-26T11:00:00Z",
            "valid_until": "2026-07-27T11:00:00Z",
            "operator": "reviewed-test",
            "results": {
                "exclusive_create": True,
                "second_client_blocked": True,
                "normal_release": True,
                "interrupted_client_recovered": True,
                "distributed_operation_lease": True,
                "terraform_backend_access_denied": True,
                "cross_credential_own_bucket_list_succeeded": True,
                "cross_credential_own_bucket_read_succeeded": True,
                "cross_credential_own_bucket_write_succeeded": True,
                "cross_credential_own_bucket_delete_succeeded": True,
                "cross_credential_list_denied": True,
                "cross_credential_read_denied": True,
                "cross_credential_write_denied": True,
                "cross_credential_delete_denied": True,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            proof_path = Path(temporary) / "proof.json"
            proof_path.write_text(json.dumps(proof), encoding="utf-8")
            signature = {
                "signature_sha256": "1" * 64,
                "allowed_signers_sha256": "2" * 64,
                "signer_identity": terraform_safety.LOCK_PROOF_SIGNER,
            }
            with mock.patch.dict(
                os.environ,
                {
                    "AWS_ACCESS_KEY_ID": primary_access_key,
                    "AWS_SECRET_ACCESS_KEY": "primary-r2-secret-key",
                },
                clear=True,
            ):
                with mock.patch.object(
                    terraform_safety,
                    "verify_lock_proof_signature",
                    return_value=signature,
                ):
                    attestation = terraform_safety.attest_backend(
                        "cloudflare/apptolast-dns",
                        metadata,
                        "default",
                        PROJECT_ROOT,
                        proof_path,
                        now,
                    )
                    pending_role_proof = dict(proof)
                    pending_role_proof["backend_registry_role"] = "pending_destination"
                    pending_role_path = Path(temporary) / "pending-proof.json"
                    pending_role_path.write_text(
                        json.dumps(pending_role_proof),
                        encoding="utf-8",
                    )
                    with self.assertRaises(terraform_safety.TerraformSafetyError):
                        terraform_safety.attest_backend(
                            "cloudflare/apptolast-dns",
                            metadata,
                            "default",
                            PROJECT_ROOT,
                            pending_role_path,
                            now,
                        )
            self.assertEqual(
                attestation["locking"]["mechanism"],
                "s3-lockfile",
            )
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.attest_backend(
                    "cloudflare/apptolast-dns",
                    s3_metadata(use_lockfile=False),
                    "default",
                    PROJECT_ROOT,
                    proof_path,
                    now,
                )
            with mock.patch.dict(
                os.environ,
                {
                    "AWS_ACCESS_KEY_ID": primary_access_key,
                    "AWS_SECRET_ACCESS_KEY": "primary-r2-secret-key",
                },
                clear=True,
            ):
                with mock.patch.object(
                    terraform_safety,
                    "verify_lock_proof_signature",
                    return_value=signature,
                ):
                    with self.assertRaises(terraform_safety.TerraformSafetyError):
                        terraform_safety.attest_backend(
                            "cloudflare/apptolast-dns",
                            metadata,
                            "default",
                            PROJECT_ROOT,
                            proof_path,
                            now + timedelta(days=31),
                        )

    def test_shared_r2_credential_waives_only_the_denial_checks(self) -> None:
        now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
        metadata = s3_metadata()
        shared_access_key = PRIMARY_ACCESS_KEY
        shared_identity = terraform_safety.sha256_bytes(
            shared_access_key.encode("utf-8")
        )
        shared_registry = backend_registry()
        shared_registry["netcup/perimeter"]["production"][
            "access_key_id_sha256"
        ] = shared_identity
        locking_scope = {
            "backend_type": "s3",
            "bucket": "apptolast-dns-state",
            "endpoint": (
                "https://"
                f"{terraform_safety.CLOUDFLARE_ACCOUNT_ID}"
                ".r2.cloudflarestorage.com"
            ),
            "root": "cloudflare/apptolast-dns",
            "backend_registry_role": "production",
            "backend_registry_sha256": BACKEND_REGISTRY_SHA256,
            "primary_access_key_id_sha256": shared_identity,
            "terraform_version": terraform_safety.TERRAFORM_VERSION,
        }
        scope_sha = terraform_safety.sha256_bytes(
            terraform_safety.canonical_json(locking_scope)
        )
        positive_scope = {
            "backend_type": "s3",
            "bucket": "apptolast-netcup-state",
            "endpoint": (
                "https://"
                f"{terraform_safety.CLOUDFLARE_ACCOUNT_ID}"
                ".r2.cloudflarestorage.com"
            ),
            "root": "netcup/perimeter",
            "backend_registry_role": "production",
            "backend_registry_sha256": BACKEND_REGISTRY_SHA256,
            "primary_access_key_id_sha256": shared_identity,
            "terraform_version": terraform_safety.TERRAFORM_VERSION,
        }

        def build_proof(*, honest: bool) -> dict:
            denial_value = None if honest else True
            return {
                "schema": 1,
                "terraform_version": terraform_safety.TERRAFORM_VERSION,
                "root": "cloudflare/apptolast-dns",
                "backend_registry_role": "production",
                "cross_root": "netcup/perimeter",
                "locking_scope_sha256": scope_sha,
                "locking_contract_sha256": terraform_safety.locking_contract(
                    PROJECT_ROOT
                )["sha256"],
                "primary_access_key_id_sha256": shared_identity,
                "cross_access_key_id_sha256": shared_identity,
                "cross_credential_positive_scope_sha256": (
                    terraform_safety.sha256_bytes(
                        terraform_safety.canonical_json(positive_scope)
                    )
                ),
                "source_commit": "9" * 40,
                "signer_identity": terraform_safety.LOCK_PROOF_SIGNER,
                "tested_at": "2026-07-27T11:00:00Z",
                "valid_until": "2026-07-28T11:00:00Z",
                "operator": "reviewed-test",
                "results": {
                    "exclusive_create": True,
                    "second_client_blocked": True,
                    "normal_release": True,
                    "interrupted_client_recovered": True,
                    "distributed_operation_lease": True,
                    "terraform_backend_access_denied": denial_value,
                    "cross_credential_own_bucket_list_succeeded": True,
                    "cross_credential_own_bucket_read_succeeded": True,
                    "cross_credential_own_bucket_write_succeeded": True,
                    "cross_credential_own_bucket_delete_succeeded": True,
                    "cross_credential_list_denied": denial_value,
                    "cross_credential_read_denied": denial_value,
                    "cross_credential_write_denied": denial_value,
                    "cross_credential_delete_denied": denial_value,
                },
            }

        signature = {
            "signature_sha256": "1" * 64,
            "allowed_signers_sha256": "2" * 64,
            "signer_identity": terraform_safety.LOCK_PROOF_SIGNER,
        }
        with tempfile.TemporaryDirectory() as temporary:
            honest_path = Path(temporary) / "honest-proof.json"
            honest_path.write_text(
                json.dumps(build_proof(honest=True)), encoding="utf-8"
            )
            dishonest_path = Path(temporary) / "dishonest-proof.json"
            dishonest_path.write_text(
                json.dumps(build_proof(honest=False)), encoding="utf-8"
            )
            with mock.patch.object(
                terraform_safety,
                "load_backend_identities",
                return_value=(shared_registry, BACKEND_REGISTRY_SHA256),
            ):
                with mock.patch.dict(
                    os.environ,
                    {
                        "AWS_ACCESS_KEY_ID": shared_access_key,
                        "AWS_SECRET_ACCESS_KEY": "shared-r2-secret-key",
                    },
                    clear=True,
                ):
                    with mock.patch.object(
                        terraform_safety,
                        "verify_lock_proof_signature",
                        return_value=signature,
                    ):
                        attestation = terraform_safety.attest_backend(
                            "cloudflare/apptolast-dns",
                            metadata,
                            "default",
                            PROJECT_ROOT,
                            honest_path,
                            now,
                        )
                        self.assertEqual(
                            attestation["locking"]["mechanism"],
                            "s3-lockfile",
                        )
                        # A proof claiming True for a property that was
                        # never testable (the credential cannot be denied
                        # access to itself) is a fabricated result, not a
                        # stronger one, and must still be rejected.
                        with self.assertRaises(
                            terraform_safety.TerraformSafetyError
                        ):
                            terraform_safety.attest_backend(
                                "cloudflare/apptolast-dns",
                                metadata,
                                "default",
                                PROJECT_ROOT,
                                dishonest_path,
                                now,
                            )

    def test_every_attest_backend_caller_archives_the_lease_helper(self) -> None:
        # attest-backend -> validate_lock_proof -> locking_contract() hashes
        # every path in LOCKING_CONTRACT_PATHS, including
        # scripts/r2-operation-lease.py. Any wrapper that materializes a
        # runtime copy via `git archive` and then calls the attest-backend
        # subcommand must include that file in its archive list, or the
        # runtime copy is missing a file terraform-safety.py will try to
        # open (plan-terraform.sh did not, until this was found by actually
        # running it against real infrastructure on 2026-07-27).
        wrapper_scripts = sorted((PROJECT_ROOT / "scripts").glob("*.sh"))
        checked = 0
        for script_path in wrapper_scripts:
            text = script_path.read_text(encoding="utf-8")
            if "attest-backend" not in text or "git " not in text:
                continue
            archive_match = re.search(
                r"archive --format=tar[^|]*\|", text, re.DOTALL
            )
            self.assertIsNotNone(
                archive_match,
                f"{script_path.name} calls attest-backend but has no git "
                "archive block to check",
            )
            archived_block = archive_match.group(0)
            self.assertIn(
                "scripts/r2-operation-lease.py",
                archived_block,
                f"{script_path.name} calls attest-backend (which needs "
                "locking_contract()) but its git archive list omits "
                "scripts/r2-operation-lease.py",
            )
            checked += 1
        self.assertGreaterEqual(
            checked,
            2,
            "expected at least plan-terraform.sh and apply-terraform.sh to "
            "be checked here",
        )

    def test_every_terraform_validate_caller_archives_the_config_directory(
        self,
    ) -> None:
        # contract.tf (and other roots) read
        # `${path.module}/../../../../config/*.yml` via yamldecode(file(...)),
        # which Terraform only evaluates starting at `terraform validate`.
        # Any wrapper that materializes a runtime copy via `git archive` and
        # then calls verify-terraform-validate must include config/ in its
        # archive list, or the isolated copy is missing the files those
        # roots read (plan-terraform.sh did not, until this was found by
        # actually running it against real infrastructure on 2026-07-27).
        wrapper_scripts = sorted((PROJECT_ROOT / "scripts").glob("*.sh"))
        checked = 0
        for script_path in wrapper_scripts:
            text = script_path.read_text(encoding="utf-8")
            if "verify-terraform-validate" not in text or "git " not in text:
                continue
            archive_match = re.search(
                r"archive --format=tar[^|]*\|", text, re.DOTALL
            )
            self.assertIsNotNone(
                archive_match,
                f"{script_path.name} calls verify-terraform-validate but "
                "has no git archive block to check",
            )
            archived_block = archive_match.group(0)
            self.assertRegex(
                archived_block,
                r"\bconfig\b",
                f"{script_path.name} calls verify-terraform-validate "
                "(which runs terraform validate against a real root) but "
                "its git archive list omits the config directory that "
                "contract.tf reads via file()",
            )
            checked += 1
        self.assertGreaterEqual(
            checked,
            3,
            "expected plan-terraform.sh, apply-terraform.sh and "
            "migrate-terraform-state.sh to be checked here",
        )

    def test_saved_plan_policy_rejects_every_delete(self) -> None:
        terraform_safety.validate_plan_document(
            "cloudflare/apptolast-dns",
            dns_plan(),
            "established",
        )
        for actions in (["delete"], ["delete", "create"], ["create", "delete"]):
            with self.subTest(actions=actions):
                with self.assertRaises(terraform_safety.TerraformSafetyError):
                    terraform_safety.validate_plan_document(
                        "cloudflare/apptolast-dns",
                        dns_plan(actions),
                        "established",
                    )

    def test_saved_plan_rejects_actions_and_unknown_execution_fields(self) -> None:
        invoked = dns_plan()
        invoked["action_invocations"] = [{"address": "action.example.unreviewed"}]
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.validate_plan_document(
                "cloudflare/apptolast-dns",
                invoked,
                "established",
            )

        triggered = dns_plan()
        triggered["configuration"] = {
            "root_module": {
                "resources": [
                    {
                        "address": "cloudflare_dns_record.managed",
                        "action_triggers": [{"events": ["after_update"]}],
                    }
                ]
            }
        }
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.validate_plan_document(
                "cloudflare/apptolast-dns",
                triggered,
                "established",
            )

        future = dns_plan()
        future["unreviewed_execution_graph"] = {}
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.validate_plan_document(
                "cloudflare/apptolast-dns",
                future,
                "established",
            )

        data_resource = dns_plan()
        data_resource["planned_values"]["root_module"]["resources"].append(
            {
                "address": "data.external.unreviewed",
                "mode": "data",
                "type": "external",
                "name": "unreviewed",
                "values": {},
            }
        )
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.validate_plan_document(
                "cloudflare/apptolast-dns",
                data_resource,
                "established",
            )

        unmodeled_change = dns_plan()
        unmodeled_change["resource_changes"].append(
            {
                "address": "terraform_data.unreviewed",
                "change": {"actions": ["create"]},
            }
        )
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.validate_plan_document(
                "cloudflare/apptolast-dns",
                unmodeled_change,
                "established",
            )

    def test_nested_unknown_values_are_wildcards_but_relationships_hold(
        self,
    ) -> None:
        address = "netcup_server_firewall.host[0]"
        contract = terraform_safety.selected_managed_contract(
            {
                address: (
                    "netcup_server_firewall",
                    {
                        "server_id": terraform_safety.NETCUP_SERVER_ID,
                        "mac": terraform_safety.NETCUP_SERVER_MAC,
                        "policy_ids": [None],
                        "active": True,
                    },
                )
            },
            planned=True,
            unknowns={address: {"policy_ids": [True]}},
        )
        self.assertEqual(
            contract[address]["policy_ids"],
            [terraform_safety.UNKNOWN_VALUE],
        )

        inventory = [
            "terraform_data.root_identity",
            "netcup_firewall_policy.host_ingress[0]",
            address,
        ]
        policy_id = 91234
        expected_contract = {
            "netcup_firewall_policy.host_ingress[0]": {
                "id": terraform_safety.UNKNOWN_VALUE,
                "name": "apptolast-host-ingress",
            },
            address: {
                "server_id": terraform_safety.NETCUP_SERVER_ID,
                "mac": terraform_safety.NETCUP_SERVER_MAC,
                "policy_ids": [terraform_safety.UNKNOWN_VALUE],
                "active": True,
            },
        }
        actual_contract = {
            "netcup_firewall_policy.host_ingress[0]": {
                "id": policy_id,
                "name": "apptolast-host-ingress",
            },
            address: {
                "server_id": terraform_safety.NETCUP_SERVER_ID,
                "mac": terraform_safety.NETCUP_SERVER_MAC,
                "policy_ids": [policy_id],
                "active": True,
            },
        }
        before = {
            "root": "netcup/perimeter",
            "lineage": "netcup-lineage",
            "serial": 3,
            "root_identity": terraform_safety.ROOT_IDENTITIES["netcup/perimeter"],
        }
        after = {
            **before,
            "serial": 4,
            "inventory": inventory,
            "inventory_sha256": terraform_safety.sha256_bytes(
                terraform_safety.canonical_json(inventory)
            ),
            "managed_contract": actual_contract,
            "managed_contract_sha256": terraform_safety.sha256_bytes(
                terraform_safety.canonical_json(actual_contract)
            ),
        }
        sidecar = {
            "planned_inventory": inventory,
            "planned_inventory_sha256": after["inventory_sha256"],
            "planned_managed_contract": expected_contract,
            "planned_managed_contract_sha256": (
                terraform_safety.sha256_bytes(
                    terraform_safety.canonical_json(expected_contract)
                )
            ),
        }
        terraform_safety.verify_post_state(before, after, sidecar)
        after["managed_contract"][address]["policy_ids"] = [policy_id + 1]
        after["managed_contract_sha256"] = terraform_safety.sha256_bytes(
            terraform_safety.canonical_json(after["managed_contract"])
        )
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.verify_post_state(before, after, sidecar)

    def test_dns_initialize_cannot_combine_import_and_cutover(self) -> None:
        terraform_safety.validate_plan_document(
            "cloudflare/apptolast-dns",
            dns_plan(adoption_only=True),
            "initialize",
        )
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.validate_plan_document(
                "cloudflare/apptolast-dns",
                dns_plan(adoption_only=False),
                "initialize",
            )
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.validate_plan_document(
                "cloudflare/apptolast-dns",
                dns_plan(adoption_only=True),
                "established",
            )
        rollback = dns_plan(adoption_only=False)
        rollback["planned_values"]["root_module"]["resources"][5]["values"][
            "content"
        ] = terraform_safety.DNS_LEGACY_IPV4
        terraform_safety.validate_plan_document(
            "cloudflare/apptolast-dns",
            rollback,
            "established",
        )
        with self.assertRaisesRegex(
            terraform_safety.TerraformSafetyError,
            "host-readiness coordinator",
        ):
            terraform_safety.validate_plan_document(
                "cloudflare/apptolast-dns",
                dns_plan(legacy_to_target=True),
                "established",
            )
        initialize_edge = dns_plan(adoption_only=True)
        initialize_edge["resource_changes"][0] = {
            "address": 'cloudflare_dns_record.managed["edge"]',
            "change": {
                "actions": ["create"],
                "before": None,
                "after": {
                    "content": terraform_safety.DNS_PLATFORM_IPV4,
                },
            },
        }
        with self.assertRaisesRegex(
            terraform_safety.TerraformSafetyError,
            "host-readiness coordinator",
        ):
            terraform_safety.validate_plan_document(
                "cloudflare/apptolast-dns",
                initialize_edge,
                "initialize",
            )

    def test_sidecar_binds_commit_plan_backend_and_state(self) -> None:
        plan_bytes = terraform_safety.canonical_json(dns_plan())
        backend = {
            "schema": 1,
            "root": "cloudflare/apptolast-dns",
            "identity_sha256": "a" * 64,
        }
        state = terraform_safety.attest_state(
            "cloudflare/apptolast-dns",
            dns_state(),
        )
        sidecar = terraform_safety.build_sidecar(
            "cloudflare/apptolast-dns",
            plan_bytes,
            "b" * 64,
            "c" * 40,
            backend,
            state,
            None,
            "established",
            clean_plan_ui_attestation(),
        )
        terraform_safety.verify_sidecar(
            "cloudflare/apptolast-dns",
            sidecar,
            plan_bytes,
            "b" * 64,
            "c" * 40,
            backend,
            state,
            terraform_safety.canonical_json(sidecar),
            Path("/unused/plan.metadata.json.sig"),
            PROJECT_ROOT,
            datetime(2026, 7, 26, 12, 30, tzinfo=timezone.utc),
        )
        drifted = dict(state)
        drifted["serial"] = state["serial"] + 1
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.verify_sidecar(
                "cloudflare/apptolast-dns",
                sidecar,
                plan_bytes,
                "b" * 64,
                "c" * 40,
                backend,
                drifted,
                terraform_safety.canonical_json(sidecar),
                Path("/unused/plan.metadata.json.sig"),
                PROJECT_ROOT,
                datetime(2026, 7, 26, 12, 30, tzinfo=timezone.utc),
            )
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.verify_sidecar(
                "cloudflare/apptolast-dns",
                sidecar,
                plan_bytes,
                "b" * 64,
                "c" * 40,
                backend,
                state,
                terraform_safety.canonical_json(sidecar),
                Path("/unused/plan.metadata.json.sig"),
                PROJECT_ROOT,
                datetime(2026, 7, 26, 13, 0, 1, tzinfo=timezone.utc),
            )
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.verify_sidecar(
                "cloudflare/apptolast-dns",
                sidecar,
                plan_bytes,
                "b" * 64,
                "c" * 40,
                backend,
                state,
                terraform_safety.canonical_json(sidecar),
                Path("/unused/plan.metadata.json.sig"),
                PROJECT_ROOT,
                datetime(2026, 7, 26, 11, 59, 59, tzinfo=timezone.utc),
            )

    def test_initialize_and_evolve_lifecycles_converge_exactly(self) -> None:
        plan_bytes = terraform_safety.canonical_json(dns_plan(adoption_only=True))
        backend = {
            "schema": 1,
            "root": "cloudflare/apptolast-dns",
            "identity_sha256": "d" * 64,
        }
        empty = terraform_safety.attest_empty_state("cloudflare/apptolast-dns")
        initialize_sidecar = terraform_safety.build_sidecar(
            "cloudflare/apptolast-dns",
            plan_bytes,
            "e" * 64,
            "f" * 40,
            backend,
            empty,
            None,
            "initialize",
            clean_plan_ui_attestation(),
        )
        initialized = terraform_safety.attest_state(
            "cloudflare/apptolast-dns",
            dns_state(serial=1, adoption_only=True),
        )
        terraform_safety.verify_post_state(
            empty,
            initialized,
            initialize_sidecar,
        )

        evolving_raw = dns_state(serial=2)
        evolving_raw["resources"][1]["instances"].pop()
        evolving = terraform_safety.attest_state(
            "cloudflare/apptolast-dns",
            evolving_raw,
        )
        established_plan_bytes = terraform_safety.canonical_json(dns_plan())
        established_sidecar = terraform_safety.build_sidecar(
            "cloudflare/apptolast-dns",
            established_plan_bytes,
            "1" * 64,
            "2" * 40,
            backend,
            evolving,
            None,
            "established",
            clean_plan_ui_attestation(),
        )
        converged = terraform_safety.attest_state(
            "cloudflare/apptolast-dns",
            dns_state(serial=3),
        )
        terraform_safety.verify_post_state(
            evolving,
            converged,
            established_sidecar,
        )

    def test_wrong_backend_key_and_workspace_are_rejected(self) -> None:
        metadata = s3_metadata()
        metadata["backend"]["config"]["key"] = "netcup/perimeter/terraform.tfstate"
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.attest_backend(
                "cloudflare/apptolast-dns",
                metadata,
                "default",
                PROJECT_ROOT,
                None,
            )
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.attest_backend(
                "cloudflare/apptolast-dns",
                s3_metadata(),
                "production",
                PROJECT_ROOT,
                None,
            )

    def test_backend_registry_and_transport_fail_closed(self) -> None:
        wrong_bucket = s3_metadata()
        wrong_bucket["backend"]["config"]["bucket"] = "unregistered-clone"
        with mock.patch.dict(
            os.environ,
            {
                "AWS_ACCESS_KEY_ID": PRIMARY_ACCESS_KEY,
                "AWS_SECRET_ACCESS_KEY": "primary-r2-secret-key",
            },
            clear=True,
        ):
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.attest_backend(
                    "cloudflare/apptolast-dns",
                    wrong_bucket,
                    "default",
                    PROJECT_ROOT,
                    None,
                    require_lock_proof=False,
                )

            insecure = s3_metadata()
            insecure["backend"]["config"]["insecure"] = True
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.attest_backend(
                    "cloudflare/apptolast-dns",
                    insecure,
                    "default",
                    PROJECT_ROOT,
                    None,
                    require_lock_proof=False,
                )

            wrong_credential_registry = backend_registry()
            wrong_credential_registry["cloudflare/apptolast-dns"]["production"][
                "access_key_id_sha256"
            ] = ("0" * 64)
            with mock.patch.object(
                terraform_safety,
                "load_backend_identities",
                return_value=(
                    wrong_credential_registry,
                    BACKEND_REGISTRY_SHA256,
                ),
            ):
                with self.assertRaises(terraform_safety.TerraformSafetyError):
                    terraform_safety.attest_backend(
                        "cloudflare/apptolast-dns",
                        s3_metadata(),
                        "default",
                        PROJECT_ROOT,
                        None,
                        require_lock_proof=False,
                    )

    def test_pending_destination_is_not_production(self) -> None:
        pending_registry = backend_registry()
        pending_registry["cloudflare/apptolast-dns"]["pending_destination"] = {
            "backend_type": "s3",
            "bucket": "apptolast-dns-state-next",
            "key": "cloudflare/apptolast-dns/terraform.tfstate",
            "access_key_id_sha256": terraform_safety.sha256_bytes(
                b"pending-access-key"
            ),
        }
        pending_metadata = s3_metadata()
        pending_metadata["backend"]["config"]["bucket"] = "apptolast-dns-state-next"
        with mock.patch.object(
            terraform_safety,
            "load_backend_identities",
            return_value=(pending_registry, "d" * 64),
        ):
            with mock.patch.dict(
                os.environ,
                {
                    "AWS_ACCESS_KEY_ID": "pending-access-key",
                    "AWS_SECRET_ACCESS_KEY": "pending-secret-key",
                },
                clear=True,
            ):
                terraform_safety.attest_migration_destination_backend(
                    "cloudflare/apptolast-dns",
                    pending_metadata,
                    "default",
                    PROJECT_ROOT,
                    None,
                    require_lock_proof=False,
                )
                pending_lock_metadata = json.loads(json.dumps(pending_metadata))
                pending_lock_metadata["backend"]["config"][
                    "key"
                ] = "lock-tests/pending-destination.tfstate"
                pending_scope = terraform_safety.describe_locking_scope(
                    pending_lock_metadata,
                    "cloudflare/apptolast-dns",
                    PROJECT_ROOT,
                    "pending_destination",
                )
                self.assertEqual(
                    pending_scope["locking_scope"]["backend_registry_role"],
                    "pending_destination",
                )
                with self.assertRaises(terraform_safety.TerraformSafetyError):
                    terraform_safety.describe_locking_scope(
                        pending_lock_metadata,
                        "cloudflare/apptolast-dns",
                        PROJECT_ROOT,
                        "production",
                    )
                with self.assertRaises(terraform_safety.TerraformSafetyError):
                    terraform_safety.attest_backend(
                        "cloudflare/apptolast-dns",
                        pending_metadata,
                        "default",
                        PROJECT_ROOT,
                        None,
                        require_lock_proof=False,
                    )

    def test_migration_source_backend_rejects_embedded_credentials(self) -> None:
        metadata = s3_metadata()
        metadata["backend"]["config"]["secret_key"] = "must-not-be-persisted"
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.attest_migration_source_backend(
                "cloudflare/apptolast-dns",
                metadata,
                "default",
                PROJECT_ROOT,
                None,
            )

    def test_apply_and_migration_wrappers_keep_required_gates(self) -> None:
        plan = PLAN_WRAPPER.read_text(encoding="utf-8")
        apply = APPLY_WRAPPER.read_text(encoding="utf-8")
        migrate = MIGRATE_WRAPPER.read_text(encoding="utf-8")
        snapshot = SNAPSHOT_WRAPPER.read_text(encoding="utf-8")
        locking = LOCKING_HARNESS.read_text(encoding="utf-8")
        credential_probe = (
            PROJECT_ROOT / "scripts/r2-cross-credential-probe.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("migrate-state -force-copy", plan)
        self.assertIn("attest-state", plan)
        self.assertIn("build-sidecar", plan)
        self.assertIn("ssh-keygen -Y sign", plan)
        self.assertIn("plan.allowed-signers", plan)
        self.assertIn('git -C "${PROJECT_DIR}" archive', plan)
        self.assertIn("verify-sidecar", apply)
        self.assertIn("verify-plan-signature", apply)
        self.assertIn("reviewed.tfplan.metadata.json.sig", apply)
        self.assertIn('git -C "${PROJECT_DIR}" archive', apply)
        self.assertIn('"${runtime_snapshot_bin}"', apply)
        self.assertIn("run_snapshot contract", apply)
        self.assertIn('run_snapshot "${post_state_validation}"', apply)
        self.assertIn("%Y%m%dT%H%M%S.%NZ", snapshot)
        self.assertIn("snapshot_nonce=", snapshot)
        self.assertIn(
            "encrypted state snapshot was not published atomically",
            snapshot,
        )
        self.assertIn(
            "state snapshot checksum was not published atomically",
            snapshot,
        )
        self.assertIn("-lockfile=readonly", snapshot)
        self.assertIn("pass-validated-state", snapshot)
        self.assertIn('--validation "${state_validation}"', snapshot)
        self.assertIn("flock --nonblock 9", snapshot)
        for wrapper in (plan, apply, migrate, snapshot, locking):
            self.assertIn("TF_*)", wrapper)
            self.assertIn("TF_CLI_CONFIG_FILE=/dev/null", wrapper)
        for invariant in (
            "lock-tests/",
            "Error acquiring the state lock",
            "kill -KILL",
            "force-unlock",
            "locking-scope",
            "--backend-role",
            "locking-contract",
            "ssh-keygen -Y sign",
            "lock-proof.allowed-signers",
            "--other-backend-config",
            "--other-root",
            "other-authorized-init.log",
        ):
            self.assertIn(invariant, locking)
        for invariant in (
            "cross_credential_list_denied",
            "cross_credential_read_denied",
            "cross_credential_write_denied",
            "cross_credential_delete_denied",
            "cross_credential_own_bucket_list_succeeded",
            "AccessDenied",
        ):
            self.assertIn(invariant, credential_probe)
        self.assertIn("--confirm", apply)
        self.assertIn("local-backend apply is disabled", apply)
        self.assertIn("local-backend migration is disabled", migrate)
        self.assertIn("quarantine", apply)
        self.assertIn("quarantine", migrate)
        self.assertIn("writer_started", apply)
        self.assertIn("writer_started", migrate)
        self.assertLess(
            apply.index("apply_status=$?"),
            apply.index("post_snapshot_status=$?"),
        )
        self.assertIn(
            "post-attempt snapshot status=${post_snapshot_status}",
            apply,
        )
        self.assertNotIn("-migrate-state", migrate)
        self.assertNotIn("-force-copy", migrate)
        self.assertIn("state pull", migrate)
        self.assertIn("state push", migrate)
        self.assertIn("attest-migration-destination", migrate)
        self.assertIn("--backend-mode migration-destination", migrate)
        self.assertIn("-lock-timeout=60s -", migrate)
        self.assertIn("destination backend already contains", migrate)
        self.assertIn('"${runtime_snapshot_bin}"', migrate)
        self.assertIn("run_source_snapshot", migrate)
        self.assertIn("run_destination_snapshot", migrate)
        self.assertIn("mkfifo --mode=0600", migrate)
        self.assertLess(
            migrate.index("source_writer_status=$?"),
            migrate.index("destination_snapshot_status=$?"),
        )
        self.assertIn(
            "destination emergency snapshot status=${destination_snapshot_status}",
            migrate,
        )

    def test_external_resources_depend_on_root_identity(self) -> None:
        guarded_files = (
            PROJECT_ROOT / "infra/terraform/cloudflare/state-bootstrap/main.tf",
            PROJECT_ROOT / "infra/terraform/cloudflare/apptolast-dns/dns.tf",
            PROJECT_ROOT / "infra/terraform/netcup/perimeter/main.tf",
        )
        expected_dependencies = (4, 1, 3)
        for path, expected_count in zip(
            guarded_files,
            expected_dependencies,
            strict=True,
        ):
            with self.subTest(path=path):
                source = path.read_text(encoding="utf-8")
                self.assertEqual(
                    source.count("depends_on = [terraform_data.root_identity]"),
                    expected_count,
                )

    def test_all_production_check_inventories_pass_exactly(self) -> None:
        bootstrap = terraform_safety.validate_check_results(
            "cloudflare/state-bootstrap",
            [
                passing_check(
                    "terraform_data.root_identity",
                    kind="resource",
                ),
                passing_check("var.cloudflare_account_id", kind="var"),
                passing_check("var.dockerswarm_backup_bucket", kind="var"),
                passing_check("var.state_buckets", kind="var"),
            ],
            {"terraform_data.root_identity"},
        )
        self.assertEqual(
            bootstrap["checks"][0]["address"],
            "terraform_data.root_identity",
        )

        dns = terraform_safety.validate_check_results(
            "cloudflare/apptolast-dns",
            [passing_check("check.platform_contract")],
            {
                "terraform_data.root_identity",
                *{
                    f"cloudflare_dns_record.managed[{json.dumps(key)}]"
                    for key in terraform_safety.DNS_KEYS
                },
            },
        )
        self.assertEqual(dns["checks"][0]["status"], "pass")

        netcup = terraform_safety.validate_check_results(
            "netcup/perimeter",
            [
                passing_check("check.platform_contract"),
                passing_check(
                    "netcup_firewall_policy.host_ingress",
                    kind="resource",
                    instances=[],
                ),
                passing_check(
                    "netcup_server_firewall.host",
                    kind="resource",
                    instances=[],
                ),
                passing_check("var.admin_cidrs", kind="var"),
                passing_check("var.environment", kind="var"),
                passing_check("var.preserved_policy_ids", kind="var"),
                passing_check("var.scp_ssh_public_keys", kind="var"),
                passing_check("var.ssh_port", kind="var"),
            ],
            {"terraform_data.root_identity"},
        )
        self.assertEqual(len(netcup["checks"]), 8)
        raw_netcup = terraform_safety.validate_raw_check_results(
            "netcup/perimeter",
            [
                passing_raw_check(
                    "check.platform_contract",
                    kind="check",
                ),
                passing_raw_check(
                    "netcup_firewall_policy.host_ingress",
                    kind="resource",
                    instances=[],
                ),
                passing_raw_check(
                    "netcup_server_firewall.host",
                    kind="resource",
                    instances=[],
                ),
                *[
                    passing_raw_check(address, kind="var")
                    for address in (
                        "var.admin_cidrs",
                        "var.environment",
                        "var.preserved_policy_ids",
                        "var.scp_ssh_public_keys",
                        "var.ssh_port",
                    )
                ],
            ],
            {"terraform_data.root_identity"},
        )
        self.assertEqual(raw_netcup, netcup)
        invalid_null = [
            passing_raw_check(
                address,
                kind=("check" if address.startswith("check.") else "var"),
            )
            for address in (
                "check.platform_contract",
                "var.admin_cidrs",
                "var.environment",
                "var.preserved_policy_ids",
                "var.scp_ssh_public_keys",
                "var.ssh_port",
            )
        ]
        invalid_null.extend(
            [
                passing_raw_check(
                    "netcup_firewall_policy.host_ingress",
                    kind="resource",
                    instances=[],
                ),
                passing_raw_check(
                    "netcup_server_firewall.host",
                    kind="resource",
                    instances=[],
                ),
            ]
        )
        invalid_null[1]["objects"] = None
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.validate_raw_check_results(
                "netcup/perimeter",
                invalid_null,
                {"terraform_data.root_identity"},
            )

    def test_check_status_and_inventory_fail_closed(self) -> None:
        baseline = dns_plan()
        variants: list[dict[str, object]] = []
        missing = json.loads(json.dumps(baseline))
        missing.pop("checks")
        variants.append(missing)
        empty = json.loads(json.dumps(baseline))
        empty["checks"] = []
        variants.append(empty)
        duplicate = json.loads(json.dumps(baseline))
        duplicate["checks"].append(passing_check("check.platform_contract"))
        variants.append(duplicate)
        extra = json.loads(json.dumps(baseline))
        extra["checks"].append(passing_check("check.unreviewed"))
        variants.append(extra)
        for status in ("fail", "error", "unknown"):
            parent = json.loads(json.dumps(baseline))
            parent["checks"][0]["status"] = status
            variants.append(parent)
            instance = json.loads(json.dumps(baseline))
            instance["checks"][0]["instances"][0]["status"] = status
            variants.append(instance)
        problem = json.loads(json.dumps(baseline))
        problem["checks"][0]["problems"] = [{"message": "withheld"}]
        variants.append(problem)
        for index, variant in enumerate(variants):
            with self.subTest(index=index):
                with self.assertRaises(terraform_safety.TerraformSafetyError):
                    terraform_safety.validate_plan_document(
                        "cloudflare/apptolast-dns",
                        variant,
                        "established",
                    )
        for status in ("fail", "error", "unknown"):
            raw_state = dns_state()
            raw_state["check_results"][0]["status"] = status
            with self.subTest(raw_status=status):
                with self.assertRaises(terraform_safety.TerraformSafetyError):
                    terraform_safety.attest_state(
                        "cloudflare/apptolast-dns",
                        raw_state,
                    )

    def test_post_apply_checks_must_match_signed_plan(self) -> None:
        plan = dns_plan()
        plan_contract = terraform_safety.validate_plan_document(
            "cloudflare/apptolast-dns",
            plan,
            "established",
        )
        state_document = {
            "format_version": "1.0",
            "terraform_version": terraform_safety.TERRAFORM_VERSION,
            "values": plan["planned_values"],
            "checks": plan["checks"],
        }
        sidecar = {
            "root": "cloudflare/apptolast-dns",
            "checks": plan_contract["checks"],
            "checks_sha256": plan_contract["checks_sha256"],
        }
        terraform_safety.verify_post_apply_checks(
            "cloudflare/apptolast-dns",
            state_document,
            sidecar,
        )
        failed = json.loads(json.dumps(state_document))
        failed["checks"][0]["status"] = "fail"
        with self.assertRaises(terraform_safety.TerraformSafetyError):
            terraform_safety.verify_post_apply_checks(
                "cloudflare/apptolast-dns",
                failed,
                sidecar,
            )

    def test_machine_readable_ui_rejects_every_warning_and_truncation(
        self,
    ) -> None:
        version = {
            "@level": "info",
            "@module": "terraform.ui",
            "terraform": terraform_safety.TERRAFORM_VERSION,
            "type": "version",
            "ui": "1.3",
        }
        summary = {
            "@level": "info",
            "@module": "terraform.ui",
            "changes": {
                "action_invocation": 0,
                "add": 1,
                "change": 0,
                "import": 0,
                "operation": "plan",
                "remove": 0,
            },
            "type": "change_summary",
        }
        clean = b"".join(
            terraform_safety.canonical_json(event) for event in (version, summary)
        )
        warning = {
            "@level": "warn",
            "@module": "terraform.ui",
            "diagnostic": {
                "severity": "warning",
                "summary": "Check block assertion failed",
            },
            "type": "diagnostic",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "events.ndjson"
            stderr = root / "stderr"
            events.write_bytes(clean)
            stderr.write_bytes(b"")
            result = terraform_safety.attest_terraform_ui_stream(
                "plan",
                events,
                stderr,
            )
            self.assertEqual(result["diagnostic_count"], 0)

            rejected_streams = [
                clean + terraform_safety.canonical_json(warning),
                clean.removesuffix(b"\n"),
                b"",
                (
                    b'{"@level":"info","@level":"warn",'
                    b'"@module":"terraform.ui","terraform":"1.15.8",'
                    b'"type":"version","ui":"1.3"}\n'
                    + terraform_safety.canonical_json(summary)
                ),
                terraform_safety.canonical_json({**version, "terraform": "1.15.7"})
                + terraform_safety.canonical_json(summary),
                terraform_safety.canonical_json(version),
            ]
            for index, rejected in enumerate(rejected_streams):
                with self.subTest(index=index):
                    events.write_bytes(rejected)
                    stderr.write_bytes(b"")
                    with self.assertRaises(terraform_safety.TerraformSafetyError):
                        terraform_safety.attest_terraform_ui_stream(
                            "plan",
                            events,
                            stderr,
                        )
            events.write_bytes(clean)
            stderr.write_bytes(b"provider warning")
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.attest_terraform_ui_stream(
                    "plan",
                    events,
                    stderr,
                )

    def test_known_benign_r2_diagnostic_is_scoped_narrowly(self) -> None:
        version = {
            "@level": "info",
            "@module": "terraform.ui",
            "terraform": terraform_safety.TERRAFORM_VERSION,
            "type": "version",
            "ui": "1.3",
        }
        summary = {
            "@level": "info",
            "@module": "terraform.ui",
            "changes": {
                "action_invocation": 0,
                "add": 1,
                "change": 0,
                "import": 0,
                "operation": "plan",
                "remove": 0,
            },
            "type": "change_summary",
        }
        clean = b"".join(
            terraform_safety.canonical_json(event) for event in (version, summary)
        )
        # Captured verbatim from a real `terraform plan -json` run against
        # the actual apptolast R2 account on 2026-07-27.
        benign = {
            "@level": "warn",
            "@module": "terraform.ui",
            "diagnostic": {
                "severity": "warning",
                "summary": "Resource Destruction Considerations",
                "detail": (
                    "This resource cannot be destroyed from Terraform. If "
                    "you create this resource, it will be present in the "
                    "API until manually deleted."
                ),
                "address": 'cloudflare_r2_managed_domain.terraform_state["cloudflare_dns"]',
            },
            "type": "diagnostic",
        }
        wrong_summary = {
            **benign,
            "diagnostic": {**benign["diagnostic"], "summary": "Something else"},
        }
        wrong_address = {
            **benign,
            "diagnostic": {
                **benign["diagnostic"],
                "address": "cloudflare_r2_bucket.terraform_state",
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "events.ndjson"
            stderr = root / "stderr"
            stderr.write_bytes(b"")

            # Allowed only for the exact root it was captured against, and
            # only during plan.
            events.write_bytes(
                clean + terraform_safety.canonical_json(benign)
            )
            result = terraform_safety.attest_terraform_ui_stream(
                "plan",
                events,
                stderr,
                "cloudflare/state-bootstrap",
            )
            self.assertEqual(result["diagnostic_count"], 1)

            # Every other root, no root at all, and every other operation
            # still fail closed on the identical diagnostic.
            for operation, root_name in (
                ("plan", None),
                ("plan", "cloudflare/apptolast-dns"),
                ("apply", "cloudflare/state-bootstrap"),
            ):
                with self.subTest(operation=operation, root=root_name):
                    with self.assertRaises(terraform_safety.TerraformSafetyError):
                        terraform_safety.attest_terraform_ui_stream(
                            operation,
                            events,
                            stderr,
                            root_name,
                        )

            # A near-miss (wrong summary text, or wrong resource address)
            # is never treated as benign, even for the matching root.
            for near_miss in (wrong_summary, wrong_address):
                with self.subTest(near_miss=near_miss["diagnostic"]):
                    events.write_bytes(
                        clean + terraform_safety.canonical_json(near_miss)
                    )
                    with self.assertRaises(terraform_safety.TerraformSafetyError):
                        terraform_safety.attest_terraform_ui_stream(
                            "plan",
                            events,
                            stderr,
                            "cloudflare/state-bootstrap",
                        )

            # validate_plan_ui_attestation independently re-bounds
            # diagnostic_count by root: a forged attestation claiming
            # diagnostics for a root with no known-benign allowlist is
            # rejected even if internally self-consistent.
            events.write_bytes(
                clean + terraform_safety.canonical_json(benign)
            )
            attestation = terraform_safety.attest_terraform_ui_stream(
                "plan",
                events,
                stderr,
                "cloudflare/state-bootstrap",
            )
            terraform_safety.validate_plan_ui_attestation(
                attestation, "cloudflare/state-bootstrap"
            )
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.validate_plan_ui_attestation(
                    attestation, "cloudflare/apptolast-dns"
                )

    @unittest.skipUnless(
        (PROJECT_ROOT / ".tools/terraform").is_file(),
        "pinned Terraform binary is not bootstrapped",
    )
    def test_real_terraform_exit_success_check_warning_is_rejected(self) -> None:
        terraform = PROJECT_ROOT / ".tools/terraform"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.tf").write_text(
                f"""
terraform {{
  required_version = "= {terraform_safety.TERRAFORM_VERSION}"
}}

resource "terraform_data" "probe" {{
  input = "probe"
}}

resource "terraform_data" "optional" {{
  count = 0
  input = "optional"

  lifecycle {{
    precondition {{
      condition     = terraform_data.probe.input == "probe"
      error_message = "probe input changed"
    }}
  }}
}}

check "must_fail" {{
  assert {{
    condition     = terraform_data.probe.input != "probe"
    error_message = "deliberate offline check failure"
  }}
}}
""".lstrip(),
                encoding="utf-8",
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("TF_")
            }
            environment.update(
                {
                    "TF_DATA_DIR": str(root / ".terraform-data"),
                    "TF_IN_AUTOMATION": "1",
                    "TF_INPUT": "0",
                }
            )
            init = subprocess.run(
                (
                    str(terraform),
                    f"-chdir={root}",
                    "init",
                    "-backend=false",
                    "-input=false",
                    "-json",
                ),
                check=False,
                capture_output=True,
                env=environment,
            )
            self.assertEqual(init.returncode, 0)
            plan_path = root / "warning.tfplan"
            plan = subprocess.run(
                (
                    str(terraform),
                    f"-chdir={root}",
                    "plan",
                    "-detailed-exitcode",
                    "-input=false",
                    f"-out={plan_path}",
                    "-json",
                ),
                check=False,
                capture_output=True,
                env=environment,
            )
            self.assertEqual(plan.returncode, 2)
            plan_events = root / "plan.ndjson"
            plan_stderr = root / "plan.stderr"
            plan_events.write_bytes(plan.stdout)
            plan_stderr.write_bytes(plan.stderr)
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.attest_terraform_ui_stream(
                    "plan",
                    plan_events,
                    plan_stderr,
                )

            apply = subprocess.run(
                (
                    str(terraform),
                    f"-chdir={root}",
                    "apply",
                    "-input=false",
                    "-json",
                    str(plan_path),
                ),
                check=False,
                capture_output=True,
                env=environment,
            )
            self.assertEqual(apply.returncode, 0)
            apply_events = root / "apply.ndjson"
            apply_stderr = root / "apply.stderr"
            apply_events.write_bytes(apply.stdout)
            apply_stderr.write_bytes(apply.stderr)
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.attest_terraform_ui_stream(
                    "apply",
                    apply_events,
                    apply_stderr,
                )
            raw_state = subprocess.run(
                (
                    str(terraform),
                    f"-chdir={root}",
                    "state",
                    "pull",
                ),
                check=True,
                capture_output=True,
                env=environment,
            )
            state_document = json.loads(raw_state.stdout)
            optional_check = next(
                result
                for result in state_document["check_results"]
                if result["config_addr"] == "terraform_data.optional"
            )
            self.assertEqual(optional_check["status"], "pass")
            self.assertIsNone(optional_check["objects"])

    def test_validate_json_requires_zero_diagnostics(self) -> None:
        clean = {
            "format_version": "1.0",
            "valid": True,
            "error_count": 0,
            "warning_count": 0,
            "diagnostics": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = root / "validate.json"
            stderr = root / "validate.stderr"
            document.write_bytes(terraform_safety.canonical_json(clean))
            stderr.write_bytes(b"")
            terraform_safety.validate_terraform_validate_document(
                document,
                stderr,
            )
            warned = dict(clean)
            warned["warning_count"] = 1
            warned["diagnostics"] = [{"severity": "warning"}]
            document.write_bytes(terraform_safety.canonical_json(warned))
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.validate_terraform_validate_document(
                    document,
                    stderr,
                )

            stderr.write_bytes(
                b"No state file was found!\n"
                b"\n"
                b"State management commands require a state file. Run this command\n"
                b"in a directory where Terraform has been run or use the -state flag\n"
                b"to point the command to a specific state location.\n"
            )
            terraform_safety.validate_empty_state_probe(stderr)
            stderr.write_bytes(
                stderr.read_bytes() + b"Warning: unreviewed diagnostic\n"
            )
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.validate_empty_state_probe(stderr)

    def test_snapshot_artifacts_are_fsynced_and_unique(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            artifact = directory / "snapshot.tfstate.age"
            artifact.write_bytes(b"encrypted snapshot")
            result = terraform_safety.fsync_artifact(artifact, directory)
            self.assertTrue(result["durable"])
            self.assertEqual(
                result["sha256"],
                terraform_safety.sha256_bytes(b"encrypted snapshot"),
            )
            symlink = directory / "snapshot.symlink"
            symlink.symlink_to(artifact)
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.fsync_artifact(symlink, directory)
            hardlink = directory / "snapshot.hardlink"
            os.link(artifact, hardlink)
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.fsync_artifact(artifact, directory)

        snapshot = SNAPSHOT_WRAPPER.read_text(encoding="utf-8")
        self.assertGreaterEqual(snapshot.count("fsync-artifact"), 5)
        self.assertLess(
            snapshot.index('fsync-artifact \\\n  --path "${snapshot_tmp}"'),
            snapshot.index('mv --no-clobber "${snapshot_tmp}" "${snapshot_path}"'),
        )
        self.assertLess(
            snapshot.index('fsync-artifact \\\n  --path "${checksum_tmp}"'),
            snapshot.index('mv --no-clobber "${checksum_tmp}" "${checksum_path}"'),
        )
        self.assertLess(
            snapshot.rindex('fsync-artifact \\\n  --path "${checksum_path}"'),
            snapshot.index("Encrypted state snapshot created:"),
        )


class TerraformPlanSignatureTests(unittest.TestCase):
    def test_real_openssh_signature_verification_and_tamper_rejection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            terraform_dir = project / "infra/terraform"
            terraform_dir.mkdir(parents=True)
            private_key = project / "plan-signing-key"
            subprocess.run(
                (
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(private_key),
                ),
                check=True,
            )
            public_key = Path(f"{private_key}.pub").read_text(encoding="utf-8").strip()
            (terraform_dir / "plan.allowed-signers").write_text(
                f"{terraform_safety.PLAN_SIGNER} "
                f'namespaces="{terraform_safety.PLAN_NAMESPACE}" '
                f"{public_key}\n",
                encoding="utf-8",
            )
            sidecar = project / "plan.metadata.json"
            sidecar.write_text('{"schema":1}\n', encoding="utf-8")
            subprocess.run(
                (
                    "ssh-keygen",
                    "-Y",
                    "sign",
                    "-f",
                    str(private_key),
                    "-n",
                    terraform_safety.PLAN_NAMESPACE,
                    str(sidecar),
                ),
                check=True,
                stdout=subprocess.DEVNULL,
            )
            result = terraform_safety.verify_plan_signature(
                sidecar.read_bytes(),
                Path(f"{sidecar}.sig"),
                project,
            )
            self.assertEqual(
                result["signer_identity"],
                terraform_safety.PLAN_SIGNER,
            )
            with self.assertRaises(terraform_safety.TerraformSafetyError):
                terraform_safety.verify_plan_signature(
                    b'{"schema":2}\n',
                    Path(f"{sidecar}.sig"),
                    project,
                )


if __name__ == "__main__":
    unittest.main()
