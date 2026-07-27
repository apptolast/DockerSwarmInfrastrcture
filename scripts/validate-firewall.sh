#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

readonly POLICY_CHAIN="DOCKERSWARM-INGRESS"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

(( EUID == 0 )) || fail "run this script as root"

for command_name in \
  docker ip6tables ip6tables-save iptables iptables-save jq ss ufw; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done

contract_python="${PROJECT_DIR}/.venv/bin/python"
[[ -x "${contract_python}" ]] ||
  fail "locked Python environment is missing; run scripts/bootstrap-tooling.sh"
mapfile -t firewall_contract < <(
  "${contract_python}" - "${PROJECT_DIR}/config/platform.yml" <<'PY'
from pathlib import Path
import sys

import yaml

contract = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
ports = contract["platform_public_tcp_ports"]
enabled = contract["platform_minecraft_public_enabled"]
effective = ports if enabled else [port for port in ports if port != 25565]
print(contract["platform_public_interface"])
print(contract["platform_public_ipv4"])
print(" ".join(str(port) for port in ports))
print(" ".join(str(port) for port in effective))
print(" ".join(str(port) for port in contract["platform_public_ipv6_tcp_ports"]))
print("true" if enabled else "false")
PY
)
(( ${#firewall_contract[@]} == 6 )) ||
  fail "cannot derive the firewall contract from config/platform.yml"
readonly PUBLIC_INTERFACE="${firewall_contract[0]}"
readonly PUBLIC_IPV4="${firewall_contract[1]}"
IFS=' ' read -r -a public_tcp_ports <<<"${firewall_contract[2]}"
IFS=' ' read -r -a effective_public_tcp_ports <<<"${firewall_contract[3]}"
IFS=' ' read -r -a public_ipv6_tcp_ports <<<"${firewall_contract[4]}"
readonly MINECRAFT_PUBLIC_ENABLED="${firewall_contract[5]}"
readonly -a public_tcp_ports
readonly -a effective_public_tcp_ports
readonly -a public_ipv6_tcp_ports
[[ "${MINECRAFT_PUBLIC_ENABLED}" == "true" ||
  "${MINECRAFT_PUBLIC_ENABLED}" == "false" ]] ||
  fail "platform_minecraft_public_enabled must be a boolean"

contains_port() {
  local needle="$1"
  shift
  local port
  for port in "$@"; do
    [[ "${port}" == "${needle}" ]] && return 0
  done
  return 1
}

ufw_status="$(ufw status verbose)"
grep -Fx 'Status: active' <<<"${ufw_status}" >/dev/null ||
  fail "UFW is not active"
grep -F \
  'Default: deny (incoming), deny (outgoing), deny (routed)' \
  <<<"${ufw_status}" >/dev/null ||
  fail "UFW defaults are not deny for incoming, outgoing and routed traffic"

ufw_ipv4_rules="$(iptables -S ufw-user-input)"
for port in "${public_tcp_ports[@]}"; do
  matching_rules="$(
    grep -F -- "-d ${PUBLIC_IPV4}/32" <<<"${ufw_ipv4_rules}" |
      grep -F -- "-i ${PUBLIC_INTERFACE}" |
      grep -F -- "--dport ${port}" |
      grep -E -- '(-j|--jump) ACCEPT([[:space:]]|$)' ||
      true
  )"
  if contains_port "${port}" "${effective_public_tcp_ports[@]}"; then
    [[ "$(grep -c . <<<"${matching_rules}")" -eq 1 ]] ||
      fail "UFW must authorize reviewed IPv4 TCP port ${port} exactly once"
  else
    [[ -z "${matching_rules}" ]] ||
      fail "staged IPv4 TCP port ${port} is open before its safety gate"
  fi
done

(( ${#public_ipv6_tcp_ports[@]} == 0 )) ||
  fail "public IPv6 application TCP is outside the reviewed contract"
ufw_ipv6_rules="$(ip6tables -S ufw6-user-input)"
for port in "${public_tcp_ports[@]}"; do
  matching_rules="$(
    grep -F -- "--dport ${port}" <<<"${ufw_ipv6_rules}" |
      grep -E -- '(-j|--jump) ACCEPT([[:space:]]|$)' ||
      true
  )"
  [[ -z "${matching_rules}" ]] ||
    fail "IPv6 application TCP port ${port} is unexpectedly public"
done

[[ "$(iptables -S INPUT | head -n 1)" == "-P INPUT DROP" ]] ||
  fail "IPv4 INPUT policy is not DROP"
[[ "$(iptables -S FORWARD | head -n 1)" == "-P FORWARD DROP" ]] ||
  fail "IPv4 FORWARD policy is not DROP"
[[ "$(iptables -S OUTPUT | head -n 1)" == "-P OUTPUT DROP" ]] ||
  fail "IPv4 OUTPUT policy is not DROP"
[[ "$(ip6tables -S INPUT | head -n 1)" == "-P INPUT DROP" ]] ||
  fail "IPv6 INPUT policy is not DROP"
[[ "$(ip6tables -S FORWARD | head -n 1)" == "-P FORWARD DROP" ]] ||
  fail "IPv6 FORWARD policy is not DROP"
[[ "$(ip6tables -S OUTPUT | head -n 1)" == "-P OUTPUT DROP" ]] ||
  fail "IPv6 OUTPUT policy is not DROP"

accepted_swarm_ports="$(
  {
    iptables-save
    ip6tables-save
  } |
    grep -E -- '--dport (2377|7946|4789)([[:space:]]|$)' |
    grep -E -- '(-j|--jump) ACCEPT([[:space:]]|$)' ||
    true
)"
[[ -z "${accepted_swarm_ports}" ]] || {
  printf '%s\n' "${accepted_swarm_ports}" >&2
  fail "an internal Swarm port is explicitly accepted"
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

iptables -C DOCKER-USER -j "${POLICY_CHAIN}" ||
  fail "${POLICY_CHAIN} is not reached from DOCKER-USER"
[[ "$(
  iptables -S DOCKER-USER |
    grep -Fc -- "-j ${POLICY_CHAIN}"
)" -eq 1 ]] ||
  fail "DOCKER-USER must contain exactly one ${POLICY_CHAIN} jump"

docker_user_rules="$(iptables -S DOCKER-USER)"
policy_position="$(
  grep -nF -- "-j ${POLICY_CHAIN}" <<<"${docker_user_rules}" |
    cut -d: -f1
)"
crowdsec_position="$(
  grep -niE -- '-j [^ ]*[Cc][Rr][Oo][Ww][Dd][Ss][Ee][Cc][^ ]*' \
    <<<"${docker_user_rules}" |
    head -n 1 |
    cut -d: -f1 ||
    true
)"
if [[ -n "${crowdsec_position}" ]] &&
  (( crowdsec_position >= policy_position )); then
  fail "CrowdSec must run before ${POLICY_CHAIN} in DOCKER-USER"
fi

iptables -C "${POLICY_CHAIN}" \
  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT ||
  fail "${POLICY_CHAIN} lacks the established-traffic rule"
for port in "${public_tcp_ports[@]}"; do
  if contains_port "${port}" "${effective_public_tcp_ports[@]}"; then
    iptables -C "${POLICY_CHAIN}" \
      -i "${PUBLIC_INTERFACE}" \
      -p tcp \
      -m conntrack --ctdir ORIGINAL \
      --ctorigdstport "${port}" \
      -j ACCEPT ||
      fail "${POLICY_CHAIN} lacks reviewed public TCP port ${port}"
  else
    ! iptables -C "${POLICY_CHAIN}" \
      -i "${PUBLIC_INTERFACE}" \
      -p tcp \
      -m conntrack --ctdir ORIGINAL \
      --ctorigdstport "${port}" \
      -j ACCEPT 2>/dev/null ||
      fail "${POLICY_CHAIN} opens staged TCP port ${port}"
  fi
done
iptables -C "${POLICY_CHAIN}" \
  -i "${PUBLIC_INTERFACE}" \
  -m conntrack --ctstate NEW \
  -j DROP ||
  fail "${POLICY_CHAIN} lacks the default public-ingress drop"
iptables -C "${POLICY_CHAIN}" -j RETURN ||
  fail "${POLICY_CHAIN} lacks its terminal return"

ip6tables -C DOCKER-USER -j "${POLICY_CHAIN}" ||
  fail "IPv6 ${POLICY_CHAIN} is not reached from DOCKER-USER"
[[ "$(
  ip6tables -S DOCKER-USER |
    grep -Fc -- "-j ${POLICY_CHAIN}"
)" -eq 1 ]] ||
  fail "IPv6 DOCKER-USER must contain exactly one ${POLICY_CHAIN} jump"

ip6_docker_user_rules="$(ip6tables -S DOCKER-USER)"
ip6_policy_position="$(
  grep -nF -- "-j ${POLICY_CHAIN}" <<<"${ip6_docker_user_rules}" |
    cut -d: -f1
)"
ip6_crowdsec_position="$(
  grep -niE -- '-j [^ ]*[Cc][Rr][Oo][Ww][Dd][Ss][Ee][Cc][^ ]*' \
    <<<"${ip6_docker_user_rules}" |
    head -n 1 |
    cut -d: -f1 ||
    true
)"
if [[ -n "${ip6_crowdsec_position}" ]] &&
  (( ip6_crowdsec_position >= ip6_policy_position )); then
  fail "IPv6 CrowdSec must run before ${POLICY_CHAIN} in DOCKER-USER"
fi

ip6tables -C "${POLICY_CHAIN}" \
  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT ||
  fail "IPv6 ${POLICY_CHAIN} lacks the established-traffic rule"
for port in "${public_tcp_ports[@]}"; do
  ! ip6tables -C "${POLICY_CHAIN}" \
    -i "${PUBLIC_INTERFACE}" \
    -p tcp \
    -m conntrack --ctdir ORIGINAL \
    --ctorigdstport "${port}" \
    -j ACCEPT 2>/dev/null ||
    fail "IPv6 ${POLICY_CHAIN} unexpectedly opens TCP port ${port}"
done
ip6tables -C "${POLICY_CHAIN}" \
  -i "${PUBLIC_INTERFACE}" \
  -m conntrack --ctstate NEW \
  -j DROP ||
  fail "IPv6 ${POLICY_CHAIN} lacks the default public-ingress drop"
ip6tables -C "${POLICY_CHAIN}" -j RETURN ||
  fail "IPv6 ${POLICY_CHAIN} lacks its terminal return"

published_ports=()
mapfile -t service_ids < <(docker service ls --quiet)
if (( ${#service_ids[@]} > 0 )); then
  mapfile -t published_ports < <(
    docker service inspect \
      --format \
      '{{range .Endpoint.Spec.Ports}}{{$.Spec.Name}}|{{.PublishedPort}}|{{.TargetPort}}|{{.Protocol}}|{{.PublishMode}}{{println}}{{end}}' \
      "${service_ids[@]}" |
      sed '/^[[:space:]]*$/d' |
      sort
  )
fi

expected_ports=()
mapfile -t active_service_names < <(docker service ls --format '{{.Name}}')
if printf '%s\n' "${active_service_names[@]}" |
  grep -Fx edge_traefik >/dev/null; then
  expected_ports+=(
    "edge_traefik|443|8443|tcp|host"
    "edge_traefik|80|8000|tcp|host"
  )
fi
if [[ "${MINECRAFT_PUBLIC_ENABLED}" == "true" ]]; then
  if printf '%s\n' "${active_service_names[@]}" |
    grep -Fx workloads_minecraft >/dev/null; then
    expected_ports+=(
      "workloads_minecraft|25565|25565|tcp|host"
    )
  fi
fi
if (( ${#published_ports[@]} > 0 || ${#expected_ports[@]} > 0 )); then
  mapfile -t expected_ports < <(printf '%s\n' "${expected_ports[@]}" | sort)
  [[ "$(printf '%s\n' "${published_ports[@]}")" == \
    "$(printf '%s\n' "${expected_ports[@]}")" ]] || {
    printf 'Published service ports:\n%s\n' \
      "$(printf '  %s\n' "${published_ports[@]}")" >&2
    fail "published service ports differ from the reviewed edge contract"
  }
fi

printf '%s\n' \
  "Firewall validation passed: only gated IPv4 ports are public," \
  "internal Swarm ports are closed, and DOCKER-USER ordering is valid."
