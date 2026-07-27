#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
readonly API_BASE="https://api.cloudflare.com/client/v4"
readonly HOST_LOCK_HELPER="${PROJECT_DIR}/scripts/host_global_operation_lock.py"
readonly SCRIPT_PATH="${SCRIPT_DIR}/${BASH_SOURCE[0]##*/}"

token_file=""
secret_name=""
zone_name=""
token="" # gitleaks:allow -- Runtime-only secret; never assigned a literal.
verification_record_id=""
zone_id=""
original_args=("$@")

usage() {
  cat <<'EOF'
Usage:
  install-cloudflare-secret.sh [--token-file PATH]
      [--secret-name NAME] [--zone NAME]

Without --token-file, the token is read silently from the controlling terminal.
The token is verified with a temporary TXT create/delete operation before it is
created as a Docker Swarm secret. Existing secrets are never overwritten.
EOF
}

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

cloudflare_request() {
  local method="$1"
  local endpoint="$2"
  local payload="${3:-}"
  local curl_config

  printf -v curl_config '%s\n%s\n' \
    "header = \"Authorization: Bearer ${token}\"" \
    'header = "Content-Type: application/json"'

  if [[ -n "${payload}" ]]; then
    curl \
      --config /dev/fd/3 \
      --fail-with-body \
      --request "${method}" \
      --data "${payload}" \
      --proto '=https' \
      --silent \
      --show-error \
      --tlsv1.2 \
      "${API_BASE}${endpoint}" \
      3<<<"${curl_config}"
  else
    curl \
      --config /dev/fd/3 \
      --fail-with-body \
      --request "${method}" \
      --proto '=https' \
      --silent \
      --show-error \
      --tlsv1.2 \
      "${API_BASE}${endpoint}" \
      3<<<"${curl_config}"
  fi
}

cleanup() {
  if [[ -n "${verification_record_id}" && -n "${zone_id}" && -n "${token}" ]]; then
    cloudflare_request \
      DELETE \
      "/zones/${zone_id}/dns_records/${verification_record_id}" \
      >/dev/null 2>&1 || true
  fi
  token=""
}
trap cleanup EXIT

while (( $# > 0 )); do
  case "$1" in
    --token-file)
      (( $# >= 2 )) || fail "--token-file requires a path"
      token_file="$2"
      shift 2
      ;;
    --secret-name)
      (( $# >= 2 )) || fail "--secret-name requires a value"
      secret_name="$2"
      shift 2
      ;;
    --zone)
      (( $# >= 2 )) || fail "--zone requires a value"
      zone_name="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "unknown argument: $1"
      ;;
  esac
done

ensure_host_global_lock cloudflare-secret-install

for command_name in curl docker jq stat; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done
[[ -x "${PYTHON_BIN}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"

mapfile -t contract_defaults < <(
  "${PYTHON_BIN}" - "${PROJECT_DIR}" <<'PY'
import pathlib
import sys
import yaml

root = pathlib.Path(sys.argv[1])
contract = yaml.safe_load((root / "config/platform.yml").read_text())
group_vars = yaml.safe_load((root / "ansible/group_vars/all.yml").read_text())
print(contract["edge_cloudflare_zone"])
print(group_vars["edge_traefik_cloudflare_secret_name"])
PY
)
(( ${#contract_defaults[@]} == 2 )) ||
  fail "cannot read Cloudflare defaults from the reviewed configuration"
[[ -n "${zone_name}" ]] || zone_name="${contract_defaults[0]}"
[[ -n "${secret_name}" ]] || secret_name="${contract_defaults[1]}"

if ! docker info >/dev/null 2>&1; then
  if [[ "${CLOUDFLARE_SECRET_REEXEC:-0}" != 1 ]] &&
    getent group docker |
      awk -F: -v user_name="$(id -un)" '
        $1 == "docker" && ("," $4 ",") ~ ("," user_name ",") { found = 1 }
        END { exit !found }
      '; then
    printf -v quoted_command '%q ' "$0" "${original_args[@]}"
    exec sg docker -c \
      "CLOUDFLARE_SECRET_REEXEC=1 ${quoted_command}"
  fi
  fail "the current identity cannot access the Docker daemon"
fi

[[ "${secret_name}" =~ ^[a-zA-Z0-9_.-]+$ ]] ||
  fail "the Docker secret name is invalid"
[[ "${zone_name}" =~ ^[a-z0-9.-]+$ ]] ||
  fail "the Cloudflare zone name is invalid"

if [[ -n "${token_file}" ]]; then
  [[ -f "${token_file}" && ! -L "${token_file}" ]] ||
    fail "the token file must be a regular, non-symlink file"
  token_mode="$(stat --format='%a' "${token_file}")"
  (( (8#${token_mode} & 8#077) == 0 )) ||
    fail "the token file must not be accessible by group or other users"
  IFS= read -r token <"${token_file}" || [[ -n "${token}" ]]
else
  [[ -r /dev/tty ]] ||
    fail "no controlling terminal; use --token-file with mode 0600"
  IFS= read -r -s -p "Cloudflare DNS API token: " token </dev/tty
  printf '\n' >/dev/tty
fi

[[ "${token}" =~ ^[A-Za-z0-9_-]{20,256}$ ]] ||
  fail "the supplied value does not have the format of an API token"

[[ "$(docker info --format '{{.Swarm.LocalNodeState}}')" == "active" ]] ||
  fail "Docker Swarm is not active"
[[ "$(docker info --format '{{.Swarm.ControlAvailable}}')" == "true" ]] ||
  fail "run this command on a Swarm manager"

if docker secret inspect "${secret_name}" >/dev/null 2>&1; then
  fail "Docker secret ${secret_name} already exists; rotate with a new name"
fi

zone_response="$(
  cloudflare_request \
    GET \
    "/zones?name=${zone_name}&status=active&per_page=2"
)"
jq --exit-status '.success == true' <<<"${zone_response}" >/dev/null ||
  fail "Cloudflare rejected the token while reading the zone"
[[ "$(jq '.result | length' <<<"${zone_response}")" -eq 1 ]] ||
  fail "the token must expose exactly one active ${zone_name} zone"
zone_id="$(jq --raw-output '.result[0].id' <<<"${zone_response}")"
[[ "${zone_id}" =~ ^[a-f0-9]{32}$ ]] ||
  fail "Cloudflare returned an invalid zone identifier"

verification_name="_dockerswarm-token-check-$(date -u +%s)-${RANDOM}"
verification_payload="$(
  jq --null-input \
    --arg name "${verification_name}" \
    '{
      type: "TXT",
      name: $name,
      content: "permission-check",
      ttl: 60,
      comment: "Ephemeral Docker Swarm ACME token verification"
    }'
)"
verification_response="$(
  cloudflare_request \
    POST \
    "/zones/${zone_id}/dns_records" \
    "${verification_payload}"
)"
jq --exit-status '.success == true' <<<"${verification_response}" >/dev/null ||
  fail "the token cannot create a DNS record in ${zone_name}"
verification_record_id="$(
  jq --raw-output '.result.id' <<<"${verification_response}"
)"
[[ "${verification_record_id}" =~ ^[a-f0-9]{32}$ ]] ||
  fail "Cloudflare returned an invalid verification record identifier"

delete_response="$(
  cloudflare_request \
    DELETE \
    "/zones/${zone_id}/dns_records/${verification_record_id}"
)"
jq --exit-status '.success == true' <<<"${delete_response}" >/dev/null ||
  fail "the token created but could not delete the verification record"
verification_record_id=""

printf '%s' "${token}" | docker secret create \
  --label com.apptolast.managed-by=manual-bootstrap \
  --label com.apptolast.purpose=traefik-cloudflare-dns \
  "${secret_name}" \
  - >/dev/null

docker secret inspect "${secret_name}" >/dev/null
token=""
trap - EXIT

printf 'Cloudflare DNS token verified and Docker secret %s created.\n' \
  "${secret_name}"
printf 'Cloudflare zone ID (not a credential): %s\n' "${zone_id}"
