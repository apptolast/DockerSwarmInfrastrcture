#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR

command -v python3 >/dev/null || {
  printf 'ERROR: python3 is required\n' >&2
  exit 1
}

exec python3 "${SCRIPT_DIR}/observability-tunnel.py" "$@"
