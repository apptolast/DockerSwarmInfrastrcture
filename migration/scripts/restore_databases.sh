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
DEFAULT_COMPOSE_FILE="$(
  cd -- "${SCRIPT_DIR}/../compose" && pwd -P
)/restore.yml"
readonly DEFAULT_COMPOSE_FILE
DEFAULT_VECTOR_081_OVERRIDE="$(
  cd -- "${SCRIPT_DIR}/../compose" && pwd -P
)/restore-vectors-081.yml"
readonly DEFAULT_VECTOR_081_OVERRIDE

services_root="${1:-/srv/dockerswarm/services}"
compose_file="${2:-${DEFAULT_COMPOSE_FILE}}"
phase="${3:-all}"
vector_081_override="${VECTOR_081_OVERRIDE:-${DEFAULT_VECTOR_081_OVERRIDE}}"
wait_timeout="${COMPOSE_WAIT_TIMEOUT:-300}"
recovery="${services_root}/recovery"
state_dir="${services_root}/restore-state"
active_workflows_file="${state_dir}/n8n-active-workflows.json"
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

for command_name in awk date docker mktemp python3 rm sha256sum; do
  command -v "${command_name}" >/dev/null 2>&1 ||
    fail "Falta el comando requerido: ${command_name}"
done
docker compose version >/dev/null 2>&1 ||
  fail "Docker Compose v2 no disponible"

[[ "${services_root}" == /* && "${services_root}" != "/" ]] ||
  fail "SERVICES_ROOT debe ser una ruta absoluta distinta de /"
[[ ! -L "${services_root}" && -d "${services_root}" ]] ||
  fail "SERVICES_ROOT ausente o inseguro: ${services_root}"
services_root="$(cd -- "${services_root}" && pwd -P)"
recovery="${services_root}/recovery"
state_dir="${services_root}/restore-state"
active_workflows_file="${state_dir}/n8n-active-workflows.json"
case "${phase}" in
  core|vectors-081|rag-082|all|status|cleanup|activate-n8n)
    ;;
  *)
    fail "Fase inválida: ${phase}"
    ;;
esac
[[ -f "${compose_file}" && ! -L "${compose_file}" ]] ||
  fail "Compose ausente o inseguro: ${compose_file}"
[[ -f "${vector_081_override}" && ! -L "${vector_081_override}" ]] ||
  fail "Override pgvector 0.8.1 ausente o inseguro"
[[ -d "${recovery}" && ! -L "${recovery}" ]] ||
  fail "Recovery ausente o inseguro: ${recovery}"
[[ -f "${recovery}/SHA256SUMS" ]] ||
  fail "Runtime no preparado: ${recovery}"
[[ -f "${SAFETY_TOOL}" && ! -L "${SAFETY_TOOL}" ]] ||
  fail "Falta la utilidad: ${SAFETY_TOOL}"

python3 "${SAFETY_TOOL}" \
  verify-tree "${recovery}" "${recovery}/SHA256SUMS" >/dev/null

if [[ "${services_root}" == "/srv/dockerswarm/services" && \
  "${phase}" != "status" && "${phase}" != "activate-n8n" ]]; then
  ensure_host_global_lock "migration-restore-${phase}"
fi

if [[ "${phase}" != "status" && "${phase}" != "activate-n8n" ]]; then
  if [[ -e "${state_dir}" ]]; then
    [[ -d "${state_dir}" && ! -L "${state_dir}" ]] ||
      fail "Directorio de estado inseguro: ${state_dir}"
  else
    install -d -m 0700 "${state_dir}"
  fi
fi

compose() {
  SERVICES_ROOT="${services_root}" docker compose -f "${compose_file}" "$@"
}

compose_vectors_081() {
  SERVICES_ROOT="${services_root}" docker compose \
    -f "${compose_file}" \
    -f "${vector_081_override}" \
    "$@"
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

ensure_empty_database() {
  local service=$1
  local user=$2
  local database=$3
  local exists
  local table_count

  exists="$(
    psql_value "${service}" "${user}" postgres \
      "SELECT 1 FROM pg_database WHERE datname='${database}';"
  )"
  if [[ "${exists}" != "1" ]]; then
    compose exec -T "${service}" createdb -U "${user}" "${database}"
    return
  fi

  table_count="$(
    psql_value "${service}" "${user}" "${database}" \
      "SELECT count(*) FROM pg_catalog.pg_class WHERE relkind IN ('r','p') AND relnamespace NOT IN (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname LIKE 'pg_%' OR nspname='information_schema');"
  )"
  [[ "${table_count}" == "0" ]] ||
    fail "La base ${database} contiene ${table_count} tablas; no se sobrescribe"
}

database_table_count() {
  local service=$1
  local user=$2
  local database=$3
  psql_value "${service}" "${user}" "${database}" \
      "SELECT count(*) FROM pg_catalog.pg_class WHERE relkind IN ('r','p') AND relnamespace NOT IN (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname LIKE 'pg_%' OR nspname='information_schema');"
}

database_schema_sha256() {
  local service=$1
  local user=$2
  local database=$3
  psql_value "${service}" "${user}" "${database}" \
    "SELECT json_build_array(n.nspname, c.relname, c.relkind, a.attnum, a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull, COALESCE(pg_catalog.pg_get_expr(d.adbin, d.adrelid), ''))::text FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' AND c.relkind IN ('r','p','v','m','f') ORDER BY n.nspname, c.relname, c.relkind, a.attnum;" |
    sha256sum |
    awk '{print $1}'
}

restore_dump() {
  local service=$1
  local user=$2
  local database=$3
  local dump="${recovery}/databases/${database}.dump"

  [[ -s "${dump}" && ! -L "${dump}" ]] ||
    fail "Falta el dump seguro: ${dump}"
  ensure_empty_database "${service}" "${user}" "${database}"
  compose exec -T "${service}" \
    pg_restore \
      --exit-on-error \
      --single-transaction \
      --no-owner \
      --no-acl \
      -U "${user}" \
      -d "${database}" \
    <"${dump}"
}

write_marker() {
  local name=$1
  local temporary
  [[ ! -e "${state_dir}/${name}.json" ]] ||
    fail "El marker ${name}.json ya existe"
  temporary="$(mktemp "${state_dir}/.${name}.XXXXXX")"
  printf '{"schemaVersion":1,"phase":"%s","completedAt":"%s"}\n' \
    "${name}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"${temporary}"
  chmod 0600 "${temporary}"
  mv --no-clobber -- "${temporary}" "${state_dir}/${name}.json"
  [[ ! -e "${temporary}" ]] ||
    fail "El marker ${name}.json apareció durante la escritura"
}

write_database_marker() {
  local database=$1
  local dump_sha256=$2
  local table_count=$3
  local marker="${state_dir}/database-${database}-restored.json"
  local temporary
  [[ "${database}" =~ ^[a-z0-9-]+$ ]] ||
    fail "Identificador de base inválido"
  [[ "${dump_sha256}" =~ ^[a-f0-9]{64}$ ]] ||
    fail "SHA-256 de dump inválido"
  [[ "${table_count}" =~ ^[0-9]+$ && "${table_count}" -gt 0 ]] ||
    fail "Conteo de tablas inválido para ${database}"
  [[ ! -e "${marker}" ]] ||
    fail "El marker de ${database} ya existe"
  temporary="$(mktemp "${state_dir}/.database-${database}.XXXXXX")"
  printf \
    '{"schemaVersion":1,"database":"%s","dumpSha256":"%s","tableCount":%s,"completedAt":"%s"}\n' \
    "${database}" \
    "${dump_sha256}" \
    "${table_count}" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >"${temporary}"
  chmod 0600 "${temporary}"
  mv --no-clobber -- "${temporary}" "${marker}"
  [[ ! -e "${temporary}" ]] ||
    fail "El marker de ${database} apareció durante la escritura"
}

validate_database_marker() {
  local database=$1
  local dump_sha256=$2
  local table_count=$3
  local marker="${state_dir}/database-${database}-restored.json"
  python3 - "${marker}" "${database}" "${dump_sha256}" "${table_count}" <<'PY'
import json
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
if path.is_symlink() or not path.is_file():
    raise SystemExit("Marker de base ausente o inseguro")
document = json.loads(path.read_text(encoding="utf-8"))
if (
    not isinstance(document, dict)
    or set(document)
    != {
        "schemaVersion",
        "database",
        "dumpSha256",
        "tableCount",
        "completedAt",
    }
    or document["schemaVersion"] != 1
    or document["database"] != sys.argv[2]
    or document["dumpSha256"] != sys.argv[3]
    or not sys.argv[4].isdigit()
    or int(sys.argv[4]) <= 0
    or document["tableCount"] != int(sys.argv[4])
    or not isinstance(document["completedAt"], str)
    or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
        document["completedAt"],
    )
    is None
):
    raise SystemExit("Marker de base inválido o con fingerprint obsoleto")
PY
}

write_vector_database_marker() {
  local phase_name=$1
  local database=$2
  local dump_sha256=$3
  local table_count=$4
  local schema_sha256=$5
  local extension_version=$6
  local marker="${state_dir}/${phase_name}.json"
  local temporary
  [[ "${phase_name}" =~ ^database-(vectors-081|rag-082)$ ]] ||
    fail "Fase vectorial inválida"
  [[ "${database}" =~ ^(vectors|rag)$ ]] ||
    fail "Base vectorial inválida"
  [[ "${dump_sha256}" =~ ^[a-f0-9]{64}$ ]] ||
    fail "SHA-256 de dump vectorial inválido"
  [[ "${table_count}" =~ ^[0-9]+$ && "${table_count}" -gt 0 ]] ||
    fail "Conteo de tablas vectorial inválido"
  [[ "${schema_sha256}" =~ ^[a-f0-9]{64}$ ]] ||
    fail "SHA-256 de esquema vectorial inválido"
  [[ "${extension_version}" == "0.8.1" || \
    "${extension_version}" == "0.8.2" ]] ||
    fail "Versión pgvector inválida"
  [[ ! -e "${marker}" ]] ||
    fail "El marker ${phase_name}.json ya existe"
  temporary="$(mktemp "${state_dir}/.${phase_name}.XXXXXX")"
  printf \
    '{"schemaVersion":1,"phase":"%s","database":"%s","dumpSha256":"%s","tableCount":%s,"schemaSha256":"%s","vectorExtensionVersion":"%s","completedAt":"%s"}\n' \
    "${phase_name}" \
    "${database}" \
    "${dump_sha256}" \
    "${table_count}" \
    "${schema_sha256}" \
    "${extension_version}" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >"${temporary}"
  chmod 0600 "${temporary}"
  mv --no-clobber -- "${temporary}" "${marker}"
  [[ ! -e "${temporary}" ]] ||
    fail "El marker ${phase_name}.json apareció durante la escritura"
}

validate_vector_database_marker() {
  local phase_name=$1
  local database=$2
  local dump_sha256=$3
  local table_count=$4
  local schema_sha256=$5
  local extension_version=$6
  local marker="${state_dir}/${phase_name}.json"
  python3 - \
    "${marker}" \
    "${phase_name}" \
    "${database}" \
    "${dump_sha256}" \
    "${table_count}" \
    "${schema_sha256}" \
    "${extension_version}" <<'PY'
import json
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
if path.is_symlink() or not path.is_file():
    raise SystemExit("Marker vectorial ausente o inseguro")
document = json.loads(path.read_text(encoding="utf-8"))
if (
    not isinstance(document, dict)
    or set(document)
    != {
        "schemaVersion",
        "phase",
        "database",
        "dumpSha256",
        "tableCount",
        "schemaSha256",
        "vectorExtensionVersion",
        "completedAt",
    }
    or document["schemaVersion"] != 1
    or document["phase"] != sys.argv[2]
    or document["database"] != sys.argv[3]
    or document["dumpSha256"] != sys.argv[4]
    or not sys.argv[5].isdigit()
    or int(sys.argv[5]) <= 0
    or document["tableCount"] != int(sys.argv[5])
    or document["schemaSha256"] != sys.argv[6]
    or re.fullmatch(r"[a-f0-9]{64}", document["schemaSha256"]) is None
    or document["vectorExtensionVersion"] != sys.argv[7]
    or not isinstance(document["completedAt"], str)
    or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
        document["completedAt"],
    )
    is None
):
    raise SystemExit("Marker vectorial inválido, obsoleto o ajeno al esquema")
PY
}

restore_core_database() {
  local service=$1
  local user=$2
  local database=$3
  local dump="${recovery}/databases/${database}.dump"
  local dump_sha256
  local table_count
  local workflow_table_exists
  local marker="${state_dir}/database-${database}-restored.json"

  [[ -s "${dump}" && ! -L "${dump}" ]] ||
    fail "Falta el dump seguro: ${dump}"
  dump_sha256="$(sha256sum "${dump}" | awk '{print $1}')"
  table_count="$(database_table_count "${service}" "${user}" "${database}")"
  if [[ -e "${marker}" ]]; then
    validate_database_marker "${database}" "${dump_sha256}" "${table_count}"
    [[ "${table_count}" =~ ^[0-9]+$ && "${table_count}" -gt 0 ]] ||
      fail "La base ${database} marcada no contiene tablas"
    return
  fi

  if [[ "${table_count}" != "0" ]]; then
    if [[ "${database}" != "n8n" || ! -s "${active_workflows_file}" ]]; then
      fail "La base ${database} tiene datos sin evidencia; no se adopta"
    fi
    validate_workflow_inventory "${active_workflows_file}" >/dev/null
    workflow_table_exists="$(
      psql_value n8n-db n8n n8n \
        "SELECT CASE WHEN to_regclass('public.workflow_entity') IS NULL THEN 0 ELSE 1 END;"
    )"
    [[ "${workflow_table_exists}" == "1" ]] ||
      fail "La importación parcial n8n no contiene workflow_entity"
    write_database_marker "${database}" "${dump_sha256}" "${table_count}"
    printf 'Importación n8n previa adoptada con fingerprint verificado.\n'
    return
  fi

  restore_dump "${service}" "${user}" "${database}"
  table_count="$(database_table_count "${service}" "${user}" "${database}")"
  write_database_marker "${database}" "${dump_sha256}" "${table_count}"
}

validate_workflow_inventory() {
  local inventory_path=$1
  python3 - "${inventory_path}" <<'PY'
import json
from pathlib import Path
import sys

document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(document, list):
    raise SystemExit("Inventario de workflows activos inválido")
identifiers = []
for item in document:
    if not isinstance(item, dict) or set(item) != {"id", "activeVersionId"}:
        raise SystemExit("Inventario de workflows activos inválido")
    workflow_id = item["id"]
    version_id = item["activeVersionId"]
    if (
        not isinstance(workflow_id, str)
        or not workflow_id
        or not isinstance(version_id, str)
        or not version_id
        or any(character in workflow_id + version_id for character in "\x00\r\n\t")
    ):
        raise SystemExit("Inventario de workflows activos inválido")
    identifiers.append(workflow_id)
if len(identifiers) != len(set(identifiers)):
    raise SystemExit("Inventario de workflows activos duplicado")
print(len(document))
PY
}

quiesce_n8n_workflows() {
  local temporary
  local current_inventory
  local expected
  local remaining
  local quiesced_marker="${state_dir}/n8n-workflows-quiesced.json"

  if [[ -e "${quiesced_marker}" ]]; then
    [[ -s "${active_workflows_file}" && ! -L "${active_workflows_file}" ]] ||
      fail "Falta el inventario para validar el marker de quiesce"
    expected="$(validate_workflow_inventory "${active_workflows_file}")"
    remaining="$(
      psql_value n8n-db n8n n8n \
        "SELECT count(*) FROM workflow_entity w WHERE active IS TRUE OR (to_jsonb(w)->>'activeVersionId') IS NOT NULL;"
    )"
    [[ "${remaining}" == "0" ]] ||
      fail "El marker de quiesce existe pero hay workflows activos"
    printf 'Quiesce n8n ya validado: %s workflows inventariados.\n' "${expected}"
    return
  fi

  if [[ -e "${active_workflows_file}" ]]; then
    [[ -s "${active_workflows_file}" && ! -L "${active_workflows_file}" ]] ||
      fail "Inventario n8n existente vacío o inseguro"
    expected="$(validate_workflow_inventory "${active_workflows_file}")"
  else
    temporary="$(mktemp "${state_dir}/.n8n-active-workflows.XXXXXX")"
    psql_value n8n-db n8n n8n \
      "SELECT COALESCE(json_agg(json_build_object('id', id, 'activeVersionId', \"activeVersionId\") ORDER BY id), '[]'::json)::text FROM workflow_entity WHERE active IS TRUE OR \"activeVersionId\" IS NOT NULL;" \
      >"${temporary}"
    expected="$(validate_workflow_inventory "${temporary}")"
    chmod 0600 "${temporary}"
    mv --no-clobber -- "${temporary}" "${active_workflows_file}"
    [[ ! -e "${temporary}" ]] ||
      fail "El inventario n8n apareció durante la escritura"
  fi

  remaining="$(
    psql_value n8n-db n8n n8n \
      "SELECT count(*) FROM workflow_entity w WHERE active IS TRUE OR (to_jsonb(w)->>'activeVersionId') IS NOT NULL;"
  )"
  if [[ "${remaining}" != "0" ]]; then
    current_inventory="$(
      mktemp "${state_dir}/.n8n-current-workflows.XXXXXX"
    )"
    psql_value n8n-db n8n n8n \
      "SELECT COALESCE(json_agg(json_build_object('id', id, 'activeVersionId', \"activeVersionId\") ORDER BY id), '[]'::json)::text FROM workflow_entity WHERE active IS TRUE OR \"activeVersionId\" IS NOT NULL;" \
      >"${current_inventory}"
    validate_workflow_inventory "${current_inventory}" >/dev/null
    if ! python3 - \
      "${active_workflows_file}" "${current_inventory}" <<'PY'
import json
from pathlib import Path
import sys

expected = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
current = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
if current != expected:
    raise SystemExit("El inventario activo n8n cambió durante la reanudación")
PY
    then
      rm -f -- "${current_inventory}"
      fail "Los workflows activos difieren del inventario auditado"
    fi
    rm -f -- "${current_inventory}"

    # Root reads host-private Compose secrets; n8n then runs as image UID 1000.
    # shellcheck disable=SC2016
    compose --profile maintenance run --rm --no-deps \
      --user 0:0 \
      --cap-add SETUID \
      --cap-add SETGID \
      --entrypoint /bin/sh \
      n8n-maintenance \
      -ec '
        test "$(id -u)" = 0
        export DB_POSTGRESDB_PASSWORD="$(cat /run/secrets/n8n_db_password)"
        export N8N_ENCRYPTION_KEY="$(cat /run/secrets/n8n_encryption_key)"
        exec su node -s /bin/sh -c "exec n8n unpublish:workflow --all"
      '
  fi

  remaining="$(
    psql_value n8n-db n8n n8n \
      "SELECT count(*) FROM workflow_entity w WHERE active IS TRUE OR (to_jsonb(w)->>'activeVersionId') IS NOT NULL;"
  )"
  [[ "${remaining}" == "0" ]] ||
    fail "No se pudieron desactivar todos los workflows n8n"
  write_marker n8n-workflows-quiesced
  printf 'Workflows n8n desactivados y auditados: %s\n' "${expected}"
}

start_databases() {
  compose up -d --no-build --wait --wait-timeout "${wait_timeout}" \
    n8n-db passbolt-db shlink-db
}

restore_core() {
  start_databases
  for service in n8n-db passbolt-db shlink-db; do
    service_running "${service}"
  done
  restore_core_database n8n-db n8n n8n
  quiesce_n8n_workflows
  restore_core_database passbolt-db passbolt passbolt
  restore_core_database shlink-db shlink shlink
  if [[ -e "${state_dir}/databases-core.json" ]]; then
    [[ -s "${state_dir}/databases-core.json" && \
      ! -L "${state_dir}/databases-core.json" ]] ||
      fail "El marker core existente es inseguro"
  else
    write_marker databases-core
  fi
}

restore_vectors_081() {
  local dump="${recovery}/databases/vectors.dump"
  local dump_sha256
  local schema_sha256
  local table_count
  local version
  [[ -s "${state_dir}/databases-core.json" ]] ||
    fail "Debe completarse antes la fase core"
  [[ -s "${dump}" && ! -L "${dump}" ]] ||
    fail "Falta el dump seguro: ${dump}"
  dump_sha256="$(sha256sum "${dump}" | awk '{print $1}')"
  if [[ -e "${state_dir}/database-vectors-081.json" ]]; then
    version="$(psql_value n8n-db n8n vectors \
      "SELECT extversion FROM pg_extension WHERE extname='vector';")"
    [[ "${version}" == "0.8.1" ]] ||
      fail "El marker vectors-081 no coincide con la base"
    table_count="$(database_table_count n8n-db n8n vectors)"
    schema_sha256="$(database_schema_sha256 n8n-db n8n vectors)"
    validate_vector_database_marker \
      database-vectors-081 \
      vectors \
      "${dump_sha256}" \
      "${table_count}" \
      "${schema_sha256}" \
      "${version}"
    return
  fi
  compose_vectors_081 up -d --no-build --force-recreate \
    --wait --wait-timeout "${wait_timeout}" n8n-db
  restore_dump n8n-db n8n vectors
  version="$(psql_value n8n-db n8n vectors \
    "SELECT extversion FROM pg_extension WHERE extname='vector';")"
  [[ "${version}" == "0.8.1" ]] ||
    fail "vectors debe usar exactamente pgvector 0.8.1: ${version}"
  table_count="$(database_table_count n8n-db n8n vectors)"
  schema_sha256="$(database_schema_sha256 n8n-db n8n vectors)"
  write_vector_database_marker \
    database-vectors-081 \
    vectors \
    "${dump_sha256}" \
    "${table_count}" \
    "${schema_sha256}" \
    "${version}"
}

restore_rag_082() {
  local dump="${recovery}/databases/rag.dump"
  local dump_sha256
  local schema_sha256
  local table_count
  local version
  local restored_vectors_version
  local vectors_dump="${recovery}/databases/vectors.dump"
  local vectors_dump_sha256
  local vectors_schema_sha256
  local vectors_table_count
  [[ -s "${state_dir}/database-vectors-081.json" ]] ||
    fail "Debe completarse antes la fase vectors-081"
  [[ -s "${dump}" && ! -L "${dump}" ]] ||
    fail "Falta el dump seguro: ${dump}"
  [[ -s "${vectors_dump}" && ! -L "${vectors_dump}" ]] ||
    fail "Falta el dump seguro: ${vectors_dump}"
  dump_sha256="$(sha256sum "${dump}" | awk '{print $1}')"
  vectors_dump_sha256="$(sha256sum "${vectors_dump}" | awk '{print $1}')"
  restored_vectors_version="$(psql_value n8n-db n8n vectors \
    "SELECT extversion FROM pg_extension WHERE extname='vector';")"
  [[ "${restored_vectors_version}" == "0.8.1" ]] ||
    fail "El marker vectors-081 no coincide con la base"
  vectors_table_count="$(database_table_count n8n-db n8n vectors)"
  vectors_schema_sha256="$(database_schema_sha256 n8n-db n8n vectors)"
  validate_vector_database_marker \
    database-vectors-081 \
    vectors \
    "${vectors_dump_sha256}" \
    "${vectors_table_count}" \
    "${vectors_schema_sha256}" \
    "${restored_vectors_version}"
  if [[ -e "${state_dir}/database-rag-082.json" ]]; then
    version="$(psql_value n8n-db n8n rag \
      "SELECT extversion FROM pg_extension WHERE extname='vector';")"
    [[ "${version}" == "0.8.2" ]] ||
      fail "El marker rag-082 no coincide con la base"
    table_count="$(database_table_count n8n-db n8n rag)"
    schema_sha256="$(database_schema_sha256 n8n-db n8n rag)"
    validate_vector_database_marker \
      database-rag-082 \
      rag \
      "${dump_sha256}" \
      "${table_count}" \
      "${schema_sha256}" \
      "${version}"
    return
  fi
  compose up -d --no-build --force-recreate \
    --wait --wait-timeout "${wait_timeout}" n8n-db
  restored_vectors_version="$(psql_value n8n-db n8n vectors \
    "SELECT extversion FROM pg_extension WHERE extname='vector';")"
  [[ "${restored_vectors_version}" == "0.8.1" ]] ||
    fail "El cambio de imagen alteró la versión de vectors"
  vectors_table_count="$(database_table_count n8n-db n8n vectors)"
  vectors_schema_sha256="$(database_schema_sha256 n8n-db n8n vectors)"
  validate_vector_database_marker \
    database-vectors-081 \
    vectors \
    "${vectors_dump_sha256}" \
    "${vectors_table_count}" \
    "${vectors_schema_sha256}" \
    "${restored_vectors_version}"
  restore_dump n8n-db n8n rag
  version="$(psql_value n8n-db n8n rag \
    "SELECT extversion FROM pg_extension WHERE extname='vector';")"
  [[ "${version}" == "0.8.2" ]] ||
    fail "rag debe usar exactamente pgvector 0.8.2: ${version}"
  table_count="$(database_table_count n8n-db n8n rag)"
  schema_sha256="$(database_schema_sha256 n8n-db n8n rag)"
  write_vector_database_marker \
    database-rag-082 \
    rag \
    "${dump_sha256}" \
    "${table_count}" \
    "${schema_sha256}" \
    "${version}"
}

case "${phase}" in
  core)
    restore_core
    ;;
  vectors-081)
    restore_vectors_081
    ;;
  rag-082)
    restore_rag_082
    ;;
  all)
    restore_core
    restore_vectors_081
    restore_rag_082
    ;;
  status)
    if [[ -d "${state_dir}" && ! -L "${state_dir}" ]]; then
      find "${state_dir}" -maxdepth 1 -type f -name '*.json' \
        -printf '%f\n' | sort
    elif [[ -e "${state_dir}" ]]; then
      fail "Directorio de estado inseguro: ${state_dir}"
    fi
    ;;
  cleanup)
    compose --profile maintenance down --remove-orphans
    ;;
  activate-n8n)
    fail "La activación no pertenece al restore aislado; hágala tras validar el stack Swarm"
    ;;
esac

printf 'OK: fase %s completada mediante Compose aislado.\n' "${phase}"
