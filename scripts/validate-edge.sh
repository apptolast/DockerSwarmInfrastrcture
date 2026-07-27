#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
# v3.7.9 emits this unconditionally before it evaluates effective config:
# https://github.com/traefik/traefik/blob/d0bd2ec198533d760c1abfc74b98033d6d92d039/cmd/traefik/traefik.go#L100-L103
readonly KNOWN_WARNING="Traefik can reject some encoded characters in the request path"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

if ! docker info >/dev/null 2>&1; then
  if [[ "${EDGE_RUNTIME_REEXEC:-0}" != 1 ]] &&
    getent group docker |
      awk -F: -v user_name="$(id -un)" '
        $1 == "docker" && ("," $4 ",") ~ ("," user_name ",") { found = 1 }
        END { exit !found }
      '; then
    printf -v quoted_script '%q' "$0"
    exec sg docker -c "EDGE_RUNTIME_REEXEC=1 ${quoted_script}"
  fi
  fail "the current identity cannot access the Docker daemon"
fi

for command_name in curl docker getent grep jq mktemp sed sort; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done
[[ -x "${PYTHON_BIN}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"

mapfile -t edge_contract < <(
  "${PYTHON_BIN}" - "${PROJECT_DIR}" <<'PY'
import json
import pathlib
import sys
import yaml

root = pathlib.Path(sys.argv[1])
contract = yaml.safe_load((root / "config/platform.yml").read_text())
group_vars = yaml.safe_load((root / "ansible/group_vars/all.yml").read_text())
print(contract["platform_public_ipv4"])
print(contract["edge_traefik_hostname"])
print(contract["platform_edge_monitoring_network"])
print(json.dumps(contract["platform_edge_networks"], sort_keys=True))
print(group_vars["edge_traefik_image"])
print(group_vars["edge_traefik_cloudflare_secret_name"])
PY
)
(( ${#edge_contract[@]} == 6 )) ||
  fail "cannot read the edge contract"
public_ipv4="${edge_contract[0]}"
hostname="${edge_contract[1]}"
monitoring_network_name="${edge_contract[2]}"
edge_network_map_json="${edge_contract[3]}"
traefik_image="${edge_contract[4]}"
secret_name="${edge_contract[5]}"

mapfile -t expected_network_names < <(
  jq --raw-output 'to_entries | sort_by(.key) | .[].value' \
    <<<"${edge_network_map_json}"
)
expected_network_names+=("${monitoring_network_name}")
(( ${#expected_network_names[@]} == 9 )) ||
  fail "the isolated edge network allowlist is incomplete"

network_ids=()
for network_name in "${expected_network_names[@]}"; do
  network_json="$(docker network inspect "${network_name}")"
  jq --exit-status '
    length == 1 and
    .[0].Driver == "overlay" and
    .[0].Scope == "swarm" and
    .[0].Attachable == false and
    .[0].Internal == false and
    .[0].Options.encrypted == ""
  ' <<<"${network_json}" >/dev/null ||
    fail "edge network ${network_name} differs from the isolation contract"
  network_ids+=("$(jq --raw-output '.[0].Id' <<<"${network_json}")")
done
expected_network_ids_json="$(
  printf '%s\n' "${network_ids[@]}" |
    jq --raw-input --slurp 'split("\n") | map(select(length > 0)) | sort'
)"

service_json="$(docker service inspect edge_traefik)"
jq --exit-status \
  --arg image "${traefik_image}" \
  --arg secret "${secret_name}" \
  --argjson network_ids "${expected_network_ids_json}" \
  '
    length == 1 and
    .[0].Spec.TaskTemplate.ContainerSpec.Image == $image and
    .[0].Spec.TaskTemplate.ContainerSpec.User == "65532:65532" and
    .[0].Spec.TaskTemplate.ContainerSpec.ReadonlyRootfs == true and
    .[0].Spec.TaskTemplate.ContainerSpec.Privileges.NoNewPrivileges == true and
    .[0].Spec.Mode.Replicated.Replicas == 1 and
    .[0].Spec.UpdateConfig.Order == "stop-first" and
    .[0].Spec.UpdateConfig.FailureAction == "rollback" and
    .[0].Spec.RollbackConfig.Order == "stop-first" and
    (
      .[0].Spec.TaskTemplate.ContainerSpec.Secrets |
      length == 1 and .[0].SecretName == $secret
    ) and
    (
      .[0].Spec.TaskTemplate.ContainerSpec.Configs |
      length == 2
    ) and
    (
      .[0].Spec.TaskTemplate.Networks |
      map(.Target) | sort == $network_ids
    ) and
    (
      .[0].Spec.TaskTemplate.ContainerSpec.Mounts |
      length == 1 and
      .[0].Type == "bind" and
      .[0].Target == "/data"
    ) and
    (
      .[0].Endpoint.Spec.Ports |
      sort_by(.PublishedPort) == [
        {
          Protocol: "tcp",
          TargetPort: 8000,
          PublishedPort: 80,
          PublishMode: "host"
        },
        {
          Protocol: "tcp",
          TargetPort: 8443,
          PublishedPort: 443,
          PublishMode: "host"
        }
      ]
    )
  ' <<<"${service_json}" >/dev/null ||
  fail "edge_traefik differs from the reviewed service contract"

mapfile -t task_containers < <(
  docker ps \
    --filter label=com.docker.swarm.service.name=edge_traefik \
    --filter status=running \
    --quiet
)
(( ${#task_containers[@]} == 1 )) ||
  fail "edge_traefik must have exactly one running task"
task_container="${task_containers[0]}"
[[ "$(docker inspect --format '{{.State.Health.Status}}' "${task_container}")" == \
  "healthy" ]] ||
  fail "the Traefik task is not healthy"

traefik_logs="$(docker logs "${task_container}" 2>&1)"
problem_logs="$(
  grep -Ei '"level":"(warn|error|fatal|panic)"' <<<"${traefik_logs}" ||
    true
)"
unknown_logs="$(
  grep -Fv "${KNOWN_WARNING}" <<<"${problem_logs}" ||
    true
)"
[[ -z "${unknown_logs}" ]] || {
  printf '%s\n' "${unknown_logs}" >&2
  fail "Traefik emitted an unexpected warning-or-higher entry"
}
[[ "$(grep -Fc "${KNOWN_WARNING}" <<<"${problem_logs}" || true)" -eq 1 ]] ||
  fail "the unconditional pinned Traefik warning did not occur exactly once"

resolved_addresses="$(
  getent ahostsv4 "${hostname}" |
    awk '{print $1}' |
    sort --unique
)"
grep -Fx "${public_ipv4}" <<<"${resolved_addresses}" >/dev/null ||
  fail "${hostname} does not resolve to ${public_ipv4}"

validation_tmp="$(mktemp -d)"
cleanup() {
  [[ ! -d "${validation_tmp}" ]] ||
    find "${validation_tmp}" -depth -delete
}
trap cleanup EXIT

curl \
  --fail \
  --silent \
  --show-error \
  --connect-timeout 5 \
  --max-time 20 \
  --resolve "${hostname}:443:${public_ipv4}" \
  --dump-header "${validation_tmp}/https-headers" \
  --output "${validation_tmp}/https-body" \
  "https://${hostname}/ping"
[[ "$(tr -d '\r\n' <"${validation_tmp}/https-body")" == "OK" ]] ||
  fail "the public Traefik health endpoint did not return OK"
grep -Eiq \
  '^strict-transport-security:[[:space:]]*max-age=31536000' \
  "${validation_tmp}/https-headers" ||
  fail "the public HTTPS response lacks the reviewed HSTS policy"
if grep -Eiq '^server:[[:space:]]*[^[:space:]]' \
  "${validation_tmp}/https-headers"; then
  fail "the public HTTPS response exposes a Server header"
fi

http_status="$(
  curl \
    --silent \
    --show-error \
    --connect-timeout 5 \
    --max-time 20 \
    --resolve "${hostname}:80:${public_ipv4}" \
    --output /dev/null \
    --write-out '%{http_code}' \
    "http://${hostname}/ping"
)"
[[ "${http_status}" == "308" ]] ||
  fail "HTTP does not return the permanent HTTPS redirect"

cleanup
trap - EXIT
printf 'Traefik edge runtime, DNS, TLS, health and hardening validation passed.\n'
