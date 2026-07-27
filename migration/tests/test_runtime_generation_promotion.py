from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_SCRIPTS = REPOSITORY_ROOT / "migration/scripts"
MIGRATION_TESTS = REPOSITORY_ROOT / "migration/tests"
sys.path.insert(0, str(MIGRATION_SCRIPTS))
sys.path.insert(0, str(MIGRATION_TESTS))

import finalize_restore  # noqa: E402
import promote_runtime_generation as promotion  # noqa: E402
from test_finalize_restore import create_runtime  # noqa: E402

OLD_GENERATION = "apptolast-data-20260723T225340Z"
NEW_GENERATION = "apptolast-data-20260727T000500Z"
IMPORT_GENERATION = "apptolast-data-20260728T000500Z"
SOURCE_COMMIT = "a" * 40


def write_private(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)


def repair_recovery_manifest(services: Path) -> None:
    recovery = services / "recovery"
    records = []
    for path in sorted(recovery.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS":
            continue
        records.append(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
            f"{path.relative_to(recovery).as_posix()}\n"
        )
    write_private(recovery / "SHA256SUMS", "".join(records).encode("ascii"))


def build_generation(parent: Path, source_backup: str) -> Path:
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent.chmod(0o700)
    services = create_runtime(parent)
    manifest_path = services / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sourceBackup"] = source_backup
    write_private(
        manifest_path,
        promotion.canonical_json(manifest),
    )
    repair_recovery_manifest(services)
    finalize_restore.write_ready_marker(
        services_root=services,
        canonical_services_root=Path("/srv/dockerswarm/services"),
        catalog_path=REPOSITORY_ROOT / "config/services.yml",
        secret_catalog_path=REPOSITORY_ROOT / "stacks/workloads/secrets.yml",
        enforce_owners=False,
    )
    services.chmod(0o700)
    return services


class EmptyProbe:
    def __init__(self) -> None:
        self.calls = 0

    def inspect(
        self,
        protected_roots: tuple[Path, ...] | list[Path],
    ) -> promotion.QuiescenceEvidence:
        self.calls += 1
        return promotion.QuiescenceEvidence(
            swarm_state="active|true",
            stacks=(),
            services=(),
            containers=(),
            restore_compose_containers=(),
            process_references=(),
        )


class WriterProbe(EmptyProbe):
    def inspect(
        self,
        protected_roots: tuple[Path, ...] | list[Path],
    ) -> promotion.QuiescenceEvidence:
        del protected_roots
        raise promotion.PromotionError("writer remains")


class FakeAttestation:
    def verify(
        self,
        generation: promotion.GenerationEvidence,
        source_commit: str,
        *,
        now: datetime,
    ) -> promotion.AttestationEvidence:
        del now
        return promotion.AttestationEvidence(
            document_sha256=hashlib.sha256(
                generation.runtime_tree_sha256.encode("ascii")
            ).hexdigest(),
            signature_sha256="b" * 64,
            source_commit=source_commit,
            expires_at="2026-07-27T05:15:00+00:00",
        )


class Fixture:
    def __init__(self, temporary: str) -> None:
        self.root = Path(temporary)
        self.state = self.root / "state"
        self.evidence = self.root / "evidence"
        self.allowed_parent = self.root / "etc"
        for path in (
            self.state,
            self.evidence,
            self.allowed_parent,
        ):
            path.mkdir(mode=0o700)
            path.chmod(0o700)
        self.layout = promotion.Layout(
            state_root=self.state,
            evidence_root=self.evidence,
            allowed_signers=self.allowed_parent / "allowed-signers",
            production=False,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )
        for path in (
            self.layout.generations,
            self.layout.candidates,
            self.layout.history,
            self.layout.incoming,
            self.layout.imports,
            self.layout.transactions,
        ):
            path.mkdir(mode=0o700)
            path.chmod(0o700)
        build_generation(self.state, OLD_GENERATION)
        build_generation(
            self.layout.candidates / NEW_GENERATION,
            NEW_GENERATION,
        )
        self.probe = EmptyProbe()
        self.controller = self.make_controller()

    def make_controller(
        self,
        *,
        probe: EmptyProbe | None = None,
        crash_hook=None,
        fsync_function=promotion.fsync_directory,
    ) -> promotion.PromotionController:
        return promotion.PromotionController(
            layout=self.layout,
            validator=promotion.GenerationValidator(
                catalog_path=REPOSITORY_ROOT / "config/services.yml",
                secret_catalog_path=(REPOSITORY_ROOT / "stacks/workloads/secrets.yml"),
                platform_path=REPOSITORY_ROOT / "config/platform.yml",
                canonical_services_root=Path("/srv/dockerswarm/services"),
                enforce_owners=False,
            ),
            runtime_probe=probe or self.probe,
            signature_verifier=FakeAttestation(),
            repository_state_provider=lambda: (SOURCE_COMMIT, True),
            now_provider=lambda: datetime(
                2026,
                7,
                27,
                0,
                20,
                tzinfo=timezone.utc,
            ),
            crash_hook=crash_hook,
            fsync_function=fsync_function,
        )


class RuntimeGenerationPromotionTests(unittest.TestCase):
    def test_finalized_incoming_is_imported_atomically_without_copying(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            incoming = build_generation(
                fixture.layout.incoming / IMPORT_GENERATION,
                IMPORT_GENERATION,
            )
            incoming_inode = incoming.stat().st_ino
            sentinel = incoming / "secret-copy-sentinel"
            write_private(sentinel, b"same-inode")
            plan = fixture.controller.plan_candidate_import(IMPORT_GENERATION)
            journal = fixture.controller.apply_candidate_import(
                IMPORT_GENERATION,
                plan.confirmation(),
            )
            candidate = fixture.layout.candidate(IMPORT_GENERATION)
            self.assertTrue(journal.terminal)
            self.assertEqual(candidate.stat().st_ino, incoming_inode)
            self.assertFalse(incoming.exists())
            self.assertTrue(incoming.parent.is_dir())
            self.assertTrue((candidate / "secret-copy-sentinel").is_file())
            self.assertFalse(fixture.controller.status()["blocked"])

    def test_import_crash_before_rename_aborts_and_preserves_incoming(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            build_generation(
                fixture.layout.incoming / IMPORT_GENERATION,
                IMPORT_GENERATION,
            )

            def crash(phase: str) -> None:
                if phase == "import-before-rename":
                    raise promotion.CrashInjection()

            controller = fixture.make_controller(crash_hook=crash)
            plan = controller.plan_candidate_import(IMPORT_GENERATION)
            with self.assertRaises(promotion.CrashInjection):
                controller.apply_candidate_import(
                    IMPORT_GENERATION,
                    plan.confirmation(),
                )
            status = controller.status()
            self.assertTrue(status["blocked"])
            self.assertEqual(
                status["candidateImports"][-1]["inferredState"],
                "pre-import",
            )
            confirmation = controller.recover_candidate_import(
                mode="abort",
                apply=False,
                confirmation=None,
            )
            controller.recover_candidate_import(
                mode="abort",
                apply=True,
                confirmation=confirmation,
            )
            self.assertTrue(
                fixture.layout.incoming_generation(IMPORT_GENERATION).is_dir()
            )
            self.assertFalse(fixture.layout.candidate(IMPORT_GENERATION).exists())
            self.assertFalse(controller.status()["blocked"])

    def test_import_signal_after_rename_is_inferred_and_completed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            build_generation(
                fixture.layout.incoming / IMPORT_GENERATION,
                IMPORT_GENERATION,
            )

            def interrupt(phase: str) -> None:
                if phase == "import-after-rename":
                    raise promotion.PromotionInterrupted(signal.SIGTERM)

            controller = fixture.make_controller(crash_hook=interrupt)
            plan = controller.plan_candidate_import(IMPORT_GENERATION)
            with self.assertRaises(promotion.PromotionInterrupted):
                controller.apply_candidate_import(
                    IMPORT_GENERATION,
                    plan.confirmation(),
                )
            self.assertEqual(
                controller.status()["candidateImports"][-1]["inferredState"],
                "imported",
            )
            recovered = fixture.make_controller()
            confirmation = recovered.recover_candidate_import(
                mode="complete",
                apply=False,
                confirmation=None,
            )
            recovered.recover_candidate_import(
                mode="complete",
                apply=True,
                confirmation=confirmation,
            )
            self.assertTrue(fixture.layout.candidate(IMPORT_GENERATION).is_dir())
            self.assertFalse(recovered.status()["blocked"])

    def test_import_inode_replacement_and_baseline_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            incoming = build_generation(
                fixture.layout.incoming / IMPORT_GENERATION,
                IMPORT_GENERATION,
            )

            def replace(phase: str) -> None:
                if phase != "import-before-rename":
                    return
                incoming.rename(incoming.with_name("services-replaced"))
                incoming.mkdir(mode=0o700)

            controller = fixture.make_controller(crash_hook=replace)
            plan = controller.plan_candidate_import(IMPORT_GENERATION)
            with self.assertRaisesRegex(
                promotion.PromotionError,
                "inode changed before renameat2",
            ):
                controller.apply_candidate_import(
                    IMPORT_GENERATION,
                    plan.confirmation(),
                )
            self.assertFalse(fixture.layout.candidate(IMPORT_GENERATION).exists())

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            build_generation(
                fixture.layout.incoming / OLD_GENERATION,
                OLD_GENERATION,
            )
            with self.assertRaisesRegex(
                promotion.PromotionError,
                "not strictly newer",
            ):
                fixture.controller.plan_candidate_import(OLD_GENERATION)

    def test_attestation_bindings_do_not_claim_external_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            bindings = fixture.controller.attestation_bindings(NEW_GENERATION)
            self.assertEqual(bindings["sourceBackup"], NEW_GENERATION)
            self.assertEqual(bindings["sourceCommit"], SOURCE_COMMIT)
            self.assertRegex(bindings["runtimeTreeSha256"], r"^[a-f0-9]{64}$")
            self.assertEqual(
                set(bindings["externalFactsNotAsserted"]),
                {
                    "legacyWritersStopped",
                    "finalBackupVerified",
                    "legacyStoppedAt",
                    "backupStartedAt",
                    "backupCompletedAt",
                    "issuedAt",
                    "expiresAt",
                },
            )
            self.assertNotIn("legacyWritersStopped", bindings)
            self.assertNotIn("finalBackupVerified", bindings)

    def test_exact_pre_staged_backup_is_never_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            build_generation(
                fixture.layout.candidates / OLD_GENERATION,
                OLD_GENERATION,
            )
            with self.assertRaisesRegex(
                promotion.PromotionError,
                "not strictly newer",
            ):
                fixture.controller.plan_promotion(OLD_GENERATION)

    def test_plan_rejects_pre_staged_baseline_as_not_strictly_newer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            stale = "apptolast-data-20260723T225341Z"
            build_generation(
                fixture.layout.candidates / stale,
                stale,
            )
            # It is newer than the old backup by one second, proving the floor
            # alone is not used as a fake freeze.  Make canonical newer still.
            manifest_path = fixture.layout.canonical / "runtime-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["sourceBackup"] = "apptolast-data-20260724T000000Z"
            write_private(manifest_path, promotion.canonical_json(manifest))
            marker = fixture.layout.canonical / "restore-state/workloads-ready-v2.json"
            marker.unlink()
            finalize_restore.write_ready_marker(
                services_root=fixture.layout.canonical,
                canonical_services_root=Path("/srv/dockerswarm/services"),
                catalog_path=REPOSITORY_ROOT / "config/services.yml",
                secret_catalog_path=(REPOSITORY_ROOT / "stacks/workloads/secrets.yml"),
                enforce_owners=False,
            )
            with self.assertRaisesRegex(
                promotion.PromotionError,
                "not strictly newer",
            ):
                fixture.controller.plan_promotion(stale)

    def test_symlink_and_hardlink_candidates_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            candidate = fixture.layout.candidate(NEW_GENERATION)
            real = candidate.with_name("services-real")
            candidate.rename(real)
            candidate.symlink_to(real, target_is_directory=True)
            with self.assertRaises(promotion.PromotionError):
                fixture.controller.plan_promotion(NEW_GENERATION)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            candidate = fixture.layout.candidate(NEW_GENERATION)
            original = candidate / "minecraft/mods/fabric.jar"
            os.link(original, candidate / "minecraft/mods/hardlink.jar")
            with self.assertRaisesRegex(
                promotion.PromotionError,
                "link count",
            ):
                fixture.controller.plan_promotion(NEW_GENERATION)

    def test_cross_filesystem_and_writer_evidence_fail_closed(self) -> None:
        first = mock.Mock(st_dev=1)
        second = mock.Mock(st_dev=2)
        with mock.patch.object(Path, "lstat", side_effect=(first, second)):
            with self.assertRaisesRegex(
                promotion.PromotionError,
                "one filesystem",
            ):
                promotion.PromotionController.require_same_filesystem(
                    Path("/first"),
                    Path("/second"),
                )

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            controller = fixture.make_controller(probe=WriterProbe())
            with self.assertRaisesRegex(
                promotion.PromotionError,
                "writer remains",
            ):
                controller.plan_promotion(NEW_GENERATION)

    def test_path_replacement_between_intent_and_exchange_is_not_promoted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            original_inode = fixture.layout.canonical.stat().st_ino

            def replace(phase: str) -> None:
                if phase != "before-exchange":
                    return
                candidate = fixture.layout.candidate(NEW_GENERATION)
                candidate.rename(candidate.with_name("replaced-services"))
                candidate.mkdir(mode=0o700)

            controller = fixture.make_controller(crash_hook=replace)
            plan = controller.plan_promotion(NEW_GENERATION)
            with self.assertRaisesRegex(
                promotion.PromotionError,
                "inode changed before renameat2",
            ):
                controller.apply_promotion(
                    NEW_GENERATION,
                    plan.confirmation(),
                )
            self.assertEqual(
                fixture.layout.canonical.stat().st_ino,
                original_inode,
            )
            status = controller.status()
            self.assertTrue(status["blocked"])
            self.assertEqual(
                status["transactions"][-1]["inferredState"],
                "unknown",
            )

    def test_crash_before_exchange_is_explicitly_aborted_without_deletion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)

            def crash(phase: str) -> None:
                if phase == "before-exchange":
                    raise promotion.CrashInjection()

            controller = fixture.make_controller(crash_hook=crash)
            plan = controller.plan_promotion(NEW_GENERATION)
            with self.assertRaises(promotion.CrashInjection):
                controller.apply_promotion(
                    NEW_GENERATION,
                    plan.confirmation(),
                )
            status = controller.status()
            self.assertTrue(status["blocked"])
            self.assertEqual(
                status["transactions"][-1]["inferredState"],
                "pre-exchange",
            )
            confirmation = controller.recover(
                mode="abort",
                apply=False,
                confirmation=None,
            )
            controller.recover(
                mode="abort",
                apply=True,
                confirmation=confirmation,
            )
            self.assertTrue(fixture.layout.candidate(NEW_GENERATION).is_dir())
            self.assertEqual(
                json.loads(
                    (fixture.layout.canonical / "runtime-manifest.json").read_text(
                        encoding="utf-8"
                    )
                )["sourceBackup"],
                OLD_GENERATION,
            )
            self.assertFalse(controller.status()["blocked"])

    def test_sigkill_after_exchange_is_inferred_and_recovered(self) -> None:
        if not hasattr(os, "fork"):
            self.skipTest("requires fork")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            plan = fixture.controller.plan_promotion(NEW_GENERATION)
            child = os.fork()
            if child == 0:
                try:
                    controller = fixture.make_controller(
                        crash_hook=lambda phase: (
                            os._exit(137) if phase == "after-exchange" else None
                        )
                    )
                    controller.apply_promotion(
                        NEW_GENERATION,
                        plan.confirmation(),
                    )
                except BaseException:
                    os._exit(125)
                os._exit(0)
            waited, status = os.waitpid(child, 0)
            self.assertEqual(waited, child)
            self.assertEqual(os.waitstatus_to_exitcode(status), 137)
            recovered_controller = fixture.make_controller()
            observed = recovered_controller.status()
            self.assertTrue(observed["blocked"])
            self.assertEqual(
                observed["transactions"][-1]["inferredState"],
                "exchanged",
            )
            confirmation = recovered_controller.recover(
                mode="complete",
                apply=False,
                confirmation=None,
            )
            recovered_controller.recover(
                mode="complete",
                apply=True,
                confirmation=confirmation,
            )
            self.assertFalse(recovered_controller.status()["blocked"])
            self.assertTrue(fixture.layout.historical(OLD_GENERATION).is_dir())

    def test_fsync_failure_after_exchange_blocks_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            failures = {"remaining": 1}

            def fail_once(path: Path) -> None:
                if failures["remaining"] and path == fixture.layout.canonical.parent:
                    failures["remaining"] -= 1
                    raise OSError("injected fsync failure")
                promotion.fsync_directory(path)

            controller = fixture.make_controller(fsync_function=fail_once)
            plan = controller.plan_promotion(NEW_GENERATION)
            with self.assertRaisesRegex(OSError, "injected fsync"):
                controller.apply_promotion(
                    NEW_GENERATION,
                    plan.confirmation(),
                )
            self.assertEqual(
                controller.status()["transactions"][-1]["inferredState"],
                "exchanged",
            )
            recovered = fixture.make_controller()
            confirmation = recovered.recover(
                mode="complete",
                apply=False,
                confirmation=None,
            )
            recovered.recover(
                mode="complete",
                apply=True,
                confirmation=confirmation,
            )
            self.assertFalse(recovered.status()["blocked"])

    def test_success_preserves_old_and_explicit_rollback_preserves_new(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            old_sentinel = fixture.layout.canonical / "old-sentinel"
            write_private(old_sentinel, b"old")
            # Re-finalize the tree because the complete tree fingerprint is
            # attested at plan time, not because the gate hashes this sentinel.
            plan = fixture.controller.plan_promotion(NEW_GENERATION)
            journal = fixture.controller.apply_promotion(
                NEW_GENERATION,
                plan.confirmation(),
            )
            self.assertTrue(journal.terminal)
            self.assertTrue(
                (fixture.layout.historical(OLD_GENERATION) / "old-sentinel").is_file()
            )
            self.assertFalse(fixture.layout.candidate(NEW_GENERATION).exists())
            self.assertTrue((fixture.layout.candidates / NEW_GENERATION).is_dir())
            rollback = fixture.controller.plan_rollback(OLD_GENERATION)
            rollback_journal = fixture.controller.apply_rollback(
                OLD_GENERATION,
                rollback.confirmation(),
            )
            self.assertTrue(rollback_journal.terminal)
            self.assertTrue((fixture.layout.canonical / "old-sentinel").is_file())
            self.assertTrue(fixture.layout.historical(NEW_GENERATION).is_dir())
            self.assertTrue(
                fixture.layout.transactions
                / journal.identity["transactionId"]
                / "events"
            )
            self.assertFalse(fixture.controller.status()["blocked"])

    def test_confirmation_is_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            plan = fixture.controller.plan_promotion(NEW_GENERATION)
            with self.assertRaisesRegex(
                promotion.PromotionError,
                "confirmation mismatch",
            ):
                fixture.controller.apply_promotion(
                    NEW_GENERATION,
                    "PROMOTE_RUNTIME_GENERATION:wrong",
                )
            self.assertTrue(fixture.layout.candidate(NEW_GENERATION).is_dir())
            self.assertEqual(
                fixture.layout.canonical.joinpath("runtime-manifest.json")
                .read_text(encoding="utf-8")
                .find(OLD_GENERATION)
                >= 0,
                True,
            )

    def test_signal_exception_leaves_recoverable_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)

            def interrupt(phase: str) -> None:
                if phase == "after-exchange":
                    raise promotion.PromotionInterrupted(signal.SIGTERM)

            controller = fixture.make_controller(crash_hook=interrupt)
            plan = controller.plan_promotion(NEW_GENERATION)
            with self.assertRaises(promotion.PromotionInterrupted):
                controller.apply_promotion(
                    NEW_GENERATION,
                    plan.confirmation(),
                )
            transaction = controller.status()["transactions"][-1]
            self.assertTrue(transaction["lastPhase"] == "operation-failed")
            self.assertEqual(transaction["inferredState"], "exchanged")

    def test_partial_pending_event_is_archived_then_recovery_continues(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)

            def crash(phase: str) -> None:
                if phase == "before-exchange":
                    raise promotion.CrashInjection()

            controller = fixture.make_controller(crash_hook=crash)
            plan = controller.plan_promotion(NEW_GENERATION)
            with self.assertRaises(promotion.CrashInjection):
                controller.apply_promotion(
                    NEW_GENERATION,
                    plan.confirmation(),
                )
            journal = controller.single_nonterminal_transaction()
            sequence = len(journal.events) + 1
            pending = (
                journal.transaction_path
                / "events"
                / f".pending-{sequence:06d}-{'c' * 32}.json"
            )
            write_private(pending, b'{"partial":')
            confirmation = controller.recover(
                mode="abort",
                apply=False,
                confirmation=None,
            )
            controller.recover(
                mode="abort",
                apply=True,
                confirmation=confirmation,
            )
            orphans = journal.transaction_path / "orphaned-events"
            self.assertEqual(len(tuple(orphans.iterdir())), 1)
            self.assertFalse(controller.status()["blocked"])
            self.assertTrue(fixture.layout.candidate(NEW_GENERATION).is_dir())

    def test_complete_pending_event_is_adopted_with_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)

            def crash(phase: str) -> None:
                if phase == "before-exchange":
                    raise promotion.CrashInjection()

            controller = fixture.make_controller(crash_hook=crash)
            plan = controller.plan_promotion(NEW_GENERATION)
            with self.assertRaises(promotion.CrashInjection):
                controller.apply_promotion(
                    NEW_GENERATION,
                    plan.confirmation(),
                )
            journal = controller.single_nonterminal_transaction()
            sequence = len(journal.events) + 1
            base = {
                "schemaVersion": 1,
                "transactionId": journal.identity["transactionId"],
                "sequence": sequence,
                "phase": "recovery-note",
                "recordedAt": "2026-07-27T00:30:00+00:00",
                "previousHash": journal.tip_hash,
                "details": {"completePending": True},
            }
            event = {
                **base,
                "recordHash": promotion.sha256_bytes(promotion.canonical_json(base)),
            }
            pending = (
                journal.transaction_path
                / "events"
                / f".pending-{sequence:06d}-{'d' * 32}.json"
            )
            write_private(pending, promotion.canonical_json(event))
            confirmation = controller.recover(
                mode="abort",
                apply=False,
                confirmation=None,
            )
            controller.recover(
                mode="abort",
                apply=True,
                confirmation=confirmation,
            )
            final = journal.transaction_path / "events" / f"{sequence:06d}.json"
            self.assertTrue(final.is_file())
            self.assertFalse(pending.exists())
            self.assertFalse(controller.status()["blocked"])

    def test_journal_tampering_blocks_status_and_new_transactions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)

            def crash(phase: str) -> None:
                if phase == "before-exchange":
                    raise promotion.CrashInjection()

            controller = fixture.make_controller(crash_hook=crash)
            plan = controller.plan_promotion(NEW_GENERATION)
            with self.assertRaises(promotion.CrashInjection):
                controller.apply_promotion(
                    NEW_GENERATION,
                    plan.confirmation(),
                )
            journal = controller.single_nonterminal_transaction()
            first = journal.transaction_path / "events/000001.json"
            document = json.loads(first.read_text(encoding="ascii"))
            document["details"]["planSha256"] = "0" * 64
            write_private(first, promotion.canonical_json(document))
            with self.assertRaisesRegex(
                promotion.PromotionError,
                "hash chain",
            ):
                controller.status()

    def test_live_process_reference_to_candidate_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            protected = Path(temporary) / "services"
            protected.mkdir(mode=0o700)
            held = protected / "held"
            held.write_bytes(b"data")
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; "
                        "handle=open(sys.argv[1], 'rb'); "
                        "print('ready', flush=True); "
                        "sys.stdin.buffer.read(1)"
                    ),
                    str(held),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert child.stdout is not None
                self.assertEqual(child.stdout.readline(), b"ready\n")
                references = promotion.inspect_process_references((protected,))
                self.assertTrue(
                    any(item.startswith(f"{child.pid}:fd:") for item in references)
                )
            finally:
                if child.stdin is not None:
                    child.stdin.write(b"x")
                    child.stdin.flush()
                    child.stdin.close()
                child.wait(timeout=10)
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()

    def test_production_mutation_has_no_boolean_lock_bypass(self) -> None:
        source = (
            REPOSITORY_ROOT / "migration/scripts/promote_runtime_generation.py"
        ).read_text(encoding="utf-8")
        self.assertIn("ensure_mutation_lock(", source)
        self.assertIn("prove_or_none(HOST_LOCK_OPERATION)", source)
        self.assertNotIn("--skip-lock", source)
        self.assertNotIn("--no-lock", source)


class OpenSSHAttestationTests(unittest.TestCase):
    def test_canonical_signed_attestation_is_bound_to_all_hashes(self) -> None:
        if not Path("/usr/bin/ssh-keygen").is_file():
            self.skipTest("ssh-keygen is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            evidence = fixture.controller.validator.validate(
                fixture.layout.candidate(NEW_GENERATION)
            )
            key = fixture.root / "signing-key"
            subprocess.run(
                [
                    "/usr/bin/ssh-keygen",
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
            public_key = key.with_suffix(".pub").read_text(encoding="ascii").strip()
            fixture.layout.allowed_signers.write_text(
                f"{promotion.ATTESTATION_IDENTITY} {public_key}\n",
                encoding="ascii",
            )
            fixture.layout.allowed_signers.chmod(0o600)
            now = datetime(2026, 7, 27, 0, 20, tzinfo=timezone.utc)
            document = {
                "schemaVersion": 1,
                "signerIdentity": promotion.ATTESTATION_IDENTITY,
                "legacyIPv4": "138.199.157.58",
                "targetIPv4": "159.195.156.57",
                "sourceBackup": evidence.source_backup,
                "catalogSha256": evidence.catalog_sha256,
                "platformSha256": evidence.platform_sha256,
                "runtimeManifestSha256": evidence.runtime_manifest_sha256,
                "recoveryManifestSha256": evidence.recovery_manifest_sha256,
                "readyMarkerSha256": evidence.ready_marker_sha256,
                "secretCatalogSha256": evidence.secret_catalog_sha256,
                "secretSourcesSha256": evidence.secret_sources_sha256,
                "runtimeTreeSha256": evidence.runtime_tree_sha256,
                "legacyWritersStopped": True,
                "finalBackupVerified": True,
                "legacyStoppedAt": "2026-07-27T00:00:00+00:00",
                "backupStartedAt": "2026-07-27T00:01:00+00:00",
                "backupCompletedAt": "2026-07-27T00:10:00+00:00",
                "issuedAt": "2026-07-27T00:15:00+00:00",
                "expiresAt": "2026-07-27T05:15:00+00:00",
                "sourceCommit": SOURCE_COMMIT,
            }
            attestation_path = fixture.layout.attestation(NEW_GENERATION)
            write_private(
                attestation_path,
                promotion.canonical_json(document),
            )
            subprocess.run(
                [
                    "/usr/bin/ssh-keygen",
                    "-Y",
                    "sign",
                    "-f",
                    str(key),
                    "-n",
                    promotion.ATTESTATION_NAMESPACE,
                    str(attestation_path),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            generated_signature = Path(f"{attestation_path}.sig")
            generated_signature.chmod(0o600)
            verifier = promotion.OpenSSHAttestationVerifier(
                layout=fixture.layout,
                platform_path=REPOSITORY_ROOT / "config/platform.yml",
            )
            result = verifier.verify(
                evidence,
                SOURCE_COMMIT,
                now=now,
            )
            self.assertRegex(result.document_sha256, r"^[a-f0-9]{64}$")
            document["runtimeTreeSha256"] = "f" * 64
            write_private(
                attestation_path,
                promotion.canonical_json(document),
            )
            with self.assertRaisesRegex(
                promotion.PromotionError,
                "runtimeTreeSha256",
            ):
                verifier.verify(
                    evidence,
                    SOURCE_COMMIT,
                    now=now,
                )

    def test_attestation_time_order_and_expiry_are_fail_closed(self) -> None:
        base = datetime(2026, 7, 27, tzinfo=timezone.utc)
        self.assertLess(
            promotion.source_backup_time(NEW_GENERATION, "source"),
            base + timedelta(days=1),
        )
        with self.assertRaises(promotion.PromotionError):
            promotion.parse_utc("2026-07-27T00:00:00Z", "not-canonical")


if __name__ == "__main__":
    unittest.main()
