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
readonly CREDENTIAL_PROBE_BIN="${PROJECT_DIR}/scripts/r2-cross-credential-probe.py"
readonly LEASE_BIN="${PROJECT_DIR}/scripts/r2-operation-lease.py"
readonly PROBE_ROOT="${PROJECT_DIR}/infra/terraform/testing/r2-lock"
readonly ALLOWED_SIGNERS="${PROJECT_DIR}/infra/terraform/lock-proof.allowed-signers"
readonly BACKEND_IDENTITIES="${PROJECT_DIR}/infra/terraform/backend-identities.json"
readonly SIGNER_IDENTITY="apptolast-terraform-lock-proof"
readonly SIGNATURE_NAMESPACE="terraform-r2-lock-proof"

backend_config=""
other_backend_config=""
root_name=""
backend_role=""
other_root_name=""
other_access_key_file=""
other_secret_key_file=""
operator=""
proof_output=""
signing_key=""

usage() {
  cat <<'EOF'
Usage:
  test-terraform-r2-locking.sh --root ROOT
      --backend-role production|pending_destination
      --backend-config PATH
      --other-root ROOT
      --other-backend-config PATH
      --other-access-key-file PATH --other-secret-key-file PATH
      --operator NAME --proof-output PATH --signing-key PATH

The primary backend config must use the selected root's registered production
or pending_destination bucket but a disposable key below lock-tests/, with
use_lockfile=true. The other backend config must use the other root's production
bucket. Primary bucket credentials come only from
AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY. The two 0600 files contain the other
root's R2 credentials. Those credentials must first pass read, write, list, and
delete controls in their own bucket, then receive exact AccessDenied responses
for those operations against the primary bucket.

The probe creates only terraform_data in the disposable state key. It tests two
clients, normal release, stale-lock recovery after SIGKILL, and credential
isolation. It destroys terraform_data at the end and writes a non-secret,
24-hour proof only if every check succeeds. Never point it at a production state
key. The proof and its adjacent .sig are accepted only when signed by the
explicitly approved key in the tracked lock-proof.allowed-signers registry.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
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

while (( $# > 0 )); do
  case "$1" in
    --root)
      (( $# >= 2 )) || fail "--root requires a value"
      root_name="$2"
      shift 2
      ;;
    --backend-role)
      (( $# >= 2 )) || fail "--backend-role requires a value"
      backend_role="$2"
      shift 2
      ;;
    --backend-config)
      (( $# >= 2 )) || fail "--backend-config requires a path"
      backend_config="$2"
      shift 2
      ;;
    --other-backend-config)
      (( $# >= 2 )) || fail "--other-backend-config requires a path"
      other_backend_config="$2"
      shift 2
      ;;
    --other-root)
      (( $# >= 2 )) || fail "--other-root requires a value"
      other_root_name="$2"
      shift 2
      ;;
    --other-access-key-file)
      (( $# >= 2 )) || fail "--other-access-key-file requires a path"
      other_access_key_file="$2"
      shift 2
      ;;
    --other-secret-key-file)
      (( $# >= 2 )) || fail "--other-secret-key-file requires a path"
      other_secret_key_file="$2"
      shift 2
      ;;
    --operator)
      (( $# >= 2 )) || fail "--operator requires a value"
      operator="$2"
      shift 2
      ;;
    --proof-output)
      (( $# >= 2 )) || fail "--proof-output requires a path"
      proof_output="$2"
      shift 2
      ;;
    --signing-key)
      (( $# >= 2 )) || fail "--signing-key requires a path"
      signing_key="$2"
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

case "${root_name}:${other_root_name}" in
  cloudflare/apptolast-dns:netcup/perimeter|netcup/perimeter:cloudflare/apptolast-dns)
    ;;
  *)
    fail "--root and --other-root must be the two distinct reviewed remote roots"
    ;;
esac
case "${backend_role}" in
  production|pending_destination)
    ;;
  *)
    fail "--backend-role must be production or pending_destination"
    ;;
esac

for command_name in chmod date dirname env find flock git grep head install kill mktemp mv realpath sed sleep ssh-keygen stat tar; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done
[[ -x "${TERRAFORM_BIN}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -x "${PYTHON_BIN}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -f "${SAFETY_BIN}" && ! -L "${SAFETY_BIN}" ]] ||
  fail "Terraform safety helper is absent or unsafe"
[[ -f "${CREDENTIAL_PROBE_BIN}" && ! -L "${CREDENTIAL_PROBE_BIN}" ]] ||
  fail "R2 cross-credential probe is absent or unsafe"
[[ -f "${LEASE_BIN}" && ! -L "${LEASE_BIN}" ]] ||
  fail "R2 operation lease helper is absent or unsafe"
[[ -d "${PROBE_ROOT}" && ! -L "${PROBE_ROOT}" ]] ||
  fail "R2 lock probe root is absent or unsafe"
[[ -f "${ALLOWED_SIGNERS}" && ! -L "${ALLOWED_SIGNERS}" ]] ||
  fail "approved tracked lock-proof signer registry is absent"
git -C "${PROJECT_DIR}" ls-files --error-unmatch \
  "infra/terraform/lock-proof.allowed-signers" >/dev/null ||
  fail "lock-proof.allowed-signers must be tracked"
[[ -f "${BACKEND_IDENTITIES}" && ! -L "${BACKEND_IDENTITIES}" ]] ||
  fail "approved tracked backend identity registry is absent"
git -C "${PROJECT_DIR}" ls-files --error-unmatch \
  "infra/terraform/backend-identities.json" >/dev/null ||
  fail "backend-identities.json must be tracked"
[[ -n "${operator}" ]] || fail "--operator must not be empty"
[[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" ]] ||
  fail "primary R2 credentials are absent from the environment"
for aws_selector in \
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
  [[ -z "${!aws_selector:-}" ]] ||
    fail "alternate AWS credential selector is forbidden: ${aws_selector}"
done

for input_file in \
  "${backend_config}" \
  "${other_backend_config}" \
  "${other_access_key_file}" \
  "${other_secret_key_file}" \
  "${signing_key}"; do
  [[ -f "${input_file}" && ! -L "${input_file}" ]] ||
    fail "input must be a regular, non-symlink file: ${input_file}"
done
[[ "$(stat --format='%a' "${other_access_key_file}")" == "600" ]] ||
  fail "other access-key file must be mode 0600"
[[ "$(stat --format='%a' "${other_secret_key_file}")" == "600" ]] ||
  fail "other secret-key file must be mode 0600"
case "$(stat --format='%a' "${signing_key}")" in
  400|600)
    ;;
  *)
    fail "locking-proof signing key must be mode 0400 or 0600"
    ;;
esac
backend_config="$(realpath "${backend_config}")"
other_backend_config="$(realpath "${other_backend_config}")"
other_access_key_file="$(realpath "${other_access_key_file}")"
other_secret_key_file="$(realpath "${other_secret_key_file}")"
signing_key="$(realpath "${signing_key}")"
proof_output="$(realpath -m "${proof_output}")"
[[ ! -e "${proof_output}" ]] ||
  fail "refusing to overwrite an existing locking proof"
[[ ! -e "${proof_output}.sig" ]] ||
  fail "refusing to overwrite an existing locking proof signature"
project_real="$(realpath "${PROJECT_DIR}")"
case "${proof_output}" in
  "${project_real}"|"${project_real}/"*)
    fail "locking proofs are expiring operational evidence and stay outside Git"
    ;;
esac
install --directory --mode=0700 "$(dirname "${proof_output}")"
install --directory --mode=0750 "${PROJECT_DIR}/.terraform.d"

exec 9>"${PROJECT_DIR}/.terraform.d/terraform-operation.lock"
flock --nonblock 9 ||
  fail "another local Terraform operation holds the repository lock"

worktree_status="$(
  git -C "${PROJECT_DIR}" status --porcelain --untracked-files=normal
)"
[[ -z "${worktree_status}" ]] ||
  fail "commit or discard every worktree change before testing R2 locking"
revision="$(git -C "${PROJECT_DIR}" rev-parse --verify HEAD)"
[[ "${revision}" =~ ^[a-f0-9]{40}$ ]] ||
  fail "cannot resolve the source commit"

probe_dir="$(mktemp -d)"
client_a="${probe_dir}/client-a"
client_b="${probe_dir}/client-b"
client_other_authorized="${probe_dir}/client-other-authorized"
client_other_denied="${probe_dir}/client-other-denied"
install --directory --mode=0700 \
  "${client_a}" \
  "${client_b}" \
  "${client_other_authorized}" \
  "${client_other_denied}"
apply_pid=""
proof_tmp=""
proof_signature_tmp=""
cleanup() {
  if [[ -n "${apply_pid}" ]] && kill -0 "${apply_pid}" 2>/dev/null; then
    kill -KILL "${apply_pid}" 2>/dev/null || true
    wait "${apply_pid}" 2>/dev/null || true
  fi
  [[ -z "${proof_tmp}" || ! -f "${proof_tmp}" ]] ||
    find "${proof_tmp}" -maxdepth 0 -type f -delete
  [[ -z "${proof_signature_tmp}" || ! -f "${proof_signature_tmp}" ]] ||
    find "${proof_signature_tmp}" -maxdepth 0 -type f -delete
  [[ ! -d "${probe_dir}" ]] ||
    find "${probe_dir}" -depth -delete
}
trap cleanup EXIT

runtime_project="${probe_dir}/source"
install --directory --mode=0700 "${runtime_project}"
git -C "${PROJECT_DIR}" archive --format=tar "${revision}" \
  infra/terraform \
  scripts/r2-cross-credential-probe.py \
  scripts/r2-operation-lease.py \
  scripts/terraform-safety.py \
  scripts/test-terraform-r2-locking.sh |
  tar --extract --directory="${runtime_project}"
runtime_probe_root="${runtime_project}/infra/terraform/testing/r2-lock"
runtime_safety_bin="${runtime_project}/scripts/terraform-safety.py"
runtime_credential_probe_bin="$(
  realpath "${runtime_project}/scripts/r2-cross-credential-probe.py"
)"
runtime_lease_bin="${runtime_project}/scripts/r2-operation-lease.py"
runtime_allowed_signers="$(
  realpath "${runtime_project}/infra/terraform/lock-proof.allowed-signers"
)"
[[ -d "${runtime_probe_root}" &&
  -f "${runtime_safety_bin}" &&
  ! -L "${runtime_safety_bin}" &&
  -f "${runtime_credential_probe_bin}" &&
  ! -L "${runtime_credential_probe_bin}" &&
  -f "${runtime_lease_bin}" &&
  ! -L "${runtime_lease_bin}" &&
  -f "${runtime_allowed_signers}" &&
  ! -L "${runtime_allowed_signers}" ]] ||
  fail "cannot materialize the committed R2 locking harness"

sanitize_terraform_environment
export TF_IN_AUTOMATION=1
export TF_INPUT=0
export TF_WORKSPACE=default
export TF_CLI_CONFIG_FILE=/dev/null
export TF_PLUGIN_CACHE_DIR="${PROJECT_DIR}/.terraform.d/plugin-cache"
install --directory --mode=0750 "${TF_PLUGIN_CACHE_DIR}"

install --mode=0600 "${backend_config}" \
  "${probe_dir}/primary.tfbackend"
backend_config="${probe_dir}/primary.tfbackend"
install --mode=0600 "${other_backend_config}" \
  "${probe_dir}/other.tfbackend"
other_backend_config="${probe_dir}/other.tfbackend"

for client_dir in "${client_a}" "${client_b}"; do
  TF_DATA_DIR="${client_dir}" \
    "${TERRAFORM_BIN}" -chdir="${runtime_probe_root}" init \
      -backend-config="${backend_config}" \
      -input=false \
      -lockfile=readonly \
      -reconfigure >/dev/null
done

scope_document="${probe_dir}/scope.json"
"${PYTHON_BIN}" "${runtime_safety_bin}" locking-scope \
  --root "${root_name}" \
  --backend-role "${backend_role}" \
  --project-dir "${runtime_project}" \
  --metadata "${client_a}/terraform.tfstate" >"${scope_document}"
contract_document="${probe_dir}/contract.json"
"${PYTHON_BIN}" "${runtime_safety_bin}" locking-contract \
  --project-dir "${runtime_project}" >"${contract_document}"
"${PYTHON_BIN}" - "${client_a}/terraform.tfstate" <<'PY'
import json
from pathlib import Path
import re
import sys

metadata = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
key = metadata["backend"]["config"]["key"]
if not isinstance(key, str) or re.fullmatch(
    r"lock-tests/[a-zA-Z0-9._/-]+\.tfstate",
    key,
) is None:
    raise SystemExit("ERROR: backend key is not below lock-tests/")
if any(part in {"", ".", ".."} for part in key.split("/")):
    raise SystemExit("ERROR: backend key contains an unsafe path segment")
PY

initial_state_error="${probe_dir}/initial-state.error"
set +e
initial_inventory="$(
  TF_DATA_DIR="${client_a}" \
    "${TERRAFORM_BIN}" -chdir="${runtime_probe_root}" state list \
      -no-color 2>"${initial_state_error}"
)"
initial_state_status=$?
set -e
if [[ "${initial_state_status}" -ne 1 ]] ||
  ! grep -F "No state file was found!" "${initial_state_error}" >/dev/null; then
  fail "disposable locking key must have no pre-existing state object"
fi
[[ -z "${initial_inventory}" ]] ||
  fail "disposable locking key unexpectedly contains managed resources"
initial_outputs="$(
  TF_DATA_DIR="${client_a}" \
    "${TERRAFORM_BIN}" -chdir="${runtime_probe_root}" output -json
)"
[[ "${initial_outputs}" == "{}" ]] ||
  fail "disposable locking key unexpectedly contains outputs"

other_access_key="$(
  "${PYTHON_BIN}" -c \
    'from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text().strip())' \
    "${other_access_key_file}"
)"
other_secret_key="$(
  "${PYTHON_BIN}" -c \
    'from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text().strip())' \
    "${other_secret_key_file}"
)"
[[ -n "${other_access_key}" && -n "${other_secret_key}" ]] ||
  fail "cross-credential files must not be empty"

run_with_other_credentials() (
  export AWS_ACCESS_KEY_ID="${other_access_key}"
  export AWS_SECRET_ACCESS_KEY="${other_secret_key}"
  unset \
    AWS_CONFIG_FILE \
    AWS_CONTAINER_CREDENTIALS_FULL_URI \
    AWS_CONTAINER_CREDENTIALS_RELATIVE_URI \
    AWS_DEFAULT_PROFILE \
    AWS_PROFILE \
    AWS_ROLE_ARN \
    AWS_ROLE_SESSION_NAME \
    AWS_SECURITY_TOKEN \
    AWS_SESSION_TOKEN \
    AWS_SHARED_CREDENTIALS_FILE \
    AWS_WEB_IDENTITY_TOKEN_FILE
  "$@"
)

run_with_other_credentials \
  env TF_DATA_DIR="${client_other_authorized}" \
  "${TERRAFORM_BIN}" -chdir="${runtime_probe_root}" init \
    -backend-config="${other_backend_config}" \
    -input=false \
    -lockfile=readonly \
    -reconfigure >"${probe_dir}/other-authorized-init.log" 2>&1 ||
  fail "the cross credential cannot initialize its authorized control bucket"
other_scope_document="${probe_dir}/other-scope.json"
run_with_other_credentials \
  "${PYTHON_BIN}" "${runtime_safety_bin}" locking-scope \
    --root "${other_root_name}" \
    --backend-role production \
    --project-dir "${runtime_project}" \
    --metadata "${client_other_authorized}/terraform.tfstate" \
    >"${other_scope_document}"
"${PYTHON_BIN}" - "${client_other_authorized}/terraform.tfstate" <<'PY'
import json
from pathlib import Path
import re
import sys

metadata = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
key = metadata["backend"]["config"]["key"]
if not isinstance(key, str) or re.fullmatch(
    r"lock-tests/[a-zA-Z0-9._/-]+\.tfstate",
    key,
) is None:
    raise SystemExit("ERROR: other backend key is not below lock-tests/")
if any(part in {"", ".", ".."} for part in key.split("/")):
    raise SystemExit("ERROR: other backend key contains an unsafe path segment")
PY

set +e
run_with_other_credentials \
  env TF_DATA_DIR="${client_other_denied}" \
  "${TERRAFORM_BIN}" -chdir="${runtime_probe_root}" init \
    -backend-config="${backend_config}" \
    -input=false \
    -lockfile=readonly \
    -reconfigure >"${probe_dir}/other-init.log" 2>&1
other_status=$?
set -e
[[ "${other_status}" -ne 0 ]] ||
  fail "the other root credential can access this state bucket"
grep -F "AccessDenied" "${probe_dir}/other-init.log" >/dev/null ||
  fail "the other-credential backend init failed for a reason other than AccessDenied"
credential_results="${probe_dir}/credential-results.json"
credential_probe_key="lock-tests/credential-isolation/$(date -u +%Y%m%d%H%M%S)-${RANDOM}.probe"
"${PYTHON_BIN}" "${runtime_credential_probe_bin}" \
  --metadata "${client_a}/terraform.tfstate" \
  --other-metadata "${client_other_authorized}/terraform.tfstate" \
  --other-access-key-file "${other_access_key_file}" \
  --other-secret-key-file "${other_secret_key_file}" \
  --probe-key "${credential_probe_key}" >"${credential_results}"
unset other_access_key other_secret_key

lease_metadata="${probe_dir}/operation-lease-metadata.json"
"${PYTHON_BIN}" - \
  "${client_a}/terraform.tfstate" \
  "${root_name}" \
  "${lease_metadata}" <<'PY'
import json
from pathlib import Path
import sys

keys = {
    "cloudflare/apptolast-dns": "cloudflare/apptolast-dns/terraform.tfstate",
    "netcup/perimeter": "netcup/perimeter/terraform.tfstate",
}
document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
document["backend"]["config"]["key"] = keys[sys.argv[2]]
Path(sys.argv[3]).write_text(
    json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
lease_backend_identity="$(
  "${PYTHON_BIN}" -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["locking_scope_sha256"])
' "${scope_document}"
)"
lease_registry_sha256="$(
  "${PYTHON_BIN}" -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["locking_scope"]["backend_registry_sha256"])
' "${scope_document}"
)"
lease_plan_sha256="$(
  "${PYTHON_BIN}" -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["sha256"])
' "${contract_document}"
)"
lease_token_a="${probe_dir}/operation-lease-a.json"
lease_token_b="${probe_dir}/operation-lease-b.json"
lease_acquire_args=(
  --root "${root_name}"
  --metadata "${lease_metadata}"
  --source-commit "${revision}"
  --operation apply
  --backend-identity-sha256 "${lease_backend_identity}"
  --registry-sha256 "${lease_registry_sha256}"
  --plan-sha256 "${lease_plan_sha256}"
)
"${PYTHON_BIN}" "${runtime_lease_bin}" acquire \
  "${lease_acquire_args[@]}" \
  --token-file "${lease_token_a}"
set +e
"${PYTHON_BIN}" "${runtime_lease_bin}" acquire \
  "${lease_acquire_args[@]}" \
  --token-file "${lease_token_b}" \
  >"${probe_dir}/operation-lease-contention.log" 2>&1
lease_contention_status=$?
set -e
[[ "${lease_contention_status}" -ne 0 && ! -e "${lease_token_b}" ]] ||
  fail "second distributed operation lease owner was not rejected"
"${PYTHON_BIN}" "${runtime_lease_bin}" assert-held \
  --root "${root_name}" \
  --metadata "${lease_metadata}" \
  --token-file "${lease_token_a}"
"${PYTHON_BIN}" "${runtime_lease_bin}" release \
  --pre-write \
  --root "${root_name}" \
  --metadata "${lease_metadata}" \
  --token-file "${lease_token_a}"

generation_one="normal-$(date -u +%Y%m%d%H%M%S)-${RANDOM}"
marker_one="${probe_dir}/normal.marker"
TF_DATA_DIR="${client_a}" \
  "${TERRAFORM_BIN}" -chdir="${runtime_probe_root}" apply \
    -auto-approve \
    -input=false \
    -lock-timeout=5s \
    -var="generation=${generation_one}" \
    -var="marker=${marker_one}" \
    -var="hold_seconds=15" >"${probe_dir}/normal-apply.log" 2>&1 &
apply_pid=$!

for _attempt in {1..80}; do
  [[ ! -f "${marker_one}" ]] || break
  sleep 0.25
done
[[ -f "${marker_one}" ]] ||
  fail "first client did not enter the lock-holding provisioner"

set +e
TF_DATA_DIR="${client_b}" \
  "${TERRAFORM_BIN}" -chdir="${runtime_probe_root}" plan \
    -input=false \
    -lock-timeout=2s \
    -var="generation=${generation_one}" \
    -var="marker=${marker_one}" \
    -var="hold_seconds=0" >"${probe_dir}/blocked-plan.log" 2>&1
blocked_status=$?
set -e
[[ "${blocked_status}" -ne 0 ]] ||
  fail "second client wrote while the first client held the lock"
grep -F "Error acquiring the state lock" \
  "${probe_dir}/blocked-plan.log" >/dev/null ||
  fail "second client failed for a reason other than state locking"
wait "${apply_pid}" ||
  fail "normal lock-holding apply failed"
apply_pid=""

TF_DATA_DIR="${client_b}" \
  "${TERRAFORM_BIN}" -chdir="${runtime_probe_root}" plan \
    -input=false \
    -lock-timeout=5s \
    -var="generation=${generation_one}" \
    -var="marker=${marker_one}" \
    -var="hold_seconds=0" >/dev/null

generation_two="abrupt-$(date -u +%Y%m%d%H%M%S)-${RANDOM}"
marker_two="${probe_dir}/abrupt.marker"
TF_DATA_DIR="${client_a}" \
  "${TERRAFORM_BIN}" -chdir="${runtime_probe_root}" apply \
    -auto-approve \
    -input=false \
    -lock-timeout=5s \
    -var="generation=${generation_two}" \
    -var="marker=${marker_two}" \
    -var="hold_seconds=30" >"${probe_dir}/abrupt-apply.log" 2>&1 &
apply_pid=$!
for _attempt in {1..80}; do
  [[ ! -f "${marker_two}" ]] || break
  sleep 0.25
done
[[ -f "${marker_two}" ]] ||
  fail "interrupted client did not enter the lock-holding provisioner"
kill -KILL "${apply_pid}"
wait "${apply_pid}" 2>/dev/null || true
apply_pid=""

set +e
TF_DATA_DIR="${client_b}" \
  "${TERRAFORM_BIN}" -chdir="${runtime_probe_root}" plan \
    -input=false \
    -lock-timeout=2s \
    -var="generation=${generation_two}" \
    -var="marker=${marker_two}" \
    -var="hold_seconds=0" >"${probe_dir}/stale-lock.log" 2>&1
stale_status=$?
set -e
[[ "${stale_status}" -ne 0 ]] ||
  fail "SIGKILL did not leave the expected disposable stale lock"
lock_id="$(
  sed -nE \
    's/^[[:space:]]*ID:[[:space:]]*([^[:space:]]+).*$/\1/p' \
    "${probe_dir}/stale-lock.log" |
    head -n 1
)"
[[ "${lock_id}" =~ ^[a-f0-9-]{16,64}$ ]] ||
  fail "cannot extract the disposable stale-lock ID"
TF_DATA_DIR="${client_b}" \
  "${TERRAFORM_BIN}" -chdir="${runtime_probe_root}" force-unlock \
    -force "${lock_id}" >/dev/null

TF_DATA_DIR="${client_b}" \
  "${TERRAFORM_BIN}" -chdir="${runtime_probe_root}" apply \
    -auto-approve \
    -input=false \
    -lock-timeout=5s \
    -var="generation=${generation_two}" \
    -var="marker=${marker_two}" \
    -var="hold_seconds=0" >/dev/null
TF_DATA_DIR="${client_b}" \
  "${TERRAFORM_BIN}" -chdir="${runtime_probe_root}" destroy \
    -auto-approve \
    -input=false \
    -lock-timeout=5s \
    -var="generation=${generation_two}" \
    -var="marker=${marker_two}" \
    -var="hold_seconds=0" >/dev/null

proof_tmp="$(
  mktemp "$(dirname "${proof_output}")/.terraform-locking-proof.XXXXXX"
)"
"${PYTHON_BIN}" - \
  "${scope_document}" \
  "${other_scope_document}" \
  "${contract_document}" \
  "${credential_results}" \
  "${other_access_key_file}" \
  "${operator}" \
  "${revision}" \
  "${proof_tmp}" <<'PY'
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

scope = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
other_scope = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
contract = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
credential_results = json.loads(
    Path(sys.argv[4]).read_text(encoding="utf-8")
)
primary_access_key = os.environ["AWS_ACCESS_KEY_ID"]
cross_access_key = Path(sys.argv[5]).read_text(encoding="utf-8").strip()
primary_identity = hashlib.sha256(primary_access_key.encode()).hexdigest()
cross_identity = hashlib.sha256(cross_access_key.encode()).hexdigest()
if primary_identity == cross_identity:
    raise SystemExit("ERROR: primary and cross credentials are identical")
if (
    scope["locking_scope"]["primary_access_key_id_sha256"]
    != primary_identity
    or other_scope["locking_scope"]["primary_access_key_id_sha256"]
    != cross_identity
):
    raise SystemExit("ERROR: credential identity differs from its tested scope")
if (
    scope["locking_scope"]["bucket"]
    == other_scope["locking_scope"]["bucket"]
):
    raise SystemExit("ERROR: positive and negative controls share one bucket")
root = scope["locking_scope"]["root"]
cross_root = other_scope["locking_scope"]["root"]
if {root, cross_root} != {
    "cloudflare/apptolast-dns",
    "netcup/perimeter",
}:
    raise SystemExit("ERROR: proof scopes are not the two remote roots")
operator = sys.argv[6].strip()
revision = sys.argv[7]
now = datetime.now(timezone.utc).replace(microsecond=0)
document = {
    "schema": 1,
    "terraform_version": "1.15.8",
    "root": root,
    "backend_registry_role": (
        scope["locking_scope"]["backend_registry_role"]
    ),
    "cross_root": cross_root,
    "locking_scope_sha256": scope["locking_scope_sha256"],
    "locking_contract_sha256": contract["sha256"],
    "primary_access_key_id_sha256": primary_identity,
    "cross_access_key_id_sha256": cross_identity,
    "cross_credential_positive_scope_sha256": (
        other_scope["locking_scope_sha256"]
    ),
    "source_commit": revision,
    "signer_identity": "apptolast-terraform-lock-proof",
    "tested_at": now.isoformat().replace("+00:00", "Z"),
    "valid_until": (now + timedelta(hours=24)).isoformat().replace(
        "+00:00",
        "Z",
    ),
    "operator": operator,
    "results": {
        "exclusive_create": True,
        "second_client_blocked": True,
        "normal_release": True,
        "interrupted_client_recovered": True,
        "distributed_operation_lease": True,
        "terraform_backend_access_denied": True,
        **credential_results,
    },
}
Path(sys.argv[8]).write_text(
    json.dumps(document, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
ssh-keygen -Y sign \
  -f "${signing_key}" \
  -n "${SIGNATURE_NAMESPACE}" \
  "${proof_tmp}" >/dev/null
proof_signature_tmp="${proof_tmp}.sig"
[[ -s "${proof_signature_tmp}" && ! -L "${proof_signature_tmp}" ]] ||
  fail "ssh-keygen did not create a safe locking-proof signature"
ssh-keygen -Y verify \
  -f "${runtime_allowed_signers}" \
  -I "${SIGNER_IDENTITY}" \
  -n "${SIGNATURE_NAMESPACE}" \
  -s "${proof_signature_tmp}" <"${proof_tmp}" >/dev/null ||
  fail "the signing key is not approved by lock-proof.allowed-signers"
mv --no-clobber "${proof_tmp}" "${proof_output}"
[[ ! -e "${proof_tmp}" && -f "${proof_output}" && ! -L "${proof_output}" ]] ||
  fail "locking proof was not published atomically"
mv --no-clobber "${proof_signature_tmp}" "${proof_output}.sig"
[[ ! -e "${proof_signature_tmp}" &&
  -f "${proof_output}.sig" &&
  ! -L "${proof_output}.sig" ]] ||
  fail "locking proof signature was not published atomically"
chmod 0600 "${proof_output}" "${proof_output}.sig"

printf 'R2 two-client locking proof created: %s\n' "${proof_output}"
printf 'OpenSSH proof signature created: %s.sig\n' "${proof_output}"
printf 'The disposable terraform_data resource was destroyed.\n'

cleanup
trap - EXIT
