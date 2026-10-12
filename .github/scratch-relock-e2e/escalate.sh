#!/usr/bin/env bash
# Scratch end-to-end run (never merged): when the install of one cap was hit or
# OOM-killed (install.sh leaves a `capped` file for it), repeat it on a
# deleted and recreated cluster with the next larger cap, 256 MiB and then
# 512 MiB: a re-run over an installed cluster is a different workload. The
# registry keeps the images in its volume, so nothing is imported twice.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

previous="$(cfg .substrate.install.memory_limit_mib)"
status=0
for cap in 256 512; do
  [[ "${cap}" -gt "${previous}" ]] || continue
  if [[ ! -s "${E2E}/c${previous}/capped" ]]; then
    echo "The ${previous} MiB run did not hit its cap: no ${cap} MiB run."
    break
  fi
  echo "The ${previous} MiB run: $(cat "${E2E}/c${previous}/capped")"
  echo "Running the same install again with ${cap} MiB on a fresh cluster."
  "${HERE}/cluster.sh" delete
  "${HERE}/cluster.sh" create
  "${HERE}/images.sh" import_all
  "${HERE}/install.sh" "c${cap}" "${cap}" || status=1
  "${HERE}/verify.sh" wait_ready "c${cap}" || status=1
  "${HERE}/verify.sh" diag "c${cap}" || true
  previous="${cap}"
done
exit "${status}"
