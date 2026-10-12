#!/usr/bin/env bash
# Scratch end-to-end run (never merged): while the install runs, print the pods
# and the newest warning events every 30 s, so that a stuck rollout can be
# read from the log of one iteration. Only names, phases, counters and event
# messages are printed, never a pod's environment.
#
# usage: watch.sh <label>
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

label="$1"
started="${SECONDS}"
while sleep 30; do
  {
    echo "t+$((SECONDS - started))s"
    kubectl_lab get pods --all-namespaces --no-headers \
      --output custom-columns='NS:.metadata.namespace,POD:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,RESTARTS:.status.containerStatuses[*].restartCount' \
      2>&1 | head -n 40
    kubectl_lab get events --all-namespaces \
      --field-selector type=Warning --sort-by=.lastTimestamp 2>&1 |
      tail -n 8
  } 2>&1 | sed -u "s/^/[watch ${label}] /"
done
