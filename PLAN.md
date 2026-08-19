# Development plan — autonomous web agent

This document replaces the "Development plan" and "Amendments" sections of
`CLAUDE.md`: both amendments are integrated here in their logical place
rather than listed as a separate patch. In case of divergence, this file
is authoritative.

Phases 0–2 below are delivered (see `docs/project-status.md` for the
detail). Phase 3 is superseded by `docs/briefs/security-hardening.md` and
by benchmark v2's family C. Phase 4 (consolidation) survives as the final
step. For everything past this point — sequencing, open efforts, what's
missing — see `## Roadmap` below.

## Context

The stack now serves Qwen3.6-27B EXL3 via TabbyAPI/ExLlamaV3 (dual-GPU,
vision + MTP), the langgraph/langchain-openai/openai trio is migrated to
1.x/2.x, and an MCP Playwright server is wired in — GhostDesk has been
fully removed (Roadmap effort 3): `browser_take_screenshot` covers every
case that channel used to. Goal of this effort: move the agent from
"executes approved actions" to "accomplishes multi-step web tasks
autonomously", without weakening the existing security model (approval
tiers). PromptGuard and an egress firewall do not exist yet — they are
planned under `docs/briefs/security-hardening.md`, not part of the
current stack.

## Phase 0 — The instrument first: TASK-level harness

Before any change to the graph, build `tests_integration/test_web_tasks.py`
(opt-in `RUN_LIVE_AGENT_TESTS=1`):

1. **11 fixed, reproducible web tasks**: 7 on self-hosted fixtures
   (e-commerce catalog, doc site, mini HR app — ground truth known by
   construction), 3 on stable real sites (Wikipedia, Google/INSEE,
   books.toscrape.com), + T11 (staleness probe, see the "temporal
   awareness" amendment below). Full spec, exact prompts and assertion
   criteria: `docs/benchmark-v1.md`.
2. **PROGRAMMATIC success criterion per task** (assertion on the result:
   exact extracted value, final form state, file present) — never "the
   answer looks right". Per-task detail in `docs/benchmark-v1.md`.
3. **Per-run metrics** (all 11 tasks): success rate /11, steps per task,
   tokens consumed, approval interventions required, duration, classified
   failure cause (navigation / extraction / hallucination / loop /
   external block / infra).
4. **Immediate baseline**: replay the suite against the CURRENT agent,
   as-is, 3 repetitions. Record under `docs/campaigns/` (see the reporting
   convention in `docs/operations/runbook.md`/`scripts/run-campaign.sh`).
   This is the point zero — the whole effort is measured against it.

🧑 **Checkpoint: I sign off on the 11-task list and the baseline.**

## Phase 1 — Plan → act → verify → replan loop

Progress status (campaign scores, past checkpoints): see
`docs/project-status.md`, not here — this file stays the roadmap.

**Rest of Phase 1 ("cognitive core")**: points 1 to 7 below are detailed
and sequenced iteration by iteration (one iteration = one mechanism = one
designated judge = one checkpoint) in
`docs/briefs/phase-1-coeur-cognitif.md`, committed before any code for
this sub-effort. This plan keeps the overview; the brief is authoritative
for execution order and pass criteria.

In `app/graph.py`, without breaking the existing approval flow:

1. **Explicit plan state** in `AgentState`: list of subtasks (description,
   status: todo/in-progress/done/failed, result), overall objective,
   per-subtask attempt counter.
2. **Planner node**: on receiving a task, decompose it into subtasks
   (structured JSON, validated schema). Replanning triggered only on
   subtask failure or an invalidating discovery — not every turn (cost).
3. **Plan validation pipeline** (from the dedicated amendment), inserted
   between the planner node and execution:
   a. **Programmatic heuristics** (dedicated module, unit-testable):
      existing tools, domains within scope, size bounds, no
      duplicates/cycles, verifiable success criterion per subtask,
      plan/task tier consistency. Justified rejection → back to the
      planner, max 2 cycles then human escalation.
   b. **LLM judge** (creation + replanning only): structured JSON verdict
      (feasible yes/no, risks, missing steps). Negative verdict → back to
      the planner with the verdict. Tracked metrics: veto rate, outcome of
      vetoed-then-fixed plans. Withdrawal clause: if, after the full
      Phase 0 suite, the judge hasn't caught any defect the heuristics
      didn't already see, disable it by default (env flag) and record it.
   c. **Tiered human validation**: plan tier = tier of its worst action.
      Pure READ → auto after a+b. WRITE → human approval of the plan
      (relaxable via a session grant). COMMIT → mandatory plan approval
      AND individual approval of the committing action at execution time
      (not combinable into a single yes). Plan displayed in the existing
      approval format (numbered subtasks + each one's tier). Any
      replanning goes back through the full pipeline.
4. **Systematic post-action verification**: after every web tool call, the
   agent must observe the result BEFORE the next action — via the
   Playwright observation (targeted accessibility/DOM snapshot) rather
   than a pixel capture when possible. The action's success criterion
   stated BEFORE its execution (in structured reasoning), compared
   afterward.
5. **Failure budget**: N attempts per subtask (env, default 3) with an
   alternative strategy required on each retry (not the same action
   repeated); beyond that → replanning; if replanning is exhausted →
   honest failure report to the user with the state reached. Never an
   infinite loop, never a false success.
6. **Hybrid perception** (as delivered): Playwright = primary channel for
   anything that is a web page (cheaper, more reliable); GhostDesk =
   explicit fallback (canvas, outside the browser, Playwright failure).
   **Superseded**: the visual-channel feasibility probe found nothing
   tested is lost without GhostDesk (`browser_take_screenshot` covers
   canvas/WebGL/images/native PDF); GhostDesk is removed, `ocr-service`
   kept but moved from a callable tool to a graph capability — see
   Roadmap effort 3 below.
7. **Temporal awareness** (from the dedicated amendment):
   a. Date injection into the system prompt on EVERY request: DAY
      granularity (never the time — preserves the prefix cache), format
      "Current date: {weekday} {date} ({timezone})", placed at the END of
      the system block after the static sections. Timezone from the host
      env.
   b. Staleness directive in the system prompt (~10 lines): the model's
      estimated cutoff (conservative bound, recorded with its source),
      categories to verify via the web before asserting (versions,
      prices, news, roles, service status), memory-based answers allowed
      only for stable facts.

Unit tests: mocked planning, state transitions, validation pipeline
(heuristics, judge, tiers), failure budget, hybrid routing, date
injection. Then replay the Phase 0 suite (11 tasks, T11 in particular):
the delta vs. baseline is this phase's verdict. Metric added to the
harness: human interventions per task — the validation pipeline's goal is
for this number to GO DOWN at equal or better control. 🧑 **Checkpoint.**

## Phase 2 — Context discipline

1. **Images**: keep only the last 2 captures in the history; earlier ones
   replaced by their textual description generated at the time of use
   (already in the thread) + a `[capture removed]` note.
2. **Episode compaction**: beyond a turn-count threshold (env), completed
   subtasks are compacted into a structured summary (subtask, key
   actions, result) injected in place of the detailed turns. The plan and
   the objective always stay whole.
3. Before/after measurement on the Phase 0 suite: tokens/task and success
   rate (compaction must NOT degrade the rate — if it does, thresholds to
   revisit at the checkpoint). 🧑 **Checkpoint.**

## Phase 3 — Security tiers by action nature (superseded)

**Not started as written.** Superseded by `docs/briefs/security-hardening.md`
and by benchmark v2: point 4 below is stale (see correction after it).
Kept for historical context; execution order and pass criteria now live
in the security-hardening brief (Roadmap effort 5).

Extend the existing approval policy, without removing from it:

1. **Classification by nature** for web tools: READ (navigation, snapshot,
   extraction) → auto-approvable tier; REVERSIBLE WRITE (filling a field,
   clicking without submitting) → auto-approvable under a granted
   session; COMMIT (submitting a form, downloading/uploading, any action
   with an external effect) → mandatory approval, not covered by the
   session grant. Tool→nature mapping in config, not in code.
2. **Per-task domain scope**: allowlist declared when the task starts; any
   navigation outside scope → approval escalation. Consistent with the
   egress firewall philosophy.
3. **Dedicated browser profile**: the agent's Playwright context is blank
   (no personal-profile cookies/credentials), persistent per task only if
   needed.
4. **Prompt injection** (as originally written; superseded): page content
   is an UNTRUSTED INPUT; the plan was to add 2 trapped tasks to the
   Phase 0 suite (11 → 13 tasks). **Superseded**: benchmark v2's family C
   already covers injection and scope (C1/C2/C3), measured 9/9 at
   baseline — see Roadmap effort 5 below, which also notes this 9/9 leaves
   no progression margin for a security campaign and requires a new
   hostile family (v2.1) first.

🧑 **Checkpoint: review the nature×tier matrix together before merging.**

## Phase 4 — Consolidation

Final step, once the Roadmap efforts below land: replay
the current frozen suite (v2, not the stale "13 tasks" figure above), 3
repetitions, record the phase-by-phase evolution table under
`docs/campaigns/`. README: new "Autonomy" section (loop architecture, web
tier policy, known and accepted limitations). 🧑 **Final checkpoint.**

## Roadmap

Everything past Phase 4, absorbed from `docs/briefs/update-plan.md`
(archived — see `docs/briefs/archives/update-plan.md`) now that this
section is the single authoritative source for sequencing and status.
**Sequencing principle**: what makes every later measurement cheaper
comes first; what may *remove* mechanisms comes before what improves
them; what needs an instrument waits for the instrument. Full narrative
and campaign numbers for every item below: `docs/project-status.md`
(current state) and `docs/engineering-log.md` (entry-by-entry history).

### Effort 1 — Make campaigns cheap (prerequisite to everything)

1.1 (tool-schema weight audit) and 1.2 (remove/trim heavy unused MCP
servers) — **done**. 1.3 (reopen parallel campaign execution) —
**deferred**, quantified (×2.2–×3 expected gain), blocked on per-worker
isolation (session reset, volume purge, distinct thread ids covering
four known contamination incidents). Brief:
`docs/briefs/effort-1.3-parallel-campaigns.md`.

### Effort 2 — Factorial ablation of the cognitive-core flags

**Closed.** Decisive cfg1-vs-cfg8 measurement: cfg1 (all four flags off)
15/15 vs cfg8 (all on) 13/15 on the success judge, +76% cumulative time
for essentially identical real work → defaults flipped to `false` for
`PLANNER_ENABLED`/`VERIFICATION_ENABLED`/`PLAN_JUDGE_ENABLED`.
`PLAN_VALIDATION_ENABLED` kept `true` (safety-value exception, no LLM
call — de facto inert without a planner, see `docs/project-status.md`).
A fifth "merged planning" condition was tried and dropped: it engaged on
one task family but never revised a plan mid-task, so it had nothing to
demonstrate. Full v2 campaign confirms no family regressed, family A
materially improved. Removal PR: defaults-to-`false` done; deleting the
now flag-gated-dead code (`plan_task`/`verify_action`/the judge, their
directives and tests) is **not done**.

### Effort 3 — GhostDesk removal

**Closed.** GhostDesk fully removed (container, image, volume, secrets).
`browser_take_screenshot` covers every case it used to (canvas, WebGL,
images, native PDF) — the feasibility probe found nothing tested is
lost. `ocr-service` kept as a graph capability, not a callable tool; a
proactive-trigger design was tried and **abandoned, not implemented** —
empirically falsified (`browser_snapshot`'s text carries no detectable
canvas/WebGL/alt-less-image signal). Replaced by a routing hint on
`browser_take_screenshot`'s own tool description. Live-verified: E1 3/3,
E2 2/3 (a vision-reading limit, not a routing defect), E3 3/3.

### Effort 4 — Scaffolding improvements

Brief: `docs/briefs/scaffolding-optimisation.md`. Diff-based observation
history: built, live-measured, result mixed/within noise on short tasks
— flag stays off, a longer task is the natural next candidate if
revisited. Coarse-grained actions: `browser_click`/`browser_navigate`
now return the resulting page state in their own response (closed,
live-verified, turns and tokens both down on the two tasks measured);
bulk `browser_extract` adoption closed as a non-problem (already
adopted, the "never chosen" premise was itself an audit-log blind spot
— `docs/resolved-bugs.md` #52); a form-filling composite tool is
shelved, no measured bottleneck.

### Effort 5 — Security

Brief: `docs/briefs/security-hardening.md`. Gated on a working
instrument: benchmark v2's family C already measures 9/9 at baseline, so
a new hostile family (v2.1: indirect/multi-step injection, a
canary-token task) must ship first — a perfect score leaves no
progression margin to demonstrate an improvement against. **Not
started.**

### Effort 6 — Unblocking and session persistence

Brief: `docs/briefs/unblocking-and-session.md`, committed. Worth doing
only if usage frequency justifies it — currently one task blocked across
the whole v2 suite. **Not started.**

### Effort 7 — Quantisation evaluation

Brief: `docs/briefs/quantisation-evaluation.md`. Gated on a stable
baseline, sequenced last. **Not started.**

### Effort 8 — Visual-only navigation mode

A `VISUAL_NAVIGATION_ONLY` mode (capture + OCR only, no DOM/accessibility
tree, coordinate-based interaction) — kept separate from effort 3
deliberately: effort 3 redistributes a capability, this would create a
whole operating mode with its own action space. Prerequisites: effort 3
(done) and effort 1 (campaigns cheap enough to afford a second
reference — not yet). **Not started.**

### Backlog (not sequenced into an effort above)

- **External calibration**: one held-out WebArena run via BrowserGym,
  never done — no current signal on whether ten-plus campaigns of tuning
  against the same fixtures has caused overfitting to them.
- **Campaign control** (`docs/briefs/archives/campaign-control.md`) never
  verified end-to-end against a real live campaign — unit-level only.
- **Multi-agent / sub-agent tool**: discussed as a context-boundary
  mechanism (a single parameterised READ-tier tool, own budget,
  closed-question outputs only), no brief yet — belongs in the Mjolnir
  folder arbitration below, not a separate effort.

## Explicitly out of scope

- OmniParser / GPU grounding on the 5060 Ti: a later iteration, motivated
  by OBSERVED failures of the task suite (if the DOM channel covers the
  need, don't add it).
- Agent authentication on real accounts, payments, captchas.
- Multi-agent / parallel sub-agents (a single parameterised READ-tier tool
  is discussed, not yet a brief — see the Roadmap backlog below).
- Any task requiring more than the current browser scope (GhostDesk is
  removed — see Roadmap effort 3).

## Deferred architecture effort: Mjolnir folder (second model)

Recorded after the "latency fix 2/2" checkpoint (see docs/engineering-log.md): the
cache/context isolation of the auxiliary calls (`planner_llm` —
plan_task/revise_plan/replan_task/_judge_plan) from the main loop was
diagnosed as a probable cause of part of the residual cache=0 on the
TabbyAPI side (alternating request shape evicting the shared prefix
cache) — not resolved by simply raising `cache_size` (see docs/engineering-log.md,
"chasing cache=0"). Joins the Mjolnir folder, where a second model already
has a planned role (critique/compaction): **three candidate uses for a
single architecture decision** (critique, compaction, planner/cache
isolation), to be worked out with the checkpoint's numbers rather than
treated in isolation here.
