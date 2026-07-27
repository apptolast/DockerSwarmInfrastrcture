#!/bin/sh

set -eu

read_required_secret() {
  variable_name="$1"
  secret_path="$2"
  if [ ! -f "${secret_path}" ]; then
    printf 'ERROR: required secret file is absent: %s\n' \
      "${secret_path}" >&2
    exit 1
  fi
  secret_value="$(cat -- "${secret_path}")"
  if [ -z "${secret_value}" ]; then
    printf 'ERROR: required secret is empty: %s\n' "${variable_name}" >&2
    exit 1
  fi
  export "${variable_name}=${secret_value}"
  unset secret_value
}

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

read_required_secret \
  DB_POSTGRESDB_PASSWORD /run/secrets/n8n_db_password
read_required_secret \
  N8N_ENCRYPTION_KEY /run/secrets/n8n_encryption_key
read_required_secret \
  N8N_RUNNERS_AUTH_TOKEN /run/secrets/n8n_runners_auth_token
read_optional_secret N8N_EMAIL_MODE /run/secrets/n8n_email_mode
read_optional_secret N8N_SMTP_HOST /run/secrets/n8n_smtp_host
read_optional_secret N8N_SMTP_PORT /run/secrets/n8n_smtp_port
read_optional_secret N8N_SMTP_USER /run/secrets/n8n_smtp_user
read_optional_secret N8N_SMTP_PASS /run/secrets/n8n_smtp_pass
read_optional_secret N8N_SMTP_SENDER /run/secrets/n8n_smtp_sender
read_optional_secret N8N_SMTP_SSL /run/secrets/n8n_smtp_ssl
read_optional_secret N8N_SMTP_STARTTLS /run/secrets/n8n_smtp_starttls

exec tini -- /docker-entrypoint.sh
