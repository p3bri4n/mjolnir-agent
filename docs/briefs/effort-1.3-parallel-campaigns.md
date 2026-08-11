# Effort 1.3 — parallel campaign execution

Status: brief only, zero code written. Resumes `docs/briefs/update-plan.md`
effort 1.3, deferred at the 2026-08-05 archives-only recompute (see
docs/history.md, "EFFORT 1.3") pending efforts 1.2/2/4.2 landing and a
fresh median-duration re-measurement. 1.2 and 2 are delivered (2.4:
cognitive-core removal); 4.2 is not started. Re-measured anyway below —
the composition of campaign time has changed enough on its own to be
worth re-checking now, ahead of 4.2.

## Why now, re-measured (archives only, zero runs)

The original estimate (×2.2 pessimistic / ×3 optimistic) was built on a
pre-GPU-placement, pre-2.4 33-task campaign where GPU-bound (prefill) and
I/O-bound (tool round-trips) time were roughly 50/50 (22.9s/22s per
task). Two things have changed since, both independently: deterministic
GPU placement (`docs/history.md`, "DETERMINISTIC GPU PLACEMENT" —
prefill throughput +49%) and the cognitive-core removal (effort 2.4 —
cut auxiliary planner/judge LLM calls, which were GPU-bound). Recomputed
from the most recent full-family v2 campaign
(`docs/campaigns/campaign-20260810T142745Z-benchmark-v2.json`, 62 runs,
post-GPU-placement, post-2.4):

- total `duration_seconds`: 2017.8s
- total `prefill_seconds`: 526.2s
- **GPU fraction of run time: 26%**, down from ~51% in the original
  estimate.

Applying the same pessimistic/optimistic model as the original estimate
(pessimistic: GPU time stays fully serial regardless of worker count,
I/O time divides by N; optimistic: near-linear on TabbyAPI's own
concurrent batching) to these numbers, N=3 workers:

- pessimistic: `526.2 + (2017.8-526.2)/3 ≈ 1023s` → **×1.97**
- optimistic: `2017.8/3 ≈ 673s` → **×3.0**

Directionally the same conclusion as the original estimate (worth doing),
with the pessimistic floor slightly lower in this recompute (workload is
now more I/O-dominated, so serializing the now-smaller GPU-bound share
matters proportionally less, but I/O — the bulk of the time — is exactly
what parallelizes). Both estimates share the same unconfirmed variable as
the original brief: **TabbyAPI's actual concurrent-batching behavior
under real parallel load has never been measured live** — the
pessimistic/optimistic split brackets it, it does not resolve it. Phase 0
below measures it directly instead of estimating it a second time.

## Architecture question resolved by an existing verified finding

The original brief's "preferred fix" (scope `mcp-client`'s
`_persistent_sessions` by a caller `worker_id` rather than standing up N
full container sets) rests on one assumption: that N concurrent MCP
sessions to the SAME `playwright-mcp` server give N genuinely isolated
browser contexts, not one shared browser fought over by N sessions. This
is not a new question — it was already answered, verified against the
installed image, while fixing the original ephemeral-session bug
(`docs/resolved-bugs.md`, session-continuity entry): *"Playwright MCP
scope son contexte navigateur (page, cookies, historique) à la session,
pas au process"* — i.e. **context is per-MCP-session already**, which is
exactly the unit `worker_id`-scoping would create N of. The preferred fix
is architecturally sound, not just resource-cheaper than N containers —
confirmed against already-verified behavior (CLAUDE.md #8), not a new
guess.

## What needs to change

**1. `mcp-client` (`services/mcp-client/app/main.py`)**

- `_persistent_sessions`/`_persistent_locks`: currently keyed by
  `server_name` alone (only `"browser"` has `persistent_session: True`
  today). Rekey by `(server_name, worker_id)`. `worker_id` absent
  (existing single-caller usage — interactive Open WebUI, non-parallel
  campaigns) must keep today's behavior identically: a fixed sentinel
  (e.g. `worker_id or "default"`) preserves the current single-shared-
  session behavior with zero change for every caller that doesn't opt in.
- `POST /reset-session/{server_name}` gains an optional `worker_id`
  (query param, matching `CallRequest`'s existing optional `thread_id`
  precedent): resets only that worker's session, leaving the others
  untouched. Without this, one worker's cross-task reset would blow away
  every other worker's live browser state mid-run.
- `CallRequest` gains an optional `worker_id`, threaded through
  `_run_on_server` the same way `thread_id` already is for
  `_maybe_capture_visual`.

**2. Downloads volume (`agent-downloads`, shared bind mount) — RESOLVED
by config inspection, no live run needed.** `docker-compose.yml`'s
`playwright-mcp` service sets `--output-dir=/downloads` as a container-
launch CLI flag, not a per-MCP-session parameter — unlike the browser
CONTEXT (isolated per session, point 1 above), the download DIRECTORY is
one shared path for the whole process, with no per-session override
exposed by the image. Option (a) from the original open question (a
per-`worker_id` subdirectory) is therefore not available without N
separate `playwright-mcp` containers, which the brief already rejects as
heavier than worker-scoping. Option (b) (snapshot-diff purge) has its own
real hazard: if the SAME task_id (e.g. T5, the only download-touching
task today) ever lands on two workers in the same round, both would
write to the SAME filename in the SAME shared directory regardless of
purge timing — a content collision, not just a purge race.

**Chosen design: serialize only the download-touching task(s) across
workers, at the harness level (Phase 2), not the volume.** A single
`threading.Lock` held for the duration of any task tagged
download-touching (currently only T5) turns that narrow slice back to
sequential while every other task stays parallel — `_purge_downloads_volume`
stays exactly as it is today (no snapshot-diff, no worker-scoping),
correct by construction since only one worker is ever inside that
critical section. Cheaper and more robust than trying to make a
single shared directory safe under real concurrent writers. Moves this
point out of Phase 1's scope entirely — `mcp-client` needs no downloads-
related change.

**3. Test harness (`tests_integration/test_web_tasks.py`)** — the
sequential `for entry in remaining:` loop (`_run_campaign`) becomes an
N-worker pool. `run_task`/`_chat`/`_approve` are synchronous
(`requests`/`subprocess`-based, not `asyncio`) — a `ThreadPoolExecutor`
fits without a rewrite to `asyncio`. Each worker needs its own
`worker_id` threaded into `_purge_downloads_volume`/`_reset_browser_session`
(both gain a `worker_id` parameter, forwarded to mcp-client per point 1)
and into every `_chat`/`_approve` call so `app/main.py` can derive a
worker-scoped `thread_id` distinct from other workers' concurrent tasks
(already unique per repetition via the existing `uuid.uuid4()` marker —
confirm this stays sufficient under concurrent submission, not just
sequential).

**4. Pause/resume/segment tracking (B2,
`docs/briefs/archive/A6-campaign-control.md`) must survive N workers.**
`remaining = state["planned"][len(state["completed"]):]` is a strict
ordered-slice cursor — correct only when completions happen in launch
order, which N concurrent workers break immediately (worker B can finish
entry 5 before worker A finishes entry 3). Needs converting to a set
difference (`planned` items not yet in `completed`, matched by
`(task_id, repetition)` rather than position) before any parallel launch
— **this fix is a prerequisite of parallelizing at all**, not an
enhancement: without it, a pause mid-campaign would resume from the wrong
cursor and either replay completed work or skip pending work depending on
which worker happened to finish last. The pause sentinel itself (checked
once per loop iteration today) needs a check per worker, all workers
draining cleanly before the campaign reports `paused=true`.

**5. `_tools_schema_cache` (`app/graph.py`) — noted, not in scope here.**
Named in the original effort 1.3 archives note as the same defect family
(unscoped global state), but it is a same-VALUE cache (the tool schema
langgraph-agent sees is identical regardless of caller), not per-caller
state — parallel workers sharing it is correct, not a contamination risk.
Its real defect is staleness after an independent `mcp-client` restart,
unrelated to parallelism. Left out of this chantier's scope; revisit
separately if it ever causes a real incident.

## Sequencing

**Phase 0 — measure before building (live, on the user's machine, this
sandbox cannot run Docker/GPU).** Two things this brief cannot resolve on
archives alone:
- TabbyAPI's actual concurrent-request behavior: fire 3 concurrent
  `/v1/chat/completions` requests against the real server, compare
  latency to 3 sequential — confirms or corrects the pessimistic/
  optimistic bracket above with a real number instead of two guesses.
- `playwright-mcp` session isolation under real concurrent load: **must
  bypass `mcp-client`** — its own `_persistent_sessions` is still keyed
  by `server_name` alone today (Phase 1 hasn't landed), so two calls
  through `mcp-client`'s `/call` would reuse the SAME shared session and
  test nothing new. Open 2 independent MCP client sessions DIRECTLY
  against `playwright-mcp`'s Streamable HTTP endpoint
  (`http://playwright-mcp:8931/mcp`, the same `mcp` Python package
  `mcp-client` itself already depends on — no new code, a throwaway
  script), each navigating to a DIFFERENT URL, confirm both
  `browser_snapshot`s show their own page, not a shared/overwritten one.
  Verifies the architecture argument above empirically, not just by
  reading a resolved-bugs entry.

🧑 **Checkpoint after Phase 0** — both checks gate whether Phase 1 is
worth building at all.

**Phase 0 result (2026-08-11, user's machine,
`scripts/probe-parallel-phase0.sh`), both checks green:**
- TabbyAPI concurrent-request behavior: 3 sequential distinct prompts
  2.79s, 3 concurrent distinct prompts 1.40s → **×2.0 real speedup**,
  landing on the pessimistic bracket (×1.97) rather than the optimistic
  one (×3.0) — TabbyAPI serializes more of the work than the optimistic
  scenario assumed. First attempt (identical prompt repeated) was
  invalid: 0.38s for 3 sequential requests was a prefix-cache artifact
  (this project already tracks `cache_zero_rate` as a real phenomenon),
  fixed by using 3 distinct, UUID-prefixed prompts with ~150 words of
  filler per arm, sequential and concurrent arms never sharing a prompt.
- `playwright-mcp` session isolation: confirmed — two independent MCP
  sessions opened directly against `playwright-mcp` each kept their own
  navigated page, no cross-talk.
- **Reading**: Phase 3's realistic target is closer to **×2** on the full
  campaign than the optimistic ×3 — still a real, worthwhile win (roughly
  halves campaign wall time), set as the expectation for Phase 3's
  threshold rather than the more optimistic number. Phase 1 is confirmed
  worth building.

**Phase 1 — `mcp-client` worker-scoping** (point 1 above only — point 2's
downloads question is resolved above and moves to Phase 2, no
`mcp-client` change needed for it). Unit-tested the same way the
existing session-persistence tests are (`tests/test_main.py`),
default-caller (`worker_id` absent) behavior covered by a regression test
proving zero change for every existing caller.

**Phase 1 delivered**: `_persistent_sessions`/`_persistent_locks` rekeyed
`(server_name, worker_id)` (`_persistent_locks` now a lazily-populated
`defaultdict`, safe without an extra guard lock — single-process uvicorn,
no `await` inside `defaultdict.__missing__`); `_worker_key` normalizes a
missing/empty `worker_id` to the same `"default"` bucket every existing
caller has always used. `POST /reset-session/{server_name}` gained an
optional `worker_id` query param; `CallRequest` gained an optional
`worker_id`, threaded through every `_run_on_server`/`_maybe_capture_visual`
call site in `call_tool`. Caught while fixing the tests: two existing
assertions (`"browser" not in _persistent_sessions`,
`_persistent_locks["browser"] = asyncio.Lock()`) referenced the OLD
bare-string key — the first would have silently become a vacuous pass
after this change (never matching any real key again) rather than a
loud failure; both fixed, one turned into a real worker-isolation
regression test. 5 new tests, `mcp-client` suite 55→60 passed, 0
regressions in `langgraph-agent` (466/466, untouched by this phase).

**Phase 2 — harness N-worker runner** (points 2-4: the download-task
serialization lock, the N-worker pool, and the pause/resume cursor fix).
Pause/resume correctness (point 4) is testable without live Docker — the
cursor logic is pure data manipulation, unit-testable against a
synthetic `planned`/`completed` state. Same for the download-lock logic
(point 2) — a synthetic task list with an interleaved download-touching
entry is enough to prove serialization without live Docker.

**Gap found and closed before Phase 2 could start: `worker_id` had
nowhere to travel from the harness to `mcp-client`.** Phase 1 gave
`mcp-client` the parameter; nothing on `langgraph-agent`'s side read it
from an HTTP request. `ChatCompletionRequest`/`ApprovalDecisionRequest`
gained an optional `worker_id` (absent for every real client — Open WebUI
never sends it), forwarded into `config["configurable"]` by
`_resolve_run`/`/approve`, extracted there by
`_execute_tool_calls`/`run_slash_command_direct`, passed through
`_call_mcp_tool`. Planner/verification nodes left unscoped (cognitive-
core flags default off since effort 2.4, the config parallel campaigns
actually run under). 4 tests fixed (exact-match `/call` payload
assertions now include `"worker_id": None`), 4 new (forwarding through
`_call_mcp_tool` directly, the non-streaming endpoint, and `/approve`'s
resume path). `langgraph-agent` suite 466→469 passed.

**Phase 2 scope grew mid-implementation, reported at the checkpoint
before continuing (user: "continuer maintenant, périmètre élargi"):**
the pause/resume cursor (`planned[len(completed):]`) turned out to be
`campaign_persistence.py`'s own documented contract
(`init_progress_state`'s docstring), also depended on by
`compute_remaining_eta()` (the dashboard's live ETA) — fixing it for real
meant `remaining_runs()` (a set difference on `(task_id, repetition)`,
safe under out-of-order completions) landing in `campaign_persistence.py`
itself, PLUS its deliberately-duplicated mirror in
`services/dashboard/app/main.py` (`_remaining_runs`, same "harness
writes, dashboard reads" decoupling as `_normalize_duration_estimate`).
7 tests across both (4 + 3), both suites green.

**Phase 2 delivered.** `_run_planned_tasks` (`test_web_tasks.py`) is the
shared N-worker loop both `_run_campaign` (v1) and `_run_campaign_v2`
call, parameterized by a `build_row` callback (each suite's own row
fields) and `purge_fns`/`serialized_task_ids` (which shared fixtures need
exclusive access — v1: `_purge_downloads_volume`/T5 only; **v2 also needs
`_purge_admin_stock_file`/`FAMILY_B_BETA_TASK_IDS`**, `stock_updates.json`
turned out to be the exact same shared-single-file hazard as T5's
`/downloads`, found while porting the loop, not anticipated in this
brief's original point 2). `n_workers=1` (`WEB_TASKS_WORKERS`, default)
passes `worker_id=None` throughout — verified as a real, separate
invariant (a first draft always generated `"worker-1"` even at
`n_workers=1`, caught by its own regression test, fixed). `state["current"]`
kept as a single dict (dashboard `campaign.html` untouched) — "whichever
run was claimed most recently," a documented degradation for
`n_workers>1` (shows one of the active runs, not all); `state["active"]`
(new) carries the full in-flight list for a future dashboard enhancement,
explicitly out of scope here. 5 new tests
(`tests/test_run_planned_tasks.py`, no Docker/HTTP), including a real-
threading proof that the download lock blocks another worker's purge
until the serialized task's ENTIRE run finishes, not just its own purge.
`langgraph-agent` suite 469→478 passed overall.

🧑 **Checkpoint before Phase 3's live measurement** — nothing live-run
yet in this phase; everything above is unit-tested against synthetic
state only, per the brief's own discipline.

**Phase 3 — measurement.** One parallel campaign (N=3, the same declared
subset already used for effort 2's decisive measurement — a subset
already trusted for discriminating power) vs. its sequential equivalent,
same tasks. Judges, declared now:
- **primary**: wall-clock campaign duration — the entire point of this
  chantier, threshold not frozen yet (fill in after Phase 0's live
  numbers replace the estimate above).
- **secondary, veto power**: score/CuP non-regression. Per the standing
  decision-table convention, any score regression invalidates the win
  regardless of speed — concurrency bugs (a stolen browser tab, a
  cross-worker download collision) would show up here first.

🧑 **Checkpoint before Phase 3's live measurement**, same discipline as
every other effort in this plan.

## Risks flagged, not resolved here

- The download-serialization lock (point 2) only knows about T5 today —
  if a future task also touches downloads, it must be added to the
  tagged set explicitly; nothing detects this automatically.
- `AUTO_APPROVAL_STREAK_LIMIT`/session grants
  (`app/approval_policy.py`) are per-`thread_id`, already independent per
  task — no new risk expected here, but not explicitly re-verified for
  this brief; worth a glance in Phase 2 given it's adjacent state.
