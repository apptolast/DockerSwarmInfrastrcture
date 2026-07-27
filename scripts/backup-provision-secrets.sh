#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly HOST_LOCK_HELPER="${SCRIPT_DIR}/host_global_operation_lock.py"
readonly SCRIPT_PATH="${SCRIPT_DIR}/${BASH_SOURCE[0]##*/}"
readonly CREDENTIAL_DIRECTORY=/etc/dockerswarm/backup
temporary_directory=""
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

cleanup() {
  [[ -z "${temporary_directory}" || ! -d "${temporary_directory}" ]] ||
    find "${temporary_directory}" -depth -delete
  unset r2_access_key_id r2_secret_access_key
  unset restic_password restic_password_confirmation swarm_unlock_key
}
trap cleanup EXIT

(( EUID == 0 )) || fail "run this command as root from a trusted console"
(( $# == 0 )) || fail "this command does not accept arguments"
ensure_host_global_lock backup-provision-secrets

for command_name in docker find install mktemp; do
  command -v "${command_name}" >/dev/null ||
    fail "required command is unavailable: ${command_name}"
done

for credential_name in \
  restic-password \
  r2-access-key-id \
  r2-secret-access-key \
  swarm-unlock-key; do
  [[ ! -e "${CREDENTIAL_DIRECTORY}/${credential_name}" ]] ||
    fail "refusing to overwrite ${CREDENTIAL_DIRECTORY}/${credential_name}"
done

autolock="$(
  docker info \
    --format '{{json .Swarm.Cluster.Spec.EncryptionConfig.AutoLockManagers}}'
)"
[[ "${autolock}" == "true" ]] ||
  fail "enable Swarm autolock and escrow its key before provisioning backup"

swarm_unlock_key="$(docker swarm unlock-key -q)"
[[ "${swarm_unlock_key}" == SWMKEY-1-* ]] ||
  fail "Docker returned a malformed Swarm unlock key"

IFS= read -r -s -p "R2 access-key ID: " r2_access_key_id
printf '\n'
IFS= read -r -s -p "R2 secret-access key: " r2_secret_access_key
printf '\n'
IFS= read -r -s -p "New restic repository password: " restic_password
printf '\n'
IFS= read -r -s -p "Repeat restic repository password: " \
  restic_password_confirmation
printf '\n'

[[ "${r2_access_key_id}" =~ ^[A-Za-z0-9]{16,128}$ ]] ||
  fail "R2 access-key ID format is invalid"
[[ "${r2_secret_access_key}" =~ ^[A-Za-z0-9/+=_-]{16,256}$ ]] ||
  fail "R2 secret-access key format is invalid"
[[ ${#restic_password} -ge 32 ]] ||
  fail "restic repository password must contain at least 32 characters"
[[ "${restic_password}" == "${restic_password_confirmation}" ]] ||
  fail "restic repository passwords do not match"

temporary_directory="$(mktemp -d)"
printf '%s\n' "${restic_password}" \
  >"${temporary_directory}/restic-password"
printf '%s\n' "${r2_access_key_id}" \
  >"${temporary_directory}/r2-access-key-id"
printf '%s\n' "${r2_secret_access_key}" \
  >"${temporary_directory}/r2-secret-access-key"
printf '%s\n' "${swarm_unlock_key}" \
  >"${temporary_directory}/swarm-unlock-key"

install \
  --directory \
  --owner=root \
  --group=root \
  --mode=0700 \
  "${CREDENTIAL_DIRECTORY}"
for credential_name in \
  restic-password \
  r2-access-key-id \
  r2-secret-access-key \
  swarm-unlock-key; do
  install \
    --owner=root \
    --group=root \
    --mode=0600 \
    "${temporary_directory}/${credential_name}" \
    "${CREDENTIAL_DIRECTORY}/${credential_name}"
done

printf 'Backup credentials and Swarm unlock-key escrow installed as root:root 0600.\n'
