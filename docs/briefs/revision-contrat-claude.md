# Work contract — targeted revision of CLAUDE.md + methodology.md

Own commit, before the closing 33-run campaign. Two of the sections added
below protect that very campaign.

**Method: targeted additions, not a rewrite.** The current CLAUDE.md is
sound (numbered rules + docstring/markdown/language contracts). Keep it,
keep the numbering, add what's missing. Selection criterion for anything
that lives in CLAUDE.md: **a rule belongs here only if forgetting it causes
silent damage** — no error, no red test, just a false measurement or a
diagnosis to redo later. Everything caught by tests, review, or an
immediate error goes into a linked document with a pointer.

---

## 1. Fix rule 1 (stale — highest priority, one line)

`docs/project-status.md` now exists and is not in the reading order. An
agent following the contract literally starts blind to the delivered
batch. Replace rule 1 with:

> 1. At session start, read in this order: `CLAUDE.md` →
>    `docs/project-status.md` (where we are) → `README.md` (short) → the
>    brief of the ongoing effort (`docs/briefs/`). Never load in full:
>    `docs/history.md`, `docs/resolved-bugs.md`, `docs/briefs/archive/`,
>    `docs/campaigns/` reports — consult them via targeted search. Startup
>    budget: ~5000 tokens; report it rather than loading more.

## 2. Add a section "Measured behavior — do not touch lightly"

Rationale: touching any of these breaks nothing immediately and silently
falsifies every subsequent campaign. Currently only prompts are covered
(rule 11); flags, budgets, thresholds and fixtures are not.

> Any change to the following alters results and requires its own
> single-variable validation campaign:
> - system prompts and directives sent to the model (`*_DIRECTIVE`,
>   `*_SYSTEM_PROMPT`);
> - cognitive-core flags (`PLANNER_ENABLED`, `VERIFICATION_ENABLED`,
>   `PLAN_VALIDATION_ENABLED`, `PLAN_JUDGE_ENABLED`), budgets
>   (`MAX_TOOL_ITERATIONS`, attempts, replans), truncation thresholds,
>   approval tiers;
> - benchmark task prompts, assertions and fixtures — **frozen**: any
>   change creates a new benchmark version, and cross-version comparisons
>   are forbidden.

Draw the exact variable names from `graph.py` — do not guess them.

## 3. Add a section "Operational traps"

Each of these has already produced a void campaign. They are in prose in
the README/history; they belong here.

> - Environment variables are read at import: a change requires
>   `docker compose up -d --force-recreate <service>`; a restart is not
>   enough. Setting them in the harness shell has NO effect (the harness
>   talks to the agent over HTTP) — silent trap.
> - `entrypoint.sh` is copied at build time: any change requires
>   `docker compose build <service>` before `up -d`.
> - Effective configuration is read from `/proc/1/cmdline` or
>   `docker exec … env`, never from the file.
> - No campaign starts without a green preflight (tool schema, image
>   freshness, effective flags, resets and purges). A campaign started on
>   an unverified stack is void.

## 4. Add measurement rules to the numbered list

> - One variable per experiment; each mechanism has its judge designated
>   BEFORE the measurement. If a technical coupling forces two changes to
>   ship together, declare it at the checkpoint beforehand, with one judge
>   per mechanism.
> - Decision thresholds are frozen: they apply as written and are not
>   reinterpreted in light of the results.
> - Report without advocacy: missed criteria are announced as such. Do not
>   start a fix on an unvalidated result.
> - Archives first, zero runs: exhaust the audit log and existing reports
>   before relaunching anything.
> - Beware flattering zeros: an error counter at zero on a rarely
>   triggered mechanism measures nothing — every reliability judge comes
>   with a coverage judge.
> - The brief before the code: every effort's instructions are written in
>   `docs/briefs/` and committed before the first line. A closed effort
>   gets a status header (result, deviations from the brief) and moves to
>   `docs/briefs/archive/`.

## 5. Add `docs/methodology.md` and point to it

Create the file from the French document supplied separately, translated
to English (it is documentation, phase 4 scope). Add to CLAUDE.md, at the
end of the measurement rules:

> Full rationale: `docs/methodology.md` — read once, not every session.

## 6. Verification

- CLAUDE.md stays under ~130 lines. If an addition pushes past it, move
  the least "silent" content to `docs/methodology.md` or
  `docs/operations/runbook.md` and leave a pointer.
- Every file path mentioned in CLAUDE.md exists (check them one by one —
  rule 1 was stale precisely because nobody did).
- Own commit, message `docs: revise the work contract (CLAUDE.md) +
  methodology`.

## Out of scope for this commit

- Renaming `docs/history.md` (a lab notebook, not a version changelog —
  `docs/engineering-log.md` was proposed, plus a dated index at the top of
  the file). To be handled with the trimming catch-up pass, not here.
- The trimming catch-up pass itself: phase 1 only covered `graph.py`,
  while phase 4 has since translated the other services untrimmed. Trim
  them after phase 4 — cheaper on already-written English, but do it
  before the volume sets.
