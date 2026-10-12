#!/usr/bin/env bash
# Scratch end-to-end run (never merged): what the role does after the install.
# `wait <label>` runs the manager's cluster-status every 10 s, up to 60 times,
# until no workload is left not ready, as substrate.yml does; then it requires
# the node label and the workloads, with their images, to equal what the
# contract pins. `diag <label>` records pods, restarts, events and the logs of
# whatever is not ready or restarted. Nothing prints a pod template, a Secret
# or the kubeconfig: pods are only read through jq filters that select names
# and counters.
#
# usage: verify.sh wait|diag <label>
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

cluster_status() {
  local -a installed
  mapfile -t installed < <(installed_args)
  "${MANAGER[@]}" cluster-status \
    --kubectl "${LAB}/bin/kubectl" \
    --kubeconfig "${KUBECONFIG_PATH}" \
    --home "${LAB}/home" \
    --context "kind-$(cfg .cluster.name)" \
    --node "$(cfg .cluster.node_container)" \
    --registry-port "$(cfg .registry.host_port)" \
    --tag "$(cfg .substrate.version)" \
    "${installed[@]}"
}

wait_ready() {
  local label="$1" out="${E2E}/$1" line="" ready=false attempt
  mkdir -p "${out}"
  group "cluster-status until no workload is left not ready (60 x 10 s)"
  for attempt in $(seq 1 60); do
    if cluster_status >"${out}/cluster-status.json" 2>"${out}/cluster-status.err"; then
      line="$(grep -E '^[{]' "${out}/cluster-status.json" | tail -n 1 || true)"
      if [[ -n "${line}" ]]; then
        echo "attempt ${attempt}: not_ready = $(jq -c .not_ready <<<"${line}")"
        if jq --exit-status '.not_ready == []' <<<"${line}" >/dev/null; then
          ready=true
          break
        fi
      fi
    else
      echo "attempt ${attempt}: cluster-status failed: $(head -n 2 "${out}/cluster-status.err")"
    fi
    sleep 10
  done
  endgroup
  echo "${ready}" >"${out}/ready"

  if [[ -z "${line}" ]]; then
    echo "::error::cluster-status never printed a result"
    return 1
  fi
  echo "${line}" >"${out}/cluster-status-last.json"

  group "cluster-status result (identities only, no data)"
  jq . <<<"${line}"
  endgroup

  group "The workloads equal what the contract pins"
  local equal=true
  if ! jq --exit-status --slurpfile lab "${CFG}" '
      .workloads == $lab[0].substrate.workloads
    ' <<<"${line}" >/dev/null; then
    equal=false
    diff <(jq -S '.workloads' <<<"${line}") \
      <(jq -S '.substrate.workloads' "${CFG}") || true
  fi
  local label_ok=true
  if ! jq --exit-status --slurpfile lab "${CFG}" '
      .node_label == $lab[0].substrate.version
    ' <<<"${line}" >/dev/null; then
    label_ok=false
  fi
  echo "workloads equal the pins: ${equal}; node label equals the version: ${label_ok}"
  echo "${equal}" >"${out}/workloads-equal"
  endgroup

  {
    echo ""
    echo "### Substrate workloads after \`${label}\`"
    echo ""
    echo "Every workload ready: ${ready}. Workloads and images equal the pins:" \
      "${equal}. Node label equals the version: ${label_ok}."
    echo ""
    echo "| Workload | Ready | Images |"
    echo "| --- | --- | --- |"
    jq -r '
      . as $s
      | .workloads[]
      | "\(.namespace)/\(.kind)/\(.name)" as $key
      | "| \($key) | \($s.objects[$key].ready) | \(((.init_images // []) + .images) | join(", ")) |"
    ' <<<"${line}"
  } | summary

  [[ "${ready}" == true && "${equal}" == true && "${label_ok}" == true ]]
}

diag() {
  local label="$1" out="${E2E}/$1"
  mkdir -p "${out}"
  group "diag ${label}: nodes, pods, restarts"
  kubectl_lab get nodes --output wide || true
  kubectl_lab get pods --all-namespaces --output wide || true
  kubectl_lab get pods --all-namespaces --no-headers \
    --output custom-columns='NS:.metadata.namespace,POD:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,RESTARTS:.status.containerStatuses[*].restartCount,WAITING:.status.containerStatuses[*].state.waiting.reason,LAST_TERMINATED:.status.containerStatuses[*].lastState.terminated.reason' \
    | tee "${out}/restarts.txt" || true
  kubectl_lab get deployments,statefulsets,daemonsets,jobs \
    --all-namespaces || true
  endgroup

  group "diag ${label}: every event, then the Warning ones"
  kubectl_lab get events --all-namespaces --sort-by=.lastTimestamp \
    | tee "${out}/events.txt" || true
  echo "--- Warning events"
  kubectl_lab get events --all-namespaces --field-selector type=Warning \
    --sort-by=.lastTimestamp | tee "${out}/events-warning.txt" || true
  endgroup

  group "diag ${label}: logs of every pod that is not ready or restarted"
  local json="${RUNNER_TEMP}/pods-${label}.json" namespace pod restarts
  kubectl_lab get pods --all-namespaces --output json >"${json}" || true
  while read -r namespace pod restarts; do
    [[ -n "${pod}" ]] || continue
    echo "=== ${namespace}/${pod} (restarts ${restarts})"
    kubectl_lab logs --namespace "${namespace}" "${pod}" --all-containers \
      --tail=100 2>&1 | head -n 150 || true
    if [[ "${restarts}" -gt 0 ]]; then
      echo "--- previous"
      kubectl_lab logs --namespace "${namespace}" "${pod}" --all-containers \
        --previous --tail=100 2>&1 | head -n 150 || true
    fi
  done < <(
    jq -r '
      .items[]
      | select(.status.phase != "Succeeded")
      | ([.status.initContainerStatuses[]?, .status.containerStatuses[]?]) as $c
      | select(
          .status.phase != "Running"
          or ($c | any(.ready == false and (.state.terminated.reason // "") != "Completed"))
          or ($c | any(.restartCount > 0)))
      | "\(.metadata.namespace) \(.metadata.name) \([$c[].restartCount] | add // 0)"
    ' "${json}" 2>/dev/null || true
  )
  rm -f "${json}"
  endgroup

  group "diag ${label}: environment signatures in events and logs"
  local -a sources=()
  local source
  for source in "${out}/events.txt" "${out}/ate-setup.log"; do
    [[ -f "${source}" ]] && sources+=("${source}")
  done
  if [[ "${#sources[@]}" -gt 0 ]] && grep -hiE \
    'toomanyrequests|rate limit|too many open files|inotify|userns|apparmor|operation not permitted|no space left|/dev/kvm|cgroup' \
    "${sources[@]}"; then
    echo "^ lines that look environmental (runner), not image related"
  else
    echo "none"
  fi
  endgroup

  group "diag ${label}: containers of the lab on the runner"
  docker stats --no-stream --format \
    '{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}} pids={{.PIDs}}' || true
  docker exec "$(cfg .cluster.node_container)" /usr/bin/df -h / || true
  docker logs --tail 20 "$(cfg .cluster.node_container)" 2>&1 | tail -n 20 \
    || true
  endgroup
}

"$@"
