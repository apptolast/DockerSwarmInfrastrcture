#!/usr/bin/env python3
"""Strict validation helpers for the migration backup format.

This module intentionally uses only the Python standard library so it can run on
the destination before application dependencies are installed.  It never reads
or prints secret values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import BinaryIO

PACKAGE_RE = re.compile(
    r"^apptolast-data-(?P<stamp>[0-9]{8}T[0-9]{6}Z)\.tar\.zst\.gpg$"
)
TAG_RE = re.compile(r"^backup-(?P<stamp>[0-9]{8}T[0-9]{6}Z)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
KEY_RE = re.compile(rb"^[A-Za-z0-9+/]+={0,2}\n?$")
STAGE_RE = re.compile(r"^apptolast-data-[0-9]{8}T[0-9]{6}Z$")
MANIFEST_FIELDS = {
    "schemaVersion",
    "tag",
    "package",
    "packageSize",
    "packageSha256",
    "recoveryKeyAsset",
    "partCount",
    "partSize",
}
PART_BYTES = 1024 * 1024 * 1024
MAX_PACKAGE_BYTES = int(
    os.environ.get("BACKUP_MAX_PACKAGE_BYTES", str(512 * 1024 * 1024 * 1024))
)
MAX_TAR_MEMBERS = int(os.environ.get("BACKUP_MAX_TAR_MEMBERS", "1000000"))
MAX_TAR_UNPACKED_BYTES = int(
    os.environ.get("BACKUP_MAX_TAR_UNPACKED_BYTES", str(1024**4))
)


class SafetyError(RuntimeError):
    """Raised when an artifact does not meet the strict backup contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_regular_file(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise SafetyError(f"Falta {label}: {path}") from exc
    if not stat.S_ISREG(mode):
        raise SafetyError(f"{label} no es un fichero regular: {path}")


def safe_basename(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SafetyError(f"{field} debe ser una cadena no vacía")
    if value != os.path.basename(value) or "/" in value or "\\" in value:
        raise SafetyError(f"{field} debe ser un basename seguro")
    if value in {".", ".."} or "\x00" in value:
        raise SafetyError(f"{field} contiene una ruta no permitida")
    return value


def load_manifest(path: Path) -> dict[str, object]:
    require_regular_file(path, "RELEASE-MANIFEST.json")
    if path.stat().st_size > 64 * 1024:
        raise SafetyError("RELEASE-MANIFEST.json excede 64 KiB")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafetyError("RELEASE-MANIFEST.json no es JSON UTF-8 válido") from exc
    if not isinstance(value, dict):
        raise SafetyError("RELEASE-MANIFEST.json debe contener un objeto")
    fields = set(value)
    if fields != MANIFEST_FIELDS:
        missing = sorted(MANIFEST_FIELDS - fields)
        extra = sorted(fields - MANIFEST_FIELDS)
        raise SafetyError(
            f"Campos de manifiesto inválidos; faltan={missing}, sobran={extra}"
        )
    if value["schemaVersion"] != 1:
        raise SafetyError("schemaVersion debe ser exactamente 1")

    tag = value["tag"]
    package = safe_basename(value["package"], "package")
    key_name = safe_basename(value["recoveryKeyAsset"], "recoveryKeyAsset")
    tag_match = TAG_RE.fullmatch(tag) if isinstance(tag, str) else None
    package_match = PACKAGE_RE.fullmatch(package)
    if tag_match is None or package_match is None:
        raise SafetyError("Tag o nombre de paquete fuera del formato admitido")
    if tag_match.group("stamp") != package_match.group("stamp"):
        raise SafetyError("El timestamp del tag no coincide con el paquete")
    expected_key = package.removesuffix(".tar.zst.gpg") + ".RECOVERY-KEY.txt"
    if key_name != expected_key:
        raise SafetyError("La clave no corresponde al paquete")

    package_size = value["packageSize"]
    part_count = value["partCount"]
    if (
        not isinstance(package_size, int)
        or isinstance(package_size, bool)
        or not 1 <= package_size <= MAX_PACKAGE_BYTES
    ):
        raise SafetyError("packageSize está fuera de límites")
    if (
        not isinstance(part_count, int)
        or isinstance(part_count, bool)
        or not 1 <= part_count <= 999
    ):
        raise SafetyError("partCount debe estar entre 1 y 999")
    expected_parts = (package_size + PART_BYTES - 1) // PART_BYTES
    if part_count != expected_parts:
        raise SafetyError("partCount no corresponde a packageSize")
    if value["partSize"] != "1GiB":
        raise SafetyError("partSize debe ser exactamente 1GiB")
    if not isinstance(value["packageSha256"], str) or not SHA256_RE.fullmatch(
        value["packageSha256"]
    ):
        raise SafetyError("packageSha256 no es un SHA-256 canónico")
    return value


def normalize_checksum_name(name: str, *, basename_only: bool) -> str:
    name = name.removeprefix("./")
    if not name or "\x00" in name or "\r" in name or "\n" in name:
        raise SafetyError("Nombre vacío o de control en fichero de checksums")
    if "\\" in name:
        raise SafetyError(f"Ruta con barra inversa no permitida: {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise SafetyError(f"Ruta insegura en fichero de checksums: {name!r}")
    normalized = pure.as_posix()
    if basename_only and len(pure.parts) != 1:
        raise SafetyError(f"Se esperaba un basename en checksums: {name!r}")
    return normalized


def parse_checksums(path: Path, *, basename_only: bool = False) -> dict[str, str]:
    require_regular_file(path, "fichero de checksums")
    if path.stat().st_size > 128 * 1024 * 1024:
        raise SafetyError("El fichero de checksums excede 128 MiB")
    checksums: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise SafetyError("El fichero de checksums no es UTF-8") from exc
    if not lines:
        raise SafetyError("El fichero de checksums está vacío")
    for line_number, line in enumerate(lines, start=1):
        if len(line) < 67 or line[64:66] != "  ":
            raise SafetyError(f"Formato SHA-256 inválido en línea {line_number}")
        digest = line[:64]
        if not SHA256_RE.fullmatch(digest):
            raise SafetyError(f"SHA-256 inválido en línea {line_number}")
        name = normalize_checksum_name(line[66:], basename_only=basename_only)
        if name in checksums:
            raise SafetyError(f"Entrada de checksum duplicada: {name}")
        checksums[name] = digest
    return checksums


def tree_regular_files(root: Path, excluded: Path | None = None) -> set[str]:
    if root.is_symlink() or not root.is_dir():
        raise SafetyError(f"La raíz no es un directorio real: {root}")
    root = root.resolve()
    excluded_resolved = excluded.resolve() if excluded is not None else None
    files: set[str] = set()
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise SafetyError(f"No se admiten enlaces en el staging: {path}")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise SafetyError(f"Tipo de fichero no admitido: {path}")
        resolved = path.resolve()
        if excluded_resolved is not None and resolved == excluded_resolved:
            continue
        files.add(path.relative_to(root).as_posix())
    return files


def verify_checksum_tree(root: Path, checksum_path: Path) -> int:
    root = root.resolve()
    checksum_path = checksum_path.resolve()
    try:
        checksum_path.relative_to(root)
    except ValueError as exc:
        raise SafetyError("SHA256SUMS debe estar dentro del staging") from exc
    checksums = parse_checksums(checksum_path)
    actual = tree_regular_files(root, checksum_path)
    expected = set(checksums)
    if actual != expected:
        missing = sorted(expected - actual)[:20]
        uncovered = sorted(actual - expected)[:20]
        raise SafetyError(
            f"Cobertura SHA-256 inexacta; faltan={missing}, no_cubiertos={uncovered}"
        )
    for name, expected_digest in checksums.items():
        path = root.joinpath(*PurePosixPath(name).parts)
        if sha256_file(path) != expected_digest:
            raise SafetyError(f"SHA-256 incorrecto: {name}")
    return len(checksums)


def release_part_names(package: str, count: int) -> list[str]:
    return [f"{package}.part-{index:03d}.bin" for index in range(count)]


def validate_recovery_key(path: Path) -> None:
    require_regular_file(path, "clave de recuperación")
    data = path.read_bytes()
    if not 32 <= len(data) <= 4096 or KEY_RE.fullmatch(data) is None:
        raise SafetyError("La clave de recuperación no tiene formato seguro")


def validate_release_directory(directory: Path) -> dict[str, object]:
    if directory.is_symlink() or not directory.is_dir():
        raise SafetyError("El directorio de release no es válido")
    manifest = load_manifest(directory / "RELEASE-MANIFEST.json")
    package = str(manifest["package"])
    key_name = str(manifest["recoveryKeyAsset"])
    part_names = release_part_names(package, int(manifest["partCount"]))
    expected_names = set(part_names) | {
        key_name,
        "PARTS_SHA256SUMS",
        "ORIGINAL_SHA256SUMS",
        "RELEASE-MANIFEST.json",
        "README-RELEASE.txt",
    }
    actual_names: set[str] = set()
    for path in directory.iterdir():
        require_regular_file(path, "asset de release")
        actual_names.add(path.name)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise SafetyError(f"Assets inesperados; faltan={missing}, sobran={extra}")

    validate_recovery_key(directory / key_name)
    part_checksums = parse_checksums(directory / "PARTS_SHA256SUMS", basename_only=True)
    if set(part_checksums) != set(part_names):
        raise SafetyError("PARTS_SHA256SUMS no cubre exactamente todas las partes")
    package_size = int(manifest["packageSize"])
    observed_size = 0
    for index, name in enumerate(part_names):
        path = directory / name
        size = path.stat().st_size
        expected_size = (
            PART_BYTES
            if index < len(part_names) - 1
            else package_size - PART_BYTES * (len(part_names) - 1)
        )
        if size != expected_size:
            raise SafetyError(f"Tamaño incorrecto para {name}")
        if sha256_file(path) != part_checksums[name]:
            raise SafetyError(f"SHA-256 incorrecto para {name}")
        observed_size += size
    if observed_size != package_size:
        raise SafetyError("Las partes no suman packageSize")

    original = parse_checksums(directory / "ORIGINAL_SHA256SUMS", basename_only=True)
    if original != {package: str(manifest["packageSha256"])}:
        raise SafetyError("ORIGINAL_SHA256SUMS no coincide con el manifiesto")
    return manifest


def reassemble_release(directory: Path) -> Path:
    manifest = validate_release_directory(directory)
    package = str(manifest["package"])
    target = directory / package
    if target.exists() or target.is_symlink():
        raise SafetyError(f"Se rechaza sobrescribir el paquete: {target}")
    part_names = release_part_names(package, int(manifest["partCount"]))
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=directory, prefix=f".{package}.assembling.", delete=False
        ) as output:
            temporary = Path(output.name)
            for name in part_names:
                with (directory / name).open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if temporary.stat().st_size != int(manifest["packageSize"]):
            raise SafetyError("Tamaño incorrecto tras recomponer el paquete")
        if sha256_file(temporary) != str(manifest["packageSha256"]):
            raise SafetyError("SHA-256 incorrecto tras recomponer el paquete")
        os.replace(temporary, target)
        temporary = None
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def safe_tar_name(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise SafetyError("Nombre tar vacío o inseguro")
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise SafetyError(f"Ruta insegura en tar: {name!r}")
    return pure


def ensure_empty_real_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise SafetyError(f"El destino de extracción no es un directorio real: {path}")
    if any(path.iterdir()):
        raise SafetyError(f"El destino de extracción no está vacío: {path}")


def open_output_file(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, "wb")


def process_tar_stream(fileobj: BinaryIO, destination: Path | None) -> str:
    if destination is not None:
        ensure_empty_real_directory(destination)
        destination = destination.resolve()
    roots: set[str] = set()
    names: set[str] = set()
    total_size = 0
    count = 0
    required_checksum: str | None = None
    with tarfile.open(fileobj=fileobj, mode="r|") as archive:
        for member in archive:
            count += 1
            if count > MAX_TAR_MEMBERS:
                raise SafetyError("El tar excede el límite de entradas")
            pure = safe_tar_name(member.name.rstrip("/"))
            normalized = pure.as_posix()
            if normalized in names:
                raise SafetyError(f"Entrada tar duplicada: {normalized}")
            names.add(normalized)
            roots.add(pure.parts[0])
            if len(roots) != 1:
                raise SafetyError("El tar debe contener exactamente una raíz")
            if not STAGE_RE.fullmatch(pure.parts[0]):
                raise SafetyError("La raíz del tar no es un backup reconocido")
            if normalized == f"{pure.parts[0]}/SHA256SUMS":
                required_checksum = normalized
            if member.isdir():
                if destination is not None:
                    target = destination.joinpath(*pure.parts)
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if target.is_symlink() or not target.is_dir():
                        raise SafetyError(
                            f"Directorio inseguro al extraer: {normalized}"
                        )
                    target.chmod(0o700)
                continue
            if not member.isfile():
                raise SafetyError(
                    f"Tipo tar no permitido para {normalized}: {member.type!r}"
                )
            if member.size < 0:
                raise SafetyError(f"Tamaño negativo en tar: {normalized}")
            total_size += member.size
            if total_size > MAX_TAR_UNPACKED_BYTES:
                raise SafetyError("El tar excede el límite de tamaño descomprimido")
            source = archive.extractfile(member)
            if source is None:
                raise SafetyError(f"No se pudo leer la entrada tar: {normalized}")
            if destination is None:
                while source.read(1024 * 1024):
                    pass
                continue
            target = destination.joinpath(*pure.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open_output_file(target) as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            target.chmod(0o600)
    if len(roots) != 1:
        raise SafetyError("El tar no contiene exactamente una raíz")
    root = next(iter(roots))
    if required_checksum != f"{root}/SHA256SUMS":
        raise SafetyError("El tar no contiene SHA256SUMS en la raíz")
    return root


def verify_single_package_checksum(package: Path, checksum: Path) -> None:
    require_regular_file(package, "paquete cifrado")
    entries = parse_checksums(checksum, basename_only=True)
    if entries != {package.name: sha256_file(package)}:
        raise SafetyError("El checksum externo no corresponde exactamente al paquete")


def command_validate_manifest(args: argparse.Namespace) -> None:
    manifest = load_manifest(Path(args.manifest))
    if args.tag is not None and manifest["tag"] != args.tag:
        raise SafetyError("El tag solicitado no coincide con el manifiesto")
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":")))


def command_validate_release(args: argparse.Namespace) -> None:
    manifest = validate_release_directory(Path(args.directory))
    if args.tag is not None and manifest["tag"] != args.tag:
        raise SafetyError("El tag solicitado no coincide con el manifiesto")
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":")))


def command_reassemble(args: argparse.Namespace) -> None:
    path = reassemble_release(Path(args.directory))
    print(path.name)


def command_verify_tree(args: argparse.Namespace) -> None:
    root = Path(args.root)
    checksum = Path(args.checksum) if args.checksum else root / "SHA256SUMS"
    count = verify_checksum_tree(root, checksum)
    print(count)


def command_verify_package_checksum(args: argparse.Namespace) -> None:
    verify_single_package_checksum(Path(args.package), Path(args.checksum))
    print("OK")


def command_process_tar(args: argparse.Namespace, *, extract: bool) -> None:
    destination = Path(args.destination) if extract else None
    root = process_tar_stream(sys.stdin.buffer, destination)
    print(root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_manifest = subparsers.add_parser("validate-manifest")
    validate_manifest.add_argument("manifest")
    validate_manifest.add_argument("--tag")
    validate_manifest.set_defaults(handler=command_validate_manifest)

    validate_release = subparsers.add_parser("validate-release")
    validate_release.add_argument("directory")
    validate_release.add_argument("--tag")
    validate_release.set_defaults(handler=command_validate_release)

    reassemble = subparsers.add_parser("reassemble-release")
    reassemble.add_argument("directory")
    reassemble.set_defaults(handler=command_reassemble)

    verify_tree = subparsers.add_parser("verify-tree")
    verify_tree.add_argument("root")
    verify_tree.add_argument("checksum", nargs="?")
    verify_tree.set_defaults(handler=command_verify_tree)

    verify_package = subparsers.add_parser("verify-package-checksum")
    verify_package.add_argument("package")
    verify_package.add_argument("checksum")
    verify_package.set_defaults(handler=command_verify_package_checksum)

    inspect_tar = subparsers.add_parser("inspect-tar-stream")
    inspect_tar.set_defaults(
        handler=lambda args: command_process_tar(args, extract=False)
    )

    extract_tar = subparsers.add_parser("safe-extract-tar-stream")
    extract_tar.add_argument("destination")
    extract_tar.set_defaults(
        handler=lambda args: command_process_tar(args, extract=True)
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (SafetyError, OSError, tarfile.TarError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
