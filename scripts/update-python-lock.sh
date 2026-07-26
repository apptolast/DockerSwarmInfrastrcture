#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly PIP_TOOLS_VERSION="7.6.0"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

for command_name in find mktemp python3; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done

python3 -c '
import sys
if sys.version_info[:2] != (3, 14):
    raise SystemExit("Python 3.14 is required to regenerate this lock")
'

lock_environment="$(mktemp -d)"
cleanup() {
  [[ ! -d "${lock_environment}" ]] ||
    find "${lock_environment}" -depth -delete
}
trap cleanup EXIT

python3 -m venv "${lock_environment}"
"${lock_environment}/bin/python" -m pip install \
  --disable-pip-version-check \
  --quiet \
  "pip-tools==${PIP_TOOLS_VERSION}"

cd "${PROJECT_DIR}"
"${lock_environment}/bin/pip-compile" \
  --generate-hashes \
  --output-file=ansible/requirements-dev.txt \
  --quiet \
  --resolver=backtracking \
  --strip-extras \
  ansible/requirements-dev.in

cleanup
trap - EXIT
printf 'Regenerated the hash-locked Python dependency graph.\n'
