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
