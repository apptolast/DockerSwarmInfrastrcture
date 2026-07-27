#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly VENV_DIR="${PROJECT_DIR}/.venv"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ -x "${VENV_DIR}/bin/ansible-playbook" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -x "${VENV_DIR}/bin/python" ]] ||
  fail "the locked Python environment is absent"
command -v docker >/dev/null ||
  fail "docker is required"

if ! docker info >/dev/null 2>&1; then
  if [[ "${WORKLOADS_DOCKER_REEXEC:-0}" != 1 ]] &&
    getent group docker |
      awk -F: -v user_name="$(id -un)" '
        $1 == "docker" && ("," $4 ",") ~ ("," user_name ",") { found = 1 }
        END { exit !found }
      '; then
    printf -v quoted_script '%q' "$0"
    exec sg docker -c "WORKLOADS_DOCKER_REEXEC=1 ${quoted_script}"
  fi
  fail "the current identity cannot access the Docker daemon"
fi

cd "${PROJECT_DIR}"
export ANSIBLE_CONFIG="${PROJECT_DIR}/ansible/ansible.cfg"

"${VENV_DIR}/bin/ansible-playbook" \
  --inventory ansible/inventory/local/hosts.yml \
  ansible/playbooks/render-workloads.yml >/dev/null

docker stack config \
  --compose-file "${PROJECT_DIR}/.build/workloads/stack.yml" >/dev/null

"${VENV_DIR}/bin/python" scripts/validate-workloads.py \
  --stack "${PROJECT_DIR}/.build/workloads/stack.yml" \
  --platform "${PROJECT_DIR}/config/platform.yml" \
  --services "${PROJECT_DIR}/config/services.yml" \
  --secrets "${PROJECT_DIR}/stacks/workloads/secrets.yml" \
  --config-dir "${PROJECT_DIR}/.build/workloads/config" \
  --runner-context "${PROJECT_DIR}/images/n8n-runners"

"${VENV_DIR}/bin/python" -m unittest \
  discover \
  --start-directory tests \
  --pattern 'test_workloads_contract.py'

"${VENV_DIR}/bin/python" -m unittest \
  discover \
  --start-directory migration/tests \
  --pattern 'test_repository_integration.py'

"${VENV_DIR}/bin/python" -m unittest \
  discover \
  --start-directory migration/tests \
  --pattern 'test_n8n_workflow_cutover.py'

printf 'Workloads render, Docker config and cross-repository checks passed.\n'
