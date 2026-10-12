#!/usr/bin/env bash
# Scratch end-to-end run (never merged): what the role does to the host before
# the cluster exists. Prints what the runner is, so that a failure can be told
# apart from the lab host, applies the inotify limits kind needs, installs the
# pinned kind and kubectl by sha256, places the pristine Substrate checkout
# where the install reads it and renders the reviewed kind configuration.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

group "The runner, to compare with the lab host"
uname -a
head -n 4 /etc/os-release
nproc
free -m
cat /proc/swaps
df -h / /var /mnt || true
docker info --format \
  'docker {{.ServerVersion}}, cgroup driver {{.CgroupDriver}}, cgroup version {{.CgroupVersion}}, storage {{.Driver}}'
docker info 2>&1 | grep -iE 'swap|warning' || echo "no swap or cgroup warning"
sysctl kernel.apparmor_restrict_unprivileged_userns \
  kernel.unprivileged_userns_clone user.max_user_namespaces \
  fs.inotify.max_user_watches fs.inotify.max_user_instances \
  kernel.core_pattern || true
ls -l /dev/kvm || echo "no /dev/kvm, as on the lab host"
cat /sys/fs/cgroup/cgroup.controllers
endgroup

group "Free what the builds no longer need"
sudo rm -rf /var/lib/ax-lab-ci/cache "${RUNNER_TEMP}/go"
df -h / || true
endgroup

group "Docker daemon settings of the lab host (config/daemon.json, merged)"
existing='{}'
if sudo test -s /etc/docker/daemon.json; then
  existing="$(sudo cat /etc/docker/daemon.json)"
fi
echo "daemon.json on the runner: ${existing}"
jq -s '.[0] * .[1]' <(printf '%s' "${existing}") config/daemon.json \
  >"${RUNNER_TEMP}/daemon.json"
cat "${RUNNER_TEMP}/daemon.json"
sudo install -o root -g root -m 0644 "${RUNNER_TEMP}/daemon.json" \
  /etc/docker/daemon.json
sudo systemctl restart docker
for _ in $(seq 1 60); do
  docker info >/dev/null 2>&1 && break
  sleep 1
done
docker info --format \
  'docker {{.ServerVersion}}, cgroup driver {{.CgroupDriver}}, cgroup version {{.CgroupVersion}}, storage {{.Driver}}, log driver {{.LoggingDriver}}'
endgroup

group "Read the contract"
mkdir -p "${E2E}"
.venv/bin/python -I - <<'PY' >"${CFG}"
import json

import yaml

with open("config/ax-lab.yml", encoding="utf-8") as handle:
    print(json.dumps(yaml.safe_load(handle)["ax_lab"], sort_keys=True))
PY
jq '{kind: .binaries.kind.version, kubectl: .binaries.kubectl.version,
     kind_node: .images.kind_node, registry: .images.registry,
     cluster: .cluster, registry_container: .registry,
     version: .substrate.version, install: .substrate.install,
     toolbox: .images.toolbox, ax_tag: .ax.tag}' "${CFG}"
endgroup

group "inotify limits of the role (config sysctl)"
for key in $(jq -r '.sysctl | keys[]' "${CFG}"); do
  value="$(jq -er --arg key "${key}" '.sysctl[$key]' "${CFG}")"
  sudo sysctl -w "${key}=${value}"
done
endgroup

group "Lab directories, root-only as on the host"
sudo install -d -o root -g root -m 0755 /var/lib/ax-lab-ci
sudo install -d -o root -g root -m 0750 "${LAB}"
sudo install -d -o root -g root -m 0755 "${LAB}/bin" "${LAB}/src"
sudo install -d -o root -g root -m 0700 "${LAB}/home" "${LAB}/home/.kube" \
  "${LAB}/state"
endgroup

group "Pinned kind and kubectl, verified by sha256"
for name in kind kubectl; do
  url="$(cfg ".binaries.${name}.url")"
  sha="$(cfg ".binaries.${name}.sha256")"
  curl -fsSL --retry 3 --output "${RUNNER_TEMP}/${name}.bin" "${url}"
  echo "${sha}  ${RUNNER_TEMP}/${name}.bin" | sha256sum --check --strict
  sudo install -o root -g root -m 0755 "${RUNNER_TEMP}/${name}.bin" \
    "${LAB}/bin/${name}"
done
sudo "${LAB}/bin/kind" version
sudo "${LAB}/bin/kubectl" version --client
endgroup

group "The pristine Substrate checkout the install reads"
[[ "$(git -C substrate-src rev-parse HEAD)" == "$(cfg .sources.substrate.commit)" ]]
[[ -z "$(git -C substrate-src status --porcelain --untracked-files=all)" ]]
sudo cp -a substrate-src "${LAB}/src/substrate"
sudo chown -R root:root "${LAB}/src/substrate"
sudo test -f "${LAB}/src/substrate/go.mod"
if sudo test -d "${LAB}/src/substrate/vendor"; then
  echo "pristine checkout in place; it carries the upstream vendor directory"
else
  echo "pristine checkout in place; it has no vendor directory"
fi
endgroup

group "The reviewed kind configuration, rendered by the validator"
.venv/bin/python scripts/validate-ax-lab.py \
  --kind-config-output "${RUNNER_TEMP}/kind-config.yaml"
sudo install -o root -g root -m 0640 "${RUNNER_TEMP}/kind-config.yaml" \
  "${LAB}/kind-config.yaml"
cat "${RUNNER_TEMP}/kind-config.yaml"
.venv/bin/python scripts/validate-ax-lab.py --kind-config-sha256
endgroup
