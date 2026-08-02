#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly VENV_DIR="${PROJECT_DIR}/.venv"
readonly TERRAFORM_BIN="${PROJECT_DIR}/.tools/terraform"
readonly SHELLCHECK_IMAGE="koalaman/shellcheck@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d"
readonly SCRIPT_PATH="${SCRIPT_DIR}/${BASH_SOURCE[0]##*/}"

# shellcheck source=scripts/host-global-docker-validation-lock.sh
source "${SCRIPT_DIR}/host-global-docker-validation-lock.sh"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ -x "${VENV_DIR}/bin/ansible-playbook" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -x "${TERRAFORM_BIN}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"

cd "${PROJECT_DIR}"

if ! docker info >/dev/null 2>&1; then
  if [[ "${IAC_DOCKER_REEXEC:-0}" != 1 ]] &&
    getent group docker |
      awk -F: -v user_name="$(id -un)" '
        $1 == "docker" && ("," $4 ",") ~ ("," user_name ",") { found = 1 }
        END { exit !found }
      '; then
    printf -v quoted_script '%q' "$0"
    exec sg docker -c "IAC_DOCKER_REEXEC=1 ${quoted_script}"
  fi
  fail "the current identity cannot access the Docker daemon"
fi

ensure_docker_validation_lock iac-docker-validation "${SCRIPT_PATH}" "$@"

export ANSIBLE_CONFIG="${PROJECT_DIR}/ansible/ansible.cfg"
export PATH="${VENV_DIR}/bin:${PATH}"
export TF_IN_AUTOMATION=1
export TF_INPUT=0
export TF_PLUGIN_CACHE_DIR="${PROJECT_DIR}/.terraform.d/plugin-cache"

install --directory --mode=0750 "${TF_PLUGIN_CACHE_DIR}"

expected_python_packages="$(
  sed -nE \
    's/^([A-Za-z0-9_.-]+==[^[:space:]]+)[[:space:]]+\\$/\1/p' \
    "${PROJECT_DIR}/ansible/requirements-dev.txt" |
    awk -F== '{
      name = tolower($1)
      gsub(/[._]+/, "-", name)
      print name "==" $2
    }' |
    LC_ALL=C sort
)"
installed_python_packages="$(
  "${VENV_DIR}/bin/python" -m pip freeze |
    grep -Ev '^(pip|setuptools|wheel)==' |
    awk -F== '{
      name = tolower($1)
      gsub(/[._]+/, "-", name)
      print name "==" $2
    }' |
    LC_ALL=C sort
)"
[[ "${installed_python_packages}" == "${expected_python_packages}" ]] ||
  fail "the Python environment differs from requirements-dev.txt"

while IFS= read -r direct_python_package; do
  grep -Fxi "${direct_python_package}" \
    <<<"${expected_python_packages}" >/dev/null ||
    fail "${direct_python_package} is absent from the generated Python lock"
done < <(
  sed -nE \
    's/^([A-Za-z0-9_.-]+==[^[:space:]#]+)[[:space:]]*$/\1/p' \
    "${PROJECT_DIR}/ansible/requirements-dev.in"
)

collection_inventory="$(
  ansible-galaxy collection list --format=json
)"
"${VENV_DIR}/bin/python" -c '
import json
import sys
collections = json.load(sys.stdin)
flattened = {}
for location in collections.values():
    flattened.update(
        {name: value["version"] for name, value in location.items()}
    )
expected = {
    "community.docker": "5.2.1",
    "community.library_inventory_filtering_v1": "1.1.5",
}
if any(flattened.get(name) != version for name, version in expected.items()):
    raise SystemExit("locked Ansible collection versions are not installed")
' <<<"${collection_inventory}"

jq --exit-status '
  . == {
    "log-driver": "local",
    "log-opts": {
      "max-file": "5",
      "max-size": "20m"
    },
    "no-new-privileges": true,
    "userland-proxy": false
  }
' config/daemon.json >/dev/null
dockerd --validate --config-file=config/daemon.json

yamllint \
  --strict \
  .github \
  ansible \
  config/capacity.yml \
  config/host-security.yml \
  config/minecraft.yml \
  config/platform.yml \
  config/services.yml \
  migration/compose \
  stacks \
  tests/fixtures
ansible-lint ansible

mapfile -t playbooks < <(
  find ansible/playbooks -maxdepth 1 -type f -name '*.yml' | sort
)
for playbook in "${playbooks[@]}"; do
  ansible-playbook \
    --inventory ansible/inventory/local/hosts.yml \
    --syntax-check \
    "${playbook}"
done

ansible-playbook \
  --inventory ansible/inventory/local/hosts.yml \
  ansible/playbooks/render-platform-assets.yml >/dev/null
ansible-playbook \
  --inventory ansible/inventory/local/hosts.yml \
  ansible/playbooks/render-edge.yml >/dev/null

docker run \
  --rm \
  --volume "${PROJECT_DIR}/.build/platform:/workspace:ro" \
  "${SHELLCHECK_IMAGE}" \
  --severity=style \
  /workspace/dockerswarm-docker-firewall
"${VENV_DIR}/bin/python" scripts/validate-contract.py
"${VENV_DIR}/bin/python" scripts/validate-host-security.py
"${VENV_DIR}/bin/python" scripts/validate-ssh-policy.py
"${VENV_DIR}/bin/python" scripts/validate-services.py --self-test

mapfile -t python_scripts < <(
  find scripts migration/scripts -maxdepth 1 -type f -name '*.py' |
    sort
)
python_scripts+=("backup/backupctl.py")
PYTHONPYCACHEPREFIX="${PROJECT_DIR}/.build/pycache" \
  "${VENV_DIR}/bin/python" -m py_compile "${python_scripts[@]}"
scripts/validate-workloads.sh
scripts/validate-observability.sh
scripts/validate-capacity.sh --reuse-rendered
scripts/backup-self-test.sh
PYTHONPYCACHEPREFIX="${PROJECT_DIR}/.build/pycache" \
  "${VENV_DIR}/bin/python" -m unittest discover \
  -s tests \
  -v
PYTHONPYCACHEPREFIX="${PROJECT_DIR}/.build/pycache" \
  "${VENV_DIR}/bin/python" -m unittest discover \
  -s migration/tests \
  -v

docker compose --profile maintenance \
  -f migration/compose/restore.yml \
  config --quiet
docker compose --profile maintenance \
  -f migration/compose/restore.yml \
  -f migration/compose/restore-vectors-081.yml \
  config --quiet

terraform_tmp_dirs=()
cleanup() {
  for terraform_tmp_dir in "${terraform_tmp_dirs[@]}"; do
    [[ -d "${terraform_tmp_dir}" ]] || continue
    find "${terraform_tmp_dir}" -depth -delete
  done
}
trap cleanup EXIT

"${TERRAFORM_BIN}" fmt -check -recursive infra/terraform
for terraform_root in \
  infra/terraform/cloudflare/state-bootstrap \
  infra/terraform/cloudflare/apptolast-dns \
  infra/terraform/netcup/perimeter \
  infra/terraform/testing/r2-lock; do
  terraform_tmp_dir="$(mktemp -d)"
  terraform_tmp_dirs+=("${terraform_tmp_dir}")

  TF_DATA_DIR="${terraform_tmp_dir}" \
    "${TERRAFORM_BIN}" -chdir="${terraform_root}" init \
    -backend=false \
    -input=false \
    -lockfile=readonly >/dev/null
  TF_DATA_DIR="${terraform_tmp_dir}" \
    "${TERRAFORM_BIN}" -chdir="${terraform_root}" validate
  TF_DATA_DIR="${terraform_tmp_dir}" \
    "${TERRAFORM_BIN}" -chdir="${terraform_root}" test
done

mapfile -t shell_scripts < <(
  find scripts migration/scripts stacks/workloads/config \
    -maxdepth 1 -type f -name '*.sh' |
    sort
)
for shell_script in "${shell_scripts[@]}"; do
  bash -n "${shell_script}"
done

scripts/validate-traefik-config.sh
git diff --check

cleanup
trap - EXIT

printf 'Terraform, Ansible, contract and Traefik validation passed.\n'
