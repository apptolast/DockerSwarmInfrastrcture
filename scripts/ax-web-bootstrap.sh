#!/usr/bin/env bash
# Owner-run bootstrap of the AX web panel's credentials (docs/AX_WEB.md,
# «Arranque»). Nothing here is run by Ansible, and no key or token is ever
# printed, logged or passed in argv.
#
#   init  Issue a private ECDSA P-256 CA and two leaves with openssl in a
#         tmpfs directory under /run: the panel's server certificate (SAN
#         DNS:ax-web, serverAuth) and Traefik's client certificate (CN
#         edge-traefik, clientAuth). Create the Docker secrets Traefik mounts
#         (edge-ax-upstream-client-v1: client certificate and key in one PEM;
#         edge-ax-upstream-ca-v1: the CA certificate) and keep the server
#         certificate, key and CA certificate root-only in
#         /etc/dockerswarm/ax/web-tls. The CA key is deleted once both leaves
#         are signed. Existing secrets or material are never overwritten.
#   k8s   Create the Kubernetes Secrets ax-web/ax-web-tls (tls.crt, tls.key,
#         client-ca.crt) and ax-web/ax-web-agent (claude-oauth-token) with
#         `kubectl create secret generic --from-file`, from the root-only
#         files, in the namespace the ax-lab playbook created. Existing
#         Secrets are never overwritten. Run it again after a cluster
#         recreation.
#
# Both run as root under the host-global lock (host_global_operation_lock.py
# run), as every direct host mutation does.

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly HOST_LOCK_HELPER="${PROJECT_DIR}/scripts/host_global_operation_lock.py"
readonly SCRIPT_PATH="${SCRIPT_DIR}/${BASH_SOURCE[0]##*/}"

# config/ax-lab.yml (web, credential_directory, install_root, cluster) and
# the edge contract; tests/test_ax_web_deploy_contract.py pins them.
readonly CREDENTIAL_DIRECTORY=/etc/dockerswarm/ax
readonly TLS_DIRECTORY=/etc/dockerswarm/ax/web-tls
readonly AGENT_FILE=/etc/dockerswarm/ax/claude-oauth-token
readonly SERVER_NAME=ax-web
readonly CLIENT_COMMON_NAME=edge-traefik
readonly VALIDITY_DAYS=1095
readonly CLIENT_SECRET=edge-ax-upstream-client-v1
readonly CA_SECRET=edge-ax-upstream-ca-v1
readonly NAMESPACE=ax-web
readonly LAB_HOME=/opt/dockerswarm/ax-lab/home
readonly KUBECTL=/opt/dockerswarm/ax-lab/bin/kubectl
readonly KUBECONFIG_PATH=/opt/dockerswarm/ax-lab/home/.kube/config
readonly KUBE_CONTEXT=kind-kind

original_args=("$@")
workdir=""
staging=""
created_secrets=()

usage() {
  cat <<'EOF'
Usage:
  sudo -- ./scripts/ax-web-bootstrap.sh init
  sudo -- ./scripts/ax-web-bootstrap.sh k8s

init: the private CA, the panel and Traefik certificates, the two Docker
secrets and /etc/dockerswarm/ax/web-tls. k8s: the Kubernetes Secrets
ax-web-tls and ax-web-agent. Nothing existing is ever overwritten.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

ensure_host_global_lock() {
  local operation=$1
  [[ -f "${HOST_LOCK_HELPER}" && ! -L "${HOST_LOCK_HELPER}" ]] ||
    fail "host-global operation-lock helper is absent or unsafe"
  if [[ -n "${DOCKERSWARM_IAC_LOCK_SCOPE:-}" ]]; then
    /usr/bin/python3 "${HOST_LOCK_HELPER}" \
      prove --operation "${operation}" >/dev/null ||
      fail "host-global operation-lock proof failed"
    return
  fi
  exec /usr/bin/python3 "${HOST_LOCK_HELPER}" \
    run --operation "${operation}" -- \
    "${SCRIPT_PATH}" "${original_args[@]}"
}

cleanup() {
  local status=$?
  local name
  if ((status != 0)); then
    # Only what this run created and did not finish: never anything older.
    for name in "${created_secrets[@]}"; do
      docker secret rm "${name}" >/dev/null 2>&1 || true
    done
    if [[ -n "${staging}" ]]; then
      rm -rf -- "${staging}"
    fi
  fi
  if [[ -n "${workdir}" ]]; then
    rm -rf -- "${workdir}"
  fi
  return "${status}"
}
trap cleanup EXIT

# A root-owned, root-only regular file or directory, never a link.
require_private() {
  local path=$1 kind=$2 mode=$3
  [[ ! -L "${path}" ]] || fail "${path} is a symbolic link"
  if [[ "${kind}" == directory ]]; then
    [[ -d "${path}" ]] || fail "${path} is not a directory"
  else
    [[ -f "${path}" ]] || fail "${path} is not a regular file"
  fi
  [[ "$(stat --format '%u:%g %a' -- "${path}")" == "0:0 ${mode}" ]] ||
    fail "${path} is not root:root ${mode}"
}

secret_exists() {
  docker secret inspect "$1" >/dev/null 2>&1
}

# One leaf signed by the CA: its own P-256 key, a random serial, 3 years.
issue_leaf() {
  local name=$1 subject=$2 extensions=$3
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
    -out "${workdir}/${name}.key" 2>/dev/null
  openssl req -new -key "${workdir}/${name}.key" -subj "${subject}" \
    -out "${workdir}/${name}.csr"
  printf '%s\n' "${extensions}" >"${workdir}/${name}.ext"
  openssl x509 -req -in "${workdir}/${name}.csr" \
    -CA "${workdir}/ca.crt" -CAkey "${workdir}/ca.key" \
    -set_serial "0x$(openssl rand -hex 16)" -days "${VALIDITY_DAYS}" \
    -sha256 -extfile "${workdir}/${name}.ext" \
    -out "${workdir}/${name}.crt" 2>/dev/null
}

bootstrap_init() {
  local name
  require_private "${CREDENTIAL_DIRECTORY}" directory 700
  [[ ! -e "${TLS_DIRECTORY}" && ! -L "${TLS_DIRECTORY}" ]] ||
    fail "${TLS_DIRECTORY} already exists: its material is never replaced"
  for name in "${CLIENT_SECRET}" "${CA_SECRET}"; do
    ! secret_exists "${name}" ||
      fail "Docker secret ${name} already exists: it is never replaced"
  done

  # tmpfs: the CA key never reaches a disk.
  workdir="$(mktemp -d /run/ax-web-bootstrap.XXXXXXXX)"
  chmod 0700 -- "${workdir}"

  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
    -out "${workdir}/ca.key" 2>/dev/null
  openssl req -x509 -new -key "${workdir}/ca.key" -sha256 \
    -days "${VALIDITY_DAYS}" -subj "/CN=ax-web private CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "${workdir}/ca.crt"
  issue_leaf server "/CN=${SERVER_NAME}" \
    "$(printf '%s\n' \
      'basicConstraints=critical,CA:FALSE' \
      'keyUsage=critical,digitalSignature' \
      'extendedKeyUsage=serverAuth' \
      "subjectAltName=DNS:${SERVER_NAME}")"
  issue_leaf client "/CN=${CLIENT_COMMON_NAME}" \
    "$(printf '%s\n' \
      'basicConstraints=critical,CA:FALSE' \
      'keyUsage=critical,digitalSignature' \
      'extendedKeyUsage=clientAuth')"
  # Nothing else is ever signed by this CA.
  rm -f -- "${workdir}/ca.key"

  openssl verify -CAfile "${workdir}/ca.crt" -purpose sslserver \
    -verify_hostname "${SERVER_NAME}" "${workdir}/server.crt" >/dev/null ||
    fail "the server certificate does not verify"
  openssl verify -CAfile "${workdir}/ca.crt" -purpose sslclient \
    "${workdir}/client.crt" >/dev/null ||
    fail "the client certificate does not verify"

  # The server material goes next to the credentials first, under a
  # temporary name, and only takes its final name once both secrets exist.
  staging="$(mktemp -d "${CREDENTIAL_DIRECTORY}/.web-tls.XXXXXXXX")"
  chmod 0700 -- "${staging}"
  install -m 0600 -o root -g root -- "${workdir}/server.crt" "${staging}/tls.crt"
  install -m 0600 -o root -g root -- "${workdir}/server.key" "${staging}/tls.key"
  install -m 0600 -o root -g root -- "${workdir}/ca.crt" "${staging}/client-ca.crt"

  # Traefik reads certFile and keyFile from the same PEM (docs/AX_WEB.md).
  cat -- "${workdir}/client.crt" "${workdir}/client.key" | docker secret create \
    --label com.apptolast.managed-by=manual-bootstrap \
    --label com.apptolast.purpose=traefik-upstream-mtls \
    "${CLIENT_SECRET}" - >/dev/null
  created_secrets+=("${CLIENT_SECRET}")
  docker secret create \
    --label com.apptolast.managed-by=manual-bootstrap \
    --label com.apptolast.purpose=traefik-upstream-mtls \
    "${CA_SECRET}" "${workdir}/ca.crt" >/dev/null
  created_secrets+=("${CA_SECRET}")

  mv -T -- "${staging}" "${TLS_DIRECTORY}"
  staging=""
  created_secrets=()

  printf 'Docker secrets %s and %s created.\n' "${CLIENT_SECRET}" "${CA_SECRET}"
  printf 'Panel certificate, key and client CA kept in %s.\n' "${TLS_DIRECTORY}"
  printf 'Record these expiry dates in docs/DEPLOYMENT_STATUS.md:\n'
  for name in ca server client; do
    printf '  %s: %s\n' "${name}" \
      "$(openssl x509 -in "${workdir}/${name}.crt" -noout -enddate)"
  done
}

kubectl_lab() {
  HOME="${LAB_HOME}" "${KUBECTL}" --kubeconfig "${KUBECONFIG_PATH}" \
    --context "${KUBE_CONTEXT}" --request-timeout 10s "$@"
}

bootstrap_k8s() {
  local name
  require_private "${TLS_DIRECTORY}" directory 700
  for name in tls.crt tls.key client-ca.crt; do
    require_private "${TLS_DIRECTORY}/${name}" file 600
  done
  require_private "${AGENT_FILE}" file 600
  require_private "${KUBECONFIG_PATH}" file 600
  [[ -x "${KUBECTL}" && ! -L "${KUBECTL}" ]] || fail "${KUBECTL} is missing"

  # The ax-lab playbook creates and owns the namespace; this script never
  # does. It stops there until these Secrets exist.
  [[ "$(kubectl_lab get namespace "${NAMESPACE}" --ignore-not-found \
    --output 'jsonpath={.metadata.labels.com\.apptolast\.managed-by}')" == ansible ]] ||
    fail "namespace ${NAMESPACE} is missing: apply the ax-lab playbook first"
  for name in ax-web-tls ax-web-agent; do
    [[ -z "$(kubectl_lab get secret "${name}" --namespace "${NAMESPACE}" \
      --ignore-not-found --output name)" ]] ||
      fail "Secret ${NAMESPACE}/${name} already exists: it is never replaced"
  done

  kubectl_lab create secret generic ax-web-tls --namespace "${NAMESPACE}" \
    --from-file="tls.crt=${TLS_DIRECTORY}/tls.crt" \
    --from-file="tls.key=${TLS_DIRECTORY}/tls.key" \
    --from-file="client-ca.crt=${TLS_DIRECTORY}/client-ca.crt" >/dev/null
  kubectl_lab create secret generic ax-web-agent --namespace "${NAMESPACE}" \
    --from-file="claude-oauth-token=${AGENT_FILE}" >/dev/null
  for name in ax-web-tls ax-web-agent; do
    kubectl_lab label secret "${name}" --namespace "${NAMESPACE}" \
      com.apptolast.managed-by=manual-bootstrap >/dev/null
  done
  printf 'Secrets %s/ax-web-tls and %s/ax-web-agent created.\n' \
    "${NAMESPACE}" "${NAMESPACE}"
}

(($# == 1)) || {
  usage >&2
  exit 64
}
case "$1" in
  init | k8s) ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 64
    ;;
esac
((EUID == 0)) || fail "run it as root: sudo -- ./scripts/ax-web-bootstrap.sh $1"
ensure_host_global_lock "ax-web-bootstrap-$1"

for command_name in docker install mktemp openssl stat; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done

if [[ "$1" == init ]]; then
  bootstrap_init
else
  bootstrap_k8s
fi
