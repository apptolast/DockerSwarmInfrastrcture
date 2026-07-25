#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

(( EUID == 0 )) || fail "run this script as root"

for command_name in docker ip6tables ip6tables-save iptables iptables-save ss ufw; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done

ufw_status="$(ufw status verbose)"
grep -Fx 'Status: active' <<<"${ufw_status}" >/dev/null ||
  fail "UFW is not active"
grep -F 'Default: deny (incoming)' <<<"${ufw_status}" >/dev/null ||
  fail "UFW does not deny incoming traffic by default"

[[ "$(iptables -S INPUT | head -n 1)" == "-P INPUT DROP" ]] ||
  fail "IPv4 INPUT policy is not DROP"
[[ "$(ip6tables -S INPUT | head -n 1)" == "-P INPUT DROP" ]] ||
  fail "IPv6 INPUT policy is not DROP"

swarm_port_rules="$(
  {
    iptables-save
    ip6tables-save
  } |
    grep -E -- '--dport (2377|7946|4789)([[:space:]]|$)' ||
    true
)"
[[ -z "${swarm_port_rules}" ]] || {
  printf '%s\n' "${swarm_port_rules}" >&2
  fail "firewall rules explicitly reference internal Swarm ports"
}

remote_api_listeners="$(
  ss -H -lnt |
    grep -E ':(2375|2376)([[:space:]]|$)' ||
    true
)"
[[ -z "${remote_api_listeners}" ]] || {
  printf '%s\n' "${remote_api_listeners}" >&2
  fail "Docker remote API is listening on TCP"
}

published_ports="$(
  docker service ls --format '{{.Name}}|{{.Ports}}' |
    awk -F '|' 'length($2) > 0'
)"
[[ -z "${published_ports}" ]] || {
  printf '%s\n' "${published_ports}" >&2
  fail "a Swarm service publishes ports"
}

printf 'Firewall validation passed: internal Swarm ports are not authorized.\n'
