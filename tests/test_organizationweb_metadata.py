"""Execute the real metadata role with the new playbook's actual vars files."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class OrganizationWebMetadataTests(unittest.TestCase):
    def test_unknown_playbook_is_rejected_before_rendering_metadata(self):
        result = self.run_metadata_check("organizationweb-unknown")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Deploy through scripts/deploy-ansible.sh", result.stdout)
        self.assertNotIn("Record source identity", result.stdout)

    def test_check_renders_only_organizationweb_metadata_from_real_inputs(self):
        result = self.run_metadata_check("organizationweb")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('component: "organizationweb"', result.stdout)
        self.assertIn('playbook: "organizationweb"', result.stdout)
        self.assertIn('release: "0.1.0"', result.stdout)
        self.assertNotIn('component: "workloads"', result.stdout)
        self.assertNotIn('component: "edge"', result.stdout)

    def run_metadata_check(self, identity):
        source_path = ROOT / "ansible/playbooks/organizationweb.yml"
        source = yaml.safe_load(source_path.read_text())[0]
        self.assertEqual(source["roles"][-1], {"role": "deployment_metadata"})
        (ROOT / ".build").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="organizationweb-metadata-", dir=ROOT / ".build"
        ) as temporary:
            directory = Path(temporary)
            installed = directory / "installed"
            (installed / "deployments").mkdir(parents=True)
            play = [{
                "name": "Check real application metadata in a disposable path",
                "hosts": "localhost",
                "connection": "local",
                "gather_facts": False,
                "become": False,
                "vars_files": [
                    str((source_path.parent / item).resolve())
                    for item in source["vars_files"]
                ],
                "vars": {
                    "platform_install_root": str(installed),
                    "deployment_metadata_source_revision": "a" * 40,
                    "deployment_metadata_contract_sha256": "b" * 64,
                    "deployment_metadata_profile": "production",
                    "deployment_metadata_playbook": identity,
                    "deployment_metadata_repository_slug":
                        "apptolast/DockerSwarmInfrastrcture",
                },
                "roles": ["deployment_metadata"],
            }]
            path = directory / "check.yml"
            path.write_text(yaml.safe_dump(play, sort_keys=False))
            environment = dict(os.environ)
            environment["ANSIBLE_CONFIG"] = str(ROOT / "ansible/ansible.cfg")
            environment["ANSIBLE_NOCOLOR"] = "1"
            result = subprocess.run(
                [str(ROOT / ".venv/bin/ansible-playbook"), "-i", "localhost,",
                 "--check", "--diff", str(path)],
                cwd=ROOT, env=environment, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=60,
            )
            self.assertEqual(list(installed.rglob("*.yml")), [])
            return result
