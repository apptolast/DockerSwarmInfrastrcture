"""Contract of the kernel command line that host_baseline pins for GRUB.

On 2026-10-05 a kernel install regenerated /boot/grub/grub.cfg without
/etc/default/grub, which does not exist on the host: the public interface
booted as ens3 instead of eth0, and kdump-tools added crashkernel=. These
tests cover the validator rule, the role's gates (run through the reviewed-task
harness against synthetic reads, never against the host), the rendered drop-in
and the runbook.
"""

from __future__ import annotations

import base64
import copy
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

import yaml
from jinja2 import Environment, StrictUndefined

from ansible_task_harness import AnsibleTaskAssertions, run_task_definitions
from test_host_bootstrap_contract import (
    PROJECT_ROOT,
    host_security_validator,
    load_nested_tasks,
    load_tasks,
)

ROLE = "ansible/roles/host_baseline"
BOOT = f"{ROLE}/tasks/boot.yml"
TEMPLATE = PROJECT_ROOT / ROLE / "templates/grub-boot-cmdline.cfg.j2"
CONTRACT = PROJECT_ROOT / "config/host-security.yml"
DROP_IN = "/etc/default/grub.d/zz-dockerswarm-boot-cmdline.cfg"
TOPIC = "Host baseline regenerate GRUB configuration"
LINUX = ["net.ifnames=0", "console=tty0", "video=1024x768"]
DEFAULT = ["autoinstall", "ds=nocloud-net"]
# /etc/default/grub.d/kdump-tools.cfg as the kdump-tools package installs it
# on the host: it appends to the value earlier files left behind.
KDUMP_TOOLS_CFG = (
    'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT '
    'crashkernel=2G-4G:320M,4G-32G:512M,32G-64G:1024M,64G-128G:2048M,128G-:4096M"\n'
)
# The /boot/grub/grub.cfg structure grub-mkconfig wrote on the host
# (00_header and 10_linux), with a placeholder root UUID.
GRUB_HEADER = """\
if [ "${next_entry}" ] ; then
   set default="${next_entry}"
   set next_entry=
   save_env next_entry
   set boot_once=true
else
   set default="0"
fi
"""
GRUB_ENTRIES = """\
menuentry 'GNU/Linux' --class gnu-linux --class gnu --class os {
\trecordfail
\tload_video
\tinsmod gzio
\tlinux\t/vmlinuz-7.0.0-34-generic ROOT SIMPLE
\tinitrd\t/initrd.img-7.0.0-34-generic
}
submenu 'Advanced options for GNU/Linux' {
\tmenuentry 'GNU/Linux, with Linux 7.0.0-34-generic (recovery mode)' {
\t\trecordfail
\t\tlinux\t/vmlinuz-7.0.0-34-generic ROOT single nomodeset RECOVERY
\t\tinitrd\t/initrd.img-7.0.0-34-generic
\t}
}
"""
ROOT = "root=UUID=00000000-0000-4000-8000-000000000000 ro"
RUNNING = f"BOOT_IMAGE=/vmlinuz-7.0.0-34-generic {ROOT} {' '.join(LINUX + DEFAULT)}\n"


def grub_menu(simple: str, header: str = GRUB_HEADER) -> str:
    """A generated menu whose first (default) linux line boots `simple`."""
    return header + (
        GRUB_ENTRIES.replace("ROOT", ROOT)
        .replace("SIMPLE", simple)
        .replace("RECOVERY", " ".join(LINUX))
    )


def encoded(contents: str) -> dict[str, str]:
    """A registered slurp result."""
    return {"content": base64.b64encode(contents.encode("utf-8")).decode("ascii")}


def render(linux: list[str], default: list[str]) -> str:
    return (
        Environment(
            autoescape=False,
            keep_trailing_newline=True,
            undefined=StrictUndefined,
        )
        .from_string(TEMPLATE.read_text(encoding="utf-8"))
        .render(
            host_baseline_boot_cmdline_linux=linux,
            host_baseline_boot_cmdline_linux_default=default,
        )
    )


def source_grub_defaults(files: dict[str, str]) -> dict[str, str]:
    """Source the drop-ins exactly as grub-mkconfig does, under /bin/sh."""
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        for name, contents in files.items():
            (directory / name).write_text(contents, encoding="utf-8")
        completed = subprocess.run(
            [
                "/bin/sh",
                "-c",
                'for x in "$1"/*.cfg; do if [ -e "$x" ]; then . "$x"; fi; done; '
                'printf "%s\\n" "$GRUB_CMDLINE_LINUX" "$GRUB_CMDLINE_LINUX_DEFAULT"',
                "sh",
                str(directory),
            ],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        )
    linux, default = completed.stdout.split("\n")[:2]
    return {"linux": linux, "default": default}


class BootCmdlineValidatorTests(unittest.TestCase):
    """scripts/validate-host-security.py rejects every unsafe command line."""

    NOW = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
        self.contract["host_security_ubuntu_snapshot"] = "20260819T000000Z"

    def assert_rejected(self, change: dict[str, Any], message: str) -> None:
        contract = copy.deepcopy(self.contract)
        for key, value in change.items():
            if value is None:
                contract.pop(key)
            else:
                contract[key] = value
        with self.assertRaisesRegex(
            host_security_validator.HostSecurityContractError, message
        ):
            host_security_validator.validate(contract, self.NOW)

    def test_the_reviewed_command_line_is_the_live_one(self) -> None:
        self.assertEqual(self.contract["host_security_boot_cmdline_linux"], LINUX)
        self.assertEqual(
            self.contract["host_security_boot_cmdline_linux_default"], DEFAULT
        )
        host_security_validator.validate(self.contract, self.NOW)

    def test_both_lists_are_required(self) -> None:
        for key in (
            "host_security_boot_cmdline_linux",
            "host_security_boot_cmdline_linux_default",
        ):
            for label, value in (
                ("missing", None),
                ("string", "net.ifnames=0 console=tty0"),
                ("mapping", {"net.ifnames": "0"}),
            ):
                with self.subTest(key=key, value=label):
                    self.assert_rejected({key: value}, f"{key} must be a list")
        self.assert_rejected(
            {"host_security_boot_cmdline_linux": []}, "must not be empty"
        )
        # Only the common list must hold something: net.ifnames=0 lives there.
        contract = copy.deepcopy(self.contract)
        contract["host_security_boot_cmdline_linux_default"] = []
        host_security_validator.validate(contract, self.NOW)

    def test_every_parameter_is_one_shell_safe_word(self) -> None:
        unsafe = [
            'a"b',
            "$(id)",
            "`id`",
            "a;b",
            "a b",
            "",
            "-x",
            "a\\b",
            42,
            # GRUB script and shell metacharacters: 10_linux writes the
            # parameters unquoted into the `linux` line of grub.cfg.
            "a'b",
            "a|b",
            "a&b",
            "a{b",
            "a<b",
            "a#b",
            "a/b",
            "a\nb",
            "a\tb",
            "\u00e9",
        ]
        for key, base in (
            ("host_security_boot_cmdline_linux", LINUX),
            ("host_security_boot_cmdline_linux_default", DEFAULT),
        ):
            for token in unsafe:
                with self.subTest(key=key, token=token):
                    self.assert_rejected(
                        {key: [*base, token]}, "unsafe in a shell-sourced"
                    )

    def test_no_parameter_is_declared_twice(self) -> None:
        cases = {
            "within the common list": {
                "host_security_boot_cmdline_linux": [*LINUX, "console=tty0"]
            },
            "across both lists": {
                "host_security_boot_cmdline_linux_default": [*DEFAULT, "console=tty0"]
            },
            "net.ifnames=0 in both lists": {
                "host_security_boot_cmdline_linux_default": [*DEFAULT, "net.ifnames=0"]
            },
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.assert_rejected(change, "declared more than once")

    def test_every_entry_keeps_eth0(self) -> None:
        without = [token for token in LINUX if token != "net.ifnames=0"]
        cases = {
            "removed": {"host_security_boot_cmdline_linux": without},
            # The recovery entries only boot GRUB_CMDLINE_LINUX.
            "moved to the default-only list": {
                "host_security_boot_cmdline_linux": without,
                "host_security_boot_cmdline_linux_default": [
                    *DEFAULT,
                    "net.ifnames=0",
                ],
            },
            "replaced by predictable names": {
                "host_security_boot_cmdline_linux": [*without, "net.ifnames=1"]
            },
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.assert_rejected(change, "must keep net.ifnames=0")
        for token in ("net.ifnames=1", "net.ifnames"):
            for key, base in (
                ("host_security_boot_cmdline_linux", LINUX),
                ("host_security_boot_cmdline_linux_default", DEFAULT),
            ):
                with self.subTest(key=key, token=token):
                    self.assert_rejected(
                        {key: [*base, token]},
                        "may only be declared as net.ifnames=0",
                    )

    def test_no_crash_kernel_memory_is_reserved(self) -> None:
        for token in ("crashkernel=512M", "crashkernel"):
            for key, base in (
                ("host_security_boot_cmdline_linux", LINUX),
                ("host_security_boot_cmdline_linux_default", DEFAULT),
            ):
                with self.subTest(key=key, token=token):
                    self.assert_rejected(
                        {key: [*base, token]}, "below the capacity floor"
                    )


class BootCmdlineTemplateTests(unittest.TestCase):
    """The rendered drop-in replaces kdump-tools' append when GRUB loads it."""

    def test_the_drop_in_is_two_plain_assignments(self) -> None:
        rendered = render(LINUX, DEFAULT)
        lines = rendered.splitlines()
        self.assertTrue(lines[0].startswith("# Managed by Ansible."))
        self.assertEqual(
            [line for line in lines if not line.startswith("#")],
            [
                'GRUB_CMDLINE_LINUX="net.ifnames=0 console=tty0 video=1024x768"',
                'GRUB_CMDLINE_LINUX_DEFAULT="autoinstall ds=nocloud-net"',
            ],
        )
        self.assertNotIn("$", rendered)
        self.assertTrue(rendered.endswith("\n"))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "candidate.cfg"
            path.write_text(rendered, encoding="utf-8")
            subprocess.run(["/bin/sh", "-n", str(path)], check=True)

    def test_the_drop_in_discards_the_kdump_tools_crash_kernel(self) -> None:
        defaults = yaml.safe_load(
            (PROJECT_ROOT / ROLE / "defaults/main.yml").read_text(encoding="utf-8")
        )
        name = Path(defaults["host_baseline_boot_cmdline_dropin"]).name
        self.assertEqual(name, Path(DROP_IN).name)
        loaded = source_grub_defaults(
            {"kdump-tools.cfg": KDUMP_TOOLS_CFG, name: render(LINUX, DEFAULT)}
        )
        self.assertEqual(
            loaded,
            {
                "linux": "net.ifnames=0 console=tty0 video=1024x768",
                "default": "autoinstall ds=nocloud-net",
            },
        )

    def test_an_appending_drop_in_would_keep_the_crash_kernel(self) -> None:
        # The fixture is real: written like kdump-tools.cfg, the drop-in
        # would leave crashkernel= in every normal entry, as on 2026-10-05.
        appending = render(LINUX, DEFAULT).replace(
            'GRUB_CMDLINE_LINUX_DEFAULT="',
            'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT ',
        )
        loaded = source_grub_defaults(
            {"kdump-tools.cfg": KDUMP_TOOLS_CFG, Path(DROP_IN).name: appending}
        )
        self.assertIn("crashkernel=", loaded["default"])
        # Without the drop-in, the 2026-10-05 state: no net.ifnames=0.
        loaded = source_grub_defaults({"kdump-tools.cfg": KDUMP_TOOLS_CFG})
        self.assertEqual(loaded["linux"], "")
        self.assertIn("crashkernel=", loaded["default"])


class BootCmdlineGateTests(AnsibleTaskAssertions, unittest.TestCase):
    """host_baseline installs the drop-in and fails closed on a bad menu."""

    CANDIDATE = "Render the GRUB command-line candidate in memory"
    VERIFY_CANDIDATE = "Verify the GRUB command-line candidate"
    PARSE_CANDIDATE = "Parse the GRUB command-line candidate as POSIX shell"
    INSTALL = "Install the reviewed kernel command line for GRUB"
    FLUSH = "Regenerate the GRUB menu before verification"
    READ_MENU = "Read the generated GRUB menu"
    PARSE_MENU = "Parse the generated GRUB menu"
    VERIFY_MENU = "Verify the default GRUB entry boots the reviewed command line"
    READ_RUNNING = "Read the running kernel command line"
    VERIFY_RUNNING = "Verify the running kernel keeps eth0 and reserves no crash memory"
    boot: ClassVar[dict[str, dict[str, Any]]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.boot = load_tasks(BOOT)

    def lists(
        self, linux: list[str] | None = None, default: list[str] | None = None
    ) -> dict[str, Any]:
        return {
            "host_baseline_boot_cmdline_linux": LINUX if linux is None else linux,
            "host_baseline_boot_cmdline_linux_default": (
                DEFAULT if default is None else default
            ),
        }

    # -- structure ----------------------------------------------------------

    def test_the_drop_in_is_installed_validated_and_never_backed_up(self) -> None:
        task = self.boot[self.INSTALL]
        self.assertEqual(
            task["ansible.builtin.template"],
            {
                "src": "grub-boot-cmdline.cfg.j2",
                "dest": "{{ host_baseline_boot_cmdline_dropin }}",
                "owner": "root",
                "group": "root",
                "mode": "0644",
                "validate": "/bin/sh -n %s",
            },
        )
        self.assertEqual(task["notify"], TOPIC)
        # `--check --diff` shows the change; a backup would sit in grub.d.
        self.assertNotIn("when", task)
        self.assertNotIn("backup", task["ansible.builtin.template"])
        directory = self.boot["Create the GRUB defaults drop-in directory"]
        self.assertEqual(
            directory["ansible.builtin.file"],
            {
                "path": str(Path(DROP_IN).parent),
                "state": "directory",
                "owner": "root",
                "group": "root",
                "mode": "0755",
            },
        )
        names = list(self.boot)
        self.assertLess(names.index(directory["name"]), names.index(self.INSTALL))
        parse = self.boot[self.PARSE_CANDIDATE]
        self.assertEqual(
            parse["ansible.builtin.command"]["argv"], ["/bin/sh", "-n", "/dev/stdin"]
        )
        self.assertEqual(
            parse["ansible.builtin.command"]["stdin"],
            "{{ host_baseline_boot_cmdline_candidate }}",
        )
        self.assertIs(parse["changed_when"], False)
        self.assertIs(parse["check_mode"], False)
        self.assertEqual(
            self.boot[self.FLUSH]["ansible.builtin.meta"], "flush_handlers"
        )

    def test_the_contract_reaches_the_role(self) -> None:
        defaults = yaml.safe_load(
            (PROJECT_ROOT / ROLE / "defaults/main.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            defaults["host_baseline_boot_cmdline_linux"],
            "{{ host_security_boot_cmdline_linux }}",
        )
        self.assertEqual(
            defaults["host_baseline_boot_cmdline_linux_default"],
            "{{ host_security_boot_cmdline_linux_default }}",
        )
        self.assertEqual(defaults["host_baseline_boot_cmdline_dropin"], DROP_IN)
        # zz- sorts after kdump-tools.cfg, so its assignment wins.
        self.assertGreater(Path(DROP_IN).name, "kdump-tools.cfg")

    def test_only_the_handler_regenerates_grub(self) -> None:
        handlers = yaml.safe_load(
            (PROJECT_ROOT / ROLE / "handlers/main.yml").read_text(encoding="utf-8")
        )
        handler = next(
            item
            for item in handlers
            if item["name"] == "Regenerate the GRUB configuration"
        )
        self.assertEqual(
            handler["ansible.builtin.command"], {"argv": ["/usr/sbin/update-grub"]}
        )
        self.assertIs(handler["changed_when"], True)
        self.assertEqual(handler["listen"], TOPIC)
        # Without check_mode: false the command module skips it under --check.
        self.assertNotIn("check_mode", handler)
        self.assertEqual(
            [item["name"] for item in handlers if item.get("listen") == TOPIC],
            [handler["name"]],
        )
        # No task runs it, so a converged second apply reports changed=0.
        for path in sorted((PROJECT_ROOT / ROLE / "tasks").glob("*.yml")):
            relative = str(path.relative_to(PROJECT_ROOT))
            for task in load_nested_tasks(relative).values():
                for module in (
                    "ansible.builtin.command",
                    "ansible.builtin.shell",
                    "ansible.builtin.raw",
                    "ansible.builtin.script",
                ):
                    if module in task:
                        text = yaml.safe_dump(task[module])
                        with self.subTest(file=relative, task=task["name"]):
                            self.assertNotIn("update-grub", text)
                            self.assertNotIn("grub-mkconfig", text)

    def test_verification_follows_the_regeneration(self) -> None:
        names = list(self.boot)
        order = [
            self.CANDIDATE,
            self.VERIFY_CANDIDATE,
            self.PARSE_CANDIDATE,
            self.INSTALL,
            self.FLUSH,
            self.READ_MENU,
            self.PARSE_MENU,
            self.VERIFY_MENU,
        ]
        self.assertEqual(
            [names.index(name) for name in order],
            sorted(names.index(name) for name in order),
        )
        self.assertLess(names.index(self.FLUSH), names.index(self.READ_RUNNING))
        self.assertLess(
            names.index(self.READ_RUNNING), names.index(self.VERIFY_RUNNING)
        )
        for name, source, register in (
            (self.READ_MENU, "/boot/grub/grub.cfg", "host_baseline_grub_cfg"),
            (self.READ_RUNNING, "/proc/cmdline", "host_baseline_running_cmdline"),
        ):
            with self.subTest(name):
                self.assertEqual(
                    self.boot[name]["ansible.builtin.slurp"], {"src": source}
                )
                self.assertEqual(self.boot[name]["register"], register)
                self.assertNotIn("when", self.boot[name])
        for name in (self.VERIFY_MENU, self.VERIFY_RUNNING):
            with self.subTest(name):
                self.assertEqual(self.boot[name]["when"], "not ansible_check_mode")
                self.assertNotIn("no_log", self.boot[name])
        # grub-mkconfig writes the menu 0600, readable by root only.
        for name in (self.READ_MENU, self.PARSE_MENU):
            with self.subTest(name):
                self.assertIs(self.boot[name].get("no_log"), True)
        self.assertNotIn("no_log", self.boot[self.READ_RUNNING])

    def test_the_role_pins_the_command_line_after_the_kernel_settings(self) -> None:
        main = yaml.safe_load(
            (PROJECT_ROOT / ROLE / "tasks/main.yml").read_text(encoding="utf-8")
        )
        imports = [task["ansible.builtin.import_tasks"] for task in main]
        self.assertEqual(imports.index("boot.yml"), imports.index("sysctl.yml") + 1)
        self.assertLess(imports.index("boot.yml"), imports.index("security.yml"))
        self.assertIn(
            {
                "name": "Pin the reviewed kernel command line",
                "ansible.builtin.import_tasks": "boot.yml",
            },
            main,
        )

    def test_the_sensitive_path_guard_covers_the_control(self) -> None:
        guard = (
            PROJECT_ROOT / ".github/workflows/guard-sensitive-paths.yml"
        ).read_text(encoding="utf-8")
        lines = guard.split("<<'PATTERNS'\n", 1)[1].splitlines()
        patterns = [
            re.compile(line.strip())
            for line in lines[: [line.strip() for line in lines].index("PATTERNS")]
        ]
        for path in (
            BOOT,
            f"{ROLE}/templates/grub-boot-cmdline.cfg.j2",
            f"{ROLE}/handlers/main.yml",
            f"{ROLE}/defaults/main.yml",
            "scripts/validate-host-security.py",
            "config/host-security.yml",
        ):
            with self.subTest(path):
                self.assertTrue(any(pattern.search(path) for pattern in patterns))

    # -- in-memory candidate ------------------------------------------------

    def test_the_candidate_gate_accepts_only_the_reviewed_drop_in(self) -> None:
        reviewed = render(LINUX, DEFAULT)
        self.assert_task_accepts(
            BOOT,
            self.VERIFY_CANDIDATE,
            {**self.lists(), "host_baseline_boot_cmdline_candidate": reviewed},
        )
        without = [token for token in LINUX if token != "net.ifnames=0"]
        unsafe = [*DEFAULT, 'x";/bin/true;"']
        crash = [*DEFAULT, "crashkernel=512M"]
        rejected = {
            "appends to the earlier value": (
                self.lists(),
                reviewed.replace(
                    'GRUB_CMDLINE_LINUX_DEFAULT="',
                    'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT ',
                ),
            ),
            "misses an assignment": (
                self.lists(),
                "".join(
                    line
                    for line in reviewed.splitlines(keepends=True)
                    if not line.startswith("GRUB_CMDLINE_LINUX_DEFAULT=")
                ),
            ),
            "adds an assignment": (self.lists(), reviewed + "GRUB_DEFAULT=saved\n"),
            "expands in a comment": (self.lists(), "# `id`\n" + reviewed),
            "expands a dollar in a comment": (self.lists(), "# $(id)\n" + reviewed),
            "lacks net.ifnames=0": (
                self.lists(linux=without),
                render(without, DEFAULT),
            ),
            "carries a shell-unsafe parameter": (
                self.lists(default=unsafe),
                render(LINUX, unsafe),
            ),
            "reserves crash-kernel memory": (
                self.lists(default=crash),
                render(LINUX, crash),
            ),
        }
        # The validator's other rules, which deploy-ansible.sh never runs.
        for label, default in {
            "declares a parameter twice": [*DEFAULT, "console=tty0"],
            "declares net.ifnames=0 twice": [*DEFAULT, "net.ifnames=0"],
            "declares net.ifnames=1": [*DEFAULT, "net.ifnames=1"],
            "declares a bare net.ifnames": [*DEFAULT, "net.ifnames"],
        }.items():
            rejected[label] = (self.lists(default=default), render(LINUX, default))
        # One list item must be one word: an item with a space would hide a
        # second parameter from the crashkernel check, which reads items.
        for token in (
            "console=tty1 crashkernel=512M",
            "a;b",
            "a b",
            "a\\b",
            "-x",
            "",
        ):
            default = [*DEFAULT, token]
            rejected[f"carries {token!r}"] = (
                self.lists(default=default),
                render(LINUX, default),
            )
        for label, (variables, candidate) in rejected.items():
            with self.subTest(label):
                self.assert_task_rejects(
                    BOOT,
                    self.VERIFY_CANDIDATE,
                    {**variables, "host_baseline_boot_cmdline_candidate": candidate},
                    "rendered GRUB command-line candidate",
                )

    # -- generated menu -----------------------------------------------------

    def run_menu_gate(
        self, contents: str, check_mode: bool = False
    ) -> subprocess.CompletedProcess[str]:
        return run_task_definitions(
            [self.boot[self.PARSE_MENU], self.boot[self.VERIFY_MENU]],
            {**self.lists(), "host_baseline_grub_cfg": encoded(contents)},
            check_mode=check_mode,
        )

    def test_the_menu_gate_accepts_the_reviewed_default_entry(self) -> None:
        reviewed = " ".join(LINUX + DEFAULT)
        accepted = {
            "as generated on the host": grub_menu(reviewed),
            "without the grub-reboot branch": grub_menu(
                reviewed, header='set default="0"\n'
            ),
            "with an extra parameter": grub_menu(reviewed + " quiet"),
            # Harmless for eth0: only another value is ambiguous.
            "with net.ifnames=0 repeated": grub_menu(reviewed + " net.ifnames=0"),
        }
        for label, contents in accepted.items():
            with self.subTest(label):
                completed = self.run_menu_gate(contents)
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )

    def test_the_menu_gate_rejects_a_default_entry_that_would_lose_eth0(
        self,
    ) -> None:
        reviewed = " ".join(LINUX + DEFAULT)
        crash = "crashkernel=2G-4G:320M,4G-32G:512M"
        rejected = {
            # What the kernel install generated on 2026-10-05.
            "2026-10-05": grub_menu(f" {crash}"),
            "without net.ifnames=0": grub_menu(reviewed.replace("net.ifnames=0 ", "")),
            "without an installer parameter": grub_menu(
                reviewed.replace(" ds=nocloud-net", "")
            ),
            "with crashkernel=": grub_menu(f"{reviewed} {crash}"),
            "with another default entry": grub_menu(
                reviewed, header=GRUB_HEADER.replace('"0"', '"1"')
            ),
            "with GRUB_DEFAULT=saved": grub_menu(
                reviewed,
                header=GRUB_HEADER.replace('"0"', '"${saved_entry}"'),
            ),
            "with a later saved default": grub_menu(
                reviewed, header=GRUB_HEADER + 'set default="${saved_entry}"\n'
            ),
            "with a bare default assignment": grub_menu(
                reviewed, header=GRUB_HEADER + 'default="1"\n'
            ),
            "with a tab-separated default": grub_menu(
                reviewed, header=GRUB_HEADER + 'set\tdefault="1"\n'
            ),
            "without the 00_header default": grub_menu(reviewed, header=""),
            "with only the grub-reboot branch": grub_menu(
                reviewed, header='set default="${next_entry}"\n'
            ),
            # A later drop-in that appends to GRUB_CMDLINE_LINUX, as
            # kdump-tools.cfg does with crashkernel=.
            "with a later net.ifnames=1": grub_menu(reviewed + " net.ifnames=1"),
            "with a bare net.ifnames": grub_menu(reviewed + " net.ifnames"),
            "without a linux line": GRUB_HEADER,
            "empty": "",
        }
        for label, contents in rejected.items():
            with self.subTest(label):
                completed = self.run_menu_gate(contents)
                output = completed.stdout + completed.stderr
                self.assertNotEqual(completed.returncode, 0, output)
                self.assertIn("Do not reboot until this check passes", output)
                self.assertIn("run sudo -- update-grub and apply again", output)

    # -- running kernel -----------------------------------------------------

    def test_the_running_kernel_gate_checks_only_its_invariants(self) -> None:
        def running(cmdline: str) -> dict[str, Any]:
            return {"host_baseline_running_cmdline": encoded(cmdline)}

        self.assert_task_accepts(BOOT, self.VERIFY_RUNNING, running(RUNNING))
        # A parameter change waits for the next manual reboot without failing
        # every apply until then.
        self.assert_task_accepts(
            BOOT, self.VERIFY_RUNNING, running(RUNNING.replace(" autoinstall", ""))
        )
        self.assert_task_accepts(
            BOOT,
            self.VERIFY_RUNNING,
            running(RUNNING.rstrip("\n") + " net.ifnames=0\n"),
        )
        for label, cmdline in {
            "booted as ens3": RUNNING.replace("net.ifnames=0 ", ""),
            "predictable names": RUNNING.replace("net.ifnames=0", "net.ifnames=1"),
            "both values": RUNNING.rstrip("\n") + " net.ifnames=1\n",
            "a bare net.ifnames": RUNNING.rstrip("\n") + " net.ifnames\n",
            "non-boolean value": RUNNING.replace("net.ifnames=0", "net.ifnames=01"),
            "crash-kernel memory": RUNNING.rstrip("\n") + " crashkernel=512M\n",
        }.items():
            with self.subTest(label):
                self.assert_task_rejects(
                    BOOT,
                    self.VERIFY_RUNNING,
                    running(cmdline),
                    "a reboot in the manual window",
                )

    def test_check_mode_skips_both_verifications(self) -> None:
        tasks = [
            self.boot[self.PARSE_MENU],
            self.boot[self.VERIFY_MENU],
            self.boot[self.VERIFY_RUNNING],
        ]
        variables = {
            **self.lists(),
            "host_baseline_grub_cfg": encoded(grub_menu("crashkernel=512M")),
            "host_baseline_running_cmdline": encoded("ro crashkernel=512M\n"),
        }
        completed = run_task_definitions(tasks, variables)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        completed = run_task_definitions(tasks, variables, check_mode=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.count("skipping: [localhost]"), 2)


class BootCmdlineDocsTests(unittest.TestCase):
    """The patch window compares GRUB with the running kernel before rebooting."""

    def test_the_patch_window_checks_the_command_line_around_the_reboot(
        self,
    ) -> None:
        text = (PROJECT_ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
        section = text[
            text.index("## Parcheo del sistema operativo") : text.index("## Reinicios")
        ]
        before = section[: section.index("El reinicio sigue «Reinicios»")]
        after = section[section.index("El reinicio sigue «Reinicios»") :]
        for fragment in (
            "/proc/cmdline",
            "/boot/grub/grub.cfg",
            "host-baseline",
            # The reviewed change a reboot activates is not a blocker.
            "host_security_boot_cmdline_linux_default",
        ):
            with self.subTest(before=fragment):
                self.assertIn(fragment, before)
        for fragment in ("ip -brief link", "net.ifnames=0", "crashkernel="):
            with self.subTest(after=fragment):
                self.assertIn(fragment, after)
        readme = (PROJECT_ROOT / ROLE / "README.md").read_text(encoding="utf-8")
        self.assertIn("### Kernel command line", readme)
        self.assertIn(DROP_IN, readme)
        # A rebuilt host may first boot with crashkernel=: the runbook names
        # the task that stops and the reboot that clears it.
        rebuild = " ".join(
            (PROJECT_ROOT / "docs/REBUILD.md").read_text(encoding="utf-8").split()
        )
        self.assertIn(BootCmdlineGateTests.VERIFY_RUNNING, rebuild)


if __name__ == "__main__":
    unittest.main()
