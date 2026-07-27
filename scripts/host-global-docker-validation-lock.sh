#!/usr/bin/env bash

# This file is a sourced library. Docker-based validation is a host mutation
# when it targets an active Swarm node, even when every container is ephemeral.

ensure_docker_validation_lock() {
  local operation="$1"
  local script_path="$2"
  shift 2

  # SCRIPT_DIR and fail are supplied by the reviewed caller before sourcing.
  # shellcheck disable=SC2154
  local helper="${SCRIPT_DIR}/host_global_operation_lock.py"
  [[ -f "${helper}" && ! -L "${helper}" ]] ||
    fail "host-global operation-lock helper is absent or unsafe"

  if [[ -n "${DOCKERSWARM_IAC_LOCK_SCOPE:-}" ]]; then
    /usr/bin/python3 "${helper}" prove --operation "${operation}" >/dev/null ||
      fail "host-global operation-lock proof failed"
    return
  fi

  local swarm_state
  swarm_state="$(/usr/bin/docker info --format '{{.Swarm.LocalNodeState}}')" ||
    fail "cannot classify the Docker daemon"
  case "${swarm_state}" in
    inactive)
      return
      ;;
    active|pending|locked|error)
      ;;
    *)
      fail "Docker returned an unknown local Swarm state"
      ;;
  esac

  exec /usr/bin/python3 "${helper}" \
    run --operation "${operation}" -- \
    "${script_path}" "$@"
}
