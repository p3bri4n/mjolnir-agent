# Project-specific instructions

1. At session start, read in this order: `CLAUDE.md` →
   `docs/project-status.md` (where we are) → `README.md` (short) → the
   brief of the ongoing effort (`docs/briefs/`). Never load in full:
   `docs/history.md`, `docs/resolved-bugs.md`, `docs/briefs/archive/`,
   `docs/campaigns/` reports — consult them via targeted search. Startup
   budget: ~5000 tokens; report it rather than loading more.
2. resolved bugs must be recorded in docs/resolved-bugs.md
3. progress history must be recorded in docs/history.md
4. always inform the user of the commands they need to type if a docker
   service needs restarting/rebuilding
5. one phase = one PR. One commit = one nature of change (never a move
   and a rewrite in the same commit — the diff becomes unreadable and the
   file history, this repo's central argument, is lost).
6. STOP 🧑 at checkpoints.
7. No opportunistic refactor outside scope — propose it at the checkpoint.
8. Any claim about a library's behavior is verified against the installed code.
9. README updated as you go, existing style.
10. Suggest obvious simplifications when it's opportune
11. Code/doc language ("restructuring + English" effort, see
    `docs/briefs/`): new content in English — docstrings, comments,
    non-exposed internal identifiers, README/CLAUDE.md/PLAN.md/docs.
    Stays in French: system prompts/directives sent to the model and
    benchmark task prompts (behavior, not documentation — translated in
    an isolated phase 6, never mixed with a refactor); already-written
    entries of `docs/history.md`/`docs/resolved-bugs.md` (dated
    archives); approval messages/user-facing notices (separate decision
    to come).
12. Measurement rules:
    - one variable per experiment; each mechanism has its judge designated
      BEFORE the measurement. If a technical coupling forces two changes
      to ship together, declare it at the checkpoint beforehand, with one
      judge per mechanism.
    - decision thresholds are frozen: they apply as written and are not
      reinterpreted in light of the results.
    - report without advocacy: missed criteria are announced as such. Do
      not start a fix on an unvalidated result.
    - archives first, zero runs: exhaust the audit log and existing
      reports before relaunching anything.
    - beware flattering zeros: an error counter at zero on a rarely
      triggered mechanism measures nothing — every reliability judge
      comes with a coverage judge.
    - the brief before the code: every effort's instructions are written
      in `docs/briefs/` and committed before the first line. A closed
      effort gets a status header (result, deviations from the brief) and
      moves to `docs/briefs/archive/`.

    Full rationale: `docs/methodology.md` — read once, not every session.

## Docstrings/comments contract

- one line by default; expanded docstring only if the behavior is
  non-obvious (side effect, invariant, error contract, WHY of this
  choice);
- never a paraphrase of the signature, never an Args/Returns/Raises
  section when names and types already suffice;
- the comment explains the WHY; code that requires explaining the WHAT
  should be rewritten instead;
- history does not live in the code ("fixed in iteration 3") → a
  one-line pointer to `docs/history.md`/`docs/resolved-bugs.md`, never
  the detail copied in;
- **accepted exception**: a block documenting a verified external
  constraint (library behavior, backend gotcha, reason for a flag)
  stays — it's hard-won knowledge. Cut the paraphrase, keep the
  justification.

## Markdown contract

No summary of what precedes, no "Conclusion"/"Key points" section in
technical docs, no table when three lines suffice. One document = one
function.

## Measured behavior — do not touch lightly

Any change to the following alters results and requires its own
single-variable validation campaign:
- system prompts and directives sent to the model (`GROUNDING_DIRECTIVE`,
  `DOWNLOAD_DIRECTIVE`, `BULK_CHECK_DIRECTIVE`, `PEREMPTION_DIRECTIVE`,
  `NO_THINK_DIRECTIVE`, `PLANNER_SYSTEM_PROMPT`, `PLAN_JUDGE_SYSTEM_PROMPT`);
- cognitive-core flags (`PLANNER_ENABLED`, `VERIFICATION_ENABLED`,
  `PLAN_VALIDATION_ENABLED`, `PLAN_JUDGE_ENABLED`, `PLANNER_THINKING_ENABLED`),
  budgets (`MAX_TOOL_ITERATIONS`, `SUBTASK_ATTEMPT_BUDGET`,
  `REPLAN_BUDGET`), truncation thresholds (`AFFORDANCE_THRESHOLD`),
  approval tiers;
- benchmark task prompts, assertions and fixtures — **frozen**: any change
  creates a new benchmark version, and cross-version comparisons are
  forbidden.

## Operational traps

- Environment variables are read at import: a change requires
  `docker compose up -d --force-recreate <service>`; a restart is not
  enough. Setting them in the harness shell has NO effect (the harness
  talks to the agent over HTTP) — silent trap.
- `entrypoint.sh` is copied at build time: any change requires
  `docker compose build <service>` before `up -d`.
- Effective configuration is read from `/proc/1/cmdline` or
  `docker exec … env`, never from the file.
- No campaign starts without a green preflight (tool schema, image
  freshness, effective flags, resets and purges). A campaign started on
  an unverified stack is void.


# Context

The stack now serves Qwen3.6-27B EXL3 via TabbyAPI/ExLlamaV3 (dual-GPU,
vision + MTP), the langgraph/langchain-openai/openai trio is migrated to
1.x/2.x, and an MCP Playwright server is wired in alongside GhostDesk.
Goal of this effort: move the agent from "executes approved actions" to
"accomplishes multi-step web tasks autonomously", without weakening the
existing security model (approval tiers, PromptGuard, egress firewall).


# Development plan

See `PLAN.md` — detailed plan by phase (0 to 4), amendments integrated.
