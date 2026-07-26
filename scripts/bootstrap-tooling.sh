#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly TERRAFORM_VERSION="1.15.8"
readonly TERRAFORM_AMD64_SHA256="d25ce7b6902013ad905db3d2eab0be4cd905887fe88b81a6171b8d5503c31f3d"
readonly TERRAFORM_ARM64_SHA256="8891e9dcedc9e3b8950bc6af9d4d8af1f4cfade3062f53b9dc403a89f6ce8c9c"
readonly TERRAFORM_AMD64_BINARY_SHA256="8b6cb96cd46080ee1287baf646c70078715a99123b9b3a6ce2a7fe3892ec703a"
readonly TERRAFORM_ARM64_BINARY_SHA256="00f55981f5215594c418cd6b20f44fa4c99f9126650602e65d533d131005ea81"
readonly TOOLS_DIR="${PROJECT_DIR}/.tools"
readonly TERRAFORM_BIN="${TOOLS_DIR}/terraform"
readonly VENV_DIR="${PROJECT_DIR}/.venv"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

for command_name in curl install python3 sha256sum uname; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done

python3 -c '
import sys
if sys.version_info[:2] != (3, 14):
    raise SystemExit("Python 3.14 is required by the locked tooling environment")
'

case "$(uname -m)" in
  x86_64)
    terraform_arch="amd64"
    terraform_sha256="${TERRAFORM_AMD64_SHA256}"
    terraform_binary_sha256="${TERRAFORM_AMD64_BINARY_SHA256}"
    ;;
  aarch64|arm64)
    terraform_arch="arm64"
    terraform_sha256="${TERRAFORM_ARM64_SHA256}"
    terraform_binary_sha256="${TERRAFORM_ARM64_BINARY_SHA256}"
    ;;
  *)
    fail "unsupported architecture: $(uname -m)"
    ;;
esac

install --directory --mode=0750 "${TOOLS_DIR}"

terraform_ready=false
if [[ -x "${TERRAFORM_BIN}" ]] &&
  "${TERRAFORM_BIN}" version |
    grep -Fx "Terraform v${TERRAFORM_VERSION}" >/dev/null &&
  printf '%s  %s\n' "${terraform_binary_sha256}" "${TERRAFORM_BIN}" |
    sha256sum --check --status -; then
  terraform_ready=true
fi

if [[ "${terraform_ready}" == false ]]; then
  tooling_tmp="$(mktemp -d)"
  cleanup() {
    [[ ! -d "${tooling_tmp}" ]] ||
      find "${tooling_tmp}" -depth -delete
  }
  trap cleanup EXIT

  terraform_archive="terraform_${TERRAFORM_VERSION}_linux_${terraform_arch}.zip"
  curl \
    --fail \
    --location \
    --proto '=https' \
    --silent \
    --show-error \
    --tlsv1.2 \
    --output "${tooling_tmp}/${terraform_archive}" \
    "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/${terraform_archive}"

  printf '%s  %s\n' \
    "${terraform_sha256}" \
    "${tooling_tmp}/${terraform_archive}" |
    sha256sum --check --status -

  python3 -m zipfile \
    --extract \
    "${tooling_tmp}/${terraform_archive}" \
    "${tooling_tmp}/terraform"
  install \
    --mode=0750 \
    "${tooling_tmp}/terraform/terraform" \
    "${TERRAFORM_BIN}"
  printf '%s  %s\n' "${terraform_binary_sha256}" "${TERRAFORM_BIN}" |
    sha256sum --check --status - ||
    fail "the extracted Terraform binary does not match the reviewed checksum"

  cleanup
  trap - EXIT
fi

if [[ -e "${VENV_DIR}" && ! -x "${VENV_DIR}/bin/python" ]]; then
  fail "${VENV_DIR} exists but is not a usable virtual environment"
fi

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  python3 -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/python" -m pip install \
  --disable-pip-version-check \
  --require-hashes \
  --requirement "${PROJECT_DIR}/ansible/requirements-dev.txt"

ANSIBLE_CONFIG="${PROJECT_DIR}/ansible/ansible.cfg" \
  "${VENV_DIR}/bin/ansible-galaxy" collection install \
  --requirements-file "${PROJECT_DIR}/ansible/requirements.yml" \
  --collections-path "${PROJECT_DIR}/.ansible/collections"

"${TERRAFORM_BIN}" version
"${VENV_DIR}/bin/ansible" --version | sed -n '1p'
printf 'Locked Terraform and Ansible tooling is ready.\n'
