# Visual feedback during campaigns — brief

> **Context**: v2 campaigns run unattended for over an hour. The control
> dashboard shows counters and audit entries; it shows nothing of what the
> agent is *looking at*. This adds a visual channel to the live view and a
> post-mortem replay for diagnosis.
>
> **Companion to** `campaign-control` (live progress, pause/resume). Same
> principle: **the harness writes, the dashboard reads**. No HTTP server in
> the harness, no push.
>
> **Non-negotiable constraint**: observability must not alter the measured
> system. Every mechanism below is a side channel — nothing it produces ever
> enters the model's context.

---

## 1 — The problem the obvious solution does not solve

The agent is DOM-first by routing policy: on most web tasks it never takes a
screenshot. Displaying "the last capture the agent took" would therefore
leave the panel empty for entire runs, and biased towards the rare turns
where the agent chose to look.

So the capture must be **harness-side and systematic**, independent of the
agent's own tool calls.

## 2 — Side-channel capture

1. After each browser action (in the `mcp-client` browser wrapper, on the
   result path), take a viewport screenshot through the same Playwright
   session — JPEG, quality ~60, viewport only, no full-page scroll.
2. Write it to `docs/campaigns/artifacts/<campaign-id>/<thread-id>/
   <seq>-<tool>.jpg`. Never base64 in the progress file, never in the audit
   JSONL: paths only.
3. **This image is never returned to the agent** and never appended to any
   message list. Add a unit test asserting that the observability capture
   path cannot reach the model context — this is the one thing that would
   silently invalidate every campaign.

## 3 — Cost, measured and recorded

A capture per action costs latency, and median time per task is a permanent
judge. Therefore:

- The feature is behind a flag (`CAMPAIGN_VISUAL_CAPTURE`, default off).
- Its overhead is **measured once** (a smoke run with and without, same
  fixtures) and recorded in `docs/operations/campaigns.md`.
- The flag's effective value goes into the campaign metadata JSON, like every
  other behaviour flag. A campaign compared against another must have the
  same value — a report showing different values across compared campaigns
  must say so.
- If the overhead proves material, fall back to capture on **failure only**
  (buffer the last N captures in memory, flush to disk when a run is
  classified as failed): full diagnostic value, near-zero cost on successes.

## 4 — Dashboard live panel

In the campaign page:

- the latest capture for the current run, refreshed with the existing 2–3 s
  poll;
- a thumbnail strip of the last ~8 captures, each labelled with its tool and
  timestamp — the visual counterpart of the audit entries already displayed;
- clicking a thumbnail opens the full image;
- when a run ends, its strip stays visible until the next run starts.

Read-only, served from the artifacts directory. No new transport.

## 5 — Post-mortem: Playwright traces

For diagnosis rather than live viewing, enable Playwright tracing
(`screenshots=True, snapshots=True`) per run. It produces a replayable
timeline with, for each action, a screenshot, the DOM state, and the network
— in headless mode, altering nothing. This answers the question that has cost
this project the most hours: *what was the agent looking at when it clicked
there.*

Retention: keep traces for failed runs, purge successes at campaign end.
Same flag family as above, same metadata recording.

**Do not enable headed mode** to "watch it live". Headed changes rendering,
timings, anti-bot detection and resource use — it changes the measured
system, and every prior campaign becomes non-comparable. If a live desktop
view is ever wanted, it is a demo tool, not a measurement tool.

## 6 — Retention and sensitive content

Screenshots and traces contain the content of every page visited. They fall
under the same retention and redaction policy as the audit log (security
plan, Phase 1). Decide before the volume grows:

- artifacts purged for successful runs at campaign end;
- failed-run artifacts kept for N campaigns (env, default 3), then purged;
- the artifacts directory is git-ignored.

## Tests and judges

- Unit: the observability capture never appears in any message list passed
  to the model (the critical test); path construction; retention purge.
- Integration: a 2-run smoke with the flag on produces one artifact directory
  per run, populated, and the dashboard page renders the latest image.
- Judge: with the flag **off**, campaign results are byte-for-byte comparable
  to the previous campaign — the feature must be invisible when disabled.

## Status: minimal subset implemented (2026-08-06)

Everything above stays the target shape of B5 in full. What actually
shipped is deliberately smaller — explicit instruction: "sous-ensemble de
B5, tout le reste du brief reste hors périmètre (traces Playwright, bande
de vignettes, mode headed, VNC)."

**Delivered**: §2.1/2.3 (harness-side capture, the critical
never-in-context test) and §4's live panel (latest capture only, no
thumbnail strip), §3's flag/measurement discipline (flag default pending
the with/without smoke — not run from this sandbox, no Docker/GPU access).
**Explicitly not built**: §2.2's per-action `<seq>-<tool>.jpg` history (one
file, overwritten, per §"UN SEUL FICHIER" — no history means retention/purge
(§6) has no object: the footprint is one small JPEG per thread ever run,
never growing per-action), the thumbnail strip, Playwright traces (§5),
headed mode, VNC.

**Two deviations from this file's literal text, made for concrete
architectural reasons found while implementing — not oversights:**

1. **Keyed by `thread_id`, not `campaign_id`.** Verified against the
   running code (not assumed): `campaign_id` is a harness/dashboard-only
   concept (`campaign_persistence.py`, generated client-side by the test
   runner) — it is never sent to `langgraph-agent`'s graph nor to
   `mcp-client`; grepping `services/mcp-client` for either `campaign_id` or
   `thread_id` returns nothing today. `thread_id`, by contrast, already
   flows end-to-end (`_execute_tool_calls`/`run_slash_command_direct` in
   `app/graph.py` both have it in scope at the exact call site that reaches
   `mcp-client`). Threading `campaign_id` through 3 services for a
   side-channel feature would be the bigger change the "petit chantier"
   framing (point 2 of the implementation instruction) explicitly wants to
   avoid. The dashboard already resolves campaign → in-flight thread_id via
   `state["current"]["thread_id"]` (`campaign_persistence.py`, written
   BEFORE `run_task()` starts specifically "so the progress file can name
   the in-flight thread_id for the dashboard to tail", B2 Part 1.2) — reused
   as-is, no new plumbing needed on that side. Net effect for the user: the
   campaign page still shows "the current run's latest capture" exactly as
   specified; the file on disk is named by `thread_id`, not `campaign_id`.
   Revisit if effort 1.3 (parallel run execution) ever lands: today
   campaigns run one task at a time, so `thread_id` disambiguates
   perfectly; concurrent campaigns sharing one `mcp-client` browser session
   would need `campaign_id` (or a real per-campaign session) regardless of
   this feature.
2. **Written to `./workspace/visual-capture/`, not
   `docs/campaigns/artifacts/`.** `docs/campaigns/` is a fully git-tracked
   archive (confirmed: 223 tracked files, including the live-updated
   `*.progress.json`) — not a place for gitignored runtime output.
   `./workspace/` is already a shared, git-ignored, writable volume between
   `langgraph-agent` and `mcp-client` (`docker-compose.yml`), and its
   existing `.gitignore` pattern (`workspace/*`) already covers a new
   `workspace/visual-capture/` subdirectory — no new `.gitignore` line
   needed (verified via `git check-ignore`), simpler than adding one.

**Retention (§6) not built, matching `.audit`'s own current state, not a
gap specific to this feature**: the audit log has no retention/redaction
either — `docs/briefs/update-plan.md`, Effort 5/B6 explicitly defers "the
audit log's own retention and redaction" as future security work. This
feature's directory should get the identical policy when that lands, not
before — inventing one now for `latest.jpg` while `.audit` has none would
be inconsistent, and per point 2 above the single-overwritten-file design
makes it a non-issue in practice regardless (bounded by "number of
distinct threads ever run", not by campaign duration).

Full implementation detail, deviations, and the with/without measurement
handoff: `docs/history.md`, "VISUAL FEEDBACK MINIMAL".
