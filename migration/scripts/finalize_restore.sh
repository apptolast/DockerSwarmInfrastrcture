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
compose_file="${2:-${REPOSITORY_ROOT}/migration/compose/restore.yml}"
catalog_file="${3:-${REPOSITORY_ROOT}/config/services.yml}"
secret_catalog_file="${WORKLOADS_SECRET_CATALOG:-${REPOSITORY_ROOT}/stacks/workloads/secrets.yml}"
recovery="${services_root}/recovery"
state_dir="${services_root}/restore-state"
ready_marker="${state_dir}/workloads-ready-v2.json"
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

for command_name in docker python3; do
  command -v "${command_name}" >/dev/null 2>&1 ||
    fail "Falta el comando requerido: ${command_name}"
done
docker compose version >/dev/null 2>&1 ||
  fail "Docker Compose v2 no disponible"
python3 -c 'import yaml' >/dev/null 2>&1 ||
  fail "Falta PyYAML; aplique primero el role platform"

[[ "${services_root}" == /* && "${services_root}" != "/" ]] ||
  fail "SERVICES_ROOT debe ser absoluto y distinto de /"
[[ -d "${services_root}" && ! -L "${services_root}" ]] ||
  fail "SERVICES_ROOT ausente o inseguro"
services_root="$(cd -- "${services_root}" && pwd -P)"
recovery="${services_root}/recovery"
state_dir="${services_root}/restore-state"
ready_marker="${state_dir}/workloads-ready-v2.json"
for required_file in \
  "${compose_file}" \
  "${catalog_file}" \
  "${secret_catalog_file}" \
  "${SAFETY_TOOL}"; do
  [[ -f "${required_file}" && ! -L "${required_file}" ]] ||
    fail "Fichero de contrato ausente o inseguro: ${required_file}"
done
[[ ! -e "${ready_marker}" && ! -L "${ready_marker}" ]] ||
  fail "Se rechaza sobrescribir el gate existente"

if [[ "${services_root}" == "/srv/dockerswarm/services" ]]; then
  ensure_host_global_lock migration-finalize-restore
fi

python3 "${SAFETY_TOOL}" \
  verify-tree "${recovery}" "${recovery}/SHA256SUMS" >/dev/null
for phase_marker in \
  databases-core.json \
  database-vectors-081.json \
  database-rag-082.json; do
  [[ -s "${state_dir}/${phase_marker}" && \
    ! -L "${state_dir}/${phase_marker}" ]] ||
    fail "Falta la fase de restore: ${phase_marker}"
done

compose() {
  SERVICES_ROOT="${services_root}" docker compose -f "${compose_file}" "$@"
}

service_running() {
  local service=$1
  local identifier
  identifier="$(compose ps -q "${service}")"
  [[ -n "${identifier}" ]] ||
    fail "Servicio Compose no creado: ${service}"
  [[ "$(docker inspect -f '{{.State.Running}}' "${identifier}")" == "true" ]] ||
    fail "Servicio Compose no está en ejecución: ${service}"
}

psql_value() {
  local service=$1
  local user=$2
  local database=$3
  local query=$4
  compose exec -T "${service}" \
    psql -X -v ON_ERROR_STOP=1 -A -t \
    -U "${user}" -d "${database}" -c "${query}"
}

for service in n8n-db passbolt-db shlink-db; do
  service_running "${service}"
done

for database_spec in \
  "n8n-db:n8n:n8n" \
  "passbolt-db:passbolt:passbolt" \
  "shlink-db:shlink:shlink"; do
  IFS=: read -r database_service database_user database_name \
    <<<"${database_spec}"
  table_count="$(
    psql_value \
      "${database_service}" \
      "${database_user}" \
      "${database_name}" \
      "SELECT count(*) FROM pg_catalog.pg_class WHERE relkind IN ('r','p') AND relnamespace NOT IN (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname LIKE 'pg_%' OR nspname='information_schema');"
  )"
  [[ "${table_count}" =~ ^[0-9]+$ && "${table_count}" -gt 0 ]] ||
    fail "La base ${database_name} no contiene tablas restauradas"
done

n8n_active="$(
  psql_value n8n-db n8n n8n \
    "SELECT count(*) FROM workflow_entity w WHERE active IS TRUE OR (to_jsonb(w)->>'activeVersionId') IS NOT NULL;"
)"
[[ "${n8n_active}" == "0" ]] ||
  fail "n8n contiene workflows activos; no se emite el gate"

vectors_version="$(
  psql_value n8n-db n8n vectors \
    "SELECT extversion FROM pg_extension WHERE extname='vector';"
)"
[[ "${vectors_version}" == "0.8.1" ]] ||
  fail "vectors no conserva pgvector 0.8.1"
rag_version="$(
  psql_value n8n-db n8n rag \
    "SELECT extversion FROM pg_extension WHERE extname='vector';"
)"
[[ "${rag_version}" == "0.8.2" ]] ||
  fail "rag no usa pgvector 0.8.2"

PYTHONPATH="${SCRIPT_DIR}" python3 - \
  "${services_root}" \
  "${catalog_file}" \
  "${secret_catalog_file}" <<'PY'
from pathlib import Path
import sys

from finalize_restore import write_ready_marker

marker = write_ready_marker(
    services_root=Path(sys.argv[1]),
    canonical_services_root=Path("/srv/dockerswarm/services"),
    catalog_path=Path(sys.argv[2]),
    secret_catalog_path=Path(sys.argv[3]),
)
print(f"OK: gate de restore verificado y creado en {marker}")
PY
