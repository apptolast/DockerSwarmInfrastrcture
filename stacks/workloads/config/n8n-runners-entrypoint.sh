#!/bin/sh

set -eu

secret_path=/run/secrets/n8n_runners_auth_token
if [ ! -f "${secret_path}" ]; then
  printf 'ERROR: required secret file is absent: %s\n' "${secret_path}" >&2
  exit 1
fi
secret_value="$(cat -- "${secret_path}")"
if [ -z "${secret_value}" ]; then
  printf 'ERROR: N8N_RUNNERS_AUTH_TOKEN is empty\n' >&2
  exit 1
fi
export N8N_RUNNERS_AUTH_TOKEN="${secret_value}"
unset secret_value

exec tini -- /usr/local/bin/task-runner-launcher javascript python
