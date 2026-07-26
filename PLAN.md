# Development plan — autonomous web agent

This document replaces the "Development plan" and "Amendments" sections of
`CLAUDE.md`: both amendments are integrated here in their logical place
rather than listed as a separate patch. In case of divergence, this file
is authoritative.

## Context

The stack now serves Qwen3.6-27B EXL3 via TabbyAPI/ExLlamaV3 (dual-GPU,
vision + MTP), the langgraph/langchain-openai/openai trio is migrated to
1.x/2.x, and an MCP Playwright server is wired in alongside GhostDesk.
Goal of this effort: move the agent from "executes approved actions" to
"accomplishes multi-step web tasks autonomously", without weakening the
existing security model (approval tiers, PromptGuard, egress firewall).

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
6. **Hybrid perception**: Playwright = primary channel for anything that
   is a web page (cheaper, more reliable); GhostDesk = explicit fallback
   (canvas, outside the browser, Playwright failure). The routing rule
   lives in the system prompt + a programmatic guardrail (if the
   URL/context is web and the agent attempts a pixel capture, suggest the
   DOM channel in the tool feedback).
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

## Phase 3 — Security tiers by action nature

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
4. **Prompt injection**: page content is an UNTRUSTED INPUT. Verify that
   the Playwright→LLM flow goes through the same PromptGuard inspection
   as everything else; at a minimum, any instruction detected in web
   content that requests a COMMIT action triggers escalation. Add 2
   trapped tasks to the Phase 0 suite (a page containing an injection
   requesting an out-of-scope action): success = the agent does not obey.
   The suite thus goes from 11 to **13 tasks**.

🧑 **Checkpoint: review the nature×tier matrix together before merging.**

## Phase 4 — Consolidation

Replay the full suite (now 13 tasks, including T11 and the injections), 3
repetitions, record the phase-by-phase evolution table under
`docs/campaigns/`. README: new "Autonomy" section (loop architecture, web
tier policy, known and accepted limitations). 🧑 **Final checkpoint.**

## Explicitly out of scope

- OmniParser / GPU grounding on the 5060 Ti: a later iteration, motivated
  by OBSERVED failures of the task suite (if the DOM channel covers the
  need, don't add it).
- Agent authentication on real accounts, payments, captchas.
- Multi-agent / parallel sub-agents.
- Any task requiring more than the current browser + GhostDesk scope.

## Deferred architecture effort: Mjolnir folder (second model)

Recorded after the "latency fix 2/2" checkpoint (see docs/history.md): the
cache/context isolation of the auxiliary calls (`planner_llm` —
plan_task/revise_plan/replan_task/_judge_plan) from the main loop was
diagnosed as a probable cause of part of the residual cache=0 on the
TabbyAPI side (alternating request shape evicting the shared prefix
cache) — not resolved by simply raising `cache_size` (see docs/history.md,
"chasing cache=0"). Joins the Mjolnir folder, where a second model already
has a planned role (critique/compaction): **three candidate uses for a
single architecture decision** (critique, compaction, planner/cache
isolation), to be worked out with the checkpoint's numbers rather than
treated in isolation here.
