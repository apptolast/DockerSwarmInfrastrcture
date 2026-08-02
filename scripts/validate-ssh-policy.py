#!/usr/bin/env python3
"""Render and, as root, parse the two deterministic OpenSSH policies."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONTRACT_PATH = PROJECT_DIR / "config/host-security.yml"
TEMPLATE_DIR = PROJECT_DIR / "ansible/templates"
POLICIES = {
    "staged": (
        "ssh-bootstrap-staged.conf.j2",
        "prohibit-password",
        ["root", "admin"],
    ),
    "final": ("ssh-hardening.conf.j2", "no", ["admin"]),
}


class SshPolicyError(RuntimeError):
    """The rendered or effective OpenSSH policy is unsafe."""


def load_contract() -> dict[str, Any]:
    try:
        document = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise SshPolicyError("cannot load the host-security contract") from error
    if not isinstance(document, dict):
        raise SshPolicyError("host-security contract must be a mapping")
    return document


def render(template_name: str, contract: dict[str, Any]) -> str:
    try:
        source = (TEMPLATE_DIR / template_name).read_text(encoding="utf-8")
        rendered = (
            Environment(
                undefined=StrictUndefined,
                autoescape=False,
                keep_trailing_newline=True,
            )
            .from_string(source)
            .render(**contract)
        )
    except (OSError, ValueError) as error:
        raise SshPolicyError(f"cannot render {template_name}") from error
    if "Include " in rendered or "\r" in rendered or "\x00" in rendered:
        raise SshPolicyError(f"{template_name} is not self-contained")
    return rendered


def parse_effective(output: str) -> dict[str, list[str]]:
    effective: dict[str, list[str]] = {}
    for line in output.splitlines():
        key, separator, value = line.partition(" ")
        if not separator or not key or not value:
            raise SshPolicyError("sshd returned a malformed effective setting")
        effective.setdefault(key, []).append(value)
    return effective


def require_one(effective: dict[str, list[str]], key: str) -> str:
    values = effective.get(key, [])
    if len(values) != 1:
        raise SshPolicyError(f"sshd returned {len(values)} values for {key}")
    return values[0]


def validate_effective(
    effective: dict[str, list[str]],
    contract: dict[str, Any],
    permit_root_login: str,
    allow_users: list[str],
) -> None:
    exact_lists = {
        "kexalgorithms": (
            contract["host_security_ssh_pq_kex_algorithms"]
            + contract["host_security_ssh_classical_kex_algorithms"]
        ),
        "ciphers": contract["host_security_ssh_ciphers"],
        "macs": contract["host_security_ssh_macs"],
        "hostkeyalgorithms": contract["host_security_ssh_host_key_algorithms"],
        "pubkeyacceptedalgorithms": contract["host_security_ssh_pubkey_algorithms"],
    }
    for key, expected in exact_lists.items():
        if require_one(effective, key).split(",") != expected:
            raise SshPolicyError(f"effective {key} differs from the contract")

    exact_scalars = {
        "permitrootlogin": permit_root_login,
        "pubkeyauthentication": "yes",
        "passwordauthentication": "no",
        "kbdinteractiveauthentication": "no",
        "authenticationmethods": "publickey",
        "requiredrsasize": "2048",
        "disableforwarding": "yes",
        "x11forwarding": "no",
        "allowagentforwarding": "no",
        "allowtcpforwarding": "no",
        "allowstreamlocalforwarding": "no",
        "permittunnel": "no",
        "permituserenvironment": "no",
        "permituserrc": "no",
    }
    for key, expected in exact_scalars.items():
        if require_one(effective, key) != expected:
            raise SshPolicyError(f"effective {key} differs from the contract")
    effective_allow_users = [
        user for value in effective.get("allowusers", []) for user in value.split()
    ]
    if effective_allow_users != allow_users:
        raise SshPolicyError("effective AllowUsers differs from the phase")


def validate_runtime(sshd: str, contract: dict[str, Any]) -> None:
    for phase, (template_name, permit_root_login, allow_users) in POLICIES.items():
        completed = subprocess.run(
            [sshd, "-T", "-f", "/dev/stdin"],
            input=render(template_name, contract),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "no diagnostic"
            raise SshPolicyError(f"sshd rejected {phase} policy: {detail}")
        validate_effective(
            parse_effective(completed.stdout),
            contract,
            permit_root_login,
            allow_users,
        )


def validate_rendered(contract: dict[str, Any]) -> None:
    for phase, (template_name, _permit_root_login, allow_users) in POLICIES.items():
        rendered = render(template_name, contract)
        required_lines = {
            "AuthenticationMethods publickey",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "RequiredRSASize 2048",
            f"AllowUsers {' '.join(allow_users)}",
        }
        missing = required_lines.difference(rendered.splitlines())
        if missing:
            raise SshPolicyError(
                f"{phase} policy is missing: {', '.join(sorted(missing))}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runtime",
        action="store_true",
        help="also parse both policies with the installed sshd (requires root)",
    )
    parser.add_argument("--sshd", default="/usr/sbin/sshd")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        contract = load_contract()
        validate_rendered(contract)
        if args.runtime:
            validate_runtime(args.sshd, contract)
    except SshPolicyError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    suffix = " and sshd runtime" if args.runtime else ""
    print(f"Staged and final OpenSSH policies passed render{suffix} validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
