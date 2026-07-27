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
readonly PLAN_ALLOWED_SIGNERS="${PROJECT_DIR}/infra/terraform/plan.allowed-signers"
readonly BACKEND_IDENTITIES="${PROJECT_DIR}/infra/terraform/backend-identities.json"
readonly LOCK_ALLOWED_SIGNERS="${PROJECT_DIR}/infra/terraform/lock-proof.allowed-signers"
readonly PLAN_SIGNER_IDENTITY="apptolast-terraform-plan"
readonly PLAN_SIGNATURE_NAMESPACE="terraform-saved-plan"

root_name=""
backend_config=""
var_file=""
output_plan=""
backend_mode=""
lock_proof=""
initialize_confirmation=""
signing_key=""

usage() {
  cat <<'EOF'
Usage:
  plan-terraform.sh --root ROOT --backend-config PATH
      --backend-mode established|initialize [--lock-proof PATH]
      --signing-key PATH
      [--confirm-initialize 'INITIALIZE:ROOT:COMMIT:BACKEND_SHA256']
      [--var-file PATH] [--out PATH]

ROOT is one of:
  cloudflare/state-bootstrap
  cloudflare/apptolast-dns
  netcup/perimeter

established requires the exact root identity already present and rejects every
unmodeled state address. initialize requires a truly empty backend plus an
explicit confirmation bound to root, clean commit, and backend identity. Its
saved non-destructive plan may perform reviewed import blocks and creates the
root identity atomically. State migration uses migrate-terraform-state.sh.

Remote R2 roots also require --lock-proof: a current two-client proof bound to
the exact bucket, endpoint, and pinned Terraform version.

The worktree must be clean so every saved plan maps to one Git commit.
Terraform's detailed exit code is preserved: 0 means no changes, 2 means the
reviewed plan contains changes, and 1 means an error.
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
    --backend-mode)
      (( $# >= 2 )) || fail "--backend-mode requires a value"
      backend_mode="$2"
      shift 2
      ;;
    --lock-proof)
      (( $# >= 2 )) || fail "--lock-proof requires a path"
      lock_proof="$2"
      shift 2
      ;;
    --confirm-initialize)
      (( $# >= 2 )) || fail "--confirm-initialize requires a value"
      initialize_confirmation="$2"
      shift 2
      ;;
    --signing-key)
      (( $# >= 2 )) || fail "--signing-key requires a path"
      signing_key="$2"
      shift 2
      ;;
    --var-file)
      (( $# >= 2 )) || fail "--var-file requires a path"
      var_file="$2"
      shift 2
      ;;
    --out)
      (( $# >= 2 )) || fail "--out requires a path"
      output_plan="$2"
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
case "${backend_mode}" in
  established|initialize)
    ;;
  *)
    fail "--backend-mode must be established or initialize"
    ;;
esac
if [[ "${backend_mode}" == "established" ]]; then
  [[ -z "${initialize_confirmation}" ]] ||
    fail "--confirm-initialize is valid only in initialize mode"
fi

for command_name in awk flock git grep install mktemp mv realpath sha256sum ssh-keygen stat tar; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done
[[ -x "${TERRAFORM_BIN}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -x "${PYTHON_BIN}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -f "${SAFETY_BIN}" && ! -L "${SAFETY_BIN}" ]] ||
  fail "Terraform safety helper is absent or unsafe"
[[ -f "${PLAN_ALLOWED_SIGNERS}" && ! -L "${PLAN_ALLOWED_SIGNERS}" ]] ||
  fail "approved tracked plan signer registry is absent"
git -C "${PROJECT_DIR}" ls-files --error-unmatch \
  "infra/terraform/plan.allowed-signers" >/dev/null ||
  fail "plan.allowed-signers must be tracked"
[[ -f "${BACKEND_IDENTITIES}" && ! -L "${BACKEND_IDENTITIES}" ]] ||
  fail "approved tracked backend identity registry is absent"
git -C "${PROJECT_DIR}" ls-files --error-unmatch \
  "infra/terraform/backend-identities.json" >/dev/null ||
  fail "backend-identities.json must be tracked"
[[ -f "${backend_config}" && ! -L "${backend_config}" ]] ||
  fail "the backend config must be a regular, non-symlink file"
[[ -f "${signing_key}" && ! -L "${signing_key}" ]] ||
  fail "the saved-plan signing key must be a regular, non-symlink file"
case "$(stat --format='%a' "${signing_key}")" in
  400|600)
    ;;
  *)
    fail "the saved-plan signing key must be mode 0400 or 0600"
    ;;
esac
signing_key="$(realpath "${signing_key}")"
backend_config="$(realpath "${backend_config}")"
if [[ -n "${lock_proof}" ]]; then
  [[ -f "${LOCK_ALLOWED_SIGNERS}" && ! -L "${LOCK_ALLOWED_SIGNERS}" ]] ||
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
if [[ -n "${var_file}" ]]; then
  [[ -f "${var_file}" && ! -L "${var_file}" ]] ||
    fail "the var file must be a regular, non-symlink file"
  var_file="$(realpath "${var_file}")"
fi

worktree_status="$(
  git -C "${PROJECT_DIR}" status --porcelain --untracked-files=normal
)"
[[ -z "${worktree_status}" ]] ||
  fail "commit or discard every worktree change before a production plan"

revision="$(git -C "${PROJECT_DIR}" rev-parse --verify HEAD)"
[[ "${revision}" =~ ^[a-f0-9]{40}$ ]] ||
  fail "cannot resolve the source commit"

terraform_root="${PROJECT_DIR}/infra/terraform/${root_name}"
[[ -d "${terraform_root}" ]] ||
  fail "Terraform root not found: ${terraform_root}"
implicit_variable_files=()
for implicit_candidate in \
  "${terraform_root}/terraform.tfvars" \
  "${terraform_root}/terraform.tfvars.json"; do
  if [[ -e "${implicit_candidate}" || -L "${implicit_candidate}" ]]; then
    implicit_variable_files+=("${implicit_candidate}")
  fi
done
shopt -s nullglob
implicit_variable_files+=(
  "${terraform_root}"/*.auto.tfvars
  "${terraform_root}"/*.auto.tfvars.json
)
shopt -u nullglob
(( ${#implicit_variable_files[@]} == 0 )) ||
  fail "implicit Terraform variable files are forbidden; use --var-file"

safe_root="${root_name//\//-}"
terraform_data_dir="$(mktemp -d)"
plugin_cache_dir="${PROJECT_DIR}/.terraform.d/plugin-cache"
install --directory --mode=0750 \
  "${plugin_cache_dir}" \
  "${PROJECT_DIR}/.build/plans"

exec 9>"${PROJECT_DIR}/.terraform.d/terraform-operation.lock"
flock --nonblock 9 ||
  fail "another local Terraform operation holds the repository lock"

if [[ -z "${output_plan}" ]]; then
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  output_plan="${PROJECT_DIR}/.build/plans/${safe_root}-${timestamp}.tfplan"
fi
output_plan="$(realpath -m "${output_plan}")"
case "${output_plan}" in
  "${PROJECT_DIR}/.build/plans/"*.tfplan)
    ;;
  *)
    fail "--out must be a .tfplan file below .build/plans"
    ;;
esac
[[ ! -e "${output_plan}" ]] ||
  fail "refusing to overwrite existing plan: ${output_plan}"
sidecar_path="${output_plan}.metadata.json"
[[ ! -e "${sidecar_path}" ]] ||
  fail "refusing to overwrite existing plan sidecar: ${sidecar_path}"
sidecar_signature_path="${sidecar_path}.sig"
[[ ! -e "${sidecar_signature_path}" ]] ||
  fail "refusing to overwrite existing plan signature: ${sidecar_signature_path}"

sanitize_terraform_environment
reject_unsafe_cloud_environment
export TF_DATA_DIR="${terraform_data_dir}"
export TF_IN_AUTOMATION=1
export TF_INPUT=0
export TF_CLI_CONFIG_FILE=/dev/null
export TF_PLUGIN_CACHE_DIR="${plugin_cache_dir}"
export TF_WORKSPACE=default

attestation_dir="$(
  mktemp -d "${PROJECT_DIR}/.build/plans/.terraform-plan-build.XXXXXX"
)"
plan_created=false
retain_plan=false
cleanup() {
  [[ ! -d "${terraform_data_dir}" ]] ||
    find "${terraform_data_dir}" -depth -delete
  [[ ! -d "${attestation_dir}" ]] ||
    find "${attestation_dir}" -depth -delete
  if [[ "${plan_created}" == true && "${retain_plan}" == false ]]; then
    [[ ! -f "${output_plan}" ]] ||
      find "${output_plan}" -maxdepth 0 -type f -delete
    [[ ! -f "${sidecar_path}" ]] ||
      find "${sidecar_path}" -maxdepth 0 -type f -delete
    [[ ! -f "${sidecar_signature_path}" ]] ||
      find "${sidecar_signature_path}" -maxdepth 0 -type f -delete
  fi
}
trap cleanup EXIT

runtime_project="${attestation_dir}/source"
install --directory --mode=0700 "${runtime_project}"
git -C "${PROJECT_DIR}" archive --format=tar "${revision}" \
  config \
  infra/terraform \
  scripts/r2-cross-credential-probe.py \
  scripts/r2-operation-lease.py \
  scripts/terraform-safety.py \
  scripts/test-terraform-r2-locking.sh |
  tar --extract --directory="${runtime_project}"
terraform_root="${runtime_project}/infra/terraform/${root_name}"
runtime_safety_bin="${runtime_project}/scripts/terraform-safety.py"
[[ -d "${terraform_root}" &&
  -f "${runtime_safety_bin}" &&
  ! -L "${runtime_safety_bin}" ]] ||
  fail "cannot materialize the committed Terraform source"

implicit_variable_files=()
for implicit_candidate in \
  "${terraform_root}/terraform.tfvars" \
  "${terraform_root}/terraform.tfvars.json"; do
  if [[ -e "${implicit_candidate}" || -L "${implicit_candidate}" ]]; then
    implicit_variable_files+=("${implicit_candidate}")
  fi
done
shopt -s nullglob
implicit_variable_files+=(
  "${terraform_root}"/*.auto.tfvars
  "${terraform_root}"/*.auto.tfvars.json
)
shopt -u nullglob
(( ${#implicit_variable_files[@]} == 0 )) ||
  fail "committed implicit variable files are forbidden; use --var-file"

install --mode=0600 "${backend_config}" \
  "${attestation_dir}/backend.tfbackend"
backend_config="${attestation_dir}/backend.tfbackend"
if [[ -n "${lock_proof}" ]]; then
  install --mode=0600 "${lock_proof}" \
    "${attestation_dir}/locking-proof.json"
  install --mode=0600 "${lock_proof}.sig" \
    "${attestation_dir}/locking-proof.json.sig"
  lock_proof="${attestation_dir}/locking-proof.json"
fi
if [[ -n "${var_file}" ]]; then
  install --mode=0600 "${var_file}" "${attestation_dir}/variables.tfvars"
  var_file="${attestation_dir}/variables.tfvars"
fi
install --mode=0400 "${signing_key}" \
  "${attestation_dir}/plan-signing-key"
signing_key="${attestation_dir}/plan-signing-key"

init_args=(
  -backend-config="${backend_config}"
  -input=false
  -lockfile=readonly
  -reconfigure
)

init_events="${attestation_dir}/terraform-init.ndjson"
init_stderr="${attestation_dir}/terraform-init.stderr"
"${TERRAFORM_BIN}" -chdir="${terraform_root}" init \
  -json "${init_args[@]}" >"${init_events}" 2>"${init_stderr}"
"${PYTHON_BIN}" "${runtime_safety_bin}" attest-terraform-ui \
  --operation init \
  --events "${init_events}" \
  --stderr "${init_stderr}" \
  --root "${root_name}" >/dev/null

workspace_stderr="${attestation_dir}/workspace.stderr"
workspace="$(
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" workspace show \
    2>"${workspace_stderr}"
)"
assert_empty_file "${workspace_stderr}" "Terraform workspace show"
[[ "${workspace}" == "default" ]] ||
  fail "production plans require the default Terraform workspace"
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

state_attestation="${attestation_dir}/state.json"
if [[ "${backend_mode}" == "initialize" ]]; then
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
    fail "initialize mode requires absence of any existing state object"
  else
    fail "cannot prove that the initialization backend is empty"
  fi
  [[ -z "${state_inventory}" ]] ||
    fail "initialize mode received an unexpected state inventory"
  state_output_stderr="${attestation_dir}/state-output.stderr"
  state_outputs="$(
    "${TERRAFORM_BIN}" -chdir="${terraform_root}" output -json \
      2>"${state_output_stderr}"
  )"
  assert_empty_file "${state_output_stderr}" "Terraform output"
  [[ "${state_outputs}" == "{}" ]] ||
    fail "initialize mode requires a backend with no state outputs"
  "${PYTHON_BIN}" "${runtime_safety_bin}" attest-empty-state \
    --root "${root_name}" >"${state_attestation}"
  backend_identity_sha256="$(
    "${PYTHON_BIN}" -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["identity_sha256"])
' "${backend_attestation}"
  )"
  expected_initialize_confirmation="INITIALIZE:${root_name}:${revision}:${backend_identity_sha256}"
  if [[ "${initialize_confirmation}" != "${expected_initialize_confirmation}" ]]; then
    printf 'Required confirmation: %s\n' \
      "${expected_initialize_confirmation}" >&2
    fail "initialize confirmation does not bind this root, commit, and backend"
  fi
else
  state_pull_stderr="${attestation_dir}/state-pull.stderr"
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" state pull \
    2>"${state_pull_stderr}" |
    "${PYTHON_BIN}" "${runtime_safety_bin}" attest-state \
      --root "${root_name}" >"${state_attestation}"
  assert_empty_file "${state_pull_stderr}" "Terraform state pull"
fi
validate_document="${attestation_dir}/terraform-validate.json"
validate_stderr="${attestation_dir}/terraform-validate.stderr"
"${TERRAFORM_BIN}" -chdir="${terraform_root}" validate -json \
  >"${validate_document}" 2>"${validate_stderr}"
"${PYTHON_BIN}" "${runtime_safety_bin}" verify-terraform-validate \
  --document "${validate_document}" \
  --stderr "${validate_stderr}" >/dev/null

plan_args=(
  -detailed-exitcode
  -input=false
  -lock-timeout=60s
  -out="${output_plan}"
)
if [[ -n "${var_file}" ]]; then
  plan_args+=("-var-file=${var_file}")
fi

set +e
plan_events="${attestation_dir}/terraform-plan.ndjson"
plan_stderr="${attestation_dir}/terraform-plan.stderr"
plan_ui_attestation="${attestation_dir}/terraform-plan-ui.json"
"${TERRAFORM_BIN}" -chdir="${terraform_root}" plan \
  -json "${plan_args[@]}" >"${plan_events}" 2>"${plan_stderr}"
plan_status=$?
"${PYTHON_BIN}" "${runtime_safety_bin}" attest-terraform-ui \
  --operation plan \
  --events "${plan_events}" \
  --stderr "${plan_stderr}" \
  --root "${root_name}" >"${plan_ui_attestation}"
plan_ui_status=$?
set -e
if [[ -f "${output_plan}" ]]; then
  plan_created=true
fi
[[ "${plan_ui_status}" -eq 0 ]] ||
  fail "Terraform plan emitted a warning, error, or malformed UI stream"

case "${plan_status}" in
  0)
    printf 'No Terraform changes for %s at commit %s.\n' \
      "${root_name}" "${revision}"
    ;;
  2)
    plan_sha256="$(sha256sum "${output_plan}" | awk '{print $1}')"
    variable_file_sha256="none"
    if [[ -n "${var_file}" ]]; then
      variable_file_sha256="$(
        sha256sum "${var_file}" | awk '{print $1}'
      )"
    fi
    temporary_sidecar="${attestation_dir}/plan.metadata.json"
    plan_document="${attestation_dir}/terraform-plan.json"
    plan_show_stderr="${attestation_dir}/terraform-plan-show.stderr"
    "${TERRAFORM_BIN}" -chdir="${terraform_root}" show \
      -json "${output_plan}" >"${plan_document}" 2>"${plan_show_stderr}"
    assert_empty_file "${plan_show_stderr}" "Terraform show saved plan"
    "${PYTHON_BIN}" "${runtime_safety_bin}" build-sidecar \
        --root "${root_name}" \
        --plan-sha256 "${plan_sha256}" \
        --source-commit "${revision}" \
        --backend-attestation "${backend_attestation}" \
        --state-attestation "${state_attestation}" \
        --ui-attestation "${plan_ui_attestation}" \
        --variable-file-sha256 "${variable_file_sha256}" \
        --operation-mode "${backend_mode}" \
        <"${plan_document}" >"${temporary_sidecar}"
    ssh-keygen -Y sign \
      -f "${signing_key}" \
      -n "${PLAN_SIGNATURE_NAMESPACE}" \
      "${temporary_sidecar}" >/dev/null
    temporary_signature="${temporary_sidecar}.sig"
    [[ -s "${temporary_signature}" && ! -L "${temporary_signature}" ]] ||
      fail "ssh-keygen did not create a safe saved-plan signature"
    ssh-keygen -Y verify \
      -f "${runtime_project}/infra/terraform/plan.allowed-signers" \
      -I "${PLAN_SIGNER_IDENTITY}" \
      -n "${PLAN_SIGNATURE_NAMESPACE}" \
      -s "${temporary_signature}" <"${temporary_sidecar}" >/dev/null ||
      fail "the signing key is not approved by plan.allowed-signers"
    chmod 0600 \
      "${output_plan}" \
      "${temporary_sidecar}" \
      "${temporary_signature}"
    mv --no-clobber "${temporary_sidecar}" "${sidecar_path}"
    [[ ! -e "${temporary_sidecar}" &&
      -f "${sidecar_path}" &&
      ! -L "${sidecar_path}" ]] ||
      fail "saved plan sidecar was not published atomically"
    mv --no-clobber \
      "${temporary_signature}" \
      "${sidecar_signature_path}"
    [[ ! -e "${temporary_signature}" &&
      -f "${sidecar_signature_path}" &&
      ! -L "${sidecar_signature_path}" ]] ||
      fail "saved plan signature was not published atomically"
    retain_plan=true
    printf 'Reviewable Terraform plan created: %s\n' "${output_plan}"
    printf 'Fail-closed plan sidecar created: %s\n' "${sidecar_path}"
    printf 'Approved saved-plan signature created: %s\n' \
      "${sidecar_signature_path}"
    printf 'Source commit: %s\n' "${revision}"
    printf 'Plan SHA-256: %s\n' "${plan_sha256}"
    printf '%s\n' \
      "Exit status 2 is expected when the plan contains changes." \
      "Treat the plan as sensitive and never commit it."
    ;;
  *)
    fail "terraform plan failed for ${root_name}"
    ;;
esac

cleanup
trap - EXIT
exit "${plan_status}"
