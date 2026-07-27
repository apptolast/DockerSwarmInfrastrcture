#!/usr/bin/env bash

set -Eeuo pipefail
set +x
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
readonly ALLOWED_SIGNERS="${PROJECT_DIR}/infra/terraform/host-readiness.allowed-signers"
readonly SIGNER_IDENTITY="apptolast-terraform-host-readiness"
readonly SIGNATURE_NAMESPACE="terraform-host-readiness"
# Must stay identical to HOST_READINESS_TARGET_HOSTNAME/DNS_PLATFORM_IPV4 in
# scripts/terraform-safety.py and to edge_traefik_hostname/
# platform_public_ipv4 in config/platform.yml -- a drift here would let this
# script attest a hostname/IP terraform-safety.py never asked about.
readonly TARGET_HOSTNAME="edge.apptolast.com"
readonly TARGET_IPV4="159.195.156.57"
readonly SWARM_SERVICE_LABEL="com.docker.swarm.service.name=edge_traefik"
readonly VALIDITY_SECONDS=3600

root_name=""
operator=""
proof_output=""
signing_key=""

usage() {
  cat <<'EOF'
Usage:
  host-readiness-probe.sh --root cloudflare/apptolast-dns
      --operator NAME --proof-output PATH --signing-key PATH

Proves, right now, against the real host, that the platform edge is ready to
receive a DNS cutover: exactly one running edge_traefik Swarm task reporting
a healthy Docker healthcheck, and a real HTTPS request for
https://edge.apptolast.com/ping -- resolved directly at 159.195.156.57 via
curl --resolve, independent of DNS -- returning exactly "OK" over a
certificate the system genuinely trusts. It performs no writes to Docker,
Terraform state, or DNS, and it only ever targets the production edge; it
has no ACME-staging mode, because a staging certificate must never satisfy a
production cutover gate.

Requires root (it inspects Docker Swarm task health); invoke it with
sudo -- ./scripts/host-readiness-probe.sh ...

On success it writes a non-secret, 1-hour proof signed with the dedicated
host-readiness key, only if every check passes; on any failure it writes
nothing. terraform-safety.py accepts this proof only when it is signed by
the exact key registered in the tracked host-readiness.allowed-signers
registry, only while it remains fresh, and only for the DNS record it
explicitly names.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while (( $# > 0 )); do
  case "$1" in
    --root)
      (( $# >= 2 )) || fail "--root requires a value"
      root_name="$2"
      shift 2
      ;;
    --operator)
      (( $# >= 2 )) || fail "--operator requires a value"
      operator="$2"
      shift 2
      ;;
    --proof-output)
      (( $# >= 2 )) || fail "--proof-output requires a path"
      proof_output="$2"
      shift 2
      ;;
    --signing-key)
      (( $# >= 2 )) || fail "--signing-key requires a path"
      signing_key="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "unknown argument: $1"
      ;;
  esac
done

[[ "${root_name}" == "cloudflare/apptolast-dns" ]] ||
  fail "--root must be cloudflare/apptolast-dns"
[[ -n "${operator}" ]] || fail "--operator must not be empty"
[[ -n "${proof_output}" ]] || fail "--proof-output is required"
[[ -n "${signing_key}" ]] || fail "--signing-key is required"
[[ "$(id -u)" -eq 0 ]] ||
  fail "must run as root to inspect Docker Swarm task health"

for command_name in curl date docker git install mktemp realpath ssh-keygen stat; do
  command -v "${command_name}" >/dev/null ||
    fail "required command not found: ${command_name}"
done
[[ -x "${PYTHON_BIN}" ]] ||
  fail "run scripts/bootstrap-tooling.sh first"
[[ -f "${ALLOWED_SIGNERS}" && ! -L "${ALLOWED_SIGNERS}" ]] ||
  fail "approved tracked host-readiness signer registry is absent"
git -C "${PROJECT_DIR}" ls-files --error-unmatch \
  "infra/terraform/host-readiness.allowed-signers" >/dev/null ||
  fail "host-readiness.allowed-signers must be tracked"

[[ -f "${signing_key}" && ! -L "${signing_key}" ]] ||
  fail "input must be a regular, non-symlink file: ${signing_key}"
case "$(stat --format='%a' "${signing_key}")" in
  400|600)
    ;;
  *)
    fail "host-readiness signing key must be mode 0400 or 0600"
    ;;
esac
signing_key="$(realpath "${signing_key}")"
proof_output="$(realpath -m "${proof_output}")"
[[ ! -e "${proof_output}" ]] ||
  fail "refusing to overwrite an existing host-readiness proof"
[[ ! -e "${proof_output}.sig" ]] ||
  fail "refusing to overwrite an existing host-readiness proof signature"
project_real="$(realpath "${PROJECT_DIR}")"
case "${proof_output}" in
  "${project_real}"|"${project_real}/"*)
    fail "host-readiness proofs are expiring operational evidence and stay outside Git"
    ;;
esac
install --directory --mode=0700 "$(dirname "${proof_output}")"

worktree_status="$(
  git -C "${PROJECT_DIR}" status --porcelain --untracked-files=normal
)"
[[ -z "${worktree_status}" ]] ||
  fail "commit or discard every worktree change before probing host readiness"
revision="$(git -C "${PROJECT_DIR}" rev-parse --verify HEAD)"
[[ "${revision}" =~ ^[a-f0-9]{40}$ ]] ||
  fail "cannot resolve the source commit"

proof_tmp=""
proof_signature_tmp=""
cleanup() {
  [[ -z "${proof_tmp}" || ! -f "${proof_tmp}" ]] ||
    find "${proof_tmp}" -maxdepth 0 -type f -delete
  [[ -z "${proof_signature_tmp}" || ! -f "${proof_signature_tmp}" ]] ||
    find "${proof_signature_tmp}" -maxdepth 0 -type f -delete
}
trap cleanup EXIT

task_ids="$(
  /usr/bin/docker ps \
    --filter "label=${SWARM_SERVICE_LABEL}" \
    --filter status=running \
    --format '{{.ID}}'
)"
task_count="$(printf '%s\n' "${task_ids}" | grep -c . || true)"
[[ "${task_count}" -eq 1 ]] ||
  fail "expected exactly one running edge_traefik task, found: ${task_count}"
task_health="$(
  /usr/bin/docker inspect --format '{{.State.Health.Status}}' "${task_ids}"
)"
[[ "${task_health}" == "healthy" ]] ||
  fail "edge_traefik task is not healthy: ${task_health}"

probe_output="$(
  /usr/bin/curl --fail --silent --show-error \
    --connect-timeout 5 --max-time 20 \
    --resolve "${TARGET_HOSTNAME}:443:${TARGET_IPV4}" \
    "https://${TARGET_HOSTNAME}/ping"
)"
[[ "${probe_output}" == "OK" ]] ||
  fail "HTTPS ping probe did not return OK: ${probe_output}"

issued_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
expires_at="$(
  date -u -d "@$(($(date -u '+%s') + VALIDITY_SECONDS))" '+%Y-%m-%dT%H:%M:%SZ'
)"
proof_tmp="$(mktemp "$(dirname "${proof_output}")/.host-readiness-proof.XXXXXX")"
"${PYTHON_BIN}" - \
  "${root_name}" "${operator}" "${revision}" "${issued_at}" "${expires_at}" \
  "${task_health}" "${probe_output}" "${TARGET_HOSTNAME}" "${TARGET_IPV4}" \
  "${proof_tmp}" <<'PY'
import json
import sys

(
    root,
    operator,
    revision,
    issued_at,
    expires_at,
    task_health,
    probe_result,
    target_hostname,
    target_ipv4,
    output_path,
) = sys.argv[1:]

document = {
    "schema": 1,
    "root": root,
    "source_commit": revision,
    "signer_identity": "apptolast-terraform-host-readiness",
    "target_hostname": target_hostname,
    "target_ipv4": target_ipv4,
    "swarm_task_health": task_health,
    "probe_result": probe_result,
    "operator": operator,
    "issued_at": issued_at,
    "expires_at": expires_at,
}
with open(output_path, "w", encoding="utf-8") as stream:
    stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
PY

ssh-keygen -Y sign \
  -f "${signing_key}" \
  -n "${SIGNATURE_NAMESPACE}" \
  "${proof_tmp}" >/dev/null
proof_signature_tmp="${proof_tmp}.sig"
[[ -s "${proof_signature_tmp}" && ! -L "${proof_signature_tmp}" ]] ||
  fail "ssh-keygen did not create a safe host-readiness signature"
ssh-keygen -Y verify \
  -f "${ALLOWED_SIGNERS}" \
  -I "${SIGNER_IDENTITY}" \
  -n "${SIGNATURE_NAMESPACE}" \
  -s "${proof_signature_tmp}" <"${proof_tmp}" >/dev/null ||
  fail "the signing key is not approved by host-readiness.allowed-signers"
mv --no-clobber "${proof_tmp}" "${proof_output}"
[[ ! -e "${proof_tmp}" && -f "${proof_output}" && ! -L "${proof_output}" ]] ||
  fail "host-readiness proof was not published atomically"
mv --no-clobber "${proof_signature_tmp}" "${proof_output}.sig"
[[ ! -e "${proof_signature_tmp}" &&
  -f "${proof_output}.sig" &&
  ! -L "${proof_output}.sig" ]] ||
  fail "host-readiness proof signature was not published atomically"
proof_tmp=""
proof_signature_tmp=""

printf 'Host-readiness proof written: %s\n' "${proof_output}"
printf 'Valid from %s until %s.\n' "${issued_at}" "${expires_at}"
