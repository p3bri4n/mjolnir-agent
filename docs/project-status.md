# Progress status

Changes at every checkpoint — see `PLAN.md` for the roadmap (changes
rarely, source of truth in case of divergence) and `docs/history.md` for
the full chronological detail.

## Phase 0 — task-level harness

Delivered: `tests_integration/test_web_tasks.py`, 11 tasks
(`docs/benchmark-v1.md`), programmatic criteria, per-run metrics, baseline
recorded.

## Phase 1 — plan → act → verify → replan loop

**First slice** (URL-fabrication guardrail + snapshot truncation):
Campaign A, budget 20, overall score 16/33 → 24/33 — none of the 5 pass
criteria set at the checkpoint were fully met (see docs/history.md).

**Final Campaign A** (cross-task isolation + `browser_extract`): 30/33.

**"Cognitive core"** (the 7 points of Phase 1, sequenced iteration by
iteration — see `docs/briefs/phase-1-coeur-cognitif.md`): delivered and
measured. Final campaign (4 mechanisms active): 29/33, consistent with
Campaign A (30/33, not a regression) — task-by-task detail in
docs/history.md and `docs/campaigns/2026-07-23_campaign_coeur-cognitif.md`.
T1/T7/T9 backlog investigated and closed (see docs/history.md,
"PERSISTENCE INVENTORY" and the T7/T9 investigations).

**Follow-up on this batch** (see docs/history.md):
- Campaign persistence (`campaign_persistence.py`): JSON per run,
  `thread_id`, raw TabbyAPI samples — delivered.
- Cognitive-core flags: defaults flipped to `true` (measured and
  adopted), preflight guardrail (`check_agent_flags`) — delivered.
- Audit blind spot: `call_tools` (post-approval) now logs to the audit
  trail too, closing the gap where the first call of each tool per
  thread was invisible — delivered.
- Bulk mode for `browser_extract`: `urls` parameter checks several pages
  in one call, TIER_READ, replacing the `browser_evaluate`-based
  workaround — delivered.
- Restructuring + English (`docs/briefs/archive/restructuration-et-anglais.md`,
  archived and closed — see its status header): all 6 phases delivered,
  including Phase 5 (33-run closing campaign, run 2026-07-28): **29/33,
  consistent with the pre-restructuring checkpoint (29-30/33), no
  regression** — see
  `docs/campaigns/2026-07-28_campaign_post-rename-mjolnir-v2.md`. A first
  attempt the same day scored 14/33 and is marked invalid in its report:
  the self-hosted fixtures (`fixture-catalog`/`fixture-docs`/`fixture-hr-app`,
  profile `test-fixtures`) hadn't been started before launch — operational
  mistake, not a behavioral regression.
- Mjolnir rename: done (repo, local folder, README/docs, Docker
  project/image/volume names). Ran ahead of Phase 5 (deviation from the
  brief's declared order, at explicit user request) — since covered by a
  green Phase 5 result above.

**Preflight gap fixed**: `campaign_preflight.py` now checks the
`test-fixtures` profile is reachable before a campaign starts
(`check_fixtures_reachable`) — closes the gap that let the invalid 14/33
run above execute for 44 minutes on unreachable fixtures.

## Phase 2 — Context discipline

Point 1 (image retention, `MAX_IMAGES_IN_CONTEXT`) was already delivered
as part of the cognitive-core batch above — not revisited here.

Point 2 (episode compaction) delivered, OFF by default
(`EPISODE_COMPACTION_ENABLED=false`): completed subtasks' raw turns
replaced by a structured summary in what's sent to the LLM only
(checkpointer/audit log untouched) beyond
`EPISODE_COMPACTION_TURN_THRESHOLD` (40) messages.

**2026-07-28 campaign (30/33) — REQUALIFIED "non concluante"**: mechanism
triggered in only **9-15% of runs** (`episode_compaction_messages_max`/
`episode_compaction_applied_count`, coverage judge added retroactively —
see below — applied via an archives-only proxy reconstruction, the real
counter didn't exist at run time). Below any reasonable coverage bar, this
campaign mostly measured the noise of runs the mechanism never touched,
not its effect. The observed cache=0 IMPROVEMENT (16.7% vs 20.9% baseline)
specifically cannot be attributed to compaction either way: replacing
messages rewrites the prompt prefix, which should if anything DEGRADE the
KV cache hit rate for that request, not improve it — the direction of the
delta itself is inconsistent with compaction being the cause, reinforcing
that it's noise. See `docs/campaigns/2026-07-28_campaign_episode-compaction-enabled.md`.

**Coverage counters made permanent** (`episode_compaction_messages_max`/
`episode_compaction_applied_count`, logged on EVERY `call_llm` call
regardless of the flag — `app/graph.py`/`test_web_tasks.py`): any future
campaign now reports its own trigger rate, no reconstruction needed. New
rule added to `CLAUDE.md` (measurement rules): a conditional mechanism
ships with its trigger-rate counter.

**Real tokens/task judge added** (`prompt_tokens_total`,
`campaign_persistence.aggregate_prefill_stats`): sum of
`cached_tokens`+`new_tokens` per TabbyAPI call — prefill seconds alone
conflate token volume with cache-hit rate and backend throughput, exactly
what made the campaign above unreadable as a token-reduction signal.

**Targeted test attempted, inconclusive for a deeper reason than expected**
(`tests_integration/probe_episode_compaction.py`, never added to the
frozen suite): building a local task guaranteeing >60 messages while
`PLANNER_ENABLED`+`VERIFICATION_ENABLED` stay on (required for compaction
to have any "fait"/"echoue" subtask to compact) ran into the plan/verify
pipeline's OWN structural limits — `_PLAN_SUBTASKS_MAX=8` bounds subtask
granularity, and `verify_action`'s page-snapshot-based judge can't confirm
success criteria that aren't visible on the page (e.g. "extract and add to
a list"), so it returns `non_atteint` even on genuine progress, exhausting
`SUBTASK_ATTEMPT_BUDGET`×`REPLAN_BUDGET` after ~17 tool_calls,
reproducibly (identical across 3 reps, not sampling noise). **Long
single-task episodes appear structurally rare with the current
architecture, not just under-sampled by the 11-task benchmark** — a
useful diagnostic for benchmark v2's task design in its own right.

**Phase 2 fully closed**, see `docs/briefs/archive/phase-2-discipline-contexte.md`
(written retroactively — deviation from "brief before code", assumed and
recorded) for the full reasoning. `EPISODE_COMPACTION_ENABLED` stays
`false`; re-evaluation deferred to benchmark v2's long tasks, designed
with page-observable success criteria to work with the current verifier.

## Phases 3 to 4 (of PLAN.md)

Not started (security tiers by action nature, consolidation — see
`PLAN.md`).

## Deferred effort: Mjolnir folder (second model)

Not started — see `PLAN.md`.

## Campaign control (`docs/briefs/B2-campaign-control.md`)

Tooling effort, outside PLAN.md's phase numbering. **Part 1 (live
progress) delivered**: incremental `<campaign-id>.progress.json`,
per-task ETA range (`compute_remaining_eta`), read-only dashboard page
(`/campaign`) — see docs/history.md, "B2.1 — CAMPAIGN LIVE PROGRESS".
Parts 2-3 (pause/resume, segment validity rules for cache-sensitive
metrics) not started.
