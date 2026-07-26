#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

readonly PUBLIC_INTERFACE="eth0"
readonly POLICY_CHAIN="DOCKERSWARM-INGRESS"

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

ufw_status="$(ufw status verbose)"
grep -Fx 'Status: active' <<<"${ufw_status}" >/dev/null ||
  fail "UFW is not active"
grep -F \
  'Default: deny (incoming), deny (outgoing), deny (routed)' \
  <<<"${ufw_status}" >/dev/null ||
  fail "UFW defaults are not deny for incoming, outgoing and routed traffic"

for port in 80 443; do
  grep -E \
    "^${port}/tcp[[:space:]]+ALLOW IN[[:space:]]+Anywhere" \
    <<<"${ufw_status}" >/dev/null ||
    fail "UFW does not authorize reviewed TCP port ${port}"
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
for port in 80 443; do
  iptables -C "${POLICY_CHAIN}" \
    -i "${PUBLIC_INTERFACE}" \
    -p tcp \
    -m conntrack --ctdir ORIGINAL \
    --ctorigdstport "${port}" \
    -j ACCEPT ||
    fail "${POLICY_CHAIN} lacks reviewed public TCP port ${port}"
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
for port in 80 443; do
  ip6tables -C "${POLICY_CHAIN}" \
    -i "${PUBLIC_INTERFACE}" \
    -p tcp \
    -m conntrack --ctdir ORIGINAL \
    --ctorigdstport "${port}" \
    -j ACCEPT ||
    fail "IPv6 ${POLICY_CHAIN} lacks reviewed public TCP port ${port}"
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

expected_ports=(
  "edge_traefik|443|8443|tcp|host"
  "edge_traefik|80|8000|tcp|host"
)
if (( ${#published_ports[@]} > 0 )); then
  mapfile -t expected_ports < <(printf '%s\n' "${expected_ports[@]}" | sort)
  [[ "$(printf '%s\n' "${published_ports[@]}")" == \
    "$(printf '%s\n' "${expected_ports[@]}")" ]] || {
    printf 'Published service ports:\n%s\n' \
      "$(printf '  %s\n' "${published_ports[@]}")" >&2
    fail "published service ports differ from the reviewed edge contract"
  }
fi

printf '%s\n' \
  "Firewall validation passed: only reviewed edge ports are public," \
  "internal Swarm ports are closed, and DOCKER-USER ordering is valid."
