# CLAUDE.md

This is the Infrastructure-as-Code repository for `apptolast`: a
single-manager/single-worker production Docker Swarm cluster, built from
Terraform (providers) and Ansible (host, Swarm, stacks). It is the single
source of truth for that server.

The golden rule, stated in [`README.md`](README.md), is:

> La regla de oro es que un servidor perdido se reconstruye desde un commit
> revisado más los secretos y backups externos. Ninguna configuración manual
> del host se considera estado válido si no queda codificada o documentada
> aquí.

In English: a lost server is rebuilt from a reviewed commit plus external
secrets/backups only. No manual host configuration is valid state unless it
is codified or documented in this repository. Every action you take here
should be judged against that rule.

## Fail-closed philosophy

The cluster has zero high availability. It is a single manager/worker node;
per README.md ("Límites") a reboot or VPS loss takes down the control plane
and every workload with it, and per
[`docs/OPERATIONS.md`](docs/OPERATIONS.md) ("Topología") the cluster
"tolera cero fallos del manager" (tolerates zero manager failures). Because
there is no second node to fail over to, every script in this repo treats
ambiguous state — a crashed writer, a stale lock, a dirty worktree,
unverifiable process ancestry — as unsafe to proceed past, never as
something to auto-clean. A wrong guess here has no redundancy to absorb it.

Concrete manifestations you will run into:

- Locks and markers never expire by age; a crash leaves them in place
  indefinitely.
- Recovering a marker always requires explicit, evidenced recovery (proof
  the original holder is gone), never silent or automatic cleanup.
- File reads of sensitive/root-owned files use an O_NOFOLLOW + `fstat`
  TOCTOU-safe idiom throughout `scripts/` (see the house-style section
  below), not a plain `open()`/`stat()`.
- Terraform wrappers demand zero warnings, zero stray diagnostics, and fail
  closed on ambiguous NDJSON output even when Terraform itself exits `0`.
- STOP gates reject any override/bypass flag or invented credential; there
  is no `-force`, no `-lock=false`, no fabricated value that satisfies them.

If you are tempted to add a `--force`/`--skip-checks` flag or delete a
lock/marker file to get unstuck, stop and read the recovery procedure below
instead — that is exactly the shortcut this repo is designed to refuse.

## Validation workflow

Run, in this exact order:

```bash
./scripts/bootstrap-tooling.sh
./scripts/validate-iac.sh
./scripts/lint.sh
```

`scripts/bootstrap-tooling.sh` installs/pins the pinned Terraform binary
under `.tools/terraform` and creates the Python virtualenv under `.venv`
(Ansible, lint, and validation dependencies). `scripts/validate-iac.sh`
requires that tooling to already exist: it checks for
`.venv/bin/ansible-playbook` and `.tools/terraform` and fails immediately
with an explicit "run scripts/bootstrap-tooling.sh first" message if either
is missing. Do not try to work around that check — run bootstrap first.

**Node.js / PATH gotcha for lint.sh.** `scripts/lint.sh` hard-requires
`bash`, `docker`, `dockerd`, `git`, `jq`, and `npx` on `PATH`, checked in a
loop, and fails immediately with `required command not found` if any one is
missing. In particular, Node.js (needed only so `npx` can run
`markdownlint-cli2`) must be installed even though nothing else in this
repository needs Node — `scripts/bootstrap-tooling.sh` does not install it
for you.

Separately: wherever this repo's scripts invoke the Python lock helper
under `sudo`, they hardcode `/usr/bin/python3` rather than a bare
`python3`. This is deliberate: `sudo` resets `PATH` to its own
`secure_path`, but a shell alias, function, or this project's own `.venv`
could otherwise shadow `python3` with an unprivileged, non-root-trusted
interpreter. Reproduce this exact pattern in any new script or ad hoc
command you run — `sudo -- /usr/bin/python3 scripts/...`, never
`sudo python3 scripts/...` and never `sudo -E python3 ...`.

**Why validate-iac.sh/lint.sh may require root.** Both scripts source
`scripts/host-global-docker-validation-lock.sh`, which checks
`docker info --format '{{.Swarm.LocalNodeState}}'`. If Swarm state is
`inactive` (e.g. a dev laptop), no lock is taken and the script runs fine
as any docker-group member. But if Swarm state is `active`, `pending`,
`locked`, or `error` — i.e. you are on the real production host where the
Swarm is live — the script transparently re-execs itself through
`scripts/host_global_operation_lock.py` as a `run --operation ...` "direct"
operation, whose `hold_direct_lock()` explicitly refuses to proceed unless
running as root (euid 0) when using the default lock path. So on the
production host, running `validate-iac.sh` or `lint.sh` requires `sudo`,
even though nothing in the top-level README states this outright — it only
manifests once Swarm is active.

If you get a root-required failure running these scripts on this repo's
actual server, that is expected behavior, not a bug — re-run with `sudo`
(and remember the `/usr/bin/python3` PATH gotcha above if you invoke the
lock helper directly).

## Access model

Per [`docs/OPERATIONS.md`](docs/OPERATIONS.md) ("Modelo de acceso"):

- Repo/CI validation runs as an unprivileged user.
- Remote Ansible runs as `admin` with `--ask-become-pass`.
- Local Ansible (`--local`) elevates the whole local supervisor and Ansible
  together; this is unusual — remote is the normal mode.
- Docker diagnostics on the host use `sudo -- docker ...`.
- Production helper scripts that administer Docker use
  `sudo -- ./scripts/...`.
- The end-state contract removes ALL human users, including `admin`, from
  the root-equivalent `docker` group.
- Sudo passwords are never stored anywhere in this repo (inventory, vars,
  shell history, git).

## Stale marker recovery

A shared `flock` at `/run/lock/dockerswarm-iac.lock` backs three distinct
marker files, each naming a different execution scope. The subtlety worth
over-documenting: recovery of each marker *type* goes through a
**different script**, even though the workflow looks identical from the
outside.

- Direct/manual host mutations (e.g. validate scripts running under the
  docker-validation lock) create `/run/lock/dockerswarm-direct.marker` and
  are recovered via `scripts/host_global_operation_lock.py`.
- Ansible playbook runs (via `deploy-ansible.sh`) and bootstrap runs create
  `/run/lock/dockerswarm-ansible.marker` or
  `/run/lock/dockerswarm-bootstrap.marker` respectively, and are recovered
  via `scripts/ansible-operation-lock.py` (its `recover` subcommand
  handles both, with extra flags for the bootstrap marker specifically).

Using the wrong script's recover subcommand against the wrong marker type
will not silently work around this — check which marker file exists under
`/run/lock/` before picking a tool.

Only run the steps below after confirming, via `/proc` (the recovery
tool's own live-mutator check performs this), that the original
controller/supervisor process is actually stopped and nothing else is
mutating.

Step 0 — prepare the evidence archive directory (idempotent):

```bash
sudo -- install -d -o root -g root -m 0700 /var/backups/dockerswarm
```

Step 1, dry run, **ansible or bootstrap marker** — inspects the marker,
checks for still-live holder/supervisor PIDs via `/proc`, and PRINTS an
exact confirmation string. It does NOT mutate anything without `--apply`:

```bash
sudo -- /usr/bin/python3 scripts/ansible-operation-lock.py \
  recover \
  --operation-id ID_64_HEX
```

Step 1, dry run, **direct marker** instead — same dry-run-first, same
printed-confirmation pattern, but a different script because it is a
different marker type/owner (root-owned `0:0` direct marker vs. the
`1001:1001` ansible/bootstrap markers):

```bash
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py \
  recover
```

Step 2, apply — re-run the exact same command, adding `--apply` and
`--confirm` with the value copy-pasted byte-for-byte from what the dry run
printed. For the direct-marker case the format looks like:

```text
RECOVER_DIRECT_OPERATION:<operation_id>:<sha256-of-marker-json>:CONTROLLER_STOPPED
```

```bash
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py \
  recover \
  --apply \
  --confirm 'CONFIRMACION_EXACTA_MOSTRADA'
```

If the confirm value does not match exactly, recovery fails with
`recovery confirmation differs` (or the equivalent message for the
ansible/bootstrap marker). Do not attempt to reconstruct or guess this
string — always copy it from the actual dry-run output of this specific
incident.

Step 3, **bootstrap marker only** — add three extra flags to both the dry
run and the apply command:

```text
--marker-path /run/lock/dockerswarm-bootstrap.marker
--owner-uid 0
--owner-gid 0
```

Before a marker is unlinked, its raw bytes are archived under
`/var/backups/dockerswarm/<...>-recovery/<operation-id>-<sha256>.json`
(mode `0600`, created `O_EXCL` so it can never silently overwrite prior
evidence) — this is why Step 0 must happen first.

A reboot kills the stray processes but wipes `/run` itself, destroying the
ability to prove via `/proc` that the original holder is gone. So if a
host has a stuck marker, archive evidence and run recovery **before**
rebooting it, never after. Prefer running the recovery helper from the
exact commit registered as the operation's `source_revision` when
possible.

## Writer command pattern

Always dry-run before applying:

```bash
./scripts/deploy-ansible.sh \
  --playbook platform \
  --check \
  --ask-become-pass

./scripts/deploy-ansible.sh \
  --playbook platform \
  --confirm-production \
  --ask-become-pass
```

Valid `--playbook` values for `scripts/deploy-ansible.sh` are: `platform`,
`host-baseline`, `preflight-images`, `edge`, `workloads`, `observability`,
`backup`, `site`. Valid `--profile` values are `production` (default) and
`acme-staging` (edge playbook only, uses a separate ACME storage file).
`scripts/bootstrap-host.sh` is a separate script (its own flags: `--host`,
`--authorized-keys-file`, `--password-hash-file`,
`--confirm-production`) that always drives playbook `bootstrap-host` under
profile `fresh-host`; the shared lock/profile validation in
`scripts/ansible-operation-lock.py` enforces that `bootstrap-host` and
`fresh-host` are always paired 1:1.

Writers unconditionally refuse a dirty git worktree — commit (or discard)
your changes first.

## Open STOP gates (as of last review)

This list reflects the state observed as of 2026-07-27 per README.md and
the docs below. It MUST be re-verified against current `README.md`,
`docs/TERRAFORM_STATE.md`, and `docs/MIGRATION.md` before being treated as
current — gates close over time.

- Needs two independent R2 backends with separate credentials, live
  locking proof, and encrypted snapshots.
- Needs real signing identities/signatures for plans and locking tests,
  kept outside Git.
- Cloudflare Terraform token for DNS operations: provisioned 2026-07-27,
  real Zone:DNS scope confirmed against the live Cloudflare API for the
  `apptolast.com` zone, stored outside Git under
  `/etc/dockerswarm/terraform/dns-zone-api-token.txt`. Its Cloudflare
  token ID (`185f75d78a7b79a5b1d41e595fdaf90f`, confirmed via
  `/user/tokens/verify`) is identical to the already-registered
  `cloudflare_dns_api_token_v2` Docker secret
  (`docs/EDGE.md`, "Registro de secrets instalados") — this is the same
  ACME/Traefik credential reused for Terraform, not a distinct one, per
  the owner's explicit authorization to reuse credentials across
  purposes rather than provision new ones. The originally-requested
  rotated ACME credential is still open and unrelated to this reuse.
- Needs real Netcup SCP/import credentials if that root is activated.
- Needs an external custodian for the Swarm unlock key before autolock is
  enabled.
- Needs explicit OAuth/business acceptance of n8n.
- Needs an explicit decision on Minecraft's `online-mode=false` before
  publishing it.
- Needs proof the staged migration snapshot is final, or a versioned
  refresh/promotion via
  `migration/scripts/promote_runtime_generation.py`, before starting
  workloads.
- DNS cutover to the new platform IP: the signed host-readiness
  coordinator this gate needed now exists
  (`scripts/host-readiness-probe.sh` plus the verification chain in
  `scripts/terraform-safety.py`, added 2026-07-27) and
  `plan-terraform.sh`/`apply-terraform.sh` accept its proof via
  `--host-readiness`. Absence of a proof, or one for the wrong hostname,
  still fails exactly as before. The coordinator has never been run
  against the real platform and no cutover has happened — that remains a
  separate, deliberate decision requiring genuine platform readiness
  (Traefik/ACME actually deployed and serving) plus the repository
  owner's explicit go-ahead, not just the presence of the code.

None of these gates may be satisfied by inventing values, hardcoding a
credential, or adding a bypass flag. If a task seems to require passing
one of these gates, stop and surface that to the user rather than working
around it.

## Commit and changelog conventions

Commits follow Conventional Commits (`feat:`, `fix:`, `docs:`, etc.),
imperative mood, lower-case after the prefix, terse single-line subjects
with no trailing period. Real examples from `git log`:

```text
feat: bootstrap production single-node Docker Swarm
feat: codify reproducible Docker Swarm platform
feat: add backup, observability, workloads and host-security automation
```

Detail belongs in [`CHANGELOG.md`](CHANGELOG.md), not the commit body. It
follows Keep a Changelog (es-ES/1.1.0) with Added/Changed/Security/Fixed
sections under an `[Unreleased]` heading, and Semantic Versioning for
version numbers.

Only create commits when the user explicitly asks.

## House style for any new code

- 80-column wrapping for prose in `.md` files, enforced by markdownlint
  MD013 via `scripts/lint.sh`. Wide tables must be bracketed exactly around
  the table block, matching the pattern already used in
  [`docs/EDGE.md`](docs/EDGE.md),
  [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md),
  [`docs/REBUILD.md`](docs/REBUILD.md), and
  [`docs/SERVICE_CATALOG.md`](docs/SERVICE_CATALOG.md):

  ```text
  <!-- markdownlint-disable MD013 -->

  | Wide | Table | Here |
  | --- | --- | --- |
  | ... | ... | ... |

  <!-- markdownlint-enable MD013 -->
  ```

  Never disable MD013 file-wide.
- The TOCTOU-safe file-read idiom used throughout `scripts/`: open with
  `os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW` (never following a
  symlink), then call `os.fstat()` on the already-open descriptor (never a
  second path-based `stat()`, which could race a symlink swap) to check
  it is a regular file, has the exact expected mode bits, exact owner
  uid/gid, `st_nlink == 1`, and a bounded size — only then read/decode the
  contents (often strict ASCII JSON that rejects duplicate keys). This
  appears in `scripts/ansible-operation-lock.py`,
  `scripts/host_global_operation_lock.py`, `scripts/terraform-safety.py`,
  `scripts/install-observability-secrets.py`, and
  `scripts/r2-operation-lease.py`. Any new script touching
  sensitive/root-owned files should reuse it rather than plain
  `open()`/`stat()`.
- Writer scripts never accept a dirty git worktree.
- Secrets rotation is always make-new/repoint/then-revoke-old, never
  destroy-then-recreate.

## When in doubt

Prefer reading [`docs/OPERATIONS.md`](docs/OPERATIONS.md),
[`docs/TERRAFORM_STATE.md`](docs/TERRAFORM_STATE.md), and
[`docs/MIGRATION.md`](docs/MIGRATION.md) for anything not covered here,
and prefer the `iac-validator` / `terraform-operator` / `ansible-operator`
subagents (`.claude/agents/`) for the workflows they own rather than
re-deriving the command sequences inline.
