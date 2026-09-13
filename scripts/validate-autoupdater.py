#!/usr/bin/env python3
"""Validate and render the reviewed image watcher stack (Shepherd).

`config/autoupdater.yml` is the only input. This validator pins the exact
image digest, the exact environment (with the label filter and without any
setting that widens the watcher's scope), the external registry secret, the
read-only Docker socket bind, manager placement, the kill switch and explicit
resources, then renders `stacks/autoupdater/stack.yml.j2`. The rendered
service must carry exactly the reviewed keys and deploy mapping, so an
added entrypoint, command, user or host setting is refused.

The Docker socket is root-equivalent whatever its bind mode; the read-only
bind only keeps the socket inode itself from being replaced.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
STACK_NAME = "autoupdater"
SERVICE_NAME = "shepherd"
# v1.8.1 (upstream tag commit ee48ca1); :latest resolved to the same index
# digest when the research was done. Bumping it is a reviewed edit here.
REVIEWED_IMAGE = (
    "containrrr/shepherd:v1.8.1@sha256:"
    "b117c2394832e088932d5e1eebb8df6c"
    "1924f47d0fef31788cb310c0fe3bf7db"
)
CATALOG_KEYS = {
    "schema_version",
    "stack_name",
    "enabled",
    "image",
    "sleep_time",
    "timeout_seconds",
    "registry_user",
    "registry_password_secret",
}
REVIEWED_SLEEP_TIME = "1h"
REVIEWED_TIMEOUT_SECONDS = 900
REVIEWED_REGISTRY_USER = "ocholoko888"
SECRET_NAME_RE = re.compile(r"autoupdater-dockerhub-pat-v[1-9][0-9]*")
SECRET_TARGET = "shepherd_registry_password"
DOCKER_SOCKET = "/var/run/docker.sock"
MANAGER_CONSTRAINT = "node.role == manager"
REVIEWED_RESOURCES = {
    "reservations": {"cpus": "0.10", "memory": "18M"},
    "limits": {"cpus": "0.25", "memory": "45M"},
}
FILTER = "label=apptolast.autoupdate=true"
REVIEWED_STACK_KEYS = {"version", "services", "networks", "secrets"}
REVIEWED_SERVICE_KEYS = {
    "image",
    "init",
    "logging",
    "environment",
    "secrets",
    "volumes",
    "networks",
    "deploy",
}
REVIEWED_LOGGING = {
    "driver": "local",
    "options": {"max-file": "5", "max-size": "20m"},
}
REVIEWED_RESTART_POLICY = {"condition": "any", "delay": "30s", "window": "60s"}
REVIEWED_UPDATE_CONFIG = {
    "parallelism": 1,
    "order": "stop-first",
    "failure_action": "rollback",
    "monitor": "30s",
    "max_failure_ratio": 0,
}
REVIEWED_ROLLBACK_CONFIG = {
    "parallelism": 1,
    "order": "stop-first",
    "failure_action": "pause",
    "monitor": "30s",
    "max_failure_ratio": 0,
}


class AutoupdaterError(ValueError):
    """The watcher catalog or its rendered stack breaks the contract."""


def load_channel_module():
    path = ROOT / "scripts/validate-image-channels.py"
    spec = importlib.util.spec_from_file_location("validate_image_channels", path)
    if spec is None or spec.loader is None:
        raise AutoupdaterError("cannot load the image channel validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


channels = load_channel_module()


def expected_environment(catalog: dict[str, Any]) -> dict[str, str]:
    return {
        "FILTER_SERVICES": FILTER,
        "SLEEP_TIME": catalog["sleep_time"],
        "TIMEOUT": str(catalog["timeout_seconds"]),
        "VERBOSE": "true",
        "TZ": "UTC",
        "WITH_REGISTRY_AUTH": "true",
        "REGISTRY_USER": catalog["registry_user"],
    }


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    path = path or ROOT / "config/autoupdater.yml"
    try:
        document = channels.load_unique_yaml(path)
    except channels.ChannelError as error:
        raise AutoupdaterError(str(error)) from error
    return validate_catalog(document)


def validate_catalog(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {"autoupdater"}:
        raise AutoupdaterError("catalog: unexpected or missing keys")
    catalog = document["autoupdater"]
    if not isinstance(catalog, dict) or set(catalog) != CATALOG_KEYS:
        raise AutoupdaterError("autoupdater: unexpected or missing keys")
    if type(catalog["schema_version"]) is not int or catalog["schema_version"] != 1:
        raise AutoupdaterError("autoupdater schema version is unsupported")
    if catalog["stack_name"] != STACK_NAME:
        raise AutoupdaterError("stack_name must be autoupdater")
    if type(catalog["enabled"]) is not bool:
        raise AutoupdaterError("enabled must be a boolean kill switch")
    if catalog["image"] != REVIEWED_IMAGE:
        raise AutoupdaterError("image must be the reviewed Shepherd tag and digest")
    if catalog["sleep_time"] != REVIEWED_SLEEP_TIME:
        raise AutoupdaterError(
            "sleep_time differs from the reviewed Docker Hub rate-limit budget"
        )
    if (
        type(catalog["timeout_seconds"]) is not int
        or catalog["timeout_seconds"] != REVIEWED_TIMEOUT_SECONDS
    ):
        raise AutoupdaterError("timeout_seconds differs from the reviewed value")
    if catalog["registry_user"] != REVIEWED_REGISTRY_USER:
        raise AutoupdaterError("registry_user differs from the reviewed account")
    secret = catalog["registry_password_secret"]
    if not isinstance(secret, str) or SECRET_NAME_RE.fullmatch(secret) is None:
        raise AutoupdaterError(
            "registry_password_secret must name a versioned external secret"
        )
    return catalog


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AutoupdaterError(message)


def validate_render(stack: Any, catalog: dict[str, Any]) -> None:
    """Prove the rendered stack is exactly the reviewed watcher."""
    require(isinstance(stack, dict), "rendered stack is not a mapping")
    require(
        set(stack) == REVIEWED_STACK_KEYS and stack.get("version") == "3.8",
        "the rendered stack top-level keys differ from the reviewed set",
    )
    services = stack.get("services")
    require(
        isinstance(services, dict) and set(services) == {SERVICE_NAME},
        "the autoupdater stack must contain exactly the shepherd service",
    )
    service = services[SERVICE_NAME]
    require(isinstance(service, dict), "shepherd service is malformed")
    require(service.get("image") == REVIEWED_IMAGE, "shepherd image differs from the pin")
    require(
        service.get("image") == catalog["image"],
        "shepherd image differs from the catalog",
    )
    # An exact key set, not a denylist: entrypoint, command, user,
    # extra_hosts, dns, cap_add, security_opt and any future compose key
    # would run unreviewed code next to the Docker socket.
    extra = sorted(set(service) - REVIEWED_SERVICE_KEYS)
    require(not extra, f"shepherd must not declare {', '.join(extra)}")
    missing = sorted(REVIEWED_SERVICE_KEYS - set(service))
    require(not missing, f"shepherd lacks reviewed keys: {', '.join(missing)}")
    require(service["init"] is True, "shepherd must run with init: true")
    require(
        service["logging"] == REVIEWED_LOGGING,
        "shepherd logging differs from the reviewed local driver",
    )

    environment = service.get("environment")
    try:
        channels.validate_autoupdater_environment(environment, channels.AUTOUPDATE_LABEL)
        observed = channels.normalize_environment(environment)
    except channels.ChannelError as error:
        raise AutoupdaterError(str(error)) from error
    require(
        observed == expected_environment(catalog),
        "shepherd environment differs from the reviewed exact set",
    )
    require(observed["FILTER_SERVICES"] == FILTER, "FILTER_SERVICES differs")

    secrets = service.get("secrets")
    require(
        isinstance(secrets, list) and len(secrets) == 1,
        "shepherd must mount exactly the registry password secret",
    )
    reference = secrets[0]
    require(
        reference
        == {
            "source": "registry_password",
            "target": SECRET_TARGET,
            "uid": "0",
            "gid": "0",
            "mode": 0o400,
        },
        "the registry secret reference differs from the reviewed mount",
    )
    require(
        stack.get("secrets")
        == {
            "registry_password": {
                "external": True,
                "name": catalog["registry_password_secret"],
            }
        },
        "the registry password must be the reviewed external secret",
    )

    require(
        service.get("volumes")
        == [
            {
                "type": "bind",
                "source": DOCKER_SOCKET,
                "target": DOCKER_SOCKET,
                "read_only": True,
            }
        ],
        "shepherd must bind only the Docker socket, read-only",
    )

    deploy = service.get("deploy")
    require(isinstance(deploy, dict), "shepherd deploy is malformed")
    require(deploy.get("mode") == "replicated", "shepherd must be replicated")
    replicas = deploy.get("replicas")
    expected_replicas = 1 if catalog["enabled"] else 0
    require(
        type(replicas) is int and replicas == expected_replicas,
        f"shepherd replicas must be {expected_replicas} for enabled="
        f"{catalog['enabled']}",
    )
    require(
        deploy.get("placement") == {"constraints": [MANAGER_CONSTRAINT]},
        "shepherd must run only on the manager",
    )
    require(
        deploy.get("labels") == {channels.AUTOUPDATE_LABEL: "false"},
        "the watcher must never opt itself in",
    )
    require(
        deploy.get("resources") == REVIEWED_RESOURCES,
        "shepherd resources differ from the reviewed capacity budget",
    )
    require(
        deploy.get("restart_policy") == REVIEWED_RESTART_POLICY,
        "shepherd restart_policy differs from the reviewed one",
    )
    require(
        deploy.get("update_config") == REVIEWED_UPDATE_CONFIG,
        "shepherd update_config differs from the reviewed rollback on failure",
    )
    require(
        deploy.get("rollback_config") == REVIEWED_ROLLBACK_CONFIG,
        "shepherd rollback_config differs from the reviewed one",
    )
    require(
        deploy
        == {
            "mode": "replicated",
            "replicas": expected_replicas,
            "placement": {"constraints": [MANAGER_CONSTRAINT]},
            "labels": {channels.AUTOUPDATE_LABEL: "false"},
            "restart_policy": REVIEWED_RESTART_POLICY,
            "update_config": REVIEWED_UPDATE_CONFIG,
            "rollback_config": REVIEWED_ROLLBACK_CONFIG,
            "resources": REVIEWED_RESOURCES,
        },
        "shepherd deploy differs from the reviewed exact mapping",
    )
    networks = stack.get("networks")
    require(
        networks == {"egress": {"driver": "overlay", "attachable": False}},
        "shepherd networks differ from the reviewed egress network",
    )
    require(service.get("networks") == ["egress"], "shepherd network differs")


def render(catalog: dict[str, Any]) -> str:
    template = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROOT / "stacks/autoupdater"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    ).get_template("stack.yml.j2")
    return template.render(autoupdater=catalog)


def render_validated(catalog: dict[str, Any] | None = None) -> tuple[str, Any]:
    catalog = catalog if catalog is not None else load_catalog()
    rendered = render(catalog)
    stack = yaml.safe_load(rendered)
    validate_render(stack, catalog)
    return rendered, stack


def docker_stack_config(rendered: str) -> None:
    result = subprocess.run(
        ["docker", "stack", "config", "--compose-file", "-"],
        input=rendered,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode or "Ignoring" in result.stderr:
        raise AutoupdaterError("Docker rejected stack options: " + result.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        rendered, _stack = render_validated()
        docker_stack_config(rendered)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
    except (AutoupdaterError, OSError, yaml.YAMLError, jinja2.TemplateError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Autoupdater catalog, watcher contract and Docker stack format passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
