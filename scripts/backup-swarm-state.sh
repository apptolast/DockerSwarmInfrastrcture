#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

readonly BACKUP_CONTROLLER=/usr/local/libexec/dockerswarm-backupctl
readonly BACKUP_CONFIG=/etc/dockerswarm/backup/config.json

[[ -x "${BACKUP_CONTROLLER}" ]] || {
  printf 'ERROR: backup controller is not installed\n' >&2
  exit 1
}

exec "${BACKUP_CONTROLLER}" \
  --config "${BACKUP_CONFIG}" \
  swarm-state \
  --confirm-docker-stop
