#!/usr/bin/env bash

set -Eeuo pipefail
set +x
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="${TERRAFORM_SNAPSHOT_PROJECT_DIR:-$(cd -- "${SCRIPT_DIR}/.." && pwd)}"
PROJECT_DIR="$(realpath "${PROJECT_DIR}")"
readonly PROJECT_DIR
readonly TERRAFORM_BIN="${PROJECT_DIR}/.tools/terraform"
readonly PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
readonly SAFETY_BIN="${PROJECT_DIR}/scripts/terraform-safety.py"
readonly LEASE_BIN="${PROJECT_DIR}/scripts/r2-operation-lease.py"
readonly BACKEND_IDENTITIES="${PROJECT_DIR}/infra/terraform/backend-identities.json"
readonly SNAPSHOT_RECIPIENTS="${PROJECT_DIR}/infra/terraform/snapshot-recipients.json"

root_name=""
backend_config=""
recipient_file=""
output_dir=""
backend_mode="production"
state_validation="contract"

usage() {
  cat <<'EOF'
Usage:
  snapshot-terraform-state.sh
      --root cloudflare/state-bootstrap|cloudflare/apptolast-dns|netcup/perimeter
      --backend-config PATH
      --recipient-file PATH
      --output-dir PATH
      [--backend-mode production|migration-source|migration-destination]
      [--state-validation contract|structural]

The pulled state is encrypted directly with age. Plaintext state is never
written to disk. The destination must be outside this Git repository.
Structural validation is reserved for emergency snapshots after a failed
writer; normal snapshots require the exact root contract.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

assert_empty_file() {
  local path="$1"
  local context="$2"
  [[ -f "${path}" && ! -L "${path}" && ! -s "${path}" ]] ||
    fail "${context} wrote unexpected standard error; output withheld"
}

while (( $# > 0 )); do
  case "$1" in
    --root)
      (( $# >= 2 )) || fail "--root requires a value"
      root_name="$2"
      shift 2
      ;;
    --backend-config)
      (( $# >= 2 )) || fail "--backend-config requires a path"
      backend_config="$2"
      shift 2
      ;;
    --recipient-file)
      (( $# >= 2 )) || fail "--recipient-file requires a path"
      recipient_file="$2"
      shift 2
      ;;
    --output-dir)
      (( $# >= 2 )) || fail "--output-dir requires a path"
      output_dir="$2"
      shift 2
      ;;
    --backend-mode)
      (( $# >= 2 )) || fail "--backend-mode requires a value"
      backend_mode="$2"
      shift 2
      ;;
    --state-validation)
      (( $# >= 2 )) || fail "--state-validation requires a value"
      state_validation="$2"
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

case "${root_name}" in
  cloudflare/state-bootstrap|cloudflare/apptolast-dns|netcup/perimeter)
    ;;
  *)
    fail "--root is missing or not one of the reviewed Terraform roots"
    ;;
esac
case "${state_validation}" in
  contract|structural)
    ;;
  *)
    fail "--state-validation must be contract or structural"
    ;;
esac
case "${backend_mode}" in
  production|migration-source|migration-destination)
    ;;
  *)
    fail "--backend-mode must be production, migration-source, or migration-destination"
    ;;
esac

sanitize_terraform_environment() {
  local environment_name
  while IFS= read -r environment_name; do
    case "${environment_name}" in
      TF_*)
        unset "${environment_name}"
        ;;
    esac
  done < <(compgen -e)
}

reject_unsafe_cloud_environment() {
  local environment_name
  for environment_name in \
    ALL_PROXY \
    AWS_CA_BUNDLE \
    AWS_CONFIG_FILE \
    AWS_CONTAINER_CREDENTIALS_FULL_URI \
    AWS_CONTAINER_CREDENTIALS_RELATIVE_URI \
    AWS_DEFAULT_PROFILE \
    AWS_ENDPOINT_URL \
    AWS_ENDPOINT_URL_S3 \
    AWS_PROFILE \
    AWS_ROLE_ARN \
    AWS_ROLE_SESSION_NAME \
    AWS_SECURITY_TOKEN \
    AWS_SESSION_TOKEN \
    AWS_SHARED_CREDENTIALS_FILE \
    AWS_WEB_IDENTITY_TOKEN_FILE \
    HTTP_PROXY \
    HTTPS_PROXY \
    SSL_CERT_DIR \
    SSL_CERT_FILE \
    all_proxy \
    http_proxy \
    https_proxy; do
    [[ -z "${!environment_name:-}" ]] ||
      fail "unsafe inherited cloud environment variable: ${environment_name}"
  done
}

for command_name in age basename chmod date find flock git grep head install mktemp mv readlink realpath sha256sum tar; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done
[[ -x "${TERRAFORM_BIN}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -x "${PYTHON_BIN}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -f "${SAFETY_BIN}" && ! -L "${SAFETY_BIN}" ]] ||
  fail "Terraform safety helper is absent or unsafe"
[[ -f "${LEASE_BIN}" && ! -L "${LEASE_BIN}" ]] ||
  fail "Terraform distributed lease helper is absent or unsafe"
[[ -f "${BACKEND_IDENTITIES}" && ! -L "${BACKEND_IDENTITIES}" ]] ||
  fail "approved tracked backend identity registry is absent"
git -C "${PROJECT_DIR}" ls-files --error-unmatch \
  "infra/terraform/backend-identities.json" >/dev/null ||
  fail "backend-identities.json must be tracked"
[[ -f "${SNAPSHOT_RECIPIENTS}" && ! -L "${SNAPSHOT_RECIPIENTS}" ]] ||
  fail "approved tracked snapshot recipient registry is absent"
git -C "${PROJECT_DIR}" ls-files --error-unmatch \
  "infra/terraform/snapshot-recipients.json" >/dev/null ||
  fail "snapshot-recipients.json must be tracked"
[[ -f "${backend_config}" && ! -L "${backend_config}" ]] ||
  fail "backend configuration not found: ${backend_config}"
[[ -f "${recipient_file}" && ! -L "${recipient_file}" ]] ||
  fail "age recipient file not found: ${recipient_file}"
[[ -n "${output_dir}" ]] || fail "--output-dir is required"

backend_config="$(realpath "${backend_config}")"
recipient_file="$(realpath "${recipient_file}")"
project_real="$(realpath -m "${PROJECT_DIR}")"
output_real="$(realpath -m "${output_dir}")"
if [[ "${output_real}" == "${project_real}" ||
  "${output_real}" == "${project_real}/"* ]]; then
  fail "state snapshots must be stored outside the Git repository"
fi

install --directory --mode=0700 "${output_real}"
install --directory --mode=0750 "${PROJECT_DIR}/.terraform.d"
install --directory --mode=0700 "${PROJECT_DIR}/.terraform.d/leases"

operation_lock="$(
  realpath -m "${PROJECT_DIR}/.terraform.d/terraform-operation.lock"
)"
inherited_lock=""
if [[ -e /proc/self/fd/9 ]]; then
  inherited_lock="$(readlink --canonicalize /proc/self/fd/9 2>/dev/null || true)"
fi
if [[ "${inherited_lock}" != "${operation_lock}" ]]; then
  exec 9>"${operation_lock}"
fi
flock --nonblock 9 ||
  fail "another local Terraform operation holds the repository lock"

terraform_data_dir="$(mktemp -d)"
snapshot_tmp=""
checksum_tmp=""
lease_metadata=""
lease_token_file=""
cleanup() {
  [[ -z "${snapshot_tmp}" || ! -e "${snapshot_tmp}" ]] ||
    find "${snapshot_tmp}" -depth -delete
  [[ -z "${checksum_tmp}" || ! -e "${checksum_tmp}" ]] ||
    find "${checksum_tmp}" -depth -delete
  [[ ! -d "${terraform_data_dir}" ]] ||
    find "${terraform_data_dir}" -depth -delete
}
trap cleanup EXIT

revision="${TERRAFORM_SNAPSHOT_REVISION:-$(git -C "${PROJECT_DIR}" rev-parse --verify HEAD)}"
[[ "${revision}" =~ ^[a-f0-9]{40}$ ]] ||
  fail "cannot resolve the source commit"
runtime_project="${terraform_data_dir}/source"
install --directory --mode=0700 "${runtime_project}"
git -C "${PROJECT_DIR}" archive --format=tar "${revision}" \
  infra/terraform \
  scripts/r2-operation-lease.py \
  scripts/terraform-safety.py |
  tar --extract --directory="${runtime_project}"
terraform_root="${runtime_project}/infra/terraform/${root_name}"
runtime_safety_bin="${runtime_project}/scripts/terraform-safety.py"
runtime_lease_bin="${runtime_project}/scripts/r2-operation-lease.py"
[[ -d "${terraform_root}" &&
  -f "${runtime_safety_bin}" &&
  ! -L "${runtime_safety_bin}" &&
  -f "${runtime_lease_bin}" &&
  ! -L "${runtime_lease_bin}" ]] ||
  fail "cannot materialize the committed snapshot contract"

install --mode=0600 "${backend_config}" \
  "${terraform_data_dir}/backend.tfbackend"
backend_config="${terraform_data_dir}/backend.tfbackend"
install --mode=0600 "${recipient_file}" \
  "${terraform_data_dir}/age-recipients"
recipient_file="${terraform_data_dir}/age-recipients"
"${PYTHON_BIN}" "${runtime_safety_bin}" attest-snapshot-recipient \
  --recipient-file "${recipient_file}" \
  --project-dir "${runtime_project}" \
  >"${terraform_data_dir}/recipient-attestation.json"

sanitize_terraform_environment
reject_unsafe_cloud_environment
export TF_IN_AUTOMATION=1
export TF_INPUT=0
export TF_WORKSPACE=default
export TF_CLI_CONFIG_FILE=/dev/null
export TF_PLUGIN_CACHE_DIR="${PROJECT_DIR}/.terraform.d/plugin-cache"
install --directory --mode=0750 "${TF_PLUGIN_CACHE_DIR}"

init_events="${terraform_data_dir}/terraform-init.ndjson"
init_stderr="${terraform_data_dir}/terraform-init.stderr"
TF_DATA_DIR="${terraform_data_dir}" \
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" init \
    -json \
    -backend-config="${backend_config}" \
    -input=false \
    -lockfile=readonly \
    -reconfigure >"${init_events}" 2>"${init_stderr}"
"${PYTHON_BIN}" "${runtime_safety_bin}" attest-terraform-ui \
  --operation init \
  --events "${init_events}" \
  --stderr "${init_stderr}" >/dev/null
workspace_stderr="${terraform_data_dir}/workspace.stderr"
workspace="$(
  TF_DATA_DIR="${terraform_data_dir}" \
    "${TERRAFORM_BIN}" -chdir="${terraform_root}" workspace show \
      2>"${workspace_stderr}"
)"
assert_empty_file "${workspace_stderr}" "Terraform workspace show"
[[ "${workspace}" == "default" ]] ||
  fail "state snapshots require the default Terraform workspace"
backend_metadata="${terraform_data_dir}/terraform.tfstate"
[[ -f "${backend_metadata}" && ! -L "${backend_metadata}" ]] ||
  fail "Terraform backend metadata is absent or unsafe"
snapshot_backend_args=(
  attest-snapshot-backend
  --root "${root_name}"
  --metadata "${backend_metadata}"
  --workspace "${workspace}"
  --project-dir "${runtime_project}"
)
if [[ "${backend_mode}" == "migration-source" ]]; then
  snapshot_backend_args+=(--migration-source)
elif [[ "${backend_mode}" == "migration-destination" ]]; then
  snapshot_backend_args+=(--migration-destination)
fi
"${PYTHON_BIN}" "${runtime_safety_bin}" \
  "${snapshot_backend_args[@]}" >"${terraform_data_dir}/backend-attestation.json"

if [[ -n "${TERRAFORM_OPERATION_LEASE_EXTERNAL_ASSERT:-}" &&
  "${TERRAFORM_OPERATION_LEASE_EXTERNAL_ASSERT}" != "1" ]]; then
  fail "TERRAFORM_OPERATION_LEASE_EXTERNAL_ASSERT must be unset or 1"
fi
if [[ "${TERRAFORM_OPERATION_LEASE_EXTERNAL_ASSERT:-}" == "1" &&
  -z "${TERRAFORM_OPERATION_LEASE_FILE:-}" ]]; then
  fail "external lease assertion requires TERRAFORM_OPERATION_LEASE_FILE"
fi
if [[ "${root_name}" != "cloudflare/state-bootstrap" &&
  -n "${TERRAFORM_OPERATION_LEASE_FILE:-}" ]]; then
  lease_metadata="${backend_metadata}"
  lease_token_file="$(realpath "${TERRAFORM_OPERATION_LEASE_FILE}")"
  if [[ "${TERRAFORM_OPERATION_LEASE_EXTERNAL_ASSERT:-}" != "1" ]]; then
    "${PYTHON_BIN}" "${runtime_lease_bin}" assert-held \
      --root "${root_name}" \
      --metadata "${lease_metadata}" \
      --token-file "${lease_token_file}"
  fi
fi

timestamp="$(date -u +%Y%m%dT%H%M%S.%NZ)"
safe_root="${root_name//\//-}"
snapshot_tmp="$(mktemp "${output_real}/.${safe_root}.XXXXXX")"
snapshot_nonce="${snapshot_tmp##*.}"
snapshot_path="${output_real}/${safe_root}-${timestamp}-${snapshot_nonce}.tfstate.age"
checksum_path="${snapshot_path}.sha256"
[[ ! -e "${snapshot_path}" && ! -e "${checksum_path}" ]] ||
  fail "refusing to collide with an existing state snapshot"

state_pull_stderr="${terraform_data_dir}/state-pull.stderr"
TF_DATA_DIR="${terraform_data_dir}" \
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" state pull \
    2>"${state_pull_stderr}" |
  "${PYTHON_BIN}" "${runtime_safety_bin}" pass-validated-state \
    --root "${root_name}" \
    --validation "${state_validation}" |
  age --recipients-file "${recipient_file}" --output "${snapshot_tmp}"
assert_empty_file "${state_pull_stderr}" "Terraform state pull"
"${PYTHON_BIN}" "${runtime_safety_bin}" fsync-artifact \
  --path "${snapshot_tmp}" \
  --directory "${output_real}" >/dev/null

if [[ -n "${lease_token_file}" &&
  "${TERRAFORM_OPERATION_LEASE_EXTERNAL_ASSERT:-}" != "1" ]]; then
  "${PYTHON_BIN}" "${runtime_lease_bin}" assert-held \
    --root "${root_name}" \
    --metadata "${lease_metadata}" \
    --token-file "${lease_token_file}"
fi

[[ -s "${snapshot_tmp}" ]] || fail "the encrypted snapshot is empty"
head -n 1 "${snapshot_tmp}" |
  grep -Fx 'age-encryption.org/v1' >/dev/null ||
  fail "the snapshot is not an age file"

mv --no-clobber "${snapshot_tmp}" "${snapshot_path}"
[[ ! -e "${snapshot_tmp}" && -f "${snapshot_path}" && ! -L "${snapshot_path}" ]] ||
  fail "encrypted state snapshot was not published atomically"
snapshot_tmp=""
"${PYTHON_BIN}" "${runtime_safety_bin}" fsync-artifact \
  --path "${snapshot_path}" \
  --directory "${output_real}" >/dev/null
checksum_tmp="$(mktemp "${output_real}/.${safe_root}.checksum.XXXXXX")"
(
  cd "${output_real}"
  sha256sum "$(basename "${snapshot_path}")"
) >"${checksum_tmp}"
"${PYTHON_BIN}" "${runtime_safety_bin}" fsync-artifact \
  --path "${checksum_tmp}" \
  --directory "${output_real}" >/dev/null
mv --no-clobber "${checksum_tmp}" "${checksum_path}"
[[ ! -e "${checksum_tmp}" && -f "${checksum_path}" && ! -L "${checksum_path}" ]] ||
  fail "state snapshot checksum was not published atomically"
checksum_tmp=""
chmod 0600 "${snapshot_path}" "${checksum_path}"
"${PYTHON_BIN}" "${runtime_safety_bin}" fsync-artifact \
  --path "${snapshot_path}" \
  --directory "${output_real}" >/dev/null
"${PYTHON_BIN}" "${runtime_safety_bin}" fsync-artifact \
  --path "${checksum_path}" \
  --directory "${output_real}" >/dev/null

printf 'Encrypted state snapshot created: %s\n' "${snapshot_path}"
printf 'Source commit: %s\n' "${revision}"
printf 'State validation: %s\n' "${state_validation}"

cleanup
trap - EXIT
