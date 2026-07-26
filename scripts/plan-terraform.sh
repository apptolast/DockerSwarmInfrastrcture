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
var_file=""
output_plan=""

usage() {
  cat <<'EOF'
Usage:
  plan-terraform.sh --root ROOT --backend-config PATH
      [--var-file PATH] [--out PATH]

ROOT is one of:
  cloudflare/state-bootstrap
  cloudflare/apptolast-dns
  netcup/perimeter

The worktree must be clean so every saved plan maps to one Git commit.
Terraform's detailed exit code is preserved: 0 means no changes, 2 means the
reviewed plan contains changes, and 1 means an error.
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

for command_name in git install realpath; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done
[[ -x "${TERRAFORM_BIN}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -f "${backend_config}" && ! -L "${backend_config}" ]] ||
  fail "the backend config must be a regular, non-symlink file"
backend_config="$(realpath "${backend_config}")"
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

safe_root="${root_name//\//-}"
terraform_data_dir="${PROJECT_DIR}/.terraform.d/work/${safe_root}"
plugin_cache_dir="${PROJECT_DIR}/.terraform.d/plugin-cache"
install --directory --mode=0750 \
  "${terraform_data_dir}" \
  "${plugin_cache_dir}" \
  "${PROJECT_DIR}/.build/plans"

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

export TF_DATA_DIR="${terraform_data_dir}"
export TF_IN_AUTOMATION=1
export TF_INPUT=0
export TF_PLUGIN_CACHE_DIR="${plugin_cache_dir}"

"${TERRAFORM_BIN}" -chdir="${terraform_root}" init \
  -backend-config="${backend_config}" \
  -input=false \
  -lockfile=readonly \
  -reconfigure
"${TERRAFORM_BIN}" -chdir="${terraform_root}" validate

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
"${TERRAFORM_BIN}" -chdir="${terraform_root}" plan "${plan_args[@]}"
plan_status=$?
set -e

case "${plan_status}" in
  0)
    printf 'No Terraform changes for %s at commit %s.\n' \
      "${root_name}" "${revision}"
    ;;
  2)
    printf 'Reviewable Terraform plan created: %s\n' "${output_plan}"
    printf 'Source commit: %s\n' "${revision}"
    printf '%s\n' \
      "Exit status 2 is expected when the plan contains changes." \
      "Treat the plan as sensitive and never commit it."
    ;;
  *)
    fail "terraform plan failed for ${root_name}"
    ;;
esac

exit "${plan_status}"
