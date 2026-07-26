#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

apply=false
if [[ "${1:-}" == "--apply" ]]; then
  apply=true
  shift
fi
(( $# == 0 )) || {
  printf 'Usage: %s [--apply]\n' "${0##*/}" >&2
  exit 2
}

for command_name in docker sort; do
  command -v "${command_name}" >/dev/null || {
    printf 'ERROR: required command not found: %s\n' "${command_name}" >&2
    exit 1
  }
done

mapfile -t managed_configs < <(
  docker config ls \
    --filter label=com.apptolast.managed-by=ansible \
    --filter label=com.apptolast.stack=edge \
    --format '{{.Name}}' |
    sort --unique
)

mapfile -t retained_configs < <(
  mapfile -t service_ids < <(docker service ls --quiet)
  if (( ${#service_ids[@]} > 0 )); then
    docker service inspect \
      --format \
      '{{range .Spec.TaskTemplate.ContainerSpec.Configs}}{{.ConfigName}}{{println}}{{end}}{{with .PreviousSpec}}{{range .TaskTemplate.ContainerSpec.Configs}}{{.ConfigName}}{{println}}{{end}}{{end}}' \
      "${service_ids[@]}"
  fi |
    sed '/^[[:space:]]*$/d' |
    sort --unique
)

for config_kind in static dynamic; do
  mapfile -t newest_kind_configs < <(
    docker config ls \
      --filter label=com.apptolast.managed-by=ansible \
      --filter label=com.apptolast.stack=edge \
      --filter "label=com.apptolast.kind=${config_kind}" \
      --format '{{.CreatedAt}}|{{.Name}}' |
      sort --reverse |
      head -n 2 |
      cut -d '|' -f 2-
  )
  retained_configs+=("${newest_kind_configs[@]}")
done
mapfile -t retained_configs < <(
  printf '%s\n' "${retained_configs[@]}" |
    sed '/^[[:space:]]*$/d' |
    sort --unique
)

is_retained() {
  local candidate="$1"
  local retained
  for retained in "${retained_configs[@]}"; do
    [[ "${candidate}" == "${retained}" ]] && return 0
  done
  return 1
}

removal_candidates=()
for config_name in "${managed_configs[@]}"; do
  if ! is_retained "${config_name}"; then
    removal_candidates+=("${config_name}")
  fi
done

if (( ${#removal_candidates[@]} == 0 )); then
  printf 'No unreferenced Ansible-managed edge configs found.\n'
  exit 0
fi

printf 'Unreferenced Ansible-managed edge configs:\n'
printf '  %s\n' "${removal_candidates[@]}"

if [[ "${apply}" == false ]]; then
  printf '%s\n' \
    "Dry run only; current/previous references from every service and the" \
    "newest two static/dynamic generations are retained." \
    "Pass --apply only after reviewing the list."
  exit 0
fi

for config_name in "${removal_candidates[@]}"; do
  docker config rm "${config_name}" >/dev/null
  printf 'Removed %s (not recoverable from Swarm; reproducible from Git).\n' \
    "${config_name}"
done
