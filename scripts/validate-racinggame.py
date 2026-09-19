#!/usr/bin/env python3
"""Validate the independent racing-game catalog and its rendered stack."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import re
import subprocess
import sys

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]
STACK_SERVICES = {"web"}
# The only repository this stack may run, so a typo cannot point the public
# hostname at an image nobody reviewed.
REPOSITORIES = {"web": "ocholoko888/racinggame"}
# Traefik reaches the game at http://racinggame_web:3000, so the container
# port is part of that contract and is pinned here too.
CONTAINER_PORT = 3000


def exact_keys(value, keys, context):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{context}: unexpected or missing keys")


def validate_catalog(document):
    """Validate the reviewed baseline; the runtime image is the channel map."""
    exact_keys(document, ["racinggame"], "catalog")
    app = document["racinggame"]
    exact_keys(
        app,
        [
            "schema_version",
            "stack_name",
            "release",
            "hostname",
            "edge_network",
            "images",
        ],
        "racinggame",
    )
    for key, expected in {
        "schema_version": 1,
        "stack_name": "racinggame",
        "hostname": "racinggame.apptolast.com",
        "edge_network": "apptolast-edge-racinggame",
    }.items():
        if type(app[key]) is not type(expected) or app[key] != expected:
            raise ValueError(
                f"{key} differs from the independent application contract"
            )
    if not isinstance(app["release"], str) or not re.fullmatch(
        r"[a-f0-9]{40}", app["release"]
    ):
        raise ValueError("release must identify the published commit")
    exact_keys(app["images"], REPOSITORIES, "images")
    for name, repository in REPOSITORIES.items():
        pattern = re.escape(repository) + r"@sha256:[a-f0-9]{64}"
        if not isinstance(app["images"][name], str) or not re.fullmatch(
            pattern, app["images"][name]
        ):
            raise ValueError(
                f"image {name} must use its reviewed repository and digest"
            )
    return app


def load_channel_module():
    path = ROOT / "scripts/validate-image-channels.py"
    spec = importlib.util.spec_from_file_location(
        "validate_image_channels", path
    )
    if spec is None or spec.loader is None:
        raise ValueError("cannot load the image channel validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_image_channels(document=None):
    """Return the validated per-stack channel map used to render stacks."""
    module = load_channel_module()
    try:
        channel_map = module.load_channel_map(ROOT, racinggame=document)
    except module.ChannelError as error:
        raise ValueError(f"image channel map: {error}") from error
    entries = channel_map["services"].get("racinggame", {})
    if set(entries) != STACK_SERVICES:
        raise ValueError("racing-game channel entries differ from the stack")
    for name, entry in entries.items():
        if entry["baseline"] != {"catalog": "racinggame", "component": name}:
            raise ValueError(
                f"channel {name} is not bound to its catalog image"
            )
    return channel_map["services"]


def validate_render(stack, image_channels):
    """Prove the rendered stack is exactly the reviewed stateless game."""
    services = stack.get("services") if isinstance(stack, dict) else None
    if not isinstance(services, dict) or set(services) != STACK_SERVICES:
        raise ValueError("rendered racing-game services changed")
    for name, service in services.items():
        entry = image_channels["racinggame"][name]
        if service.get("image") != entry["reference"]:
            raise ValueError(
                f"rendered image {name} differs from its channel entry"
            )
        labels = (service.get("deploy") or {}).get("labels") or {}
        if labels.get("apptolast.autoupdate") != entry["label"]:
            raise ValueError(
                f"rendered autoupdate label {name} differs from its channel"
            )
        # The game keeps no state. A bind mount, a secret or a published port
        # would each be blast radius that nothing in this repo reviews.
        for item in service.get("volumes") or []:
            if not isinstance(item, dict) or item.get("type") != "tmpfs":
                raise ValueError(f"{name} may only mount tmpfs")
        if service.get("secrets") or service.get("configs"):
            raise ValueError(f"{name} must not consume secrets or configs")
        if service.get("ports"):
            raise ValueError(f"{name} is reached through Traefik, not a port")
        if service.get("read_only") is not True:
            raise ValueError(f"{name} needs a read-only root filesystem")
        if service.get("cap_drop") != ["ALL"]:
            raise ValueError(f"{name} must drop every capability")
        test = " ".join((service.get("healthcheck") or {}).get("test") or [])
        if f"127.0.0.1:{CONTAINER_PORT}" not in test:
            raise ValueError(
                f"{name} must health-check the port Traefik forwards to"
            )
        environment = service.get("environment") or {}
        if environment.get("PORT") != str(CONTAINER_PORT):
            raise ValueError(f"{name} must listen on port {CONTAINER_PORT}")
    networks = stack.get("networks") or {}
    if (networks.get("edge") or {}).get("external") is not True:
        raise ValueError("the edge network must stay external to this stack")


def render(document, image_channels):
    template = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROOT / "stacks/racinggame"),
        undefined=jinja2.StrictUndefined,
    ).get_template("stack.yml.j2")
    return template.render(**document, image_channels_map=image_channels)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        document = yaml.safe_load(
            (ROOT / "config/racinggame.yml").read_text(encoding="utf-8")
        )
        validate_catalog(document)
        image_channels = load_image_channels(document)
        rendered = render(document, image_channels)
        validate_render(yaml.safe_load(rendered), image_channels)
        result = subprocess.run(
            ["docker", "stack", "config", "--compose-file", "-"],
            input=rendered,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode or "Ignoring" in result.stderr:
            raise ValueError("Docker rejected stack options: " + result.stderr)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
    except (ValueError, OSError, yaml.YAMLError, jinja2.TemplateError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("RacingGame catalog, image channels and Docker stack format passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
