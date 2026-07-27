#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR

cd "${PROJECT_DIR}"

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_backupctl \
  tests.test_backup_contract \
  tests.test_platform_firewall \
  tests.test_platform_lifecycle
