"""Contract tests for the fresh-host bootstrap boundary."""

from __future__ import annotations

import base64
import copy
import importlib.util
import os
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ansible_task_harness import AnsibleTaskAssertions, run_task_definition

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
HOST_SECURITY_MAIN = "ansible/roles/host_security/tasks/main.yml"
HOST_SECURITY_HANDLERS = "ansible/roles/host_security/handlers/main.yml"
HOST_BASELINE_ROLE = "ansible/roles/host_baseline"
HOST_BASELINE_SYSCTL = f"{HOST_BASELINE_ROLE}/tasks/sysctl.yml"
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
        contract["host_security_ubuntu_snapshot"] = "20260819T000000Z"
        now = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
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


# A reduced /etc/ufw/before.rules in Ubuntu's layout. The PSAD tasks edit the
# lines just before its final COMMIT.
UFW_BEFORE_RULES = """\
*filter
:ufw-before-input - [0:0]
:ufw-before-output - [0:0]
:ufw-before-forward - [0:0]
:ufw-not-local - [0:0]
-A ufw-before-input -i lo -j ACCEPT
-A ufw-before-input -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

# don't delete the 'COMMIT' line or these rules won't be processed
"""
LEGACY_PSAD_LINES = (
    "# log all traffic so psad can analyze\n"
    '-A INPUT -j LOG --log-tcp-options --log-prefix "[IPTABLES] "\n'
    '-A FORWARD -j LOG --log-tcp-options --log-prefix "[IPTABLES] "\n'
)
# The settings of /etc/default/ufw read on the production host on 2026-09-25,
# without its comments.
CONVERGED_UFW_DEFAULTS = """\
# /etc/default/ufw
#
IPV6=yes
DEFAULT_INPUT_POLICY="DROP"
DEFAULT_OUTPUT_POLICY="DROP"
DEFAULT_FORWARD_POLICY="DROP"
DEFAULT_APPLICATION_POLICY="SKIP"
MANAGE_BUILTINS=no
"""


def load_tasks(relative_path: str) -> dict[str, dict[str, Any]]:
    tasks = yaml.safe_load((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
    return {task["name"]: task for task in tasks}


def command_argvs(relative_path: str) -> list[list[str]]:
    tasks = yaml.safe_load((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
    return [
        task["ansible.builtin.command"]["argv"]
        for task in tasks
        if "argv" in (task.get("ansible.builtin.command") or {})
    ]


class HostBaselineConvergenceTests(AnsibleTaskAssertions, unittest.TestCase):
    """A converged host-baseline apply changes nothing and never opens UFW."""

    CONVERGE = "Converge only the drifted managed kernel settings"
    APPORT_GATE = "Require a known Apport kernel crash-handler unit state"
    APPORT_STOP = "Keep Apport from rewriting the kernel core-dump policy"
    UFW_PARSE = "Parse the persisted UFW default policies"
    UFW_GATE = "Require exactly one readable policy per UFW direction"
    UFW_DEFAULT = "Set fail-closed UFW defaults"
    PSAD_CLEANUP = "Remove unmarked legacy PSAD UFW lines"
    PSAD_BLOCK = "Install managed PSAD logging in UFW"

    @classmethod
    def setUpClass(cls) -> None:
        cls.sysctl = load_tasks(HOST_BASELINE_SYSCTL)
        cls.security = load_tasks(HOST_SECURITY_MAIN)
        cls.baseline_defaults = yaml.safe_load(
            (PROJECT_ROOT / HOST_BASELINE_ROLE / "defaults/main.yml").read_text(
                encoding="utf-8"
            )
        )

    def condition_holds(
        self,
        condition: str,
        variables: dict[str, Any],
        item: dict[str, Any] | None = None,
        task_vars: dict[str, Any] | None = None,
    ) -> bool:
        """Evaluate one production `when` exactly as Ansible would."""
        probe: dict[str, Any] = {
            "name": "Evaluate one reviewed condition",
            "ansible.builtin.assert": {"that": [condition], "quiet": True},
        }
        if item is not None:
            probe["loop"] = [item]
        if task_vars is not None:
            probe["vars"] = task_vars
        completed = run_task_definition(probe, variables)
        output = completed.stdout + completed.stderr
        self.assertTrue(completed.returncode == 0 or "evaluated_to" in output, output)
        return completed.returncode == 0

    # -- journal integrity ---------------------------------------------------

    def test_journal_integrity_reads_only_closed_files(self) -> None:
        journald = load_tasks(f"{HOST_BASELINE_ROLE}/tasks/journald.yml")
        names = list(journald)
        find = journald["Find the closed persistent journal files"]
        verify = journald["Verify persistent journal integrity"]
        self.assertLess(names.index(find["name"]), names.index(verify["name"]))
        self.assertEqual(find["ansible.builtin.find"]["patterns"], "*@*.journal")
        self.assertEqual(find["ansible.builtin.find"]["paths"], "/var/log/journal")
        # The active files journald is writing are never read by --verify.
        self.assertEqual(
            verify["ansible.builtin.command"]["argv"],
            [
                "/usr/bin/journalctl",
                "--verify",
                "--quiet",
                "--file=/var/log/journal/*/*@*.journal",
            ],
        )
        # With no closed file yet the glob would match nothing and fail, so
        # the check runs only when the find saw one.
        gate = "host_baseline_closed_journal_files.matched | default(0) > 0"
        self.assertIn(gate, verify["when"])
        self.assertFalse(
            self.condition_holds(
                gate, {"host_baseline_closed_journal_files": {"matched": 0}}
            )
        )
        self.assertTrue(
            self.condition_holds(
                gate, {"host_baseline_closed_journal_files": {"matched": 3}}
            )
        )

    # -- A. kernel settings -------------------------------------------------

    def test_sysctl_converges_each_drifted_key_never_sysctl_system(self) -> None:
        names = list(self.sysctl)
        converge = self.sysctl[self.CONVERGE]
        self.assertEqual(
            converge["ansible.builtin.command"]["argv"],
            ["/usr/sbin/sysctl", "-w", "{{ item.item.key }}={{ item.item.value }}"],
        )
        self.assertEqual(
            converge["loop"], "{{ host_baseline_observed_sysctl.results }}"
        )
        self.assertEqual(converge["when"], "item.stdout != item.item.value")
        self.assertIs(converge["changed_when"], True)
        observed = self.sysctl["Read every managed kernel setting before convergence"]
        self.assertEqual(observed["register"], "host_baseline_observed_sysctl")
        self.assertEqual(observed["loop"], "{{ host_baseline_sysctl | dict2items }}")
        self.assertIs(observed["check_mode"], False)
        self.assertLess(
            names.index(self.APPORT_STOP),
            names.index("Read every managed kernel setting before convergence"),
        )
        self.assertLess(
            names.index(self.CONVERGE),
            names.index("Read every managed kernel setting"),
        )
        self.assertLess(
            names.index("Read every managed kernel setting"),
            names.index("Verify every managed kernel setting"),
        )
        # The persisted file no longer triggers a blanket reload, and no task
        # or handler of the role runs `sysctl --system` except as a dry run.
        template = self.sysctl[
            "Install authoritative Swarm-compatible kernel hardening"
        ]
        self.assertNotIn("notify", template)
        role = PROJECT_ROOT / HOST_BASELINE_ROLE
        role_files = [
            *sorted((role / "tasks").glob("*.yml")),
            role / "handlers/main.yml",
        ]
        for path in role_files:
            relative = str(path.relative_to(PROJECT_ROOT))
            for argv in command_argvs(relative):
                with self.subTest(file=relative, argv=argv):
                    if argv[0] == "/usr/sbin/sysctl" and "--system" in argv:
                        self.assertIn("--dry-run", argv)
        handlers = (role / "handlers/main.yml").read_text(encoding="utf-8")
        self.assertNotIn("Host baseline apply sysctl", handlers)

    def test_sysctl_convergence_selects_only_drifted_keys(self) -> None:
        condition = self.sysctl[self.CONVERGE]["when"]
        managed = {"key": "fs.suid_dumpable", "value": "0"}
        self.assertTrue(
            self.condition_holds(condition, {}, {"stdout": "2", "item": managed})
        )
        self.assertFalse(
            self.condition_holds(condition, {}, {"stdout": "0", "item": managed})
        )

    def test_sysctl_postcheck_still_rejects_live_drift(self) -> None:
        name = "Verify every managed kernel setting"
        managed = {"key": "fs.suid_dumpable", "value": "0"}
        self.assert_task_accepts(
            HOST_BASELINE_SYSCTL,
            name,
            {
                "host_baseline_runtime_sysctl": {
                    "results": [{"stdout": "0", "item": managed}]
                }
            },
        )
        self.assert_task_rejects(
            HOST_BASELINE_SYSCTL,
            name,
            {
                "host_baseline_runtime_sysctl": {
                    "results": [{"stdout": "2", "item": managed}]
                }
            },
            "fs.suid_dumpable is 2, expected 0.",
        )

    def test_apport_crash_handler_is_stopped_and_disabled_not_masked(self) -> None:
        self.assertEqual(
            self.baseline_defaults["host_baseline_sysctl"]["fs.suid_dumpable"], "0"
        )
        self.assertEqual(
            self.baseline_defaults["host_baseline_apport_crash_handler_service"],
            "apport.service",
        )
        read = self.sysctl["Read the Apport kernel crash-handler unit"]
        self.assertEqual(
            read["ansible.builtin.command"]["argv"],
            [
                "/usr/bin/systemctl",
                "show",
                "--property=LoadState",
                "--value",
                "{{ host_baseline_apport_crash_handler_service }}",
            ],
        )
        self.assertIs(read["changed_when"], False)
        stop = self.sysctl[self.APPORT_STOP]
        self.assertEqual(
            stop["ansible.builtin.systemd_service"],
            {
                "name": "{{ host_baseline_apport_crash_handler_service }}",
                "enabled": False,
                "state": "stopped",
            },
        )
        # /etc/default/apport is deliberately left alone: apport.service does
        # not read it, and changing it would only silence userspace reports.
        role = PROJECT_ROOT / HOST_BASELINE_ROLE
        for path in sorted((role / "tasks").glob("*.yml")):
            for task in yaml.safe_load(path.read_text(encoding="utf-8")):
                for arguments in task.values():
                    if isinstance(arguments, dict):
                        with self.subTest(task=task["name"]):
                            self.assertNotIn(
                                "/etc/default/apport",
                                {arguments.get("path"), arguments.get("dest")},
                            )

    def test_apport_unit_state_gate_fails_closed(self) -> None:
        service = {"host_baseline_apport_crash_handler_service": "apport.service"}
        for state in ("loaded", "masked", "not-found"):
            with self.subTest(state=state):
                self.assert_task_accepts(
                    HOST_BASELINE_SYSCTL,
                    self.APPORT_GATE,
                    {
                        **service,
                        "host_baseline_apport_crash_handler": {"stdout": state},
                    },
                )
        for state in ("bad-setting", "error", ""):
            with self.subTest(state=state):
                self.assert_task_rejects(
                    HOST_BASELINE_SYSCTL,
                    self.APPORT_GATE,
                    {
                        **service,
                        "host_baseline_apport_crash_handler": {"stdout": state},
                    },
                    "cannot be proven",
                )

    def test_apport_is_stopped_only_when_its_unit_is_loaded(self) -> None:
        condition = self.sysctl[self.APPORT_STOP]["when"]
        expected = {"loaded": True, "masked": False, "not-found": False}
        for state, should_stop in expected.items():
            with self.subTest(state=state):
                self.assertEqual(
                    self.condition_holds(
                        condition,
                        {"host_baseline_apport_crash_handler": {"stdout": state}},
                    ),
                    should_stop,
                )

    # -- B. UFW default policies --------------------------------------------

    def ufw_variables(self, contents: str) -> dict[str, Any]:
        encoded = base64.b64encode(contents.encode("utf-8")).decode("ascii")
        return {"host_security_ufw_defaults_file": {"content": encoded}}

    def ufw_parse_vars(self) -> dict[str, Any]:
        """The production parse, evaluated lazily as a task variable."""
        parse = self.security[self.UFW_PARSE]["ansible.builtin.set_fact"]
        return {
            "host_security_ufw_default_policy_lines": parse[
                "host_security_ufw_default_policy_lines"
            ]
        }

    def ufw_directions_to_rewrite(self, contents: str) -> list[str]:
        task = self.security[self.UFW_DEFAULT]
        return [
            item["direction"]
            for item in task["loop"]
            if self.condition_holds(
                task["when"],
                self.ufw_variables(contents),
                item,
                self.ufw_parse_vars(),
            )
        ]

    def test_ufw_defaults_are_read_without_touching_ufw(self) -> None:
        names = list(self.security)
        read = self.security[
            "Read the persisted UFW default policies without touching UFW"
        ]
        self.assertEqual(read["ansible.builtin.slurp"], {"src": "/etc/default/ufw"})
        self.assertEqual(read["register"], "host_security_ufw_defaults_file")
        order = [
            "Keep IPv6 under the same UFW policy",
            "Read the persisted UFW default policies without touching UFW",
            self.UFW_PARSE,
            self.UFW_GATE,
            self.UFW_DEFAULT,
            "Rate-limit the sole administrative ingress",
        ]
        self.assertEqual(order, sorted(order, key=names.index))
        task = self.security[self.UFW_DEFAULT]
        # ufw maps incoming/outgoing/routed to INPUT/OUTPUT/FORWARD
        # (ufw/backend_iptables.py, set_default_policy).
        self.assertEqual(
            task["loop"],
            [
                {"direction": "incoming", "chain": "INPUT"},
                {"direction": "outgoing", "chain": "OUTPUT"},
                {"direction": "routed", "chain": "FORWARD"},
            ],
        )
        self.assertEqual(
            task["ansible.builtin.command"]["argv"],
            ["/usr/sbin/ufw", "--force", "default", "deny", "{{ item.direction }}"],
        )
        self.assertIn("when", task)
        self.assertIs(task["changed_when"], True)
        # Nothing else stops or restarts UFW; only the Reload UFW handler does,
        # and only when a managed UFW file really changed.
        default_calls = []
        for relative in (
            HOST_SECURITY_MAIN,
            f"{HOST_BASELINE_ROLE}/tasks/firewall.yml",
        ):
            for argv in command_argvs(relative):
                if argv[0] == "/usr/sbin/ufw":
                    with self.subTest(file=relative, argv=argv):
                        self.assertFalse({"reload", "disable", "reset"} & set(argv))
                    if "default" in argv:
                        default_calls.append(argv)
        self.assertEqual(default_calls, [task["ansible.builtin.command"]["argv"]])
        handlers = yaml.safe_load(
            (PROJECT_ROOT / HOST_SECURITY_HANDLERS).read_text(encoding="utf-8")
        )
        reloads = [
            handler["name"]
            for handler in handlers
            if "reload" in handler.get("ansible.builtin.command", {}).get("argv", [])
        ]
        self.assertEqual(reloads, ["Reload UFW"])

    def test_converged_ufw_defaults_do_not_restart_ufw(self) -> None:
        self.assertEqual(self.ufw_directions_to_rewrite(CONVERGED_UFW_DEFAULTS), [])
        unquoted = CONVERGED_UFW_DEFAULTS.replace('"DROP"', "DROP")
        self.assertEqual(self.ufw_directions_to_rewrite(unquoted), [])

    def test_ufw_default_rewrites_only_the_differing_direction(self) -> None:
        # Ubuntu's packaged default allows outgoing traffic.
        fresh = CONVERGED_UFW_DEFAULTS.replace(
            'DEFAULT_OUTPUT_POLICY="DROP"', 'DEFAULT_OUTPUT_POLICY="ACCEPT"'
        )
        self.assertEqual(self.ufw_directions_to_rewrite(fresh), ["outgoing"])
        rejecting = CONVERGED_UFW_DEFAULTS.replace(
            'DEFAULT_FORWARD_POLICY="DROP"', 'DEFAULT_FORWARD_POLICY="REJECT"'
        )
        self.assertEqual(self.ufw_directions_to_rewrite(rejecting), ["routed"])

    def test_unreadable_ufw_defaults_fail_closed(self) -> None:
        gate = dict(self.security[self.UFW_GATE])
        gate["vars"] = self.ufw_parse_vars()
        accepted = {
            "converged": CONVERGED_UFW_DEFAULTS,
            "fresh": CONVERGED_UFW_DEFAULTS.replace(
                'OUTPUT_POLICY="DROP"', 'OUTPUT_POLICY="ACCEPT"'
            ),
        }
        for case, contents in accepted.items():
            with self.subTest(case=case):
                completed = run_task_definition(gate, self.ufw_variables(contents))
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
        rejected = {
            "missing forward": CONVERGED_UFW_DEFAULTS.replace(
                'DEFAULT_FORWARD_POLICY="DROP"\n', ""
            ),
            "duplicate input": CONVERGED_UFW_DEFAULTS
            + 'DEFAULT_INPUT_POLICY="ACCEPT"\n',
            "trailing comment": CONVERGED_UFW_DEFAULTS.replace(
                'INPUT_POLICY="DROP"', 'INPUT_POLICY="DROP" # keep'
            ),
            "lower case": CONVERGED_UFW_DEFAULTS.replace(
                'OUTPUT_POLICY="DROP"', 'OUTPUT_POLICY="deny"'
            ),
            "empty": "",
        }
        for case, contents in rejected.items():
            with self.subTest(case=case):
                completed = run_task_definition(gate, self.ufw_variables(contents))
                output = completed.stdout + completed.stderr
                self.assertNotEqual(completed.returncode, 0, output)
                self.assertIn("cannot be proven", output)

    # -- C. PSAD logging in UFW ---------------------------------------------

    def managed_psad_block(self) -> str:
        """Render the block exactly as blockinfile writes it."""
        arguments = self.security[self.PSAD_BLOCK]["ansible.builtin.blockinfile"]
        marker = arguments["marker"]
        return (
            marker.replace("{mark}", "BEGIN")
            + "\n"
            + arguments["block"]
            + marker.replace("{mark}", "END")
            + "\n"
        )

    def remove_legacy_psad(self, contents: str) -> tuple[str, bool]:
        """ansible.builtin.replace without before/after: subn under MULTILINE.

        Its result is changed only when the substituted text differs.
        """
        arguments = self.security[self.PSAD_CLEANUP]["ansible.builtin.replace"]
        pattern = re.compile(arguments["regexp"], re.MULTILINE)
        result, count = pattern.subn(arguments["replace"], contents)
        return result, count > 0 and result != contents

    def test_psad_cleanup_leaves_the_managed_block_untouched(self) -> None:
        managed = UFW_BEFORE_RULES + self.managed_psad_block() + "COMMIT\n"
        self.assertEqual(self.remove_legacy_psad(managed), (managed, False))
        # Without any PSAD lines there is nothing to change either.
        fresh = UFW_BEFORE_RULES + "COMMIT\n"
        self.assertEqual(self.remove_legacy_psad(fresh), (fresh, False))

    def test_psad_cleanup_removes_only_unmarked_legacy_lines(self) -> None:
        block = self.managed_psad_block()
        cases = {
            "legacy only": (
                UFW_BEFORE_RULES + LEGACY_PSAD_LINES + "COMMIT\n",
                UFW_BEFORE_RULES + "COMMIT\n",
            ),
            "legacy before the block": (
                UFW_BEFORE_RULES + LEGACY_PSAD_LINES + block + "COMMIT\n",
                UFW_BEFORE_RULES + block + "COMMIT\n",
            ),
            "legacy after the block": (
                UFW_BEFORE_RULES + block + LEGACY_PSAD_LINES + "COMMIT\n",
                UFW_BEFORE_RULES + block + "COMMIT\n",
            ),
        }
        for case, (before, after) in cases.items():
            with self.subTest(case=case):
                self.assertEqual(self.remove_legacy_psad(before), (after, True))

    def test_psad_cleanup_is_bound_to_the_managed_markers(self) -> None:
        cleanup = self.security[self.PSAD_CLEANUP]
        block = self.security[self.PSAD_BLOCK]
        # The managed rules are byte-identical to the legacy ones, so a
        # per-line removal cannot tell them apart: that is what made every
        # apply rewrite before*.rules and notify Reload UFW.
        managed_rules = [
            line
            for line in block["ansible.builtin.blockinfile"]["block"].splitlines()
            if line.startswith("-A ")
        ]
        self.assertEqual(len(managed_rules), 2)
        for line in managed_rules:
            self.assertIn(line + "\n", LEGACY_PSAD_LINES)
        self.assertNotIn("ansible.builtin.lineinfile", cleanup)
        regexp = cleanup["ansible.builtin.replace"]["regexp"]
        # No Jinja delimiter, so templating hands the module this exact text.
        for delimiter in ("{{", "{%", "{#"):
            self.assertNotIn(delimiter, regexp)
        match = re.compile(regexp, re.MULTILINE).search(self.managed_psad_block())
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1) + "\n", self.managed_psad_block())
        self.assertEqual(cleanup["loop"], block["loop"])
        self.assertEqual(
            cleanup["ansible.builtin.replace"]["validate"],
            block["ansible.builtin.blockinfile"]["validate"],
        )
        self.assertEqual(cleanup["notify"], "Reload UFW")
        names = list(self.security)
        self.assertLess(names.index(self.PSAD_CLEANUP), names.index(self.PSAD_BLOCK))


if __name__ == "__main__":
    unittest.main()
