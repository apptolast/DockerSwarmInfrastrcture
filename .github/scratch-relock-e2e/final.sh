#!/usr/bin/env bash
# Scratch end-to-end run (never merged): the table of every install of the run
# and the gate. Each earlier step ran with continue-on-error so that all the
# evidence is collected in one iteration; this one fails the job when any
# required step did not succeed.
#
# env: OUTCOMES, one `name=outcome` per line
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

{
  echo ""
  echo "## Installs of ate-setup in this run"
  echo ""
  echo "| Run | Cap MiB | Exit | oom_killed | Seconds | Manager peak | % of cap | Sampled peak | Max anon | Max file | Max pids |"
  echo "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
  for result in "${E2E}"/*/result.json; do
    [[ -f "${result}" ]] || continue
    jq -r '
      "| \(.label) | \(.memory_mib) | \(.exit_status) | \(.manager.oom_killed | if . == null then "n/a" else tostring end) | \(.manager.seconds // "n/a") | \(.manager.memory_peak_bytes // "n/a") | \(.pct_of_cap_manager_peak // "n/a") | \(.sampler.peak // "n/a") | \(.sampler.max_anon // "n/a") | \(.sampler.max_file // "n/a") | \(.sampler.max_pids // "n/a") |"
    ' "${result}"
  done
} | summary

echo "${OUTCOMES}" | sed '/^$/d' | tee "${E2E}/outcomes.txt"
status=0
while IFS='=' read -r name outcome; do
  [[ -n "${name}" ]] || continue
  case "${name}:${outcome}" in
    escalate:success | escalate:skipped) ;;
    *:success) ;;
    *)
      echo "::error::step ${name} ended as ${outcome}"
      status=1
      ;;
  esac
done <"${E2E}/outcomes.txt"
exit "${status}"
