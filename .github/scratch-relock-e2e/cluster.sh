#!/usr/bin/env bash
# Scratch end-to-end run (never merged): the lab cluster, as the role's
# cluster.yml and node.yml create it. `create` makes the kind cluster from the
# reviewed configuration and the pinned node image, converges the limits of
# the node, starts the registry on the kind network with a containerd mirror
# and waits for the API and the node. `delete` removes the cluster and the
# registry container, whose named volume keeps the imported images.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

registry_name="$(cfg .registry.container)"
registry_port="$(cfg .registry.host_port)"
node="$(cfg .cluster.node_container)"
network="$(cfg .cluster.network)"

start_registry() {
  local -a publish=()
  local address
  for address in "$@"; do
    publish+=("--publish=${address}:${registry_port}:$(cfg .registry.container_port)")
  done
  local memory reservation
  memory="$(cfg .registry.resources.memory_limit_mib)"
  reservation="$(cfg .registry.resources.memory_reservation_mib)"
  docker run --detach \
    --name "${registry_name}" \
    --restart "$(cfg .registry.restart_policy)" \
    --memory "${memory}m" --memory-swap "${memory}m" \
    --memory-reservation "${reservation}m" \
    --cpus "$(cpus_of "$(cfg .registry.resources.cpu_limit_millicores)")" \
    --pids-limit "$(cfg .registry.resources.pids_limit)" \
    --label com.apptolast.managed-by=ansible \
    --label com.apptolast.ax-lab=registry \
    --network "${network}" \
    --mount "type=volume,source=$(cfg .registry.volume),target=/var/lib/registry" \
    "${publish[@]}" \
    "$(cfg .images.registry)"
}

create() {
  group "kind create cluster, as the role runs it"
  kind_lab create cluster \
    --name "$(cfg .cluster.name)" \
    --config "${LAB}/kind-config.yaml" \
    --image "$(cfg .images.kind_node)" \
    --kubeconfig "${KUBECONFIG_PATH}"
  sudo stat --format '%U:%G %a %n' "${KUBECONFIG_PATH}"
  endgroup

  group "Limits and restart policy of the node (docker update, as the role)"
  local memory
  memory="$(cfg .cluster.resources.memory_limit_mib)"
  docker update \
    --memory "${memory}m" --memory-swap "${memory}m" \
    --memory-reservation \
    "$(cfg .cluster.resources.memory_reservation_mib)m" \
    --cpus "$(cpus_of "$(cfg .cluster.resources.cpu_limit_millicores)")" \
    --pids-limit "$(cfg .cluster.resources.pids_limit)" \
    --restart "$(cfg .cluster.restart_policy)" \
    "${node}"
  docker inspect --format \
    'node: memory={{.HostConfig.Memory}} swap={{.HostConfig.MemorySwap}} nanocpus={{.HostConfig.NanoCpus}} pids={{.HostConfig.PidsLimit}} restart={{.HostConfig.RestartPolicy.Name}}' \
    "${node}"
  endgroup

  group "Local registry on the kind network, published on the loopback"
  if ! start_registry 127.0.0.1 "[::1]"; then
    echo "DEVIATION: publishing on [::1] failed on this runner; loopback IPv4 only"
    docker rm --force "${registry_name}" || true
    start_registry 127.0.0.1
  fi
  docker inspect --format \
    'registry: memory={{.HostConfig.Memory}} swap={{.HostConfig.MemorySwap}} nanocpus={{.HostConfig.NanoCpus}} pids={{.HostConfig.PidsLimit}} networks={{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}ports={{json .HostConfig.PortBindings}}' \
    "${registry_name}"
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${registry_port}/v2/" >/dev/null; then
      break
    fi
    sleep 1
  done
  curl -fsS "http://127.0.0.1:${registry_port}/v2/"
  echo
  endgroup

  group "Inside the node: proxy ARP and NDP, registry mirror"
  docker exec "${node}" /usr/sbin/sysctl -w net.ipv4.conf.all.proxy_arp=1
  docker exec "${node}" /usr/sbin/sysctl -e -w net.ipv6.conf.all.proxy_ndp=1
  local hosts="/etc/containerd/certs.d/localhost:${registry_port}"
  docker exec "${node}" /usr/bin/mkdir -p "${hosts}"
  printf '[host."http://%s:%s"]\n' "${registry_name}" \
    "$(cfg .registry.container_port)" |
    docker exec --interactive "${node}" /usr/bin/cp /dev/stdin \
      "${hosts}/hosts.toml"
  docker exec "${node}" /usr/bin/cat "${hosts}/hosts.toml"
  endgroup

  group "Wait for the API server and the node"
  local ready=""
  for _ in $(seq 1 30); do
    ready="$(kubectl_lab get --raw /readyz 2>/dev/null || true)"
    [[ "${ready}" == "ok" ]] && break
    sleep 10
  done
  [[ "${ready}" == "ok" ]]
  local status=""
  for _ in $(seq 1 30); do
    status="$(
      kubectl_lab get node "${node}" --output \
        'jsonpath={.status.conditions[?(@.type=="Ready")].status}' \
        2>/dev/null || true
    )"
    [[ "${status}" == "True" ]] && break
    sleep 10
  done
  [[ "${status}" == "True" ]]
  kubectl_lab get nodes --output wide
  docker exec "${node}" /usr/bin/cat /sys/fs/cgroup/memory.max
  endgroup

  group "KEP-1755 ConfigMap that documents the registry"
  jq -n --arg host "localhost:${registry_port}" '{
    apiVersion: "v1", kind: "ConfigMap",
    metadata: {name: "local-registry-hosting", namespace: "kube-public"},
    data: {"localRegistryHosting.v1":
      ("host: \"" + $host + "\"\nhelp: \"https://kind.sigs.k8s.io/docs/user/local-registry/\"\n")}
  }' | kubectl_lab apply --server-side --field-manager ax-lab --filename -
  endgroup
}

delete() {
  group "Delete the cluster and the registry container (the volume stays)"
  docker rm --force "${registry_name}" || true
  kind_lab delete cluster --name "$(cfg .cluster.name)"
  sudo rm -f "${KUBECONFIG_PATH}"
  docker ps --all --format '{{.Names}} {{.Status}}'
  endgroup
}

"$@"
