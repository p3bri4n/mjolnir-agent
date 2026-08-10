# Campaign control — live progress + pause/resume

**Status: closed.** Both parts delivered — see docs/history.md, "B2.1 —
CAMPAIGN LIVE PROGRESS" and "B2.2 — PAUSE/RESUME + SEGMENT VALIDITY
RULES". Deviations: the literal `progress.json` schema in Part 1.1 was
extended (`planned` entries are `{task_id, repetition}` objects, not bare
task_ids; `segments`, per-run `approvals`/`fabricated_urls_count` added)
where later parts of this SAME brief needed fields it didn't literally
list. Part 1.1's instruction to make the full `campaign-<id>.json`
incremental too (not just `progress.json`) was missed in the first pass
(B2.1) and fixed retroactively in B2.2, once resume's need for it became
concrete. Part 2.4 (dashboard pause button) not built — marked optional
in the brief ("after the rest works"). Not verified end-to-end against a
real live campaign — unit-level coverage only, flagged as a follow-up.

> **Context**: v2 campaigns run 22 tasks (56+ runs, family A tasks lasting
> minutes each) — well past the point where watching a terminal is tolerable.
> Two features: a local live view, and the ability to pause a campaign,
> release the GPU, and resume later.
>
> **Design principle**: the harness *writes*, the dashboard *reads*. No HTTP
> server inside the harness, no push, no coupling. Every feature below is a
> file the harness maintains and a reader consumes — the same discipline that
> made the audit log reusable for diagnostics it was never designed for.

---

## Part 1 — Live progress

### 1.1 Progress file (harness side)

`docs/campaigns/<campaign-id>.progress.json`, rewritten atomically (write to
temp + rename) at every run boundary:

```
{ campaign_id, label, started_at, total_runs, config_digest,
  current: { task_id, repetition, thread_id, started_at },
  completed: [ { task_id, repetition, status, failure_cause, duration_s,
                 tool_calls, thread_id } ],
  paused: false }
```

Make `campaign_persistence.py` append **as it goes** rather than serialising
at the end — the per-run JSON already exists, only its timing changes. A
campaign killed mid-flight then keeps everything up to the last completed
run, which is worth having independently of this feature.

### 1.2 Step-level detail (no new plumbing)

Within a run, do **not** invent a second event stream: the audit log already
records every intention and result as JSONL, keyed by `thread_id`. The
dashboard tails the audit log filtered on `current.thread_id` and renders the
live sequence of tool calls. Existing infrastructure, zero extra coupling.

### 1.3 Dashboard page

New page in the existing `dashboard` service, polling every 2–3s (SSE only
if polling proves insufficient — polling a local file is not a bottleneck):

- header: campaign label, elapsed, runs done / total, ETA (see 1.4);
- table: one line per run, status colour-coded, failure cause, duration;
- live panel: current task, elapsed on it, expected duration for that task,
  last ~15 audit entries;
- running counters: CuP so far, per-family score, fabrications, constat
  coverage, approvals.

Read-only. Control (pause/resume) is a separate concern — see 2.4.

### 1.4 ETA — per task, never a global median

Task durations are deliberately heterogeneous in v2 (family A runs for
minutes by design, family F is short and only twice repeated, D2 depends on
a live web request). A global median of completed runs applied to the
remaining ones drifts with execution order: start with family F and the ETA
is wildly optimistic, start with A and it is the reverse.

Compute instead: **remaining time = sum, over each remaining run, of the
expected duration of *its* task.** The per-`task_id` duration cache
(`_duration_estimates.cache.json`, whose only purpose this is) supplies the
expected values; runs completed in the current campaign update them in
memory as they go.

Three requirements:

- **Range, not a point.** Display min–max (or median ± spread) per the
  project's documented run-to-run noise. An ETA to the minute would be false
  precision.
- **Cold start is stated, not hidden.** v2 tasks have no history: the first
  campaigns must display "estimate unreliable (no history for N tasks)"
  rather than a confident wrong number.
- **Pause segments are excluded** from elapsed and from the estimate — a
  campaign resumed the next day must not report a 19-hour projection.

The most useful figure in practice is not the ETA but the pair *elapsed on
the current task / expected for that task*: a run at 8 minutes on a task
that normally takes 3 is the signal to go and look.

## Part 2 — Pause / resume

### 2.1 Granularity: between runs, never mid-run

A run is atomic. Pausing mid-run would require checkpointing the browser
session, the fixture state and the agent thread coherently — cost out of all
proportion to the benefit, and a permanent source of doubt about the
resulting measurement. With family A runs at a few minutes, a graceful pause
costs at most one run's remaining time.

Pause is requested by a sentinel file (`<campaign-id>.pause`) or a `--pause`
subcommand of `run-campaign.sh` that creates it. The harness checks for it at
each run boundary, finishes the current run, writes the state, and exits 0
with a distinct exit code meaning "paused, not finished".

### 2.2 Resource release

Once paused, releasing is the operator's business, not the harness's:
`docker compose stop tabbyapi` (and optionally `playwright-mcp`,
`test-fixtures`). Document the sequence in `docs/operations/runbook.md`.
`run-campaign.sh --pause --release` may chain the two, but the harness itself
never stops a service.

### 2.3 Resume

`run-campaign.sh --resume <campaign-id>`: reads the state file, computes the
remaining runs, replays the **full preflight** (tool schema, image digests,
effective flags, fixtures reachable, TabbyAPI readiness), then continues.

### 2.4 Control from the dashboard (optional, after the rest works)

A pause button that only creates the sentinel file — the dashboard never
talks to the harness directly. Resume stays a command line action, since it
requires restarting services.

## Part 3 — Validity rules (the part that matters)

A paused-and-resumed campaign is **not** a continuous campaign. Restarting
TabbyAPI empties the prefix cache and reloads the model; runs after the
resume start from a cold cache. Without the rules below, pause/resume becomes
a silent contamination of exactly the kind this project has already paid for
(shared `thread_id`s, leftover browser session, unreachable fixtures).

1. **Segment marking.** The state file records every pause/resume boundary
   with its timestamp. Each run carries a `segment` index. The campaign
   report shows segment boundaries.
2. **Cache-sensitive metrics are reported per segment**, never pooled:
   `prefill_total`, `cache_zero_rate`, and any tokens/second figure. The
   first runs of a segment are cold-cache by construction — pooling them
   with warm runs would produce the same kind of artefact as the invalid
   14/33 campaign.
3. **Resume refuses on configuration drift.** If the preflight finds a
   different image digest, a different effective flag set, or a different
   git commit than the ones recorded at campaign start, resume is **refused**
   with a diff. A campaign whose second half measured a different agent is
   void — better to restart it than to publish it.
4. **Score metrics stay poolable** (CuP, task success, failure causes): they
   do not depend on cache state. Say so explicitly in the report, so nobody
   later assumes the whole campaign is suspect.
5. **Staleness guard.** A resume more than N days after the pause (env,
   default 7) prints a warning: real sites and live ground truths (D2's
   staleness probe) may have moved. Not a refusal, a warning to record.

## Tests and judges

- Unit: state serialisation, run-remaining computation, sentinel detection at
  boundary, segment numbering, resume refusal on digest/flag drift, ETA
  computed per task (a fixture with two tasks of very different durations
  must not produce a global-median estimate), pause time excluded from
  elapsed and ETA.
- Integration: launch a 4-run smoke, pause after run 2, stop `tabbyapi`,
  restart, resume — final report must contain 4 runs, 2 segments, and
  per-segment cache metrics.
- Non-regression: a campaign run without pausing produces a report identical
  in shape to today's (single segment).

## Out of scope

- Mid-run pause.
- Parallel run execution (a separate subject: inference is the bottleneck,
  expected gain modest, and it complicates the isolation guarantees).
- The harness controlling Docker services.
