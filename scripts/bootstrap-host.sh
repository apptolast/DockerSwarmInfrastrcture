#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly ANSIBLE_PLAYBOOK="${PROJECT_DIR}/.venv/bin/ansible-playbook"
readonly ANSIBLE="${PROJECT_DIR}/.venv/bin/ansible"
readonly LOCK_HELPER="${PROJECT_DIR}/scripts/ansible-operation-lock.py"
readonly LOCKED_COMMAND_RUNNER="${PROJECT_DIR}/scripts/run-locked-command.py"
readonly PYTHON_BIN="/usr/bin/python3"

target_host=""
authorized_keys_file=""
password_hash_file=""
confirmed=false
operation_id=""
operation_controller=""
lock_holder_pid=""
lock_holder_read_fd=""
lock_holder_write_fd=""
cleanup_started=false

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

close_lock_holder() {
  if [[ -n "${lock_holder_write_fd}" ]]; then
    exec {lock_holder_write_fd}>&-
    lock_holder_write_fd=""
  fi
  if [[ -n "${lock_holder_pid}" ]]; then
    wait "${lock_holder_pid}" 2>/dev/null || true
    lock_holder_pid=""
  fi
  if [[ -n "${lock_holder_read_fd}" ]]; then
    exec {lock_holder_read_fd}<&-
    lock_holder_read_fd=""
  fi
}

cleanup() {
  local exit_status=$?
  if [[ "${cleanup_started}" == true ]]; then
    return
  fi
  cleanup_started=true
  trap - EXIT INT TERM HUP
  close_lock_holder
  exit "${exit_status}"
}

trap cleanup EXIT
trap 'exit 130' INT TERM HUP

usage() {
  cat <<'EOF'
Usage:
  bootstrap-host.sh --host IPV4 --authorized-keys-file PATH
    --password-hash-file PATH --confirm-production

The target must be a fresh Ubuntu 26.04 host reachable as root with an SSH key
already trusted in known_hosts. Input files stay outside Git and must be
regular, non-symlink files owned by the caller with mode 0600.
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --host)
      (( $# >= 2 )) || fail "--host requires a value"
      target_host="$2"
      shift 2
      ;;
    --authorized-keys-file)
      (( $# >= 2 )) || fail "--authorized-keys-file requires a value"
      authorized_keys_file="$2"
      shift 2
      ;;
    --password-hash-file)
      (( $# >= 2 )) || fail "--password-hash-file requires a value"
      password_hash_file="$2"
      shift 2
      ;;
    --confirm-production)
      confirmed=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "unknown argument: $1"
      ;;
  esac
done

[[ "${confirmed}" == true ]] ||
  fail "bootstrap requires --confirm-production"
[[ -x "${ANSIBLE_PLAYBOOK}" && -x "${ANSIBLE}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -r "${LOCK_HELPER}" && -r "${LOCKED_COMMAND_RUNNER}" ]] ||
  fail "bootstrap operation-lock helpers are absent"
for command_name in awk base64 flock git hostname sha256sum ssh; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done

canonical_target_host="$(
  /usr/bin/python3 - "${target_host}" <<'PY'
import ipaddress
import sys

try:
    address = ipaddress.ip_address(sys.argv[1])
except ValueError:
    raise SystemExit(1)
if address.version != 4:
    raise SystemExit(1)
print(address)
PY
)" || fail "--host must be a canonical IPv4 address"
[[ "${canonical_target_host}" == "${target_host}" ]] ||
  fail "--host must be a canonical IPv4 address"

for input_path in "${authorized_keys_file}" "${password_hash_file}"; do
  [[ -n "${input_path}" && -f "${input_path}" && ! -L "${input_path}" ]] ||
    fail "bootstrap input is absent, non-regular or a symlink"
  [[ "$(stat --format='%a:%u' -- "${input_path}")" == "600:${UID}" ]] ||
    fail "bootstrap inputs must be caller-owned mode 0600"
done

exec 8>"${PROJECT_DIR}/.git/dockerswarm-bootstrap.lock"
flock --nonblock 8 ||
  fail "another fresh-host wrapper is using this checkout"

worktree_status="$(
  git -C "${PROJECT_DIR}" status --porcelain --untracked-files=normal
)"
[[ -z "${worktree_status}" ]] ||
  fail "commit or discard every worktree change before host bootstrap"
source_revision="$(
  git -C "${PROJECT_DIR}" rev-parse --verify HEAD
)"
[[ "${source_revision}" =~ ^[a-f0-9]{40}$ ]] ||
  fail "cannot resolve the source commit"

bootstrap_contract_sha256="$(
  for contract_path in \
    "${PROJECT_DIR}/config/host-security.yml" \
    "${PROJECT_DIR}/config/platform.yml"; do
    sha256sum "${contract_path}" | awk '{print $1}'
  done |
    sha256sum |
    awk '{print $1}'
)"
[[ "${bootstrap_contract_sha256}" =~ ^[a-f0-9]{64}$ ]] ||
  fail "cannot hash the fresh-host contracts"
operation_id="$(
  "${PYTHON_BIN}" -c 'import secrets; print(secrets.token_hex(32))'
)"
[[ "${operation_id}" =~ ^[a-f0-9]{64}$ ]] ||
  fail "cannot generate a bootstrap operation ID"
controller_host="$(hostname --fqdn 2>/dev/null || hostname)"
operation_controller="$(id -un)@${controller_host}"
[[ "${operation_controller}" =~ ^[A-Za-z0-9_.@:-]{1,128}$ ]] ||
  fail "controller identity is not safe for bootstrap metadata"

lock_arguments=(
  hold
  --lock-path
  /run/lock/dockerswarm-iac.lock
  --marker-path
  /run/lock/dockerswarm-bootstrap.marker
  --owner-uid
  0
  --owner-gid
  0
  --operation-id
  "${operation_id}"
  --source-revision
  "${source_revision}"
  --contract-sha256
  "${bootstrap_contract_sha256}"
  --playbook
  bootstrap-host
  --profile
  fresh-host
  --mode
  apply
  --controller
  "${operation_controller}"
)
lock_helper_payload="$(base64 --wrap=0 "${LOCK_HELPER}")"
[[ "${lock_helper_payload}" =~ ^[A-Za-z0-9+/=]+$ ]] ||
  fail "cannot encode the bootstrap operation-lock helper"
remote_python=(
  /usr/bin/python3
  -c
  "import base64;exec(compile(base64.b64decode('${lock_helper_payload}'),'<dockerswarm-bootstrap-lock>','exec'))"
  "${lock_arguments[@]}"
)
printf -v remote_lock_command '%q ' "${remote_python[@]}"
coproc BOOTSTRAP_LOCK_HOLDER {
  /usr/bin/ssh \
    -T \
    -o BatchMode=yes \
    -o ControlMaster=no \
    -o StrictHostKeyChecking=yes \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=3 \
    "root@${target_host}" \
    "${remote_lock_command}"
}
lock_holder_pid="${BOOTSTRAP_LOCK_HOLDER_PID}"
lock_holder_read_fd="${BOOTSTRAP_LOCK_HOLDER[0]}"
lock_holder_write_fd="${BOOTSTRAP_LOCK_HOLDER[1]}"
lock_handshake=""
IFS= read -r -t 20 lock_handshake <&"${lock_holder_read_fd}" ||
  fail "the atomic fresh-host bootstrap intent was not acquired"
[[ "${lock_handshake}" == "LOCKED:${operation_id}" ]] ||
  fail "fresh-host bootstrap lock handshake is invalid"
kill -0 "${lock_holder_pid}" 2>/dev/null ||
  fail "fresh-host bootstrap lock holder exited after its handshake"

run_guarded() {
  local guarded_status
  set +e
  "${PYTHON_BIN}" \
    "${LOCKED_COMMAND_RUNNER}" \
    --watch-pid "${lock_holder_pid}" \
    -- \
    "$@"
  guarded_status=$?
  set -e
  [[ "${guarded_status}" -eq 0 ]] ||
    fail "bootstrap command failed; operation marker retained for recovery"
  kill -0 "${lock_holder_pid}" 2>/dev/null ||
    fail "bootstrap lock holder died; operation marker retained for recovery"
}

export ANSIBLE_CONFIG="${PROJECT_DIR}/ansible/ansible.cfg"
bootstrap_metadata_args=(
  --extra-vars
  "deployment_metadata_source_revision=${source_revision}"
  --extra-vars
  "deployment_metadata_contract_sha256=${bootstrap_contract_sha256}"
  --extra-vars
  "deployment_metadata_profile=fresh-host"
  --extra-vars
  "deployment_metadata_playbook=bootstrap-host"
  --extra-vars
  "operation_lock_guard_operation_id=${operation_id}"
  --extra-vars
  "operation_lock_guard_controller=${operation_controller}"
  --extra-vars
  "operation_lock_guard_mode=apply"
)

printf 'Fresh-host source revision: %s\n' "${source_revision}"
printf 'Fresh-host operation ID: %s\n' "${operation_id}"
run_guarded "${ANSIBLE_PLAYBOOK}" \
  --inventory "${target_host}," \
  --user root \
  "${PROJECT_DIR}/ansible/playbooks/bootstrap-host.yml" \
  --extra-vars \
  "host_bootstrap_authorized_keys_file=${authorized_keys_file}" \
  --extra-vars \
  "host_bootstrap_password_hash_file=${password_hash_file}" \
  --extra-vars \
  "host_bootstrap_source_revision=${source_revision}" \
  --extra-vars \
  "host_bootstrap_operation_id=${operation_id}" \
  --extra-vars \
  "host_bootstrap_operation_controller=${operation_controller}" \
  --extra-vars \
  "host_bootstrap_operation_contract_sha256=${bootstrap_contract_sha256}" \
  "${bootstrap_metadata_args[@]}"

run_guarded "${ANSIBLE}" \
  --inventory "${target_host}," \
  --user admin \
  --extra-vars ansible_become=false \
  all \
  --module-name ansible.builtin.ping

run_guarded "${ANSIBLE_PLAYBOOK}" \
  --inventory "${target_host}," \
  --user root \
  "${PROJECT_DIR}/ansible/playbooks/bootstrap-host-security.yml" \
  "${bootstrap_metadata_args[@]}"

run_guarded "${ANSIBLE}" \
  --inventory "${target_host}," \
  --user admin \
  --extra-vars ansible_become=false \
  all \
  --module-name ansible.builtin.ping

run_guarded "${ANSIBLE_PLAYBOOK}" \
  --inventory "${target_host}," \
  --user root \
  "${PROJECT_DIR}/ansible/playbooks/bootstrap-host-ssh-final.yml" \
  --extra-vars \
  "host_security_ssh_confirmation_nonce=${source_revision}" \
  "${bootstrap_metadata_args[@]}"

run_guarded "${ANSIBLE}" \
  --inventory "${target_host}," \
  --user admin \
  --extra-vars ansible_become=false \
  all \
  --module-name ansible.builtin.copy \
  --args \
  "content=${source_revision} \
dest=/run/dockerswarm-bootstrap-ssh/confirmed \
owner=admin group=admin mode=0600"

rollback_units_removed=false
for _ in {1..30}; do
  if /usr/bin/ssh \
    -o BatchMode=yes \
    -o ConnectTimeout=5 \
    -o ConnectionAttempts=1 \
    "admin@${target_host}" \
    "test ! -e \
/etc/systemd/system/dockerswarm-bootstrap-ssh-rollback.timer \
-a ! -e /etc/systemd/system/dockerswarm-bootstrap-ssh-rollback.path \
-a ! -e /etc/systemd/system/dockerswarm-bootstrap-ssh-rollback.service"
  then
    rollback_units_removed=true
    break
  fi
  kill -0 "${lock_holder_pid}" 2>/dev/null ||
    fail "bootstrap lock holder died before SSH confirmation"
  sleep 1
done
[[ "${rollback_units_removed}" == true ]] ||
  fail "SSH confirmation watcher did not disarm the rollback units"

run_guarded "${ANSIBLE}" \
  --inventory "${target_host}," \
  --user admin \
  --extra-vars ansible_become=false \
  all \
  --module-name ansible.builtin.ping

if /usr/bin/ssh \
  -o BatchMode=yes \
  -o ConnectTimeout=5 \
  -o ConnectionAttempts=1 \
  "root@${target_host}" true >/dev/null 2>&1; then
  fail "root SSH remains usable after the security bootstrap"
fi

[[ "$(git -C "${PROJECT_DIR}" rev-parse --verify HEAD)" == "${source_revision}" ]] ||
  fail "source HEAD changed during bootstrap; operation marker retained"
post_worktree_status="$(
  git -C "${PROJECT_DIR}" status --porcelain --untracked-files=normal
)"
[[ -z "${post_worktree_status}" ]] ||
  fail "source worktree changed during bootstrap; operation marker retained"

printf 'RELEASE:%s\n' "${operation_id}" >&"${lock_holder_write_fd}"
release_handshake=""
IFS= read -r -t 20 release_handshake <&"${lock_holder_read_fd}" ||
  fail "fresh-host bootstrap lock release was not acknowledged"
[[ "${release_handshake}" == "RELEASED:${operation_id}" ]] ||
  fail "fresh-host bootstrap lock release acknowledgement is invalid"
exec {lock_holder_write_fd}>&-
lock_holder_write_fd=""
set +e
wait "${lock_holder_pid}"
lock_holder_status=$?
set -e
lock_holder_pid=""
exec {lock_holder_read_fd}<&-
lock_holder_read_fd=""
[[ "${lock_holder_status}" -eq 0 ]] ||
  fail "fresh-host bootstrap lock holder failed during release"
trap - EXIT INT TERM HUP

printf '%s\n' \
  'Fresh-host identity, security base and independent admin login passed.'
