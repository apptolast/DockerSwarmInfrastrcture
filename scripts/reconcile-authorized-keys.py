#!/usr/bin/env python3
"""Remove only cryptographically invalid authorized-key lines, with backup."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))
from host_global_operation_lock import ensure_mutation_lock  # noqa: E402

VALIDATOR_PATH = PROJECT_DIR / "scripts/validate-authorized-keys.py"
DEFAULT_PATH = Path("/home/admin/.ssh/authorized_keys")
BACKUP_ROOT = Path("/var/backups/dockerswarm")


class ReconciliationError(RuntimeError):
    """The authorized-key cleanup cannot be proven safe."""


def load_validator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "dockerswarm_authorized_key_validator",
        VALIDATOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise ReconciliationError("cannot load the authorized-key validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require_safe_target(path: Path) -> os.stat_result:
    try:
        path_state = path.lstat()
    except OSError as error:
        raise ReconciliationError("cannot inspect authorized_keys") from error
    if (
        not stat.S_ISREG(path_state.st_mode)
        or path_state.st_uid != 1001
        or path_state.st_gid != 1001
        or stat.S_IMODE(path_state.st_mode) != 0o600
        or path_state.st_nlink != 1
    ):
        raise ReconciliationError(
            "authorized_keys must be admin:admin 0600 with one hard link"
        )
    return path_state


def build_plan(path: Path, ssh_keygen: str) -> dict[str, Any]:
    require_safe_target(path)
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReconciliationError("authorized_keys is not UTF-8") from error
    if "\r" in text or "\x00" in text:
        raise ReconciliationError("authorized_keys contains unsafe bytes")
    lines = text.splitlines()
    if not lines:
        raise ReconciliationError("authorized_keys is empty")

    validator = load_validator()
    retained: list[str] = []
    invalid_line_numbers: list[int] = []
    for line_number, line in enumerate(lines, start=1):
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="dockerswarm-authorized-key-line-",
        ) as candidate:
            candidate.write(f"{line}\n")
            candidate.flush()
            try:
                validator.validate(Path(candidate.name), ssh_keygen)
            except validator.AuthorizedKeysError:
                invalid_line_numbers.append(line_number)
            else:
                retained.append(line)
    if not retained:
        raise ReconciliationError("cleanup would remove every authorized key")

    after = ("\n".join(retained) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix="dockerswarm-authorized-keys-candidate-",
    ) as candidate:
        candidate.write(after)
        candidate.flush()
        validator.validate(Path(candidate.name), ssh_keygen)

    before_sha256 = hashlib.sha256(raw).hexdigest()
    after_sha256 = hashlib.sha256(after).hexdigest()
    confirmation = (
        "AUTHORIZED_KEYS_CLEAN:"
        f"{before_sha256}:{after_sha256}:{len(invalid_line_numbers)}"
    )
    return {
        "before": raw,
        "after": after,
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "invalid_line_numbers": invalid_line_numbers,
        "retained_count": len(retained),
        "confirmation": confirmation,
    }


def apply_plan(path: Path, plan: dict[str, Any], confirmation: str) -> Path:
    if os.geteuid() != 0:
        raise ReconciliationError("--apply requires root")
    if confirmation != plan["confirmation"]:
        raise ReconciliationError("confirmation differs from the dry-run plan")
    current_state = require_safe_target(path)
    if path.read_bytes() != plan["before"]:
        raise ReconciliationError("authorized_keys changed after the dry run")

    BACKUP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(BACKUP_ROOT, 0, 0)
    os.chmod(BACKUP_ROOT, 0o700)
    backup = BACKUP_ROOT / (f"authorized_keys.{plan['before_sha256']}.pre-iac")
    if backup.exists():
        if (
            backup.is_symlink()
            or not backup.is_file()
            or backup.read_bytes() != plan["before"]
        ):
            raise ReconciliationError("existing backup is unsafe or different")
    else:
        with backup.open("xb") as stream:
            stream.write(plan["before"])
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(backup, 0, 0)
        os.chmod(backup, 0o600)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".authorized_keys.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(plan["after"])
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, current_state.st_uid, current_state.st_gid)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
    require_safe_target(path)
    if path.read_bytes() != plan["after"]:
        raise ReconciliationError("authorized_keys atomic replacement drifted")
    return backup


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--ssh-keygen", default="/usr/bin/ssh-keygen")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.apply:
        ensure_mutation_lock(
            "authorized-keys-reconcile",
            [
                sys.executable,
                str(Path(__file__).resolve()),
                *sys.argv[1:],
            ],
            caller=Path(__file__),
        )
    try:
        plan = build_plan(args.path, args.ssh_keygen)
        print(
            "Authorized-key cleanup plan: "
            f"remove={len(plan['invalid_line_numbers'])}, "
            f"retain={plan['retained_count']}, "
            f"before={plan['before_sha256']}, "
            f"after={plan['after_sha256']}"
        )
        print(f"Confirmation: {plan['confirmation']}")
        if not args.apply:
            print("Dry run only; no key file was changed.")
            return 0
        backup = apply_plan(args.path, plan, args.confirm)
        print(f"Applied atomically; recoverable backup: {backup}")
    except (OSError, ReconciliationError) as error:
        print(f"ERROR: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
