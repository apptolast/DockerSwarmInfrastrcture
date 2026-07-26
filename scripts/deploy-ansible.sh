#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly ANSIBLE_PLAYBOOK="${PROJECT_DIR}/.venv/bin/ansible-playbook"

playbook_name=""
deployment_profile="production"
check_only=false
confirmed_production=false
ask_become_pass=false

usage() {
  cat <<'EOF'
Usage:
  deploy-ansible.sh --playbook NAME [--profile PROFILE]
      [--ask-become-pass] [--check | --confirm-production]

NAME is one of: platform, host-baseline, edge, site.
PROFILE is production (default) or acme-staging. The staging profile is valid
only with the edge playbook and uses a separate ACME storage file.

The wrapper refuses a dirty worktree and records the exact commit and platform
contract checksum on the host. A real run requires --confirm-production.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

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
  platform|host-baseline|edge|site)
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

for command_name in awk git sha256sum; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done
[[ -x "${ANSIBLE_PLAYBOOK}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"

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
  sha256sum "${PROJECT_DIR}/config/platform.yml" |
    awk '{print $1}'
)"
[[ "${contract_sha256}" =~ ^[a-f0-9]{64}$ ]] ||
  fail "cannot hash config/platform.yml"

export ANSIBLE_CONFIG="${PROJECT_DIR}/ansible/ansible.cfg"
ansible_args=(
  --inventory
  "${PROJECT_DIR}/ansible/inventory/production/hosts.yml"
  "${PROJECT_DIR}/ansible/playbooks/${playbook_name}.yml"
  --extra-vars
  "deployment_metadata_source_revision=${source_revision}"
  --extra-vars
  "deployment_metadata_contract_sha256=${contract_sha256}"
  --extra-vars
  "deployment_metadata_profile=${deployment_profile}"
  --extra-vars
  "deployment_metadata_playbook=${playbook_name}"
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

printf 'Ansible source revision: %s\n' "${source_revision}"
printf 'Deployment profile: %s\n' "${deployment_profile}"
exec "${ANSIBLE_PLAYBOOK}" "${ansible_args[@]}"
