#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly SHELLCHECK_IMAGE="koalaman/shellcheck@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d"
readonly GITLEAKS_IMAGE="ghcr.io/gitleaks/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
readonly MARKDOWNLINT_VERSION="0.23.1"

cd "${PROJECT_DIR}"

for command_name in bash docker dockerd git jq npx; do
  command -v "${command_name}" >/dev/null || {
    printf 'ERROR: required command not found: %s\n' "${command_name}" >&2
    exit 1
  }
done

jq --exit-status '. == {"log-driver":"local"}' config/daemon.json >/dev/null
dockerd --validate --config-file=config/daemon.json

mapfile -t shell_scripts < <(find scripts -maxdepth 1 -type f -name '*.sh' | sort)
for script_file in "${shell_scripts[@]}"; do
  bash -n "${script_file}"
done

container_script_paths=()
for script_file in "${shell_scripts[@]}"; do
  container_script_paths+=("/workspace/${script_file}")
done

docker run \
  --rm \
  --volume "${PROJECT_DIR}:/workspace:ro" \
  "${SHELLCHECK_IMAGE}" \
  --severity=style \
  "${container_script_paths[@]}"

npx --yes "markdownlint-cli2@${MARKDOWNLINT_VERSION}" '**/*.md'

docker run \
  --rm \
  --volume "${PROJECT_DIR}:/repo:ro" \
  "${GITLEAKS_IMAGE}" \
  dir \
  --exit-code=1 \
  --no-banner \
  --redact \
  /repo

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git diff --check
  git diff --cached --check
fi

printf 'Repository lint and secret scan passed.\n'
