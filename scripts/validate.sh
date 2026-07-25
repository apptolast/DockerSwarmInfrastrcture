#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly SOURCE_CONFIG="${PROJECT_DIR}/config/daemon.json"
readonly TARGET_CONFIG="/etc/docker/daemon.json"
readonly EXPECTED_SHA256="d526660c82329ffb132a48c2b1f4d3e303ccbfb667c9876fb1cb36997efe6873"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

for command_name in cmp docker dockerd sha256sum stat systemctl; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done

[[ -r "${TARGET_CONFIG}" ]] || fail "cannot read ${TARGET_CONFIG}"
cmp -s "${SOURCE_CONFIG}" "${TARGET_CONFIG}" ||
  fail "installed daemon config differs from the repository"

actual_sha256="$(sha256sum "${TARGET_CONFIG}" | awk '{print $1}')"
[[ "${actual_sha256}" == "${EXPECTED_SHA256}" ]] ||
  fail "unexpected installed checksum: ${actual_sha256}"

file_metadata="$(stat --format='%a %U:%G' "${TARGET_CONFIG}")"
[[ "${file_metadata}" == "644 root:root" ]] ||
  fail "unexpected daemon config metadata: ${file_metadata}"

dockerd --validate --config-file="${TARGET_CONFIG}"
systemctl is-active --quiet docker.service ||
  fail "docker.service is not active"
systemctl is-enabled --quiet docker.service ||
  fail "docker.service is not enabled"

docker version >/dev/null

[[ "$(docker info --format '{{.LoggingDriver}}')" == "local" ]] ||
  fail "Docker default logging driver is not local"
[[ "$(docker info --format '{{.Swarm.LocalNodeState}}')" == "active" ]] ||
  fail "Swarm is not active"
[[ "$(docker info --format '{{.Swarm.ControlAvailable}}')" == "true" ]] ||
  fail "the local node is not a manager"

docker_warnings="$(docker info --format '{{json .Warnings}}')"
[[ "${docker_warnings}" == "null" || "${docker_warnings}" == "[]" ]] ||
  fail "Docker reports warnings: ${docker_warnings}"

mapfile -t nodes < <(
  docker node ls --format '{{.Hostname}}|{{.Status}}|{{.Availability}}|{{.ManagerStatus}}'
)
[[ "${#nodes[@]}" -eq 1 ]] ||
  fail "expected exactly one Swarm node, found ${#nodes[@]}"

IFS='|' read -r node_hostname node_status node_availability node_manager_status \
  <<<"${nodes[0]}"
[[ -n "${node_hostname}" ]] || fail "node hostname is empty"
[[ "${node_status}" == "Ready" ]] ||
  fail "node status is ${node_status}"
[[ "${node_availability}" == "Active" ]] ||
  fail "node availability is ${node_availability}"
[[ "${node_manager_status}" == "Leader" ]] ||
  fail "manager status is ${node_manager_status}"

service_count="$(docker service ls --quiet | wc -l)"
[[ "${service_count}" -eq 0 ]] ||
  fail "expected no persistent services, found ${service_count}"

if (( EUID == 0 )); then
  bash \
    "${PROJECT_DIR}/scripts/validate-daemon-journal.sh" \
    --allow-known-swarm-startup
  bash "${PROJECT_DIR}/scripts/validate-firewall.sh"
  bash "${PROJECT_DIR}/scripts/validate-logrotate.sh"
fi

printf 'Docker Swarm validation passed for node %s.\n' "${node_hostname}"
