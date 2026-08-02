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

## Open STOP gates (revalidated 2026-08-02)

Fully revalidated on 2026-08-02 against the worktree at `8a76620`
("feat(agents): add the four reviewers and the sensitive-path guard
(#10)"), which is the current `origin/main`. The previous list described
2026-07-27 and was stale by construction: `CLAUDE.md` has not been
touched since `d461e13` (2026-07-27), and the tree has advanced 48
commits since then
(`git log --oneline d461e13..origin/main | wc -l`).

Do NOT revalidate these gates against `README.md`,
`docs/MIGRATION.md`, `docs/BACKUP_RECOVERY.md`, or
`migration/RUNTIME_GENERATION_PROMOTION.md`: none of them has been
touched since `63dd546` (2026-07-27) and today they contradict the real
state — `README.md:32` asserts
`platform_minecraft_public_enabled: false` while
`config/platform.yml:53` says `true`. The authoritative document for
deployed state is `docs/DEPLOYMENT_STATUS.md` (`2cd966e`, 2026-07-28),
which is itself an ancestor of `08cace6` and is already stale on
Minecraft and on the DNS cutover.

Labels: **CLOSED** (with the commit that closed it), **PREMISE
OBSOLETE** (the fact that motivated the gate no longer holds), and
**OPEN** (with what is missing and who can close it). A partially closed
gate is marked OPEN, never CLOSED.

### 1. R2 backends and encrypted snapshots — OPEN

Partly closed by `ca38a3d` (2026-07-27), which is an **ancestor** of
`d461e13` and which the previous version of this list omitted:
`infra/terraform/backend-identities.json` registers real buckets for
`cloudflare/apptolast-dns` (`apptolast-tfstate-dns`) and
`netcup/perimeter` (`apptolast-tfstate-netcup`);
`cloudflare/state-bootstrap` is still `null`.

"Separate credentials" is no longer the requirement: both roots declare
the same `access_key_id_sha256` (`4364051a…`) at
`infra/terraform/backend-identities.json:9` and `:22`. The owner
authorized a single account-scoped R2 credential covering all three
buckets (`docs/EDGE.md:50-56`, commit `ca38a3d`), and `a245f71` rewrote
the harness so that the five denial results — the four
`cross_credential_*_denied` plus `terraform_backend_access_denied` —
are recorded as `null` instead of a fabricated `true`
(`docs/TERRAFORM_STATE.md:226-235`,
`scripts/terraform-safety.py`).

Missing, verified: `infra/terraform/snapshot-recipients.json` does
**not** exist; only `snapshot-recipients.json.example` is present, and
`git ls-files infra/terraform/` does not list the real file. Without it,
`scripts/apply-terraform.sh:188`,
`scripts/migrate-terraform-state.sh:218`, and
`scripts/snapshot-terraform-state.sh:183` all fail closed, so **no
`terraform apply` can run through the wrapper today**.

Missing, not verifiable from the repo: the live locking proof lives
outside Git (only `infra/terraform/locking-proof.json.example` is here).

Only the owner can close this: register the SHA-256 of the approved
`age` recipients file (`recipient_file_sha256`) and run
`scripts/test-terraform-r2-locking.sh` against real R2.

### 2. Signing identities — OPEN

Partly closed by `ca38a3d` and `d461e13`, both at or before the last
commit that touched this document, and also omitted before. Four
tracked trust registries exist, with real `ssh-ed25519` keys and a
restricted namespace: `infra/terraform/lock-proof.allowed-signers`,
`plan.allowed-signers`, `lease-recovery.allowed-signers` (the first
three from `ca38a3d`) and `host-readiness.allowed-signers` (from
`d461e13`). They differ from their `.example` counterparts, which
authorize nobody
(`infra/terraform/plan.allowed-signers.example`).

Missing, not verifiable from the repo: the private keys live outside Git
under `/etc/dockerswarm/`; the runtime-promotion allowed-signers file
lives outside Git by its own contract
(`/etc/dockerswarm/migration/runtime-promotion.allowed-signers`,
`migration/RUNTIME_GENERATION_PROMOTION.md:47`) and there is no tracked
example at all (`ls migration/*allowed*` finds nothing). No real plan or
lock-proof signature is observable from the repository.

### 3. Cloudflare Terraform token and rotated ACME — OPEN

The Terraform-token half is still exact and is reconfirmed:
`docs/EDGE.md:137-145` documents that
`/etc/dockerswarm/terraform/dns-zone-api-token.txt` (root:root, 0600)
reuses the same credential `185f75d78a7b79a5b1d41e595fdaf90f` as the
`cloudflare_dns_api_token_v2` Docker secret (`docs/EDGE.md:132`), by the
owner's explicit decision to reuse rather than provision a new one. It
is not a distinct token.

Missing: revoking `cloudflare_dns_api_token_v1`. `docs/EDGE.md:23`
declares it a rotation "pending revocation until the service is verified
with v2", and `docs/EDGE.md:133` sets the condition as "revoke after
verifying `v2` in service". That condition is **already met**:
`docs/DEPLOYMENT_STATUS.md:14` records 9 of 9 certificates issued by
Let's Encrypt production. Only the revocation itself is left, and only
the owner can perform it in Cloudflare.

### 4. Netcup SCP credentials — OPEN

Not verifiable from the repo, and nothing indicates the root has been
activated. The credential is injected through the environment as
`NETCUP_SCP_REFRESH_TOKEN`
(`infra/terraform/netcup/perimeter/README.md:29`,
`docs/TERRAFORM_STATE.md:314`), and the only tracked variables file is
`infra/terraform/netcup/perimeter/terraform.tfvars.example`
(`git ls-files | grep tfvars` returns no real `.tfvars`). On top of
that, while `snapshot-recipients.json` is missing (gate 1), no `apply`
against this root is even possible.

### 5. External custodian for the unlock key — OPEN

Confirmed by the most recent document in the repository:
`docs/DEPLOYMENT_STATUS.md:69-75` says the backup is still blocked, that
it requires an external custodian for the Swarm unlock key and an R2
bucket with its own credential, and that "this host has no copies
outside itself". The STOP with its mandatory six-step sequence remains
intact at `docs/BACKUP_RECOVERY.md:89` and is repeated at
`docs/OPERATIONS.md:163-167`. `docker swarm update --autolock=true` MUST
never be run by hand, under any circumstances. Only the owner can close
this by supplying the real external destination.

### 6. n8n OAuth/business acceptance — OPEN

The mechanism exists and is still fail-closed:
`migration/scripts/manage_n8n_workflows.py:50` defines
`PUBLISH_CONFIRMATION = "I_HAVE_CONFIRMED_GOOGLE_OAUTH_CONSENT"` and the
CLI demands it literally at `:1478`. There is no tracked
`n8n-active-workflows.json` inventory (`git ls-files` does not list it)
and no record that the confirmation was ever given.

Beware a false closure signal: n8n serving real traffic
(`docs/DEPLOYMENT_STATUS.md:18`) does **not** close this gate. The
acceptance is about publishing the 46 restored workflows
(`docs/MIGRATION.md:20-21`), not about bringing the service up. Only the
owner can give it.

### 7. Minecraft `online-mode=false` — CLOSED

Closed by `08cace6` (2026-07-28). `config/platform.yml:61` declares
`platform_minecraft_offline_public_accepted: true`, with the reasoned
acceptance of the exact risk at `config/platform.yml:54-60`, and
`config/platform.yml:53` sets `platform_minecraft_public_enabled: true`.

The gate itself was **not** removed: its default is still `false`
(`ansible/roles/platform/defaults/main.yml:24`) and it is still checked
at `ansible/roles/platform/tasks/main.yml:19-26`,
`scripts/validate-contract.py:185-197`, and
`infra/terraform/cloudflare/apptolast-dns/contract.tf:49-56`.
`config/minecraft.yml:9` still holds `online_mode: false`, which is
precisely what was accepted.

Note: `docs/DEPLOYMENT_STATUS.md:77-78` still says Minecraft is waiting
for the flag; that document is from `2cd966e`, an ancestor of
`08cace6`.

### 8. Final staged snapshot or versioned promotion — OPEN

Open, and its temporal premise ("before starting workloads") has already
been overrun without closing it. `docs/DEPLOYMENT_STATUS.md:15` records
11 of 16 Swarm services at `1/1`: workloads were started on 2026-07-28
without the repository documenting either of the two conditions required
by `docs/MIGRATION.md:264-271`.

The previous tree was set aside as
`/srv/dockerswarm/services.pre-runtime-v4-20260728T002105Z`
(`docs/DEPLOYMENT_STATUS.md:27-29`), a path that does **not** match the
transactional layout of
`migration/scripts/promote_runtime_generation.py`
(`/srv/dockerswarm/runtime-generations/…`,
`migration/RUNTIME_GENERATION_PROMOTION.md:41-47`). The runbook is still
at `STOP` (`migration/RUNTIME_GENERATION_PROMOTION.md:15-33`) and has
not been touched since `63dd546`; there is no signed attestation in the
repository and no evidence that this CLI has ever been run against
`/srv`.

Services already running does **not** close this gate: that is exactly
the reasoning the closing paragraph forbids. Only the owner can close
it, by one of the two routes in `docs/MIGRATION.md:264-271`.

### 9. DNS cutover to the platform IP — PREMISE OBSOLETE

The cutover **has already happened**, by hand in Cloudflare, not through
Terraform. `08cace6` (2026-07-28) flipped the nine remaining
`platform_dns_cutover` flags to `true` (`edge` was already `true`), so
all ten at `config/platform.yml:17-27` now declare the cutover, and
documents at `config/platform.yml:9-16` that the ten labels resolve to
`platform_public_ipv4` (`159.195.156.57`, verified with
`dig +short <label>.apptolast.com A` on 2026-07-28) and that the legacy
server `138.199.157.58` was deleted by the owner. `CHANGELOG.md:72-79`
records the same.

What does **not** change: the signed coordinator is still mandatory.
`scripts/host-readiness-probe.sh` and the verification chain in
`scripts/terraform-safety.py` have existed since `d461e13`, and
`scripts/terraform-safety.py:2598-2607` still unconditionally rejects
any create or change toward the platform IP without a valid proof naming
that exact hostname. The probe has never been run against the real
platform and no proof has ever been issued.

What does change, and is more dangerous than before: the risk has
inverted. `docs/DEPLOYMENT_STATUS.md:80-87` warns that Terraform MUST
**not** be run against `cloudflare/apptolast-dns`, because `initialize`
mode forces `adoption_only=true` and
`infra/terraform/cloudflare/apptolast-dns/dns.tf:55-67` then computes
the legacy IP for the nine non-`edge` records, pointing them back at a
server that no longer exists. On top of that, `imports.tf` does not
cover the `edge` record (grepping for `edge` in
`infra/terraform/cloudflare/apptolast-dns/imports.tf` yields no
matches). Adopting the real state into Terraform is prerequisite work,
not just one more `apply`.

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
