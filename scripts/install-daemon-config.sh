#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly SOURCE_CONFIG="${PROJECT_DIR}/config/daemon.json"
readonly TARGET_CONFIG="/etc/docker/daemon.json"
readonly EXPECTED_SHA256="b2759d7fc799030511d1de541edcfc18549e40409921546d775132f7e9737baa"

restart_daemon=true

usage() {
  printf 'Usage: %s [--no-restart]\n' "${0##*/}"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

case "${1:-}" in
  "")
    ;;
  --no-restart)
    restart_daemon=false
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

for command_name in dockerd install sha256sum systemctl; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done

[[ -f "${SOURCE_CONFIG}" ]] || fail "missing source config: ${SOURCE_CONFIG}"

actual_sha256="$(sha256sum "${SOURCE_CONFIG}" | awk '{print $1}')"
[[ "${actual_sha256}" == "${EXPECTED_SHA256}" ]] ||
  fail "unexpected source checksum: ${actual_sha256}"

dockerd --validate --config-file="${SOURCE_CONFIG}"

exec_start="$(systemctl show docker.service --property=ExecStart --value)"
if [[ "${exec_start}" =~ (^|[[:space:]])--log-driver(=|[[:space:]]) ]]; then
  fail "log-driver is already set in the systemd ExecStart command"
fi

temporary_config="$(mktemp --tmpdir=/etc/docker .daemon.json.XXXXXX)"
cleanup() {
  rm -f -- "${temporary_config}"
}
trap cleanup EXIT

install --owner=root --group=root --mode=0644 \
  "${SOURCE_CONFIG}" "${temporary_config}"
dockerd --validate --config-file="${temporary_config}"

if [[ -e "${TARGET_CONFIG}" ]] && ! cmp -s "${temporary_config}" "${TARGET_CONFIG}"; then
  backup_path="${TARGET_CONFIG}.backup.$(date -u +%Y%m%dT%H%M%SZ)"
  cp --archive -- "${TARGET_CONFIG}" "${backup_path}"
  printf 'Backup created: %s\n' "${backup_path}"
fi

mv --force -- "${temporary_config}" "${TARGET_CONFIG}"
trap - EXIT

chown root:root "${TARGET_CONFIG}"
chmod 0644 "${TARGET_CONFIG}"
dockerd --validate --config-file="${TARGET_CONFIG}"

if [[ "${restart_daemon}" == false ]]; then
  printf 'Installed %s; Docker restart intentionally deferred.\n' "${TARGET_CONFIG}"
  exit 0
fi

systemctl restart docker.service
systemctl is-active --quiet docker.service ||
  fail "docker.service is not active after restart"
systemctl is-enabled --quiet docker.service ||
  fail "docker.service is not enabled"

logging_driver="$(docker info --format '{{.LoggingDriver}}')"
[[ "${logging_driver}" == "local" ]] ||
  fail "unexpected logging driver after restart: ${logging_driver}"

bash \
  "${PROJECT_DIR}/scripts/validate-daemon-journal.sh" \
  --allow-known-swarm-startup

printf 'Docker daemon configuration installed and verified.\n'
