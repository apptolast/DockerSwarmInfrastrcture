#!/usr/bin/env bash
# Scratch end-to-end run (never merged): the production path. The owner's
# window does not install into an empty cluster: ate-setup runs over the
# lab's existing install of the OLD pins (origin/main's config/ax-lab.yml), so
# this builds the old Substrate images from the pristine checkout with the
# toolbox of that config, installs them under the lab's cap in a fresh cluster
# (the old-pin control: peak, readiness, restarts), moves the registry's tags
# to the re-pinned images with the manager's import and runs the same install
# command again over them. The re-pinned images were built by the shared
# steps and sit in the default layout.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

new_cfg="${CFG}"
old_cfg="${RUNNER_TEMP}/lab-old.json"
old_layout=/var/lib/ax-lab-ci/backup-old
status=0

group "The old pins: config/ax-lab.yml of origin/main"
git fetch --quiet --no-tags --depth=1 origin main
echo "origin/main is $(git rev-parse FETCH_HEAD)"
git show FETCH_HEAD:config/ax-lab.yml >"${RUNNER_TEMP}/ax-lab-main.yml"
.venv/bin/python -I - "${RUNNER_TEMP}/ax-lab-main.yml" >"${old_cfg}" <<'PY'
import json
import sys

import yaml

with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.dumps(yaml.safe_load(handle)["ax_lab"], sort_keys=True))
PY
jq -n --slurpfile old "${old_cfg}" --slurpfile new "${new_cfg}" '
  {old_toolbox: $old[0].images.toolbox, new_toolbox: $new[0].images.toolbox,
   old: $old[0].substrate.images, new: $new[0].substrate.images,
   same_version: ($old[0].substrate.version == $new[0].substrate.version),
   same_workloads: ($old[0].substrate.workloads == $new[0].substrate.workloads)}'
endgroup

# The build borrows the node's budget and refuses to run beside it.
"${HERE}/cluster.sh" delete

group "Build the old Substrate images from the pristine checkout (toolbox of the old config)"
sudo install -d -o root -g root -m 0700 /var/lib/ax-lab-ci/cache
for name in $(jq -r '.substrate.images | keys[]' "${old_cfg}"); do
  digest="$(jq -r --arg name "${name}" '.substrate.images[$name]' "${old_cfg}")"
  sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py build \
    --image "${name}=${digest}" \
    --toolbox-image "$(jq -r .images.toolbox "${old_cfg}")" \
    --source "${GITHUB_WORKSPACE}/substrate-src" \
    --cache /var/lib/ax-lab-ci/cache \
    --layout "${old_layout}" \
    --version "$(jq -r .substrate.version "${old_cfg}")" \
    --node-container "$(jq -r .cluster.node_container "${old_cfg}")" \
    --memory-mib "$(jq .substrate.build.memory_limit_mib "${old_cfg}")" \
    --memory-reservation-mib \
    "$(jq .substrate.build.memory_reservation_mib "${old_cfg}")" \
    --cpu-millicores "$(jq .substrate.build.cpu_limit_millicores "${old_cfg}")" \
    --pids-limit "$(jq .substrate.build.pids_limit "${old_cfg}")" \
    --timeout-seconds "$(jq .substrate.build.timeout_seconds "${old_cfg}")" \
    --min-mem-available-mib \
    "$(jq .substrate.build.min_mem_available_mib "${old_cfg}")" \
    --mem-available-floor-mib \
    "$(jq .substrate.build.mem_available_floor_mib "${old_cfg}")" || status=1
done
sudo rm -rf /var/lib/ax-lab-ci/cache
endgroup
[[ "${status}" -eq 0 ]]

"${HERE}/cluster.sh" create

export CFG="${old_cfg}"
cap="$(cfg .substrate.install.memory_limit_mib)"
LAYOUT="${old_layout}" "${HERE}/images.sh" import_substrate
"${HERE}/install.sh" "old${cap}" "${cap}" || status=1
"${HERE}/verify.sh" wait_ready "old${cap}" || status=1
"${HERE}/verify.sh" diag "old${cap}" || true
[[ "${status}" -eq 0 ]] || exit "${status}"

# The re-pinned images: the same tags move to the new digests.
export CFG="${new_cfg}"
cap="$(cfg .substrate.install.memory_limit_mib)"
"${HERE}/images.sh" import_substrate
"${HERE}/images.sh" registry_report
"${HERE}/install.sh" "upgrade${cap}" "${cap}" || status=1
"${HERE}/verify.sh" wait_ready "upgrade${cap}" || status=1
"${HERE}/verify.sh" diag "upgrade${cap}" || true
"${HERE}/verify.sh" identity "old${cap}" "upgrade${cap}" || status=1
exit "${status}"
