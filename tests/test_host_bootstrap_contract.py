"""Contract tests for the fresh-host bootstrap boundary."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROLE_TASKS = PROJECT_ROOT / "ansible/roles/host_bootstrap/tasks/main.yml"
ROLE_DEFAULTS = PROJECT_ROOT / "ansible/roles/host_bootstrap/defaults/main.yml"
WRAPPER = PROJECT_ROOT / "scripts/bootstrap-host.sh"
HOST_SECURITY_TASKS = PROJECT_ROOT / "ansible/roles/host_security/tasks/main.yml"
HOST_SECURITY_SSH_TASKS = PROJECT_ROOT / "ansible/roles/host_security/tasks/ssh.yml"
HOST_SECURITY_CONTRACT = PROJECT_ROOT / "config/host-security.yml"
HOST_BASELINE_FIREWALL_TASKS = (
    PROJECT_ROOT / "ansible/roles/host_baseline/tasks/firewall.yml"
)
KEY_VALIDATOR_PATH = PROJECT_ROOT / "scripts/validate-authorized-keys.py"
HOST_SECURITY_VALIDATOR_PATH = PROJECT_ROOT / "scripts/validate-host-security.py"
SSH_POLICY_VALIDATOR_PATH = PROJECT_ROOT / "scripts/validate-ssh-policy.py"
KEY_RECONCILER_PATH = PROJECT_ROOT / "scripts/reconcile-authorized-keys.py"


def load_key_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_authorized_keys",
        KEY_VALIDATOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot import authorized-key validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


key_validator = load_key_validator()


def load_host_security_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_host_security",
        HOST_SECURITY_VALIDATOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot import host-security validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host_security_validator = load_host_security_validator()


def load_key_reconciler():
    spec = importlib.util.spec_from_file_location(
        "reconcile_authorized_keys",
        KEY_RECONCILER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot import authorized-key reconciler")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


key_reconciler = load_key_reconciler()


def load_ssh_policy_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_ssh_policy",
        SSH_POLICY_VALIDATOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot import SSH policy validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ssh_policy_validator = load_ssh_policy_validator()


class HostBootstrapContractTests(unittest.TestCase):
    def test_bootstrap_creates_only_the_reviewed_identity(self) -> None:
        tasks = yaml.safe_load(ROLE_TASKS.read_text(encoding="utf-8"))
        rendered = ROLE_TASKS.read_text(encoding="utf-8")
        self.assertTrue(tasks)
        self.assertIn("host_bootstrap_admin_uid | int == 1001", rendered)
        self.assertIn("host_bootstrap_admin_gid | int == 1001", rendered)
        self.assertIn("/.ssh/authorized_keys", rendered)
        self.assertIn('mode: "0600"', rendered)
        self.assertIn("password_lock: false", rendered)
        self.assertIn("update_password: on_create", rendered)
        self.assertNotIn("update_password: always", rendered)
        self.assertNotIn("NOPASSWD", rendered)
        self.assertLess(
            rendered.index("Inspect reserved bootstrap account and group identities"),
            rendered.index("Create the reviewed administrator primary group"),
        )
        defaults = ROLE_DEFAULTS.read_text(encoding="utf-8")
        self.assertIn("/etc/containerd", defaults)
        self.assertIn("/etc/docker", defaults)
        self.assertIn("/var/lib/containerd", defaults)
        self.assertIn("/var/lib/docker", defaults)
        self.assertIn("  - containerd\n", defaults)
        self.assertIn("docker-ce-rootless-extras", defaults)
        self.assertIn("docker-buildx-plugin", defaults)
        self.assertIn("docker-ce", defaults)
        self.assertIn("host_bootstrap_is_fresh", rendered)
        self.assertIn("host_bootstrap_is_possible_resume", rendered)
        self.assertIn(
            "Prove an exact completed bootstrap before resuming",
            rendered,
        )
        self.assertLess(
            rendered.index("Prove safe path structure before reading resume files"),
            rendered.index("Read structurally proven existing bootstrap provenance"),
        )
        self.assertLess(
            rendered.index("Prove safe path structure before reading resume files"),
            rendered.index("Read structurally proven administrator key allowlist"),
        )

    def test_wrapper_requires_external_private_inputs_and_clean_git(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("--confirm-production", wrapper)
        self.assertIn("600:${UID}", wrapper)
        self.assertIn("status --porcelain", wrapper)
        self.assertIn("--user root", wrapper)
        self.assertIn("--user admin", wrapper)
        self.assertIn("ipaddress.ip_address", wrapper)
        self.assertIn("address.version != 4", wrapper)
        self.assertGreaterEqual(
            wrapper.count("--extra-vars ansible_become=false"),
            3,
        )
        self.assertLess(
            wrapper.index("bootstrap-host-security.yml"),
            wrapper.index("bootstrap-host-ssh-final.yml"),
        )
        self.assertIn("dockerswarm-bootstrap-ssh/confirmed", wrapper)
        self.assertIn("dockerswarm-iac.lock", wrapper)
        self.assertIn("dockerswarm-bootstrap.marker", wrapper)
        self.assertIn("run-locked-command.py", wrapper)
        self.assertIn("LOCKED:${operation_id}", wrapper)
        self.assertIn(
            "printf 'RELEASE:%s\\n' \"${operation_id}\"",
            wrapper,
        )
        self.assertGreaterEqual(wrapper.count("status --porcelain"), 2)
        self.assertNotIn("--ask-pass", wrapper)

    def test_security_bootstrap_closes_the_fresh_host_dead_end(self) -> None:
        contract = yaml.safe_load(HOST_SECURITY_CONTRACT.read_text(encoding="utf-8"))
        required = set(contract["host_security_required_packages"])
        self.assertTrue(
            {
                "chrony",
                "crowdsec",
                "crowdsec-firewall-bouncer-iptables",
                "fail2ban",
                "psad",
                "rsyslog",
                "ufw",
            }.issubset(required)
        )
        self.assertIn(
            {"protocol": "tcp", "port": 4460},
            contract["host_security_required_host_egress"],
        )
        self.assertIn(
            {"protocol": "tcp", "port": 443},
            contract["host_security_required_host_egress"],
        )
        tasks = HOST_SECURITY_TASKS.read_text(encoding="utf-8")
        self.assertIn("host_security_crowdsec_repository_key_fingerprint", tasks)
        self.assertIn('select("match", "^pub:")', tasks)
        platform_tasks = (
            PROJECT_ROOT / "ansible/roles/platform/tasks/main.yml"
        ).read_text(encoding="utf-8")
        self.assertGreaterEqual(
            platform_tasks.count('select("match", "^pub:")'),
            2,
        )
        dependency_lock = contract["host_security_crowdsec_hub_dependency_lock"]
        self.assertGreaterEqual(len(dependency_lock), 17)
        self.assertIn(
            {
                "type": "scenarios",
                "name": "crowdsecurity/ssh-cve-2024-6387",
                "version": "0.2",
                "sha256": (
                    "7888f1f31ea75d55f7b4bdf56b6f0840ca4ecbd937af0655cdf263062a11e85a"
                ),
            },
            dependency_lock,
        )
        self.assertIn("Enforce the exact CrowdSec acquisition set", tasks)
        self.assertIn(
            "Enforce installed CrowdSec Hub versions and content digests",
            tasks,
        )
        self.assertIn("Enforce GitHub SSH maintenance over TCP 443", tasks)
        github_client = (
            PROJECT_ROOT / "ansible/roles/host_security/templates/"
            "github-over-https.conf.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("HostName ssh.github.com", github_client)
        self.assertIn("Port 443", github_client)
        self.assertIn("Set fail-closed UFW defaults", tasks)
        self.assertIn("Wait for Chrony synchronization", tasks)
        self.assertIn(
            "Reconcile the exact administrator supplementary groups",
            tasks,
        )
        hardened_ssh = (
            PROJECT_ROOT / "ansible/templates/ssh-hardening.conf.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("AllowUsers {{ host_security_admin_user }}", hardened_ssh)
        self.assertNotIn("AllowGroups", hardened_ssh)

    def test_legacy_security_tools_are_never_destructively_reconciled(
        self,
    ) -> None:
        contract = yaml.safe_load(HOST_SECURITY_CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(
            contract["host_security_legacy_tool_policy"],
            "preserve-unmanaged",
        )
        self.assertIn(
            "needrestart",
            contract["host_security_preserved_legacy_packages"],
        )
        tasks = HOST_SECURITY_TASKS.read_text(encoding="utf-8")
        self.assertNotIn("host_security_removed_packages", tasks)
        self.assertNotIn(
            "Remove unreviewed overlapping host-security tools",
            tasks,
        )
        self.assertNotIn("purge: true", tasks)

    def test_post_state_checks_do_not_break_ansible_check_mode(self) -> None:
        security_tasks = yaml.safe_load(HOST_SECURITY_TASKS.read_text(encoding="utf-8"))
        by_name = {task["name"]: task for task in security_tasks}
        for name in (
            "Read the CrowdSec Hub update unit states",
            "Enforce versioned-only CrowdSec Hub promotion",
            "Reject every unreviewed UFW user-chain rule",
            "Read the active UFW policy",
            "Verify the fail-closed UFW base",
            "Validate the CrowdSec engine",
            "Validate the credential-bearing CrowdSec bouncer",
            "Verify Fail2ban and its SSH jail",
            "Verify PSAD runtime",
            "Wait for Chrony synchronization",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    by_name[name]["when"],
                    "not ansible_check_mode",
                )

        firewall_tasks = yaml.safe_load(
            HOST_BASELINE_FIREWALL_TASKS.read_text(encoding="utf-8")
        )
        for task in firewall_tasks:
            with self.subTest(name=task["name"]):
                self.assertEqual(
                    task["when"],
                    "not ansible_check_mode",
                )

    def test_snapshot_promotion_policy_is_age_bounded(self) -> None:
        contract = yaml.safe_load(HOST_SECURITY_CONTRACT.read_text(encoding="utf-8"))
        now = datetime(2026, 7, 26, 12, tzinfo=timezone.utc)
        host_security_validator.validate(contract, now)

        stale = copy.deepcopy(contract)
        stale["host_security_ubuntu_snapshot"] = "20260701T000000Z"
        with self.assertRaises(host_security_validator.HostSecurityContractError):
            host_security_validator.validate(stale, now)

        unattended = copy.deepcopy(contract)
        unattended["host_security_required_packages"].append("unattended-upgrades")
        with self.assertRaises(host_security_validator.HostSecurityContractError):
            host_security_validator.validate(unattended, now)

    def test_final_ssh_phase_has_timed_automatic_rollback(self) -> None:
        tasks = HOST_SECURITY_SSH_TASKS.read_text(encoding="utf-8")
        rollback = (
            PROJECT_ROOT / "ansible/roles/host_security/templates/"
            "bootstrap-ssh-rollback.sh.j2"
        ).read_text(encoding="utf-8")
        timer = (
            PROJECT_ROOT / "ansible/roles/host_security/templates/"
            "bootstrap-ssh-rollback.timer.j2"
        ).read_text(encoding="utf-8")
        staged = (
            PROJECT_ROOT / "ansible/templates/ssh-bootstrap-staged.conf.j2"
        ).read_text(encoding="utf-8")
        self.assertIn('host_security_ssh_phase == "final"', tasks)
        self.assertLess(
            tasks.index("Arm the persistent SSH rollback timer"),
            tasks.index("Install the validated complete SSH bootstrap policy"),
        )
        self.assertIn("PermitRootLogin prohibit-password", staged)
        self.assertIn("OnActiveSec=180s", timer)
        self.assertIn("OnBootSec=180s", timer)
        self.assertIn("systemctl reload ssh.service", rollback)
        self.assertIn("1001:600:regular file", rollback)
        self.assertIn("EXPECTED_STAGED_SHA256", rollback)
        self.assertLess(
            rollback.index('/usr/sbin/sshd -t -f "${STAGED_CONFIG}"'),
            rollback.index("/usr/bin/mv --force --no-target-directory"),
        )
        self.assertIn(
            "host_security_expected_staged_ssh_sha256",
            tasks,
        )
        self.assertIn("dockerswarm-bootstrap-ssh-rollback.path", tasks)
        self.assertIn("did not disarm", WRAPPER.read_text(encoding="utf-8"))

    def test_both_complete_ssh_policies_render_deterministically(self) -> None:
        contract = ssh_policy_validator.load_contract()
        ssh_policy_validator.validate_rendered(contract)
        for (
            _phase,
            (template_name, _permit_root_login, _allow_users),
        ) in ssh_policy_validator.POLICIES.items():
            rendered = ssh_policy_validator.render(template_name, contract)
            self.assertNotIn("Include ", rendered)
            self.assertIn("AuthenticationMethods publickey", rendered)

    def test_every_authorized_key_is_validated_and_duplicates_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = root / "key"
            subprocess.run(
                [
                    "/usr/bin/ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(private_key),
                ],
                check=True,
            )
            public_key = (
                private_key.with_suffix(".pub").read_text(encoding="utf-8").strip()
            )
            allowlist = root / "authorized_keys"
            allowlist.write_text(f"{public_key}\n", encoding="utf-8")
            fingerprints = key_validator.validate(
                allowlist,
                "/usr/bin/ssh-keygen",
            )
            self.assertEqual(len(fingerprints), 1)

            key_type, encoded_key, *_ = public_key.split()
            allowlist.write_text(
                f"{public_key}\n{key_type} {encoded_key} duplicate\n",
                encoding="utf-8",
            )
            with self.assertRaises(key_validator.AuthorizedKeysError):
                key_validator.validate(allowlist, "/usr/bin/ssh-keygen")

    def test_authorized_key_options_blank_and_malformed_lines_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = root / "key"
            subprocess.run(
                [
                    "/usr/bin/ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(private_key),
                ],
                check=True,
            )
            public_key = (
                private_key.with_suffix(".pub").read_text(encoding="utf-8").strip()
            )
            allowlist = root / "authorized_keys"
            candidates = [
                f'command="/bin/false" {public_key}\n',
                f"{public_key}\n\n",
                "# comment\n",
                "ssh-ed25519 not-base64\n",
            ]
            for candidate in candidates:
                allowlist.write_text(candidate, encoding="utf-8")
                with self.subTest(candidate=candidate.split()[0]):
                    with self.assertRaises(key_validator.AuthorizedKeysError):
                        key_validator.validate(
                            allowlist,
                            "/usr/bin/ssh-keygen",
                        )

    def test_weak_rsa_authorized_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = root / "weak-rsa"
            subprocess.run(
                [
                    "/usr/bin/ssh-keygen",
                    "-q",
                    "-t",
                    "rsa",
                    "-b",
                    "1024",
                    "-N",
                    "",
                    "-f",
                    str(private_key),
                ],
                check=True,
            )
            allowlist = root / "authorized_keys"
            allowlist.write_text(
                private_key.with_suffix(".pub").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with self.assertRaises(key_validator.AuthorizedKeysError):
                key_validator.validate(allowlist, "/usr/bin/ssh-keygen")

    def test_invalid_key_cleanup_is_hash_bound_and_retains_valid_keys(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = root / "key"
            subprocess.run(
                [
                    "/usr/bin/ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(private_key),
                ],
                check=True,
            )
            public_key = (
                private_key.with_suffix(".pub").read_text(encoding="utf-8").strip()
            )
            allowlist = root / "authorized_keys"
            allowlist.write_text(
                f"{public_key}\nssh-rsa invalid-base64\n",
                encoding="utf-8",
            )
            allowlist.chmod(0o600)
            if os.getuid() == 0:
                # build_plan() enforces admin:admin ownership on its real
                # production target; when this suite runs as root (required
                # on a host with an active Swarm, see
                # host-global-docker-validation-lock.sh) the fixture must be
                # reowned to match, since tempfile otherwise leaves it
                # root:root.
                os.chown(allowlist, 1001, 1001)
            plan = key_reconciler.build_plan(
                allowlist,
                "/usr/bin/ssh-keygen",
            )
            self.assertEqual(plan["invalid_line_numbers"], [2])
            self.assertEqual(plan["retained_count"], 1)
            self.assertIn(
                plan["before_sha256"],
                plan["confirmation"],
            )
            self.assertIn(
                plan["after_sha256"],
                plan["confirmation"],
            )


if __name__ == "__main__":
    unittest.main()
