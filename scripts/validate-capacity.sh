#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly VENV_DIR="${PROJECT_DIR}/.venv"

reuse_rendered=false
verify_host=false

usage() {
  cat <<'EOF'
Usage: validate-capacity.sh [--reuse-rendered] [--verify-host]

Without --reuse-rendered, render all three stacks before validating them.
--verify-host also compares local /proc facts with the reviewed capacity floor.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while (( $# > 0 )); do
  case "$1" in
    --reuse-rendered)
      reuse_rendered=true
      shift
      ;;
    --verify-host)
      verify_host=true
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

[[ -x "${VENV_DIR}/bin/ansible-playbook" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -x "${VENV_DIR}/bin/python" ]] ||
  fail "the locked Python environment is absent"
command -v docker >/dev/null ||
  fail "docker is required"

cd "${PROJECT_DIR}"
export ANSIBLE_CONFIG="${PROJECT_DIR}/ansible/ansible.cfg"

if [[ "${reuse_rendered}" == false ]]; then
  for stack_id in edge workloads observability; do
    "${VENV_DIR}/bin/ansible-playbook" \
      --inventory ansible/inventory/local/hosts.yml \
      "ansible/playbooks/render-${stack_id}.yml" >/dev/null
  done
fi

for stack_id in edge workloads observability; do
  stack_path="${PROJECT_DIR}/.build/${stack_id}/stack.yml"
  [[ -f "${stack_path}" ]] ||
    fail "rendered stack is absent: ${stack_path}"
  docker stack config --compose-file "${stack_path}" >/dev/null
done

validator_arguments=()
if [[ "${verify_host}" == true ]]; then
  validator_arguments+=(--verify-host)
fi
"${VENV_DIR}/bin/python" \
  scripts/validate-capacity.py \
  "${validator_arguments[@]}"

"${VENV_DIR}/bin/python" -m unittest \
  discover \
  --start-directory tests \
  --pattern 'test_capacity_contract.py'

printf 'Capacity render, host budget contract and negative tests passed.\n'
