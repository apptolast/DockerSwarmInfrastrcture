#!/usr/bin/env bash
# Scratch end-to-end run (never merged): a read-only sampler of the ate-setup
# container's memory cgroup, started before the manager's install and killed
# after it. The manager reads memory.peak every 5 s and keeps only the last 200
# lines of the container's log before it removes it; this reads the cgroup
# four times a second, splits what it holds into anonymous memory (what an OOM
# kill is about) and file cache (what only fills the cap), and follows the
# container's log into a file of its own.
#
# usage: sample.sh <csv> <log> <label>
set -uo pipefail

csv="$1"
logfile="$2"
label="$3"
name=ax-lab-ate-setup

printf 'epoch_us,current,peak,anon,file,shmem,swap,pids,ev_max,ev_oom,ev_oom_kill\n' >"${csv}"

id=""
until [[ -n "${id}" ]]; do
  id="$(docker inspect --format '{{.Id}}' "${name}" 2>/dev/null || true)"
  [[ -n "${id}" ]] || sleep 0.2
done

cgroup=""
for candidate in "/sys/fs/cgroup/system.slice/docker-${id}.scope" \
  "/sys/fs/cgroup/docker/${id}"; do
  if [[ -d "${candidate}" ]]; then
    cgroup="${candidate}"
    break
  fi
done
if [[ -z "${cgroup}" ]]; then
  echo "[sampler ${label}] no cgroup directory for ${id}"
  exit 0
fi

{
  echo "cgroup ${cgroup}"
  for file in memory.max memory.high memory.low memory.swap.max \
    memory.swap.high pids.max cpu.max; do
    printf '%s %s\n' "${file}" "$(<"${cgroup}/${file}")"
  done
} >"${csv%.csv}-limits.txt" 2>&1
cat "${csv%.csv}-limits.txt"

docker logs --follow --timestamps "${name}" 2>&1 |
  tee "${logfile}" | sed -u "s/^/[ate-setup ${label}] /" &
logger=$!
trap 'kill "${logger}" 2>/dev/null || true' EXIT

while [[ -d "${cgroup}" ]]; do
  current="" peak="" swap="" pids=""
  current="$(<"${cgroup}/memory.current")" || break
  peak="$(<"${cgroup}/memory.peak")" || break
  swap="$(<"${cgroup}/memory.swap.current")" || swap=0
  pids="$(<"${cgroup}/pids.current")" || pids=0
  anon=0 file=0 shmem=0
  while read -r key value; do
    case "${key}" in
      anon) anon="${value}" ;;
      file) file="${value}" ;;
      shmem) shmem="${value}" ;;
      *) ;;
    esac
  done <"${cgroup}/memory.stat" || break
  ev_max=0 ev_oom=0 ev_oom_kill=0
  while read -r key value; do
    case "${key}" in
      max) ev_max="${value}" ;;
      oom) ev_oom="${value}" ;;
      oom_kill) ev_oom_kill="${value}" ;;
      *) ;;
    esac
  done <"${cgroup}/memory.events" || break
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' "${EPOCHREALTIME/./}" \
    "${current}" "${peak}" "${anon}" "${file}" "${shmem}" "${swap}" \
    "${pids}" "${ev_max}" "${ev_oom}" "${ev_oom_kill}" >>"${csv}"
  sleep 0.25
done
echo "[sampler ${label}] cgroup gone after $(($(wc -l <"${csv}") - 1)) samples"
wait "${logger}" 2>/dev/null || true
