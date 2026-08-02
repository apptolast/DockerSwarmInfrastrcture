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
readonly SCRIPT_PATH="${SCRIPT_DIR}/${BASH_SOURCE[0]##*/}"

# shellcheck source=scripts/host-global-docker-validation-lock.sh
source "${SCRIPT_DIR}/host-global-docker-validation-lock.sh"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

cd "${PROJECT_DIR}"

for command_name in bash docker dockerd git jq npx; do
  command -v "${command_name}" >/dev/null || {
    printf 'ERROR: required command not found: %s\n' "${command_name}" >&2
    exit 1
  }
done

if ! docker info >/dev/null 2>&1; then
  if [[ "${LINT_DOCKER_REEXEC:-0}" != 1 ]] &&
    getent group docker |
      awk -F: -v user_name="$(id -un)" '
        $1 == "docker" && ("," $4 ",") ~ ("," user_name ",") { found = 1 }
        END { exit !found }
      '; then
    printf -v quoted_script '%q' "$0"
    exec sg docker -c "LINT_DOCKER_REEXEC=1 ${quoted_script}"
  fi
  printf 'ERROR: the current identity cannot access the Docker daemon\n' >&2
  exit 1
fi

ensure_docker_validation_lock repository-lint-docker "${SCRIPT_PATH}" "$@"

jq --exit-status '
  . == {
    "log-driver": "local",
    "log-opts": {
      "max-file": "5",
      "max-size": "20m"
    },
    "no-new-privileges": true,
    "userland-proxy": false
  }
' config/daemon.json >/dev/null
dockerd --validate --config-file=config/daemon.json

mapfile -t shell_scripts < <(
  find scripts migration/scripts stacks/workloads/config \
    -maxdepth 1 -type f -name '*.sh' |
    sort
)
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
  --workdir /workspace \
  "${SHELLCHECK_IMAGE}" \
  --severity=style \
  --external-sources \
  "${container_script_paths[@]}"

mapfile -t markdown_files < <(
  git ls-files --cached --others --exclude-standard -- '*.md' |
    sort
)
if (( ${#markdown_files[@]} > 0 )); then
  npx --yes \
    "markdownlint-cli2@${MARKDOWNLINT_VERSION}" \
    "${markdown_files[@]}"
fi

docker run \
  --rm \
  --volume "${PROJECT_DIR}:/repo:ro" \
  "${GITLEAKS_IMAGE}" \
  dir \
  --exit-code=1 \
  --no-banner \
  --redact \
  /repo

docker run \
  --rm \
  --user "$(id -u):$(id -g)" \
  --volume "${PROJECT_DIR}:/repo:ro" \
  --workdir /repo \
  "${GITLEAKS_IMAGE}" \
  git \
  --exit-code=1 \
  --log-opts=--all \
  --no-banner \
  --redact \
  /repo

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git diff --check
  git diff --cached --check
fi

printf 'Repository lint and secret scan passed.\n'
