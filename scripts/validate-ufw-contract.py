#!/usr/bin/env python3
"""Fail closed when UFW's user chains contain an unreviewed rule."""

from __future__ import annotations

import argparse
import ipaddress
import shlex
import subprocess
import sys
from collections import Counter


class UfwContractError(RuntimeError):
    """The live UFW user policy differs from its IaC contract."""


def inspect_chain(command: str, chain: str) -> list[list[str]]:
    completed = subprocess.run(
        [command, "-w", "-S", chain],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise UfwContractError(f"cannot inspect firewall chain {chain}")
    rules: list[list[str]] = []
    for line in completed.stdout.splitlines():
        tokens = shlex.split(line)
        if tokens[:2] == ["-N", chain]:
            continue
        if tokens[:2] != ["-A", chain]:
            raise UfwContractError(f"unexpected firewall output for {chain}")
        rules.append(tokens)
    return rules


def ssh_rules(chain: str, port: int, ipv6: bool) -> list[list[str]]:
    mask = "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff" if ipv6 else "255.255.255.255"
    limit_chain = "ufw6-user-limit" if ipv6 else "ufw-user-limit"
    accept_chain = f"{limit_chain}-accept"
    base = [
        "-A",
        chain,
        "-p",
        "tcp",
        "-m",
        "tcp",
        "--dport",
        str(port),
    ]
    recent = [
        "-m",
        "conntrack",
        "--ctstate",
        "NEW",
        "-m",
        "recent",
    ]
    return [
        base
        + recent
        + [
            "--set",
            "--name",
            "DEFAULT",
            "--mask",
            mask,
            "--rsource",
        ],
        base
        + recent
        + [
            "--update",
            "--seconds",
            "30",
            "--hitcount",
            "6",
            "--name",
            "DEFAULT",
            "--mask",
            mask,
            "--rsource",
            "-j",
            limit_chain,
        ],
        base + ["-j", accept_chain],
    ]


def app_rule_matches(
    rule: list[str],
    chain: str,
    interface: str,
    address: str,
    allowed_ports: set[int],
) -> bool:
    if len(rule) != 14:
        return False
    expected_static = Counter(
        [
            "-A",
            chain,
            "-i",
            interface,
            "-d",
            f"{address}/32",
            "-p",
            "tcp",
            "-m",
            "tcp",
            "--dport",
            "-j",
            "ACCEPT",
        ]
    )
    try:
        port_index = rule.index("--dport")
        port = int(rule[port_index + 1])
    except (ValueError, IndexError):
        return False
    without_port = rule[: port_index + 1] + rule[port_index + 2 :]
    return port in allowed_ports and Counter(without_port) == expected_static


def deny_rule_matches(rule: list[str], chain: str) -> bool:
    """Recognize a source-scoped denial, such as a fail2ban ban.

    This repository installs fail2ban with ``banaction = ufw``, so every ban it
    issues is appended to the very user chain this contract validates. Refusing
    them would make each reviewed run depend on whether a ban happened to be
    active, which is not a property the contract is meant to assert.

    A denial restricted to a source address can only narrow ingress, never
    widen it, so tolerating it keeps the contract meaningful while making it
    deterministic. Every ACCEPT rule is still checked by ``app_rule_matches``,
    including one scoped to a source address.
    """
    if len(rule) < 6 or rule[:2] != ["-A", chain]:
        return False
    if rule[2] != "-s" or "/" not in rule[3]:
        return False
    try:
        ipaddress.ip_network(rule[3], strict=False)
    except ValueError:
        return False
    verdict = rule[4:]
    if verdict == ["-j", "DROP"]:
        return True
    return len(verdict) == 4 and verdict[:3] == [
        "-j",
        "REJECT",
        "--reject-with",
    ] and verdict[3] in {"icmp-port-unreachable", "icmp6-port-unreachable"}


def output_rules(chain: str, egress: set[tuple[str, int]]) -> list[list[str]]:
    return [
        [
            "-A",
            chain,
            "-p",
            protocol,
            "-m",
            protocol,
            "--dport",
            str(port),
            "-j",
            "ACCEPT",
        ]
        for protocol, port in sorted(egress)
    ]


def validate(
    *,
    ipv4_input: list[list[str]],
    ipv6_input: list[list[str]],
    ipv4_output: list[list[str]],
    ipv6_output: list[list[str]],
    ssh_port: int,
    public_interface: str,
    public_ipv4: str,
    public_ports: set[int],
    required_public: bool,
    egress: set[tuple[str, int]],
) -> None:
    expected_ipv4_ssh = ssh_rules("ufw-user-input", ssh_port, False)
    if not all(rule in ipv4_input for rule in expected_ipv4_ssh):
        raise UfwContractError("the exact IPv4 SSH limit is absent")
    remaining_ipv4 = [
        rule
        for rule in ipv4_input
        if rule not in expected_ipv4_ssh
        and not deny_rule_matches(rule, "ufw-user-input")
    ]
    app_ports: list[int] = []
    for rule in remaining_ipv4:
        if not app_rule_matches(
            rule,
            "ufw-user-input",
            public_interface,
            public_ipv4,
            public_ports,
        ):
            raise UfwContractError("unreviewed IPv4 ingress rule")
        app_ports.append(int(rule[rule.index("--dport") + 1]))
    if len(app_ports) != len(set(app_ports)):
        raise UfwContractError("duplicate IPv4 application ingress rule")
    if required_public and set(app_ports) != public_ports:
        raise UfwContractError("required IPv4 application ingress is incomplete")

    expected_ipv6 = ssh_rules("ufw6-user-input", ssh_port, True)
    observed_ipv6 = [
        rule
        for rule in ipv6_input
        if not deny_rule_matches(rule, "ufw6-user-input")
    ]
    if observed_ipv6 != expected_ipv6:
        raise UfwContractError("IPv6 ingress is not exactly SSH-only")

    expected_ipv4_output = output_rules("ufw-user-output", egress)
    expected_ipv6_output = output_rules("ufw6-user-output", egress)
    if Counter(map(tuple, ipv4_output)) != Counter(map(tuple, expected_ipv4_output)):
        raise UfwContractError("IPv4 host egress contains drift")
    if Counter(map(tuple, ipv6_output)) != Counter(map(tuple, expected_ipv6_output)):
        raise UfwContractError("IPv6 host egress contains drift")


def parse_egress(values: list[str]) -> set[tuple[str, int]]:
    result: set[tuple[str, int]] = set()
    for value in values:
        try:
            protocol, raw_port = value.split(":", 1)
            port = int(raw_port)
        except (ValueError, TypeError) as exc:
            raise UfwContractError("invalid egress contract") from exc
        if protocol not in {"tcp", "udp"} or not 1 <= port <= 65535:
            raise UfwContractError("invalid egress contract")
        if (protocol, port) in result:
            raise UfwContractError("duplicate egress contract")
        result.add((protocol, port))
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iptables", default="/usr/sbin/iptables")
    parser.add_argument("--ip6tables", default="/usr/sbin/ip6tables")
    parser.add_argument("--ssh-port", type=int, required=True)
    parser.add_argument("--public-interface", required=True)
    parser.add_argument("--public-ipv4", required=True)
    parser.add_argument("--public-port", type=int, action="append", default=[])
    parser.add_argument("--egress", action="append", default=[])
    parser.add_argument("--require-public", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        address = ipaddress.ip_address(args.public_ipv4)
        if address.version != 4 or str(address) != args.public_ipv4:
            raise UfwContractError("public address is not canonical IPv4")
        if not args.public_interface or not 1 <= args.ssh_port <= 65535:
            raise UfwContractError("invalid host firewall identity")
        public_ports = set(args.public_port)
        if len(public_ports) != len(args.public_port) or any(
            not 1 <= port <= 65535 for port in public_ports
        ):
            raise UfwContractError("invalid public-port contract")
        validate(
            ipv4_input=inspect_chain(args.iptables, "ufw-user-input"),
            ipv6_input=inspect_chain(args.ip6tables, "ufw6-user-input"),
            ipv4_output=inspect_chain(args.iptables, "ufw-user-output"),
            ipv6_output=inspect_chain(args.ip6tables, "ufw6-user-output"),
            ssh_port=args.ssh_port,
            public_interface=args.public_interface,
            public_ipv4=args.public_ipv4,
            public_ports=public_ports,
            required_public=args.require_public,
            egress=parse_egress(args.egress),
        )
    except UfwContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("UFW user chains match the exact reviewed host contract.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
