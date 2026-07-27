#!/bin/bash

set -Eeuo pipefail

read_required_secret() {
  local variable_name="$1"
  local secret_path="$2"
  local secret_value
  if [[ ! -f "${secret_path}" ]]; then
    printf 'ERROR: required secret file is absent: %s\n' \
      "${secret_path}" >&2
    exit 1
  fi
  secret_value="$(<"${secret_path}")"
  if [[ -z "${secret_value}" ]]; then
    printf 'ERROR: required secret is empty: %s\n' "${variable_name}" >&2
    exit 1
  fi
  export "${variable_name}=${secret_value}"
  unset secret_value
}

read_optional_secret() {
  local variable_name="$1"
  local secret_path="$2"
  local secret_value
  if [[ ! -f "${secret_path}" ]]; then
    printf 'ERROR: declared optional secret file is absent: %s\n' \
      "${secret_path}" >&2
    exit 1
  fi
  secret_value="$(<"${secret_path}")"
  if [[
    -n "${secret_value}"
    && "${secret_value}" != "__APPTOLAST_OPTIONAL_UNSET_V1__"
  ]]; then
    export "${variable_name}=${secret_value}"
  else
    unset "${variable_name}"
  fi
  unset secret_value
}

read_required_secret \
  DATASOURCES_DEFAULT_PASSWORD /run/secrets/passbolt_db_password
read_required_secret SECURITY_SALT /run/secrets/passbolt_security_salt
read_optional_secret \
  EMAIL_TRANSPORT_DEFAULT_HOST /run/secrets/passbolt_email_host
read_optional_secret \
  EMAIL_TRANSPORT_DEFAULT_PORT /run/secrets/passbolt_email_port
read_optional_secret \
  EMAIL_TRANSPORT_DEFAULT_USERNAME /run/secrets/passbolt_email_username
read_optional_secret \
  EMAIL_TRANSPORT_DEFAULT_PASSWORD /run/secrets/passbolt_email_password
read_optional_secret \
  EMAIL_TRANSPORT_DEFAULT_TLS /run/secrets/passbolt_email_tls
read_optional_secret EMAIL_DEFAULT_FROM /run/secrets/passbolt_email_from
read_optional_secret \
  EMAIL_DEFAULT_FROM_NAME /run/secrets/passbolt_email_from_name
read_optional_secret PASSBOLT_KEY_EMAIL /run/secrets/passbolt_key_email

exec /usr/bin/wait-for.sh -t 0 passbolt-db:5432 -- /docker-entrypoint.sh
