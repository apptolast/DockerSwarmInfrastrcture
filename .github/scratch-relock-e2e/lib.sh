#!/usr/bin/env bash
# Shared helpers of the scratch end-to-end run (never merged). Sourced by the
# other scripts of this directory, never executed. Everything is read from
# config/ax-lab.yml through ${CFG}, the `ax_lab` section dumped as JSON.

# shellcheck disable=SC2034  # the variables are read by the sourcing scripts
LAB=/var/lib/ax-lab-ci/lab
LAYOUT=/var/lib/ax-lab-ci/backup
SEED=/var/lib/ax-lab-ci/seed
KUBECONFIG_PATH="${LAB}/home/.kube/config"
CFG="${RUNNER_TEMP}/lab.json"
E2E="${RUNNER_TEMP}/e2e"
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MANAGER=(sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py)

# A value of the contract; fails when it is missing or null.
cfg() {
  jq -er "$1" "${CFG}"
}

kubectl_lab() {
  sudo env HOME="${LAB}/home" "${LAB}/bin/kubectl" \
    --kubeconfig "${KUBECONFIG_PATH}" \
    --context "kind-$(cfg .cluster.name)" --request-timeout 30s "$@"
}

kind_lab() {
  sudo env HOME="${LAB}/home" KUBECONFIG="${KUBECONFIG_PATH}" \
    "${LAB}/bin/kind" "$@"
}

# The Substrate images ate-setup installs, as the role derives them: the
# images of the pinned workloads that are keys of substrate.images.
installed_args() {
  jq -r '
    . as $lab
    | [.substrate.workloads[].images[]]
    | map(select(. as $i | $lab.substrate.images | has($i)))
    | unique | sort | .[]
    | "--image=\(.)=\($lab.substrate.images[.])"
  ' "${CFG}"
}

# Every pinned Substrate image, installer included.
all_args() {
  jq -r '.substrate.images | to_entries | sort_by(.key)[]
    | "--image=\(.key)=\(.value)"' "${CFG}"
}

# docker --cpus in the role's format.
cpus_of() {
  printf '%d.%03d' "$(($1 / 1000))" "$(($1 % 1000))"
}

group() {
  echo "::group::$*"
}

endgroup() {
  echo "::endgroup::"
}

# Append to the step summary and the log.
summary() {
  tee -a "${GITHUB_STEP_SUMMARY}"
}
