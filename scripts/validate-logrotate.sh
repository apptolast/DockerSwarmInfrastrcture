#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly SOURCE_CONFIG="${PROJECT_DIR}/config/logrotate/iptables"
readonly TARGET_CONFIG="/etc/logrotate.d/iptables"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

(( EUID == 0 )) || fail "run this script as root"

cmp -s "${SOURCE_CONFIG}" "${TARGET_CONFIG}" ||
  fail "installed iptables logrotate config differs from the repository"
[[ "$(stat --format='%a %U:%G' "${TARGET_CONFIG}")" == "644 root:root" ]] ||
  fail "unexpected metadata on ${TARGET_CONFIG}"

logrotate --debug /etc/logrotate.conf >/dev/null 2>&1 ||
  fail "logrotate configuration is invalid"
rsyslogd -N1 >/dev/null 2>&1 ||
  fail "rsyslog configuration is invalid"

systemctl is-enabled --quiet logrotate.timer ||
  fail "logrotate.timer is not enabled"
systemctl is-active --quiet logrotate.timer ||
  fail "logrotate.timer is not active"
[[ "$(systemctl show logrotate.service --property=Result --value)" == "success" ]] ||
  fail "the latest logrotate.service result is not successful"
systemctl is-active --quiet rsyslog.service ||
  fail "rsyslog.service is not active"

rsyslog_pid="$(systemctl show rsyslog.service --property=MainPID --value)"
[[ "${rsyslog_pid}" =~ ^[1-9][0-9]*$ ]] || fail "rsyslog has no valid PID"

stale_descriptor="$(
  readlink /proc/"${rsyslog_pid}"/fd/* 2>/dev/null |
    grep -F '/var/log/iptables.log.1' ||
    true
)"
[[ -z "${stale_descriptor}" ]] ||
  fail "rsyslog still writes to the rotated iptables log"

failed_units="$(systemctl list-units --state=failed --no-legend --plain)"
[[ -z "${failed_units}" ]] || {
  printf '%s\n' "${failed_units}" >&2
  fail "systemd still has failed units"
}

printf 'Logrotate and rsyslog validation passed.\n'
