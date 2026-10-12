#!/usr/bin/env bash
# Scratch end-to-end run (never merged): the install and the first rollouts
# are over within two minutes, which is too short to see a pod that starts and
# then falls over. Wait, print the restart counters every minute, then run the
# same readiness gate and the same diagnostics again under a label of its own.
#
# usage: settle.sh <label> <seconds>
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

label="$1"
seconds="$2"
group "Let ${label} settle for ${seconds} s"
elapsed=0
while [[ "${elapsed}" -lt "${seconds}" ]]; do
  sleep 60
  elapsed=$((elapsed + 60))
  echo "t+${elapsed}s"
  kubectl_lab get pods --all-namespaces --no-headers \
    --output custom-columns='NS:.metadata.namespace,POD:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,RESTARTS:.status.containerStatuses[*].restartCount' |
    grep -E '^(ate-system|otel-system|podcertificate)' || true
done
endgroup
status=0
"${HERE}/verify.sh" wait_ready "settled-${label}" || status=1
"${HERE}/verify.sh" diag "settled-${label}" || true
exit "${status}"
