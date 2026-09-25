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
from typing import Any, ClassVar

import yaml

from ansible_task_harness import (
    AnsibleTaskAssertions,
    run_task_definition,
    run_task_definitions,
)

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
            "Verify Fail2ban and its SSH jail",
            "Verify PSAD runtime",
            "Wait for Chrony synchronization",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    by_name[name]["when"],
                    "not ansible_check_mode",
                )
        # The bouncer test is also gated on a change the running bouncer has
        # not validated, but check mode still comes first.
        self.assertEqual(
            by_name["Validate the credential-bearing CrowdSec bouncer"]["when"][0],
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
# /etc/ufw/ufw.conf as read on the production host on 2026-09-25. It is the
# packaged /usr/share/ufw/ufw.conf, which the ufw postinst copies on a fresh
# host, so a fresh host has the same LOGLEVEL.
CONVERGED_UFW_CONF = """\
# /etc/ufw/ufw.conf
#

# Set to yes to start on boot. If setting this remotely, be sure to add a rule
# to allow your remote connection before starting ufw. Eg: 'ufw allow 22/tcp'
ENABLED=yes

# Please use the 'ufw' command to set the loglevel. Eg: 'ufw logging medium'.
# See 'man ufw' for details.
LOGLEVEL=low
"""
# `gpg --batch --show-keys --with-colons` of the installed CrowdSec keyring,
# read on the production host on 2026-09-25: the public packagecloud key. The
# armored key the repository URL serves gives the same records. The primary
# fingerprint is the reviewed one in config/host-security.yml.
REVIEWED_PRIMARY_FPR = "6A89E3C2303A901A889971D3376ED5326E93CD0C"
CROWDSEC_SIGNING_FPR = "C358C5638A13ACC3A665A9419EB2753BF09DFB77"
CONVERGED_CROWDSEC_KEYRING = (
    "pub:-:4096:1:376ED5326E93CD0C:1623225161:::-:::escaESCA::::::23::0:\n"
    f"fpr:::::::::{REVIEWED_PRIMARY_FPR}:\n"
    "uid:-::::1623225161::09586E6C86015BEB2CCE7A9A39A0FB0971203264::"
    "https\\x3a//packagecloud.io/crowdsec/crowdsec "
    "(https\\x3a//packagecloud.io/docs#gpg_signing) "
    "<support@packagecloud.io>::::::::::0:\n"
    "sub:-:4096:1:9EB2753BF09DFB77:1623225161::::::esa::::::23:\n"
    f"fpr:::::::::{CROWDSEC_SIGNING_FPR}:\n"
)
OTHER_CROWDSEC_KEY = (
    "pub:-:4096:1:0123456789ABCDEF:1623225161:::-:::escaESCA::::::23::0:\n"
    "fpr:::::::::0123456789ABCDEF0123456789ABCDEF01234567:\n"
)
# What upstream could publish under the same primary key: gpg marks a revoked
# primary with validity `r` (field 2) and prints an expiry in field 7.
REVOKED_CROWDSEC_KEYRING = CONVERGED_CROWDSEC_KEYRING.replace("pub:-:", "pub:r:", 1)
EXPIRING_CROWDSEC_KEYRING = CONVERGED_CROWDSEC_KEYRING.replace(
    ":1623225161:::-:::escaESCA:", ":1623225161:1900000000::-:::escaESCA:", 1
)
ROTATED_CROWDSEC_KEYRING = (
    CONVERGED_CROWDSEC_KEYRING
    + "sub:-:4096:1:0011223344556677:1790000000::::::s::::::23:\n"
    + "fpr:::::::::8899AABBCCDDEEFF001122330011223344556677:\n"
)
# The packaged firewall-bouncer configuration (config/
# crowdsec-firewall-bouncer.yaml of cs-firewall-bouncer v0.0.34), reduced to
# the keys that matter here, with a placeholder instead of any API key.
FRESH_BOUNCER_CONFIG = """\
mode: iptables
api_url: http://127.0.0.1:8080/
api_key: <API_KEY>
iptables_chains:
  - INPUT
#  - FORWARD
#  - DOCKER-USER
"""
CONVERGED_BOUNCER_CONFIG = FRESH_BOUNCER_CONFIG.replace(
    "  - INPUT\n", "  - INPUT\n  - DOCKER-USER\n"
)
# Read on the production host on 2026-09-25: `systemctl show
# --timestamp=us+utc --property=ExecMainStartTimestamp --value` of the running
# bouncer, and the ctime of its configuration, of the configuration's
# directory and of its binary (`stat`). There is no `.local` configuration.
CONVERGED_BOUNCER_START = "Fri 2026-09-25 13:41:19.551411 UTC"
CONVERGED_BOUNCER_START_EPOCH = 1790343679.551411
CONVERGED_BOUNCER_CONFIG_CTIME = 1785272886.6325786
CONVERGED_BOUNCER_DIRECTORY_CTIME = 1785272886.6325786
CONVERGED_BOUNCER_BINARY_CTIME = 1784670575.391828
# Enough of `iptables -S` to tell whether INPUT jumps to the bouncer chain.
ENFORCING_RULESET = (
    "-P INPUT DROP\n-N CROWDSEC_CHAIN\n-A INPUT -j CROWDSEC_CHAIN\n"
    "-A DOCKER-USER -j CROWDSEC_CHAIN\n"
)
TORN_DOWN_RULESET = "-P INPUT DROP\n-N ufw-before-input\n"
# The chain is still defined and DOCKER-USER, which the ordering helper
# repopulates, still jumps to it, but INPUT does not: the chain name is in the
# ruleset while INPUT filters nothing.
UNHOOKED_RULESET = (
    "-P INPUT DROP\n-N CROWDSEC_CHAIN\n-A DOCKER-USER -j CROWDSEC_CHAIN\n"
)
# The same rule with a comment is a different rule, not the bouncer's hook.
COMMENTED_HOOK_RULESET = (
    "-P INPUT DROP\n-N CROWDSEC_CHAIN\n"
    '-A INPUT -m comment --comment "manual" -j CROWDSEC_CHAIN\n'
)


def load_tasks(relative_path: str) -> dict[str, dict[str, Any]]:
    tasks = yaml.safe_load((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
    return {task["name"]: task for task in tasks}


def load_nested_tasks(relative_path: str) -> dict[str, dict[str, Any]]:
    """Every task by name, including those inside block, rescue and always.

    Tasks keep the order Ansible runs them in on success: a block's own
    tasks, then its always section.
    """
    pending = yaml.safe_load((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
    tasks: dict[str, dict[str, Any]] = {}
    while pending:
        task = pending.pop(0)
        tasks[task["name"]] = task
        nested = [
            child
            for section in ("block", "rescue", "always")
            for child in task.get(section) or []
        ]
        pending[:0] = nested
    return tasks


def command_result(stdout: str, rc: int = 0) -> dict[str, Any]:
    """A registered command result as Ansible leaves it."""
    stdout = stdout.rstrip("\n")
    return {"rc": rc, "stdout": stdout, "stdout_lines": stdout.splitlines()}


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
        condition: str | list[str],
        variables: dict[str, Any],
        item: dict[str, Any] | None = None,
        task_vars: dict[str, Any] | None = None,
    ) -> bool:
        """Evaluate one production `when` exactly as Ansible would.

        A list, like a `when` list, holds only when every entry holds.
        """
        conditions = condition if isinstance(condition, list) else [condition]
        probe: dict[str, Any] = {
            "name": "Evaluate one reviewed condition",
            "ansible.builtin.assert": {"that": conditions, "quiet": True},
        }
        if item is not None:
            probe["loop"] = [item]
        if task_vars is not None:
            probe["vars"] = task_vars
        completed = run_task_definition(probe, variables)
        output = completed.stdout + completed.stderr
        self.assertTrue(completed.returncode == 0 or "evaluated_to" in output, output)
        return completed.returncode == 0

    def conditions_hold(
        self,
        condition: str | list[str],
        cases: dict[str, dict[str, Any]],
        task_vars: dict[str, Any] | None = None,
    ) -> dict[str, bool]:
        """condition_holds for several host states in one playbook run.

        Each case becomes its own assert task, with that case's registered
        results as task variables, so each is evaluated exactly as Ansible
        would. A case that fails for any reason other than the condition
        being false, such as a templating error, fails the test.
        """
        conditions = condition if isinstance(condition, list) else [condition]
        names = list(cases)
        probes = [
            {
                "name": f"Evaluate case {index}",
                "ansible.builtin.assert": {"that": conditions, "quiet": True},
                "vars": {**(task_vars or {}), **cases[name]},
                "ignore_errors": True,
            }
            for index, name in enumerate(names)
        ]
        completed = run_task_definitions(probes, {})
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        sections = re.split(
            r"^TASK \[Evaluate case (\d+)\] \**$",
            completed.stdout,
            flags=re.MULTILINE,
        )
        outcomes: dict[str, bool] = {}
        for index, body in zip(sections[1::2], sections[2::2], strict=True):
            status = re.search(r"^(ok|fatal): \[localhost\]", body, re.MULTILINE)
            if status is not None and status.group(1) == "ok":
                outcomes[names[int(index)]] = True
            elif status is not None and '"evaluated_to": false' in body:
                outcomes[names[int(index)]] = False
            else:
                self.fail(f"case {names[int(index)]!r} did not evaluate:\n{output}")
        self.assertEqual(list(outcomes), names, output)
        return outcomes

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

    # -- D. UFW log level ---------------------------------------------------

    UFW_LOGLEVEL_READ = "Read the persisted UFW log level without touching UFW"
    UFW_LOGLEVEL_PARSE = "Parse the persisted UFW log level"
    UFW_LOGLEVEL_GATE = "Require a UFW log-level key that ufw logging can rewrite"
    UFW_LOGGING = "Enable bounded UFW logging"

    def ufw_conf_variables(self, contents: str) -> dict[str, Any]:
        encoded = base64.b64encode(contents.encode("utf-8")).decode("ascii")
        return {"host_security_ufw_conf_file": {"content": encoded}}

    def ufw_loglevel_parse_vars(self) -> dict[str, Any]:
        """The production parse, evaluated lazily as a task variable."""
        parse = self.security[self.UFW_LOGLEVEL_PARSE]["ansible.builtin.set_fact"]
        return {
            "host_security_ufw_loglevel_lines": parse[
                "host_security_ufw_loglevel_lines"
            ]
        }

    def ufw_logging_runs(self, files: dict[str, str]) -> dict[str, bool]:
        return self.conditions_hold(
            self.security[self.UFW_LOGGING]["when"],
            {
                case: self.ufw_conf_variables(contents)
                for case, contents in files.items()
            },
            task_vars=self.ufw_loglevel_parse_vars(),
        )

    @staticmethod
    def ufw_logging_low(contents: str) -> str:
        """/etc/ufw/ufw.conf after `ufw logging low`.

        set_default() in ufw/backend.py of ufw 0.36.2: every line matching
        `^LOGLEVEL=` becomes `LOGLEVEL=low` and, if none matched, one is
        appended. Duplicates are rewritten, not removed.
        """
        pattern = re.compile(r"^LOGLEVEL=")
        lines = contents.splitlines(keepends=True)
        written = ["LOGLEVEL=low\n" if pattern.search(line) else line for line in lines]
        if not any(pattern.search(line) for line in lines):
            written.append("LOGLEVEL=low\n")
        return "".join(written)

    @staticmethod
    def ufw_log_level(contents: str) -> str | None:
        """The level ufw applies: _get_defaults() in ufw/backend.py 0.36.2."""
        pattern = re.compile(r'^\w+="?\w+"?')
        defaults: dict[str, str] = {}
        for line in contents.splitlines(keepends=True):
            if pattern.search(line):
                fields = re.split(r"=", line.strip())
                defaults[fields[0].lower()] = fields[1].lower().strip("\"'")
        return defaults.get("loglevel")

    def test_ufw_log_level_is_read_without_touching_ufw(self) -> None:
        names = list(self.security)
        read = self.security[self.UFW_LOGLEVEL_READ]
        self.assertEqual(read["ansible.builtin.slurp"], {"src": "/etc/ufw/ufw.conf"})
        self.assertEqual(read["register"], "host_security_ufw_conf_file")
        # The gate runs before the first UFW command, so a key ufw logging
        # cannot rewrite stops the apply with UFW untouched.
        order = [
            self.UFW_GATE,
            self.UFW_LOGLEVEL_READ,
            self.UFW_LOGLEVEL_PARSE,
            self.UFW_LOGLEVEL_GATE,
            self.UFW_DEFAULT,
            "Allow only reviewed host egress classes",
            self.UFW_LOGGING,
            "Enable the fail-closed firewall",
        ]
        self.assertEqual(order, sorted(order, key=names.index))
        logging = self.security[self.UFW_LOGGING]
        self.assertEqual(
            logging["ansible.builtin.command"]["argv"],
            ["/usr/sbin/ufw", "logging", "low"],
        )
        # ufw always answers "Logging enabled", so the task says changed
        # exactly when it runs, and it runs only for a drifted level.
        self.assertIs(logging["changed_when"], True)
        self.assertIn("when", logging)
        self.assertNotIn("register", logging)

    def test_converged_ufw_log_level_does_not_run_ufw_logging(self) -> None:
        converged = {
            "converged": CONVERGED_UFW_CONF,
            "quoted": CONVERGED_UFW_CONF.replace("LOGLEVEL=low", 'LOGLEVEL="low"'),
            # ufw logging rewrites duplicates instead of removing them, so a
            # file it already rewrote must count as converged.
            "duplicate low": CONVERGED_UFW_CONF + "LOGLEVEL=low\n",
            # ufw's own parser ignores an indented line.
            "indented extra": CONVERGED_UFW_CONF + "  LOGLEVEL=full\n",
        }
        for case, contents in converged.items():
            with self.subTest(case=case):
                self.assertEqual(self.ufw_log_level(contents), "low")
        self.assertEqual(
            self.ufw_logging_runs(converged), dict.fromkeys(converged, False)
        )

    def test_ufw_logging_converges_every_other_log_level_in_one_run(self) -> None:
        drifted = {
            level: CONVERGED_UFW_CONF.replace("LOGLEVEL=low", f"LOGLEVEL={level}")
            for level in ("off", "medium", "high", "full", '"medium"', "LOW")
        }
        drifted |= {
            # ufw's own parser skips these lines, so the level is not low.
            "single quotes": CONVERGED_UFW_CONF.replace("=low", "='low'"),
            "empty value": CONVERGED_UFW_CONF.replace("=low", "="),
            # ufw keeps "low # keep", which is no level.
            "trailing comment": CONVERGED_UFW_CONF.replace("=low", "=low # keep"),
            "unknown level": CONVERGED_UFW_CONF.replace("=low", "=on"),
            "missing": CONVERGED_UFW_CONF.replace("LOGLEVEL=low\n", ""),
            "duplicate": CONVERGED_UFW_CONF + "LOGLEVEL=high\n",
            "empty": "",
        }
        self.assertEqual(self.ufw_logging_runs(drifted), dict.fromkeys(drifted, True))
        # One run of `ufw logging low` leaves each of them converged, so the
        # next apply does not run it again.
        rewritten = {case: self.ufw_logging_low(text) for case, text in drifted.items()}
        for case, contents in rewritten.items():
            with self.subTest(case=case):
                self.assertEqual(self.ufw_log_level(contents), "low")
        self.assertEqual(
            self.ufw_logging_runs(rewritten), dict.fromkeys(rewritten, False)
        )

    def test_ufw_log_level_key_ufw_logging_cannot_rewrite_fails_closed(
        self,
    ) -> None:
        gate = dict(self.security[self.UFW_LOGLEVEL_GATE])
        gate["vars"] = self.ufw_loglevel_parse_vars()
        # Everything `ufw logging low` converges passes the gate.
        accepted = {
            "converged": CONVERGED_UFW_CONF,
            "missing": CONVERGED_UFW_CONF.replace("LOGLEVEL=low\n", ""),
            "duplicate": CONVERGED_UFW_CONF + "LOGLEVEL=high\n",
            "upper-case value": CONVERGED_UFW_CONF.replace("=low", "=LOW"),
            "trailing comment": CONVERGED_UFW_CONF.replace("=low", "=low # keep"),
            "empty": "",
        }
        for case, contents in accepted.items():
            with self.subTest(case=case):
                completed = run_task_definition(gate, self.ufw_conf_variables(contents))
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
        # ufw reads these keys case-insensitively, but set_default() only
        # rewrites `^LOGLEVEL=`, so no run of `ufw logging low` converges them.
        rejected = {
            "lower-case key": CONVERGED_UFW_CONF.replace("LOGLEVEL=", "loglevel="),
            "lower-case duplicate": CONVERGED_UFW_CONF + "loglevel=high\n",
            "mixed-case key": CONVERGED_UFW_CONF.replace("LOGLEVEL=", "LogLevel="),
        }
        for case, contents in rejected.items():
            with self.subTest(case=case):
                completed = run_task_definition(gate, self.ufw_conf_variables(contents))
                output = completed.stdout + completed.stderr
                self.assertNotEqual(completed.returncode, 0, output)
                self.assertIn("cannot be proven or converged", output)
        # The stray key survives `ufw logging low`, and when it comes last ufw
        # keeps applying it, so every later apply would rewrite ufw.conf again.
        self.assertEqual(
            self.ufw_log_level(self.ufw_logging_low(rejected["lower-case duplicate"])),
            "high",
        )

    # -- E. CrowdSec repository key -----------------------------------------

    KEY_STAT = "Inspect the installed CrowdSec repository keyring"
    KEY_SHAPE = "Require the CrowdSec keyring to be absent or a regular file"
    KEY_READ = "Read the installed CrowdSec keyring identity"
    KEY_FETCH = "Fetch the published CrowdSec repository key in memory"
    KEY_PUBLISHED_READ = "Read the published CrowdSec key identity"
    KEY_PUBLISHED_AUTH = "Authenticate the published CrowdSec key"
    KEY_CURRENT = "Recognize an already authenticated CrowdSec keyring"
    KEY_BLOCK = "Authenticate and install the CrowdSec repository key"
    CONVERGED_KEYRING_STAT: ClassVar[dict[str, Any]] = {
        "exists": True,
        "isreg": True,
        "islnk": False,
        "uid": 0,
        "gid": 0,
        "mode": "0644",
    }

    def keyring_variables(
        self,
        stat: dict[str, Any],
        identity: dict[str, Any] | None,
        published: str = CONVERGED_CROWDSEC_KEYRING,
    ) -> dict[str, Any]:
        """Registered results as the stat, gpg and uri tasks would leave them."""
        contract = yaml.safe_load(HOST_SECURITY_CONTRACT.read_text(encoding="utf-8"))
        return {
            "host_security_crowdsec_keyring": (
                "/etc/apt/keyrings/crowdsec_crowdsec-archive-keyring.gpg"
            ),
            "host_security_crowdsec_repository_key_fingerprint": contract[
                "host_security_crowdsec_repository_key_fingerprint"
            ],
            "host_security_crowdsec_keyring_file": {"stat": stat},
            # A read skipped by its `when` still registers a result.
            "host_security_crowdsec_installed_key_identity": identity
            or {"changed": False, "skipped": True},
            "host_security_crowdsec_published_key_identity": command_result(published),
        }

    def key_block_runs(
        self,
        cases: dict[str, tuple[dict[str, Any], dict[str, Any] | None, str]],
    ) -> dict[str, bool]:
        current = self.security[self.KEY_CURRENT]["ansible.builtin.set_fact"]
        return self.conditions_hold(
            self.security[self.KEY_BLOCK]["when"],
            {
                case: self.keyring_variables(stat, identity, published)
                for case, (stat, identity, published) in cases.items()
            },
            task_vars={
                "host_security_crowdsec_keyring_current": current[
                    "host_security_crowdsec_keyring_current"
                ]
            },
        )

    def test_crowdsec_key_is_downloaded_only_when_the_keyring_is_not_proven(
        self,
    ) -> None:
        names = list(self.security)
        order = [
            "Create the administrator APT keyring directory",
            self.KEY_STAT,
            self.KEY_SHAPE,
            self.KEY_READ,
            self.KEY_FETCH,
            self.KEY_PUBLISHED_READ,
            self.KEY_PUBLISHED_AUTH,
            self.KEY_CURRENT,
            self.KEY_BLOCK,
            "Configure the official CrowdSec repository",
        ]
        self.assertEqual(order, sorted(order, key=names.index))
        stat = self.security[self.KEY_STAT]
        self.assertEqual(
            stat["ansible.builtin.stat"],
            {"path": "{{ host_security_crowdsec_keyring }}", "follow": False},
        )
        self.assertEqual(stat["register"], "host_security_crowdsec_keyring_file")
        read = self.security[self.KEY_READ]
        self.assertEqual(
            read["ansible.builtin.command"]["argv"],
            [
                "/usr/bin/gpg",
                "--batch",
                "--show-keys",
                "--with-colons",
                "{{ host_security_crowdsec_keyring }}",
            ],
        )
        self.assertEqual(
            read["register"], "host_security_crowdsec_installed_key_identity"
        )
        self.assertIs(read["changed_when"], False)
        # An unreadable keyring falls back to the authenticated installation.
        self.assertIs(read["failed_when"], False)
        self.assertEqual(
            read["when"],
            "host_security_crowdsec_keyring_file.stat.isreg | default(false)",
        )
        # The published key is read on every apply, in memory only: a GET
        # without `dest` never reports changed (ansible.builtin.uri).
        fetch = self.security[self.KEY_FETCH]
        self.assertEqual(
            fetch["ansible.builtin.uri"],
            {
                "url": "{{ host_security_crowdsec_repository_key_url }}",
                "method": "GET",
                "return_content": True,
                "status_code": 200,
            },
        )
        self.assertEqual(fetch["register"], "host_security_crowdsec_published_key")
        self.assertEqual(fetch["when"], "not ansible_check_mode")
        published = self.security[self.KEY_PUBLISHED_READ]
        self.assertEqual(
            published["ansible.builtin.command"],
            {
                "argv": ["/usr/bin/gpg", "--batch", "--show-keys", "--with-colons"],
                "stdin": "{{ host_security_crowdsec_published_key.content }}",
            },
        )
        self.assertEqual(
            published["register"], "host_security_crowdsec_published_key_identity"
        )
        self.assertIs(published["changed_when"], False)
        self.assertEqual(published["when"], "not ansible_check_mode")
        block = self.security[self.KEY_BLOCK]
        self.assertEqual(
            block["when"],
            ["not ansible_check_mode", "not host_security_crowdsec_keyring_current"],
        )
        # The unchanged staging, download, authentication and atomic install
        # run inside the block; the staging removal stays in `always`.
        inner = [task["name"] for task in block["block"]]
        self.assertEqual(
            inner,
            [
                "Allocate private CrowdSec key staging",
                "Download the official CrowdSec repository key",
                "Read the staged CrowdSec key identity",
                "Authenticate the staged CrowdSec key",
                "Dearmor the authenticated CrowdSec key",
                "Atomically install the authenticated CrowdSec key",
            ],
        )
        self.assertEqual(
            [task["name"] for task in block["always"]],
            ["Remove private CrowdSec key staging"],
        )
        download = block["block"][1]["ansible.builtin.get_url"]
        self.assertIs(download["force"], True)
        self.assertEqual(download["url"], fetch["ansible.builtin.uri"]["url"])
        install = block["block"][5]["ansible.builtin.copy"]
        # The gate accepts exactly the ownership and mode the install sets.
        self.assertEqual(
            (install["owner"], install["group"], install["mode"]),
            ("root", "root", self.CONVERGED_KEYRING_STAT["mode"]),
        )
        self.assertEqual(install["dest"], "{{ host_security_crowdsec_keyring }}")

    def test_converged_crowdsec_keyring_skips_the_key_download(self) -> None:
        contract = yaml.safe_load(HOST_SECURITY_CONTRACT.read_text(encoding="utf-8"))
        reviewed = contract["host_security_crowdsec_repository_key_fingerprint"]
        self.assertEqual(reviewed, REVIEWED_PRIMARY_FPR)
        # The primary key's `fpr` record, as gpg printed it on the host.
        self.assertEqual(
            CONVERGED_CROWDSEC_KEYRING.splitlines()[1], f"fpr:::::::::{reviewed}:"
        )
        converged = command_result(CONVERGED_CROWDSEC_KEYRING)
        self.assertEqual(
            self.key_block_runs(
                {
                    "converged": (
                        self.CONVERGED_KEYRING_STAT,
                        converged,
                        CONVERGED_CROWDSEC_KEYRING,
                    )
                }
            ),
            {"converged": False},
        )

    def test_crowdsec_key_is_authenticated_again_unless_proven_current(
        self,
    ) -> None:
        converged = command_result(CONVERGED_CROWDSEC_KEYRING)
        stat = self.CONVERGED_KEYRING_STAT
        published = CONVERGED_CROWDSEC_KEYRING
        cases: dict[str, tuple[dict[str, Any], dict[str, Any] | None, str]] = {
            "first install": ({"exists": False}, None, published),
            "fingerprint mismatch": (
                stat,
                command_result(OTHER_CROWDSEC_KEY),
                published,
            ),
            "second primary key": (
                stat,
                command_result(CONVERGED_CROWDSEC_KEYRING + OTHER_CROWDSEC_KEY),
                published,
            ),
            "reviewed fingerprint only on a subkey": (
                stat,
                command_result(
                    OTHER_CROWDSEC_KEY
                    + "sub:-:4096:1:376ED5326E93CD0C:1623225161::::::esa::::::23:\n"
                    + f"fpr:::::::::{REVIEWED_PRIMARY_FPR}:\n"
                ),
                published,
            ),
            "reviewed fingerprint in another field": (
                stat,
                command_result(
                    CONVERGED_CROWDSEC_KEYRING.replace(
                        f"fpr:::::::::{REVIEWED_PRIMARY_FPR}:",
                        f"fpr::::::::{REVIEWED_PRIMARY_FPR}::",
                    )
                ),
                published,
            ),
            "unreadable keyring": (
                stat,
                command_result(CONVERGED_CROWDSEC_KEYRING, rc=2),
                published,
            ),
            "no key": (stat, command_result(""), published),
            "private mode": ({**stat, "mode": "0600"}, converged, published),
            "writable mode": ({**stat, "mode": "0666"}, converged, published),
            "foreign owner": ({**stat, "uid": 1001}, converged, published),
            "foreign group": ({**stat, "gid": 1001}, converged, published),
            "not a regular file": (
                {**stat, "isreg": False, "islnk": True},
                converged,
                published,
            ),
            # Upstream changed the certificate under the same primary key:
            # the installed copy is refreshed, as every apply used to do.
            "upstream revoked the key": (stat, converged, REVOKED_CROWDSEC_KEYRING),
            "upstream set an expiry": (stat, converged, EXPIRING_CROWDSEC_KEYRING),
            "upstream added a signing subkey": (
                stat,
                converged,
                ROTATED_CROWDSEC_KEYRING,
            ),
        }
        self.assertEqual(self.key_block_runs(cases), dict.fromkeys(cases, True))

    def test_published_crowdsec_key_is_authenticated_before_any_comparison(
        self,
    ) -> None:
        gate = self.security[self.KEY_PUBLISHED_AUTH]
        accepted = {
            "converged": CONVERGED_CROWDSEC_KEYRING,
            # Same primary key: installed, so apt stops trusting it too.
            "revoked": REVOKED_CROWDSEC_KEYRING,
            "rotated subkey": ROTATED_CROWDSEC_KEYRING,
        }
        for case, published in accepted.items():
            with self.subTest(case=case):
                completed = run_task_definition(
                    gate,
                    self.keyring_variables({"exists": False}, None, published),
                )
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
        rejected = {
            "other key": OTHER_CROWDSEC_KEY,
            "second primary key": CONVERGED_CROWDSEC_KEYRING + OTHER_CROWDSEC_KEY,
            "reviewed fingerprint only on a subkey": (
                OTHER_CROWDSEC_KEY
                + "sub:-:4096:1:376ED5326E93CD0C:1623225161::::::esa::::::23:\n"
                + f"fpr:::::::::{REVIEWED_PRIMARY_FPR}:\n"
            ),
        }
        for case, published in rejected.items():
            with self.subTest(case=case):
                completed = run_task_definition(
                    gate,
                    self.keyring_variables({"exists": False}, None, published),
                )
                output = completed.stdout + completed.stderr
                self.assertNotEqual(completed.returncode, 0, output)
                self.assertIn("differs from the reviewed fingerprint", output)

    def test_crowdsec_keyring_path_must_be_absent_or_a_regular_file(self) -> None:
        gate = self.security[self.KEY_SHAPE]
        for case, stat in {
            "absent": {"exists": False},
            "regular": self.CONVERGED_KEYRING_STAT,
        }.items():
            with self.subTest(case=case):
                completed = run_task_definition(
                    gate, self.keyring_variables(stat, None)
                )
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
        for case, stat in {
            "symbolic link": {"exists": True, "isreg": False, "islnk": True},
            "directory": {"exists": True, "isreg": False, "isdir": True},
        }.items():
            with self.subTest(case=case):
                completed = run_task_definition(
                    gate, self.keyring_variables(stat, None)
                )
                output = completed.stdout + completed.stderr
                self.assertNotEqual(completed.returncode, 0, output)
                self.assertIn("cannot be proven", output)

    def test_downloaded_crowdsec_key_is_still_authenticated(self) -> None:
        block = self.security[self.KEY_BLOCK]
        authenticate = next(
            task
            for task in block["block"]
            if task["name"] == "Authenticate the staged CrowdSec key"
        )
        variables = self.keyring_variables({"exists": False}, None)
        variables["host_security_crowdsec_key_identity"] = command_result(
            CONVERGED_CROWDSEC_KEYRING
        )
        completed = run_task_definition(authenticate, variables)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        variables["host_security_crowdsec_key_identity"] = command_result(
            OTHER_CROWDSEC_KEY
        )
        completed = run_task_definition(authenticate, variables)
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0, output)
        self.assertIn("differs from the reviewed fingerprint", output)

    # -- F. CrowdSec firewall-bouncer tests ---------------------------------

    BOUNCER_TEST_SECURITY = "Validate the credential-bearing CrowdSec bouncer"
    BOUNCER_TEST_BASELINE = "Validate the CrowdSec firewall-bouncer configuration"
    BOUNCER_TEST_CANDIDATE = "Parse the credential-bearing bouncer candidate in memory"
    BOUNCER_HOOK = "Register DOCKER-USER with the CrowdSec firewall bouncer"
    BOUNCER_HOOK_BLOCK = "Test the CrowdSec bouncer and register DOCKER-USER"
    BOUNCER_FILES = "Inspect the CrowdSec bouncer files the running instance loaded"
    BOUNCER_START = "Read when the running CrowdSec bouncer started"
    BOUNCER_UNVALIDATED = (
        "Detect CrowdSec bouncer files the running instance never validated"
    )
    REQUIRE_TEST_SECURITY = "Require a passing CrowdSec bouncer test"
    REQUIRE_TEST_BASELINE = "Require a passing CrowdSec bouncer configuration test"
    RESTORE = "Restore CrowdSec enforcement when its INPUT hook is missing"
    REQUIRE_RESTORE = "Require the restored CrowdSec bouncer to start"
    BASELINE_BOUNCER_TASKS = f"{HOST_BASELINE_ROLE}/tasks/crowdsec-docker.yml"
    PACKAGES = "Install the reviewed host-security packages"
    INPUT_HOOK = "-A INPUT -j CROWDSEC_CHAIN"
    PACKAGE_RESULTS: ClassVar[dict[str, dict[str, Any]]] = {
        "installed or upgraded": {"changed": True},
        "unchanged": {"changed": False},
        "skipped": {"changed": False, "skipped": True},
    }

    @classmethod
    def bouncer_task_files(cls) -> dict[str, tuple[dict[str, Any], str]]:
        """Where each role tests the bouncer, and its variable prefix."""
        return {
            "host_security": (cls.security, "host_security"),
            "host_baseline": (
                load_nested_tasks(cls.BASELINE_BOUNCER_TASKS),
                "host_baseline",
            ),
        }

    def bouncer_source_variables(self, contents: str) -> dict[str, Any]:
        encoded = base64.b64encode(contents.encode("utf-8")).decode("ascii")
        return {"host_baseline_crowdsec_bouncer_source": {"content": encoded}}

    @staticmethod
    def regular_file(ctime: float) -> dict[str, Any]:
        return {
            "exists": True,
            "isreg": True,
            "isdir": False,
            "islnk": False,
            "ctime": ctime,
        }

    @staticmethod
    def directory(ctime: float) -> dict[str, Any]:
        return {
            "exists": True,
            "isreg": False,
            "isdir": True,
            "islnk": False,
            "ctime": ctime,
        }

    @staticmethod
    def symbolic_link(ctime: float) -> dict[str, Any]:
        return {
            "exists": True,
            "isreg": False,
            "isdir": False,
            "islnk": True,
            "ctime": ctime,
        }

    def bouncer_state(
        self,
        tasks: dict[str, dict[str, Any]],
        prefix: str,
        *,
        start: str = CONVERGED_BOUNCER_START,
        config: dict[str, Any] | None = None,
        local: dict[str, Any] | None = None,
        directory: dict[str, Any] | None = None,
        binary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Registered stat and systemctl results for one host state.

        The defaults are the production host read on 2026-09-25.
        """
        stats = [
            config or self.regular_file(CONVERGED_BOUNCER_CONFIG_CTIME),
            local or {"exists": False},
            directory or self.directory(CONVERGED_BOUNCER_DIRECTORY_CTIME),
            binary or self.regular_file(CONVERGED_BOUNCER_BINARY_CTIME),
        ]
        items = tasks[self.BOUNCER_FILES]["loop"]
        return {
            f"{prefix}_crowdsec_bouncer_files": {
                "results": [
                    {"item": item, "stat": stat}
                    for item, stat in zip(items, stats, strict=True)
                ]
            },
            f"{prefix}_crowdsec_bouncer_start": command_result(start),
        }

    def unvalidated_vars(
        self, tasks: dict[str, dict[str, Any]], prefix: str
    ) -> dict[str, Any]:
        """The production detection, evaluated lazily as task variables."""
        detect = tasks[self.BOUNCER_UNVALIDATED]
        name = f"{prefix}_crowdsec_bouncer_unvalidated"
        return {name: detect["ansible.builtin.set_fact"][name], **detect["vars"]}

    def test_every_bouncer_test_is_gated_on_a_change(self) -> None:
        """`-t` tears the live chains down, so none may run unconditionally.

        Nor may any run in check mode: a dry run cannot leave the host
        unfiltered.
        """
        expected = {
            HOST_SECURITY_MAIN: [self.BOUNCER_TEST_SECURITY],
            self.BASELINE_BOUNCER_TASKS: [
                self.BOUNCER_TEST_BASELINE,
                self.BOUNCER_TEST_CANDIDATE,
            ],
        }
        command_modules = {
            f"{namespace}{module}"
            for namespace in ("", "ansible.builtin.", "ansible.legacy.")
            for module in ("command", "shell")
        }
        sections = (
            "block",
            "rescue",
            "always",
            "tasks",
            "pre_tasks",
            "post_tasks",
            "handlers",
        )
        found: dict[str, list[str]] = {}
        validators: list[str] = []
        for path in sorted((PROJECT_ROOT / "ansible").rglob("*.yml")):
            relative = str(path.relative_to(PROJECT_ROOT))
            if "crowdsec-firewall-bouncer -c" in path.read_text(encoding="utf-8"):
                validators.append(relative)
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            # Each entry carries the `when` conditions and the `check_mode`
            # it inherits from its enclosing blocks or play.
            pending: list[tuple[Any, list[Any], Any]] = [
                (entry, [], None)
                for entry in (loaded if isinstance(loaded, list) else [])
            ]
            tasks: list[tuple[dict[str, Any], list[Any], Any]] = []
            # Role task files and handlers are task lists; playbooks are plays
            # whose tasks sit under tasks, pre_tasks, post_tasks and handlers.
            while pending:
                task, inherited_when, inherited_check_mode = pending.pop(0)
                if not isinstance(task, dict):
                    continue
                own_when = task.get("when")
                if not isinstance(own_when, list):
                    own_when = [own_when]
                conditions = inherited_when + [
                    condition for condition in own_when if condition is not None
                ]
                check_mode = task.get("check_mode", inherited_check_mode)
                tasks.append((task, conditions, check_mode))
                for section in sections:
                    pending.extend(
                        (child, conditions, check_mode)
                        for child in task.get(section) or []
                    )
            for task, conditions, check_mode in tasks:
                for module in command_modules.intersection(task):
                    command = task[module]
                    if isinstance(command, dict):
                        argv = (
                            command.get("argv")
                            or str(
                                command.get("cmd") or command.get("_raw_params") or ""
                            ).split()
                        )
                    else:
                        argv = str(command or "").split()
                    if not any(
                        Path(str(word)).name == "crowdsec-firewall-bouncer"
                        for word in argv
                    ):
                        continue
                    found.setdefault(relative, []).append(task["name"])
                    with self.subTest(file=relative, task=task["name"]):
                        self.assertEqual(module, "ansible.builtin.command")
                        self.assertEqual(argv[0], "/usr/bin/crowdsec-firewall-bouncer")
                        self.assertIn("-t", argv)
                        # Something besides check mode must gate it.
                        self.assertTrue(set(conditions) - {"not ansible_check_mode"})
                        # And check mode must skip it: either the task says
                        # so, or nothing forces it to run in a dry run.
                        self.assertTrue(
                            "not ansible_check_mode" in conditions
                            or check_mode is not False,
                            f"{task['name']} runs in check mode",
                        )
        self.assertEqual(found, expected)
        # The only other bouncer test is lineinfile's `validate`, which runs
        # only when the file is about to be rewritten, and never in check
        # mode.
        self.assertEqual(validators, [self.BASELINE_BOUNCER_TASKS])
        docker = load_tasks(self.BASELINE_BOUNCER_TASKS)
        hook = load_nested_tasks(self.BASELINE_BOUNCER_TASKS)[self.BOUNCER_HOOK]
        self.assertEqual(
            hook["ansible.builtin.lineinfile"]["validate"],
            "/usr/bin/crowdsec-firewall-bouncer -c %s -t",
        )
        self.assertEqual(hook["ansible.builtin.lineinfile"]["line"], "  - DOCKER-USER")
        self.assertNotIn("when", hook)
        self.assertNotIn("check_mode", hook)
        self.assertNotIn("check_mode", docker[self.BOUNCER_HOOK_BLOCK])

    def test_bouncer_freshness_is_read_without_side_effects(self) -> None:
        for role, (tasks, prefix) in self.bouncer_task_files().items():
            with self.subTest(role=role):
                names = list(tasks)
                test = (
                    self.BOUNCER_TEST_SECURITY
                    if role == "host_security"
                    else self.BOUNCER_TEST_BASELINE
                )
                order = [
                    self.BOUNCER_FILES,
                    self.BOUNCER_START,
                    self.BOUNCER_UNVALIDATED,
                    test,
                ]
                self.assertEqual(order, sorted(order, key=names.index))
                files = tasks[self.BOUNCER_FILES]
                self.assertEqual(
                    files["ansible.builtin.stat"],
                    {
                        "path": "{{ item.path }}",
                        "follow": False,
                        "get_checksum": False,
                    },
                )
                config = f"{{{{ {prefix}_crowdsec_bouncer_config }}}}"
                # The bouncer merges `<config>.local` into its configuration
                # (MergedConfig in pkg/cfg/config.go), so both are checked,
                # and so is their directory, whose ctime still moves when a
                # `.local` is removed and leaves no file to stat.
                self.assertEqual(
                    files["loop"],
                    [
                        {"path": config, "type": "file", "required": True},
                        {"path": f"{config}.local", "type": "file", "required": False},
                        {
                            "path": (
                                f"{{{{ {prefix}_crowdsec_bouncer_config | dirname }}}}"
                            ),
                            "type": "directory",
                            "required": True,
                        },
                        {
                            "path": "/usr/bin/crowdsec-firewall-bouncer",
                            "type": "file",
                            "required": True,
                        },
                    ],
                )
                self.assertEqual(files["register"], f"{prefix}_crowdsec_bouncer_files")
                start = tasks[self.BOUNCER_START]
                self.assertEqual(
                    start["ansible.builtin.command"]["argv"],
                    [
                        "/usr/bin/systemctl",
                        "show",
                        "--timestamp=us+utc",
                        "--property=ExecMainStartTimestamp",
                        "--value",
                        "crowdsec-firewall-bouncer.service",
                    ],
                )
                self.assertEqual(start["environment"], {"LC_ALL": "C"})
                self.assertIs(start["changed_when"], False)
                self.assertIs(start["check_mode"], False)
                self.assertEqual(start["register"], f"{prefix}_crowdsec_bouncer_start")

    def test_bouncer_start_parses_to_its_unix_time(self) -> None:
        """`systemctl show --timestamp=unix` gave @1790343679 on the host."""
        for role, (tasks, prefix) in self.bouncer_task_files().items():
            with self.subTest(role=role):
                started_at = f"{prefix}_crowdsec_bouncer_started_at"
                self.assertTrue(
                    self.condition_holds(
                        f"{started_at} == {CONVERGED_BOUNCER_START_EPOCH}",
                        self.bouncer_state(tasks, prefix),
                        task_vars=self.unvalidated_vars(tasks, prefix),
                    )
                )

    def test_bouncer_is_tested_only_when_the_running_instance_never_saw_it(
        self,
    ) -> None:
        """A file newer than the running bouncer is tested before any restart."""
        start = CONVERGED_BOUNCER_START_EPOCH
        states: dict[str, tuple[dict[str, Any], bool]] = {
            "converged": ({}, False),
            "configuration edited after the start": (
                {"config": self.regular_file(start + 3600)},
                True,
            ),
            # chmod and chown change the ctime too.
            "configuration touched in the same microsecond": (
                {"config": self.regular_file(start)},
                True,
            ),
            "local override added after the start": (
                {"local": self.regular_file(start + 1)},
                True,
            ),
            "local override older than the start": (
                {"local": self.regular_file(start - 1)},
                False,
            ),
            # Removing the `.local` leaves no file to stat; only its
            # directory's ctime records it.
            "local override removed after the start": (
                {"directory": self.directory(start + 1)},
                True,
            ),
            "configuration directory missing": ({"directory": {"exists": False}}, True),
            "configuration directory is a symbolic link": (
                {"directory": self.symbolic_link(start - 1)},
                True,
            ),
            "regular file where the directory should be": (
                {"directory": self.regular_file(start - 1)},
                True,
            ),
            "directory where the configuration should be": (
                {"config": self.directory(start - 1)},
                True,
            ),
            "binary replaced without a restart": (
                {"binary": self.regular_file(start + 60)},
                True,
            ),
            "configuration missing": ({"config": {"exists": False}}, True),
            "configuration is a symbolic link": (
                {"config": self.symbolic_link(start - 1)},
                True,
            ),
            # systemctl prints an empty value for a unit that never started.
            "bouncer never started": ({"start": ""}, True),
            "unreadable start": ({"start": "n/a"}, True),
        }
        for role, (tasks, prefix) in self.bouncer_task_files().items():
            test = (
                self.BOUNCER_TEST_SECURITY
                if role == "host_security"
                else self.BOUNCER_TEST_BASELINE
            )
            package = {"host_security_package_install": {"changed": False}}
            decisions = self.conditions_hold(
                tasks[test]["when"],
                {
                    case: {**self.bouncer_state(tasks, prefix, **state), **package}
                    for case, (state, _) in states.items()
                },
                task_vars=self.unvalidated_vars(tasks, prefix),
            )
            with self.subTest(role=role):
                self.assertEqual(
                    decisions,
                    {case: expected for case, (_, expected) in states.items()},
                )

    def test_host_security_tests_the_bouncer_after_a_package_change(
        self,
    ) -> None:
        names = list(self.security)
        packages = self.security[self.PACKAGES]
        self.assertEqual(packages["register"], "host_security_package_install")
        self.assertLess(
            names.index(self.PACKAGES), names.index(self.BOUNCER_TEST_SECURITY)
        )
        test = self.security[self.BOUNCER_TEST_SECURITY]
        self.assertEqual(test["when"][0], "not ansible_check_mode")
        expected = {"installed or upgraded": True, "unchanged": False, "skipped": False}
        decisions = self.conditions_hold(
            test["when"],
            {
                case: {
                    **self.bouncer_state(self.security, "host_security"),
                    "host_security_package_install": result,
                }
                for case, result in self.PACKAGE_RESULTS.items()
            },
            task_vars=self.unvalidated_vars(self.security, "host_security"),
        )
        self.assertEqual(decisions, expected)

    def test_host_baseline_repeats_no_bouncer_test_host_security_ran(
        self,
    ) -> None:
        tasks = load_nested_tasks(self.BASELINE_BOUNCER_TASKS)
        condition = tasks[self.BOUNCER_TEST_BASELINE]["when"]
        # host_security's own test and restore restart the bouncer, so the
        # files are older than the running instance by the time host_baseline
        # reads them: whatever the package result, it is not tested twice.
        cases = {
            case: {
                **self.bouncer_state(tasks, "host_baseline"),
                "host_security_package_install": result,
            }
            for case, result in self.PACKAGE_RESULTS.items()
        }
        # Without host_security in the play nothing proves the package did not
        # change, so the test runs.
        cases["without host_security"] = self.bouncer_state(tasks, "host_baseline")
        decisions = self.conditions_hold(
            condition, cases, task_vars=self.unvalidated_vars(tasks, "host_baseline")
        )
        self.assertEqual(
            decisions,
            {
                "installed or upgraded": False,
                "unchanged": False,
                "skipped": False,
                "without host_security": True,
            },
        )

    def test_bouncer_candidate_is_tested_only_when_the_hook_is_missing(
        self,
    ) -> None:
        tasks = load_nested_tasks(self.BASELINE_BOUNCER_TASKS)
        names = list(tasks)
        self.assertLess(
            names.index(self.BOUNCER_TEST_CANDIDATE), names.index(self.BOUNCER_HOOK)
        )
        condition = tasks[self.BOUNCER_TEST_CANDIDATE]["when"]
        cases = {
            # Only the packaged comment "#  - DOCKER-USER": the hook is missing.
            "fresh": (FRESH_BOUNCER_CONFIG, True),
            "converged": (CONVERGED_BOUNCER_CONFIG, False),
            "duplicate hook": (
                CONVERGED_BOUNCER_CONFIG + "  - DOCKER-USER\n",
                True,
            ),
        }
        decisions = self.conditions_hold(
            condition,
            {
                case: self.bouncer_source_variables(contents)
                for case, (contents, _) in cases.items()
            },
        )
        self.assertEqual(
            decisions, {case: expected for case, (_, expected) in cases.items()}
        )

    def test_failed_bouncer_test_stops_the_apply_only_after_the_restore(
        self,
    ) -> None:
        """Every `-t` result is demanded only after the restore has been tried.

        A `-t` that fails after backend.Init has already removed the chains.
        This pins the order and the messages, not that the restore succeeds:
        when the on-disk configuration is what failed, the restart runs the
        same test (ExecStartPre) and fails too, and that failure stops the
        apply with its own message.
        """
        security = self.security
        test = security[self.BOUNCER_TEST_SECURITY]
        self.assertEqual(test["register"], "host_security_crowdsec_bouncer_test")
        self.assertIs(test["failed_when"], False)
        names = list(security)
        order = [
            self.BOUNCER_TEST_SECURITY,
            "Read the live CrowdSec IPv4 firewall enforcement",
            "Read the live CrowdSec IPv6 firewall enforcement",
            self.RESTORE,
            self.REQUIRE_RESTORE,
            "Re-read the live CrowdSec IPv4 firewall enforcement",
            "Re-read the live CrowdSec IPv6 firewall enforcement",
            "Verify CrowdSec is enforcing on both address families",
            self.REQUIRE_TEST_SECURITY,
        ]
        self.assertEqual(order, sorted(order, key=names.index))
        self.assertNotIn("when", security[self.REQUIRE_TEST_SECURITY])

        docker = load_tasks(self.BASELINE_BOUNCER_TASKS)
        block = docker[self.BOUNCER_HOOK_BLOCK]
        # Every host_baseline bouncer test, including the one on the on-disk
        # file, runs inside the block, so no task that fails between a test
        # and the restore can stop the play first. security.yml's result is
        # demanded straight away, so a known-bad configuration is neither
        # tested again nor rewritten.
        self.assertEqual(
            [task["name"] for task in block["block"]],
            [
                self.BOUNCER_FILES,
                self.BOUNCER_START,
                self.BOUNCER_UNVALIDATED,
                self.BOUNCER_TEST_BASELINE,
                self.REQUIRE_TEST_BASELINE,
                self.BOUNCER_TEST_CANDIDATE,
                self.BOUNCER_HOOK,
            ],
        )
        self.assertEqual(
            [task["name"] for task in block["always"]],
            [
                "Read the live CrowdSec IPv4 enforcement after the bouncer tests",
                "Read the live CrowdSec IPv6 enforcement after the bouncer tests",
                self.RESTORE,
                self.REQUIRE_RESTORE,
                "Wait for CrowdSec IPv4 enforcement to return",
                "Wait for CrowdSec IPv6 enforcement to return",
            ],
        )
        security_yml = load_tasks(f"{HOST_BASELINE_ROLE}/tasks/security.yml")
        self.assertNotIn(self.BOUNCER_TEST_BASELINE, security_yml)
        baseline = {task["name"]: task for task in block["block"] + block["always"]}
        baseline_test = baseline[self.BOUNCER_TEST_BASELINE]
        self.assertEqual(
            baseline_test["register"], "host_baseline_crowdsec_bouncer_test"
        )
        self.assertIs(baseline_test["failed_when"], False)
        # A rescue would swallow the failure and let the play continue.
        self.assertNotIn("rescue", block)
        self.assertNotIn("when", block)
        self.assertNotIn("failed_when", baseline[self.BOUNCER_TEST_CANDIDATE])
        for task, register in (
            (
                security[self.REQUIRE_TEST_SECURITY],
                "host_security_crowdsec_bouncer_test",
            ),
            (
                baseline[self.REQUIRE_TEST_BASELINE],
                "host_baseline_crowdsec_bouncer_test",
            ),
        ):
            for result, passes in (
                ({"changed": False, "skipped": True}, True),
                ({"changed": False, "rc": 0}, True),
                ({"changed": False, "rc": 1}, False),
            ):
                with self.subTest(task=task["name"], result=result):
                    completed = run_task_definition(task, {register: result})
                    output = completed.stdout + completed.stderr
                    self.assertEqual(completed.returncode == 0, passes, output)
                    if not passes:
                        self.assertIn("-t rejected the", output)
                        self.assertIn("next start", output)

        # The restore's own failure is recorded, not ignored: the next task
        # stops the apply and says why CrowdSec stays down.
        for tasks, register in (
            (security, "host_security_crowdsec_bouncer_restore"),
            (baseline, "host_baseline_crowdsec_bouncer_restore"),
        ):
            restore = tasks[self.RESTORE]
            require = tasks[self.REQUIRE_RESTORE]
            self.assertEqual(restore["register"], register)
            self.assertIs(restore["ignore_errors"], True)
            self.assertNotIn("failed_when", restore)
            self.assertNotIn("when", require)
            for result, passes in (
                ({"changed": False, "skipped": True}, True),
                ({"changed": True, "state": "started"}, True),
                (
                    {
                        "changed": False,
                        "failed": True,
                        "msg": "Unable to restart service crowdsec-firewall-bouncer",
                    },
                    False,
                ),
            ):
                with self.subTest(
                    task=require["name"], register=register, result=result
                ):
                    completed = run_task_definition(require, {register: result})
                    output = completed.stdout + completed.stderr
                    self.assertEqual(completed.returncode == 0, passes, output)
                    if not passes:
                        self.assertIn("ExecStartPre", output)
                        self.assertIn("stays down", output)

    def test_crowdsec_enforcement_is_restored_whatever_removed_it(self) -> None:
        docker = load_nested_tasks(f"{HOST_BASELINE_ROLE}/tasks/crowdsec-docker.yml")
        restores = {
            "host_security": (
                self.security,
                "host_security_crowdsec_chain_ipv4",
                "host_security_crowdsec_chain_ipv6",
            ),
            "host_baseline": (
                docker,
                "host_baseline_crowdsec_ipv4",
                "host_baseline_crowdsec_ipv6",
            ),
        }
        rulesets = {
            "both enforcing": (ENFORCING_RULESET, ENFORCING_RULESET, False),
            "IPv4 torn down": (TORN_DOWN_RULESET, ENFORCING_RULESET, True),
            "IPv6 torn down": (ENFORCING_RULESET, TORN_DOWN_RULESET, True),
            "IPv4 chain defined, INPUT hook missing": (
                UNHOOKED_RULESET,
                ENFORCING_RULESET,
                True,
            ),
            "IPv6 chain defined, INPUT hook missing": (
                ENFORCING_RULESET,
                UNHOOKED_RULESET,
                True,
            ),
            "IPv4 hook is another rule": (
                COMMENTED_HOOK_RULESET,
                ENFORCING_RULESET,
                True,
            ),
        }
        for role, (tasks, ipv4, ipv6) in restores.items():
            restore = tasks[self.RESTORE]
            self.assertEqual(
                restore["ansible.builtin.systemd_service"],
                {"name": "crowdsec-firewall-bouncer", "state": "restarted"},
            )
            # The safety net depends only on the live rules, never on
            # whether a bouncer test ran in this apply.
            rendered = yaml.safe_dump(restore["when"])
            for source in ("host_security_package_install", "DOCKER-USER", "_test"):
                self.assertNotIn(source, rendered)
            decisions = self.conditions_hold(
                restore["when"],
                {
                    case: {ipv4: command_result(v4), ipv6: command_result(v6)}
                    for case, (v4, v6, _) in rulesets.items()
                },
            )
            with self.subTest(role=role):
                self.assertEqual(
                    decisions,
                    {case: expected for case, (_, _, expected) in rulesets.items()},
                )
            # The waits that follow demand the same exact hook in each family.
            waits = [
                task
                for task in tasks.values()
                if task.get("retries") == 12
                and self.INPUT_HOOK in str(task.get("until", ""))
            ]
            self.assertEqual(len(waits), 2, role)
            names = list(tasks)
            for wait in waits:
                self.assertLess(names.index(self.RESTORE), names.index(wait["name"]))
                results = {
                    case: {wait["register"]: command_result(ruleset)}
                    for case, ruleset in {
                        "enforcing": ENFORCING_RULESET,
                        "torn down": TORN_DOWN_RULESET,
                        "unhooked": UNHOOKED_RULESET,
                    }.items()
                }
                with self.subTest(role=role, wait=wait["name"]):
                    self.assertEqual(
                        self.conditions_hold(wait["until"], results),
                        {"enforcing": True, "torn down": False, "unhooked": False},
                    )
        verify = self.security["Verify CrowdSec is enforcing on both address families"]
        for case, (v4, v6, missing) in rulesets.items():
            with self.subTest(verify=case):
                completed = run_task_definition(
                    verify,
                    {
                        "host_security_crowdsec_chain_ipv4_final": command_result(v4),
                        "host_security_crowdsec_chain_ipv6_final": command_result(v6),
                    },
                )
                output = completed.stdout + completed.stderr
                self.assertEqual(completed.returncode != 0, missing, output)

    def test_input_hook_is_proven_after_the_ordering_handlers(self) -> None:
        """The handlers may restart the bouncer after the block's restore.

        The DOCKER-USER order check alone would not notice a missing INPUT
        hook: the ordering helper re-inserts the DOCKER-USER jump whenever
        CROWDSEC_CHAIN exists, and the bouncer only logs a failed INPUT jump
        (setupChain in pkg/iptables/iptables_context.go).
        """
        docker = load_tasks(self.BASELINE_BOUNCER_TASKS)
        names = list(docker)
        reads = {
            "Read the live IPv4 ruleset after the ordering handlers": (
                "/usr/sbin/iptables",
                "host_baseline_crowdsec_ipv4_ordered",
            ),
            "Read the live IPv6 ruleset after the ordering handlers": (
                "/usr/sbin/ip6tables",
                "host_baseline_crowdsec_ipv6_ordered",
            ),
        }
        verify = "Verify CrowdSec still filters INPUT after the ordering handlers"
        flush = "Apply CrowdSec and Docker ordering before verification"
        self.assertEqual(docker[flush]["ansible.builtin.meta"], "flush_handlers")
        for name, (binary, register) in reads.items():
            with self.subTest(read=name):
                task = docker[name]
                self.assertLess(names.index(flush), names.index(name))
                self.assertLess(names.index(name), names.index(verify))
                self.assertEqual(
                    task["ansible.builtin.command"]["argv"], [binary, "-w", "-S"]
                )
                self.assertEqual(task["register"], register)
                self.assertIs(task["changed_when"], False)
                self.assertIs(task["check_mode"], False)
        self.assertEqual(docker[verify]["when"], "not ansible_check_mode")
        rulesets = {
            "both enforcing": (ENFORCING_RULESET, ENFORCING_RULESET, True),
            "IPv4 torn down": (TORN_DOWN_RULESET, ENFORCING_RULESET, False),
            "IPv6 torn down": (ENFORCING_RULESET, TORN_DOWN_RULESET, False),
            "IPv4 chain defined, INPUT hook missing": (
                UNHOOKED_RULESET,
                ENFORCING_RULESET,
                False,
            ),
            "IPv6 chain defined, INPUT hook missing": (
                ENFORCING_RULESET,
                UNHOOKED_RULESET,
                False,
            ),
            "IPv4 hook is another rule": (
                COMMENTED_HOOK_RULESET,
                ENFORCING_RULESET,
                False,
            ),
        }
        decisions = self.conditions_hold(
            docker[verify]["ansible.builtin.assert"]["that"],
            {
                case: {
                    "host_baseline_crowdsec_ipv4_ordered": command_result(v4),
                    "host_baseline_crowdsec_ipv6_ordered": command_result(v6),
                }
                for case, (v4, v6, _) in rulesets.items()
            },
        )
        self.assertEqual(
            decisions,
            {case: expected for case, (_, _, expected) in rulesets.items()},
        )


if __name__ == "__main__":
    unittest.main()
