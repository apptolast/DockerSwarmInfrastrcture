#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly VENV_PYTHON="${PROJECT_DIR}/.venv/bin/python"
readonly BUILD_DIR="${1:-${PROJECT_DIR}/.build/observability}"
readonly SMOKE_SUFFIX="$$-${RANDOM}"
readonly SCRIPT_PATH="${SCRIPT_DIR}/${BASH_SOURCE[0]##*/}"

# shellcheck source=scripts/host-global-docker-validation-lock.sh
source "${SCRIPT_DIR}/host-global-docker-validation-lock.sh"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ -x "${VENV_PYTHON}" ]] || fail "the locked Python environment is absent"
[[ -f "${BUILD_DIR}/config/prometheus.yml" ]] ||
  fail "rendered observability configuration is absent"
command -v docker >/dev/null || fail "docker is required"

ensure_docker_validation_lock \
  observability-runtime-smoke \
  "${SCRIPT_PATH}" \
  "$@"

mapfile -t observability_images < <(
  "${VENV_PYTHON}" - <<'PY'
from pathlib import Path
import yaml

catalog = yaml.safe_load(Path("config/services.yml").read_text())
for item in catalog["internal_platform"]["observability"]["components"]:
    print(f"{item['id']}\t{item['image']}")
PY
)
declare -A image_by_component=()
for entry in "${observability_images[@]}"; do
  component="${entry%%$'\t'*}"
  image="${entry#*$'\t'}"
  image_by_component["${component}"]="${image}"
done

readonly prometheus_name="iac-observability-prometheus-${SMOKE_SUFFIX}"
readonly loki_name="iac-observability-loki-${SMOKE_SUFFIX}"
readonly loki_probe_name="iac-observability-loki-probe-${SMOKE_SUFFIX}"
readonly grafana_name="iac-observability-grafana-${SMOKE_SUFFIX}"
readonly alloy_name="iac-observability-alloy-${SMOKE_SUFFIX}"
# shellcheck disable=SC2016
readonly ALLOY_READY_COMMAND='
exec 3<>/dev/tcp/127.0.0.1/12345
printf "GET /-/ready HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n" >&3
IFS= read -r status <&3
[[ "${status}" == *" 200 "* ]]
'
smoke_containers=()
proxy_root=""
proxy_socket=""
proxy_log=""
proxy_pid=""

cleanup() {
  local container_name
  for container_name in "${smoke_containers[@]}"; do
    docker rm --force --volumes -- "${container_name}" >/dev/null 2>&1 || true
  done
  if [[ -n "${proxy_pid}" ]]; then
    kill "${proxy_pid}" >/dev/null 2>&1 || true
    wait "${proxy_pid}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${proxy_socket}" && -e "${proxy_socket}" ]]; then
    unlink "${proxy_socket}"
  fi
  if [[ -n "${proxy_log}" && -e "${proxy_log}" ]]; then
    unlink "${proxy_log}"
  fi
  if [[ -n "${proxy_root}" && -d "${proxy_root}" ]]; then
    rmdir "${proxy_root}"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_exec() {
  local container_name="$1"
  shift
  local attempt
  for attempt in {1..45}; do
    if docker exec "${container_name}" "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  docker logs --tail 200 "${container_name}" >&2 || true
  fail "runtime smoke probe failed for ${container_name}"
}

smoke_containers+=("${prometheus_name}")
docker run \
  --detach \
  --name "${prometheus_name}" \
  --network bridge \
  --read-only \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  --pids-limit 128 \
  --cpus 1 \
  --memory 512m \
  --user 65534:65534 \
  --tmpfs /prometheus:rw,noexec,nosuid,nodev,size=256m,uid=65534,gid=65534 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,uid=65534,gid=65534 \
  --volume \
    "${BUILD_DIR}/config/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
  --volume \
    "${BUILD_DIR}/config/prometheus-alerts.yml:/etc/prometheus/rules/platform.yml:ro" \
  --entrypoint /bin/prometheus \
  "${image_by_component[prometheus]}" \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/prometheus \
  --storage.tsdb.retention.time=1h \
  --web.listen-address=127.0.0.1:9090 >/dev/null

wait_exec \
  "${prometheus_name}" \
  /bin/wget \
  --quiet \
  --spider \
  --timeout=3 \
  http://127.0.0.1:9090/-/ready

prometheus_rules_healthy() {
  docker exec \
    "${prometheus_name}" \
    /bin/wget \
    --quiet \
    --output-document=- \
    "http://127.0.0.1:9090/api/v1/rules?type=alert" 2>/dev/null |
    "${VENV_PYTHON}" -c '
import json
import sys

expected = {
    "ApplicationBackupStale",
    "BackupLastRunFailed",
    "BackupMetricsMissing",
    "ContainerAggregateMemoryPressure",
    "ExternalAlertDeliveryDisabled",
    "HostFilesystemSpaceLow",
    "HostFilesystemWillFillIn24Hours",
    "HostMemoryPressure",
    "LokiDiscardingLogs",
    "MinecraftEndpointDown",
    "PrometheusRuleEvaluationFailures",
    "PrometheusTargetDown",
    "PublicEndpointDown",
}
document = json.load(sys.stdin)
rules = [
    rule
    for group in document.get("data", {}).get("groups", [])
    for rule in group.get("rules", [])
]
if (
    document.get("status") != "success"
    or {rule.get("name") for rule in rules} != expected
    or any(rule.get("health") != "ok" for rule in rules)
):
    raise SystemExit(1)
'
}

for attempt in {1..45}; do
  if prometheus_rules_healthy; then
    break
  fi
  if (( attempt == 45 )); then
    docker logs --tail 200 "${prometheus_name}" >&2 || true
    fail "Prometheus did not load and evaluate all 13 alert rules"
  fi
  sleep 1
done

smoke_containers+=("${loki_name}")
docker run \
  --detach \
  --name "${loki_name}" \
  --network bridge \
  --read-only \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  --pids-limit 128 \
  --cpus 1 \
  --memory 512m \
  --user 10001:10001 \
  --tmpfs /loki:rw,noexec,nosuid,nodev,size=256m,uid=10001,gid=10001 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,uid=10001,gid=10001 \
  --volume "${BUILD_DIR}/config/loki.yml:/etc/loki/config.yml:ro" \
  --entrypoint /usr/bin/loki \
  "${image_by_component[loki]}" \
  -config.file=/etc/loki/config.yml \
  -target=all >/dev/null

smoke_containers+=("${loki_probe_name}")
docker run \
  --detach \
  --name "${loki_probe_name}" \
  --network "container:${loki_name}" \
  --read-only \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  --pids-limit 32 \
  --memory 64m \
  --entrypoint /bin/sleep \
  "${image_by_component[prometheus]}" \
  180 >/dev/null

wait_exec \
  "${loki_probe_name}" \
  /bin/wget \
  --quiet \
  --spider \
  --timeout=3 \
  http://127.0.0.1:3100/ready

smoke_containers+=("${grafana_name}")
docker run \
  --detach \
  --name "${grafana_name}" \
  --network bridge \
  --read-only \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  --pids-limit 256 \
  --cpus 1 \
  --memory 512m \
  --user 472:472 \
  --tmpfs /var/lib/grafana:rw,noexec,nosuid,nodev,size=256m,uid=472,gid=472 \
  --tmpfs \
    /var/lib/grafana/dashboards:rw,noexec,nosuid,nodev,size=16m,uid=472,gid=472,mode=0750 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m,uid=472,gid=472 \
  --volume \
    "${BUILD_DIR}/config/grafana-datasources.yml:/etc/grafana/provisioning/datasources/platform.yml:ro" \
  --volume \
    "${BUILD_DIR}/config/grafana-dashboards.yml:/etc/grafana/provisioning/dashboards/platform.yml:ro" \
  --volume \
    "${BUILD_DIR}/config/dashboards/platform-overview.json:/var/lib/grafana/dashboards/platform-overview.json:ro" \
  --volume \
    "${BUILD_DIR}/config/dashboards/platform-logs.json:/var/lib/grafana/dashboards/platform-logs.json:ro" \
  --env GF_ANALYTICS_REPORTING_ENABLED=false \
  --env GF_AUTH_ANONYMOUS_ENABLED=false \
  --env GF_PATHS_DATA=/var/lib/grafana \
  --env GF_PATHS_PROVISIONING=/etc/grafana/provisioning \
  --env GF_PLUGINS_PREINSTALL_AUTO_UPDATE=false \
  --env GF_PLUGINS_PREINSTALL_DISABLED=true \
  --env GF_SECURITY_ADMIN_PASSWORD=validation-only-not-a-secret \
  --env GF_SECURITY_SECRET_KEY=validation-only-not-a-secret-key \
  --env GF_USERS_ALLOW_SIGN_UP=false \
  "${image_by_component[grafana]}" >/dev/null

wait_exec \
  "${grafana_name}" \
  /usr/bin/wget \
  --quiet \
  --spider \
  --timeout=3 \
  http://127.0.0.1:3000/api/health

docker exec \
  "${grafana_name}" \
  /usr/bin/wget \
  --quiet \
  --header="Authorization: Basic \
YWRtaW46dmFsaWRhdGlvbi1vbmx5LW5vdC1hLXNlY3JldA==" \
  --output-document=- \
  http://127.0.0.1:3000/api/datasources |
  "${VENV_PYTHON}" -c '
import json
import sys

items = json.load(sys.stdin)
actual = {(item.get("name"), item.get("uid")) for item in items}
if actual != {("Prometheus", "prometheus"), ("Loki", "loki")}:
    raise SystemExit(1)
'

docker exec \
  "${grafana_name}" \
  /usr/bin/wget \
  --quiet \
  --header="Authorization: Basic \
YWRtaW46dmFsaWRhdGlvbi1vbmx5LW5vdC1hLXNlY3JldA==" \
  --output-document=- \
  "http://127.0.0.1:3000/api/search?type=dash-db" |
  "${VENV_PYTHON}" -c '
import json
import sys

items = json.load(sys.stdin)
actual = {item.get("uid") for item in items}
expected = {"apptolast-platform-overview", "apptolast-platform-logs"}
if actual != expected:
    raise SystemExit(1)
'

proxy_root="$(mktemp -d)"
proxy_socket="${proxy_root}/docker-readonly.sock"
proxy_log="${proxy_root}/proxy.log"
"${VENV_PYTHON}" "${SCRIPT_DIR}/docker-readonly-proxy.py" \
  --listen "${proxy_socket}" >"${proxy_log}" 2>&1 &
proxy_pid="$!"
for attempt in {1..50}; do
  [[ -S "${proxy_socket}" ]] && break
  sleep 0.1
done
[[ -S "${proxy_socket}" ]] ||
  fail "restricted Docker API proxy did not create its Unix socket"
"${VENV_PYTHON}" "${SCRIPT_DIR}/docker-readonly-proxy.py" \
  --probe \
  --listen "${proxy_socket}"

smoke_containers+=("${alloy_name}")
docker run \
  --detach \
  --name "${alloy_name}" \
  --network bridge \
  --read-only \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  --pids-limit 128 \
  --cpus 0.5 \
  --memory 256m \
  --user "$(id -u):$(id -g)" \
  --tmpfs \
    "/tmp:rw,noexec,nosuid,nodev,size=64m,uid=$(id -u),gid=$(id -g)" \
  --volume \
    "${proxy_socket}:/run/docker-readonly/docker.sock:ro" \
  --volume \
    "${BUILD_DIR}/config/alloy.alloy:/etc/alloy/config.alloy:ro" \
  --entrypoint /bin/alloy \
  "${image_by_component[alloy]}" \
  run \
  --server.http.listen-addr=127.0.0.1:12345 \
  --storage.path=/tmp/alloy-data \
  /etc/alloy/config.alloy >/dev/null

wait_exec \
  "${alloy_name}" \
  /usr/bin/bash \
  -ec \
  "${ALLOY_READY_COMMAND}"
sleep 3
if rg -n \
  "denied Docker API request|Docker API proxy failure" \
  "${proxy_log}"; then
  fail "Alloy requested a Docker API operation outside the read-only allowlist"
fi
if docker logs "${alloy_name}" 2>&1 |
  rg -i "permission denied|cannot connect.*docker|error.*docker"; then
  fail "Alloy could not use the restricted Docker API proxy"
fi

printf '%s\n' \
  'Prometheus, Loki, Grafana and restricted Alloy proxy smoke passed.'
