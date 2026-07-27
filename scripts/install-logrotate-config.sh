#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly SOURCE_CONFIG="${PROJECT_DIR}/config/logrotate/iptables"
readonly TARGET_CONFIG="/etc/logrotate.d/iptables"
readonly BACKUP_DIR="/var/backups/dockerswarm"
readonly HOST_LOCK_HELPER="${PROJECT_DIR}/scripts/host_global_operation_lock.py"
readonly SCRIPT_PATH="${SCRIPT_DIR}/${BASH_SOURCE[0]##*/}"
original_args=("$@")

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

ensure_host_global_lock() {
  local operation=$1
  [[ -f "${HOST_LOCK_HELPER}" && ! -L "${HOST_LOCK_HELPER}" ]] ||
    fail "host-global operation-lock helper is absent or unsafe"
  if [[ -n "${DOCKERSWARM_IAC_LOCK_SCOPE:-}" ]]; then
    /usr/bin/python3 "${HOST_LOCK_HELPER}" \
      prove --operation "${operation}" >/dev/null ||
      fail "host-global operation-lock proof failed"
    return
  fi
  exec /usr/bin/python3 "${HOST_LOCK_HELPER}" \
    run --operation "${operation}" -- \
    "${SCRIPT_PATH}" "${original_args[@]}"
}

(( EUID == 0 )) || fail "run this script as root"
(( $# == 0 )) || fail "this command does not accept arguments"
ensure_host_global_lock logrotate-config-install

for command_name in install logger logrotate systemctl; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done

[[ -f "${SOURCE_CONFIG}" ]] || fail "missing source config: ${SOURCE_CONFIG}"
[[ -x /usr/lib/rsyslog/rsyslog-rotate ]] ||
  fail "the packaged rsyslog rotation helper is unavailable"

install --directory --owner=root --group=root --mode=0700 "${BACKUP_DIR}"

temporary_config="$(mktemp --tmpdir=/etc/logrotate.d .iptables.XXXXXX)"
trap 'rm -f -- "${temporary_config}"' EXIT

install --owner=root --group=root --mode=0644 \
  "${SOURCE_CONFIG}" "${temporary_config}"
logrotate --debug "${temporary_config}" >/dev/null 2>&1 ||
  fail "source logrotate configuration is invalid"

if [[ -e "${TARGET_CONFIG}" ]] && ! cmp -s "${SOURCE_CONFIG}" "${TARGET_CONFIG}"; then
  backup_path="${BACKUP_DIR}/iptables.logrotate.$(date -u +%Y%m%dT%H%M%SZ)"
  install --owner=root --group=root --mode=0600 \
    "${TARGET_CONFIG}" "${backup_path}"
  printf 'Backup created: %s\n' "${backup_path}"
fi

mv --force -- "${temporary_config}" "${TARGET_CONFIG}"
trap - EXIT

logrotate --debug /etc/logrotate.conf >/dev/null 2>&1 ||
  fail "installed logrotate configuration is invalid"
rsyslogd -N1 >/dev/null

/usr/lib/rsyslog/rsyslog-rotate
systemctl start logrotate.service

if systemctl is-failed --quiet logrotate.service; then
  fail "logrotate.service remains failed"
fi
[[ "$(systemctl show logrotate.service --property=Result --value)" == "success" ]] ||
  fail "logrotate.service did not finish successfully"
systemctl is-active --quiet rsyslog.service ||
  fail "rsyslog.service is not active"

validation_marker="dockerswarm-logrotate-validation-$(date +%s)"
logger --tag=dockerswarm-validation "[IPTABLES] ${validation_marker}"

for _ in {1..10}; do
  if grep -F "${validation_marker}" /var/log/iptables.log >/dev/null; then
    printf 'iptables log rotation and rsyslog reopen validation passed.\n'
    exit 0
  fi
  sleep 1
done

fail "rsyslog did not write to the current iptables log"
