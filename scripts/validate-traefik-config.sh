#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
readonly ANSIBLE_PLAYBOOK="${PROJECT_DIR}/.venv/bin/ansible-playbook"
readonly SCRIPT_PATH="${SCRIPT_DIR}/${BASH_SOURCE[0]##*/}"
# v3.7.9 emits this unconditionally before it evaluates effective config:
# https://github.com/traefik/traefik/blob/d0bd2ec198533d760c1abfc74b98033d6d92d039/cmd/traefik/traefik.go#L100-L103
readonly KNOWN_WARNING="Traefik can reject some encoded characters in the request path"

# shellcheck source=scripts/host-global-docker-validation-lock.sh
source "${SCRIPT_DIR}/host-global-docker-validation-lock.sh"

cd "${PROJECT_DIR}"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

if ! docker info >/dev/null 2>&1; then
  if [[ "${EDGE_DOCKER_REEXEC:-0}" != 1 ]] &&
    getent group docker |
      awk -F: -v user_name="$(id -un)" '
        $1 == "docker" && ("," $4 ",") ~ ("," user_name ",") { found = 1 }
        END { exit !found }
      '; then
    printf -v quoted_script '%q' "$0"
    exec sg docker -c "EDGE_DOCKER_REEXEC=1 ${quoted_script}"
  fi
  fail "the current identity cannot access the Docker daemon"
fi

ensure_docker_validation_lock \
  traefik-docker-validation \
  "${SCRIPT_PATH}" \
  "$@"

[[ -x "${PYTHON_BIN}" ]] || fail "run scripts/bootstrap-tooling.sh first"
[[ -x "${ANSIBLE_PLAYBOOK}" ]] || fail "run scripts/bootstrap-tooling.sh first"

export ANSIBLE_CONFIG="${PROJECT_DIR}/ansible/ansible.cfg"
export PATH="${PROJECT_DIR}/.venv/bin:${PATH}"

"${ANSIBLE_PLAYBOOK}" \
  --inventory "${PROJECT_DIR}/ansible/inventory/local/hosts.yml" \
  "${PROJECT_DIR}/ansible/playbooks/render-edge.yml" >/dev/null

docker stack config \
  --compose-file "${PROJECT_DIR}/.build/edge/stack.yml" >/dev/null

traefik_image="$(
  "${PYTHON_BIN}" -c '
import pathlib
import yaml
root = pathlib.Path.cwd()
stack = yaml.safe_load((root / ".build/edge/stack.yml").read_text())
print(stack["services"]["traefik"]["image"])
'
)"
[[ "${traefik_image}" =~ ^traefik@sha256:[a-f0-9]{64}$ ]] ||
  fail "rendered Traefik image is not digest-pinned"

validation_name="edge-config-validation-$$"
cleanup() {
  docker container rm --force "${validation_name}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run \
  --detach \
  --name "${validation_name}" \
  --read-only \
  --user 65532:65532 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777 \
  --tmpfs /data:rw,noexec,nosuid,nodev,size=16m,uid=65532,gid=65532,mode=0700 \
  --env CF_DNS_API_TOKEN=validation-placeholder \
  --volume "${PROJECT_DIR}/.build/edge/static.yml:/etc/traefik.yml:ro" \
  --volume \
    "${PROJECT_DIR}/tests/fixtures/traefik-empty-dynamic.yml:/etc/traefik-dynamic.yml:ro" \
  "${traefik_image}" \
  --configFile=/etc/traefik.yml >/dev/null

healthy=false
for _ in {1..20}; do
  if docker exec \
    "${validation_name}" \
    traefik healthcheck \
    --configFile=/etc/traefik.yml >/dev/null 2>&1; then
    healthy=true
    break
  fi

  running="$(docker inspect --format '{{.State.Running}}' "${validation_name}")"
  if [[ "${running}" != true ]]; then
    docker logs "${validation_name}" >&2
    fail "Traefik stopped during configuration validation"
  fi
  sleep 1
done
[[ "${healthy}" == true ]] || fail "Traefik healthcheck did not converge"

runtime_identity="$(
  docker inspect \
    --format \
    '{{.Config.User}}|{{.HostConfig.ReadonlyRootfs}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}' \
    "${validation_name}"
)"
[[ "${runtime_identity}" == \
  '65532:65532|true|["ALL"]|["no-new-privileges:true"]' ]] ||
  fail "Traefik validation container lost a security invariant"

traefik_logs="$(docker logs "${validation_name}" 2>&1)"
problem_logs="$(
  grep -Ei '"level":"(warn|error|fatal|panic)"' <<<"${traefik_logs}" ||
    true
)"
unknown_logs="$(
  grep -Fv "${KNOWN_WARNING}" <<<"${problem_logs}" ||
    true
)"
if [[ -n "${unknown_logs}" ]]; then
  printf '%s\n' "${unknown_logs}" >&2
  fail "Traefik emitted an unexpected warning-or-higher entry"
fi
known_warning_count="$(
  grep -Fc "${KNOWN_WARNING}" <<<"${problem_logs}" ||
    true
)"
[[ "${known_warning_count}" -eq 1 ]] ||
  fail "the unconditional pinned Traefik warning did not occur exactly once"

cleanup
trap - EXIT

printf '%s\n' \
  "Traefik config, healthcheck and hardening validation passed." \
  "No warning-or-higher entries beyond the one unconditional v3.7.9 notice."
