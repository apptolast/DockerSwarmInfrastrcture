#!/usr/bin/env bash

# Fail closed on the log of a gitleaks `git` scan, read from stdin.
#
# gitleaks v8 exits 0 when git itself fails inside its container: it logs
# the git error at ERR level, reports "0 commits scanned." and then "no leaks
# found". A linked worktree does exactly that when the directory its .git
# file names is not mounted, so the exit code alone cannot prove a scan. The
# scan only counts when its log has no warning-or-higher line, names exactly
# the commits the repository has, and reports a clean result.

set -Eeuo pipefail
IFS=$'\n\t'

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

(( $# == 1 )) || fail "usage: ${0##*/} EXPECTED_COMMITS < gitleaks.log"
readonly EXPECTED_COMMITS="$1"
[[ "${EXPECTED_COMMITS}" =~ ^[1-9][0-9]*$ ]] ||
  fail "expected commit count must be a positive integer"

# zerolog console lines are "<time> <LEVEL> <message>"; drop the ANSI colors
# and any carriage return a pseudo-terminal adds.
scan_log="$(sed -E 's/\x1B\[[0-9;]*m//g; s/\r$//')"

if grep -Eq '^[^ ]+ (WRN|ERR|FTL|PNC) ' <<<"${scan_log}"; then
  fail "gitleaks logged a warning or error during the history scan"
fi

mapfile -t scanned_counts < <(
  sed -En 's/^[^ ]+ INF ([0-9]+) commits scanned\.$/\1/p' <<<"${scan_log}"
)
(( ${#scanned_counts[@]} == 1 )) ||
  fail "gitleaks history scan did not report exactly one commit count"
[[ "${scanned_counts[0]}" == "${EXPECTED_COMMITS}" ]] ||
  fail "gitleaks scanned ${scanned_counts[0]} commits; the repository has ${EXPECTED_COMMITS}"

grep -Eq '^[^ ]+ INF no leaks found$' <<<"${scan_log}" ||
  fail "gitleaks history scan did not report a clean result"
