#!/usr/bin/env bash
# Scratch end-to-end run (never merged): the images move into the lab
# registry exactly as the role moves them, through the manager and never
# through docker pull and push. `seed_ko` copies the two ko layouts the build
# produced into the root-only layout the Substrate build already filled (what
# the owner's window does with seed-layout). `import_substrate` and
# `import_ax` import the seven Substrate images and the two ko images with the
# manager's import command and the tags the config names, and require every
# one to be pinned in the registry; `import_all` does both and reports what the
# registry serves.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

ax_tag="$(cfg .ax.tag)"
tag="$(cfg .substrate.version)"
registry="127.0.0.1:$(cfg .registry.host_port)"
keep="${RUNNER_TEMP}/keep/ax"
names=(ax-controller ax-server)

# The ko images as built, by the digest their layouts list.
ko_args() {
  local name
  for name in "${names[@]}"; do
    printf -- '--image=%s=%s\n' "${name}" \
      "$(jq -r '.manifests[0].digest' "${keep}/ko-${name}/index.json")"
  done
}

seed_ko() {
  group "Seed the two ko layouts into the root-only layout"
  sudo install -d -o root -g root -m 0755 "${SEED}"
  sudo install -d -o root -g root -m 0700 "${SEED}/in-ax"
  (
    cd "${keep}"
    /usr/bin/python3 -m zipfile -c "${RUNNER_TEMP}/ax-ko.zip" \
      ko-ax-controller ko-ax-server
  )
  sudo -- /usr/bin/python3 -I -m zipfile -e "${RUNNER_TEMP}/ax-ko.zip" \
    "${SEED}/in-ax"
  local name pin
  for name in "${names[@]}"; do
    pin="$(jq -r '.manifests[0].digest' "${keep}/ko-${name}/index.json")"
    echo "${name}: built ${pin}, config pin $(cfg ".ax.images[\"${name}\"]")"
    "${MANAGER[@]}" seed-layout --image-set ax \
      --source "${SEED}/in-ax/ko-${name}" --layout "${LAYOUT}" \
      --tag "${ax_tag}" "--image=${name}=${pin}"
  done
  endgroup
}

import_substrate() {
  local -a substrate_pins
  mapfile -t substrate_pins < <(all_args)
  [[ "${#substrate_pins[@]}" -eq 7 ]]
  group "Import the seven Substrate images (${CFG##*/}, layout ${LAYOUT})"
  "${MANAGER[@]}" import --registry "${registry}" --layout "${LAYOUT}" \
    --tag "${tag}" "${substrate_pins[@]}"
  endgroup
  group "image-status: the Substrate images pinned in the registry, complete in the layout"
  "${MANAGER[@]}" image-status --registry "${registry}" --layout "${LAYOUT}" \
    --tag "${tag}" "${substrate_pins[@]}" |
    tee "${RUNNER_TEMP}/status-substrate.json"
  jq --exit-status '
    (.registry | to_entries | all(.value == "pinned"))
    and (.backup | to_entries | all(.value == "complete"))
  ' "${RUNNER_TEMP}/status-substrate.json"
  endgroup
}

import_ax() {
  local -a ko_pins
  mapfile -t ko_pins < <(ko_args)
  [[ "${#ko_pins[@]}" -eq 2 ]]
  group "Import the two ko images"
  "${MANAGER[@]}" import --image-set ax --registry "${registry}" \
    --layout "${LAYOUT}" --tag "${ax_tag}" "${ko_pins[@]}"
  endgroup
  group "image-status: the ko images pinned in the registry, complete in the layout"
  "${MANAGER[@]}" image-status --image-set ax --registry "${registry}" \
    --layout "${LAYOUT}" --tag "${ax_tag}" "${ko_pins[@]}" |
    tee "${RUNNER_TEMP}/status-ax.json"
  jq --exit-status '
    (.registry | to_entries | all(.value == "pinned"))
    and (.backup | to_entries | all(.value == "complete"))
  ' "${RUNNER_TEMP}/status-ax.json"
  endgroup
}

registry_report() {
  group "What the registry serves"
  curl -fsS "http://${registry}/v2/_catalog"
  echo
  local name
  for name in $(curl -fsS "http://${registry}/v2/_catalog" | jq -r '.repositories[]'); do
    echo "${name}: $(curl -fsS "http://${registry}/v2/${name}/tags/list" | jq -c .tags)"
  done
  endgroup

  group "No OOM kill in the registry while it took the images"
  local id
  id="$(docker inspect --format '{{.Id}}' "$(cfg .registry.container)")"
  sudo cat "/sys/fs/cgroup/system.slice/docker-${id}.scope/memory.events"
  sudo grep -qx 'oom_kill 0' \
    "/sys/fs/cgroup/system.slice/docker-${id}.scope/memory.events"
  endgroup
}

import_all() {
  import_substrate
  import_ax
  registry_report
}

"$@"
