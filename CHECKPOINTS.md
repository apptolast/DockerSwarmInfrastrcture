# CHECKPOINTS.md — Objective end-state checklist

> Adapted from `TemplateSSDUncleBob`'s `CHECKPOINTS.md` for real production
> infrastructure. Where this domain has an honest equivalent to a template
> checkpoint, it is used. Where it does not, this file says so explicitly
> instead of forcing one — see C6 and C7 below, and
> [`docs/adopcion-templatessd.md`](docs/adopcion-templatessd.md) for the
> full reasoning behind every mapping in this file.

## C1 — The verification chain is complete and green

- [ ] `CLAUDE.md`, `AGENTS.md`, `CHECKPOINTS.md`, `README.md`, and
      `CHANGELOG.md` exist and are current.
- [ ] `./scripts/bootstrap-tooling.sh` succeeds (the pinned Terraform
      binary under `.tools/terraform`, the `.venv` present).
- [ ] `./scripts/validate-iac.sh` exits 0: yamllint/ansible-lint, playbook
      syntax checks, the contract/host-security/ssh-policy/services
      validators, both `unittest` suites (`tests/`, `migration/tests/`),
      the four Terraform roots' `fmt`/`init`/`validate`/`test`, and the
      Traefik config validation.
- [ ] `./scripts/lint.sh` exits 0: shellcheck at `--severity=style`,
      `markdownlint-cli2` on every tracked and untracked `.md` file,
      `gitleaks` over the working tree and the full history, and
      `git diff --check`.

## C2 — Repository and host state are coherent

- [ ] `git status --porcelain` is empty (the `Stop` hook in
      `.claude/settings.json` already enforces this before a session
      closes).
- [ ] No writer operation is left in flight: at most one Ansible apply, one
      Terraform apply, or one direct host mutation running against the
      real host at any time — enforced mechanically by
      `/run/lock/dockerswarm-iac.lock` and its markers, not just by
      convention.
- [ ] No stale, unrecovered marker (`dockerswarm-direct.marker`,
      `dockerswarm-ansible.marker`, or `dockerswarm-bootstrap.marker`) is
      left under `/run/lock/` from this session's work.

## C3 — The change respects architecture and the access model

- [ ] Changes under `ansible/`, `infra/terraform/`, `stacks/`, `scripts/`,
      or `config/` respect `docs/ARCHITECTURE.md`'s layering and
      `docs/OPERATIONS.md`'s access model.
- [ ] `security-reviewer`'s eight questions were run for any diff under
      `ansible/`, `config/`, `scripts/`, `stacks/`, `infra/terraform/`,
      `backup/`, `migration/`, or `.claude/` — this agent is mandatory
      here, unlike the optional gate it is in the template.
- [ ] No invented `--force`/`--skip-*` flag, no hardcoded or guessed
      credential, and no STOP gate closed by anything other than the
      exact evidence `CLAUDE.md` names for it.

## C4 — Verification is real, not asserted

- [ ] Every claim that something works is backed by a command actually
      run and its real output, never "should pass". This is what
      `judge.md` already requires, and what `iac-validator.md` exists to
      do.
- [ ] A new invariant (a new validator, a new fail-closed check) ships
      with both the validator and a negative test proving it actually
      rejects the bad input it claims to reject, not only a happy-path
      test. This is `guardrail-adversary`'s job, and is this domain's real
      equivalent of the template's `@s -> test` traceability (its own C6).

## C5 — The session closed cleanly

- [ ] No stray debug output, temporary files, or unlabeled TODOs remain.
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]` in the right
      section (`Added`/`Changed`/`Security`/`Fixed`), or the PR template's
      matching checkbox is justified `N/A`.
- [ ] If a `judge` review ran, its verdict file
      (`.build/review/judge-<slug>.md`, already gitignored) reflects the
      final state of the change.

## C6 — The human gate sits over the plan or the dry run, not over Gherkin

The template's single human gate approves a Gherkin `.feature` contract
before any production code exists. This adoption does not introduce a
Gherkin layer here — that would be invented, not adapted, and this
document says so plainly instead. This domain's actual point of maximum
leverage sits later, and it already existed before this adoption, already
enforced more mechanically than the template's own gate:

- [ ] Every Terraform apply was preceded by an inspected, signed plan
      (`scripts/plan-terraform.sh`, itself verified by
      `scripts/apply-terraform.sh` before it does anything).
- [ ] Every Ansible apply was preceded by a reviewed `--check --diff` dry
      run in the same session (`ansible-operator.md`'s mandatory
      check-then-apply sequence).

Same position in the lifecycle as the template's gate — approve the
contract before it can mutate anything — but a different artifact (a
signed plan or a check diff, not a `.feature` file), because that is what
this domain actually has to approve.

## C7 — Mutation testing: no honest equivalent, said outright

This checkpoint is deliberately NOT filled in with a numeric mutation
score. Verified by grep: zero occurrences of `pytest`, `mutmut`, or
`cosmic-ray` anywhere in this repository. The suite is `unittest`, Python
3.14 is a hard requirement, and a meaningful share of the tests are purely
static: they parse rendered YAML or templates and assert on structure, so
mutating production Python would move none of those assertions and would
produce a score that punishes exactly the tests that matter most.
`.claude/agents/guardrail-adversary.md` makes this same case in more
detail, independently of this adoption — it already existed before this
file did.

The adopted substitute is not a score:

- [ ] Every gate touched by the change has at least one negative test
      exercising the exact rejection branch the change adds or modifies,
      not just "a test file for this script exists" (this is
      `guardrail-adversary`'s "lower-bound rule").
- [ ] Any reported survivor is either killed with a new negative test, or
      narrowly justified in the same reply — the same escape hatch the
      template allows for a documented equivalent mutant, applied here to
      a rejection branch instead of a code mutant.

---

**How to use this file:** `judge.md` walks C1-C6 against a concrete diff.
`guardrail-adversary.md` walks C7 for whatever gates that diff touches.
Neither marks a checkbox `[x]` without citing the command or `file:line`
that backs it — an empty or unjustified box means the change is not done,
the same rule the template's own `CHECKPOINTS.md` states for its C1-C7.
