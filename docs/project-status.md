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
  archived — see its status header): phases 0-4 delivered (contracts,
  `graph.py` trimming, moves/renames, README split, translation — French
  remaining in code/docs is intentional: model-facing strings, user-facing
  notices, frozen benchmark prompts). **Phase 5 (33-run closing campaign,
  proof of neutrality) not run.**
- Mjolnir rename: done (repo, local folder, README/docs, Docker
  project/image/volume names), ahead of Phase 5 as noted above — see the
  brief's status header for the full deviation note.

**Not yet done from this batch** (prerequisite for considering the
restructuring effort fully closed): the Phase 5 33-run closing campaign.

## Phases 2 to 4 (of PLAN.md)

Not started (context discipline, security tiers by action nature,
consolidation — see `PLAN.md`).

## Deferred effort: Mjolnir folder (second model)

Not started — see `PLAN.md`.
