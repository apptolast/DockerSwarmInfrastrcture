#!/usr/bin/env bash

set -Eeuo pipefail
set +x
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly TERRAFORM_BIN="${PROJECT_DIR}/.tools/terraform"
readonly PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
readonly SAFETY_BIN="${PROJECT_DIR}/scripts/terraform-safety.py"
readonly SNAPSHOT_BIN="${PROJECT_DIR}/scripts/snapshot-terraform-state.sh"
readonly LEASE_BIN="${PROJECT_DIR}/scripts/r2-operation-lease.py"
readonly BACKEND_IDENTITIES="${PROJECT_DIR}/infra/terraform/backend-identities.json"

root_name=""
source_backend_config=""
destination_backend_config=""
source_lock_proof=""
destination_lock_proof=""
source_access_key_file=""
source_secret_key_file=""
destination_access_key_file=""
destination_secret_key_file=""
recipient_file=""
snapshot_output_dir=""
confirmation=""

usage() {
  cat <<'EOF'
Usage:
  migrate-terraform-state.sh --root ROOT
      --source-backend-config PATH --destination-backend-config PATH
      --snapshot-recipient-file PATH --snapshot-output-dir PATH
      --confirm 'COPY_STATE:ROOT:SOURCE_SHA:DEST_SHA:LINEAGE:SERIAL:STATE_SHA:RECIPIENT_SHA'
      [--source-lock-proof PATH] [--destination-lock-proof PATH]
      [--source-access-key-file PATH --source-secret-key-file PATH
       --destination-access-key-file PATH --destination-secret-key-file PATH]

The root must already contain its exact terraform_data.root_identity. The
source must be the versioned production identity and the destination the
distinct versioned pending_destination with no state object. Both remote endpoints require current
two-client lock proofs. The wrapper snapshots the source, validates an explicit
confirmation bound to both backends and the source state, copies through
`state pull | state push` without `-force`, verifies an unchanged
lineage/serial/inventory, and then snapshots the destination.

All other writers must be disabled before invoking this command. This stage
does not retire the source: a successful result is a verified copy, not an
authorized cutover. Revoke every source writer/credential and record that
external control before making the destination authoritative. The wrapper does
not seed a missing identity and cannot copy between backend types that the
selected Terraform root does not declare.
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

while (( $# > 0 )); do
  case "$1" in
    --root)
      (( $# >= 2 )) || fail "--root requires a value"
      root_name="$2"
      shift 2
      ;;
    --source-backend-config)
      (( $# >= 2 )) || fail "--source-backend-config requires a path"
      source_backend_config="$2"
      shift 2
      ;;
    --destination-backend-config)
      (( $# >= 2 )) || fail "--destination-backend-config requires a path"
      destination_backend_config="$2"
      shift 2
      ;;
    --source-lock-proof)
      (( $# >= 2 )) || fail "--source-lock-proof requires a path"
      source_lock_proof="$2"
      shift 2
      ;;
    --destination-lock-proof)
      (( $# >= 2 )) || fail "--destination-lock-proof requires a path"
      destination_lock_proof="$2"
      shift 2
      ;;
    --source-access-key-file)
      (( $# >= 2 )) || fail "--source-access-key-file requires a path"
      source_access_key_file="$2"
      shift 2
      ;;
    --source-secret-key-file)
      (( $# >= 2 )) || fail "--source-secret-key-file requires a path"
      source_secret_key_file="$2"
      shift 2
      ;;
    --destination-access-key-file)
      (( $# >= 2 )) || fail "--destination-access-key-file requires a path"
      destination_access_key_file="$2"
      shift 2
      ;;
    --destination-secret-key-file)
      (( $# >= 2 )) || fail "--destination-secret-key-file requires a path"
      destination_secret_key_file="$2"
      shift 2
      ;;
    --snapshot-recipient-file)
      (( $# >= 2 )) ||
        fail "--snapshot-recipient-file requires a path"
      recipient_file="$2"
      shift 2
      ;;
    --snapshot-output-dir)
      (( $# >= 2 )) || fail "--snapshot-output-dir requires a path"
      snapshot_output_dir="$2"
      shift 2
      ;;
    --confirm)
      (( $# >= 2 )) || fail "--confirm requires a value"
      confirmation="$2"
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
    fail "--root is missing or is not one of the reviewed Terraform roots"
    ;;
esac
[[ "${root_name}" != "cloudflare/state-bootstrap" ]] ||
  fail "local-backend migration is disabled: it has no persistent post-writer quarantine contract"

for command_name in cmp date env find flock git grep install kill mkfifo mktemp realpath sha256sum stat tar; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done
[[ -x "${TERRAFORM_BIN}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -x "${PYTHON_BIN}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -f "${SAFETY_BIN}" && ! -L "${SAFETY_BIN}" ]] ||
  fail "Terraform safety helper is absent or unsafe"
[[ -x "${SNAPSHOT_BIN}" && ! -L "${SNAPSHOT_BIN}" ]] ||
  fail "Terraform snapshot helper is absent or unsafe"
[[ -f "${LEASE_BIN}" && ! -L "${LEASE_BIN}" ]] ||
  fail "Terraform distributed lease helper is absent or unsafe"
[[ -f "${BACKEND_IDENTITIES}" && ! -L "${BACKEND_IDENTITIES}" ]] ||
  fail "approved tracked backend identity registry is absent"
git -C "${PROJECT_DIR}" ls-files --error-unmatch \
  "infra/terraform/backend-identities.json" >/dev/null ||
  fail "backend-identities.json must be tracked"
for tracked_contract in \
  "infra/terraform/lease-recovery.allowed-signers" \
  "infra/terraform/snapshot-recipients.json"; do
  [[ -f "${PROJECT_DIR}/${tracked_contract}" &&
    ! -L "${PROJECT_DIR}/${tracked_contract}" ]] ||
    fail "approved tracked Terraform contract is absent: ${tracked_contract}"
  git -C "${PROJECT_DIR}" ls-files --error-unmatch \
    "${tracked_contract}" >/dev/null ||
    fail "Terraform contract must be tracked: ${tracked_contract}"
done

for required_file in \
  "${source_backend_config}" \
  "${destination_backend_config}" \
  "${recipient_file}"; do
  [[ -f "${required_file}" && ! -L "${required_file}" ]] ||
    fail "required input must be a regular, non-symlink file: ${required_file}"
done
source_backend_config="$(realpath "${source_backend_config}")"
destination_backend_config="$(realpath "${destination_backend_config}")"
recipient_file="$(realpath "${recipient_file}")"
snapshot_output_dir="$(realpath -m "${snapshot_output_dir}")"
[[ "${source_backend_config}" != "${destination_backend_config}" ]] ||
  fail "source and destination backend configurations must differ"
if [[ -n "${source_lock_proof}" ]]; then
  [[ -f "${PROJECT_DIR}/infra/terraform/lock-proof.allowed-signers" &&
    ! -L "${PROJECT_DIR}/infra/terraform/lock-proof.allowed-signers" ]] ||
    fail "approved tracked lock-proof signer registry is absent"
  git -C "${PROJECT_DIR}" ls-files --error-unmatch \
    "infra/terraform/lock-proof.allowed-signers" >/dev/null ||
    fail "lock-proof.allowed-signers must be tracked"
  [[ -f "${source_lock_proof}" && ! -L "${source_lock_proof}" ]] ||
    fail "the source lock proof must be a regular, non-symlink file"
  source_lock_proof="$(realpath "${source_lock_proof}")"
  [[ -f "${source_lock_proof}.sig" && ! -L "${source_lock_proof}.sig" ]] ||
    fail "the source lock proof signature must be adjacent and non-symlink"
fi
if [[ -n "${destination_lock_proof}" ]]; then
  [[ -f "${PROJECT_DIR}/infra/terraform/lock-proof.allowed-signers" &&
    ! -L "${PROJECT_DIR}/infra/terraform/lock-proof.allowed-signers" ]] ||
    fail "approved tracked lock-proof signer registry is absent"
  git -C "${PROJECT_DIR}" ls-files --error-unmatch \
    "infra/terraform/lock-proof.allowed-signers" >/dev/null ||
    fail "lock-proof.allowed-signers must be tracked"
  [[ -f "${destination_lock_proof}" && ! -L "${destination_lock_proof}" ]] ||
    fail "the destination lock proof must be a regular, non-symlink file"
  destination_lock_proof="$(realpath "${destination_lock_proof}")"
  [[ -f "${destination_lock_proof}.sig" &&
    ! -L "${destination_lock_proof}.sig" ]] ||
    fail "the destination lock proof signature must be adjacent and non-symlink"
fi

credential_files=(
  "${source_access_key_file}"
  "${source_secret_key_file}"
  "${destination_access_key_file}"
  "${destination_secret_key_file}"
)
if [[ "${root_name}" == "cloudflare/state-bootstrap" ]]; then
  for credential_file in "${credential_files[@]}"; do
    [[ -z "${credential_file}" ]] ||
      fail "local-backend migration must not receive R2 credential files"
  done
else
  for credential_file in "${credential_files[@]}"; do
    [[ -f "${credential_file}" && ! -L "${credential_file}" ]] ||
      fail "every R2 credential input must be a regular, non-symlink file"
    [[ "$(stat --format='%a' "${credential_file}")" == "600" ]] ||
      fail "every R2 credential input must be mode 0600"
  done
  source_access_key_file="$(realpath "${source_access_key_file}")"
  source_secret_key_file="$(realpath "${source_secret_key_file}")"
  destination_access_key_file="$(realpath "${destination_access_key_file}")"
  destination_secret_key_file="$(realpath "${destination_secret_key_file}")"
  source_access_key="$(<"${source_access_key_file}")"
  source_secret_key="$(<"${source_secret_key_file}")"
  destination_access_key="$(<"${destination_access_key_file}")"
  destination_secret_key="$(<"${destination_secret_key_file}")"
  [[ -n "${source_access_key}" && -n "${source_secret_key}" &&
    -n "${destination_access_key}" && -n "${destination_secret_key}" ]] ||
    fail "R2 credential files must not be empty"
  [[ "${source_access_key}" != "${destination_access_key}" ]] ||
    fail "source and destination must use distinct R2 credentials"
fi

run_with_r2_credentials() {
  local access_key="$1"
  local secret_key="$2"
  shift 2
  unset \
    AWS_PROFILE \
    AWS_DEFAULT_PROFILE \
    AWS_SHARED_CREDENTIALS_FILE \
    AWS_CONFIG_FILE \
    AWS_ROLE_ARN \
    AWS_WEB_IDENTITY_TOKEN_FILE \
    AWS_ROLE_SESSION_NAME \
    AWS_SESSION_TOKEN \
    AWS_SECURITY_TOKEN \
    AWS_CONTAINER_CREDENTIALS_FULL_URI \
    AWS_CONTAINER_CREDENTIALS_RELATIVE_URI
  if [[ "${root_name}" == "cloudflare/state-bootstrap" ]]; then
    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
  else
    export AWS_ACCESS_KEY_ID="${access_key}"
    export AWS_SECRET_ACCESS_KEY="${secret_key}"
  fi
  if (( $# > 0 )) && [[ "$1" == TF_DATA_DIR=* ]]; then
    export TF_DATA_DIR="${1#TF_DATA_DIR=}"
    shift
  fi
  exec "$@"
}

run_source_credentials() (
  run_with_r2_credentials \
    "${source_access_key:-}" \
    "${source_secret_key:-}" \
    "$@"
)

run_destination_credentials() (
  run_with_r2_credentials \
    "${destination_access_key:-}" \
    "${destination_secret_key:-}" \
    "$@"
)

source_environment=(run_source_credentials)
destination_environment=(run_destination_credentials)

worktree_status="$(
  git -C "${PROJECT_DIR}" status --porcelain --untracked-files=normal
)"
[[ -z "${worktree_status}" ]] ||
  fail "commit or discard every worktree change before state migration"
revision="$(git -C "${PROJECT_DIR}" rev-parse --verify HEAD)"
[[ "${revision}" =~ ^[a-f0-9]{40}$ ]] ||
  fail "cannot resolve the source commit"
plugin_cache_dir="${PROJECT_DIR}/.terraform.d/plugin-cache"
install --directory --mode=0750 \
  "${plugin_cache_dir}" \
  "${PROJECT_DIR}/.terraform.d"
install --directory --mode=0700 "${PROJECT_DIR}/.terraform.d/leases"

exec 9>"${PROJECT_DIR}/.terraform.d/terraform-operation.lock"
flock --nonblock 9 ||
  fail "another local Terraform operation holds the repository lock"

sanitize_terraform_environment
reject_unsafe_cloud_environment
export TF_IN_AUTOMATION=1
export TF_INPUT=0
export TF_CLI_CONFIG_FILE=/dev/null
export TF_PLUGIN_CACHE_DIR="${plugin_cache_dir}"
export TF_WORKSPACE=default

migration_dir="$(mktemp -d)"
source_data_dir="${migration_dir}/source-data"
destination_data_dir="${migration_dir}/destination-data"
install --directory --mode=0700 \
  "${source_data_dir}" \
  "${destination_data_dir}"
lease_owned=false
writer_started=false
caught_signal=""
source_writer_pid=""
destination_writer_pid=""
lease_token_file=""
runtime_lease_bin=""
source_backend_metadata=""
handle_signal() {
  caught_signal="$1"
  for active_pid in "${source_writer_pid}" "${destination_writer_pid}"; do
    if [[ -n "${active_pid}" ]] && kill -0 "${active_pid}" 2>/dev/null; then
      kill -s "$1" "${active_pid}" 2>/dev/null || true
    fi
  done
}
trap 'handle_signal INT' INT
trap 'handle_signal TERM' TERM
trap 'handle_signal HUP' HUP
cleanup() {
  if [[ "${lease_owned}" == true &&
    -n "${runtime_lease_bin}" &&
    -f "${source_backend_metadata}" &&
    -f "${lease_token_file}" ]]; then
    if [[ "${writer_started}" == true ]]; then
      "${source_environment[@]}" \
        "${PYTHON_BIN}" "${runtime_lease_bin}" quarantine \
          --root "${root_name}" \
          --metadata "${source_backend_metadata}" \
          --token-file "${lease_token_file}" >/dev/null 2>&1 || true
    else
      "${source_environment[@]}" \
        "${PYTHON_BIN}" "${runtime_lease_bin}" release \
          --pre-write \
          --root "${root_name}" \
          --metadata "${source_backend_metadata}" \
          --token-file "${lease_token_file}" >/dev/null 2>&1 || true
    fi
  fi
  [[ ! -d "${migration_dir}" ]] ||
    find "${migration_dir}" -depth -delete
}
trap cleanup EXIT

runtime_project="${migration_dir}/source"
install --directory --mode=0700 "${runtime_project}"
git -C "${PROJECT_DIR}" archive --format=tar "${revision}" \
  config \
  infra/terraform \
  scripts/r2-cross-credential-probe.py \
  scripts/r2-operation-lease.py \
  scripts/snapshot-terraform-state.sh \
  scripts/terraform-safety.py \
  scripts/test-terraform-r2-locking.sh |
  tar --extract --directory="${runtime_project}"
terraform_root="${runtime_project}/infra/terraform/${root_name}"
runtime_safety_bin="${runtime_project}/scripts/terraform-safety.py"
runtime_lease_bin="${runtime_project}/scripts/r2-operation-lease.py"
runtime_snapshot_bin="${runtime_project}/scripts/snapshot-terraform-state.sh"
[[ -d "${terraform_root}" &&
  -f "${runtime_safety_bin}" &&
  ! -L "${runtime_safety_bin}" &&
  -f "${runtime_lease_bin}" &&
  ! -L "${runtime_lease_bin}" &&
  -x "${runtime_snapshot_bin}" &&
  ! -L "${runtime_snapshot_bin}" ]] ||
  fail "cannot materialize the committed migration contract"

install --mode=0600 "${source_backend_config}" \
  "${migration_dir}/source.tfbackend"
source_backend_config="${migration_dir}/source.tfbackend"
install --mode=0600 "${destination_backend_config}" \
  "${migration_dir}/destination.tfbackend"
destination_backend_config="${migration_dir}/destination.tfbackend"
install --mode=0600 "${recipient_file}" \
  "${migration_dir}/age-recipients"
recipient_file="${migration_dir}/age-recipients"
recipient_attestation="${migration_dir}/recipient.json"
"${PYTHON_BIN}" "${runtime_safety_bin}" attest-snapshot-recipient \
  --recipient-file "${recipient_file}" \
  --project-dir "${runtime_project}" >"${recipient_attestation}"
if [[ -n "${source_lock_proof}" ]]; then
  install --mode=0600 "${source_lock_proof}" \
    "${migration_dir}/source-locking-proof.json"
  install --mode=0600 "${source_lock_proof}.sig" \
    "${migration_dir}/source-locking-proof.json.sig"
  source_lock_proof="${migration_dir}/source-locking-proof.json"
fi
if [[ -n "${destination_lock_proof}" ]]; then
  install --mode=0600 "${destination_lock_proof}" \
    "${migration_dir}/destination-locking-proof.json"
  install --mode=0600 "${destination_lock_proof}.sig" \
    "${migration_dir}/destination-locking-proof.json.sig"
  destination_lock_proof="${migration_dir}/destination-locking-proof.json"
fi

source_init_events="${migration_dir}/source-init.ndjson"
source_init_stderr="${migration_dir}/source-init.stderr"
"${source_environment[@]}" TF_DATA_DIR="${source_data_dir}" \
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" init \
    -json \
    -backend-config="${source_backend_config}" \
    -input=false \
    -lockfile=readonly \
    -reconfigure >"${source_init_events}" 2>"${source_init_stderr}"
"${PYTHON_BIN}" "${runtime_safety_bin}" \
  attest-terraform-ui \
    --operation init \
    --events "${source_init_events}" \
    --stderr "${source_init_stderr}" >/dev/null
validate_document="${migration_dir}/terraform-validate.json"
validate_stderr="${migration_dir}/terraform-validate.stderr"
"${source_environment[@]}" TF_DATA_DIR="${source_data_dir}" \
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" validate -json \
    >"${validate_document}" 2>"${validate_stderr}"
"${PYTHON_BIN}" "${runtime_safety_bin}" verify-terraform-validate \
  --document "${validate_document}" \
  --stderr "${validate_stderr}" >/dev/null
source_workspace_stderr="${migration_dir}/source-workspace.stderr"
source_workspace="$(
  "${source_environment[@]}" TF_DATA_DIR="${source_data_dir}" \
    "${TERRAFORM_BIN}" -chdir="${terraform_root}" workspace show \
      2>"${source_workspace_stderr}"
)"
assert_empty_file \
  "${source_workspace_stderr}" \
  "Terraform source workspace show"
[[ "${source_workspace}" == "default" ]] ||
  fail "source backend is not using the default workspace"
source_backend_args=(
  attest-migration-source
  --root "${root_name}"
  --metadata "${source_data_dir}/terraform.tfstate"
  --workspace "${source_workspace}"
  --project-dir "${runtime_project}"
)
if [[ -n "${source_lock_proof}" ]]; then
  source_backend_args+=(--lock-proof "${source_lock_proof}")
fi
source_backend_attestation="${migration_dir}/source-backend.json"
"${source_environment[@]}" "${PYTHON_BIN}" "${runtime_safety_bin}" \
  "${source_backend_args[@]}" >"${source_backend_attestation}"

destination_init_events="${migration_dir}/destination-init.ndjson"
destination_init_stderr="${migration_dir}/destination-init.stderr"
"${destination_environment[@]}" TF_DATA_DIR="${destination_data_dir}" \
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" init \
    -json \
    -backend-config="${destination_backend_config}" \
    -input=false \
    -lockfile=readonly \
    -reconfigure >"${destination_init_events}" 2>"${destination_init_stderr}"
"${PYTHON_BIN}" "${runtime_safety_bin}" \
  attest-terraform-ui \
    --operation init \
    --events "${destination_init_events}" \
    --stderr "${destination_init_stderr}" >/dev/null
destination_workspace_stderr="${migration_dir}/destination-workspace.stderr"
destination_workspace="$(
  "${destination_environment[@]}" TF_DATA_DIR="${destination_data_dir}" \
    "${TERRAFORM_BIN}" -chdir="${terraform_root}" workspace show \
      2>"${destination_workspace_stderr}"
)"
assert_empty_file \
  "${destination_workspace_stderr}" \
  "Terraform destination workspace show"
[[ "${destination_workspace}" == "default" ]] ||
  fail "destination backend is not using the default workspace"
destination_backend_args=(
  attest-migration-destination
  --root "${root_name}"
  --metadata "${destination_data_dir}/terraform.tfstate"
  --workspace "${destination_workspace}"
  --project-dir "${runtime_project}"
)
if [[ -n "${destination_lock_proof}" ]]; then
  destination_backend_args+=(--lock-proof "${destination_lock_proof}")
fi
destination_backend_attestation="${migration_dir}/destination-backend.json"
"${destination_environment[@]}" "${PYTHON_BIN}" "${runtime_safety_bin}" \
  "${destination_backend_args[@]}" >"${destination_backend_attestation}"

source_backend_metadata="${source_data_dir}/terraform.tfstate"
source_identity_sha256="$(
  "${PYTHON_BIN}" -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["identity_sha256"])
' "${source_backend_attestation}"
)"
destination_identity_sha256="$(
  "${PYTHON_BIN}" -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["identity_sha256"])
' "${destination_backend_attestation}"
)"
source_registry_sha256="$(
  "${PYTHON_BIN}" -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["backend_registry_sha256"])
' "${source_backend_attestation}"
)"
destination_registry_sha256="$(
  "${PYTHON_BIN}" -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["backend_registry_sha256"])
' "${destination_backend_attestation}"
)"
[[ "${source_registry_sha256}" == "${destination_registry_sha256}" ]] ||
  fail "source and destination attestations use different backend registries"
recipient_sha256="$(
  "${PYTHON_BIN}" -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["recipient_file_sha256"])
' "${recipient_attestation}"
)"

lease_transition() {
  [[ "${lease_owned}" == true ]] || return 0
  "${source_environment[@]}" \
    "${PYTHON_BIN}" "${runtime_lease_bin}" transition \
      --root "${root_name}" \
      --metadata "${source_backend_metadata}" \
      --token-file "${lease_token_file}" \
      --phase "$1"
}

assert_operation_lease() {
  [[ "${lease_owned}" == true ]] || return 0
  "${source_environment[@]}" \
    "${PYTHON_BIN}" "${runtime_lease_bin}" assert-held \
      --root "${root_name}" \
      --metadata "${source_backend_metadata}" \
      --token-file "${lease_token_file}"
}

lease_token_file="$(
  printf '%s/terraform-migration-%s-%s.json' \
    "${PROJECT_DIR}/.terraform.d/leases" \
    "$(date -u +%Y%m%dT%H%M%S.%NZ)" \
    "${RANDOM}"
)"
"${source_environment[@]}" \
  "${PYTHON_BIN}" "${runtime_lease_bin}" acquire \
    --root "${root_name}" \
    --metadata "${source_backend_metadata}" \
    --token-file "${lease_token_file}" \
    --source-commit "${revision}" \
    --operation migration \
    --backend-identity-sha256 "${source_identity_sha256}" \
    --backend-identity-sha256 "${destination_identity_sha256}" \
    --registry-sha256 "${source_registry_sha256}"
lease_owned=true

source_state_attestation="${migration_dir}/source-state.json"
source_state_pull_stderr="${migration_dir}/source-state-pull.stderr"
"${source_environment[@]}" TF_DATA_DIR="${source_data_dir}" \
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" state pull \
    2>"${source_state_pull_stderr}" |
  "${PYTHON_BIN}" "${runtime_safety_bin}" attest-state \
    --root "${root_name}" >"${source_state_attestation}"
assert_empty_file \
  "${source_state_pull_stderr}" \
  "Terraform source state pull"

source_state_show="${migration_dir}/source-state-show.json"
source_state_show_stderr="${migration_dir}/source-state-show.stderr"
"${source_environment[@]}" TF_DATA_DIR="${source_data_dir}" \
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" show -json \
    >"${source_state_show}" 2>"${source_state_show_stderr}"
assert_empty_file \
  "${source_state_show_stderr}" \
  "Terraform show source state"
source_checks="${migration_dir}/source-checks.json"
"${PYTHON_BIN}" "${runtime_safety_bin}" attest-state-checks \
  --root "${root_name}" \
  --document "${source_state_show}" >"${source_checks}"

destination_state_list_error="${migration_dir}/destination-state-list.error"
set +e
destination_inventory="$(
  "${destination_environment[@]}" TF_DATA_DIR="${destination_data_dir}" \
    "${TERRAFORM_BIN}" -chdir="${terraform_root}" state list \
      -no-color 2>"${destination_state_list_error}"
)"
destination_state_list_status=$?
set -e
if [[ "${destination_state_list_status}" -eq 1 ]] &&
  "${PYTHON_BIN}" "${runtime_safety_bin}" verify-empty-state-probe \
    --stderr "${destination_state_list_error}" >/dev/null; then
  :
elif [[ "${destination_state_list_status}" -eq 0 ]]; then
  fail "destination already has a state object, even though its inventory is empty"
else
  fail "cannot prove that the migration destination is empty"
fi
[[ -z "${destination_inventory}" ]] ||
  fail "destination backend unexpectedly contains managed resources"
destination_outputs="$(
  "${destination_environment[@]}" TF_DATA_DIR="${destination_data_dir}" \
    "${TERRAFORM_BIN}" -chdir="${terraform_root}" output -json \
      2>"${migration_dir}/destination-output.stderr"
)"
assert_empty_file \
  "${migration_dir}/destination-output.stderr" \
  "Terraform destination output"
[[ "${destination_outputs}" == "{}" ]] ||
  fail "destination backend already contains state outputs"

expected_confirmation="$(
  "${PYTHON_BIN}" - \
    "${root_name}" \
    "${source_backend_attestation}" \
    "${destination_backend_attestation}" \
    "${source_state_attestation}" \
    "${recipient_sha256}" <<'PY'
import json
from pathlib import Path
import sys

root = sys.argv[1]
source = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
destination = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
state = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
recipient_sha256 = sys.argv[5]
if source["identity_sha256"] == destination["identity_sha256"]:
    raise SystemExit("ERROR: source and destination backend identities are equal")
print(
    "COPY_STATE:"
    f"{root}:"
    f"{source['identity_sha256']}:"
    f"{destination['identity_sha256']}:"
    f"{state['lineage']}:"
    f"{state['serial']}:"
    f"{state['state_sha256']}:"
    f"{recipient_sha256}"
)
PY
)"
if [[ "${confirmation}" != "${expected_confirmation}" ]]; then
  printf 'Required confirmation: %s\n' "${expected_confirmation}" >&2
  fail "typed confirmation does not bind both backends and the source state"
fi

run_source_snapshot() {
  local snapshot_environment=(
    env
    TERRAFORM_SNAPSHOT_PROJECT_DIR="${PROJECT_DIR}"
    TERRAFORM_SNAPSHOT_REVISION="${revision}"
  )
  if [[ "${lease_owned}" == true ]]; then
    snapshot_environment+=(
      TERRAFORM_OPERATION_LEASE_FILE="${lease_token_file}"
    )
  fi
  "${source_environment[@]}" "${snapshot_environment[@]}" \
    "${runtime_snapshot_bin}" \
      --root "${root_name}" \
      --backend-config "${source_backend_config}" \
      --backend-mode migration-source \
      --recipient-file "${recipient_file}" \
      --output-dir "${snapshot_output_dir}"
}

run_destination_snapshot() {
  local validation="$1"
  local snapshot_environment=(
    env
    TERRAFORM_SNAPSHOT_PROJECT_DIR="${PROJECT_DIR}"
    TERRAFORM_SNAPSHOT_REVISION="${revision}"
  )
  if [[ "${lease_owned}" == true ]]; then
    snapshot_environment+=(
      TERRAFORM_OPERATION_LEASE_FILE="${lease_token_file}"
      TERRAFORM_OPERATION_LEASE_EXTERNAL_ASSERT=1
    )
  fi
  "${destination_environment[@]}" "${snapshot_environment[@]}" \
    "${runtime_snapshot_bin}" \
      --root "${root_name}" \
      --backend-config "${destination_backend_config}" \
      --backend-mode migration-destination \
      --state-validation "${validation}" \
      --recipient-file "${recipient_file}" \
      --output-dir "${snapshot_output_dir}"
}

lease_transition prestate_verified
[[ -z "${caught_signal}" ]] ||
  fail "migration interrupted before writer_started"
run_source_snapshot
assert_operation_lease
[[ -z "${caught_signal}" ]] ||
  fail "migration interrupted before writer_started"
lease_transition writer_started
writer_started=true
[[ -z "${caught_signal}" ]] ||
  fail "migration interrupted at writer_started; lease quarantined"

state_pipe="${migration_dir}/validated-state.pipe"
mkfifo --mode=0600 "${state_pipe}"
destination_writer_stderr="${migration_dir}/destination-state-push.stderr"
source_writer_stderr="${migration_dir}/source-writer-state-pull.stderr"
source_validation_stderr="${migration_dir}/source-state-validation.stderr"
set +e
(
  run_with_r2_credentials \
    "${destination_access_key:-}" \
    "${destination_secret_key:-}" \
    TF_DATA_DIR="${destination_data_dir}" \
    "${TERRAFORM_BIN}" -chdir="${terraform_root}" state push \
      -lock-timeout=60s - 2>"${destination_writer_stderr}"
) <"${state_pipe}" &
destination_writer_pid=$!
(
  # shellcheck disable=SC2094 # pass-attested-state drains stdin to EOF
  # before it ever opens --stderr, and EOF only arrives once the state-pull
  # process has exited and closed its own stderr fd, so the read never races
  # the write (see terraform-safety.py's pass-attested-state handler).
  run_with_r2_credentials \
    "${source_access_key:-}" \
    "${source_secret_key:-}" \
    TF_DATA_DIR="${source_data_dir}" \
    "${TERRAFORM_BIN}" -chdir="${terraform_root}" state pull \
      2>"${source_writer_stderr}" |
    "${PYTHON_BIN}" "${runtime_safety_bin}" pass-attested-state \
      --root "${root_name}" \
      --attestation "${source_state_attestation}" \
      --stderr "${source_writer_stderr}"
) >"${state_pipe}" 2>"${source_validation_stderr}" &
source_writer_pid=$!

wait "${source_writer_pid}"
source_writer_status=$?
while kill -0 "${source_writer_pid}" 2>/dev/null; do
  wait "${source_writer_pid}"
  source_writer_status=$?
done
source_writer_pid=""
wait "${destination_writer_pid}"
destination_writer_status=$?
while kill -0 "${destination_writer_pid}" 2>/dev/null; do
  wait "${destination_writer_pid}"
  destination_writer_status=$?
done
destination_writer_pid=""
if [[ ! -f "${source_writer_stderr}" ||
  -L "${source_writer_stderr}" ||
  -s "${source_writer_stderr}" ||
  ! -f "${source_validation_stderr}" ||
  -L "${source_validation_stderr}" ||
  -s "${source_validation_stderr}" ]]; then
  source_writer_status=1
fi
if [[ ! -f "${destination_writer_stderr}" ||
  -L "${destination_writer_stderr}" ||
  -s "${destination_writer_stderr}" ]]; then
  destination_writer_status=1
fi
set -e

lease_transition writer_finished
destination_state_validation="contract"
if [[ "${source_writer_status}" -ne 0 ||
  "${destination_writer_status}" -ne 0 ||
  -n "${caught_signal}" ]]; then
  destination_state_validation="structural"
fi
assert_operation_lease
set +e
run_destination_snapshot "${destination_state_validation}"
destination_snapshot_status=$?
set -e

if [[ "${destination_snapshot_status}" -eq 0 ]]; then
  assert_operation_lease
  lease_transition snapshot_verified
fi
[[ -z "${caught_signal}" ]] ||
  fail "Terraform migration was interrupted by ${caught_signal}; lease quarantined"
[[ "${source_writer_status}" -eq 0 &&
  "${destination_writer_status}" -eq 0 ]] ||
  fail "Terraform migration failed; destination emergency snapshot status=${destination_snapshot_status}"
[[ "${destination_snapshot_status}" -eq 0 ]] ||
  fail "migration succeeded but the mandatory destination snapshot failed"

migrated_backend_attestation="${migration_dir}/migrated-backend.json"
migrated_backend_args=(
  attest-migration-destination
  --root "${root_name}"
  --metadata "${destination_data_dir}/terraform.tfstate"
  --workspace "default"
  --project-dir "${runtime_project}"
)
if [[ -n "${destination_lock_proof}" ]]; then
  migrated_backend_args+=(--lock-proof "${destination_lock_proof}")
fi
"${destination_environment[@]}" "${PYTHON_BIN}" "${runtime_safety_bin}" \
  "${migrated_backend_args[@]}" >"${migrated_backend_attestation}"
cmp --silent \
  "${destination_backend_attestation}" \
  "${migrated_backend_attestation}" ||
  fail "Terraform initialized a backend other than the reviewed destination"

migrated_state_attestation="${migration_dir}/migrated-state.json"
destination_state_pull_stderr="${migration_dir}/destination-state-pull.stderr"
"${destination_environment[@]}" TF_DATA_DIR="${destination_data_dir}" \
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" state pull \
    2>"${destination_state_pull_stderr}" |
  "${PYTHON_BIN}" "${runtime_safety_bin}" attest-state \
    --root "${root_name}" >"${migrated_state_attestation}"
assert_empty_file \
  "${destination_state_pull_stderr}" \
  "Terraform destination state pull"

cmp --silent \
  "${source_state_attestation}" \
  "${migrated_state_attestation}" ||
  fail "state lineage, serial, identity, or inventory changed during migration"

source_state_after_copy="${migration_dir}/source-state-after-copy.json"
source_state_after_copy_stderr="${migration_dir}/source-state-after-copy.stderr"
"${source_environment[@]}" TF_DATA_DIR="${source_data_dir}" \
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" state pull \
    2>"${source_state_after_copy_stderr}" |
  "${PYTHON_BIN}" "${runtime_safety_bin}" attest-state \
    --root "${root_name}" >"${source_state_after_copy}"
assert_empty_file \
  "${source_state_after_copy_stderr}" \
  "Terraform source post-copy state pull"
cmp --silent \
  "${source_state_attestation}" \
  "${source_state_after_copy}" ||
  fail "source state changed during the copy; destination must not be activated"

destination_state_show="${migration_dir}/destination-state-show.json"
destination_state_show_stderr="${migration_dir}/destination-state-show.stderr"
"${destination_environment[@]}" TF_DATA_DIR="${destination_data_dir}" \
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" show -json \
    >"${destination_state_show}" 2>"${destination_state_show_stderr}"
assert_empty_file \
  "${destination_state_show_stderr}" \
  "Terraform show destination state"
destination_checks="${migration_dir}/destination-checks.json"
"${PYTHON_BIN}" "${runtime_safety_bin}" attest-state-checks \
  --root "${root_name}" \
  --document "${destination_state_show}" >"${destination_checks}"
cmp --silent "${source_checks}" "${destination_checks}" ||
  fail "Terraform checks changed during state migration"

lease_transition poststate_verified
if [[ "${lease_owned}" == true ]]; then
  "${source_environment[@]}" \
    "${PYTHON_BIN}" "${runtime_lease_bin}" release \
      --root "${root_name}" \
      --metadata "${source_backend_metadata}" \
      --token-file "${lease_token_file}"
  lease_owned=false
fi
writer_started=false

printf 'Terraform state copy completed and verified.\n'
printf 'Root: %s\n' "${root_name}"
printf 'Source and destination snapshots: %s\n' "${snapshot_output_dir}"
printf '%s\n' \
  "SOURCE REMAINS AUTHORITATIVE: retire every source writer before cutover."

cleanup
trap - EXIT
