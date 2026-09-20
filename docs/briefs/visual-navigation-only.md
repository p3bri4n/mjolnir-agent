# Visual-only navigation mode — brief

> **Scope**: build and measure `VISUAL_NAVIGATION_ONLY`, a mode where
> perception is exclusively visual — capture + OCR only, no DOM snapshot,
> no accessibility tree — with a matching coordinate-based action space.
> Kept separate from effort 3 deliberately: effort 3 redistributed an
> existing capability (`browser_take_screenshot` routing), this creates a
> whole new operating mode with its own action space. Bundling the two
> would repeat the two-variables-in-one-iteration mistake this project
> already paid for once (phase 1b).
>
> **Why it's worth doing**: family E only ever tested isolated visual
> cases (screenshot routing, one visual-only task, capture-reflex
> frequency) — never what the visual channel is worth as the ONLY
> channel, across the full suite. This is the number few projects
> publish, and the closer answer to the question E4 (native dialog,
> explicitly out of scope) was going to ask.
>
> **Single variable per measurement** (`docs/methodology.md`). Sequenced
> WITHOUT effort 1 (cheap parallel campaigns) as a hard prerequisite —
> explicit user decision (2026-09-20): the full-suite measurement (Phase
> 4) runs sequentially at the current `N_WORKERS=1` default. Slower, not
> blocked in principle.

---

## Phase 0 — Close the coordinate gap in `ocr-service` (nothing measured yet)

`PaddleOCREngine.run()` (`services/ocr-service/app/ocr_engine.py`)
already computes `x`/`y`/`width`/`height` per detection. `POST /ocr`
(`services/ocr-service/app/main.py`) discards them, returning only
`text`/`confidence`. Without coordinates there is no action space for
this mode — visual-only interaction needs something to click.

1. Add `x`/`y`/`width`/`height` to the `/ocr` response.
2. Update `services/ocr-service/tests/test_main.py` and
   `docs/operations/testing.md`'s test inventory (which currently
   documents "no coordinates" as the contract) to match the new shape.
3. No agent-facing change yet — this only fixes the service's own output.

🧑 Checkpoint: response shape reviewed before anything calls it.

## Phase 1 — Design the mode and its action space

1. **New env var** `VISUAL_NAVIGATION_ONLY` (default `false`, unchanged
   behavior — same convention as `HISTORY_DIFF_ENABLED`/
   `REASONING_EFFORT`).
2. **No cheating — the mode's defining constraint, stated explicitly**:
   nothing the model receives or can call may expose information a
   sighted human looking only at the rendered screenshot would not have.
   Perception is `browser_take_screenshot` routed through `ocr-service`
   (text + bounding box), nothing else. When active, this mode must make
   genuinely UNAVAILABLE (not just discourage via a description) every
   tool that reads the DOM/accessibility tree or executes/inspects page
   internals: `browser_snapshot`, `browser_extract`, `browser_evaluate`,
   `browser_inspect` at minimum — enumerate the FULL current tool catalog
   against this criterion at design time, don't assume this list is
   exhaustive (`playwright-mcp`'s schema may expose others; verify against
   the installed catalog, CLAUDE.md rule 8, don't work from memory of
   what the catalog contained last time it was audited).
3. **Action space**: interaction tools need a coordinate-based
   counterpart (click/type at `x,y` rather than by `ref=`) — the one
   capability a human-at-a-screenshot legitimately has that pure OCR
   output doesn't provide for free. Verify what `playwright-mcp`'s
   installed schema already exposes for coordinate-based mouse actions
   before assuming a new tool is needed. Existing selector-based tools
   (`browser_click`, `browser_navigate`'s ref-targeted variants, etc.)
   must be unavailable in this mode, same hard-gate treatment as point 2
   — offering both action spaces at once would test "visual with a DOM
   safety net", not "visual-only".
4. **Guardrails carried over, not rebuilt**: URL-fabrication guardrail,
   approval tiers, `NEVER_GRANTABLE_TOOLS`/`NEVER_GRANTABLE_TOOLS_EXTRA`
   — this mode changes perception and action granularity, not the
   security model. Confirm each still applies (tier is assigned by
   action nature, not by which tool triggered it).
5. **Truncation**: `AFFORDANCE_THRESHOLD` and its neighbours were tuned
   for DOM snapshot text; an OCR detection list is a different shape
   (short strings + four numbers each) — check whether existing
   truncation logic even applies here, don't assume it transfers.

🧑 Checkpoint: design reviewed before any code — especially the
tool-availability mechanism. An env-gated hard removal from the tool
schema, not a description-only "don't use these" instruction: the
softer form already failed once for `manage_plan`'s adoption (see
`docs/engineering-log.md`, "EFFORT 2" closure) — reuse that lesson here
rather than re-learning it.

## Phase 2 — Build, unit-tested

Implementation of Phase 1's design. No live measurement in this phase.

Judge: existing suites stay green with the flag off (default, unchanged
behavior) — this phase's only judge, same bar every prior flag
introduction in this project was held to.

🧑 Checkpoint.

## Phase 3 — Live smoke (single task, n=1-3, before any full measurement)

Per `CLAUDE.md`'s measurement rules ("a live smoke precedes any final
measurement"): one or two cheap tasks, to catch the operational traps
that have caught every mechanism shipped so far before they reached a
real campaign (stale image, missing route, a judge blind to what it
needed to read — this project's own five-smoke track record).

Candidate: a short, page-observable task (family F or a short family-A
task) — not A1's multi-page complexity, this phase is about catching
plumbing bugs, not measuring anything yet.

Judge: the mode actually engages (a coverage counter from day one, per
the trigger-rate rule — e.g. `visual_navigation_only_active_count`,
logged on every relevant call regardless of outcome), and the agent
completes at least one full turn without crashing on the new action
space.

🧑 Checkpoint: smoke result reviewed before the full-suite run below.

## Phase 4 — Full v2 measurement (single variable: `VISUAL_NAVIGATION_ONLY`)

Runs sequentially at the current `N_WORKERS=1` default — no dependency
on effort 1. Estimate and report total runtime before launching, same
discipline as every prior full-suite campaign.

**Judges, declared before running**:
- CuP and per-family score vs. the current DOM-mode baseline (the last
  full v2 campaign under this same model/config) — the headline number
  this effort exists to produce.
- Tokens per task, median time per task — screenshot+OCR payloads have a
  different cost shape than DOM snapshots (per-page image cost vs.
  per-page text cost); report both directions honestly, no assumption of
  which wins.
- `context_overflow` rate — a different failure mode could emerge if OCR
  detection lists turn out more verbose than expected on busy pages.
- Phase 3's coverage counter, read across the whole campaign — confirms
  the mode was active for the entire run it's being judged on, not a
  flattering zero.

**Decision table**:

| Result | Reading |
|---|---|
| Visual-only score comparable to DOM mode | The two channels are substitutable for this task suite — publishable finding; stays a diagnostic mode, adoption as a default is not forced by this result |
| Visual-only score clearly lower | Names the DOM channel's actual value in concrete terms — the figure E4 was going to ask for. Also publishable, closes the question without further exploration |
| Visual-only score clearly higher | Surprising — re-check for a measurement artifact (a task whose success criteria happen to be OCR-friendly) before reading it as a real result |

🧑 Checkpoint: report before any follow-up decision.

## Out of scope

- Coordinate-based grounding via a dedicated model (OmniParser/GPU
  grounding) — explicitly deferred in `PLAN.md` ("motivated by OBSERVED
  failures", not assumed needed here); this brief uses `ocr-service`'s
  own bounding boxes only.
- Making `VISUAL_NAVIGATION_ONLY` a production default — this is a
  measurement effort, not a capability rollout.
- Retrofitting GhostDesk's old coordinate-click design — GhostDesk is
  removed; this reuses Playwright's own coordinate-based mouse actions.
