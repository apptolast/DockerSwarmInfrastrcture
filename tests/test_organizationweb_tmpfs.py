"""Swarm-compatible mounts survive stack conversion and remain writable."""

import json
from pathlib import Path
import subprocess
import unittest
import uuid

import importlib.util

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_image_channels_map():
    """Return the reviewed per-stack channel map the stack templates render."""
    spec = importlib.util.spec_from_file_location(
        "validate_image_channels", ROOT / "scripts/validate-image-channels.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_channel_map(ROOT)["services"]


class OrganizationWebTmpfsTests(unittest.TestCase):
    def test_converted_stack_mounts_are_real_tmpfs_with_readonly_images(self):
        variables = yaml.safe_load((ROOT / "config/organizationweb.yml").read_text())
        template = jinja2.Environment(
            loader=jinja2.FileSystemLoader(ROOT / "stacks/organizationweb"),
            undefined=jinja2.StrictUndefined,
        ).get_template("stack.yml.j2")
        converted = subprocess.run(
            ["docker", "stack", "config", "--compose-file", "-"],
            input=template.render(**variables, image_channels_map=load_image_channels_map()), text=True, capture_output=True,
            check=True, timeout=30,
        )
        services = yaml.safe_load(converted.stdout)["services"]
        expected = {"backend": {"/tmp": 67108864},
                    "web": {"/run": 16777216, "/var/cache/nginx": 16777216}}
        for service_name, destinations in expected.items():
            with self.subTest(service=service_name):
                service = services[service_name]
                mounts = service.get("volumes", [])
                self.assertEqual({item["target"] for item in mounts}, set(destinations))
                self.assertNotIn("tmpfs", service)
                self.assertTrue(service["read_only"])
                self.assertEqual(service["cap_drop"], ["ALL"])
                name = "organizationweb-tmpfs-" + uuid.uuid4().hex
                command = ["docker", "run", "--name", name, "--network", "none",
                           "--read-only", "--user", service["user"], "--cap-drop", "ALL"]
                for mount in mounts:
                    self.assertEqual(mount["type"], "tmpfs")
                    self.assertEqual(mount["tmpfs"]["size"], destinations[mount["target"]])
                    command.extend(["--mount", "type=tmpfs,destination=" + mount["target"]
                                    + ",tmpfs-size=" + str(mount["tmpfs"]["size"])])
                # Published images, real users, read-only roots; no app credentials.
                command.extend(["--entrypoint", "/bin/sh", service["image"], "-ec",
                                'for target do stat -c "%a %u:%g %n" "$target"; '
                                'test "$(stat -c %a "$target")" = 1777; '
                                'touch "$target/probe"; done; cat /proc/mounts',
                                "probe", *destinations])
                try:
                    result = subprocess.run(command, text=True, capture_output=True, timeout=45)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    for target in destinations:
                        line = next(line for line in result.stdout.splitlines()
                                    if line.split()[1] == target)
                        self.assertEqual(line.split()[2], "tmpfs")
                        self.assertTrue({"rw", "nosuid", "nodev", "noexec"}.issubset(
                            set(line.split()[3].split(","))))
                    state = json.loads(subprocess.check_output(
                        ["docker", "inspect", "--format", "{{json .HostConfig.Mounts}}", name],
                        text=True, timeout=15))
                    self.assertEqual({item["Target"] for item in state}, set(destinations))
                finally:
                    subprocess.run(["docker", "rm", "--force", name],
                                   capture_output=True, timeout=20, check=False)
