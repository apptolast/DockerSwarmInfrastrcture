#!/usr/bin/env python3
"""Provision least-privilege pg_monitor roles through local trusted sockets."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any

import yaml

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))
from host_global_operation_lock import ensure_mutation_lock  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = REPOSITORY_ROOT / "stacks/observability/secrets.yml"
DEFAULT_SOURCE_DIR = Path("/srv/dockerswarm/observability/secrets/files")
ROLE_RE = re.compile(r"[a-z][a-z0-9_]{2,62}")
CONTAINER_RE = re.compile(r"[a-f0-9]{12,64}")
DATABASES = (
    (
        "n8n",
        "workloads_n8n-db",
        "postgres_exporter_n8n_password",
    ),
    (
        "passbolt",
        "workloads_passbolt-db",
        "postgres_exporter_passbolt_password",
    ),
    (
        "shlink",
        "workloads_shlink-db",
        "postgres_exporter_shlink_password",
    ),
)


class ProvisionError(RuntimeError):
    """PostgreSQL monitoring-role provisioning failed closed."""


def load_catalog(path: Path) -> tuple[str, dict[str, dict[str, Any]]]:
    try:
        catalog = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ProvisionError(f"cannot read secret catalog: {path}") from exc
    if not isinstance(catalog, dict):
        raise ProvisionError("secret catalog is not a mapping")
    version = catalog.get("observability_secret_version")
    entries = catalog.get("observability_secrets")
    if (
        not isinstance(version, str)
        or not re.fullmatch(r"v[1-9][0-9]*", version)
        or not isinstance(entries, list)
    ):
        raise ProvisionError("secret catalog is incomplete")
    by_key = {
        entry.get("key"): entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("key"), str)
    }
    expected_keys = {item[2] for item in DATABASES}
    if not expected_keys.issubset(by_key):
        raise ProvisionError("PostgreSQL exporter secrets are incomplete")
    return version, by_key


def read_password(source_dir: Path, source_file: str) -> str:
    source = source_dir / source_file
    if source.is_symlink() or not source.is_file():
        raise ProvisionError(f"unsafe password source: {source_file}")
    if stat.S_IMODE(source.stat().st_mode) != 0o600:
        raise ProvisionError(f"password source must be mode 0600: {source_file}")
    try:
        password = source.read_text(encoding="ascii").rstrip("\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProvisionError(f"cannot read password source: {source_file}") from exc
    if not 32 <= len(password) <= 256 or any(
        ord(character) < 0x21 or ord(character) > 0x7E for character in password
    ):
        raise ProvisionError(f"password source has unsafe content: {source_file}")
    return password


def find_database_container(service_name: str) -> str:
    completed = subprocess.run(
        [
            "/usr/bin/docker",
            "ps",
            "--filter",
            f"label=com.docker.swarm.service.name={service_name}",
            "--filter",
            "status=running",
            "--format",
            "{{.ID}}",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    container_ids = [
        value.strip() for value in completed.stdout.splitlines() if value.strip()
    ]
    if (
        completed.returncode != 0
        or len(container_ids) != 1
        or not CONTAINER_RE.fullmatch(container_ids[0])
    ):
        raise ProvisionError(f"expected one running database task for {service_name}")
    return container_ids[0]


def quote_literal(value: str) -> str:
    if "\x00" in value:
        raise ProvisionError("NUL is forbidden in SQL values")
    return "'" + value.replace("'", "''") + "'"


def quote_identifier(value: str) -> str:
    if not ROLE_RE.fullmatch(value) and not re.fullmatch(
        r"[a-z][a-z0-9_-]{1,62}",
        value,
    ):
        raise ProvisionError("unsafe SQL identifier")
    return '"' + value.replace('"', '""') + '"'


def build_sql(
    role: str,
    database: str,
    password: str,
    marker: str,
) -> str:
    role_identifier = quote_identifier(role)
    database_identifier = quote_identifier(database)
    role_literal = quote_literal(role)
    password_literal = quote_literal(password)
    marker_literal = quote_literal(marker)
    return f"""\
BEGIN;
DO $provision$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {role_literal}) THEN
    CREATE ROLE {role_identifier} LOGIN PASSWORD {password_literal};
  ELSIF COALESCE(
    (
      SELECT shobj_description(oid, 'pg_authid')
      FROM pg_authid
      WHERE rolname = {role_literal}
    ),
    ''
  ) <> {marker_literal} THEN
    ALTER ROLE {role_identifier} LOGIN PASSWORD {password_literal};
  END IF;
END
$provision$;
GRANT pg_monitor TO {role_identifier};
GRANT CONNECT ON DATABASE {database_identifier} TO {role_identifier};
ALTER ROLE {role_identifier} SET statement_timeout = '10s';
COMMENT ON ROLE {role_identifier} IS {marker_literal};
COMMIT;
"""


def provision(
    container_id: str, sql: str, service_name: str, superuser: str
) -> None:
    if not ROLE_RE.fullmatch(superuser):
        raise ProvisionError(f"invalid superuser identifier for {service_name}")
    completed = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            "--interactive",
            "--user",
            "postgres",
            container_id,
            "psql",
            # `--user postgres` es la cuenta del sistema dentro del contenedor,
            # no el rol de PostgreSQL. Cada instancia se inicializa con
            # POSTGRES_USER propio (n8n, passbolt y shlink en
            # stacks/workloads/stack.yml.j2), de modo que el rol `postgres`
            # nunca llega a existir y psql, que por defecto toma el nombre de
            # la cuenta del sistema, fallaba con
            # `FATAL: role "postgres" does not exist`. Hay que nombrar el
            # superusuario real de forma explicita.
            "--username",
            superuser,
            "--dbname",
            "postgres",
            "--no-psqlrc",
            "--set",
            "ON_ERROR_STOP=1",
            "--file",
            "-",
        ],
        check=False,
        input=sql,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        if len(detail) > 800:
            detail = f"{detail[:800]}..."
        raise ProvisionError(f"role provisioning failed for {service_name}: {detail}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--role", default="apptolast_monitor")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_mutation_lock(
        "observability-db-provision",
        [
            sys.executable,
            str(Path(__file__).resolve()),
            *sys.argv[1:],
        ],
        caller=Path(__file__),
    )
    try:
        if not ROLE_RE.fullmatch(args.role):
            raise ProvisionError("invalid PostgreSQL monitoring role")
        version, entries = load_catalog(args.catalog)
        if (
            args.source_dir.is_symlink()
            or not args.source_dir.is_dir()
            or stat.S_IMODE(args.source_dir.stat().st_mode) != 0o700
        ):
            raise ProvisionError("secret source directory must be mode 0700")
        for database, service_name, secret_key in DATABASES:
            entry = entries[secret_key]
            source_file = entry.get("source_file")
            if not isinstance(source_file, str):
                raise ProvisionError(f"source_file is absent for {secret_key}")
            password = read_password(args.source_dir, source_file)
            fingerprint = hashlib.sha256((password + "\n").encode("ascii")).hexdigest()
            marker = f"apptolast-observability:{version}:" f"{secret_key}:{fingerprint}"
            container_id = find_database_container(service_name)
            provision(
                container_id,
                build_sql(args.role, database, password, marker),
                service_name,
                database,
            )
    except (OSError, ProvisionError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"PostgreSQL monitoring role reconciled in {len(DATABASES)} databases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
