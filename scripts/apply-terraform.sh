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

root_name=""
backend_config=""
plan_path=""
sidecar_path=""
lock_proof=""
recipient_file=""
snapshot_output_dir=""
confirmation=""

usage() {
  cat <<'EOF'
Usage:
  apply-terraform.sh --root ROOT --backend-config PATH
      --plan PATH --sidecar PATH
      --snapshot-recipient-file PATH --snapshot-output-dir PATH
      --confirm 'APPLY:ROOT:COMMIT:PLAN_SHA256:RECIPIENT_SHA256'
      [--lock-proof PATH]

The wrapper only applies a saved non-destructive plan created by
plan-terraform.sh. It requires the adjacent approved sidecar signature, same
clean commit, backend identity, default workspace, state
lineage/serial/inventory, and current locking proof.
Established state is snapshotted immediately before apply. Every apply attempt,
including initialization, is followed by a mandatory encrypted state snapshot.
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
    --backend-config)
      (( $# >= 2 )) || fail "--backend-config requires a path"
      backend_config="$2"
      shift 2
      ;;
    --plan)
      (( $# >= 2 )) || fail "--plan requires a path"
      plan_path="$2"
      shift 2
      ;;
    --sidecar)
      (( $# >= 2 )) || fail "--sidecar requires a path"
      sidecar_path="$2"
      shift 2
      ;;
    --lock-proof)
      (( $# >= 2 )) || fail "--lock-proof requires a path"
      lock_proof="$2"
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
  fail "local-backend apply is disabled: it has no persistent post-writer quarantine contract"

for command_name in awk date find flock git grep install kill mktemp realpath sha256sum stat tar; do
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
for tracked_contract in \
  "infra/terraform/backend-identities.json" \
  "infra/terraform/lease-recovery.allowed-signers" \
  "infra/terraform/plan.allowed-signers" \
  "infra/terraform/snapshot-recipients.json"; do
  [[ -f "${PROJECT_DIR}/${tracked_contract}" &&
    ! -L "${PROJECT_DIR}/${tracked_contract}" ]] ||
    fail "approved tracked Terraform contract is absent: ${tracked_contract}"
  git -C "${PROJECT_DIR}" ls-files --error-unmatch \
    "${tracked_contract}" >/dev/null ||
    fail "Terraform contract must be tracked: ${tracked_contract}"
done

sidecar_signature_path="${sidecar_path}.sig"
for protected_file in \
  "${backend_config}" \
  "${plan_path}" \
  "${sidecar_path}" \
  "${sidecar_signature_path}" \
  "${recipient_file}"; do
  [[ -f "${protected_file}" && ! -L "${protected_file}" ]] ||
    fail "required input must be a regular, non-symlink file: ${protected_file}"
done
if [[ -n "${lock_proof}" ]]; then
  [[ -f "${PROJECT_DIR}/infra/terraform/lock-proof.allowed-signers" &&
    ! -L "${PROJECT_DIR}/infra/terraform/lock-proof.allowed-signers" ]] ||
    fail "approved tracked lock-proof signer registry is absent"
  git -C "${PROJECT_DIR}" ls-files --error-unmatch \
    "infra/terraform/lock-proof.allowed-signers" >/dev/null ||
    fail "lock-proof.allowed-signers must be tracked"
  [[ -f "${lock_proof}" && ! -L "${lock_proof}" ]] ||
    fail "the lock proof must be a regular, non-symlink file"
  lock_proof="$(realpath "${lock_proof}")"
  [[ -f "${lock_proof}.sig" && ! -L "${lock_proof}.sig" ]] ||
    fail "the lock proof signature must be adjacent and non-symlink"
fi

backend_config="$(realpath "${backend_config}")"
plan_path="$(realpath "${plan_path}")"
sidecar_path="$(realpath "${sidecar_path}")"
sidecar_signature_path="$(realpath "${sidecar_signature_path}")"
recipient_file="$(realpath "${recipient_file}")"
snapshot_output_dir="$(realpath -m "${snapshot_output_dir}")"
expected_plan_prefix="${PROJECT_DIR}/.build/plans/"
case "${plan_path}" in
  "${expected_plan_prefix}"*.tfplan)
    ;;
  *)
    fail "the saved plan must be a .tfplan file below .build/plans"
    ;;
esac
[[ "${sidecar_path}" == "${plan_path}.metadata.json" ]] ||
  fail "the sidecar path must be PLAN.metadata.json"
[[ "${sidecar_signature_path}" == "${sidecar_path}.sig" ]] ||
  fail "the sidecar signature must be adjacent to PLAN.metadata.json"
[[ "$(stat --format='%a' "${plan_path}")" == "600" ]] ||
  fail "the saved plan must be mode 0600"
[[ "$(stat --format='%a' "${sidecar_path}")" == "600" ]] ||
  fail "the saved plan sidecar must be mode 0600"
[[ "$(stat --format='%a' "${sidecar_signature_path}")" == "600" ]] ||
  fail "the saved plan signature must be mode 0600"

worktree_status="$(
  git -C "${PROJECT_DIR}" status --porcelain --untracked-files=normal
)"
[[ -z "${worktree_status}" ]] ||
  fail "commit or discard every worktree change before Terraform apply"
revision="$(git -C "${PROJECT_DIR}" rev-parse --verify HEAD)"
[[ "${revision}" =~ ^[a-f0-9]{40}$ ]] ||
  fail "cannot resolve the source commit"

terraform_root="${PROJECT_DIR}/infra/terraform/${root_name}"
[[ -d "${terraform_root}" ]] ||
  fail "Terraform root not found: ${terraform_root}"
terraform_data_dir="$(mktemp -d)"
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
export TF_DATA_DIR="${terraform_data_dir}"
export TF_IN_AUTOMATION=1
export TF_INPUT=0
export TF_CLI_CONFIG_FILE=/dev/null
export TF_PLUGIN_CACHE_DIR="${plugin_cache_dir}"
export TF_WORKSPACE=default

attestation_dir="$(mktemp -d)"
lease_owned=false
writer_started=false
writer_pid=""
caught_signal=""
lease_token_file=""
runtime_lease_bin=""
backend_metadata=""
handle_signal() {
  caught_signal="$1"
  if [[ -n "${writer_pid}" ]] && kill -0 "${writer_pid}" 2>/dev/null; then
    kill -s "$1" "${writer_pid}" 2>/dev/null || true
  fi
}
trap 'handle_signal INT' INT
trap 'handle_signal TERM' TERM
trap 'handle_signal HUP' HUP
cleanup() {
  if [[ "${lease_owned}" == true &&
    -n "${runtime_lease_bin}" &&
    -f "${backend_metadata}" &&
    -f "${lease_token_file}" ]]; then
    if [[ "${writer_started}" == true ]]; then
      "${PYTHON_BIN}" "${runtime_lease_bin}" quarantine \
        --root "${root_name}" \
        --metadata "${backend_metadata}" \
        --token-file "${lease_token_file}" >/dev/null 2>&1 || true
    else
      "${PYTHON_BIN}" "${runtime_lease_bin}" release \
        --pre-write \
        --root "${root_name}" \
        --metadata "${backend_metadata}" \
        --token-file "${lease_token_file}" >/dev/null 2>&1 || true
    fi
  fi
  [[ ! -d "${terraform_data_dir}" ]] ||
    find "${terraform_data_dir}" -depth -delete
  [[ ! -d "${attestation_dir}" ]] ||
    find "${attestation_dir}" -depth -delete
}
trap cleanup EXIT

runtime_project="${attestation_dir}/source"
install --directory --mode=0700 "${runtime_project}"
git -C "${PROJECT_DIR}" archive --format=tar "${revision}" \
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
  fail "cannot materialize the committed Terraform source"

install --mode=0600 "${backend_config}" \
  "${attestation_dir}/backend.tfbackend"
backend_config="${attestation_dir}/backend.tfbackend"
install --mode=0600 "${plan_path}" "${attestation_dir}/reviewed.tfplan"
plan_path="${attestation_dir}/reviewed.tfplan"
install --mode=0600 "${sidecar_path}" \
  "${attestation_dir}/reviewed.tfplan.metadata.json"
sidecar_path="${attestation_dir}/reviewed.tfplan.metadata.json"
install --mode=0600 "${sidecar_signature_path}" \
  "${attestation_dir}/reviewed.tfplan.metadata.json.sig"
sidecar_signature_path="${attestation_dir}/reviewed.tfplan.metadata.json.sig"
install --mode=0600 "${recipient_file}" \
  "${attestation_dir}/age-recipients"
recipient_file="${attestation_dir}/age-recipients"
recipient_attestation="${attestation_dir}/recipient.json"
"${PYTHON_BIN}" "${runtime_safety_bin}" attest-snapshot-recipient \
  --recipient-file "${recipient_file}" \
  --project-dir "${runtime_project}" >"${recipient_attestation}"
if [[ -n "${lock_proof}" ]]; then
  install --mode=0600 "${lock_proof}" \
    "${attestation_dir}/locking-proof.json"
  install --mode=0600 "${lock_proof}.sig" \
    "${attestation_dir}/locking-proof.json.sig"
  lock_proof="${attestation_dir}/locking-proof.json"
fi

"${PYTHON_BIN}" "${runtime_safety_bin}" verify-plan-signature \
  --sidecar "${sidecar_path}" \
  --project-dir "${runtime_project}" >/dev/null

init_events="${attestation_dir}/terraform-init.ndjson"
init_stderr="${attestation_dir}/terraform-init.stderr"
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

validate_document="${attestation_dir}/terraform-validate.json"
validate_stderr="${attestation_dir}/terraform-validate.stderr"
"${TERRAFORM_BIN}" -chdir="${terraform_root}" validate -json \
  >"${validate_document}" 2>"${validate_stderr}"
"${PYTHON_BIN}" "${runtime_safety_bin}" verify-terraform-validate \
  --document "${validate_document}" \
  --stderr "${validate_stderr}" >/dev/null

workspace_stderr="${attestation_dir}/workspace.stderr"
workspace="$(
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" workspace show \
    2>"${workspace_stderr}"
)"
assert_empty_file "${workspace_stderr}" "Terraform workspace show"
[[ "${workspace}" == "default" ]] ||
  fail "production apply requires the default Terraform workspace"
backend_metadata="${terraform_data_dir}/terraform.tfstate"
[[ -f "${backend_metadata}" && ! -L "${backend_metadata}" ]] ||
  fail "Terraform backend metadata is absent or unsafe"

backend_attestation="${attestation_dir}/backend.json"
backend_attestation_args=(
  attest-backend
  --root "${root_name}"
  --metadata "${backend_metadata}"
  --workspace "${workspace}"
  --project-dir "${runtime_project}"
)
if [[ -n "${lock_proof}" ]]; then
  backend_attestation_args+=(--lock-proof "${lock_proof}")
fi
"${PYTHON_BIN}" "${runtime_safety_bin}" \
  "${backend_attestation_args[@]}" >"${backend_attestation}"

plan_sha256="$(sha256sum "${plan_path}" | awk '{print $1}')"
backend_identity_sha256="$(
  "${PYTHON_BIN}" -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["identity_sha256"])
' "${backend_attestation}"
)"
backend_registry_sha256="$(
  "${PYTHON_BIN}" -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["backend_registry_sha256"])
' "${backend_attestation}"
)"
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
  "${PYTHON_BIN}" "${runtime_lease_bin}" transition \
    --root "${root_name}" \
    --metadata "${backend_metadata}" \
    --token-file "${lease_token_file}" \
    --phase "$1"
}

assert_operation_lease() {
  [[ "${lease_owned}" == true ]] || return 0
  "${PYTHON_BIN}" "${runtime_lease_bin}" assert-held \
    --root "${root_name}" \
    --metadata "${backend_metadata}" \
    --token-file "${lease_token_file}"
}

run_snapshot() {
  local validation="$1"
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
  "${snapshot_environment[@]}" "${runtime_snapshot_bin}" \
    --root "${root_name}" \
    --backend-config "${backend_config}" \
    --state-validation "${validation}" \
    --recipient-file "${recipient_file}" \
    --output-dir "${snapshot_output_dir}"
}

lease_token_file="$(
  printf '%s/terraform-apply-%s-%s.json' \
    "${PROJECT_DIR}/.terraform.d/leases" \
    "$(date -u +%Y%m%dT%H%M%S.%NZ)" \
    "${RANDOM}"
)"
"${PYTHON_BIN}" "${runtime_lease_bin}" acquire \
  --root "${root_name}" \
  --metadata "${backend_metadata}" \
  --token-file "${lease_token_file}" \
  --source-commit "${revision}" \
  --operation apply \
  --backend-identity-sha256 "${backend_identity_sha256}" \
  --registry-sha256 "${backend_registry_sha256}" \
  --plan-sha256 "${plan_sha256}"
lease_owned=true

state_before="${attestation_dir}/state-before.json"
operation_mode="$(
  "${PYTHON_BIN}" -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    document = json.load(stream)
mode = document.get("operation_mode")
if mode not in {"established", "initialize"}:
    raise SystemExit("ERROR: invalid sidecar operation mode")
print(mode)
' "${sidecar_path}"
)"
if [[ "${operation_mode}" == "initialize" ]]; then
  state_list_error="${attestation_dir}/state-list.error"
  set +e
  state_inventory="$(
    "${TERRAFORM_BIN}" -chdir="${terraform_root}" state list \
      -no-color 2>"${state_list_error}"
  )"
  state_list_status=$?
  set -e
  if [[ "${state_list_status}" -eq 1 ]] &&
    "${PYTHON_BIN}" "${runtime_safety_bin}" verify-empty-state-probe \
      --stderr "${state_list_error}" >/dev/null; then
    :
  elif [[ "${state_list_status}" -eq 0 ]]; then
    fail "initialize apply requires absence of any existing state object"
  else
    fail "cannot prove that the initialization backend is empty"
  fi
  [[ -z "${state_inventory}" ]] ||
    fail "initialize apply received an unexpected state inventory"
  state_output_stderr="${attestation_dir}/state-output.stderr"
  state_outputs="$(
    "${TERRAFORM_BIN}" -chdir="${terraform_root}" output -json \
      2>"${state_output_stderr}"
  )"
  assert_empty_file "${state_output_stderr}" "Terraform output"
  [[ "${state_outputs}" == "{}" ]] ||
    fail "initialize apply requires a backend with no state outputs"
  "${PYTHON_BIN}" "${runtime_safety_bin}" attest-empty-state \
    --root "${root_name}" >"${state_before}"
else
  state_pull_before_stderr="${attestation_dir}/state-pull-before.stderr"
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" state pull \
    2>"${state_pull_before_stderr}" |
    "${PYTHON_BIN}" "${runtime_safety_bin}" attest-state \
      --root "${root_name}" >"${state_before}"
  assert_empty_file \
    "${state_pull_before_stderr}" \
    "Terraform pre-apply state pull"
fi

expected_confirmation="APPLY:${root_name}:${revision}:${plan_sha256}:${recipient_sha256}"
if [[ "${confirmation}" != "${expected_confirmation}" ]]; then
  printf 'Required confirmation: %s\n' "${expected_confirmation}" >&2
  fail "typed confirmation does not bind root, commit, plan, and snapshot key"
fi

reviewed_plan_document="${attestation_dir}/reviewed-plan.json"
reviewed_plan_stderr="${attestation_dir}/reviewed-plan.stderr"
"${TERRAFORM_BIN}" -chdir="${terraform_root}" show \
  -json "${plan_path}" >"${reviewed_plan_document}" \
  2>"${reviewed_plan_stderr}"
assert_empty_file "${reviewed_plan_stderr}" "Terraform show saved plan"
"${PYTHON_BIN}" "${runtime_safety_bin}" verify-sidecar \
    --root "${root_name}" \
    --sidecar "${sidecar_path}" \
    --plan-sha256 "${plan_sha256}" \
    --source-commit "${revision}" \
    --backend-attestation "${backend_attestation}" \
    --state-attestation "${state_before}" \
    --project-dir "${runtime_project}" \
    <"${reviewed_plan_document}" >/dev/null

lease_transition prestate_verified
[[ -z "${caught_signal}" ]] ||
  fail "operation interrupted before writer_started"

if [[ "${operation_mode}" == "established" ]]; then
  run_snapshot contract
else
  printf '%s\n' \
    "No pre-apply state exists; the saved sidecar contains its empty-state attestation."
fi

assert_operation_lease
[[ -z "${caught_signal}" ]] ||
  fail "operation interrupted before writer_started"
lease_transition writer_started
writer_started=true
if [[ -n "${caught_signal}" ]]; then
  fail "operation interrupted at writer_started; lease quarantined"
fi
set +e
apply_events="${attestation_dir}/terraform-apply.ndjson"
apply_stderr="${attestation_dir}/terraform-apply.stderr"
apply_ui_attestation="${attestation_dir}/terraform-apply-ui.json"
"${TERRAFORM_BIN}" -chdir="${terraform_root}" apply \
  -json \
  -input=false \
  -lock-timeout=60s \
  "${plan_path}" >"${apply_events}" 2>"${apply_stderr}" &
writer_pid=$!
wait "${writer_pid}"
apply_status=$?
while kill -0 "${writer_pid}" 2>/dev/null; do
  wait "${writer_pid}"
  apply_status=$?
done
writer_pid=""
"${PYTHON_BIN}" "${runtime_safety_bin}" attest-terraform-ui \
  --operation apply \
  --events "${apply_events}" \
  --stderr "${apply_stderr}" >"${apply_ui_attestation}"
apply_ui_status=$?
set -e
lease_transition writer_finished
post_state_validation="contract"
if [[ "${apply_status}" -ne 0 ||
  "${apply_ui_status}" -ne 0 ||
  -n "${caught_signal}" ]]; then
  post_state_validation="structural"
fi
set +e
run_snapshot "${post_state_validation}"
post_snapshot_status=$?
set -e

if [[ "${post_snapshot_status}" -eq 0 ]]; then
  lease_transition snapshot_verified
fi
[[ -z "${caught_signal}" ]] ||
  fail "Terraform apply was interrupted by ${caught_signal}; lease quarantined"
[[ "${apply_status}" -eq 0 ]] ||
  fail "Terraform apply failed; post-attempt snapshot status=${post_snapshot_status}"
[[ "${apply_ui_status}" -eq 0 ]] ||
  fail "Terraform apply emitted a warning, error, or malformed UI stream; lease quarantined"
[[ "${post_snapshot_status}" -eq 0 ]] ||
  fail "Terraform apply succeeded but the mandatory post-apply snapshot failed"

state_after="${attestation_dir}/state-after.json"
state_pull_after_stderr="${attestation_dir}/state-pull-after.stderr"
"${TERRAFORM_BIN}" -chdir="${terraform_root}" state pull \
  2>"${state_pull_after_stderr}" |
  "${PYTHON_BIN}" "${runtime_safety_bin}" attest-state \
    --root "${root_name}" >"${state_after}"
assert_empty_file \
  "${state_pull_after_stderr}" \
  "Terraform post-apply state pull"
"${PYTHON_BIN}" "${runtime_safety_bin}" verify-post-state \
  --before "${state_before}" \
  --after "${state_after}" \
  --sidecar "${sidecar_path}" >/dev/null

state_show_document="${attestation_dir}/state-after-show.json"
state_show_stderr="${attestation_dir}/state-after-show.stderr"
"${TERRAFORM_BIN}" -chdir="${terraform_root}" show -json \
  >"${state_show_document}" 2>"${state_show_stderr}"
assert_empty_file "${state_show_stderr}" "Terraform show post-apply state"
"${PYTHON_BIN}" "${runtime_safety_bin}" verify-post-apply-checks \
  --root "${root_name}" \
  --document "${state_show_document}" \
  --sidecar "${sidecar_path}" >/dev/null

lease_transition poststate_verified
if [[ "${lease_owned}" == true ]]; then
  "${PYTHON_BIN}" "${runtime_lease_bin}" release \
    --root "${root_name}" \
    --metadata "${backend_metadata}" \
    --token-file "${lease_token_file}"
  lease_owned=false
fi
writer_started=false

printf 'Terraform apply completed with verified pre/post state snapshots.\n'
printf 'Root: %s\n' "${root_name}"
printf 'Source commit: %s\n' "${revision}"
printf 'Plan SHA-256: %s\n' "${plan_sha256}"

cleanup
trap - EXIT
