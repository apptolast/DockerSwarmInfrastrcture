#!/bin/sh

set -eu

secret_path=/run/secrets/openclaw_gateway_token
if [ ! -f "${secret_path}" ]; then
  printf 'ERROR: required secret file is absent: %s\n' "${secret_path}" >&2
  exit 1
fi
secret_value="$(cat -- "${secret_path}")"
if [ -z "${secret_value}" ]; then
  printf 'ERROR: OPENCLAW_GATEWAY_TOKEN is empty\n' >&2
  exit 1
fi
export OPENCLAW_GATEWAY_TOKEN="${secret_value}"
unset secret_value

# El gateway se niega a arrancar sin configuracion ("Missing config. Run
# `openclaw setup` or set gateway.mode=local", exit 78). Se declara el modo
# de forma no interactiva e idempotente sobre el estado persistido en
# ${OPENCLAW_STATE_DIR}/openclaw.json en lugar de recurrir al bypass
# --allow-unconfigured, que segun su propia ayuda deja la config sin reparar.
node openclaw.mjs config set gateway.mode local

exec tini -s -- \
  node openclaw.mjs gateway --bind lan --port 18789 --verbose

if [ $UNQUOTED = bad ]; then
  echo roto
