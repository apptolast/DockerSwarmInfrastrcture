#!/usr/bin/env python3
"""Observe private desired n8n metadata; never activate, learn or publish it.

Linux, root, single thread, SIGCHLD default, no external child reaper required.
The host I/O budget is 10 seconds with at most 0.25 seconds extra cleanup.
Kernel/spawn stalls and SIGKILL cannot be made into absolute timing guarantees.
Killing Docker CLI does not prove immediate termination of remote psql:
PGCONNECT_TIMEOUT and statement_timeout bound different remote phases only.
"""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import fcntl
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from zoneinfo import ZoneInfo

SCOPE = "n8n_workflow_metadata"
MAX_BYTES = 65536
MAX_ROWS = 1000
MAX_COUNT = 1_000_000_000
OBSERVATION_SECONDS = 10.0
CLEANUP_SECONDS = 0.25
FUTURE_SECONDS = 2.0
DOCKER = "/usr/bin/docker"
SERVICE = "workloads_n8n-db"
STATUSES = ("canceled", "crashed", "error", "new", "running", "success",
            "unknown", "waiting")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
SWARM_ID = re.compile(r"[a-z0-9]{25}\Z")
CONTAINER_ID = re.compile(r"[a-f0-9]{64}\Z")
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")
IMAGE_REF = re.compile(r"[A-Za-z0-9._:/-]{1,400}@sha256:[a-f0-9]{64}\Z")
UTC_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
REASONS = frozenset({
    "observed", "workflow_drift", "workflow_publication_mismatch",
    "execution_errors", "execution_state_unknown", "execution_state_unrecognized",
    "root_required", "arguments_invalid", "baseline_unsafe", "baseline_invalid",
    "metadata_invalid", "database_read_failed", "docker_read_failed",
    "runtime_identity_ambiguous", "runtime_identity_changed", "observation_deadline",
    "observation_too_large", "cleanup_identity_lost", "cleanup_inconclusive",
    "unsupported_runtime", "interrupted", "clock_inconsistent", "internal_error",
})

# Values expand only inside the verified container; no DB DSN or credentials
# enter the host argv/environment. C locale closes the identifier character set.
PSQL_SCRIPT = """LC_ALL=C; export LC_ALL
case "$POSTGRES_USER" in ''|[!A-Za-z_]*|*[!A-Za-z0-9_]*) exit 64;; esac
case "$POSTGRES_DB" in ''|[!A-Za-z_]*|*[!A-Za-z0-9_]*) exit 64;; esac
[ "${#POSTGRES_USER}" -le 63 ] && [ "${#POSTGRES_DB}" -le 63 ] || exit 64
exec /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin LC_ALL=C \
PGCONNECT_TIMEOUT=2 PGPASSFILE=/dev/null \
psql -X --no-psqlrc --no-password --host=/var/run/postgresql --port=5432 \
-v ON_ERROR_STOP=1 -qAt -U "$POSTGRES_USER" -d "$POSTGRES_DB"
"""
SQL = ("""BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '3s';
SET LOCAL lock_timeout = '500ms';
SET LOCAL search_path = pg_catalog;
SELECT json_build_object(
 'snapshot_at', to_char(transaction_timestamp() AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
 'workflows', (SELECT coalesce(json_agg(json_build_object(
  'id', id, 'active', active, 'isArchived', "isArchived",
  'activeVersionId', "activeVersionId")), '[]'::json)
  FROM (SELECT id, active, "isArchived", "activeVersionId"
        FROM public.workflow_entity ORDER BY id LIMIT 1001) AS bounded),
 'executions', (SELECT json_build_object('total', count(*),
""" + ",\n".join(
    " '%s', count(*) FILTER (WHERE status = '%s')" % (item, item)
    for item in STATUSES
) + """,
 'unrecognized', count(*) FILTER (WHERE status IS NULL OR status NOT IN
 ('canceled','crashed','error','new','running','success','unknown','waiting')))
 FROM public.execution_entity
 WHERE "startedAt" >= transaction_timestamp() - interval '24 hours'
   AND "startedAt" <= transaction_timestamp()));
COMMIT;
""").encode("ascii")

SERVICE_FORMAT = ('{"id":{{json .ID}},"name":{{json .Spec.Name}},'
                  '"image":{{json .Spec.TaskTemplate.ContainerSpec.Image}}}')
CONTAINER_FORMAT = ('{"id":{{json .Id}},"running":{{json .State.Running}},'
                    '"image_id":{{json .Image}},"image":{{json .Config.Image}},'
                    '"service":{{json (index .Config.Labels "com.docker.swarm.service.name")}},'
                    '"service_id":{{json (index .Config.Labels "com.docker.swarm.service.id")}},'
                    '"task_id":{{json (index .Config.Labels "com.docker.swarm.task.id")}}}')
TASK_FORMAT = ('{"id":{{json .ID}},"service_id":{{json .ServiceID}},'
               '"desired":{{json .DesiredState}},"state":{{json .Status.State}},'
               '"container_id":{{json .Status.ContainerStatus.ContainerID}},'
               '"image":{{json .Spec.ContainerSpec.Image}}}')


class ObserverError(Exception):
    def __init__(self, reason):
        self.reason = reason if reason in REASONS else "internal_error"
        super().__init__(self.reason)


def require(condition, reason):
    if not condition:
        raise ObserverError(reason)


def utc_now():
    return datetime.now(timezone.utc)


def utc_string(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def utc_parse(value, reason):
    require(type(value) is str and UTC_TEXT.fullmatch(value), reason)
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ObserverError(reason) from None


def closed_object(value, keys, reason):
    require(type(value) is dict and set(value) == set(keys), reason)


def strict_json(raw, reason):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, reason)
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ObserverError(reason)

    require(type(raw) is bytes and len(raw) <= MAX_BYTES, reason)
    # Reject pathological nesting before asking the decoder to allocate it.
    # Quotes/escaped quotes shield literal braces; json.loads checks syntax.
    depth = 0
    quoted = escaped = False
    for character in raw:
        if quoted:
            if escaped:
                escaped = False
            elif character == 92:
                escaped = True
            elif character == 34:
                quoted = False
        elif character == 34:
            quoted = True
        elif character in (91, 123):
            depth += 1
            require(depth <= 16, reason)
        elif character in (93, 125):
            depth -= 1
    try:
        return json.loads(raw.decode("ascii"), object_pairs_hook=pairs,
                          parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ObserverError(reason) from None


def identifier(value, reason):
    require(type(value) is str and IDENTIFIER.fullmatch(value), reason)


def parse_baseline(value, now=None):
    reason = "baseline_invalid"
    now = now or utc_now()
    closed_object(value, ("schema", "scope", "provenance", "expected_active",
                          "retired_inactive", "retired_absent"), reason)
    require(type(value["schema"]) is int and value["schema"] == 1, reason)
    require(value["scope"] == "n8n_workflow_state", reason)
    provenance = value["provenance"]
    closed_object(provenance, ("observed_at", "owner_confirmed_on", "active_basis",
                               "retirement_basis"), reason)
    require(provenance["active_basis"] == "observed_current" and
            provenance["retirement_basis"] == "owner_confirmed", reason)
    observed = utc_parse(provenance["observed_at"], reason)
    require(observed <= now + timedelta(seconds=FUTURE_SECONDS), reason)
    day = provenance["owner_confirmed_on"]
    require(type(day) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day), reason)
    try:
        require(date.fromisoformat(day) <= now.astimezone(ZoneInfo("Europe/Madrid")).date(), reason)
    except ValueError:
        raise ObserverError(reason) from None
    # The historical baseline timestamp intentionally has no age-expiry.
    groups = []
    expected = value["expected_active"]
    require(type(expected) is list and len(expected) <= MAX_ROWS, reason)
    for row in expected:
        closed_object(row, ("id", "activeVersionId"), reason)
        identifier(row["id"], reason)
        identifier(row["activeVersionId"], reason)
    groups.append([row["id"] for row in expected])
    for key in ("retired_inactive", "retired_absent"):
        rows = value[key]
        require(type(rows) is list and len(rows) <= MAX_ROWS, reason)
        for item in rows:
            identifier(item, reason)
        groups.append(rows)
    combined = []
    for group in groups:
        require(group == sorted(set(group)), reason)
        combined.extend(group)
    require(len(combined) <= MAX_ROWS and len(combined) == len(set(combined)), reason)
    return value


def file_signature(state):
    return (state.st_dev, state.st_ino, state.st_mode, state.st_uid, state.st_gid,
            state.st_nlink, state.st_size, state.st_mtime_ns, state.st_ctime_ns)


def directory_signature(state):
    return (state.st_dev, state.st_ino, state.st_mode, state.st_uid, state.st_gid)


def read_bounded_file(fd):
    result = bytearray()
    while True:
        chunk = os.read(fd, min(4096, MAX_BYTES + 1 - len(result)))
        if not chunk:
            return bytes(result)
        result.extend(chunk)
        require(len(result) <= MAX_BYTES, "baseline_unsafe")


@contextmanager
def private_baseline(path, *, owner_uid=0, owner_gid=0):
    """Retain directory/file FDs and check their bindings and bytes after use.

    Owner injection is solely for offline fixtures, never exposed by the CLI.
    Production ancestors must be root-owned, not writable by group/others;
    immediate parent 0700, regular single-link file root:root 0600.
    """
    descriptors = []
    bindings = []
    try:
        require(type(path) is str and path.startswith("/"), "baseline_unsafe")
        parts = path.split("/")[1:]
        require(parts and all(part not in ("", ".", "..") for part in parts), "baseline_unsafe")
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        parent = os.open("/", flags | os.O_DIRECTORY)
        descriptors.append(parent)

        def directory(fd):
            state = os.fstat(fd)
            require(stat.S_ISDIR(state.st_mode) and not state.st_mode & 0o7022 and
                    (state.st_uid, state.st_gid) in ((0, 0), (owner_uid, owner_gid)),
                    "baseline_unsafe")
            return state

        root_signature = directory_signature(directory(parent))
        for part in parts[:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            descriptors.append(child)
            state = directory(child)
            bindings.append((parent, part, child, directory_signature(state)))
            parent = child
        require(stat.S_IMODE(directory(parent).st_mode) == 0o700, "baseline_unsafe")
        leaf = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(leaf)
        state = os.fstat(leaf)
        require(stat.S_ISREG(state.st_mode) and stat.S_IMODE(state.st_mode) == 0o600 and
                state.st_uid == owner_uid and state.st_gid == owner_gid and
                state.st_nlink == 1 and 0 < state.st_size <= MAX_BYTES, "baseline_unsafe")
        signature = file_signature(state)
        raw = read_bounded_file(leaf)
        require(file_signature(os.fstat(leaf)) == signature and len(raw) == state.st_size,
                "baseline_unsafe")
        baseline = parse_baseline(strict_json(raw, "baseline_invalid"))
        yield baseline
        os.lseek(leaf, 0, os.SEEK_SET)
        require(read_bounded_file(leaf) == raw and file_signature(os.fstat(leaf)) == signature,
                "baseline_unsafe")
        require(file_signature(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)) == signature,
                "baseline_unsafe")
        for ancestor, name, child, saved in bindings:
            require(directory_signature(directory(child)) == saved and
                    directory_signature(os.stat(name, dir_fd=ancestor, follow_symlinks=False)) == saved,
                    "baseline_unsafe")
        require(directory_signature(directory(descriptors[0])) == root_signature,
                "baseline_unsafe")
    except OSError:
        raise ObserverError("baseline_unsafe") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class Budget:
    def __init__(self, seconds=OBSERVATION_SECONDS, clock=time.monotonic):
        self.clock = clock
        self.deadline = clock() + seconds
        self.output_bytes = 0

    def remaining(self):
        remaining = self.deadline - self.clock()
        require(remaining > 0, "observation_deadline")
        return remaining

    def consume(self, size):
        self.output_bytes += size
        require(self.output_bytes <= MAX_BYTES, "observation_too_large")


def child_state(process):
    try:
        # WNOWAIT reserves the leader PID before group signals and reap.
        return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except (ChildProcessError, OSError):
        raise ObserverError("cleanup_identity_lost") from None


def kill_owned_group(process):
    child_state(process)  # ECHILD must never be followed by speculative killpg.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        raise ObserverError("cleanup_inconclusive") from None


def cleanup_owned(process, budget):
    kill_owned_group(process)
    grace = max(0.0, min(CLEANUP_SECONDS,
                         budget.deadline + CLEANUP_SECONDS - budget.clock()))
    try:
        return process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        raise ObserverError("cleanup_inconclusive") from None


def subprocess_environment():
    # No inherited Docker context/config/host, PG variables, HOME or tokens.
    return {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"}


def run_bounded(argv, budget, *, input_data=b"", failure="docker_read_failed"):
    require(sys.platform == "linux" and hasattr(os, "WNOWAIT") and
            signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL and
            threading.active_count() == 1, "unsupported_runtime")
    require(type(input_data) is bytes and len(input_data) <= MAX_BYTES, "internal_error")
    budget.remaining()
    # Allocate fallible selector/output state before any owned child exists.
    selector = selectors.DefaultSelector()
    result = bytearray()
    offset = 0
    stdout_done = False
    terminal = False
    # Block interruption only across spawn/reference registration. The child
    # inherits that mask; SIGKILL used for cleanup cannot be blocked.
    prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, cwd="/", close_fds=True,
                                   start_new_session=True, env=subprocess_environment())
    except BaseException as error:
        signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
        selector.close()
        if isinstance(error, OSError):
            raise ObserverError(failure) from None
        raise
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
        for stream in (process.stdin, process.stdout):
            os.set_blocking(stream.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, "out")
        if input_data:
            selector.register(process.stdin, selectors.EVENT_WRITE, "in")
        else:
            process.stdin.close()
        while True:
            remaining = budget.remaining()
            if child_state(process) is not None and not terminal:
                kill_owned_group(process)
                terminal = True
            if terminal and stdout_done:
                break
            for key, _events in selector.select(min(remaining, 0.02)):
                if key.data == "out":
                    try:
                        chunk = os.read(key.fd, 4096)
                    except BlockingIOError:
                        continue
                    if chunk:
                        budget.consume(len(chunk))
                        result.extend(chunk)
                    else:
                        selector.unregister(key.fileobj)
                        stdout_done = True
                else:
                    try:
                        offset += os.write(key.fd, input_data[offset:])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        offset = len(input_data)
                    if offset == len(input_data):
                        selector.unregister(key.fileobj)
                        process.stdin.close()
    finally:
        try:
            return_code = cleanup_owned(process, budget)
        finally:
            selector.close()
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
    require(return_code == 0, failure)
    return bytes(result)


def validate_identity(service, container, task, cid):
    reason = "runtime_identity_ambiguous"
    closed_object(service, ("id", "name", "image"), reason)
    closed_object(container, ("id", "running", "image_id", "image", "service",
                              "service_id", "task_id"), reason)
    closed_object(task, ("id", "service_id", "desired", "state", "container_id", "image"), reason)
    require(all(type(value) is str for value in service.values()), reason)
    require(all(type(container[key]) is str for key in container if key != "running"), reason)
    require(all(type(value) is str for value in task.values()), reason)
    require(service["name"] == container["service"] == SERVICE and
            SWARM_ID.fullmatch(service["id"]) and SWARM_ID.fullmatch(task["id"]) and
            service["id"] == container["service_id"] == task["service_id"] and
            task["id"] == container["task_id"] and container["running"] is True and
            container["id"] == task["container_id"] == cid and CONTAINER_ID.fullmatch(cid) and
            task["desired"] == task["state"] == "running" and
            IMAGE_ID.fullmatch(container["image_id"]) and IMAGE_REF.fullmatch(service["image"]) and
            service["image"] == container["image"] == task["image"], reason)


def validate_snapshot(value, now=None):
    now = now or utc_now()
    reason = "metadata_invalid"
    closed_object(value, ("snapshot_at", "workflows", "executions"), reason)
    instant = utc_parse(value["snapshot_at"], reason)
    age = (now - instant).total_seconds()
    require(-FUTURE_SECONDS <= age <= OBSERVATION_SECONDS + CLEANUP_SECONDS,
            "clock_inconsistent")
    rows = value["workflows"]
    require(type(rows) is list and len(rows) <= MAX_ROWS, reason)
    seen = set()
    for row in rows:
        closed_object(row, ("id", "active", "isArchived", "activeVersionId"), reason)
        identifier(row["id"], reason)
        require(row["id"] not in seen, reason)
        seen.add(row["id"])
        require(type(row["active"]) is bool and type(row["isArchived"]) is bool, reason)
        if row["activeVersionId"] is not None:
            identifier(row["activeVersionId"], reason)
    counts = value["executions"]
    closed_object(counts, (*STATUSES, "total", "unrecognized"), reason)
    require(all(type(count) is int and 0 <= count <= MAX_COUNT for count in counts.values()), reason)
    require(counts["total"] == sum(counts[key] for key in (*STATUSES, "unrecognized")), reason)
    return value


def collect_live(budget, *, _runner=run_bounded, _config_parent="/run"):
    """Return private validated metadata to an authorized local caller only.

    No capture/learn option exists on the CLI. All command arguments and SQL
    are fixed apart from verified Docker IDs and a disposable empty config.
    Test-only runner/config injection never appears in CLI arguments.
    """
    with tempfile.TemporaryDirectory(prefix="n8n-observer-", dir=_config_parent) as config:
        base = [DOCKER, "--host=unix:///run/docker.sock", "--config", config]

        def read(args, *, raw=False, stdin=b"", failure="docker_read_failed"):
            budget.remaining()
            output = _runner(base + args, budget, input_data=stdin, failure=failure)
            require(type(output) is bytes and len(output) <= MAX_BYTES, "metadata_invalid")
            return output if raw else strict_json(output, "metadata_invalid")

        service_args = ["service", "inspect", "--format", SERVICE_FORMAT, SERVICE]
        selector_args = ["container", "ls", "--filter", "label=com.docker.swarm.service.name=" + SERVICE,
                         "--filter", "status=running", "--no-trunc", "--format", "{{.ID}}"]
        service = read(service_args)
        selected = read(selector_args, raw=True)
        require(re.fullmatch(rb"[a-f0-9]{64}\n?", selected), "runtime_identity_ambiguous")
        cid = selected.decode("ascii").strip()
        container_args = ["container", "inspect", "--format", CONTAINER_FORMAT, cid]
        container = read(container_args)
        closed_object(container, ("id", "running", "image_id", "image", "service", "service_id", "task_id"),
                      "runtime_identity_ambiguous")
        require(type(container["task_id"]) is str and SWARM_ID.fullmatch(container["task_id"]),
                "runtime_identity_ambiguous")
        task_args = ["inspect", "--type", "task", "--format", TASK_FORMAT, container["task_id"]]
        task = read(task_args)
        validate_identity(service, container, task, cid)
        snapshot = read(["exec", "-i", cid, "/bin/sh", "-c", PSQL_SCRIPT],
                        stdin=SQL, failure="database_read_failed")
        final_container, final_task = read(container_args), read(task_args)
        final_selected, final_service = read(selector_args, raw=True), read(service_args)
        require(re.fullmatch(rb"[a-f0-9]{64}\n?", final_selected), "runtime_identity_ambiguous")
        validate_identity(final_service, final_container, final_task, cid)
        require((final_service, final_container, final_task, final_selected.strip()) ==
                (service, container, task, selected.strip()), "runtime_identity_changed")
        budget.remaining()
        return validate_snapshot(snapshot)


def report_for(baseline, snapshot):
    rows = {row["id"]: row for row in snapshot["workflows"]}
    expected = {row["id"]: row["activeVersionId"] for row in baseline["expected_active"]}
    published = {key for key, row in rows.items() if row["activeVersionId"] is not None}
    active = {key for key, row in rows.items() if row["active"]}
    counts = {
        "expected_active": len(expected), "retired_inactive": len(baseline["retired_inactive"]),
        "retired_absent": len(baseline["retired_absent"]), "observed_total": len(rows),
        "observed_active": len(active), "observed_published": len(published),
        "observed_inactive": len(rows) - len(active),
        "observed_archived": sum(row["isArchived"] for row in rows.values()),
        "missing_expected": sum(key not in rows for key in expected),
        "stopped_expected": sum(key in rows and (not rows[key]["active"] or rows[key]["isArchived"])
                                for key in expected),
        "version_drift": sum(key in active and rows[key]["activeVersionId"] != version
                             for key, version in expected.items()),
        "new_active": len(active - expected.keys()),
        "unexpected_published": len(published - expected.keys()),
        "retired_missing": sum(key not in rows for key in baseline["retired_inactive"]),
        "retired_reactivated": sum(key in active or key in published
                                   for key in (*baseline["retired_inactive"], *baseline["retired_absent"])),
        "confirmed_absent_reappeared": sum(key in rows for key in baseline["retired_absent"]),
        "publication_mismatch": sum(row["active"] != (row["activeVersionId"] is not None) or
                                    (row["isArchived"] and (row["active"] or row["activeVersionId"] is not None))
                                    for row in rows.values()),
    }
    counts.update({"execution_" + key: value for key, value in snapshot["executions"].items()})
    counts["execution_errors"] = counts["execution_error"] + counts["execution_crashed"]
    drift = ("missing_expected", "stopped_expected", "version_drift", "new_active",
             "unexpected_published", "retired_missing", "retired_reactivated", "confirmed_absent_reappeared")
    if counts["publication_mismatch"]:
        status, reason = "unknown", "workflow_publication_mismatch"
    elif counts["execution_unrecognized"]:
        status, reason = "unknown", "execution_state_unrecognized"
    elif counts["execution_unknown"]:
        status, reason = "unknown", "execution_state_unknown"
    elif counts["execution_errors"]:
        status, reason = "error", "execution_errors"
    elif any(counts[key] for key in drift):
        status, reason = "drift", "workflow_drift"
    else:
        status, reason = "ok", "observed"
    return {"schema": 1, "scope": SCOPE, "status": status, "reason": reason,
            "observed_at": snapshot["snapshot_at"], "checked_at": utc_string(utc_now()), "counts": counts}


def unknown_report(reason):
    return {"schema": 1, "scope": SCOPE, "status": "unknown",
            "reason": reason if reason in REASONS else "internal_error",
            "observed_at": None, "checked_at": utc_string(utc_now()), "counts": None}


@contextmanager
def interruption_guard():
    originals = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    def interrupted(_signum, _frame):
        # Let the first interruption unwind into bounded cleanup; avoid a
        # second TERM/INT interrupting that cleanup. SIGKILL cannot be handled.
        for sig in originals:
            signal.signal(sig, signal.SIG_IGN)
        raise ObserverError("interrupted")

    for sig in originals:
        signal.signal(sig, interrupted)
    try:
        yield
    finally:
        for sig, handler in originals.items():
            signal.signal(sig, handler)


def emit_report(report, budget):
    raw = (json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
    require(len(raw) <= 4096, "internal_error")
    original = fcntl.fcntl(1, fcntl.F_GETFL)
    deadline = min(time.monotonic() + CLEANUP_SECONDS, budget.deadline + CLEANUP_SECONDS)
    try:
        fcntl.fcntl(1, fcntl.F_SETFL, original | os.O_NONBLOCK)
        offset = 0
        while offset < len(raw):
            require(time.monotonic() < deadline, "observation_deadline")
            try:
                written = os.write(1, raw[offset:])
                require(written > 0, "internal_error")
                offset += written
            except BlockingIOError:
                require(time.monotonic() < deadline, "observation_deadline")
                time.sleep(0.002)
    finally:
        fcntl.fcntl(1, fcntl.F_SETFL, original)


def main(argv=None):
    budget = Budget()
    argv = sys.argv[1:] if argv is None else argv
    with interruption_guard():
        try:
            require(os.geteuid() == 0, "root_required")
            require(len(argv) == 2 and argv[0] == "--baseline", "arguments_invalid")
            with private_baseline(argv[1]) as baseline:
                snapshot = collect_live(budget)
                report = report_for(baseline, snapshot)
            budget.remaining()
        except ObserverError as error:
            report = unknown_report(error.reason)
        except (Exception, KeyboardInterrupt):
            report = unknown_report("internal_error")
        try:
            emit_report(report, budget)
        except (Exception, KeyboardInterrupt):
            return 1
        return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
