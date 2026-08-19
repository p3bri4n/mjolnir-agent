# Project-specific instructions

1. At session start, read in this order: `CLAUDE.md` →
   `docs/project-status.md` (where we are) → `README.md` (short) → the
   brief of the ongoing effort (`docs/briefs/`). Never load in full:
   `docs/engineering-log.md`, `docs/resolved-bugs.md`, `docs/briefs/archive/`,
   `docs/campaigns/` reports — consult them via targeted search. Startup
   budget: ~5000 tokens; report it rather than loading more.
2. resolved bugs must be recorded in docs/resolved-bugs.md
3. progress history must be recorded in `docs/engineering-log.md`, one
   entry per `## YYYY-MM-DD — <effort>: <title>` heading, then context /
   measurements / verdict / decision.
4. always inform the user of the commands they need to type if a docker
   service needs restarting/rebuilding
5. one phase = one PR. One commit = one nature of change (never a move
   and a rewrite in the same commit — the diff becomes unreadable and the
   file history, this repo's central argument, is lost).
6. STOP 🧑 at checkpoints.
7. No opportunistic refactor outside scope — propose it at the checkpoint.
8. Any claim about a library's behavior is verified against the installed code.
9. README updated as you go, existing style. Any capability claim in the
   README or public docs is verified against the installed code before
   publication. Planned features go under Roadmap, never under Features.
   Security claims are the strictest case: a reader may deploy on the
   strength of them. This applies to any document describing current state
   (`project-status.md` included) at every update, not only when the claim
   is first written — a document that says where we are must not
   contradict the code (docs/briefs/repository-gaps.md, point 2; see also
   `docs/lessons-learned.md`).
10. Suggest obvious simplifications when it's opportune
11. Code/doc language ("restructuring + English" effort, see
    `docs/briefs/`): new content in English — docstrings, comments,
    non-exposed internal identifiers, README/CLAUDE.md/PLAN.md/docs.
    **Always write documents in English** — this includes new entries in
    `docs/engineering-log.md`/`docs/resolved-bugs.md`: only their already-written
    (dated) entries stay French as archives, never used as a template for
    new ones. Stays in French: system prompts/directives sent to the
    model and benchmark task prompts (behavior, not documentation —
    translated in an isolated phase 6, never mixed with a refactor);
    approval messages/user-facing notices (separate decision to come).
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
    - a conditional mechanism ships with its trigger-rate counter: any
      mechanism gated on a threshold/condition (a flag, a turn count, a
      budget) must expose how often it actually fired, from day one —
      never bolted on after a campaign already came back unreadable (see
      episode compaction, docs/campaigns/2026-07-28_campaign_episode-
      compaction-enabled.md, requalified "non concluante" once this
      counter was added retroactively and showed a 9-15% trigger rate).
      This rule applies retroactively to mechanisms that predate it, not
      just new ones: the planner/plan validation/plan judge shipped
      without a coverage counter, and that exact gap requalified the
      first cognitive-core ablation campaign as not conclusive — see
      docs/engineering-log.md, EFFORT 2 "judge validity check". An existing
      conditional mechanism found without a trigger-rate counter is a
      blocker for the NEXT measurement that depends on it, not a
      pre-existing condition to work around.
    - the brief before the code: every effort's instructions are written
      in `docs/briefs/` and committed before the first line. A closed
      effort gets a status header (result, deviations from the brief) and
      moves to `docs/briefs/archive/`.
    - a live smoke precedes any final measurement of a family or
      mechanism: validated empirically across five smokes, five bugs
      caught before the measurement they preceded would have counted —
      a stale Docker image, a missing root route, two fixture leaks, and
      a judge blind to the audit log (see docs/engineering-log.md for each). A
      sixth: the multi-turn compaction exercise's own live smoke caught a
      real `/approve` bookkeeping defect (docs/resolved-bugs.md #44)
      before the measurement it fed into.

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
  one-line pointer to `docs/engineering-log.md`/`docs/resolved-bugs.md`, never
  the detail copied in;
- **accepted exception**: a block documenting a verified external
  constraint (library behavior, backend gotcha, reason for a flag)
  stays — it's hard-won knowledge. Cut the paraphrase, keep the
  justification.

## Tool design contract

A tool that acts returns the resulting STATE of its action, never a bare
acknowledgment — confirmed recurring defect, not a one-off: `browser_extract`
returned a matched label without its adjacent value (docs/engineering-log.md,
"EFFORT 2.3"), `manage_plan` returned `{"ok": true}` without the plan
(docs/engineering-log.md, "EFFORT 2", merged-planning fix 1/2), `browser_navigate`/
`browser_click` returned an action confirmation without the resulting page
state (docs/engineering-log.md, "SCAFFOLDING 3.1"). Check any new or revised tool
against this before shipping.

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
- More generally: any APPLICATION CODE change (not just `entrypoint.sh`)
  is baked into the image at build time — `docker compose up -d
  --force-recreate <service>` alone reuses the EXISTING image, it does
  not rebuild it. `docker compose build <service>` first, always. This
  is easy to miss because it's confusable with the env-var trap above: a
  NEW env var still lands correctly on a plain `--force-recreate` (env
  vars are injected at container start, not baked in) even while the
  CODE that would read it is stale — producing a misleadingly clean
  preflight (flags match) while the mechanism actually under test never
  ran. Hit twice in one session, on two different services (`mcp-client`
  then `langgraph-agent` — docs/engineering-log.md, "SCAFFOLDING 3.1, POINT 1"
  and the history-diff live smoke). Verify before trusting any live
  result: compare `metadata.image_ids` across campaign JSONs (identical
  digest despite an advancing commit = stale image), or a direct `/call`
  probe / `docker exec <service> env` check.
- Effective configuration is read from `/proc/1/cmdline` or
  `docker exec … env`, never from the file.
- No campaign starts without a green preflight (tool schema, image
  freshness, effective flags, resets and purges). A campaign started on
  an unverified stack is void.
- Claude's own sandbox has no GPU and cannot run the stack's Docker
  containers (`docker compose up`, live campaigns, anything touching
  TabbyAPI/fixtures). For any task requiring this, write a script under
  `scripts/` (or hand over a self-contained shell snippet) and give the
  user a single command to run it themselves on their machine — never
  attempt the container/GPU action directly, and never fabricate or
  assume its result.

## Scripts (`scripts/`)

- `docker-menu.sh` — interactive whiptail menu (start/stop/rebuild/logs
  per service). Manual/exploratory use, not for unattended runs.
- `last-chat.sh` — read-only debug: reconstructs the last Open WebUI
  conversation(s) from `webui.db`. Use instead of ad hoc sqlite queries
  when debugging a specific run.
- `run-campaign.sh` — the reference harness runner (v1/v2 suite, smoke or
  full, pause/resume). One env config per invocation: use directly for a
  single measurement (baseline, before/after a fix, a checkpoint's
  closing campaign).
- `run-flag-sweep.sh` — generic multi-config driver, wraps
  `run-campaign.sh` once per entry in its `CONFIGS` block (env-var combos
  to compare, e.g. the cognitive-core flags ablation). Edit the block in
  place per sweep rather than passing flags — sweeps differ campaign to
  campaign, not worth a CLI. Use when a measurement needs several env
  configurations compared, not just one.

This list evolves with the project — update it when a script is added,
renamed, or retired, don't treat it as frozen. One-off campaign scripts
(named after their effort) are expected to be short-lived: generalize
into `run-flag-sweep.sh`'s `CONFIGS` pattern once a second use case
confirms it's worth keeping, or delete it once its campaign concludes —
don't let single-use scripts accumulate in `scripts/`.


# Context

The stack now serves Qwen3.6-27B EXL3 via TabbyAPI/ExLlamaV3 (dual-GPU,
vision + MTP), the langgraph/langchain-openai/openai trio is migrated to
1.x/2.x, and an MCP Playwright server is wired in alongside GhostDesk.
Goal of this effort: move the agent from "executes approved actions" to
"accomplishes multi-step web tasks autonomously", without weakening the
existing security model (approval tiers, PromptGuard, egress firewall).


# Development plan

See `PLAN.md` — detailed plan by phase (0 to 4), amendments integrated.
