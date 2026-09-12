#!/usr/bin/env python3
"""Validate and render the independent OrganizationWeb catalog."""

import argparse
import importlib.util
import re
from pathlib import Path
import subprocess
import sys

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]
STACK_SERVICES = {"backend", "web", "postgres", "rabbitmq"}


def exact_keys(value, keys, context):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{context}: unexpected or missing keys")


def load_channel_module():
    path = ROOT / "scripts/validate-image-channels.py"
    spec = importlib.util.spec_from_file_location("validate_image_channels", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load the image channel validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_catalog(document):
    """Validate the reviewed baseline; the runtime image is the channel map."""
    exact_keys(document, ["organizationweb"], "catalog")
    app = document["organizationweb"]
    exact_keys(app, ["schema_version", "stack_name", "release", "hostname", "edge_network", "data_root", "images", "secrets"], "organizationweb")
    for key, expected in {
        "schema_version": 1,
        "stack_name": "organizationweb",
        "hostname": "organizacion.apptolast.com",
        "edge_network": "apptolast-edge-organizationweb",
        "data_root": "/srv/organizationweb",
    }.items():
        if type(app[key]) is not type(expected) or app[key] != expected:
            raise ValueError(f"{key} differs from the independent application contract")
    if not isinstance(app["release"], str) or not re.fullmatch(r"[a-f0-9]{40}", app["release"]):
        raise ValueError("release must identify the published commit")
    repositories = {
        "backend": "ocholoko888/organizationweb-api",
        "web": "ocholoko888/organizationweb-web",
        "postgres": "postgres",
        "rabbitmq": "rabbitmq",
    }
    exact_keys(app["images"], repositories, "images")
    for name, repository in repositories.items():
        if not isinstance(app["images"][name], str) or not re.fullmatch(re.escape(repository) + r"@sha256:[a-f0-9]{64}", app["images"][name]):
            raise ValueError(f"image {name} must use its reviewed repository and digest")
    keys = ["db_username", "db_password", "auth_username", "auth_password", "rabbitmq_username", "rabbitmq_password", "rabbitmq_config"]
    exact_keys(app["secrets"], keys, "secrets")
    for key in keys:
        pattern = "organizationweb-" + key.replace("_", "-") + r"-v[1-9][0-9]*"
        if not isinstance(app["secrets"][key], str) or not re.fullmatch(pattern, app["secrets"][key]):
            raise ValueError(f"secret {key} must name its versioned external object")
    return app


def load_image_channels(document=None):
    """Return the validated per-stack channel map used to render stacks."""
    module = load_channel_module()
    try:
        channel_map = module.load_channel_map(ROOT, organizationweb=document)
    except module.ChannelError as error:
        raise ValueError(f"image channel map: {error}") from error
    entries = channel_map["services"].get("organizationweb", {})
    if set(entries) != STACK_SERVICES:
        raise ValueError("OrganizationWeb channel entries differ from the stack")
    for name, entry in entries.items():
        if entry["baseline"] != {"catalog": "organizationweb", "component": name}:
            raise ValueError(f"channel {name} is not bound to its catalog image")
    return channel_map["services"]


def validate_render(stack, image_channels):
    services = stack.get("services") if isinstance(stack, dict) else None
    if not isinstance(services, dict) or set(services) != STACK_SERVICES:
        raise ValueError("rendered OrganizationWeb services changed")
    for name, service in services.items():
        entry = image_channels["organizationweb"][name]
        if service.get("image") != entry["reference"]:
            raise ValueError(f"rendered image {name} differs from its channel entry")
        labels = (service.get("deploy") or {}).get("labels") or {}
        if labels.get("apptolast.autoupdate") != entry["label"]:
            raise ValueError(f"rendered autoupdate label {name} differs from its channel entry")


def render(document, image_channels):
    template = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROOT / "stacks/organizationweb"),
        undefined=jinja2.StrictUndefined,
    ).get_template("stack.yml.j2")
    return template.render(**document, image_channels_map=image_channels)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        document = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())
        validate_catalog(document)
        image_channels = load_image_channels(document)
        rendered = render(document, image_channels)
        validate_render(yaml.safe_load(rendered), image_channels)
        result = subprocess.run(
            ["docker", "stack", "config", "--compose-file", "-"],
            input=rendered, text=True, capture_output=True, check=False,
        )
        if result.returncode or "Ignoring" in result.stderr:
            raise ValueError("Docker rejected stack options: " + result.stderr)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
    except (ValueError, OSError, yaml.YAMLError, jinja2.TemplateError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("OrganizationWeb catalog, image channels and Docker stack format passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
