#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly ANSIBLE_PLAYBOOK="${PROJECT_DIR}/.venv/bin/ansible-playbook"
readonly ANSIBLE_INVENTORY="${PROJECT_DIR}/.venv/bin/ansible-inventory"
readonly LOCK_HELPER="${PROJECT_DIR}/scripts/ansible-operation-lock.py"
readonly LOCKED_COMMAND_RUNNER="${PROJECT_DIR}/scripts/run-locked-command.py"
readonly SUDO_BIN="/usr/bin/sudo"
readonly PYTHON_BIN="/usr/bin/python3"

playbook_name=""
deployment_profile="production"
check_only=false
confirmed_production=false
ask_become_pass=false
local_mode=false
operation_id=""
operation_controller=""
operation_mode=""
lock_holder_pid=""
lock_holder_read_fd=""
lock_holder_write_fd=""
cleanup_started=false

usage() {
  cat <<'EOF'
Usage:
  deploy-ansible.sh --playbook NAME [--profile PROFILE]
      [--local | --ask-become-pass] [--check | --confirm-production]

NAME is one of: platform, host-baseline, preflight-images, edge, workloads,
observability, backup, site.
PROFILE is production (default) or acme-staging. The staging profile is valid
only with the edge playbook and uses a separate ACME storage file.

The wrapper refuses a dirty worktree and records the exact commit plus the
combined host, capacity, platform, service, Minecraft and secret-catalog
contract checksum on the host. Every check/apply holds the same host-global
operation lock. A crash or Ansible failure retains a recovery marker rather
than allowing another potentially overlapping mutation. A real run requires
--confirm-production.
Remote execution is the default. --local uses the localhost inventory and runs
the complete Ansible process through sudo when needed; no password is stored.
Backup is deliberately separate from site and remains fail-closed until its R2
credentials, restic password, Swarm unlock-key escrow and activation flag exist.
EOF
}

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

while (( $# > 0 )); do
  case "$1" in
    --playbook)
      (( $# >= 2 )) || fail "--playbook requires a value"
      playbook_name="$2"
      shift 2
      ;;
    --profile)
      (( $# >= 2 )) || fail "--profile requires a value"
      deployment_profile="$2"
      shift 2
      ;;
    --check)
      check_only=true
      shift
      ;;
    --confirm-production)
      confirmed_production=true
      shift
      ;;
    --ask-become-pass)
      ask_become_pass=true
      shift
      ;;
    --local)
      local_mode=true
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

case "${playbook_name}" in
  platform|host-baseline|preflight-images|edge|workloads|observability|backup|site)
    ;;
  *)
    fail "--playbook is missing or invalid"
    ;;
esac
case "${deployment_profile}" in
  production)
    ;;
  acme-staging)
    [[ "${playbook_name}" == "edge" ]] ||
      fail "acme-staging is valid only for the edge playbook"
    ;;
  *)
    fail "--profile must be production or acme-staging"
    ;;
esac
if [[ "${check_only}" == true && "${confirmed_production}" == true ]]; then
  fail "--check and --confirm-production are mutually exclusive"
fi
if [[ "${check_only}" == false && "${confirmed_production}" == false ]]; then
  fail "a mutating run requires the explicit --confirm-production flag"
fi
if [[ "${local_mode}" == true && "${ask_become_pass}" == true ]]; then
  fail "--local and --ask-become-pass are mutually exclusive"
fi

for command_name in awk base64 flock git hostname sha256sum ssh; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done
[[ -x "${ANSIBLE_PLAYBOOK}" && -x "${ANSIBLE_INVENTORY}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -r "${LOCK_HELPER}" && -r "${LOCKED_COMMAND_RUNNER}" ]] ||
  fail "operation-lock helpers are absent"
if [[ "${local_mode}" == true && "${EUID}" -ne 0 ]]; then
  [[ -x "${SUDO_BIN}" ]] ||
    fail "sudo is required for local execution"
fi
if [[ "${local_mode}" == true ]]; then
  [[ "$(id -u)" == "1001" && "$(id -g)" == "1001" ]] ||
    fail "--local must be launched by the reviewed admin UID/GID 1001"
fi

exec 8>"${PROJECT_DIR}/.git/dockerswarm-ansible-deploy.lock"
flock --nonblock 8 ||
  fail "another deployment wrapper is using this checkout"

worktree_status="$(
  git -C "${PROJECT_DIR}" status --porcelain --untracked-files=normal
)"
[[ -z "${worktree_status}" ]] ||
  fail "commit or discard every worktree change before targeting production"

source_revision="$(
  git -C "${PROJECT_DIR}" rev-parse --verify HEAD
)"
[[ "${source_revision}" =~ ^[a-f0-9]{40}$ ]] ||
  fail "cannot resolve the source commit"
contract_sha256="$(
  for contract_path in \
    "${PROJECT_DIR}/config/capacity.yml" \
    "${PROJECT_DIR}/config/host-security.yml" \
    "${PROJECT_DIR}/config/minecraft.yml" \
    "${PROJECT_DIR}/config/platform.yml" \
    "${PROJECT_DIR}/config/services.yml" \
    "${PROJECT_DIR}/stacks/observability/secrets.yml" \
    "${PROJECT_DIR}/stacks/workloads/secrets.yml"; do
    sha256sum "${contract_path}" | awk '{print $1}'
  done |
    sha256sum |
    awk '{print $1}'
)"
[[ "${contract_sha256}" =~ ^[a-f0-9]{64}$ ]] ||
  fail "cannot hash the versioned infrastructure contracts"

export ANSIBLE_CONFIG="${PROJECT_DIR}/ansible/ansible.cfg"
inventory_path="${PROJECT_DIR}/ansible/inventory/production/hosts.yml"
if [[ "${local_mode}" == true ]]; then
  inventory_path="${PROJECT_DIR}/ansible/inventory/local/hosts.yml"
fi
operation_id="$(
  "${PYTHON_BIN}" -c \
    'import secrets; print(secrets.token_hex(32))'
)"
[[ "${operation_id}" =~ ^[a-f0-9]{64}$ ]] ||
  fail "cannot generate a deployment operation ID"
controller_host="$(hostname --fqdn 2>/dev/null || hostname)"
operation_controller="$(id -un)@${controller_host}"
[[ "${operation_controller}" =~ ^[A-Za-z0-9_.@:-]{1,128}$ ]] ||
  fail "controller identity is not safe for operation metadata"
operation_mode=apply
if [[ "${check_only}" == true ]]; then
  operation_mode=check
fi

lock_arguments=(
  hold
  --owner-uid
  1001
  --owner-gid
  1001
  --operation-id
  "${operation_id}"
  --source-revision
  "${source_revision}"
  --contract-sha256
  "${contract_sha256}"
  --playbook
  "${playbook_name}"
  --profile
  "${deployment_profile}"
  --mode
  "${operation_mode}"
  --controller
  "${operation_controller}"
)

if [[ "${local_mode}" == true ]]; then
  coproc ANSIBLE_LOCK_HOLDER {
    "${PYTHON_BIN}" "${LOCK_HELPER}" "${lock_arguments[@]}"
  }
else
  inventory_json="$(
    "${ANSIBLE_INVENTORY}" \
      --inventory "${inventory_path}" \
      --list
  )"
  mapfile -t lock_target < <(
    "${PYTHON_BIN}" -c '
import json
import sys

inventory = json.load(sys.stdin)
hosts = inventory.get("swarm_managers", {}).get("hosts", [])
if len(hosts) != 1:
    raise SystemExit("production inventory must contain exactly one manager")
variables = inventory.get("_meta", {}).get("hostvars", {}).get(hosts[0], {})
host = variables.get("ansible_host")
user = variables.get("ansible_user")
if not isinstance(host, str) or not isinstance(user, str):
    raise SystemExit("manager SSH identity is incomplete")
print(host)
print(user)
' <<<"${inventory_json}"
  )
  [[ "${#lock_target[@]}" -eq 2 ]] ||
    fail "cannot resolve the sole production manager"
  [[ "${lock_target[1]}" == "admin" ]] ||
    fail "the production lock holder must use the reviewed admin identity"
  lock_helper_payload="$(base64 --wrap=0 "${LOCK_HELPER}")"
  [[ "${lock_helper_payload}" =~ ^[A-Za-z0-9+/=]+$ ]] ||
    fail "cannot encode the operation-lock helper"
  remote_python=(
    /usr/bin/python3
    -c
    "import base64;exec(compile(base64.b64decode('${lock_helper_payload}'),'<dockerswarm-operation-lock>','exec'))"
    "${lock_arguments[@]}"
  )
  printf -v remote_command '%q ' "${remote_python[@]}"
  coproc ANSIBLE_LOCK_HOLDER {
    /usr/bin/ssh \
      -T \
      -o BatchMode=yes \
      -o ControlMaster=no \
      -o StrictHostKeyChecking=yes \
      -o ServerAliveInterval=15 \
      -o ServerAliveCountMax=3 \
      "${lock_target[1]}@${lock_target[0]}" \
      "${remote_command}"
  }
fi
lock_holder_pid="${ANSIBLE_LOCK_HOLDER_PID}"
lock_holder_read_fd="${ANSIBLE_LOCK_HOLDER[0]}"
lock_holder_write_fd="${ANSIBLE_LOCK_HOLDER[1]}"
lock_handshake=""
IFS= read -r -t 20 lock_handshake <&"${lock_holder_read_fd}" ||
  fail "the host-global operation lock was not acquired"
[[ "${lock_handshake}" == "LOCKED:${operation_id}" ]] ||
  fail "operation-lock handshake is invalid"
kill -0 "${lock_holder_pid}" 2>/dev/null ||
  fail "operation-lock holder exited after its handshake"

ansible_args=(
  --inventory
  "${inventory_path}"
  "${PROJECT_DIR}/ansible/playbooks/${playbook_name}.yml"
  --extra-vars
  "deployment_metadata_source_revision=${source_revision}"
  --extra-vars
  "deployment_metadata_contract_sha256=${contract_sha256}"
  --extra-vars
  "deployment_metadata_profile=${deployment_profile}"
  --extra-vars
  "deployment_metadata_playbook=${playbook_name}"
  --extra-vars
  "operation_lock_guard_operation_id=${operation_id}"
  --extra-vars
  "operation_lock_guard_controller=${operation_controller}"
  --extra-vars
  "operation_lock_guard_mode=${operation_mode}"
)

if [[ "${deployment_profile}" == "acme-staging" ]]; then
  ansible_args+=(
    --extra-vars
    "edge_traefik_acme_ca_server=https://acme-staging-v02.api.letsencrypt.org/directory"
    --extra-vars
    "edge_traefik_acme_storage_filename=acme-staging.json"
  )
fi
if [[ "${check_only}" == true ]]; then
  ansible_args+=(--check --diff)
fi
if [[ "${ask_become_pass}" == true ]]; then
  ansible_args+=(--ask-become-pass)
fi
if [[ "${local_mode}" == true ]]; then
  ansible_args+=(
    --extra-vars
    "ansible_become=false"
  )
fi

printf 'Ansible source revision: %s\n' "${source_revision}"
printf 'Deployment profile: %s\n' "${deployment_profile}"
printf 'Deployment mode: %s\n' "${operation_mode}"
printf 'Deployment operation ID: %s\n' "${operation_id}"
ansible_command=("${ANSIBLE_PLAYBOOK}" "${ansible_args[@]}")
if [[ "${local_mode}" == true ]]; then
  printf 'Execution target: local host\n'
  if [[ "${EUID}" -ne 0 ]]; then
    printf 'Elevating the local process supervisor and Ansible with sudo.\n'
  fi
else
  printf 'Execution target: production inventory over SSH\n'
fi

locked_runner_command=(
  "${PYTHON_BIN}"
  "${LOCKED_COMMAND_RUNNER}"
  --watch-pid
  "${lock_holder_pid}"
  --
  "${ansible_command[@]}"
)
if [[ "${local_mode}" == true && "${EUID}" -ne 0 ]]; then
  locked_runner_command=(
    "${SUDO_BIN}"
    --preserve-env=ANSIBLE_CONFIG
    --
    "${locked_runner_command[@]}"
  )
fi

set +e
"${locked_runner_command[@]}"
ansible_status=$?
set -e
if [[ "${ansible_status}" -ne 0 ]]; then
  fail "Ansible failed; operation marker retained for reviewed recovery"
fi
kill -0 "${lock_holder_pid}" 2>/dev/null ||
  fail "operation-lock holder died before source revalidation"

[[ "$(git -C "${PROJECT_DIR}" rev-parse --verify HEAD)" == "${source_revision}" ]] ||
  fail "source HEAD changed during deployment; operation marker retained"
post_worktree_status="$(
  git -C "${PROJECT_DIR}" status --porcelain --untracked-files=normal
)"
[[ -z "${post_worktree_status}" ]] ||
  fail "source worktree changed during deployment; operation marker retained"

printf 'RELEASE:%s\n' "${operation_id}" >&"${lock_holder_write_fd}"
release_handshake=""
IFS= read -r -t 20 release_handshake <&"${lock_holder_read_fd}" ||
  fail "operation-lock release was not acknowledged"
[[ "${release_handshake}" == "RELEASED:${operation_id}" ]] ||
  fail "operation-lock release acknowledgement is invalid"
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
  fail "operation-lock holder failed during release"
trap - EXIT INT TERM HUP
printf 'Deployment operation %s completed and released cleanly.\n' \
  "${operation_id}"
