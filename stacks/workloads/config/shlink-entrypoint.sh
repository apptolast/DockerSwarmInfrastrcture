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

read_required_secret DB_PASSWORD /run/secrets/shlink_db_password
read_optional_secret \
  GEOLITE_LICENSE_KEY /run/secrets/shlink_geolite_license_key
read_optional_secret \
  MERCURE_PUBLIC_HUB_URL /run/secrets/shlink_mercure_public_hub_url
read_optional_secret \
  MERCURE_INTERNAL_HUB_URL /run/secrets/shlink_mercure_internal_hub_url
read_optional_secret \
  MERCURE_JWT_SECRET /run/secrets/shlink_mercure_jwt_secret
read_optional_secret MATOMO_SITE_ID /run/secrets/shlink_matomo_site_id
read_optional_secret MATOMO_URL /run/secrets/shlink_matomo_url

exec /bin/sh ./docker-entrypoint.sh
