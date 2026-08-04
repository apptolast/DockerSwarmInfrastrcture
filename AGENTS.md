# AGENTS.md — Navigation map for AI agents

> Entry point for any AI agent working in this repository. This is not a
> rulebook, it is a map: read only what the task needs, when it needs it
> (progressive disclosure) — the same principle `TemplateSSDUncleBob`'s own
> `AGENTS.md` uses.
>
> This repository already had a mature, convergent verification discipline
> before this file existed: nine STOP gates, fail-closed scripts, and seven
> operator/reviewer subagents. This file is new; almost everything it points
> to is not. See
> [`docs/adopcion-templatessd.md`](docs/adopcion-templatessd.md) for the
> full correspondence with the `TemplateSSDUncleBob` harness this adoption
> is based on, including the parts that have no honest equivalent here.

## 1. Before touching anything (mandatory)

1. Read [`CLAUDE.md`](CLAUDE.md) in full: the golden rule, the fail-closed
   philosophy, the access model, the session startup protocol (including
   the optional 2bis memory-sync step), and the nine STOP gates.
2. Within `CLAUDE.md`, read the "Open STOP gates" section specifically —
   this repository's closest equivalent to a live session-state file: what
   is open, closed, or premise-obsolete right now, and who can close each
   one. That section names which documents are safe to cross-check it
   against and which are known-stale for this purpose (as of the last
   revalidation, `docs/DEPLOYMENT_STATUS.md` was the authoritative one for
   deployed state, itself flagged there as already partly stale) — read
   that guidance before trusting any single document, including this one.
3. Run, in this exact order, before changing anything:

   ```bash
   ./scripts/bootstrap-tooling.sh
   ./scripts/validate-iac.sh
   ./scripts/lint.sh
   ```

   `CLAUDE.md`'s "Validation workflow" section documents the expected
   failure modes (missing tooling, a root-required lock, a stale marker).
   Do not work around a failure here; resolve it, or report the block.

## 2. Repository map

<!-- markdownlint-disable MD013 -->

| Path | Contains | Read it when |
| --- | --- | --- |
| `CLAUDE.md` | Golden rule, fail-closed philosophy, access model, stale-marker recovery, writer command pattern, the nine STOP gates, commit/changelog conventions, house style | Always, first |
| `AGENTS.md` | This file | Orienting at the start of a session |
| `CHECKPOINTS.md` | Objective end-state checklist, adapted from `TemplateSSDUncleBob`; states explicitly where no honest equivalent exists instead of forcing one | Before calling anything done |
| `README.md` | Observed state, IaC coverage, production order, external open gates, limits | First read of the repository |
| `CHANGELOG.md` | Keep a Changelog log; every change needs an entry or a justified `N/A` | Before closing a change |
| `harness.config.json` | This stack's commands in `TemplateSSDUncleBob`'s declarative shape, kept for cross-repo consistency only — not wired to `bin/harness` | Only when comparing this repo against the template convention |
| `docs/adopcion-templatessd.md` | The full correspondence between this repo's gates/agents and the template's gates/roles, including what does not map cleanly | Understanding this adoption |
| `docs/ARCHITECTURE.md` | Ownership boundaries, shared contract, topology, network isolation, workloads, observability, state and secrets | Before changing layering |
| `docs/OPERATIONS.md` | Topology, access model, the Ansible lock/marker mechanics, change sequence, reboots, logging, backup/autolock, maintenance | Before any writer operation |
| `docs/TERRAFORM_STATE.md` | State/backend contract per root, signed plans, rotation, the production-authorization gate checklist | Before any Terraform change |
| `docs/MIGRATION.md` | Migration/cutover evidence and runbooks | Migration or cutover work |
| `docs/EDGE.md` | Cloudflare, ACME, Traefik, credential separation and rotation | Edge/DNS/TLS work |
| `docs/BACKUP_RECOVERY.md` | Backup/restore contract, the autolock STOP | Backup or disaster-recovery work |
| `docs/OBSERVABILITY.md` | Metrics/logs/alerts stack | Observability work |
| `docs/REBUILD.md` | Full rebuild runbook | Disaster recovery |
| `docs/SERVICE_CATALOG.md` | The approved-services allowlist | Adding or changing a workload |
| `docs/CAPACITY.md` | Capacity accounting and validation | Sizing or capacity work |
| `docs/REPOSITORIES.md` | Cross-repo ownership and maintenance | Cross-repo questions |
| `docs/DEPLOYMENT_STATUS.md` | Real applied state vs. planned state | Checking what is actually live |
| `docs/KNOWN_ISSUES.md` | Known gaps and workarounds | Debugging something that looks wrong |
| `scripts/sync-memoria.sh(.ps1)` | Syncs Cénit Digital's org-wide memory into `.memoria-cache/` | Step 2bis of `CLAUDE.md`'s startup protocol |
| `.github/pull_request_template.md` | The nine evidence checkboxes every PR must satisfy or justify `N/A` | Opening a PR |
| `.github/CODEOWNERS` | Forces owner review on `ansible/`, `config/`, `infra/terraform/`, `stacks/` | Understanding required reviewers |
| `.github/workflows/validate.yml` | CI: bootstrap + validate-iac + lint on push/PR/schedule | Understanding what CI actually runs |
| `.github/workflows/guard-sensitive-paths.yml` | Labels PRs touching the CI gate, the ops engine, or repo governance | Understanding the `permissions-change` label |

<!-- markdownlint-enable MD013 -->

Everything under `ansible/`, `infra/terraform/`, `stacks/`, `scripts/`,
`config/`, `migration/`, `backup/`, `tests/`, and `migration/tests/` is
production code and its tests, not template scaffolding — read the
relevant `docs/*.md` above before changing any of it, not this file.

## 3. Agents (`.claude/agents/`)

None of these seven agents are new; this table only orders what already
exists.

<!-- markdownlint-disable MD013 -->

| Agent | Role | Edits `src`-equivalent code? |
| --- | --- | --- |
| `terraform-operator` | Plans/validates/applies Terraform across the four roots; enforces the STOP gates | No `.tf` edits — proposes diffs for a human to apply |
| `ansible-operator` | Runs playbooks via `deploy-ansible.sh` with check-before-apply discipline | Edits `ansible/` only |
| `iac-validator` | Runs bootstrap/validate/lint; diagnoses failures; walks marker recovery | No |
| `judge` | Approves or rejects a change against `CLAUDE.md` and the PR template | No |
| `security-reviewer` | Mandatory (not optional) audit against eight fixed questions | No |
| `guardrail-adversary` | Per-gate negative-test coverage check; this domain's substitute for mutation testing | No |
| `mentor` | Explains the why of an invariant, on demand | No |

<!-- markdownlint-enable MD013 -->

See [`docs/adopcion-templatessd.md`](docs/adopcion-templatessd.md) for how
these map onto `TemplateSSDUncleBob`'s six pipeline roles and three support
roles — including the two roles (`spec_partner`, `gherkin_author`) that
have no honest equivalent here, and the one (`a11y_seo_auditor`) that does
not apply at all.

## 4. Hard rules (non-negotiable)

Pointers only; the full text lives in `CLAUDE.md` and in each agent's own
file — do not treat this list as a substitute for reading them.

- Fail-closed, always: ambiguous state stops a script; it never
  auto-cleans (`CLAUDE.md`, "Fail-closed philosophy").
- No invented `--force`/`--skip-*` flag, no guessed credential, no
  reconstructed confirmation string, for any script, ever.
- Writers refuse a dirty git worktree; never work around that.
- The TOCTOU-safe read idiom (`O_NOFOLLOW` plus `fstat` on the already-open
  descriptor) for anything sensitive or root-owned (`CLAUDE.md`, "House
  style for any new code").
- `sudo -- /usr/bin/python3 scripts/...`, never a bare `sudo python3`.
- Secrets rotation is always make-new, then repoint, then revoke-old.
- None of the nine STOP gates in `CLAUDE.md` may be closed by inventing a
  value; most can only be closed by the repository owner.
- Only create commits when the user explicitly asks (`CLAUDE.md`).
- This adoption introduces no autonomous workflow of any kind:
  `terraform apply` and `ansible-playbook` against the real host stay
  100% manual, always. See the safety limit in
  `docs/adopcion-templatessd.md`.

## 5. This repository's real pipeline

There is no Gherkin contract and no `feature_list.json` here. The actual
flow for a change that touches infrastructure:

```text
propose change (branch, PR)
  -> local validate/lint (scripts/validate-iac.sh, scripts/lint.sh)
  -> operator dry run: a reviewed `--check --diff` (Ansible) or a signed,
     inspected plan (Terraform, via plan-terraform.sh)
  -> judge review, plus security-reviewer always, plus
     guardrail-adversary/mentor on demand -> APPROVED | CHANGES_REQUESTED
  -> a human merges, and only a human runs the real apply
```

See [`docs/adopcion-templatessd.md`](docs/adopcion-templatessd.md) for
exactly where this lines up with `TemplateSSDUncleBob`'s pipeline, and
where it doesn't.

## 6. Closing a session

1. `git status --porcelain` is empty — the `Stop` hook in
   `.claude/settings.json` already checks this before you finish.
2. `CHANGELOG.md` has an entry under `[Unreleased]`, or the change is
   documentation-only and genuinely needs none.
3. `CHECKPOINTS.md` — nothing you would mark `[ ]` is being reported as
   `[x]`.
4. No stray debug output, temporary files, or unlabeled TODOs remain.

## 7. If you get stuck

Re-read the relevant section of `docs/` or the owning agent's own file
first. If a tool doesn't do what you expect, do not invent a workaround:
report the block plainly, exactly as `terraform-operator.md` and
`iac-validator.md` already instruct their own operators to do.
