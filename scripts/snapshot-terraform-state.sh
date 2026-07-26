#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly TERRAFORM_BIN="${PROJECT_DIR}/.tools/terraform"

root_name=""
backend_config=""
recipient_file=""
output_dir=""

usage() {
  cat <<'EOF'
Usage:
  snapshot-terraform-state.sh
      --root cloudflare/state-bootstrap|cloudflare/apptolast-dns|netcup/perimeter
      --backend-config PATH
      --recipient-file PATH
      --output-dir PATH

The pulled state is encrypted directly with age. Plaintext state is never
written to disk. The destination must be outside this Git repository.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
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

for command_name in age find git install mktemp realpath sha256sum; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done
[[ -x "${TERRAFORM_BIN}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -f "${backend_config}" ]] ||
  fail "backend configuration not found: ${backend_config}"
[[ -f "${recipient_file}" ]] ||
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

terraform_data_dir="$(mktemp -d)"
snapshot_tmp=""
cleanup() {
  [[ -z "${snapshot_tmp}" || ! -e "${snapshot_tmp}" ]] ||
    find "${snapshot_tmp}" -depth -delete
  [[ ! -d "${terraform_data_dir}" ]] ||
    find "${terraform_data_dir}" -depth -delete
}
trap cleanup EXIT

terraform_root="${PROJECT_DIR}/infra/terraform/${root_name}"
TF_DATA_DIR="${terraform_data_dir}" \
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" init \
  -backend-config="${backend_config}" \
  -input=false \
  -reconfigure >/dev/null

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
safe_root="${root_name//\//-}"
snapshot_path="${output_real}/${safe_root}-${timestamp}.tfstate.age"
snapshot_tmp="$(mktemp "${output_real}/.${safe_root}.XXXXXX")"

TF_DATA_DIR="${terraform_data_dir}" \
  "${TERRAFORM_BIN}" -chdir="${terraform_root}" state pull |
  age --recipients-file "${recipient_file}" --output "${snapshot_tmp}"

[[ -s "${snapshot_tmp}" ]] || fail "the encrypted snapshot is empty"
head -n 1 "${snapshot_tmp}" |
  grep -Fx 'age-encryption.org/v1' >/dev/null ||
  fail "the snapshot is not an age file"

mv --no-clobber "${snapshot_tmp}" "${snapshot_path}"
snapshot_tmp=""
sha256sum "${snapshot_path}" >"${snapshot_path}.sha256"
chmod 0600 "${snapshot_path}" "${snapshot_path}.sha256"

commit_sha="$(git -C "${PROJECT_DIR}" rev-parse HEAD)"
printf 'Encrypted state snapshot created: %s\n' "${snapshot_path}"
printf 'Source commit: %s\n' "${commit_sha}"

cleanup
trap - EXIT
