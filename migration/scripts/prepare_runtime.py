#!/usr/bin/env python3
"""Materialize verified service data and Docker secret source files.

The destination is intentionally prepared atomically.  Secret values are never
written to dotenv files, rendered into a manifest, or printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from backup_safety import SafetyError, sha256_file, verify_checksum_tree

HOST_LOCK_DIRECTORY = Path(__file__).resolve().parents[2] / "scripts"
if str(HOST_LOCK_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(HOST_LOCK_DIRECTORY))
from host_global_operation_lock import ensure_mutation_lock  # noqa: E402

DEFAULT_SERVICES_ROOT = Path("/srv/dockerswarm/services")
DATABASE_DUMPS = ("n8n", "passbolt", "shlink", "vectors", "rag")
LEGACY_N8N_STORAGE_WHEELS = {
    "n8n-nodes-base.code/beautifulsoup4-4.13.3-py3-none-any.whl",
    "n8n-nodes-base.code/certifi-2025.6.15-py3-none-any.whl",
    "n8n-nodes-base.code/charset_normalizer-3.4.2-py3-none-any.whl",
    "n8n-nodes-base.code/idna-3.10-py3-none-any.whl",
    "n8n-nodes-base.code/requests-2.32.3-py3-none-any.whl",
    "n8n-nodes-base.code/soupsieve-2.6-py3-none-any.whl",
    "n8n-nodes-base.code/typing_extensions-4.14.0-py3-none-any.whl",
    "n8n-nodes-base.code/urllib3-2.3.0-py3-none-any.whl",
}
DIRECT_SECRET_NAMES = (
    "n8n_db_password",
    "passbolt_db_password",
    "shlink_db_password",
    "n8n_runners_auth_token",
    "openclaw_gateway_token",
)
SECRET_IDENTITY_KEY_FILE = "workloads_secret_identity_hmac_key"
SECRET_IDENTITY_KEY_SIZE = 32
REQUIRED_IMPORTED_SECRET_NAMES = {
    "n8n_encryption_key",
    "passbolt_security_salt",
}
LOG_DIRECTORIES = (
    "n8n",
    "n8n-postgres",
    "passbolt",
    "passbolt-postgres",
    "shlink",
    "shlink-postgres",
    "openclaw",
)
SERVICE_DATASET_DIRECTORIES = {
    "minecraft-worlds": "minecraft/data",
    "minecraft-mods": "minecraft/mods",
    "n8n-home": "n8n/home",
    "n8n-postgres": "n8n/postgres",
    "openclaw-clean-home": "openclaw-clean/home",
    "passbolt-gpg": "passbolt/gpg",
    "passbolt-jwt": "passbolt/jwt",
    "passbolt-postgres": "passbolt/postgres",
    "shlink-postgres": "shlink/postgres",
}
APPROVED_ENV_KEYS = {
    "n8n": {
        "N8N_ENCRYPTION_KEY",
        "N8N_EMAIL_MODE",
        "N8N_SMTP_HOST",
        "N8N_SMTP_PORT",
        "N8N_SMTP_USER",
        "N8N_SMTP_PASS",
        "N8N_SMTP_SENDER",
        "N8N_SMTP_SSL",
        "N8N_SMTP_STARTTLS",
    },
    "passbolt": {
        "SECURITY_SALT",
        "EMAIL_TRANSPORT_DEFAULT_HOST",
        "EMAIL_TRANSPORT_DEFAULT_PORT",
        "EMAIL_TRANSPORT_DEFAULT_USERNAME",
        "EMAIL_TRANSPORT_DEFAULT_PASSWORD",
        "EMAIL_TRANSPORT_DEFAULT_TLS",
        "EMAIL_DEFAULT_FROM",
        "EMAIL_DEFAULT_FROM_NAME",
        "PASSBOLT_KEY_EMAIL",
    },
    "shlink": {
        "GEOLITE_LICENSE_KEY",
        "MERCURE_PUBLIC_HUB_URL",
        "MERCURE_INTERNAL_HUB_URL",
        "MERCURE_JWT_SECRET",
        "MATOMO_SITE_ID",
        "MATOMO_URL",
    },
    "portfolio-pablo": {
        "NEXT_PUBLIC_EMAILJS_SERVICE_ID",
        "NEXT_PUBLIC_EMAILJS_TEMPLATE_ID",
        "NEXT_PUBLIC_EMAILJS_PUBLIC_KEY",
        "EMAILJS_SERVICE_ID",
        "EMAILJS_TEMPLATE_ID",
        "EMAILJS_PUBLIC_KEY",
    },
}
SECRET_FILE_NAMES = {
    "n8n": {
        "N8N_ENCRYPTION_KEY": "n8n_encryption_key",
        "N8N_EMAIL_MODE": "n8n_email_mode",
        "N8N_SMTP_HOST": "n8n_smtp_host",
        "N8N_SMTP_PORT": "n8n_smtp_port",
        "N8N_SMTP_USER": "n8n_smtp_user",
        "N8N_SMTP_PASS": "n8n_smtp_pass",
        "N8N_SMTP_SENDER": "n8n_smtp_sender",
        "N8N_SMTP_SSL": "n8n_smtp_ssl",
        "N8N_SMTP_STARTTLS": "n8n_smtp_starttls",
    },
    "passbolt": {
        "SECURITY_SALT": "passbolt_security_salt",
        "EMAIL_TRANSPORT_DEFAULT_HOST": "passbolt_email_host",
        "EMAIL_TRANSPORT_DEFAULT_PORT": "passbolt_email_port",
        "EMAIL_TRANSPORT_DEFAULT_USERNAME": "passbolt_email_username",
        "EMAIL_TRANSPORT_DEFAULT_PASSWORD": "passbolt_email_password",
        "EMAIL_TRANSPORT_DEFAULT_TLS": "passbolt_email_tls",
        "EMAIL_DEFAULT_FROM": "passbolt_email_from",
        "EMAIL_DEFAULT_FROM_NAME": "passbolt_email_from_name",
        "PASSBOLT_KEY_EMAIL": "passbolt_key_email",
    },
    "shlink": {
        "GEOLITE_LICENSE_KEY": "shlink_geolite_license_key",
        "MERCURE_PUBLIC_HUB_URL": "shlink_mercure_public_hub_url",
        "MERCURE_INTERNAL_HUB_URL": "shlink_mercure_internal_hub_url",
        "MERCURE_JWT_SECRET": "shlink_mercure_jwt_secret",
        "MATOMO_SITE_ID": "shlink_matomo_site_id",
        "MATOMO_URL": "shlink_matomo_url",
    },
    "portfolio-pablo": {
        "NEXT_PUBLIC_EMAILJS_SERVICE_ID": "pablo_next_public_emailjs_service_id",
        "NEXT_PUBLIC_EMAILJS_TEMPLATE_ID": "pablo_next_public_emailjs_template_id",
        "NEXT_PUBLIC_EMAILJS_PUBLIC_KEY": "pablo_next_public_emailjs_public_key",
        "EMAILJS_SERVICE_ID": "pablo_emailjs_service_id",
        "EMAILJS_TEMPLATE_ID": "pablo_emailjs_template_id",
        "EMAILJS_PUBLIC_KEY": "pablo_emailjs_public_key",
    },
}
NAMESPACE_FOR_APP = {
    "n8n": "n8n",
    "passbolt": "passbolt",
    "shlink": "shlink",
    "portfolio-pablo": "portfolio-web-pablohg",
}
MAX_ARCHIVE_MEMBERS = 1_000_000
MAX_ARCHIVE_BYTES = 1024**4


def require_stage(stage: Path) -> None:
    if stage.is_symlink() or not stage.is_dir():
        raise SafetyError(f"Staging inseguro o ausente: {stage}")
    verify_checksum_tree(stage, stage / "SHA256SUMS")
    required = {
        *(f"databases/{name}.dump" for name in DATABASE_DUMPS),
        "filesystems/n8n-home.tar.gz",
        "filesystems/passbolt-keys.tar.gz",
        "filesystems/minecraft-data.tar.zst",
        "filesystems/minecraft-bind-mods.tar.zst",
        "filesystems/traefik-acme.json",
        "filesystems/OPENCLAW-LEGACY-EXCLUDED.txt",
    }
    for relative in required:
        path = stage.joinpath(*PurePosixPath(relative).parts)
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise SafetyError(f"Falta el artefacto requerido: {relative}")
    if (stage / "filesystems/openclaw-legacy-raw.tar.zst").exists() or (
        stage / "filesystems/OPENCLAW-LEGACY-NOT-FOR-AUTORESTORE.txt"
    ).exists():
        raise SafetyError("Se rechaza cualquier staging con OpenClaw legado")


def safe_member_name(name: str) -> PurePosixPath:
    raw = name.rstrip("/")
    parts = raw.split("/")
    if (
        not raw
        or "\x00" in raw
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise SafetyError(f"Ruta insegura en archivo persistente: {name!r}")
    return PurePosixPath(*parts)


def extract_archive(
    archive: tarfile.TarFile,
    destination: Path,
    allowed_roots: set[str],
    *,
    strip_root: bool,
) -> None:
    seen: set[str] = set()
    total = 0
    files = 0
    for index, member in enumerate(archive, start=1):
        if index > MAX_ARCHIVE_MEMBERS:
            raise SafetyError("Demasiadas entradas en archivo persistente")
        pure = safe_member_name(member.name)
        if pure.parts[0] not in allowed_roots:
            raise SafetyError(f"Raíz inesperada en archivo: {pure.parts[0]}")
        output_parts = pure.parts[1:] if strip_root else pure.parts
        if not output_parts:
            if not member.isdir():
                raise SafetyError("La raíz del archivo debe ser un directorio")
            continue
        normalized = PurePosixPath(*output_parts).as_posix()
        if normalized in seen:
            raise SafetyError(f"Entrada duplicada: {normalized}")
        seen.add(normalized)
        target = destination.joinpath(*output_parts)
        if member.isdir():
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.chmod(0o700)
            continue
        if not member.isfile():
            raise SafetyError(f"Tipo no permitido en archivo: {normalized}")
        total += member.size
        if total > MAX_ARCHIVE_BYTES:
            raise SafetyError("Archivo persistente demasiado grande")
        source = archive.extractfile(member)
        if source is None:
            raise SafetyError(f"No se pudo leer: {normalized}")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            target,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        files += 1
    if files == 0:
        raise SafetyError("El archivo persistente no contiene ficheros")


def extract_gzip(
    source: Path, destination: Path, roots: set[str], *, strip_root: bool
) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.is_symlink() or not destination.is_dir():
        raise SafetyError(f"Destino de extracción inseguro: {destination}")
    with tarfile.open(source, mode="r:gz") as archive:
        extract_archive(archive, destination, roots, strip_root=strip_root)


def extract_zstd(
    source: Path, destination: Path, roots: set[str], *, strip_root: bool
) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.is_symlink() or not destination.is_dir():
        raise SafetyError(f"Destino de extracción inseguro: {destination}")
    process = subprocess.Popen(
        ["zstd", "-dc", "--", str(source)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            extract_archive(archive, destination, roots, strip_root=strip_root)
    except Exception:
        process.kill()
        process.wait()
        raise
    stderr = process.stderr.read() if process.stderr is not None else b""
    if process.wait() != 0:
        raise SafetyError(
            f"zstd no pudo leer {source.name}: {stderr.decode(errors='replace')[:200]}"
        )


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def copy_private(source: Path, target: Path) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        target,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0),
        0o600,
    )
    with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output:
        shutil.copyfileobj(input_handle, output, length=1024 * 1024)
        output.flush()
        os.fsync(output.fileno())


def decode_approved_namespace_secrets(
    stage: Path, namespace: str, approved: set[str]
) -> dict[str, str]:
    path = stage / "kubernetes" / namespace / "secrets.json"
    if not path.exists():
        return {}
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > 64 * 1024 * 1024
    ):
        raise SafetyError(f"Secrets JSON inseguro: {namespace}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafetyError(f"Secrets JSON inválido: {namespace}") from exc
    items = document.get("items", []) if isinstance(document, dict) else []
    if not isinstance(items, list):
        raise SafetyError(f"Lista de secrets inválida: {namespace}")
    found: dict[str, str] = {}
    for item in items:
        data = item.get("data", {}) if isinstance(item, dict) else {}
        if not isinstance(data, dict):
            continue
        for key in approved & set(data):
            encoded = data[key]
            if not isinstance(encoded, str):
                raise SafetyError(f"Valor secret inválido: {namespace}/{key}")
            try:
                value = base64.b64decode(encoded, validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise SafetyError(f"Secret no UTF-8/base64: {namespace}/{key}") from exc
            if any(character in value for character in ("\x00", "\r", "\n")):
                raise SafetyError(f"Secret multilinea no admitido: {namespace}/{key}")
            if key in found and found[key] != value:
                raise SafetyError(f"Secret ambiguo: {namespace}/{key}")
            found[key] = value
    return found


def write_approved_secret_files(
    secret_root: Path, decoded: dict[str, dict[str, str]]
) -> list[str]:
    """Write one private file for every approved workload secret.

    The explicit mapping prevents an attacker-controlled Kubernetes key from
    becoming a path. Empty optional values become zero-byte files because the
    Swarm catalog declares every secret and application entrypoints distinguish
    a present-but-empty optional value from a missing secret mount.
    """

    unexpected_apps = set(decoded) - set(SECRET_FILE_NAMES)
    if unexpected_apps:
        raise SafetyError(
            f"Aplicaciones de secrets no aprobadas: {sorted(unexpected_apps)}"
        )
    written: set[str] = set()
    for app in sorted(SECRET_FILE_NAMES):
        mapping = SECRET_FILE_NAMES[app]
        values = decoded.get(app, {})
        unexpected_keys = set(values) - set(mapping)
        if unexpected_keys:
            raise SafetyError(
                f"Variables no aprobadas para {app}: {sorted(unexpected_keys)}"
            )
        for key in sorted(mapping):
            value = values.get(key, "")
            if any(character in value for character in ("\x00", "\r", "\n")):
                raise SafetyError(f"Valor secret inseguro: {app}/{key}")
            filename = mapping[key]
            if filename in written or filename in DIRECT_SECRET_NAMES:
                raise SafetyError(f"Nombre de secret duplicado: {filename}")
            if filename in REQUIRED_IMPORTED_SECRET_NAMES and not value:
                raise SafetyError(f"Secret requerido vacío: {filename}")
            write_private(
                secret_root / filename,
                ((value + "\n") if value else "").encode("utf-8"),
            )
            written.add(filename)
    return sorted(written)


def n8n_encryption_key(runtime: Path, decoded: dict[str, str]) -> str:
    if decoded.get("N8N_ENCRYPTION_KEY"):
        return decoded["N8N_ENCRYPTION_KEY"]
    config = runtime / SERVICE_DATASET_DIRECTORIES["n8n-home"] / "config"
    try:
        document = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafetyError("No se pudo recuperar N8N_ENCRYPTION_KEY") from exc
    value = document.get("encryptionKey") if isinstance(document, dict) else None
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
        raise SafetyError("N8N_ENCRYPTION_KEY ausente o inválida")
    return value


def copy_recovery_artifacts(stage: Path, runtime: Path) -> None:
    recovery = runtime / "recovery"
    databases = recovery / "databases"
    databases.mkdir(mode=0o700, parents=True, exist_ok=False)
    lines: list[str] = []
    for name in DATABASE_DUMPS:
        source = stage / "databases" / f"{name}.dump"
        target = databases / f"{name}.dump"
        copy_private(source, target)
        lines.append(f"{sha256_file(target)}  databases/{target.name}")
    edge = recovery / "edge"
    edge.mkdir(mode=0o700, parents=True, exist_ok=False)
    acme_target = edge / "traefik-acme.json"
    copy_private(
        stage / "filesystems/traefik-acme.json",
        acme_target,
    )
    lines.append(f"{sha256_file(acme_target)}  edge/{acme_target.name}")
    write_private(
        recovery / "SHA256SUMS",
        ("\n".join(lines) + "\n").encode("ascii"),
    )


def remove_legacy_n8n_storage_wheels(n8n_home: Path) -> None:
    """Remove the exact obsolete runner wheel cache that blocks n8n v3 migration."""
    storage = n8n_home / "storage"
    if not storage.exists():
        return
    if storage.is_symlink() or not storage.is_dir():
        raise SafetyError("El storage legado de n8n no es un directorio seguro")
    observed: set[str] = set()
    for path in storage.rglob("*"):
        if path.is_symlink():
            raise SafetyError("El storage legado de n8n contiene enlaces")
        if path.is_file():
            observed.add(path.relative_to(storage).as_posix())
        elif not path.is_dir():
            raise SafetyError("El storage legado de n8n contiene tipos especiales")
    if observed != LEGACY_N8N_STORAGE_WHEELS:
        raise SafetyError(
            "El storage legado de n8n contiene datos no reconocidos; no se elimina"
        )
    shutil.rmtree(storage)


def set_tree_access(
    path: Path,
    uid: int,
    gid: int,
    *,
    directory_mode: int,
    file_mode: int,
) -> None:
    if os.name != "posix" or not hasattr(os, "chown") or os.geteuid() != 0:
        return
    for root, directories, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        os.chown(root_path, uid, gid, follow_symlinks=False)
        root_path.chmod(directory_mode)
        for name in directories:
            child = root_path / name
            if child.is_symlink():
                raise SafetyError(f"Enlace inesperado al aplicar permisos: {child}")
            os.chown(child, uid, gid, follow_symlinks=False)
            child.chmod(directory_mode)
        for name in files:
            child = root_path / name
            if child.is_symlink():
                raise SafetyError(f"Enlace inesperado al aplicar permisos: {child}")
            os.chown(child, uid, gid, follow_symlinks=False)
            child.chmod(file_mode)


def apply_container_ownership(runtime: Path) -> None:
    # Numeric IDs are verified against the pinned production images.
    policies = (
        ("n8n/home", 1000, 1000, 0o700, 0o600),
        ("n8n/postgres", 999, 999, 0o700, 0o600),
        ("logs/n8n", 1000, 1000, 0o750, 0o640),
        ("minecraft", 1000, 1000, 0o755, 0o644),
        ("passbolt/gpg", 33, 33, 0o750, 0o440),
        ("passbolt/jwt", 33, 33, 0o750, 0o440),
        ("passbolt/postgres", 70, 70, 0o700, 0o600),
        ("logs/passbolt", 33, 33, 0o750, 0o640),
        ("logs/n8n-postgres", 999, 999, 0o750, 0o640),
        ("logs/passbolt-postgres", 70, 70, 0o750, 0o640),
        ("logs/shlink-postgres", 70, 70, 0o750, 0o640),
        ("logs/shlink", 1001, 0, 0o750, 0o640),
        ("shlink/postgres", 70, 70, 0o700, 0o600),
        ("openclaw-clean", 1000, 1000, 0o700, 0o600),
        ("logs/openclaw", 1000, 1000, 0o750, 0o640),
    )
    for relative, uid, gid, directory_mode, file_mode in policies:
        set_tree_access(
            runtime / relative,
            uid,
            gid,
            directory_mode=directory_mode,
            file_mode=file_mode,
        )


def prepare(stage: Path, runtime_root: Path) -> None:
    os.umask(0o077)
    stage = stage.resolve(strict=True)
    require_stage(stage)
    if not runtime_root.is_absolute() or runtime_root == Path("/"):
        raise SafetyError("RUNTIME_ROOT debe ser una ruta absoluta distinta de /")
    if runtime_root.is_symlink():
        raise SafetyError("RUNTIME_ROOT no puede ser un enlace")
    if runtime_root == DEFAULT_SERVICES_ROOT and os.geteuid() != 0:
        raise SafetyError(
            "El runtime canónico requiere root para aplicar ownership numérico"
        )
    if runtime_root.exists() and (
        not runtime_root.is_dir() or any(runtime_root.iterdir())
    ):
        raise SafetyError("RUNTIME_ROOT debe estar ausente o vacío")
    runtime_root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{runtime_root.name}.preparing.", dir=runtime_root.parent
        )
    )
    committed = False
    try:
        for relative in (
            *SERVICE_DATASET_DIRECTORIES.values(),
            "secrets/files",
            "restore-state",
        ):
            (temporary / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in LOG_DIRECTORIES:
            (temporary / "logs" / name).mkdir(mode=0o700, parents=True)

        filesystems = stage / "filesystems"
        extract_gzip(
            filesystems / "n8n-home.tar.gz",
            temporary / SERVICE_DATASET_DIRECTORIES["n8n-home"],
            {".n8n"},
            strip_root=True,
        )
        remove_legacy_n8n_storage_wheels(
            temporary / SERVICE_DATASET_DIRECTORIES["n8n-home"]
        )
        extract_gzip(
            filesystems / "passbolt-keys.tar.gz",
            temporary / "passbolt",
            {"gpg", "jwt"},
            strip_root=False,
        )
        extract_zstd(
            filesystems / "minecraft-data.tar.zst",
            temporary / SERVICE_DATASET_DIRECTORIES["minecraft-worlds"],
            {"minecraft"},
            strip_root=True,
        )
        extract_zstd(
            filesystems / "minecraft-bind-mods.tar.zst",
            temporary / SERVICE_DATASET_DIRECTORIES["minecraft-mods"],
            {"mods"},
            strip_root=True,
        )
        copy_recovery_artifacts(stage, temporary)

        generated = {name: secrets.token_urlsafe(48) for name in DIRECT_SECRET_NAMES}
        for name, value in generated.items():
            write_private(
                temporary / "secrets/files" / name,
                (value + "\n").encode("ascii"),
            )
        write_private(
            temporary / "secrets/files" / SECRET_IDENTITY_KEY_FILE,
            secrets.token_bytes(SECRET_IDENTITY_KEY_SIZE),
        )

        decoded: dict[str, dict[str, str]] = {}
        for app, keys in APPROVED_ENV_KEYS.items():
            decoded[app] = decode_approved_namespace_secrets(
                stage, NAMESPACE_FOR_APP[app], keys
            )
        decoded["n8n"]["N8N_ENCRYPTION_KEY"] = n8n_encryption_key(
            temporary, decoded["n8n"]
        )
        if not decoded["passbolt"].get("SECURITY_SALT"):
            raise SafetyError("SECURITY_SALT de Passbolt no está en secrets aprobados")

        imported_secret_files = write_approved_secret_files(
            temporary / "secrets/files",
            decoded,
        )
        secret_files = sorted(set(generated) | set(imported_secret_files))

        manifest = {
            "schemaVersion": 4,
            "preparedAt": datetime.now(timezone.utc).isoformat(),
            "sourceBackup": stage.name,
            "openClawLegacyImported": False,
            "openClawState": "clean-generated-token",
            "runtimeRootContract": str(DEFAULT_SERVICES_ROOT),
            "secretDelivery": "individual-docker-secret-source-files",
            "secretFiles": secret_files,
            "secretIdentityKeyFile": SECRET_IDENTITY_KEY_FILE,
            "edgeRecoveryArtifacts": [
                "recovery/edge/traefik-acme.json",
            ],
            "numericOwnershipApplied": os.geteuid() == 0,
        }
        write_private(
            temporary / "smoke-targets.txt",
            (
                "# These canonical URLs verify the active DNS destination.\n"
                "# Before cutover, use curl --resolve against the Netcup IP.\n"
                "https://kropia.apptolast.com/health 200\n"
                "https://minecraft-stats.apptolast.com/actuator/health 200\n"
                "https://n8n.apptolast.com/healthz 200\n"
                "https://passbolt.apptolast.com/healthcheck/status.json 200\n"
                "https://generadorcodigosqr.apptolast.com/rest/health 200\n"
                "https://pablohurtadohg.apptolast.com/ 200\n"
                "https://albertohidalgo.apptolast.com/ 200\n"
            ).encode("ascii"),
        )
        write_private(
            temporary / "runtime-manifest.json",
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        verify_checksum_tree(temporary / "recovery", temporary / "recovery/SHA256SUMS")
        apply_container_ownership(temporary)
        if runtime_root.exists():
            runtime_root.rmdir()
        os.replace(temporary, runtime_root)
        committed = True
    finally:
        if not committed and temporary.exists():
            shutil.rmtree(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("staging")
    parser.add_argument("--root", default=str(DEFAULT_SERVICES_ROOT))
    args = parser.parse_args()
    runtime_root = Path(os.path.abspath(args.root))
    if runtime_root == DEFAULT_SERVICES_ROOT:
        ensure_mutation_lock(
            "migration-prepare-runtime",
            [
                sys.executable,
                str(Path(__file__).resolve()),
                *sys.argv[1:],
            ],
            caller=Path(__file__),
        )
    try:
        prepare(Path(args.staging), runtime_root)
    except (SafetyError, OSError, tarfile.TarError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        "OK: datos verificados y fuentes de Docker secrets preparados sin "
        "importar estado legado de OpenClaw"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
