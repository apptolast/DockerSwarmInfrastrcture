#!/usr/bin/env bash
# Scratch end-to-end run (never merged): the manager's install command with
# exactly the arguments the role passes (ansible/roles/ax_lab/tasks/
# substrate.yml), the lab's limits and timeouts read from the contract, and the
# memory limit as the only argument that may differ. The manager's own result
# JSON is kept even when the install fails, a read-only sampler records the
# container's cgroup next to it, and a watcher prints the pods meanwhile.
#
# usage: install.sh <label> <memory-mib>
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

label="$1"
memory="$2"
out="${E2E}/${label}"
mkdir -p "${out}"
mapfile -t installed < <(installed_args)
[[ "${#installed[@]}" -eq 5 ]]

argv=(
  "${MANAGER[@]}" install
  --registry-port "$(cfg .registry.host_port)"
  --tag "$(cfg .substrate.version)"
  --installer "ate-setup=$(cfg '.substrate.images["ate-setup"]')"
  --source "${LAB}/src/substrate"
  --kubeconfig "${KUBECONFIG_PATH}"
  --context "kind-$(cfg .cluster.name)"
  --atenet-router "$(cfg .substrate.install.atenet_router)"
  --rollout-timeout-seconds "$(cfg .substrate.install.rollout_timeout_seconds)"
  --memory-mib "${memory}"
  --memory-reservation-mib "$(cfg .substrate.install.memory_reservation_mib)"
  --cpu-millicores "$(cfg .substrate.install.cpu_limit_millicores)"
  --pids-limit "$(cfg .substrate.install.pids_limit)"
  --timeout-seconds "$(cfg .substrate.install.timeout_seconds)"
  --min-mem-available-mib "$(cfg .substrate.install.min_mem_available_mib)"
  --mem-available-floor-mib "$(cfg .substrate.install.mem_available_floor_mib)"
  "${installed[@]}"
)

group "install ${label}: the exact command (memory ${memory} MiB)"
printf '%q ' "${argv[@]}"
echo
free -m
docker ps --format '{{.Names}} {{.Status}}'
endgroup

"${HERE}/sample.sh" "${out}/cgroup.csv" "${out}/ate-setup.log" "${label}" &
sampler=$!
"${HERE}/watch.sh" "${label}" &
watcher=$!

set +e
"${argv[@]}" >"${out}/install.json" 2>"${out}/install.err"
status=$?
set -e

kill "${watcher}" 2>/dev/null || true
for _ in $(seq 1 20); do
  kill -0 "${sampler}" 2>/dev/null || break
  sleep 1
done
kill "${sampler}" 2>/dev/null || true
wait 2>/dev/null || true
echo "${status}" >"${out}/install.status"

group "install ${label}: manager result (stdout) and error (stderr)"
echo "exit status ${status}"
cat "${out}/install.json"
tail -n 60 "${out}/install.err"
endgroup

group "install ${label}: cgroup limits and the sampler's view"
cat "${out}/cgroup-limits.txt" 2>/dev/null || echo "no limits file"
samples="$(($(wc -l <"${out}/cgroup.csv") - 1))"
echo "${samples} samples, last five:"
tail -n 5 "${out}/cgroup.csv"
endgroup

manager='null'
if line="$(grep -E '^[{]' "${out}/install.json" | tail -n 1)" && [[ -n "${line}" ]]; then
  manager="${line}"
fi
sampled='{}'
if [[ "${samples}" -gt 0 ]]; then
  sampled="$(
    awk -F, 'NR > 1 {
      if ($2 + 0 > current) current = $2 + 0
      if ($3 + 0 > peak) peak = $3 + 0
      if ($4 + 0 > anon) anon = $4 + 0
      if ($5 + 0 > file) file = $5 + 0
      if ($6 + 0 > shmem) shmem = $6 + 0
      if ($7 + 0 > swap) swap = $7 + 0
      if ($8 + 0 > pids) pids = $8 + 0
      ev_max = $9 + 0; ev_oom = $10 + 0; ev_oom_kill = $11 + 0
    } END {
      printf "{\"samples\":%.0f,\"max_current\":%.0f,\"peak\":%.0f,\"max_anon\":%.0f,", NR - 1, current, peak, anon
      printf "\"max_file\":%.0f,\"max_shmem\":%.0f,\"max_swap\":%.0f,\"max_pids\":%.0f,", file, shmem, swap, pids
      printf "\"events_max\":%.0f,\"events_oom\":%.0f,\"events_oom_kill\":%.0f}\n", ev_max, ev_oom, ev_oom_kill
    }' "${out}/cgroup.csv"
  )"
fi

jq -n --arg label "${label}" --argjson memory "${memory}" \
  --argjson status "${status}" --argjson manager "${manager}" \
  --argjson sampled "${sampled}" '
  ($memory * 1048576) as $cap
  | {label: $label, memory_mib: $memory, cap_bytes: $cap,
     exit_status: $status, manager: $manager, sampler: $sampled,
     pct_of_cap_manager_peak:
       (if $manager.memory_peak_bytes then
          ($manager.memory_peak_bytes * 1000 / $cap | round / 10) else null end),
     pct_of_cap_sampler_peak:
       (if $sampled.peak then ($sampled.peak * 1000 / $cap | round / 10)
        else null end),
     pct_of_cap_max_anon:
       (if $sampled.max_anon then ($sampled.max_anon * 1000 / $cap | round / 10)
        else null end),
     capped_reasons: (
       [
         (if ($manager.oom_killed // false) then "manager oom_killed" else empty end),
         (if (($manager.oom_kill_events // 0) > 0) then "manager oom_kill_events" else empty end),
         (if (($sampled.events_oom_kill // 0) > 0) then "cgroup oom_kill events" else empty end),
         (if (($manager.memory_peak_bytes // 0) >= $cap * 0.95) then "manager peak at 95 percent of the cap or more" else empty end),
         (if (($sampled.peak // 0) >= $cap * 0.95) then "sampled peak at 95 percent of the cap or more" else empty end),
         (if (($sampled.events_max // 0) > 0) then "memory.events max was reached" else empty end)
       ])
    }' >"${out}/result.json"

group "install ${label}: result"
jq . "${out}/result.json"
endgroup

{
  echo ""
  echo "### ate-setup install \`${label}\` (cap ${memory} MiB)"
  echo ""
  jq -r '
    "- exit status: \(.exit_status), manager exit_code: \(.manager.exit_code // "none"), oom_killed: \(.manager.oom_killed | if . == null then "none" else tostring end), aborted: \(.manager.aborted // "none")",
    "- seconds: \(.manager.seconds // "none")",
    "- manager memory_peak_bytes: \(.manager.memory_peak_bytes // "none") (\(.pct_of_cap_manager_peak // "n/a") percent of the cap)",
    "- sampler peak: \(.sampler.peak // "none") (\(.pct_of_cap_sampler_peak // "n/a") percent), max anon: \(.sampler.max_anon // "none") (\(.pct_of_cap_max_anon // "n/a") percent), max file cache: \(.sampler.max_file // "none"), max shmem: \(.sampler.max_shmem // "none"), max pids: \(.sampler.max_pids // "none")",
    "- cgroup events: max \(.sampler.events_max // "none"), oom \(.sampler.events_oom // "none"), oom_kill \(.sampler.events_oom_kill // "none"), manager oom_kill_events \(.manager.oom_kill_events // "none")",
    "- cap hit or OOM: \(if (.capped_reasons | length) > 0 then (.capped_reasons | join("; ")) else "no" end)"
  ' "${out}/result.json"
} | summary

if [[ "$(jq '.capped_reasons | length' "${out}/result.json")" -gt 0 ]]; then
  jq -r '.capped_reasons | join("; ")' "${out}/result.json" >"${out}/capped"
fi
exit "${status}"
