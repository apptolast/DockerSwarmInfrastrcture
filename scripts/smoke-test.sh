#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly HOST_LOCK_HELPER="${SCRIPT_DIR}/host_global_operation_lock.py"
readonly SCRIPT_PATH="${SCRIPT_DIR}/${BASH_SOURCE[0]##*/}"
readonly IMAGE="alpine@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce"
readonly SERVICE_NAME="swarm-smoke-$RANDOM-$$"
readonly EXPECTED_OUTPUT="swarm-smoke-ok"

service_created=false
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
  [[ "${service_created}" == true ]] || return 0

  docker service rm "${SERVICE_NAME}" >/dev/null 2>&1 || true

  for _ in {1..30}; do
    if ! docker service inspect "${SERVICE_NAME}" >/dev/null 2>&1 &&
      [[ -z "$(
        docker ps \
          --all \
          --quiet \
          --filter "label=com.docker.swarm.service.name=${SERVICE_NAME}"
      )" ]]; then
      service_created=false
      return 0
    fi

    sleep 1
  done

  printf 'ERROR: smoke resources were not fully removed: %s\n' \
    "${SERVICE_NAME}" >&2
  return 1
}

on_exit() {
  exit_status=$?
  trap - EXIT

  if ! cleanup; then
    exit_status=1
  fi

  exit "${exit_status}"
}
trap on_exit EXIT

(( $# == 0 )) || fail "this command does not accept arguments"
ensure_host_global_lock swarm-smoke-test

[[ "$(docker info --format '{{.Swarm.LocalNodeState}}')" == "active" ]] ||
  fail "Swarm is not active"
[[ "$(docker info --format '{{.Swarm.ControlAvailable}}')" == "true" ]] ||
  fail "this node is not a manager"

node_id="$(docker info --format '{{.Swarm.NodeID}}')"
[[ -n "${node_id}" ]] || fail "the local Swarm node has no ID"

docker service create \
  --constraint "node.id==${node_id}" \
  --detach \
  --max-concurrent=1 \
  --mode=replicated-job \
  --name "${SERVICE_NAME}" \
  --no-resolve-image \
  --read-only \
  --replicas=1 \
  --restart-condition=none \
  "${IMAGE}" \
  /bin/sh -c "printf '%s\\n' '${EXPECTED_OUTPUT}'" >/dev/null
service_created=true

published_ports="$(
  docker service inspect \
    --format '{{json .Spec.EndpointSpec.Ports}}' \
    "${SERVICE_NAME}"
)"
[[ "${published_ports}" == "null" || "${published_ports}" == "[]" ]] ||
  fail "smoke service published ports: ${published_ports}"

task_id=""
for _ in {1..30}; do
  task_id="$(
    docker service ps \
      --no-trunc \
      --format '{{.ID}}' \
      "${SERVICE_NAME}" |
      head -n 1
  )"

  if [[ -n "${task_id}" ]]; then
    task_state="$(docker inspect --type=task --format '{{.Status.State}}' "${task_id}")"
    task_error="$(docker inspect --type=task --format '{{.Status.Err}}' "${task_id}")"

    [[ -z "${task_error}" ]] || fail "task error: ${task_error}"

    case "${task_state}" in
      complete)
        break
        ;;
      failed|orphaned|rejected|remove)
        fail "task reached terminal state: ${task_state}"
        ;;
    esac
  fi

  sleep 1
done

[[ -n "${task_id}" ]] || fail "Swarm did not create a task"
[[ "${task_state:-}" == "complete" ]] ||
  fail "task did not complete within 30 seconds"

exit_code="$(
  docker inspect \
    --type=task \
    --format '{{.Status.ContainerStatus.ExitCode}}' \
    "${task_id}"
)"
[[ "${exit_code}" == "0" ]] || fail "smoke task exit code: ${exit_code}"

container_id="$(
  docker inspect \
    --type=task \
    --format '{{.Status.ContainerStatus.ContainerID}}' \
    "${task_id}"
)"
[[ -n "${container_id}" ]] || fail "task has no container ID"

logging_driver="$(
  docker inspect --format '{{.HostConfig.LogConfig.Type}}' "${container_id}"
)"
[[ "${logging_driver}" == "local" ]] ||
  fail "smoke container used logging driver: ${logging_driver}"

container_output="$(docker logs "${container_id}")"
[[ "${container_output}" == "${EXPECTED_OUTPUT}" ]] ||
  fail "unexpected smoke output: ${container_output}"

cleanup
trap - EXIT

printf 'Swarm smoke test passed with logging driver local.\n'
