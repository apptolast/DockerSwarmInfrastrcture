#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
readonly REPOSITORY_ROOT
readonly SAFETY_TOOL="${SCRIPT_DIR}/backup_safety.py"
readonly HOST_LOCK_HELPER="${REPOSITORY_ROOT}/scripts/host_global_operation_lock.py"
readonly SCRIPT_PATH="${SCRIPT_DIR}/${BASH_SOURCE[0]##*/}"

services_root="${1:-/srv/dockerswarm/services}"
edge_state_root="${2:-/srv/dockerswarm}"
traefik_uid="${TRAEFIK_RUNTIME_UID:-65532}"
traefik_gid="${TRAEFIK_RUNTIME_GID:-65532}"
recovery_root="${services_root}/recovery"
source_file="${recovery_root}/edge/traefik-acme.json"
target_directory="${edge_state_root}/traefik"
target_file="${target_directory}/acme.json"
temporary=""
original_args=("$@")

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

ensure_host_global_lock() {
  local operation=$1
  [[ -f "${HOST_LOCK_HELPER}" && ! -L "${HOST_LOCK_HELPER}" ]] ||
    fail "Falta el helper seguro del mutex global"
  if [[ -n "${DOCKERSWARM_IAC_LOCK_SCOPE:-}" ]]; then
    /usr/bin/python3 "${HOST_LOCK_HELPER}" \
      prove --operation "${operation}" >/dev/null ||
      fail "La prueba del mutex global no es válida"
    return
  fi
  exec /usr/bin/python3 "${HOST_LOCK_HELPER}" \
    run --operation "${operation}" -- \
    "${SCRIPT_PATH}" "${original_args[@]}"
}

cleanup() {
  local status=$?
  if [[ -n "${temporary}" && -f "${temporary}" ]]; then
    rm -f -- "${temporary}"
  fi
  exit "${status}"
}
trap cleanup EXIT

[[ "${EUID}" -eq 0 ]] ||
  fail "La instalación de ACME requiere ejecución como root"
for command_name in awk chown install mktemp mv python3 rm sha256sum stat; do
  command -v "${command_name}" >/dev/null 2>&1 ||
    fail "Falta el comando requerido: ${command_name}"
done
[[ "${services_root}" == /* && "${services_root}" != "/" ]] ||
  fail "SERVICES_ROOT debe ser una ruta absoluta distinta de /"
[[ "${edge_state_root}" == /* && "${edge_state_root}" != "/" ]] ||
  fail "EDGE_STATE_ROOT debe ser una ruta absoluta distinta de /"
[[ "${traefik_uid}" =~ ^[0-9]+$ && "${traefik_gid}" =~ ^[0-9]+$ ]] ||
  fail "Los UID/GID de Traefik deben ser numéricos"
[[ -d "${services_root}" && ! -L "${services_root}" ]] ||
  fail "SERVICES_ROOT ausente o inseguro"
[[ -d "${edge_state_root}" && ! -L "${edge_state_root}" ]] ||
  fail "EDGE_STATE_ROOT ausente o inseguro"
services_root="$(cd -- "${services_root}" && pwd -P)"
edge_state_root="$(cd -- "${edge_state_root}" && pwd -P)"
recovery_root="${services_root}/recovery"
source_file="${recovery_root}/edge/traefik-acme.json"
target_directory="${edge_state_root}/traefik"
target_file="${target_directory}/acme.json"
[[ -f "${SAFETY_TOOL}" && ! -L "${SAFETY_TOOL}" ]] ||
  fail "Falta la utilidad de verificación"

if [[ "${services_root}" == "/srv/dockerswarm/services" || \
  "${edge_state_root}" == "/srv/dockerswarm" ]]; then
  ensure_host_global_lock migration-install-traefik-acme
fi

python3 "${SAFETY_TOOL}" \
  verify-tree "${recovery_root}" "${recovery_root}/SHA256SUMS" >/dev/null
[[ -s "${source_file}" && ! -L "${source_file}" ]] ||
  fail "El estado ACME recuperado está ausente o es inseguro"

python3 - "${source_file}" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
if path.stat().st_size > 64 * 1024 * 1024:
    raise SystemExit("El estado ACME excede 64 MiB")
try:
    document = json.loads(path.read_text(encoding="utf-8"))
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit("El estado ACME no es JSON UTF-8 válido") from exc
if not isinstance(document, dict):
    raise SystemExit("El estado ACME debe contener un objeto JSON")
PY

if [[ -e "${target_file}" || -L "${target_file}" ]]; then
  fail "Se rechaza sobrescribir el estado ACME existente: ${target_file}"
fi
if [[ -e "${target_directory}" ]]; then
  [[ -d "${target_directory}" && ! -L "${target_directory}" ]] ||
    fail "El directorio de Traefik es inseguro"
else
  install -d -m 0750 -- "${target_directory}"
  chown -- "+${traefik_uid}:+${traefik_gid}" "${target_directory}"
fi
[[ "$(stat -c '%a:%u:%g' "${target_directory}")" == \
  "750:${traefik_uid}:${traefik_gid}" ]] ||
  fail "Permisos o propietario del directorio Traefik incorrectos"

temporary="$(mktemp "${target_directory}/.acme.json.installing.XXXXXX")"
install -m 0600 -- "${source_file}" "${temporary}"
chown -- "+${traefik_uid}:+${traefik_gid}" "${temporary}"
[[ "$(sha256sum "${temporary}" | awk '{print $1}')" == \
  "$(sha256sum "${source_file}" | awk '{print $1}')" ]] ||
  fail "El SHA-256 cambió durante la instalación"
mv --no-clobber -- "${temporary}" "${target_file}"
[[ ! -e "${temporary}" ]] ||
  fail "El destino ACME apareció durante la instalación; no se sobrescribe"
temporary=""

[[ "$(stat -c '%a:%u:%g' "${target_file}")" == \
  "600:${traefik_uid}:${traefik_gid}" ]] ||
  fail "Permisos o propietario ACME incorrectos tras la instalación"

trap - EXIT
printf 'OK: estado ACME verificado e instalado sin sobrescritura.\n'
