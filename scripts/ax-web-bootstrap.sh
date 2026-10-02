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
#   office [--replace]
#         Create the agent office's optional Kubernetes Secret
#         ax-web/ax-web-office with `kubectl create secret generic
#         --from-file`, from whichever of the root-only files exists (at
#         least one): /etc/dockerswarm/ax/codex/auth.json as codex-auth-json
#         (Codex's seed session) and /etc/dockerswarm/ax/github-token as
#         github-token (one token, or owner=token lines). An existing Secret
#         is never overwritten unless --replace is given: then it is deleted
#         and created again. The panel reads it without restarting.
#
# All run as root under the host-global lock (host_global_operation_lock.py
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
readonly CODEX_DIRECTORY=/etc/dockerswarm/ax/codex
readonly CODEX_AUTH_FILE=/etc/dockerswarm/ax/codex/auth.json
readonly GITHUB_TOKEN_FILE=/etc/dockerswarm/ax/github-token
readonly OFFICE_SECRET=ax-web-office
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
  sudo -- ./scripts/ax-web-bootstrap.sh office [--replace]

init: the private CA, the panel and Traefik certificates, the two Docker
secrets and /etc/dockerswarm/ax/web-tls. k8s: the Kubernetes Secrets
ax-web-tls and ax-web-agent. office: the optional Kubernetes Secret
ax-web-office, from /etc/dockerswarm/ax/codex/auth.json and
/etc/dockerswarm/ax/github-token, whichever exist. Nothing existing is ever
overwritten, but for office --replace, which deletes and creates
ax-web-office again.
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

# The lab's kubectl and kubeconfig, and the namespace the ax-lab playbook
# created and owns; this script never creates it.
require_lab_namespace() {
  require_private "${KUBECONFIG_PATH}" file 600
  [[ -x "${KUBECTL}" && ! -L "${KUBECTL}" ]] || fail "${KUBECTL} is missing"
  [[ "$(kubectl_lab get namespace "${NAMESPACE}" --ignore-not-found \
    --output 'jsonpath={.metadata.labels.com\.apptolast\.managed-by}')" == ansible ]] ||
    fail "namespace ${NAMESPACE} is missing: apply the ax-lab playbook first"
}

secret_in_namespace() {
  [[ -n "$(kubectl_lab get secret "$1" --namespace "${NAMESPACE}" \
    --ignore-not-found --output name)" ]]
}

bootstrap_k8s() {
  local name
  require_private "${TLS_DIRECTORY}" directory 700
  for name in tls.crt tls.key client-ca.crt; do
    require_private "${TLS_DIRECTORY}/${name}" file 600
  done
  require_private "${AGENT_FILE}" file 600

  # The ax-lab playbook stops until these Secrets exist.
  require_lab_namespace
  for name in ax-web-tls ax-web-agent; do
    ! secret_in_namespace "${name}" ||
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

# The office's optional Secret. Each source is used only when it exists,
# and then only as a root-only regular file; nothing ever reads it here:
# kubectl takes it by path.
bootstrap_office() {
  local replace=$1
  local sources=() keys=()
  require_private "${CREDENTIAL_DIRECTORY}" directory 700
  if [[ -e "${CODEX_AUTH_FILE}" || -L "${CODEX_AUTH_FILE}" ]]; then
    require_private "${CODEX_DIRECTORY}" directory 700
    require_private "${CODEX_AUTH_FILE}" file 600
    sources+=("--from-file=codex-auth-json=${CODEX_AUTH_FILE}")
    keys+=(codex-auth-json)
  fi
  if [[ -e "${GITHUB_TOKEN_FILE}" || -L "${GITHUB_TOKEN_FILE}" ]]; then
    require_private "${GITHUB_TOKEN_FILE}" file 600
    sources+=("--from-file=github-token=${GITHUB_TOKEN_FILE}")
    keys+=(github-token)
  fi
  ((${#sources[@]} > 0)) ||
    fail "neither ${CODEX_AUTH_FILE} nor ${GITHUB_TOKEN_FILE} exists"

  require_lab_namespace
  if secret_in_namespace "${OFFICE_SECRET}"; then
    [[ "${replace}" == true ]] ||
      fail "Secret ${NAMESPACE}/${OFFICE_SECRET} already exists: only office --replace replaces it"
    kubectl_lab delete secret "${OFFICE_SECRET}" --namespace "${NAMESPACE}" \
      --wait=true >/dev/null
    printf 'Secret %s/%s deleted.\n' "${NAMESPACE}" "${OFFICE_SECRET}"
  fi
  kubectl_lab create secret generic "${OFFICE_SECRET}" \
    --namespace "${NAMESPACE}" "${sources[@]}" >/dev/null ||
    fail "Secret ${NAMESPACE}/${OFFICE_SECRET} was not created: run office again"
  kubectl_lab label secret "${OFFICE_SECRET}" --namespace "${NAMESPACE}" \
    com.apptolast.managed-by=manual-bootstrap >/dev/null
  printf 'Secret %s/%s created with the keys:' "${NAMESPACE}" "${OFFICE_SECRET}"
  printf ' %s' "${keys[@]}"
  printf '.\n'
  printf '%s\n' \
    'The panel picks it up without restarting once the kubelet refreshes the' \
    'mounted Secret: it reads the GitHub tokens on each use and the Codex' \
    'session on each run, and keeps using a Codex session it renewed itself' \
    'while that one is newer (docs/AX_WEB.md, «Oficina»).'
}

# One subcommand, and --replace only after office.
replace=false
if (($# == 2)) && [[ "$1" == office && "$2" == --replace ]]; then
  replace=true
elif (($# != 1)); then
  usage >&2
  exit 64
fi
case "$1" in
  init | k8s | office) ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 64
    ;;
esac
((EUID == 0)) ||
  fail "run it as root: sudo -- ./scripts/ax-web-bootstrap.sh $1${2:+ $2}"
ensure_host_global_lock "ax-web-bootstrap-$1"

for command_name in docker install mktemp openssl stat; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done

case "$1" in
  init) bootstrap_init ;;
  k8s) bootstrap_k8s ;;
  office) bootstrap_office "${replace}" ;;
esac
