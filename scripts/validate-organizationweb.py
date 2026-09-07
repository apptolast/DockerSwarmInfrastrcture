#!/usr/bin/env python3
"""Validate and render the independent OrganizationWeb catalog."""

import argparse
import re
from pathlib import Path
import subprocess
import sys

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]


def exact_keys(value, keys, context):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{context}: unexpected or missing keys")


def validate_catalog(document):
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        document = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())
        validate_catalog(document)
        template = jinja2.Environment(
            loader=jinja2.FileSystemLoader(ROOT / "stacks/organizationweb"),
            undefined=jinja2.StrictUndefined,
        ).get_template("stack.yml.j2")
        rendered = template.render(**document)
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
    print("OrganizationWeb catalog and Docker stack format passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
