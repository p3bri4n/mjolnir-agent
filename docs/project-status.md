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
`EPISODE_COMPACTION_TURN_THRESHOLD` (40) messages. Single-variable
validation campaign run 2026-07-28 with the flag forced to `true`: **30/33,
consistent with the 29/33 baseline (no regression)**, prefill total lower
(715.7s vs 945.9s) and cache=0 rate lower (16.7% vs 20.9%) — see
`docs/campaigns/2026-07-28_campaign_episode-compaction-enabled.md`.
**N=1 per side**: directionally encouraging, not proof of a real token
reduction given this project's documented run-to-run noise (16→24→20→24
zigzag, docs/methodology.md) — more reps needed before considering
flipping the default. Flag reverted to `false` after the experiment.

Point 3 (tokens/task before/after) partially covered by the campaign
above; a dedicated tokens/task metric (not just prefill seconds) is still
to be added if this mechanism is pursued further.

## Phases 3 to 4 (of PLAN.md)

Not started (security tiers by action nature, consolidation — see
`PLAN.md`).

## Deferred effort: Mjolnir folder (second model)

Not started — see `PLAN.md`.
