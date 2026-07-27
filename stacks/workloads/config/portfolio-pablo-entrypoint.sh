#!/bin/sh

set -eu

read_optional_secret() {
  variable_name="$1"
  secret_path="$2"
  if [ ! -f "${secret_path}" ]; then
    printf 'ERROR: declared optional secret file is absent: %s\n' \
      "${secret_path}" >&2
    exit 1
  fi
  secret_value="$(cat -- "${secret_path}")"
  if [ -n "${secret_value}" ] &&
    [ "${secret_value}" != "__APPTOLAST_OPTIONAL_UNSET_V1__" ]; then
    export "${variable_name}=${secret_value}"
  else
    unset "${variable_name}"
  fi
  unset secret_value
}

read_optional_secret \
  NEXT_PUBLIC_EMAILJS_SERVICE_ID \
  /run/secrets/pablo_next_public_emailjs_service_id
read_optional_secret \
  NEXT_PUBLIC_EMAILJS_TEMPLATE_ID \
  /run/secrets/pablo_next_public_emailjs_template_id
read_optional_secret \
  NEXT_PUBLIC_EMAILJS_PUBLIC_KEY \
  /run/secrets/pablo_next_public_emailjs_public_key
read_optional_secret \
  EMAILJS_SERVICE_ID /run/secrets/pablo_emailjs_service_id
read_optional_secret \
  EMAILJS_TEMPLATE_ID /run/secrets/pablo_emailjs_template_id
read_optional_secret \
  EMAILJS_PUBLIC_KEY /run/secrets/pablo_emailjs_public_key

exec docker-entrypoint.sh node server.js
