#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

allow_known_swarm_startup=false

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  printf 'Usage: %s [--allow-known-swarm-startup]\n' "${0##*/}"
}

case "${1:-}" in
  "")
    ;;
  --allow-known-swarm-startup)
    allow_known_swarm_startup=true
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

(( EUID == 0 )) || fail "run this script as root"

systemctl is-active --quiet docker.service ||
  fail "docker.service is not active"

invocation_id="$(
  systemctl show \
    docker.service \
    --property=InvocationID \
    --value
)"
[[ -n "${invocation_id}" ]] || fail "docker.service has no InvocationID"

invocation_log="$(
  journalctl \
    --unit=docker.service \
    --invocation="${invocation_id}" \
    --quiet \
    --no-pager \
    --output=cat
)"

bad_levels="$(
  grep -Ei \
    '(^|[[:space:]])level=(warn|warning|error|fatal|panic)([[:space:]]|$)' \
    <<<"${invocation_log}" ||
    true
)"

bad_priorities="$(
  journalctl \
    --unit=docker.service \
    --invocation="${invocation_id}" \
    --priority=warning \
    --quiet \
    --no-pager \
    --output=cat |
    grep -Eiv \
      '(^|[[:space:]])level=(debug|info|notice)([[:space:]]|$)' ||
    true
)"

mapfile -t problem_lines < <(
  printf '%s\n%s\n' "${bad_levels}" "${bad_priorities}" |
    sed '/^[[:space:]]*$/d' |
    sort --unique
)

known_cluster_count=0
known_mac_count=0
unexpected_lines=()

for problem_line in "${problem_lines[@]}"; do
  case "${problem_line}" in
    *'level=error msg="error creating cluster object" error="name conflicts with an existing object" module=node'*)
      (( known_cluster_count += 1 ))
      ;;
    *'level=warning msg="MAC address changed" iface=br0 '*)
      (( known_mac_count += 1 ))
      ;;
    *)
      unexpected_lines+=("${problem_line}")
      ;;
  esac
done

if (( ${#unexpected_lines[@]} > 0 )); then
  printf '%s\n' "${unexpected_lines[@]}" >&2
  fail "docker.service emitted unexpected warning-or-higher entries"
fi

if (( known_cluster_count > 1 || known_mac_count > 1 )); then
  printf 'cluster_conflicts=%d mac_changes=%d\n' \
    "${known_cluster_count}" "${known_mac_count}" >&2
  fail "known startup diagnostics occurred more than once"
fi

known_count=$((known_cluster_count + known_mac_count))
if (( known_count > 0 )); then
  [[ "${allow_known_swarm_startup}" == true ]] ||
    fail "known Swarm startup diagnostics require explicit acknowledgement"

  [[ "$(docker info --format '{{.Swarm.LocalNodeState}}')" == "active" ]] ||
    fail "known startup diagnostics appeared while Swarm was not active"
  [[ "$(docker info --format '{{.Swarm.ControlAvailable}}')" == "true" ]] ||
    fail "known startup diagnostics appeared on a non-manager"

  node_state="$(
    docker node inspect \
      self \
      --format '{{.Status.State}}|{{.Spec.Availability}}|{{.ManagerStatus.Leader}}'
  )"
  [[ "${node_state}" == "ready|active|true" ]] ||
    fail "manager health does not justify known startup diagnostics: ${node_state}"
fi

printf 'Docker journal validation passed for invocation %s' "${invocation_id}"
if (( known_count > 0 )); then
  printf ' with %d documented Docker startup diagnostics' "${known_count}"
fi
printf '.\n'
