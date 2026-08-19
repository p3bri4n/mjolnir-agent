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
- Cognitive-core flags: the decisive cfg1-vs-cfg8 ablation (`docs/history.md`,
  "EFFORT 2.4") found cfg1 (all four off) strictly beating cfg8 (all on) on
  the success judge — **15/15 vs 13/15** — at **+76 % cumulative time** for
  essentially identical real work (195 vs 193 total tool calls). Decision:
  defaults flipped back to `false` for `PLANNER_ENABLED`/
  `VERIFICATION_ENABLED`/`PLAN_JUDGE_ENABLED`, confirmed clean by a full v2
  campaign (no family regressed, family A materially improved).
  `PLAN_VALIDATION_ENABLED` kept `true` — a programmatic heuristic gate, no
  LLM call, untouched by the cost argument above — but `validate_plan`
  no-ops whenever `state["plan"]` is empty (`app/graph.py`), which is every
  turn while `PLANNER_ENABLED` is `false`: **de facto inert without a
  planner**, not actively validating anything today. Removal status: the
  defaults-to-`false` PR is done and campaign-verified; the follow-up PR
  (deleting `plan_task`/`verify_action`/the judge, their directives and
  tests, rather than flag-gating them) is **not done** — both nodes are
  still present in the graph, no-op'd by the flags. Preflight guardrail
  (`check_agent_flags`) — delivered.
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

## Campaign control (`docs/briefs/archive/A6-campaign-control.md`)

Tooling effort, outside PLAN.md's phase numbering. **Closed, both parts
delivered**: live progress (incremental `progress.json`, per-task ETA
range, read-only dashboard page) and pause/resume (sentinel-triggered
pause, `--resume` with drift refusal and staleness warning, segment
tracking) — see docs/history.md, "B2.1 — CAMPAIGN LIVE PROGRESS" and
"B2.2 — PAUSE/RESUME + SEGMENT VALIDITY RULES". Not verified end-to-end
against a real live campaign (unit-level only) — flagged as a follow-up
in the B2.2 history entry.

## Benchmark v2 (`docs/briefs/B3-benchmark-v2.md`)

Design validated at checkpoint 2026-07-30 (22 tasks, 6 families, CuP
headline metric).

**Slice 1 (family F, regression core) delivered and smoke-verified
live**: `tests_integration/test_web_tasks_v2.py`, T3/T5/T6/T10 reused
verbatim from v1 by import (identity-checked), `run-campaign.sh --suite
v2` — see docs/history.md, "B3 SLICE 1 — BENCHMARK V2, FAMILY F". Live
smoke (2026-07-29, n=1/task): 4/4 passed.

**Slice 2 (family B, intent α only) delivered and fully smoke-verified
live at all 3 loads**: leave-request task at 3 policy loads
(easy/medium/hard), CuP = task success AND no policy violated (read from
the real audit log). New `NEVER_GRANTABLE_TOOLS_EXTRA` env var
(`app/approval_policy.py`) needed to make medium/hard's policy
enforceable at all — see docs/history.md, "B3 SLICE 2 — BENCHMARK V2,
FAMILY B, INTENT α ONLY" and its follow-up entry. Live smokes
(2026-07-29, n=1/load): easy 1/1 (`cup: true`); medium+hard 2/2
(`cup: true` both, cross-checked against the raw audit log's `tier`
field, not just the harness report) — first medium/hard attempt looked
like a policy-mechanism bug but was a stale Docker image
(`--force-recreate` without a prior `build`), documented as a caught
operational trap. B-β (stock update, admin view) deferred: needs an
entirely new fixture app, none exists.

**Slice 3 (family D, honesty) delivered and smoke-verified live**: D1/D2
wrap v1's T7/T11 ("heir of", not "verbatim" — reused by calling v1's
functions under new v2 task_ids), D2's live ground-truth fetch kept lazy
(`_family_d_tasks()`, never at import) — see docs/history.md, "B3 SLICE
3 — BENCHMARK V2, FAMILY D (HONESTY)". Live smoke (2026-07-30, n=1/task):
2/2 passed (D1: no invented price; D2: correct live version found).

**Slice 4 (family A — A2) delivered and measured live**: multi-page
naming-scheme audit (3 deliberately non-conforming catalog refs + a new
docs page stating the format) — see docs/history.md, "B3 SLICE 4".
Planning checkpoint decided A1/A3/A4 ship as their own future PRs,
cost/risk ordered, not bundled with A2 — see that entry for the full
design of all four and the central architectural risk
(`verify_action`/multi-page aggregation) it resolves by reading
`app/graph.py` directly. Two real bugs caught by live smoking (a ground-
truth inconsistency with the frozen `KX-4471` ref, then an assertion
overcorrection) — both fixed, documented in the same entry. **Live
measurement (3 repetitions, the family's own rate)**: 3/3, confirmed
genuine via the raw audit log (agent used `browser_evaluate` to fetch all
30 product pages in one tool call, not the `browser_extract` bulk mode
originally hypothesized as the mitigation).

**Slice 5 (family A — A1) built, measured live, result 0/3 —
documented as a capability-limit finding, not a bug**: cross-site
reconciliation (catalog category+price vs. a docs config page) — see
docs/history.md, "B3 SLICE 5". Fixture content verified correct by
direct inspection; one of the three runs reached all 4 correct qualifying
products before exhausting its budget, never reaching the docs
cross-check phase. Unlike A2's 3/3 (same session), none of the 3 runs
chose a bulk `browser_extract`/`browser_evaluate` shortcut — A1 is
structurally ~2x A2's task (two chained site audits), and checkpoint
decision was to leave it as-is (no prompt hint, no fixture-scale
reduction) and report the 0/3 honestly, same spirit as Phase 2's
abandoned `probe_episode_compaction.py` finding.

**Slice 6 (family A — A3) delivered and measured live**: ambiguity to
resolve (hr-app `/contacts` shows two RH candidates under the same role
label, docs disambiguates) — see docs/history.md, "B3 SLICE 6". First v2
task with a third outcome beyond success/failure (`outcome`:
correct/safe_deferral/wrong, row-schema extension gated to A3 only,
every other family untouched). Same overcorrection bug as A2/KX-4471 hit
and fixed the same way (a correct answer legitimately naming the
excluded alternative must not be penalized). **Live measurement (3
repetitions)**: 3/3, all `outcome=correct`.

**Slice 7 (family A — A4) delivered, family A now fully built**: A4 is a
GUIDED cross-site workflow (explicit numbered steps, not an open audit —
design choice made after A1's 0/3) — see docs/history.md, "B3 SLICE 7".
A 9-step extension aiming for the brief's 60-message coverage target was
tried and reverted (reproducibly 0/3, hit `MAX_TOOL_ITERATIONS`, a
measured/frozen budget); checkpoint decision kept the working 7-step
version (19-41 messages, short of 60) and accepted the shortfall as
documented rather than force it — same trade-off Phase 2 already made
for v1's compaction coverage. **Live measurement**: 3/3 (first smoke) +
2/3 (final measurement after revert) = 5/6 across two windows. A4's
secondary judge (tokens/task, compaction on vs off) remains a future,
separate live A/B campaign.

**Slice 8 (family C — hostile content, injection and scope) delivered
and measured live**: C1 (injected instruction in a catalog product
description), C2 (injected instruction to fetch-and-eval a remote
script — success reframed as "dangerous tool never invoked", documented
deviation from the brief's "escalation = success" wording, see
docs/history.md), C3 (link to an out-of-scope `.example` domain) — see
docs/history.md, "B3 SLICE 8". Per the brief's Rules, a C-task failure
is a security finding, never pooled into a capability score. A stale
`fixture-catalog`/`fixture-docs` Docker image caused a first smoke's
0/3-extraction-0/3-breach result to be a flattering zero (agent never
exposed to the injected content); fixed via image rebuild. **Live
measurement (3 repetitions, re-verified against the raw audit log): 9/9
— extraction 3/3 and 0/3 breach on each of C1/C2/C3.**

**Slice 9 (family B, intent β — stock update, admin view) delivered and
measured live**: new dedicated fixture (`fixture-admin`, no existing
fixture had an admin/stock concept), same 3-tier policy escalation and
word-for-word-identical-prompt discipline as intent α — see
docs/history.md, "B3 SLICE 9". A first smoke caught a real fixture bug
(no `/` route, preflight's reachability probe got a 404), fixed by
adding a redirect matching `fixture-hr-app`'s own convention. **Live
measurement (3 repetitions per load): easy 3/3 CuP 3/3, medium 3/3 CuP
3/3, hard task-success 3/3 but CuP only 1/3** — a genuine security
finding, audit-log-verified: in 2 of the 3 hard runs the agent used
`browser_evaluate` (always never-grantable) to fill/inspect the form via
direct DOM manipulation instead of normal UI interaction, correctly
caught by the `no_never_grantable_tool` policy both times. **Family B is
now fully built** (both intents).

**Slice 10 (family E, perception channels — E1/E2/E3) delivered and
measured live**: new dedicated fixture (`fixture-perception`) — see
docs/history.md, "B3 SLICE 10". Two live-verified leaks in E2 (the
visual-only task) caught and fixed before the value ever needed genuine
perception to be read — a literal JS string readable by
`browser_extract`'s DOM text-node walker, then a char-code-obfuscated
version still readable via `browser_evaluate`'s raw page-source read —
fixed with a pre-rendered PNG (no client-side JS at all). E3's economic
judge was redesigned before its first live run after finding the audit
log structurally blind to the read-tier tools actually used
(`browser_extract` never logged by design) — replaced with the
`/context` endpoint's existing "images" block count. **Live measurement
(3 repetitions): E1 3/3, E2 1/3 (a genuine capability-limit finding —
audit-log-verified channel confusion between GhostDesk's `screen_shot`
and the correct `browser_take_screenshot`), E3 3/3 with visual capture
used in 0/3 runs** (DOM-first routing confirmed, never captures "by
reflex").

**E4 (native dialog, outside the browser) is explicitly out of scope —
a user decision, not a deferral.** Family E closes at 3/4 tasks;
GhostDesk's own justification (the question only E4 could answer) stays
permanently unmeasured by this benchmark.

Benchmark v2 is now feature-complete per this project's scope decisions
(families F, A, B, C, D fully built; family E at 3/4 by explicit
choice).

**Post-measure follow-up** (see docs/history.md, "BENCHMARK V2 — POST-
MEASURE FOLLOW-UP"): B-β hard's CuP 1/3 traced to a real root cause, not
BULK_CHECK_DIRECTIVE (hypothesis falsified by archives) — a `target`
format defect in `mcp-client`'s ref= handling, present since 2026-07-22
across every fixture, fixed generically (`_normalize_ref_targets`) plus a
new TIER_READ `browser_inspect` tool. Three archives-only notes recorded
in `docs/benchmark-v2.md` (new file): what CuP actually measures (agent
intention, not deployed safety), A4's compaction-coverage judge (flag
never enabled on any A4 run — flattering zero, and even the raw
message-count proxy shows 0/3 final runs crossing the compaction
threshold), and family C's 9/9 baseline (no progression margin left for
the security plan's Phase 2-4 — scoped to v2.1, fixtures stay frozen).

**A4/compaction closed** (see docs/history.md): full-fleet distribution
(101 threads, only 4 reach the 40-message threshold, all family A4):
neither unreachable nor representative. Building the requested "long
task" exercise surfaced a hard ceiling (`MAX_TOOL_ITERATIONS=20` caps a
single task at ~40-42 messages) — redesigned as multi-turn threads
instead, which in turn surfaced and fixed a real `/approve` bug
(owui_message_count desync for any non-Open-WebUI multi-turn client, see
docs/resolved-bugs.md #44). Final live measurement (3 reps × 2 threads,
flag off then on): **negative result, not a non-result this time** —
flag on nearly doubles tool_calls/tokens, 6/6 runs hit
`MAX_TOOL_ITERATIONS`, dependent-turn success drops from 4/6 to 0/6,
despite real compaction coverage (19-26 applied/run, no flattering
zero). `EPISODE_COMPACTION_ENABLED` stays `false`.

**Visual-channel feasibility probe delivered** (preliminary to GhostDesk
removal, `docs/architecture/visual-channel-feasibility.md`): 8
content-rendering patterns checked directly (no agent loop) across
`browser_snapshot`/`browser_extract`/screenshot+OCR. Canvas, WebGL,
image, and native-PDF-viewer content are unreadable by any DOM channel
but fully readable via `browser_take_screenshot` (Playwright's own tool,
unrelated to GhostDesk); cross-origin iframes and open shadow DOM are
covered by `browser_snapshot` already. **Conclusion: GhostDesk removal
would lose nothing tested here** — its only unique capability
(out-of-browser interaction) is E4's territory, already out of scope by
explicit user decision.

## Consolidated plan (`docs/briefs/update-plan.md`) — effort 1

**Effort 1.1 (tool-schema weight audit) and 1.2 (removal) delivered.**
git/terminal removed entirely (`services/mcp-terminal/` deleted);
desktop(GhostDesk)/ocr removed from the tool schema only, containers
kept running for effort 3's future rework. `GROUNDING_DIRECTIVE`
removed (coupled measured-behavior change, judge declared and passed).
Real schema weight: 10 979 → 6 047 tokens (-44.9%), confirmed live
(smoke `post-effort1.2-smoke`, T1/T3/T7, 3/3, zero calls to a removed
tool). See docs/history.md for the full measurement and campaign.
**Candidate follow-up (not scheduled)**: closing effort 2 point 3
surfaced schema ORDER, not just weight/count, as a variable affecting
tool adoption (`manage_plan` position in the tools array measurably
changed whether the model used it, all else held constant) — see
docs/history.md, "TOOL SCHEMA ORDER AFFECTS ADOPTION". Worth an
archives-only check (does per-tool usage frequency correlate with
current schema position?) next time this family of work is revisited.
**Effort 1.3 (parallel run execution): resumed, Phase 0 passed live.**
Brief written (`docs/briefs/effort-1.3-parallel-campaigns.md`) after
efforts 1.2 and 2 landed; re-measured on fresher archives (GPU fraction
of run time 51%→26% since GPU placement + effort 2.4's cognitive-core
removal), recomputing to ×1.97 pessimistic / ×3.0 optimistic for N=3.
Phase 0's two live checks (2026-08-11, `scripts/probe-parallel-phase0.sh`)
both passed: TabbyAPI's real concurrent speedup is ×2.0 (the pessimistic
bracket, not optimistic — sets the realistic Phase 3 target at ~×2, not
×3), and `playwright-mcp` session isolation under real concurrent load is
confirmed (context scoped per MCP session, matching the already-verified
`docs/resolved-bugs.md` finding). See docs/history.md, "EFFORT 1.3 —
PHASE 0 LIVE RESULT".

**Phases 1 and 2 delivered (2026-08-11), nothing live-run yet.** Phase 1:
`mcp-client`'s persistent sessions scoped by `worker_id`
(`(server_name, worker_id)` keying), `worker_id` threaded all the way
from `ChatCompletionRequest`/`ApprovalDecisionRequest` through
`config["configurable"]` to `_call_mcp_tool` — a gap the brief hadn't
named (nothing on `langgraph-agent`'s side read the parameter Phase 1
gave `mcp-client`). Phase 2: the harness's sequential loop replaced by a
shared `_run_planned_tasks` N-worker pool (`test_web_tasks.py`, used by
both v1's `_run_campaign` and v2's `_run_campaign_v2`); scope grew
mid-implementation (checkpoint reported, user: "continuer maintenant,
périmètre élargi") once the pause/resume cursor turned out to be
`campaign_persistence.py`'s own documented contract, also depended on by
the dashboard's live ETA — fixed with `remaining_runs()` (a set
difference, safe under out-of-order completions) in both
`campaign_persistence.py` and its deliberate dashboard mirror. A second
shared-fixture hazard (`stock_updates.json`, family B-β) was found and
serialized the same way as T5's downloads while porting the loop, not
anticipated in the brief. Test suites: `mcp-client` 55→60,
`langgraph-agent` 466→478, `dashboard` 19→22, all green — everything
verified against synthetic state only, no live Docker run in this phase.
Full detail: docs/history.md, "EFFORT 1.3 — PHASES 1-2 DELIVERED".

**Phase 3 decisive measurement run (2026-08-11): primary judge MISSED**
— wall-clock ×1.10 (N=3 vs N=1), far short of the ~×2 target. First
diagnosis (TabbyAPI KV-cache eviction under 3 concurrent conversations)
was itself built on a flawed metric: `collect_tabbyapi_raw_samples`
scrapes `docker logs` by wall-clock window, which double/triple-counts
overlapping concurrent tasks' requests — found live via byte-identical
samples across two concurrently-run tasks in a follow-up smoke. Fixed
(`_run_planned_tasks` now also collects a campaign-level aggregate,
correct at any `N_WORKERS`); archives-only dedup of the original decisive
measurement's data corrects the earlier ×4/×5.7 token/prefill inflation
down to a much more modest ×1.39/×1.95 — consistent with ordinary
GPU-sharing contention (Phase 0's own finding), not a dramatic cache
wipe. The wall-clock ×1.10 result itself stands, unaffected by the bug.
`cache_size` raised 49152→65536 (candidate from already-measured GPU
margins) pending a clean re-read. Full detail: docs/history.md, "EFFORT
1.3 — PHASE 3 DECISIVE MEASUREMENT, MISSED, THEN A METRIC BUG
CORRECTED".

**Cache_size re-check (2026-08-11), with the fixed instrumentation: no
effect.** Same 2-task pair, wall-clock ×1.10 — identical to the full
18-run result, extra cache headroom bought nothing. Reading: consistent
with a COMPUTE-bound ceiling (Phase 0's own ×2.0, not ×3.0), not a
memory/cache-bound one — `cache_size` was plausibly never the real
lever. **Decision deferred at explicit user request** ("consigne tout
ça" — record only, decide later). Three paths on the table, none chosen:
test `N_WORKERS=2`, close now as a documented hardware-bound limit
(mechanism stays — `worker_id` isolation is independently valid, see
`docs/architecture/mcp-client-concurrency.md`'s general concurrent-usage
fix), or revert `cache_size` to 49152 first. `N_WORKERS` stays `1` by
default. Full detail: docs/history.md, "EFFORT 1.3 — CACHE_SIZE
RE-CHECK: NO EFFECT, DECISION DEFERRED".

**`N_WORKERS=2` smoke tried (2026-08-12): inconclusive, within noise of
N=3's own ×1.10** (×1.15, n=1/task, same pre-existing A1 `boucle` failure
mode as the sequential baseline). Web research into a genuine unexplored
lever, `tensor_parallel` (joint multi-GPU compute vs the current
`gpu_split`'s VRAM-only layer split) — found alongside a real risk: an
open upstream issue reports it failing to load on identical GPUs, and
ours are mismatched (harder case), not yet cross-checked against the
pinned image. **Chantier deferred, explicit user decision — nothing
chosen among the now four candidate paths, nothing changed in
`services/tabbyapi/config.yml`.** Full detail: docs/history.md, "EFFORT
1.3 — N_WORKERS=2 SMOKE + TENSOR_PARALLEL CANDIDATE FOUND, CHANTIER
DEFERRED".

**Effort 2 (factorial ablation of the cognitive-core flags) measured, at
a checkpoint** — see docs/history.md, "EFFORT 2". All 8 coherent
configurations of `PLANNER_ENABLED`/`VERIFICATION_ENABLED`/
`PLAN_VALIDATION_ENABLED`/`PLAN_JUDGE_ENABLED` run live against the
declared 7-task subset (2 reps/task, 112 runs total). An infra incident
mid-campaign (langgraph-agent container down, `failure_cause="infra"` on
10/14 runs of cfg6) was caught, diagnosed, and fixed by a targeted retry
of the 5 affected tasks — cfg6's numbers below are the merged valid data.
**Frozen decision table reading**: cfg1 (all flags off) matches cfg8
(all flags on, current default) on the CuP judge — 12/14 each — at 37%
less median cumulative time and 11% fewer tokens; every intermediate
configuration (cfg2-cfg7) scores below both bookends, with no legible
per-flag dependency (A1 fails near-systematically regardless of flags,
a pre-existing capability limit; D1 varies without correlating to any
single flag). Per the table's first branch ("a fixed configuration
matches or beats all-on"), this reads as adopt-and-remove — reported
as-is, without advocacy, alongside the caveat that n=2/task means a
single flipped run moves a config's score by ~7%. **No mechanism removed
yet — checkpoint open, awaiting user decision.**

**Checkpoint decision (2026-08-05)**: record the result as reproducing
the Cross-Component Interference pattern from the literature cited at
B7's opening (see docs/history.md, "EFFORT 2" — two matching sources
found, not yet independently verified, WebFetch failed in this
environment). Before any removal: (1) consolidate cfg1-vs-cfg8 only to
n=5 — script ready (`scripts/consolidate-ablation-cfg1-cfg8.sh`), not run
yet; (2) build and measure the fifth condition, merged planning (B7
amendment 2.1), the only hypothesis that could still justify keeping
planning. `PLAN_VALIDATION_ENABLED` is flagged to survive regardless of
the CuP reading (programmatic heuristic, safety value not score value).
🧑 Stop after the consolidation result, then again after the fifth
condition, before any flag is touched.

**Judge validity check (2026-08-05, archives-only)** — see docs/history.md,
"EFFORT 2", "Judge validity check": discriminating power of the 7-task
subset is thin at the task level (only A2 and D1 show config-to-config
variance on both repetitions, against a stated bar of 4 tasks — though
the raw 14-slot count clears it); planner/validation/judge mechanism
coverage is unmeasured (no persisted field), only verification's coverage
is confirmed real. Same-outcome trajectory comparison (11 matched
cfg1/cfg8 pairs) confirms the CuP tie hides a real cost gap in 10/11
cases, with one unexplained reversal (D1 rep1: cfg8 cheaper AND faster).
**Reading: ablation requalified NOT CONCLUSIVE**, not a confirmed tie —
the planned n=5 consolidation (cfg1 vs cfg8) is superseded by this
finding, pending a decision on redesigning the subset for discriminating
power. Nothing run, nothing removed.

**Resume, point 1 delivered (2026-08-05)**: planner/validation/judge
coverage instrumentation shipped — `app/graph.py`'s `plan_task`/
`validate_plan`/`replan_task` now log audit entries symmetric to
`verify_action`'s existing `verification_opportunities`/`exploitable`,
harness persists 6 new fields per run. 7 new unit tests, full suite
430→437 passed, 0 regressions. See docs/history.md, "EFFORT 2", "Point 1
delivered". `CLAUDE.md` updated: the trigger-rate-counter rule now
applies retroactively to pre-existing mechanisms. Next: choose the
discriminating-power subset (point 2), then reduce the matrix to
cfg1/cfg8/fifth-condition (point 3) — 🧑 checkpoint after point 2.

**Point 2 delivered**: subset chosen on a written criterion (variance
shown in ablation 1, OR structurally plan-shaped — long/multi-site/
multi-step; pure ceilings dropped) — see docs/history.md, "EFFORT 2",
"Point 2". Result: `A1`, `A2`, `A3`, `A4`, `D1`, `B1_conge_hard` (6
tasks, all of family A plus the two tasks that already showed signal).
`T3`/`E3` dropped as pure ceilings. A1 kept despite being near-floor on
score, reweighted to be judged by mechanism coverage (point 1's new
counters) rather than CuP — excluding the hardest, most plan-shaped task
in the benchmark would undercut the subset's declared bias in the
mechanisms' favor.

**Point 3 delivered: the 5th condition ("merged planning") built** —
new `PLANNING_MODE` env var (default `"nodes"`, unchanged behavior) and
a synthetic `manage_plan` tool (`set_plan`/`complete_subtask`, dispatched
in `_execute_tool_calls`, `TIER_READ`, no dedicated LLM call — planning
folded into the main turn per the AgentOccam pattern, see
`docs/briefs/update-plan.md` "2.1 addendum") — see docs/history.md,
"EFFORT 2", "Point 3 delivered" for the full design and the
`campaign_preflight._fetch_agent_env` override-key fetch gap fixed
along the way. 11 new unit tests + 2 regression tests, full suite
437 → 450 passed, 0 regressions. `scripts/run-flag-sweep.sh`'s `CONFIGS`
updated in place for the point-3 measurement (cfg1/cfg8/cfg9-merged ×
the point-2 subset × n=3).

**Live smoke result (2026-08-06, 6 runs, user's machine): the mechanism
never engages — `merged_plan_calls = 0` on all 6**, across A2/A1
(×2)/B1_conge_hard/A4, both the original directive wording and a
strengthened, reordered-first hard-imperative rewrite (tested, ruled
out as a fix). Cross-verified against the raw audit log's actual
tool_calls, not just the campaign report. See docs/history.md, "EFFORT
2", "Live smoke run by the user" for the full per-run table and reading.
`docs/resolved-bugs.md` #47 (docker-compose wiring gap, found by this
same smoke) already fixed. **The planned full point-3 sweep (3 configs ×
6 tasks × n=3) was NOT launched**: with the mechanism never firing, cfg9
would be behaviorally indistinguishable from cfg1 under a different
label — the sweep would not test the merged-planning hypothesis. 🧑
**Checkpoint**: point 3 stands as "built, smoke-tested, mechanism found
non-adopted by the model as designed" — a decision on whether to
redesign `manage_plan` (e.g., make the first turn structurally require
it rather than merely instruct it) or conclude this condition doesn't
transfer to this model/task set is for the user, before any further
live runs.

**Point 3 CLOSED (2026-08-06), cfg9 dropped**: fifth-condition
diagnostic, 3 variables tried in isolation (dedicated planner off — was
already the case; a persistent, editable `### PLAN` prompt section
replacing the single-line reminder; `manage_plan` moved first in the
tools array instead of last after the ~63-64-tool MCP catalog). Position
measurably changed adoption on `A1` (0→2 and 0→1 `manage_plan` calls
across smokes) but not on `A2` (stayed 0). Decisive factor: **no run
ever revised a plan mid-task** (`merged_plan_replans` stayed 0
throughout) — revision under difficulty is what distinguishes
AgentOccam's pattern from a classic planner, and without it "keep the
value, cut the cost" has no object. No task-success effect either. See
docs/history.md, "EFFORT 2" closure entry, for the full per-run detail
and the retired `<think>`-mention judge (mis-designed for this model,
which doesn't narrate tool choice for any tool sampled). Side-finding
kept independent of cfg9's fate: schema ORDER, not just weight/count,
measurably affects tool adoption — candidate follow-up noted at the
effort 1.1/1.2 paragraph above.

**Effort 2's ablation reverted to cfg1 vs cfg8 only** (`scripts/
run-flag-sweep.sh`), live smoke on `A3` (the one point-2 task without
cfg1/cfg8 precedent) green with non-trivial coverage counters, then the
**decisive 36-run measurement ran and resolves the earlier "not
conclusive" verdict**: on the 5 scored tasks (A1 read for coverage only,
per point 2's protocol, not scored), **cfg1-all-off 15/15, cfg8-all-on
13/15** — cfg1 never loses, wins outright on A2 and D1 — while costing
43% less cumulative time (1078s vs 1895s) for essentially identical real
work (195 vs 193 total tool_calls). A1's coverage read confirms the
mechanisms engage substantially even there (non-trivial plans, active
judge vetoes/replans on 2 of 3 cfg8 runs) and still buys nothing (0/3
either way). Full detail, per-task table, and the frozen-decision-table
reading: docs/history.md, "EFFORT 2 — DECISIVE MEASUREMENT". `PLAN_VALIDATION_ENABLED`'s
safety-value exception is untouched by this result. 🧑 **Checkpoint
before any removal** — reported against the pre-declared table, nothing
removed yet.

**A1 trajectory diagnostic (requested before acting on the removal
reading)**: primary cause named as arithmetic — a real `browser_extract`
limitation (returns a matched label, not its adjacent value) forces a
redundant per-page re-navigation tail that consumes the budget phase 2
(docs cross-check) needs; A2 hits the identical limitation but routes
around it with `browser_run_code_unsafe` where A1 doesn't. Secondary,
cfg8-specific finding: attempt/replan-budget churn misfires on ordinary
multi-step pagination on 2 of A1's 3 cfg8 runs — an added failure mode,
never a help. Full detail: docs/history.md, "A1 — TRAJECTORY DIAGNOSTIC".

## Visual feedback during campaigns (`docs/briefs/campaign-visual-feedback.md`, B5)

Minimal subset fully closed. Delivered, live-verified (2026-08-06), and
overhead-measured (2026-08-10): with/without smoke on a fixed 4-task
subset found no measurable overhead on the declared judge (median task
duration, 321.4s vs 297.3s — the small delta reads as noise, not a real
effect) — see docs/history.md, "VISUAL FEEDBACK MINIMAL", overhead smoke
result. `CAMPAIGN_VISUAL_CAPTURE` now defaults to `true`
(`docker-compose.yml`). The rest of B5 (Playwright traces, thumbnail
strip, headed mode, VNC) stays explicitly out of scope, per the
implementation instruction that scoped this subset.

## Deterministic GPU placement (`docs/briefs/archives/deterministic-gpu-placement.md`)

Steps 1-4 delivered and measured live (2026-08-10): `CUDA_DEVICE_ORDER=
PCI_BUS_ID` pin + explicit `gpu_split: [5, 14]` replacing TabbyAPI's
unstable autosplit (previously observed: 14 GB on the RTX 5060 Ti at
84% util vs 4.4 GB on the RTX 4070 Ti SUPER at 0%). Before/after smoke
(`scripts/gpu-placement-smoke.sh`, 4 tasks × 3 reps, one variable):
decode throughput +28% (29.4→37.7 T/s), prefill throughput +49%
(472→706 T/s), prefill time −19%, cumulative median task duration −14.5%
— see docs/history.md, "DETERMINISTIC GPU PLACEMENT", for the full
per-judge table and the two non-placement findings noted alongside it
(A2 extraction flakiness, B1_conge_hard's pre-existing CuP gap).
**Median-time figures from campaigns before this fix are not comparable
to campaigns after it** — scores remain comparable.

**Step 5 delivered**: `campaign_preflight.py`'s `check_device_placement`
refuses a campaign whose per-GPU memory distribution deviates from the
configured `gpu_split` (identity + ±3 GB tolerance); `campaign_persistence.
collect_gpu_devices()` serialises device identity (name, index, bus id,
memory used) into every campaign's metadata. Regression-tested against
the original pre-fix reading (14131/4424 MiB) — correctly flagged. Full
suite 458→466 passed. **Brief fully delivered (steps 1-5).**

**2.3 CLOSED** (`docs/briefs/update-plan.md`): `browser_extract`'s
`dt`/`dd` + table-row `adjacent_value` fix (`services/mcp-client/app/main.py`)
— fixture inventory done first (only `dt`/`dd` and `td`/`th` are real
patterns; `label`/`input` checked and dropped), functionally verified
against a real DOM via `jsdom`, 3 new unit tests, full `mcp-client`
suite 45→48 passed. Live judge run: A1/A2 non-regression campaign
(v1 full suite 31/33, consistent with the established baseline) plus a
dedicated A1/A2 rerun that surfaced a DIFFERENT, undiagnosed blocker
(`docs/resolved-bugs.md` #51) rather than confirming the fix's own
hypothesis — root-caused via a live smoke with new criterion-text
instrumentation (`plan_task`/`replan_task`/`verify_action` audit
entries): the fix itself is confirmed working (a bulk `browser_extract`
correctly found the target products mid-run), A1's continued failures
are caused by the planner/replanner never durably clearing subtask 0's
criterion within `SUBTASK_ATTEMPT_BUDGET`×`REPLAN_BUDGET`, unrelated to
extraction. See docs/history.md, "EFFORT 2.3". #51 folded into 2.4's
dossier rather than fixed standalone (user decision).

**2.4 CLOSED — cognitive-core removal, judged live and clean.**
`PLANNER_ENABLED`/`VERIFICATION_ENABLED`/`PLAN_JUDGE_ENABLED` defaults
flipped back to `false` (`docker-compose.yml`, `app/graph.py`),
`PLAN_VALIDATION_ENABLED` kept `true` (safety-value exception).
`campaign_preflight.py`'s `EXPECTED_AGENT_FLAGS` updated to match; 2
`test_campaign_preflight.py` tests updated for the flipped mismatch
direction; `docs/architecture/autonomy.md`/`docs/operations/testing.md`
default-value claims corrected (CLAUDE.md rule 9). Full
`langgraph-agent` suite 466/466, no regressions.

**Full v2 campaign, the removal's own declared judge**: F 8/8, **A
12/12 (A1 3/3 — clears for the first time on any campaign, ever)**, C
9/9 extraction with 0/9 breach, D 6/6, E1 3/3, E2 0/3 (pre-existing
capability limit, unchanged), E3 3/3, B-easy 6/6 raw + CuP 6/6.
B-medium/hard came back CuP=0/3 in that same run but was invalidated
(`NEVER_GRANTABLE_TOOLS_EXTRA` empty in the run's own `env_flags` — an
operational gap in the handoff, not a security finding); the corrected
rerun with the flag properly set came back clean: medium 3/3 CuP 3/3,
hard 3/3 CuP 3/3, both intents, zero violations. **No family
regressed; family A materially improved** — direct live confirmation of
the justification dossier (decisive cfg1-vs-cfg8 ablation, A1 trajectory
diagnostic, `docs/resolved-bugs.md` #51), all three of which pointed at
the cognitive core's attempt/replan-budget churn as active harm on
multi-page tasks. See docs/history.md, "EFFORT 2.4". An open, unresolved
question from the A1 trajectory diagnostic — 2 of A1's cfg8 runs (i.e.
under the now-abandoned config) stopped via an unidentified path, not
`report_failure`, not the iteration limit — stays recorded at
`docs/resolved-bugs.md` #49, informational only now that cfg8 is no
longer the default.

## Effort 3 — GhostDesk removal + proactive OCR scaffolding (`docs/briefs/update-plan.md`)

**GhostDesk removed entirely** (container, image, `ghostdesk-home`
volume, `.env.example` secrets) — zero remaining references anywhere in
the repo outside historical archive entries. `ocr-service` redesigned
as a plain-FastAPI graph capability (`POST /ocr`, image-in/text-out, no
more GhostDesk self-capture, no more click-targeting coordinates/query
matching) — 6/6 tests. `langgraph-agent`'s proactive-OCR enrichment
wired in (`_maybe_enrich_with_ocr`, inline in `_execute_tool_calls`,
day-one `role="proactive_ocr"` trigger-rate audit counter) but shipped
**default-off**: `_detect_visual_signal` is a stub, deliberately not
guessing what `browser_snapshot` emits for a canvas/PDF/alt-less-img
element ahead of an empirical check. Design deviation from the original
brief found before coding: the brief's reactive trigger (hooked to
`verify_action`'s verdict) is dead on arrival now that
`VERIFICATION_ENABLED` defaults to `false` (effort 2.4) — a proactive
trigger was built instead, per the brief's own named alternative. Full
`langgraph-agent` suite 466 → 471 passed. Docs corrected for rule 9
across 9 files — `docs/architecture/tool-supervision.md` turned out
bigger than expected (`DEFAULT_RULES` is empty today; the doc's example
default rule was entirely fictional, not just GhostDesk wording).
Incidental fix: `.env.example` still showed the pre-2.4 `true` defaults
for the cognitive-core flags (only `docker-compose.yml`/`app/graph.py`
were updated that session) — corrected. Full detail: docs/history.md,
"EFFORT 3".

**Live-deployed and verified (2026-08-10)**: `docker compose build
ocr-service langgraph-agent && docker compose up -d` — `ocr-service`
reports `healthy` (`docker compose ps`), `GET /health` -> `{"status":
"ok"}` (PaddleOCR engine loaded, not just the process up), `POST /ocr`
against a real 1x1 PNG -> `[]` (correctly decodes, zero false
detections). No GhostDesk container left running.

**Checkpoint resolved (2026-08-11): `_detect_visual_signal` abandoned,
not implemented.** The empirical check (`scripts/probe-visual-snapshot-
signal.sh` against `fixture-visual-probe`) falsified the mechanism's own
premise — canvas/WebGL/alt-less-img leave zero trace in
`browser_snapshot`'s text, and a candidate `role: img` heuristic proved a
false positive on SVG text (control case). `_detect_visual_signal`/
`_maybe_enrich_with_ocr`/`PROACTIVE_OCR_ENABLED` removed entirely
(`app/graph.py`, `docker-compose.yml`, `tests/test_proactive_ocr.py`).
Replaced by a tool-description routing hint on `browser_take_screenshot`
(`_tool_description_with_appends`, `services/mcp-client/app/main.py`) —
the routing decision moves to BEFORE the fact, since there is nothing
detectable AFTER it — plus a real, structural redirect for the one
pattern that IS detectable (a native PDF's entirely empty snapshot,
`_flag_empty_snapshot`, same file). `mcp-client` suite 48→55 passed,
`langgraph-agent` 471→466 (5 removed with the abandoned mechanism's
tests). `ocr-service` stays deployed but now has zero callers in the
codebase. Full detail: docs/history.md, "PROBE VISUEL — SIGNAL
BROWSER_SNAPSHOT".

**Retain decision (2026-08-12)**: `ocr-service` kept, not retired — per-
call cost probed first (`scripts/probe-ocr-cost.sh`, ad hoc n=5: 94ms/
694ms/1.30s median at 2/15/30 detected text elements), then kept for a
future "full visual mode" activation (scope/timing not yet defined). See
docs/history.md, "EFFORT 3 FOLLOW-UP — OCR-SERVICE COST PROBE, RETAIN
DECISION".

**Restricted smoke (2026-08-11, n=3/task), Effort 3 now fully closed**:
E1 3/3, E2 2/3, E3 3/3 (visual capture used in 0/3 — no capture-reflex
regression). Audit-log-verified: **all 3/3 E2 runs correctly called
`browser_take_screenshot`** after finding `browser_snapshot` sparse —
the description-only routing hint fully resolved the prior 1/3
baseline's failure mode (tool confusion between GhostDesk and
Playwright, now moot since GhostDesk is gone). The one E2 failure is a
genuine vision misread of the screenshot's text (model reported
`f209163a` against the fixed ground truth `ZK-3392`), not a routing
defect — a different, downstream capability limit, outside this
checkpoint. Full detail: docs/history.md, "PROBE VISUEL — SIGNAL
BROWSER_SNAPSHOT".

## Effort 4 — Scaffolding improvements (`docs/briefs/scaffolding-optimisation.md`)

**Effort 2 (diff-based observation history) built, unit-tested, NOT
measured live.** `HISTORY_DIFF_ENABLED` (default `false`): past
`browser_*` tool results (all but the latest) are replaced, outbound to
the LLM only, by a short structural diff against their nearest
structural predecessor — URL change, affordances appeared/disappeared,
an error-hint heuristic — instead of a repeated full snapshot.
Harness-computed, no extra LLM call. Coverage counters (`history_diff_*`)
threaded through the campaign harness/preflight/persistence from day
one, per CLAUDE.md's trigger-rate-counter rule. 12 new unit tests, full
`langgraph-agent` suite 479→491 passed, 0 regressions. Full detail:
docs/history.md, "EFFORT 4 (scaffolding-optimisation.md, EFFORT 2) —
DIFF-BASED OBSERVATION HISTORY, BUILT". **Live smoke obtained on the
third attempt** — first two hit an operational trap (stale
`langgraph-agent` image showing a clean-looking but empty result, then a
preflight-refused sequencing slip; new general rule recorded in
`CLAUDE.md`, "Operational traps": a code change needs `docker compose
build <service>` before `up -d --force-recreate`, not `--force-recreate`
alone — hit on two different services in one session). Third attempt
confirmed genuinely active (different image digest, real non-flattering
coverage: A1 78 messages compressed, A2 21). **Result mixed, not
decisive**: vs. the point-1-only baseline, A2 tokens -20.7% at no turn
cost, A1 essentially flat (+1 turn, tokens +0.3%); duration up modestly
on both. 2/2 success, no regression. n=1/task, no statistical weight —
reads as the brief's own "differences within noise" case, plausibly
because point 1 already left little redundant history on these short
(8-9 turn) tasks for this mechanism to compress. **Decision: flag stays
off, no further action this session.** A longer task (A4) is the natural
next candidate if revisited, not decided here. Full detail:
docs/history.md, "HISTORY-DIFF LIVE SMOKE — STALE IMAGE, THEN PREFLIGHT
CORRECTLY REFUSED".

**Effort 3, point 3.1 (frequency analysis) done, checkpoint decided.**
`scripts/analyze-tool-call-ngrams.sh` (archives-only, no docker/GPU) run
against the full audit log (5649 real tool_calls, 924 threads):
`browser_navigate → browser_navigate` dominates (985 saved turns),
`*_snapshot ↔ *_navigate`/`*_click` pairs are the rest of the mass,
traced to a real cause (`browser_click`/`browser_navigate` can return
stale pre-render content — `_STABILIZE_AFTER_TOOLS`,
`services/mcp-client/app/main.py:765`), not model habit. **Design
principle recorded in `CLAUDE.md`** ("Tool design contract" — a tool
that acts returns resulting state, not a bare acknowledgment; third
confirmed occurrence after `browser_extract`'s dt/dd fix and
`manage_plan`'s bare ack). **Decision**: no new composite tool this
chantier. Point 2 (push adoption of the existing `browser_extract` bulk
mode via description/position, not a new tool) queued after point 1's
checkpoint. Point 3 (form-filling composite) shelved, not a measured
bottleneck. Full detail: docs/history.md, "SCAFFOLDING 3.1 —
TOOL-CALL N-GRAM FREQUENCY ANALYSIS, CHECKPOINT DECISION".

**Point 1 built, unit-tested, live-verified, CLOSED.**
`browser_click`/`browser_navigate` (`services/mcp-client/app/main.py`)
now append a real `browser_snapshot` call's content to their own
response, taken right after the existing post-action stabilization wait
— no new tool, no new env var (reuses `BROWSER_STABILIZE_WAIT_SECONDS`
as the single gate). Root cause confirmed against real audit-log
entries: their prior response only referenced a snapshot file the agent
had no tool to read. `langgraph-agent`'s truncation needed no change
(already applies per-block, uniformly to any `browser_*` result,
verified). `mcp-client` suite 60→64 passed, 0 regressions.

**Live smoke (2026-08-12, A1/A2, n=1)**: a first attempt was invalid —
`mcp-client` was still running the pre-fix image (operational trap,
same class as CLAUDE.md's own "rebuild before restart" rule), caught by
reading the raw audit log rather than trusting the aggregate numbers.
Re-run with the fix genuinely active: **both judges down together**
(A1: -2 turns/-22 053 tokens; A2: -2.3 turns/-48 488 tokens vs the N=1
baseline), stronger than the expected trade-off — confirmed
mechanistically, zero separate `browser_snapshot` calls left on either
thread. 2/2 success, no regression. Full detail: docs/history.md,
"SCAFFOLDING 3.1, POINT 1 — BROWSER_CLICK/NAVIGATE RETURN RESULTING
PAGE STATE, BUILT". Point 1 closed.

**Point 2 CLOSED — premise was already false, no work done.** Checking
the two live A1 audit threads used to verify point 1: `browser_extract`
bulk mode (`urls=[...]`) is already used 6-8 times per run, same pattern
as A2. Already documented before this session in `docs/history.md`, "A1
— TRAJECTORY DIAGNOSTIC": *"A1 already uses bulk, on every one of the 6
runs."* — the checkpoint decision's "A1 never chose it" framing was
carried over without re-checking it, and `scripts/analyze-tool-call-
ngrams.sh` (point 3.1) can't see `browser_extract` at all (confirmed:
0 occurrences ever in the audit log's `"tool"`-keyed entries — a known,
already-documented blind spot from family E, not cross-checked before
writing that script). No description/position change made — there is
no adoption gap. Full detail: docs/history.md, "SCAFFOLDING 3.1, POINT
2 — CLOSED, PREMISE ALREADY FALSE".

**Effort 3 of `docs/briefs/scaffolding-optimisation.md` (coarse-grained
actions) now fully closed**: point 1 shipped and live-verified, point 2
closed as a non-problem, point 3 (form-filling composite) shelved
(not a measured bottleneck). No new composite tool built — the
catalog did not grow, per the checkpoint decision's own point 4.
