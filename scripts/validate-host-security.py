#!/usr/bin/env python3
"""Validate the age-bounded, reproducible host-security contract."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONTRACT_PATH = PROJECT_DIR / "config/host-security.yml"


STANDALONE_HUB_TYPES = ("parsers", "scenarios", "postoverflows")
HUB_VERSION = re.compile(r"[0-9]+\.[0-9]+")
SCENARIO_NAME = re.compile(r"[a-z0-9-]+/[a-z0-9-]+")
TRAEFIK_ROUTER = re.compile(r"[a-z0-9-]+@file")
BUCKET_DURATION = re.compile(r"[1-9][0-9]*[sm]")
BAN_DURATION = re.compile(r"([0-9]+)m")
ALLOWLIST_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
ALLOWLIST_DESCRIPTION = re.compile(r"[A-Za-z0-9 ._-]{1,100}")
ALLOWLIST_SOURCE = re.compile(r"/etc/dockerswarm/crowdsec/[a-z0-9][a-z0-9-]*")
ACCESS_LOG = re.compile(r"/var/log/dockerswarm/[a-z0-9-]+/[a-z0-9-]+\.log")


class HostSecurityContractError(RuntimeError):
    """The host-security contract is unsafe or internally inconsistent."""


def require_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HostSecurityContractError(f"{context} must be a mapping")
    return value


def require_match(pattern: re.Pattern[str], value: Any, message: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise HostSecurityContractError(message)
    return value


def validate_traefik_basicauth(contract: dict[str, Any], hub_names: set[str]) -> None:
    """Check the bounded short-ban policy for Traefik basicAuth 401s."""
    # A file source only: a CrowdSec 1.7.8 Docker source that loses the
    # daemon for about 15 minutes stops every acquisition, SSH included.
    require_match(
        ACCESS_LOG,
        contract.get("host_security_crowdsec_traefik_access_log"),
        "the Traefik access log must be a plain .log file two levels below "
        "/var/log/dockerswarm",
    )
    if any(
        key.startswith("host_security_crowdsec_")
        and ("docker" in key or "container" in key)
        for key in contract
    ):
        raise HostSecurityContractError(
            "CrowdSec must not read Traefik through the Docker API"
        )
    scenario = require_match(
        SCENARIO_NAME,
        contract.get("host_security_crowdsec_basicauth_scenario"),
        "the basicAuth scenario needs an author/name identity",
    )
    if scenario in hub_names:
        raise HostSecurityContractError(
            "the local basicAuth scenario must not shadow a Hub item"
        )
    routers = contract.get("host_security_crowdsec_basicauth_routers")
    if (
        not isinstance(routers, list)
        or not routers
        or len(set(map(str, routers))) != len(routers)
    ):
        raise HostSecurityContractError(
            "the basicAuth routers must be a non-empty list without duplicates"
        )
    for router in routers:
        require_match(
            TRAEFIK_ROUTER,
            router,
            "every basicAuth router must be a file-provider router name",
        )
    capacity = contract.get("host_security_crowdsec_basicauth_capacity")
    if isinstance(capacity, bool) or not isinstance(capacity, int):
        raise HostSecurityContractError("the basicAuth capacity must be an integer")
    if not 5 <= capacity <= 20:
        raise HostSecurityContractError(
            "the basicAuth capacity must stay between 5 and 20"
        )
    for key in (
        "host_security_crowdsec_basicauth_leakspeed",
        "host_security_crowdsec_basicauth_blackhole",
    ):
        require_match(BUCKET_DURATION, contract.get(key), f"{key} is not a duration")
    ban = require_match(
        BAN_DURATION,
        contract.get("host_security_crowdsec_basicauth_ban_duration"),
        "the basicAuth ban must be a whole number of minutes",
    )
    if not 15 <= int(ban[:-1]) <= 30:
        raise HostSecurityContractError(
            "the basicAuth ban must last between 15 and 30 minutes"
        )
    require_match(
        ALLOWLIST_NAME,
        contract.get("host_security_crowdsec_allowlist_name"),
        "the CrowdSec allowlist name is invalid",
    )
    require_match(
        ALLOWLIST_DESCRIPTION,
        contract.get("host_security_crowdsec_allowlist_description"),
        "the CrowdSec allowlist description is invalid",
    )
    require_match(
        ALLOWLIST_SOURCE,
        contract.get("host_security_crowdsec_allowlist_source"),
        "the CrowdSec allowlist source must be a file under /etc/dockerswarm/crowdsec",
    )


def validate(document: Any, now: datetime) -> None:
    contract = require_mapping(document, "host-security contract")
    if contract.get("host_security_apt_update_policy") != ("promoted-snapshot-only"):
        raise HostSecurityContractError("APT policy must be promoted-snapshot-only")
    maximum_age = contract.get("host_security_snapshot_max_age_days")
    if isinstance(maximum_age, bool) or maximum_age != 14:
        raise HostSecurityContractError("snapshot maximum age must be 14 days")
    raw_snapshot = contract.get("host_security_ubuntu_snapshot")
    if not isinstance(raw_snapshot, str):
        raise HostSecurityContractError("Ubuntu snapshot must be a string")
    try:
        snapshot = datetime.strptime(
            raw_snapshot,
            "%Y%m%dT%H%M%SZ",
        ).replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise HostSecurityContractError(
            "Ubuntu snapshot timestamp is invalid"
        ) from error
    age_seconds = (now.astimezone(timezone.utc) - snapshot).total_seconds()
    if age_seconds < 0:
        raise HostSecurityContractError("Ubuntu snapshot is in the future")
    if age_seconds > maximum_age * 86400:
        raise HostSecurityContractError(
            "Ubuntu snapshot exceeds the 14-day promotion SLO"
        )

    required_packages = contract.get("host_security_required_packages")
    package_lock = contract.get("host_security_package_lock")
    if not isinstance(required_packages, list) or not isinstance(
        package_lock,
        list,
    ):
        raise HostSecurityContractError("package contracts must be lists")
    if "unattended-upgrades" in required_packages:
        raise HostSecurityContractError(
            "unattended-upgrades conflicts with promoted snapshots"
        )
    locked_names = [
        item.split("=", 1)[0]
        for item in package_lock
        if isinstance(item, str) and "=" in item
    ]
    if sorted(locked_names) != sorted(required_packages):
        raise HostSecurityContractError(
            "package lock differs from required package inventory"
        )

    if contract.get("host_security_legacy_tool_policy") != ("preserve-unmanaged"):
        raise HostSecurityContractError(
            "legacy security tools require the non-destructive "
            "preserve-unmanaged policy"
        )
    preserved_packages = contract.get("host_security_preserved_legacy_packages")
    if (
        not isinstance(preserved_packages, list)
        or not preserved_packages
        or len(preserved_packages) != len(set(preserved_packages))
        or any(not isinstance(item, str) for item in preserved_packages)
        or set(preserved_packages).intersection(required_packages)
    ):
        raise HostSecurityContractError(
            "preserved legacy package inventory is invalid or overlaps "
            "the managed package set"
        )

    collections = contract.get("host_security_crowdsec_collection_lock")
    dependencies = contract.get("host_security_crowdsec_hub_dependency_lock")
    standalone = contract.get("host_security_crowdsec_standalone_hub_lock")
    if (
        not isinstance(collections, list)
        or not isinstance(dependencies, list)
        or not isinstance(standalone, list)
    ):
        raise HostSecurityContractError("CrowdSec Hub locks must be lists")
    for raw_item in standalone:
        item = require_mapping(raw_item, "standalone CrowdSec Hub item")
        if item.get("type") not in STANDALONE_HUB_TYPES or not (
            isinstance(item.get("version"), str)
            and HUB_VERSION.fullmatch(item["version"])
        ):
            raise HostSecurityContractError(
                "standalone CrowdSec Hub items need a reviewed type and version"
            )
    names: set[str] = set()
    for raw_item in [*collections, *dependencies, *standalone]:
        item = require_mapping(raw_item, "CrowdSec Hub lock item")
        name = item.get("name")
        digest = item.get("sha256")
        if (
            not isinstance(name, str)
            or name in names
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise HostSecurityContractError(
                "CrowdSec Hub lock identity or digest is invalid"
            )
        names.add(name)

    validate_traefik_basicauth(contract, names)

    # La política de MFA de SSH debe ser una decisión explícita del contrato.
    mfa_policy = contract.get("host_security_ssh_mfa_policy")
    if mfa_policy not in ("retired", "required"):
        raise HostSecurityContractError(
            "host_security_ssh_mfa_policy must be 'retired' or 'required'"
        )

    # El endurecimiento de /proc debe declararse entero: las opciones que se
    # escriben en fstab y las que se exigen sobre el montaje efectivo tienen
    # que coincidir, y `hidepid` nunca puede quedar en modo permisivo. El
    # nucleo representa `hidepid=2` como `invisible`.
    proc_options = contract.get("host_security_proc_mount_options")
    proc_required = contract.get("host_security_proc_required_options")
    if not isinstance(proc_options, str) or not isinstance(proc_required, list):
        raise HostSecurityContractError("the /proc hardening contract is absent")
    declared = [item.strip() for item in proc_options.split(",") if item.strip()]
    if "hidepid=2" not in declared:
        raise HostSecurityContractError("/proc must declare hidepid=2")
    for mandatory in ("nosuid", "nodev", "noexec"):
        if mandatory not in declared:
            raise HostSecurityContractError(
                f"/proc must declare {mandatory}"
            )
    if not all(isinstance(item, str) for item in proc_required):
        raise HostSecurityContractError("/proc required options are invalid")
    if "hidepid=invisible" not in proc_required:
        raise HostSecurityContractError(
            "/proc must require the effective hidepid=invisible"
        )
    for mandatory in ("nosuid", "nodev", "noexec"):
        if mandatory not in proc_required:
            raise HostSecurityContractError(
                f"/proc must require the effective {mandatory}"
            )


def main() -> int:
    try:
        document = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
        validate(document, datetime.now(timezone.utc))
    except (OSError, yaml.YAMLError, HostSecurityContractError) as error:
        print(f"ERROR: {error}")
        return 1
    print("Host-security snapshot, package and CrowdSec locks are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
