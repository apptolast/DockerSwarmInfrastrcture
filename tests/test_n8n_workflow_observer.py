"""Offline synthetic regression tests: no production Docker/DB or workflows."""

from contextlib import contextmanager
import copy
import errno
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import pwd
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "observe-n8n-workflows.py"
SPEC = importlib.util.spec_from_file_location("n8n_observer", SCRIPT)
OBSERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OBSERVER)
NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def baseline():
    return {"schema": 1, "scope": "n8n_workflow_state", "provenance": {
        "observed_at": "2020-01-01T00:00:00Z", "owner_confirmed_on": "2020-01-01",
        "active_basis": "observed_current", "retirement_basis": "owner_confirmed"},
        "expected_active": [{"id": "wf_A", "activeVersionId": "version_A"}],
        "retired_inactive": ["wf_R"], "retired_absent": ["wf_X"]}


def row(identifier, active=False, archived=False, version=None):
    return {"id": identifier, "active": active, "isArchived": archived, "activeVersionId": version}


def snapshot(now=NOW):
    executions = {key: 0 for key in OBSERVER.STATUSES}
    executions.update(total=0, unrecognized=0)
    return {"snapshot_at": OBSERVER.utc_string(now), "executions": executions,
            "workflows": [row("wf_A", True, version="version_A"),
                          row("wf_R", archived=True), row("wf_D")]}


@contextmanager
def private_directory():
    # Do not teach the production reader to trust world-writable /tmp.
    with tempfile.TemporaryDirectory(prefix=".n8n-observer-test-",
                                     dir=pwd.getpwuid(os.geteuid()).pw_dir) as directory:
        os.chmod(directory, 0o700)
        yield Path(directory)


def save_private(path, value=None):
    path.write_bytes(json.dumps(value if value is not None else baseline()).encode("ascii"))
    path.chmod(0o600)


class MetadataTests(unittest.TestCase):
    def error(self, reason, function, *args, **kwargs):
        with self.assertRaises(OBSERVER.ObserverError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.reason, reason)

    def report(self, data):
        return OBSERVER.report_for(OBSERVER.parse_baseline(baseline(), NOW),
                                   OBSERVER.validate_snapshot(data, NOW))

    def test_retired_archives_and_unlisted_drafts_are_intentional(self):
        for archived in (True, False):
            with self.subTest(archived=archived):
                data = snapshot()
                data["workflows"][1]["isArchived"] = archived
                report = self.report(data)
                self.assertEqual((report["status"], report["reason"]), ("ok", "observed"))
                self.assertEqual(report["counts"]["observed_inactive"], 2)
                self.assertEqual(report["counts"]["retired_reactivated"], 0)

    def test_same_active_count_replacement_is_detected_by_id(self):
        data = snapshot()
        data["workflows"][0]["id"] = "wf_B"
        report = self.report(data)
        self.assertEqual(report["status"], "drift")
        self.assertEqual(report["counts"]["observed_active"], 1)
        self.assertEqual(report["counts"]["missing_expected"], 1)
        self.assertEqual(report["counts"]["new_active"], 1)

    def test_published_version_change_requires_manual_baseline_review(self):
        data = snapshot()
        data["workflows"][0]["activeVersionId"] = "version_B"
        report = self.report(data)
        self.assertEqual(report["status"], "drift")
        self.assertEqual(report["counts"]["version_drift"], 1)

    def test_stopped_missing_and_retired_reappearance_are_drift(self):
        cases = []
        stopped = snapshot()
        stopped["workflows"][0] = row("wf_A")
        cases.append((stopped, "stopped_expected"))
        missing = snapshot()
        missing["workflows"].pop(1)
        cases.append((missing, "retired_missing"))
        reappeared = snapshot()
        reappeared["workflows"].append(row("wf_X"))
        cases.append((reappeared, "confirmed_absent_reappeared"))
        reactivated = snapshot()
        reactivated["workflows"][1] = row("wf_R", True, version="version_R")
        cases.append((reactivated, "retired_reactivated"))
        for data, count in cases:
            with self.subTest(count=count):
                report = self.report(data)
                self.assertEqual(report["status"], "drift")
                self.assertEqual(report["counts"][count], 1)

    def test_publication_disagreement_is_valid_metadata_but_never_green(self):
        for state in (row("wf_R", version="version_R"),
                      row("wf_R", archived=True, version="version_R"),
                      row("wf_A", True),
                      row("wf_A", True, True, "version_A")):
            with self.subTest(state=state):
                data = snapshot()
                index = 0 if state["id"] == "wf_A" else 1
                data["workflows"][index] = state
                report = self.report(data)
                self.assertEqual(report["status"], "unknown")
                self.assertEqual(report["reason"], "workflow_publication_mismatch")
                self.assertEqual(report["counts"]["publication_mismatch"], 1)

    def test_status_taxonomy_does_not_invent_canceled_or_unknown_errors(self):
        for status in (*OBSERVER.STATUSES, "unrecognized"):
            with self.subTest(status=status):
                data = snapshot()
                data["executions"][status] = data["executions"]["total"] = 1
                report = self.report(data)
                expected = "error" if status in ("error", "crashed") else (
                    "unknown" if status in ("unknown", "unrecognized") else "ok")
                self.assertEqual(report["status"], expected)
                self.assertEqual(report["counts"]["execution_errors"], int(expected == "error"))

    def test_strict_schema_rejects_extra_duplicate_boolean_and_ambiguous_values(self):
        cases = []
        extra = baseline(); extra["secret"] = "not_printed"; cases.append(extra)
        boolean = baseline(); boolean["schema"] = True; cases.append(boolean)
        floating = baseline(); floating["schema"] = 1.0; cases.append(floating)
        duplicate = baseline(); duplicate["retired_inactive"] *= 2; cases.append(duplicate)
        intersect = baseline(); intersect["retired_absent"] = ["wf_A"]; cases.append(intersect)
        bad_id = baseline(); bad_id["expected_active"][0]["id"] = "id with space"; cases.append(bad_id)
        extra_row = baseline(); extra_row["expected_active"][0]["name"] = "private"; cases.append(extra_row)
        future = baseline(); future["provenance"]["observed_at"] = OBSERVER.utc_string(NOW + timedelta(seconds=2.000001)); cases.append(future)
        for value in cases:
            with self.subTest(value=value):
                self.error("baseline_invalid", OBSERVER.parse_baseline, value, NOW)
        for raw in (b'{"schema":1,"schema":1}', b'{"x":NaN}', b'{"x":Infinity}',
                    b'{} {}', b'"\xff"', b'[' * 2000 + b']' * 2000):
            with self.subTest(raw=raw[:20]):
                self.error("baseline_invalid", OBSERVER.strict_json, raw, "baseline_invalid")

    def test_baseline_is_historical_and_owner_date_uses_madrid(self):
        self.assertEqual(OBSERVER.parse_baseline(baseline(), NOW)["provenance"]["observed_at"],
                         "2020-01-01T00:00:00Z")
        value = baseline()
        value["provenance"]["owner_confirmed_on"] = "2026-10-06"
        OBSERVER.parse_baseline(value, datetime(2026, 10, 5, 23, tzinfo=timezone.utc))
        self.error("baseline_invalid", OBSERVER.parse_baseline, value,
                   datetime(2026, 10, 5, 20, tzinfo=timezone.utc))

    def test_snapshot_clock_bounds_and_counter_types(self):
        for age in (-2, 10.25):
            data = snapshot(NOW - timedelta(seconds=age))
            OBSERVER.validate_snapshot(data, NOW)
        for age in (-2.000001, 10.250001):
            self.error("clock_inconsistent", OBSERVER.validate_snapshot,
                       snapshot(NOW - timedelta(seconds=age)), NOW)
        for count in (True, 1.0, -1, OBSERVER.MAX_COUNT + 1):
            data = snapshot(); data["executions"]["total"] = count
            self.error("metadata_invalid", OBSERVER.validate_snapshot, data, NOW)
        data = snapshot(); data["executions"]["success"] = 1
        self.error("metadata_invalid", OBSERVER.validate_snapshot, data, NOW)

    def test_snapshot_rows_are_bounded_closed_unique_and_typed(self):
        cases = []
        data = snapshot(); data["workflows"] *= 2; cases.append(data)
        data = snapshot(); data["workflows"][0]["active"] = 1; cases.append(data)
        data = snapshot(); data["workflows"][0]["activeVersionId"] = "bad/version"; cases.append(data)
        data = snapshot(); data["workflows"][0]["nodes"] = []; cases.append(data)
        data = snapshot(); data["workflows"] = [row("wf_%04d" % n) for n in range(1001)]; cases.append(data)
        for data in cases:
            self.error("metadata_invalid", OBSERVER.validate_snapshot, data, NOW)

    def test_baseline_scope_provenance_date_and_list_boundaries(self):
        cases = []
        for field, value in (("scope", "other_scope"),):
            data = baseline(); data[field] = value; cases.append(data)
        for field, value in (("active_basis", "owner_confirmed"), ("retirement_basis", "observed"),
                             ("owner_confirmed_on", None), ("owner_confirmed_on", "20261006"),
                             ("owner_confirmed_on", "2026-02-30"), ("owner_confirmed_on", "2026-10-07")):
            data = baseline(); data["provenance"][field] = value; cases.append(data)
        for field in ("expected_active", "retired_inactive", "retired_absent"):
            data = baseline(); data[field] = None; cases.append(data)
            data = baseline()
            ids = ["fixture_%04d" % n for n in range(1001)]
            data[field] = ([{"id": item, "activeVersionId": "fixture_version"} for item in ids]
                           if field == "expected_active" else ids)
            cases.append(data)
        data = baseline(); data["expected_active"] = []
        data["retired_inactive"] = ["retired_%04d" % n for n in range(500)]
        data["retired_absent"] = ["absent_%04d" % n for n in range(501)]; cases.append(data)
        data = baseline(); del data["provenance"]["retirement_basis"]; cases.append(data)
        for number, data in enumerate(cases):
            with self.subTest(case=number):
                self.error("baseline_invalid", OBSERVER.parse_baseline, data, NOW)

    def test_snapshot_object_keys_and_archived_type_are_closed(self):
        cases = []
        data = snapshot(); data["private_extra"] = "private"; cases.append(data)
        data = snapshot(); del data["snapshot_at"]; cases.append(data)
        data = snapshot(); data["executions"]["private_extra"] = 1; cases.append(data)
        data = snapshot(); del data["executions"]["waiting"]; cases.append(data)
        data = snapshot(); data["workflows"][0]["isArchived"] = 0; cases.append(data)
        for data in cases:
            self.error("metadata_invalid", OBSERVER.validate_snapshot, data, NOW)

    def test_json_input_size_type_depth_and_quoted_braces(self):
        for raw in ("{}", bytearray(b"{}"), b" " * (OBSERVER.MAX_BYTES + 1)):
            self.error("baseline_invalid", OBSERVER.strict_json, raw, "baseline_invalid")
        raw = json.dumps({"literal": '[' * 1000 + '"\\' + ']' * 1000}).encode()
        self.assertEqual(OBSERVER.strict_json(raw, "baseline_invalid")["literal"],
                         '[' * 1000 + '"\\' + ']' * 1000)

    def test_report_is_closed_and_has_no_private_ids_versions_or_raw_errors(self):
        report = self.report(snapshot())
        self.assertEqual(set(report), {"schema", "scope", "status", "reason", "observed_at", "checked_at", "counts"})
        raw = json.dumps(report)
        for private in ("wf_A", "wf_R", "wf_X", "version_A", "POSTGRES", "/srv/"):
            self.assertNotIn(private, raw)
        unknown = OBSERVER.unknown_report("PRIVATE_TOKEN_AND_ERROR")
        self.assertIsNone(unknown["counts"])
        self.assertIsNone(unknown["observed_at"])
        self.assertEqual(unknown["reason"], "internal_error")


class BaselineFileTests(unittest.TestCase):
    def open(self, path):
        return OBSERVER.private_baseline(str(path), owner_uid=os.geteuid(), owner_gid=os.getegid())

    def refused(self, path):
        with self.assertRaises(OBSERVER.ObserverError) as caught:
            with self.open(path):
                self.fail("unsafe baseline was yielded")
        self.assertEqual(caught.exception.reason, "baseline_unsafe")

    def test_private_regular_file_and_retained_content(self):
        with private_directory() as directory:
            path = directory / "baseline.json"; save_private(path)
            with self.open(path) as value:
                self.assertEqual(value, baseline())
            with self.assertRaises(OBSERVER.ObserverError):
                with self.open(path):
                    path.write_bytes(b'{}')

    def test_symlinks_hardlinks_fifo_and_untrusted_parent_modes(self):
        with private_directory() as directory:
            path = directory / "baseline.json"; save_private(path)
            link = directory / "symlink"; link.symlink_to(path); self.refused(link)
            hard = directory / "hardlink"; os.link(path, hard); self.refused(path); hard.unlink()
            fifo = directory / "fifo"; os.mkfifo(fifo, 0o600); self.refused(fifo)
            for mode in (0o640, 0o644, 0o660):
                path.chmod(mode); self.refused(path)
            path.chmod(0o600)
            directory.chmod(0o770)
            try:
                self.refused(path)
            finally:
                directory.chmod(0o700)
            child = directory / "child"; child.mkdir(mode=0o700)
            save_private(child / "baseline.json")
            alias = directory / "alias"; alias.symlink_to(child, target_is_directory=True)
            self.refused(alias / "baseline.json")

    def test_owner_gid_link_count_and_oversized_file_fail_closed(self):
        with private_directory() as directory:
            path = directory / "baseline.json"; save_private(path)
            original = OBSERVER.os.fstat
            for index in (3, 4, 5):
                def changed(fd, index=index):
                    state = original(fd)
                    if stat.S_ISREG(state.st_mode):
                        fields = list(state); fields[index] += 1
                        return os.stat_result(fields)
                    return state
                with self.subTest(index=index), mock.patch.object(OBSERVER.os, "fstat", changed):
                    self.refused(path)
            path.write_bytes(b' ' * (OBSERVER.MAX_BYTES + 1)); self.refused(path)

    def test_leaf_or_ancestor_replacement_during_observation_is_rejected(self):
        with private_directory() as directory:
            child = directory / "child"; child.mkdir(mode=0o700)
            path = child / "baseline.json"; save_private(path)
            with self.assertRaises(OBSERVER.ObserverError):
                with self.open(path):
                    replacement = child / "replacement"; save_private(replacement)
                    replacement.replace(path)
            with self.assertRaises(OBSERVER.ObserverError):
                with self.open(path):
                    child.rename(directory / "moved")

    def test_relative_traversal_and_world_writable_tmp_are_not_exceptions(self):
        for path in ("relative.json", "/tmp/file.json", "/srv/../file", "/srv//file"):
            self.refused(path)

    def test_root_directory_must_still_be_safe_after_observation(self):
        with private_directory() as directory:
            path = directory / "baseline.json"; save_private(path)
            original = OBSERVER.os.fstat
            altered = [False]
            def changed(fd):
                state = original(fd)
                if altered[0] and os.readlink("/proc/self/fd/" + str(fd)) == "/":
                    fields = list(state); fields[0] |= 0o022
                    return os.stat_result(fields)
                return state
            with mock.patch.object(OBSERVER.os, "fstat", changed), self.assertRaises(OBSERVER.ObserverError) as caught:
                with self.open(path): altered[0] = True
            self.assertEqual(caught.exception.reason, "baseline_unsafe")

    def test_read_failure_is_sanitized(self):
        with private_directory() as directory:
            path = directory / "baseline.json"; save_private(path)
            with mock.patch.object(OBSERVER.os, "read", side_effect=OSError("private_path_or_secret")), self.assertRaises(OBSERVER.ObserverError) as caught:
                with self.open(path): self.fail("failed read was accepted")
            self.assertEqual(str(caught.exception), "baseline_unsafe")


class FakeDocker:
    def __init__(self):
        self.cid = "a" * 64
        self.service = {"id": "s" * 25, "name": OBSERVER.SERVICE,
                        "image": "pgvector/pgvector@sha256:" + "b" * 64}
        self.container = {"id": self.cid, "running": True, "image_id": "sha256:" + "c" * 64,
                          "image": self.service["image"], "service": OBSERVER.SERVICE,
                          "service_id": self.service["id"], "task_id": "t" * 25}
        self.task = {"id": "t" * 25, "service_id": self.service["id"], "desired": "running",
                     "state": "running", "container_id": self.cid, "image": self.service["image"]}
        self.data = snapshot(OBSERVER.utc_now())
        self.calls = []
        self.selection = (self.cid + "\n").encode()
        self.fail_exec = False
        self.final_image_change = False

    def __call__(self, argv, budget, *, input_data=b"", failure="docker_read_failed"):
        self.calls.append((argv, input_data, failure))
        budget.remaining()
        args = argv[4:]
        if args[:2] == ["service", "inspect"]:
            value = self.service
        elif args[:2] == ["container", "ls"]:
            budget.consume(len(self.selection)); return self.selection
        elif args[:2] == ["container", "inspect"]:
            value = copy.deepcopy(self.container)
            if self.final_image_change and len(self.calls) > 5:
                value["image_id"] = "sha256:" + "d" * 64
        elif args[:3] == ["inspect", "--type", "task"]:
            value = self.task
        elif args[0] == "exec":
            if self.fail_exec:
                raise OBSERVER.ObserverError(failure)
            value = self.data
        else:
            raise AssertionError("unexpected command")
        raw = json.dumps(value).encode("ascii")
        budget.consume(len(raw)); return raw


class CollectorTests(unittest.TestCase):
    def collect(self, fake, budget=None):
        with private_directory() as directory:
            return OBSERVER.collect_live(budget or OBSERVER.Budget(), _runner=fake,
                                         _config_parent=str(directory))

    def test_fixed_read_only_sql_and_docker_identity_chain(self):
        fake = FakeDocker()
        data = self.collect(fake)
        self.assertEqual(len(data["workflows"]), 3)
        self.assertEqual(len(fake.calls), 9)
        for argv, _stdin, _failure in fake.calls:
            self.assertEqual(argv[0:3], ["/usr/bin/docker", "--host=unix:///run/docker.sock", "--config"])
            self.assertNotIn("version_A", " ".join(argv))
        exec_call = fake.calls[4]
        self.assertEqual(exec_call[1], OBSERVER.SQL)
        self.assertIn("READ ONLY", exec_call[1].decode())
        self.assertIn("REPEATABLE READ", exec_call[1].decode())
        self.assertIn('AND "startedAt" <= transaction_timestamp()', exec_call[1].decode())
        for forbidden in ("execution_data", "credentials", "nodes", "UPDATE ", "DELETE ", "INSERT "):
            self.assertNotIn(forbidden, exec_call[1].decode())
        self.assertIn("public.workflow_entity", exec_call[1].decode())
        self.assertIn("public.execution_entity", exec_call[1].decode())
        shell = exec_call[0][-1]
        for required in ("env -i", "--host=/var/run/postgresql", "--port=5432", "--no-password",
                         "-X", "--no-psqlrc", "PGCONNECT_TIMEOUT=2", "PGPASSFILE=/dev/null",
                         "[!A-Za-z0-9_]*", "-le 63"):
            self.assertIn(required, shell)

    def test_service_task_container_and_image_disagreements_fail_closed(self):
        mutations = (("service", "name", "other"), ("container", "service_id", "x" * 25),
                     ("container", "running", False), ("container", "running", 1),
                     ("task", "service_id", "x" * 25), ("task", "container_id", "e" * 64),
                     ("task", "desired", "shutdown"), ("task", "state", "failed"),
                     ("task", "image", "pgvector/pgvector@sha256:" + "e" * 64),
                     ("service", "image", "pgvector/pgvector:latest"))
        for collection, key, value in mutations:
            with self.subTest(collection=collection, key=key):
                fake = FakeDocker(); getattr(fake, collection)[key] = value
                with self.assertRaises(OBSERVER.ObserverError):
                    self.collect(fake)
                self.assertFalse(any(call[0][4] == "exec" for call in fake.calls))

    def test_zero_multiple_or_changed_container_and_failed_database_are_unknown(self):
        for selection in (b"", b"a\n", ("a" * 64 + "\n" + "b" * 64 + "\n").encode()):
            fake = FakeDocker(); fake.selection = selection
            with self.assertRaises(OBSERVER.ObserverError): self.collect(fake)
        fake = FakeDocker(); fake.final_image_change = True
        with self.assertRaises(OBSERVER.ObserverError) as caught: self.collect(fake)
        self.assertEqual(caught.exception.reason, "runtime_identity_changed")
        fake = FakeDocker(); fake.fail_exec = True
        with self.assertRaises(OBSERVER.ObserverError) as caught: self.collect(fake)
        self.assertEqual(caught.exception.reason, "database_read_failed")

    def test_common_budget_covers_all_commands_and_total_output(self):
        fake = FakeDocker(); now = [0.0]; budget = OBSERVER.Budget(clock=lambda: now[0])
        def slow(*args, **kwargs):
            now[0] += 2.1
            return fake(*args, **kwargs)
        with self.assertRaises(OBSERVER.ObserverError) as caught: self.collect(slow, budget)
        self.assertEqual(caught.exception.reason, "observation_deadline")
        self.assertLess(len(fake.calls), 9)
        budget = OBSERVER.Budget(); budget.consume(OBSERVER.MAX_BYTES)
        with self.assertRaises(OBSERVER.ObserverError) as caught: self.collect(FakeDocker(), budget)
        self.assertEqual(caught.exception.reason, "observation_too_large")

    def test_actual_shell_identifier_guard_refuses_conninfo_and_invalid_values(self):
        # Run the production prefix only, stopping before any DB command.
        # A sentinel after it detects a missing guard; no psql/Docker runs.
        prefix = OBSERVER.PSQL_SCRIPT.split("exec /usr/bin/env -i", 1)[0]
        script = prefix + "printf 'fixture_guard_passed\\n'\n"
        invalid = ("", "postgres://remote/db", "host=remote", "-database", "db-name",
                   "db;echo injected", "db\nname", "p" * 64, "é", "1database")
        for key in ("POSTGRES_USER", "POSTGRES_DB"):
            for value in invalid:
                with self.subTest(key=key, value=value):
                    environment = {"PATH": "/usr/bin:/bin", "POSTGRES_USER": "postgres",
                                   "POSTGRES_DB": "n8n", key: value}
                    result = subprocess.run(["/bin/sh", "-c", script], env=environment,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            timeout=1, check=False)
                    self.assertEqual(result.returncode, 64)
                    self.assertEqual((result.stdout, result.stderr), (b"", b""))
        for valid in ("postgres", "_n8n_db", "p" * 63):
            result = subprocess.run(["/bin/sh", "-c", script],
                                    env={"PATH": "/usr/bin:/bin", "POSTGRES_USER": valid,
                                         "POSTGRES_DB": valid},
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=1, check=False)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"fixture_guard_passed\n")

    def test_identity_extra_null_and_wrong_types_are_not_accepted(self):
        for field, change in (("service", {"extra": "private"}),
                              ("service", {"id": None}),
                              ("task", {"state": True}),
                              ("container", {"image_id": "not_a_digest"})):
            fake = FakeDocker(); getattr(fake, field).update(change)
            with self.subTest(field=field, change=change), self.assertRaises(OBSERVER.ObserverError):
                self.collect(fake)

    def test_final_selector_extra_bytes_and_malformed_task_id_are_refused(self):
        fake = FakeDocker()
        def changed(*args, **kwargs):
            if len(fake.calls) == 7:
                fake.selection += b"private_extra"
            return fake(*args, **kwargs)
        with self.assertRaises(OBSERVER.ObserverError) as caught: self.collect(changed)
        self.assertEqual(caught.exception.reason, "runtime_identity_ambiguous")
        fake = FakeDocker(); fake.container["task_id"] = "--unsafe-task"
        with self.assertRaises(OBSERVER.ObserverError) as caught: self.collect(fake)
        self.assertEqual(caught.exception.reason, "runtime_identity_ambiguous")
        self.assertEqual(len(fake.calls), 3)


class OwnedProcessTests(unittest.TestCase):
    def python(self, code, seconds=1, input_data=b""):
        return OBSERVER.run_bounded([sys.executable, "-c", code], OBSERVER.Budget(seconds),
                                    input_data=input_data)

    def test_clean_environment_and_success_nonzero_stderr_flood(self):
        with mock.patch.dict(os.environ, {"DOCKER_HOST": "private_host", "DOCKER_CONTEXT": "private_context",
                                          "DOCKER_CONFIG": "private_config", "PGPASSWORD": "private_password",
                                          "GH_TOKEN": "private_token"}):
            result = json.loads(self.python("import os,json; print(json.dumps(dict(os.environ)))"))
        self.assertEqual(result["PATH"], "/usr/bin:/bin")
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "PGPASSWORD", "GH_TOKEN", "HOME"):
            self.assertNotIn(key, result)
        self.assertEqual(self.python("import os; os.write(2,b'x'*200000); print('safe')"), b"safe\n")
        with self.assertRaises(OBSERVER.ObserverError) as caught:
            self.python("import sys;sys.exit(42)")
        self.assertEqual(caught.exception.reason, "docker_read_failed")

    def test_stdout_flood_timeout_closed_pipes_and_blocked_stdin_are_bounded(self):
        cases = (("import os;os.write(1,b'x'*100000)", b"", "observation_too_large"),
                 ("import time;time.sleep(60)", b"", "observation_deadline"),
                 ("import os,time;os.close(1);time.sleep(60)", b"", "observation_deadline"),
                 ("import time;time.sleep(60)", b"x" * 65536, "observation_deadline"))
        for code, stdin, reason in cases:
            with self.subTest(reason=reason, stdin=len(stdin)):
                started = time.monotonic()
                with self.assertRaises(OBSERVER.ObserverError) as caught:
                    self.python(code, seconds=0.15, input_data=stdin)
                self.assertEqual(caught.exception.reason, reason)
                self.assertLess(time.monotonic() - started, 0.65)

    def test_exited_leader_cannot_leave_an_owned_helper_holding_pipes(self):
        code = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);print(p.pid,flush=True)"
        pid = int(self.python(code, seconds=0.5))
        self.assert_not_live(pid)

    def assert_not_live(self, pid):
        path = Path("/proc") / str(pid) / "stat"
        deadline = time.monotonic() + 0.25
        while True:
            try:
                content = path.read_text()
            except (FileNotFoundError, ProcessLookupError):
                # A dead /proc FD can fail with ESRCH after open succeeds.
                return
            state = content.rsplit(")", 1)[1].split()[0]
            if state == "Z":
                return
            if time.monotonic() >= deadline:
                self.fail("owned synthetic helper did not terminate within cleanup grace")
            time.sleep(0.005)

    def test_not_live_probe_accepts_missing_path_and_disappearance_during_read(self):
        with mock.patch.object(Path, "open", side_effect=FileNotFoundError(errno.ENOENT, "synthetic missing process")):
            self.assert_not_live(123456789)
        opened = mock.MagicMock()
        stream = opened.__enter__.return_value
        stream.read.side_effect = ProcessLookupError(errno.ESRCH, "synthetic process disappeared during read")
        with mock.patch.object(Path, "open", return_value=opened) as opening:
            self.assert_not_live(123456789)
        opening.assert_called_once()
        stream.read.assert_called_once_with()
        opened.__exit__.assert_called_once()

    def test_not_live_probe_keeps_permission_and_unexpected_io_errors_fatal(self):
        for error in (PermissionError(errno.EACCES, "synthetic permission denied"),
                      OSError(errno.EIO, "synthetic read failure")):
            with self.subTest(errno=error.errno), mock.patch.object(Path, "read_text", side_effect=error):
                with self.assertRaises(type(error)) as caught:
                    self.assert_not_live(123456789)
                self.assertIs(caught.exception, error)

    def test_not_live_probe_keeps_zombie_and_live_deadline_rules(self):
        with mock.patch.object(Path, "read_text", return_value="123 (synthetic helper) Z"), mock.patch.object(time, "sleep") as sleeping:
            self.assert_not_live(123456789)
            sleeping.assert_not_called()
        with mock.patch.object(Path, "read_text", return_value="123 (synthetic helper) S"), mock.patch.object(time, "monotonic", side_effect=(0.0, 0.25)), mock.patch.object(time, "sleep") as sleeping:
            with self.assertRaisesRegex(AssertionError, "owned synthetic helper did not terminate"):
                self.assert_not_live(123456789)
            sleeping.assert_not_called()

    def test_selector_failure_happens_before_spawn_and_signal_mask_changes(self):
        before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        with mock.patch.object(OBSERVER.selectors, "DefaultSelector", side_effect=OSError("synthetic FD limit")), mock.patch.object(OBSERVER.subprocess, "Popen") as spawned:
            with self.assertRaises(OSError): self.python("print('unused')")
            spawned.assert_not_called()
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), before)

    def test_kill_precedes_reap_and_echild_never_signals_guessed_pid(self):
        events = []
        original_kill = OBSERVER.os.killpg
        original_wait = OBSERVER.subprocess.Popen.wait
        def kill(*args): events.append("kill"); return original_kill(*args)
        def wait(process, *args, **kwargs): events.append("wait"); return original_wait(process, *args, **kwargs)
        with mock.patch.object(OBSERVER.os, "killpg", kill), mock.patch.object(OBSERVER.subprocess.Popen, "wait", wait):
            self.python("print('done')")
        self.assertLess(events.index("kill"), events.index("wait"))
        process = mock.Mock(pid=123456789)
        with mock.patch.object(OBSERVER.os, "waitid", side_effect=ChildProcessError), mock.patch.object(OBSERVER.os, "killpg") as signaled:
            with self.assertRaises(OBSERVER.ObserverError) as caught:
                OBSERVER.cleanup_owned(process, OBSERVER.Budget())
            self.assertEqual(caught.exception.reason, "cleanup_identity_lost")
            signaled.assert_not_called(); process.wait.assert_not_called()

    def test_nondefault_sigchld_is_refused_without_spawning(self):
        with mock.patch.object(OBSERVER.signal, "getsignal", return_value=signal.SIG_IGN), mock.patch.object(OBSERVER.subprocess, "Popen") as spawned:
            with self.assertRaises(OBSERVER.ObserverError) as caught: self.python("print('unused')")
            self.assertEqual(caught.exception.reason, "unsupported_runtime"); spawned.assert_not_called()

    def test_multithread_runtime_is_refused_without_spawning(self):
        with mock.patch.object(OBSERVER.threading, "active_count", return_value=2), mock.patch.object(OBSERVER.subprocess, "Popen") as spawned:
            with self.assertRaises(OBSERVER.ObserverError) as caught: self.python("print('unused')")
            self.assertEqual(caught.exception.reason, "unsupported_runtime")
            spawned.assert_not_called()

    def test_inconclusive_owned_cleanup_never_returns_ok(self):
        process = mock.Mock(pid=123456789)
        with mock.patch.object(OBSERVER.os, "waitid", return_value=None), mock.patch.object(OBSERVER.os, "killpg", side_effect=PermissionError):
            with self.assertRaises(OBSERVER.ObserverError) as caught:
                OBSERVER.cleanup_owned(process, OBSERVER.Budget())
            self.assertEqual(caught.exception.reason, "cleanup_inconclusive")
            process.wait.assert_not_called()
        process.wait.side_effect = subprocess.TimeoutExpired("synthetic", 0.25)
        with mock.patch.object(OBSERVER.os, "waitid", return_value=None), mock.patch.object(OBSERVER.os, "killpg"):
            with self.assertRaises(OBSERVER.ObserverError) as caught:
                OBSERVER.cleanup_owned(process, OBSERVER.Budget())
            self.assertEqual(caught.exception.reason, "cleanup_inconclusive")
            self.assertLessEqual(process.wait.call_args.kwargs["timeout"], 0.25)

    def test_missing_fixed_tool_closes_selector_and_restores_signal_mask(self):
        before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        selector = mock.Mock()
        with mock.patch.object(OBSERVER.selectors, "DefaultSelector", return_value=selector), mock.patch.object(OBSERVER.subprocess, "Popen", side_effect=FileNotFoundError("private_path")):
            with self.assertRaises(OBSERVER.ObserverError) as caught: self.python("print('unused')")
            self.assertEqual(caught.exception.reason, "docker_read_failed")
            self.assertNotIn("private_path", str(caught.exception))
            selector.close.assert_called_once()
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), before)

    def test_root_and_exact_cli_arguments_fail_before_collection(self):
        for uid, arguments, reason in ((1001, ["--baseline", "/private"], "root_required"),
                                       (0, [], "arguments_invalid"),
                                       (0, ["--baseline", "/private", "--force"], "arguments_invalid")):
            with self.subTest(uid=uid, arguments=arguments), mock.patch.object(OBSERVER.os, "geteuid", return_value=uid), mock.patch.object(OBSERVER, "collect_live") as collected, mock.patch.object(OBSERVER, "emit_report") as emitted:
                self.assertEqual(OBSERVER.main(arguments), 1)
                collected.assert_not_called()
                self.assertEqual(emitted.call_args.args[0]["reason"], reason)
                self.assertIsNone(emitted.call_args.args[0]["counts"])

    def test_blocked_stdout_deadline_and_zero_write_restore_flags(self):
        clock = [0.0]
        def blocked(_fd, _raw):
            clock[0] += 0.05
            raise BlockingIOError
        with mock.patch.object(OBSERVER.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(OBSERVER.time, "sleep", side_effect=lambda delta: clock.__setitem__(0, clock[0] + delta)), mock.patch.object(OBSERVER.fcntl, "fcntl", return_value=0) as flags, mock.patch.object(OBSERVER.os, "write", side_effect=blocked):
            with self.assertRaises(OBSERVER.ObserverError) as caught:
                OBSERVER.emit_report(OBSERVER.unknown_report("interrupted"),
                                     OBSERVER.Budget(clock=lambda: clock[0]))
            self.assertEqual(caught.exception.reason, "observation_deadline")
            self.assertLessEqual(clock[0], 0.31)
            self.assertEqual(flags.call_args.args, (1, OBSERVER.fcntl.F_SETFL, 0))
        with mock.patch.object(OBSERVER.fcntl, "fcntl", return_value=0) as flags, mock.patch.object(OBSERVER.os, "write", return_value=0):
            with self.assertRaises(OBSERVER.ObserverError) as caught:
                OBSERVER.emit_report(OBSERVER.unknown_report("interrupted"), OBSERVER.Budget())
            self.assertEqual(caught.exception.reason, "internal_error")
            self.assertEqual(flags.call_args.args, (1, OBSERVER.fcntl.F_SETFL, 0))

    def test_cli_publication_failure_cannot_return_success(self):
        @contextmanager
        def fixture(_path): yield baseline()
        with mock.patch.object(OBSERVER.os, "geteuid", return_value=0), mock.patch.object(OBSERVER, "private_baseline", fixture), mock.patch.object(OBSERVER, "collect_live", return_value=snapshot(OBSERVER.utc_now())), mock.patch.object(OBSERVER, "emit_report", side_effect=BrokenPipeError):
            self.assertEqual(OBSERVER.main(["--baseline", "/fixture"]), 1)

    def test_sigterm_unwinds_cleanup_in_a_disposable_cli(self):
        with private_directory() as directory:
            pid_file = directory / "child-pid"
            code = """import importlib.util,sys,subprocess,json,pathlib
s=importlib.util.spec_from_file_location('observer',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
original=m.subprocess.Popen
def spawn(*a,**k):
 p=original(*a,**k);pathlib.Path(sys.argv[2]).write_text(str(p.pid));return p
m.subprocess.Popen=spawn
with m.interruption_guard():
 try:m.run_bounded([sys.executable,'-c','import time;time.sleep(60)'],m.Budget(2))
 except m.ObserverError as e:print(json.dumps(m.unknown_report(e.reason)))
"""
            process = subprocess.Popen([sys.executable, "-c", code, str(SCRIPT), str(pid_file)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                deadline = time.monotonic() + 2
                while not pid_file.exists() and time.monotonic() < deadline: time.sleep(0.01)
                self.assertTrue(pid_file.exists())
                child = int(pid_file.read_text())
                process.terminate()  # only this disposable direct child, before any reap
                output, errors = process.communicate(timeout=2)
                self.assertEqual(errors, b"")
                self.assertEqual(json.loads(output)["reason"], "interrupted")
                self.assert_not_live(child)
            finally:
                if process.poll() is None:
                    process.kill(); process.communicate(timeout=1)

    def test_cli_failure_is_closed_json_without_raw_path_or_error(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--baseline", "/PRIVATE_NOT_A_BASELINE"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b"")
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "unknown")
        self.assertIsNone(report["counts"])
        self.assertNotIn(b"PRIVATE_NOT_A_BASELINE", result.stdout)


if __name__ == "__main__":
    unittest.main()
