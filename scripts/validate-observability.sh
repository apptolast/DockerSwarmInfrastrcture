#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly VENV_DIR="${PROJECT_DIR}/.venv"
readonly BUILD_DIR="${PROJECT_DIR}/.build/observability"
readonly SCRIPT_PATH="${SCRIPT_DIR}/${BASH_SOURCE[0]##*/}"

# shellcheck source=scripts/host-global-docker-validation-lock.sh
source "${SCRIPT_DIR}/host-global-docker-validation-lock.sh"

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
[[ -x /usr/bin/systemd-analyze ]] ||
  fail "systemd-analyze is required"
[[ -x /usr/sbin/visudo ]] ||
  fail "visudo is required"

if ! docker info >/dev/null 2>&1; then
  if [[ "${OBSERVABILITY_DOCKER_REEXEC:-0}" != 1 ]] &&
    getent group docker |
      awk -F: -v user_name="$(id -un)" '
        $1 == "docker" && ("," $4 ",") ~ ("," user_name ",") { found = 1 }
        END { exit !found }
      '; then
    printf -v quoted_script '%q' "$0"
    exec sg docker -c \
      "OBSERVABILITY_DOCKER_REEXEC=1 ${quoted_script}"
  fi
  fail "the current identity cannot access the Docker daemon"
fi

ensure_docker_validation_lock \
  observability-docker-validation \
  "${SCRIPT_PATH}" \
  "$@"

cd "${PROJECT_DIR}"
export ANSIBLE_CONFIG="${PROJECT_DIR}/ansible/ansible.cfg"

"${VENV_DIR}/bin/ansible-playbook" \
  --inventory ansible/inventory/local/hosts.yml \
  ansible/playbooks/render-observability.yml >/dev/null

docker stack config \
  --compose-file "${BUILD_DIR}/stack.yml" >/dev/null

"${VENV_DIR}/bin/python" scripts/validate-observability.py \
  --stack "${BUILD_DIR}/stack.yml" \
  --services "${PROJECT_DIR}/config/services.yml" \
  --secrets "${PROJECT_DIR}/stacks/observability/secrets.yml" \
  --platform "${PROJECT_DIR}/config/platform.yml" \
  --config-dir "${BUILD_DIR}/config"

/usr/sbin/visudo \
  --check \
  --file="${BUILD_DIR}/host/observability-forward.sudoers"
/usr/bin/systemd-analyze \
  --recursive-errors=no \
  verify \
  "${BUILD_DIR}/host/dockerswarm-docker-readonly-proxy.service"

mapfile -t observability_images < <(
  "${VENV_DIR}/bin/python" - <<'PY'
from pathlib import Path
import yaml

catalog = yaml.safe_load(Path("config/services.yml").read_text())
for item in catalog["internal_platform"]["observability"]["components"]:
    print(f"{item['id']}\t{item['image']}")
PY
)
declare -A image_by_component=()
for entry in "${observability_images[@]}"; do
  component="${entry%%$'\t'*}"
  image="${entry#*$'\t'}"
  image_by_component["${component}"]="${image}"
done

docker run --rm \
  --volume "${BUILD_DIR}/config/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
  --volume \
    "${BUILD_DIR}/config/prometheus-alerts.yml:/etc/prometheus/rules/platform.yml:ro" \
  --entrypoint /bin/promtool \
  "${image_by_component[prometheus]}" \
  check config /etc/prometheus/prometheus.yml

docker run --rm \
  --volume \
    "${BUILD_DIR}/config/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro" \
  --entrypoint /bin/amtool \
  "${image_by_component[alertmanager]}" \
  check-config /etc/alertmanager/alertmanager.yml

docker run --rm \
  --volume \
    "${BUILD_DIR}/config/blackbox.yml:/etc/blackbox_exporter/config.yml:ro" \
  --entrypoint /bin/blackbox_exporter \
  "${image_by_component[blackbox-exporter]}" \
  --config.file=/etc/blackbox_exporter/config.yml \
  --config.check

docker run --rm \
  --volume "${BUILD_DIR}/config/loki.yml:/etc/loki/config.yml:ro" \
  --entrypoint /usr/bin/loki \
  "${image_by_component[loki]}" \
  -config.file=/etc/loki/config.yml \
  -verify-config=true

docker run --rm \
  --volume "${BUILD_DIR}/config/alloy.alloy:/etc/alloy/config.alloy:ro" \
  --entrypoint /bin/alloy \
  "${image_by_component[alloy]}" \
  validate /etc/alloy/config.alloy

"${SCRIPT_DIR}/smoke-observability-runtime.sh" "${BUILD_DIR}"

"${VENV_DIR}/bin/python" -m unittest \
  discover \
  --start-directory tests \
  --pattern 'test_observability_contract.py'

printf 'Observability render, schemas, configs and negative tests passed.\n'
