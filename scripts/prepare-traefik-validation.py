#!/usr/bin/env python3
"""Prepare the rendered edge configuration for a disposable Traefik boot.

scripts/validate-traefik-config.sh boots the pinned Traefik once more with
the whole rendered dynamic configuration, so an option Traefik rejects or
drops (a serversTransport that falls back to the default TLS configuration,
an unknown key, a middleware that cannot be built) fails validation instead
of the production apply. Two things cannot run in that throwaway container
and are removed here, and nothing else:

- the ACME resolver and every reference to it: it would ask Let's Encrypt
  production for one certificate per hostname with a placeholder token;
- every backend ``healthCheck``: the Swarm and kind names do not resolve
  there, and each failed probe logs a WARN.

Every file under ``/run/secrets`` that the dynamic configuration names gets
a throwaway stand-in of the same kind: a users file for ``basicAuth``, a CA
certificate for ``rootCAs`` and one PEM with a client certificate and its
key for ``certificates``, as the real secrets are laid out (docs/EDGE.md).
A path used in any other way fails closed, so a new kind of secret is
reviewed here first. Nothing generated here is a credential: the password
and the CA key are random and discarded.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

SECRETS_DIR = "/run/secrets/"
SECRET_NAME = re.compile(r"[a-z0-9_]{1,64}")
# The container runs as 65532:65532 and reads these through bind mounts.
DIRECTORY_MODE = 0o755
FILE_MODE = 0o644


class PreparationError(Exception):
    """The render holds something this preparation has not reviewed."""


def strip_static(static: dict[str, Any]) -> dict[str, Any]:
    """The static configuration without the ACME resolver."""
    if not isinstance(static.get("certificatesResolvers"), dict):
        raise PreparationError("the static configuration has no ACME resolver")
    del static["certificatesResolvers"]
    for entry_point in static.get("entryPoints", {}).values():
        tls = (entry_point.get("http") or {}).get("tls")
        if isinstance(tls, dict):
            tls.pop("certResolver", None)
    return static


def strip_dynamic(dynamic: dict[str, Any]) -> dict[str, Any]:
    """The dynamic configuration without certResolver and healthCheck."""
    http = dynamic.get("http") or {}
    for router in (http.get("routers") or {}).values():
        if isinstance(router.get("tls"), dict):
            router["tls"].pop("certResolver", None)
    for service in (http.get("services") or {}).values():
        balancer = service.get("loadBalancer")
        if isinstance(balancer, dict):
            balancer.pop("healthCheck", None)
    return dynamic


def remaining(document: Any, key: str) -> bool:
    if isinstance(document, dict):
        return key in document or any(
            remaining(value, key) for value in document.values()
        )
    if isinstance(document, list):
        return any(remaining(value, key) for value in document)
    return False


def secret_target(path: str) -> str:
    target = path.removeprefix(SECRETS_DIR)
    if not path.startswith(SECRETS_DIR) or not SECRET_NAME.fullmatch(target):
        raise PreparationError(f"unreviewed secret path {path!r}")
    return target


def strings(document: Any, path: tuple[Any, ...] = ()) -> Any:
    """Yield the key path and value of every string in a document."""
    if isinstance(document, dict):
        for key, value in document.items():
            yield from strings(value, (*path, key))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            yield from strings(value, (*path, index))
    elif isinstance(document, str):
        yield path, document


def secret_kind(path: tuple[Any, ...]) -> str | None:
    """The kind of file a reviewed location holds, if it is one."""
    match path:
        case ("http", "middlewares", _, "basicAuth", "usersFile"):
            return "users"
        case ("http", "serversTransports", _, "rootCAs", int()):
            return "ca"
        case ("http", "serversTransports", _, "certificates", int(), key) if key in (
            "certFile",
            "keyFile",
        ):
            return "client"
    return None


def secret_kinds(dynamic: dict[str, Any]) -> dict[str, str]:
    """Map each /run/secrets file of the dynamic configuration to its kind."""
    kinds: dict[str, str] = {}
    for path, value in strings(dynamic):
        if SECRETS_DIR not in value:
            continue
        kind = secret_kind(path)
        if kind is None:
            raise PreparationError(f"{value} is used in an unreviewed place")
        target = secret_target(value)
        if kinds.setdefault(target, kind) != kind:
            raise PreparationError(f"{value} is used as two kinds of file")
    for transport in (dynamic.get("http", {}).get("serversTransports") or {}).values():
        for pair in transport.get("certificates") or []:
            if pair.get("certFile") != pair.get("keyFile"):
                raise PreparationError(
                    "a client certificate and its key are not one PEM file"
                )
    return kinds


def throwaway_users() -> bytes:
    # SHA1 is one of the hash formats basicAuth accepts (Traefik v3.7
    # basicAuth reference); the password is random and never kept.
    digest = hashlib.sha1(os.urandom(32), usedforsecurity=False).digest()
    return b"validation:{SHA}" + base64.b64encode(digest) + b"\n"


def throwaway_mtls() -> tuple[bytes, bytes]:
    """A CA certificate and a client certificate followed by its key."""
    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = now - datetime.timedelta(minutes=5)
    not_after = now + datetime.timedelta(days=1)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "validation CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    client_key = ec.generate_private_key(ec.SECP256R1())
    client = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "validation client")])
        )
        .issuer_name(ca_name)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    client_pem = client.public_bytes(
        serialization.Encoding.PEM
    ) + client_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return ca.public_bytes(serialization.Encoding.PEM), client_pem


def write(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
    path.chmod(FILE_MODE)


def prepare(render_dir: Path, output_dir: Path) -> dict[str, str]:
    static = yaml.safe_load((render_dir / "static.yml").read_text(encoding="utf-8"))
    dynamic = yaml.safe_load((render_dir / "dynamic.yml").read_text(encoding="utf-8"))
    if not isinstance(static, dict) or not isinstance(dynamic, dict):
        raise PreparationError("the rendered Traefik files are not mappings")
    static = strip_static(static)
    dynamic = strip_dynamic(dynamic)
    for document in (static, dynamic):
        for key in ("certResolver", "certificatesResolvers", "healthCheck"):
            if remaining(document, key):
                raise PreparationError(f"an unreviewed {key} is left in the render")
    kinds = secret_kinds(dynamic)

    output_dir.mkdir(mode=DIRECTORY_MODE)
    output_dir.chmod(DIRECTORY_MODE)
    secrets_dir = output_dir / "secrets"
    secrets_dir.mkdir(mode=DIRECTORY_MODE)
    secrets_dir.chmod(DIRECTORY_MODE)
    for name, document in (("static.yml", static), ("dynamic.yml", dynamic)):
        write(
            output_dir / name,
            yaml.safe_dump(document, sort_keys=False).encode("utf-8"),
        )
    ca_pem, client_pem = throwaway_mtls()
    for target, kind in sorted(kinds.items()):
        if kind == "users":
            payload = throwaway_users()
        elif kind == "ca":
            payload = ca_pem
        else:
            payload = client_pem
        write(secrets_dir / target, payload)
    return kinds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("render_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    arguments = parser.parse_args(argv)
    try:
        kinds = prepare(arguments.render_dir, arguments.output_dir)
    except (OSError, yaml.YAMLError, PreparationError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    for target, kind in sorted(kinds.items()):
        print(f"{target} {kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
