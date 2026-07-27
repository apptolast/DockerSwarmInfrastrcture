"""Negative and positive tests for the exact UFW user-chain contract."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = REPOSITORY_ROOT / "scripts/validate-ufw-contract.py"


def load_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_ufw_contract",
        VALIDATOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot import UFW contract validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = load_validator()


class UfwContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ssh_port = 22
        self.interface = "eth0"
        self.address = "192.0.2.10"
        self.public_ports = {80, 443}
        self.egress = {
            ("tcp", 53),
            ("udp", 53),
            ("tcp", 443),
            ("tcp", 4460),
        }
        self.ipv4_input = validator.ssh_rules(
            "ufw-user-input",
            self.ssh_port,
            False,
        )
        self.ipv6_input = validator.ssh_rules(
            "ufw6-user-input",
            self.ssh_port,
            True,
        )
        for port in sorted(self.public_ports):
            self.ipv4_input.append(
                [
                    "-A",
                    "ufw-user-input",
                    "-i",
                    self.interface,
                    "-d",
                    f"{self.address}/32",
                    "-p",
                    "tcp",
                    "-m",
                    "tcp",
                    "--dport",
                    str(port),
                    "-j",
                    "ACCEPT",
                ]
            )
        self.ipv4_output = validator.output_rules(
            "ufw-user-output",
            self.egress,
        )
        self.ipv6_output = validator.output_rules(
            "ufw6-user-output",
            self.egress,
        )

    def validate(self, *, required_public: bool = True) -> None:
        validator.validate(
            ipv4_input=self.ipv4_input,
            ipv6_input=self.ipv6_input,
            ipv4_output=self.ipv4_output,
            ipv6_output=self.ipv6_output,
            ssh_port=self.ssh_port,
            public_interface=self.interface,
            public_ipv4=self.address,
            public_ports=self.public_ports,
            required_public=required_public,
            egress=self.egress,
        )

    def test_exact_policy_is_accepted(self) -> None:
        self.validate()

    def test_public_rules_can_be_absent_only_before_platform(self) -> None:
        self.ipv4_input = self.ipv4_input[:3]
        self.validate(required_public=False)
        with self.assertRaises(validator.UfwContractError):
            self.validate(required_public=True)

    def test_unreviewed_ingress_rule_is_rejected(self) -> None:
        self.ipv4_input.append(
            [
                "-A",
                "ufw-user-input",
                "-p",
                "tcp",
                "-m",
                "tcp",
                "--dport",
                "8443",
                "-j",
                "ACCEPT",
            ]
        )
        with self.assertRaises(validator.UfwContractError):
            self.validate()

    def test_source_scoped_denials_are_tolerated_but_permits_are_not(
        self,
    ) -> None:
        for verdict in (
            ["-j", "REJECT", "--reject-with", "icmp-port-unreachable"],
            ["-j", "DROP"],
        ):
            with self.subTest(verdict=verdict):
                self.ipv4_input.append(
                    ["-A", "ufw-user-input", "-s", "198.51.100.7/32"] + verdict
                )
                self.ipv6_input.append(
                    ["-A", "ufw6-user-input", "-s", "2001:db8::7/128"] + verdict
                )
                self.validate()
                self.ipv4_input.pop()
                self.ipv6_input.pop()

        self.ipv4_input.append(
            ["-A", "ufw-user-input", "-s", "198.51.100.7/32", "-j", "ACCEPT"]
        )
        with self.assertRaises(validator.UfwContractError):
            self.validate()
        self.ipv4_input.pop()

        self.ipv4_input.append(
            ["-A", "ufw-user-input", "-j", "REJECT", "--reject-with", "icmp-port-unreachable"]
        )
        with self.assertRaises(validator.UfwContractError):
            self.validate()

    def test_duplicate_or_ipv6_application_rule_is_rejected(self) -> None:
        self.ipv4_input.append(list(self.ipv4_input[-1]))
        with self.assertRaises(validator.UfwContractError):
            self.validate()
        self.ipv4_input.pop()
        self.ipv6_input.append(list(self.ipv4_input[-1]))
        with self.assertRaises(validator.UfwContractError):
            self.validate()

    def test_extra_or_missing_egress_is_rejected(self) -> None:
        self.ipv4_output.append(
            [
                "-A",
                "ufw-user-output",
                "-p",
                "tcp",
                "-m",
                "tcp",
                "--dport",
                "22",
                "-j",
                "ACCEPT",
            ]
        )
        with self.assertRaises(validator.UfwContractError):
            self.validate()
        self.ipv4_output.pop()
        self.ipv6_output.pop()
        with self.assertRaises(validator.UfwContractError):
            self.validate()

    def test_egress_parser_is_exact(self) -> None:
        self.assertEqual(
            validator.parse_egress(["tcp:443", "udp:53"]),
            {("tcp", 443), ("udp", 53)},
        )
        for invalid in (["sctp:443"], ["tcp:0"], ["tcp:443", "tcp:443"]):
            with self.subTest(invalid=invalid):
                with self.assertRaises(validator.UfwContractError):
                    validator.parse_egress(invalid)


if __name__ == "__main__":
    unittest.main()
