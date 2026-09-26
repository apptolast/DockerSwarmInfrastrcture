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
# Traefik v3.7.13 (the v3 channel head since 2026-09) deprecates the option
# the four entry points use; v3.7.9 does not warn. Moving to
# aliasHeadersStrategy widens what is rejected, so it is a separate change.
readonly KNOWN_DEPRECATION="The underscoreHeadersStrategy option is deprecated"
readonly ENTRYPOINT_COUNT=4

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
# A digest hold, or exactly the reviewed major channel from
# config/image-channels.yml (validate-image-channels.py binds the render to
# that entry). Validating the channel runs the head the deploy will resolve.
traefik_image_re='^((docker\.io/library/)?traefik(:v3)?@sha256:[a-f0-9]{64}|docker\.io/library/traefik:v3)$'
[[ "${traefik_image}" =~ ${traefik_image_re} ]] ||
  fail "rendered Traefik image is neither digest-pinned nor the v3 channel"

validation_prefix="edge-config-validation-$$"
validation_dir="${PROJECT_DIR}/.build/edge-validation"
cleanup() {
  docker container rm --force \
    "${validation_prefix}-static" "${validation_prefix}-render" \
    >/dev/null 2>&1 || true
  rm -rf -- "${validation_dir}"
}
trap cleanup EXIT

# Boot the pinned Traefik as the edge runs it, with a static and a dynamic
# file and any extra `docker run` options, wait for its healthcheck and
# reject every warning-or-higher entry except the known v3.7 notices.
boot_traefik() {
  local name=$1
  local static_file=$2
  local dynamic_file=$3
  shift 3

  docker run \
    --detach \
    --name "${name}" \
    --read-only \
    --user 65532:65532 \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777 \
    --tmpfs /data:rw,noexec,nosuid,nodev,size=16m,uid=65532,gid=65532,mode=0700 \
    --tmpfs /var/log/traefik:rw,noexec,nosuid,nodev,size=16m,uid=65532,gid=65532,mode=0700 \
    --volume "${static_file}:/etc/traefik.yml:ro" \
    --volume "${dynamic_file}:/etc/traefik-dynamic.yml:ro" \
    "$@" \
    "${traefik_image}" \
    --configFile=/etc/traefik.yml >/dev/null

  local healthy=false
  local running
  for _ in {1..20}; do
    if docker exec \
      "${name}" \
      traefik healthcheck \
      --configFile=/etc/traefik.yml >/dev/null 2>&1; then
      healthy=true
      break
    fi

    running="$(docker inspect --format '{{.State.Running}}' "${name}")"
    if [[ "${running}" != true ]]; then
      docker logs "${name}" >&2
      fail "Traefik stopped during configuration validation"
    fi
    sleep 1
  done
  [[ "${healthy}" == true ]] || fail "Traefik healthcheck did not converge"

  local runtime_identity
  runtime_identity="$(
    docker inspect \
      --format \
      '{{.Config.User}}|{{.HostConfig.ReadonlyRootfs}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}' \
      "${name}"
  )"
  [[ "${runtime_identity}" == \
    '65532:65532|true|["ALL"]|["no-new-privileges:true"]' ]] ||
    fail "Traefik validation container lost a security invariant"

  local traefik_logs problem_logs unknown_logs
  traefik_logs="$(docker logs "${name}" 2>&1)"
  problem_logs="$(
    grep -Ei '"level":"(warn|error|fatal|panic)"' <<<"${traefik_logs}" ||
      true
  )"
  unknown_logs="$(
    grep -Fv -e "${KNOWN_WARNING}" -e "${KNOWN_DEPRECATION}" \
      <<<"${problem_logs}" ||
      true
  )"
  if [[ -n "${unknown_logs}" ]]; then
    printf '%s\n' "${unknown_logs}" >&2
    fail "Traefik emitted an unexpected warning-or-higher entry"
  fi
  local known_warning_count deprecation_count
  known_warning_count="$(
    grep -Fc "${KNOWN_WARNING}" <<<"${problem_logs}" ||
      true
  )"
  [[ "${known_warning_count}" -eq 1 ]] ||
    fail "the unconditional pinned Traefik warning did not occur exactly once"
  deprecation_count="$(
    grep -Fc "${KNOWN_DEPRECATION}" <<<"${problem_logs}" ||
      true
  )"
  [[ "${deprecation_count}" -eq 0 || "${deprecation_count}" -eq "${ENTRYPOINT_COUNT}" ]] ||
    fail "the underscoreHeadersStrategy deprecation did not occur once per entry point"

  docker container rm --force "${name}" >/dev/null
}

# The rendered static configuration, ACME resolver included, with a minimal
# dynamic file.
boot_traefik \
  "${validation_prefix}-static" \
  "${PROJECT_DIR}/.build/edge/static.yml" \
  "${PROJECT_DIR}/tests/fixtures/traefik-empty-dynamic.yml" \
  --env CF_DNS_API_TOKEN=validation-placeholder

# The whole rendered dynamic configuration, so an option Traefik rejects or
# drops fails here and not in the production apply: a serversTransport
# whose TLS configuration falls back to the default one logs `Could not
# configure HTTP Transport` at ERROR, an unknown key stops the file provider
# (and with it /ping), and a middleware that cannot be built disables its
# router with an ERROR. Only the ACME resolver and the backend health checks
# are removed, and every file under /run/secrets is a throwaway stand-in of
# its kind (scripts/prepare-traefik-validation.py). /ping is served by the
# render's own edge-ping-internal router.
rm -rf -- "${validation_dir}"
"${PYTHON_BIN}" "${SCRIPT_DIR}/prepare-traefik-validation.py" \
  "${PROJECT_DIR}/.build/edge" "${validation_dir}" >/dev/null
boot_traefik \
  "${validation_prefix}-render" \
  "${validation_dir}/static.yml" \
  "${validation_dir}/dynamic.yml" \
  --volume "${validation_dir}/secrets:/run/secrets:ro"

cleanup
trap - EXIT

printf '%s\n' \
  "Traefik config, healthcheck and hardening validation passed." \
  "The whole rendered dynamic configuration loaded." \
  "No warning-or-higher entries beyond the known v3.7 notices."
