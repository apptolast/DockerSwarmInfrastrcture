#!/usr/bin/env bash
# Scratch end-to-end run (never merged): the two ko binaries, run from the lab
# registry the way the cluster would pull them, and the ax CLI built by the
# image workflow. A clean exit (0) or a usage exit (2) from --help is the
# expected answer; 124 means the binary ignored the flag and started, which
# also proves it runs; anything else fails and its output is printed. Nothing
# gets a network or a credential.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

mkdir -p "${E2E}"
ax_tag="$(cfg .ax.tag)"
port="$(cfg .registry.host_port)"
keep="${RUNNER_TEMP}/keep/ax"
failed=0
: >"${E2E}/smoke.txt"

smoke_image() {
  local name="$1" digest ref rc output
  digest="$(jq -r '.manifests[0].digest' "${keep}/ko-${name}/index.json")"
  ref="localhost:${port}/${name}:${ax_tag}@${digest}"
  group "smoke ${name}: ${ref}"
  docker pull "${ref}"
  docker image inspect --format \
    'os={{.Os}} arch={{.Architecture}} size={{.Size}} entrypoint={{json .Config.Entrypoint}} cmd={{json .Config.Cmd}} user={{.Config.User}}' \
    "${ref}"
  local arg
  for arg in --help version; do
    set +e
    output="$(timeout 30 docker run --rm --network none \
      --name "smoke-${name}" "${ref}" "${arg}" 2>&1)"
    rc=$?
    set -e
    docker rm --force "smoke-${name}" >/dev/null 2>&1 || true
    echo "--- ${name} ${arg}: exit ${rc}"
    head -n 40 <<<"${output}"
    case "${rc}" in
      0) verdict="clean exit" ;;
      2) verdict="usage exit" ;;
      124) verdict="ignored the argument and kept running (killed after 30 s)" ;;
      *) verdict="exit ${rc}" ;;
    esac
    echo "${name} ${arg}: ${verdict}" >>"${E2E}/smoke.txt"
    if [[ "${rc}" == 0 || "${rc}" == 2 || "${rc}" == 124 ]]; then
      echo "${name} runs: ${verdict}"
      endgroup
      return 0
    fi
  done
  echo "::error::${name} neither answered --help nor version cleanly"
  failed=1
  endgroup
}

for name in ax-controller ax-server; do
  smoke_image "${name}"
done

group "smoke ax CLI"
ax="${keep}/ax"
sha256sum "${ax}"
echo "config cli_sha256: $(cfg .ax.cli_sha256)"
file "${ax}" || true
go version -m "${ax}" | head -n 4 || true
home="$(mktemp -d)"
set +e
help_output="$(env -i HOME="${home}" PATH=/usr/bin:/bin "${ax}" help 2>&1)"
help_rc=$?
version_output="$(env -i HOME="${home}" PATH=/usr/bin:/bin "${ax}" version 2>&1)"
version_rc=$?
set -e
echo "--- ax help: exit ${help_rc}"
head -n 30 <<<"${help_output}"
echo "--- ax version: exit ${version_rc}"
head -n 20 <<<"${version_output}"
go_version="$(grep -Eo 'go1\.[0-9]+(\.[0-9]+)?' <<<"${version_output}" | head -n 1 || true)"
echo "ax help: exit ${help_rc}; ax version: exit ${version_rc}; Go version in its output: ${go_version:-none}" \
  >>"${E2E}/smoke.txt"
if [[ "${help_rc}" != 0 ]]; then
  echo "::error::ax help did not exit 0"
  failed=1
fi
if [[ -z "${go_version}" ]]; then
  echo "NOTE: ax version printed no Go version; go version -m above is what the binary was built with"
fi
endgroup

{
  echo ""
  echo "### Smoke runs"
  echo ""
  sed 's/^/- /' "${E2E}/smoke.txt"
} | summary

exit "${failed}"
