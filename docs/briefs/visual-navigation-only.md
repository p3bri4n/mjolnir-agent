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

## Phase 1 — Design the mode and its action space (CLOSED, decisions below)

Verified against the real `@playwright/mcp` catalog (npm `0.0.82`, the
package behind `mcp/playwright:latest` — cross-check against the actual
running container's version at Phase 2 build time, this was read from
the published package, not `docker exec`) rather than assumed from
memory. Full inventory: `npm view @playwright/mcp` /
`npm pack @playwright/mcp` and its bundled `README.md`.

1. **New env var** `VISUAL_NAVIGATION_ONLY` (default `false`, unchanged
   behavior — same convention as `HISTORY_DIFF_ENABLED`/
   `REASONING_EFFORT`). Set on BOTH `langgraph-agent` (schema filtering,
   point 3) and `mcp-client` (point 4's stabilization fix) — a whole
   operating mode, not a per-thread/per-request toggle, so a static env
   var on both containers is the right shape; no new field threaded
   through `CallRequest`/`ChatCompletionRequest` the way `worker_id` was.
2. **No cheating — the mode's defining constraint**: nothing the model
   receives or can call may expose information a sighted human looking
   only at the rendered screenshot would not have. Perception is
   `browser_take_screenshot` routed through `ocr-service` (text +
   bounding box), nothing else.
3. **Tool-availability mechanism**: filter the tool schema BEFORE
   `bind_tools()` in `langgraph-agent` when `VISUAL_NAVIGATION_ONLY` is
   active — the model never sees a hard-gated tool in its schema at all,
   not merely a description telling it not to call one (the softer form
   already failed once for `manage_plan`'s adoption, see
   `docs/engineering-log.md`, "EFFORT 2" closure — reuse that lesson,
   don't re-learn it).
4. **Full tool classification, decided**:
   - **Kept**: `browser_navigate`, `browser_navigate_back`,
     `browser_press_key`, `browser_wait_for`, `browser_handle_dialog`,
     `browser_resize`, `browser_close`, `browser_tabs`,
     `browser_take_screenshot`, `browser_file_upload` (native file
     picker, no DOM ref — border case, revisit if a task ever exercises
     it and it turns out to leak something), plus the six coordinate
     tools below.
   - **Coordinate action space, currently UNAVAILABLE — requires a
     config change**: `browser_mouse_click_xy`/`_move_xy`/`_drag_xy`/
     `_down`/`_up`/`_wheel` live under `playwright-mcp`'s `vision`
     capability, opt-in via `--caps=vision` — absent from
     `docker-compose.yml` today (`playwright-mcp`'s `command:` block has
     no `--caps` at all). **Phase 2 must add `--caps=vision` to that
     command list** before any of these tools exist to gate or to use.
   - **Hard-gated when the mode is active**: `browser_snapshot`,
     `browser_find` (searches the accessibility snapshot),
     `browser_click`/`browser_hover`/`browser_drag`/`browser_drop`/
     `browser_select_option`/`browser_type`/`browser_fill_form` (all
     require a DOM `target` ref/selector), `browser_evaluate`,
     `browser_run_code_unsafe` (already never-grantable, gated here too
     for the same underlying reason), `browser_extract`/`browser_inspect`
     (this project's own synthetic tools — `browser_inspect` dispatches
     to `browser_evaluate` internally, `services/mcp-client/app/
     main.py:802`), `browser_console_messages`, `browser_network_request`/
     `browser_network_requests` (devtools-only visibility, not available
     to a plain sighted human), and the **whole filesystem MCP server**
     (`read_file`/`read_multiple_files`/`list_directory`/`directory_tree`/
     `search_files`/`get_file_info`/`list_allowed_directories`/
     `write_file`/`edit_file`/`create_directory`/`move_file`) — found live
     (`docs/resolved-bugs.md` #64), not anticipated at design time:
     `playwright-mcp` writes a real DOM accessibility-tree YAML to the
     shared `agent-downloads` volume on every `browser_navigate`, and
     `read_file` (absolute path, `/downloads/...`) can read it in full,
     `[ref=...]` tags included — the exact cheat this list exists to
     prevent, via a channel this list hadn't considered.
5. **Known gap, accepted, not solved by a new tool**: no coordinate
   equivalent of `browser_type` exists upstream — `browser_type` itself
   requires a DOM `target`. Typing in this mode goes through
   `browser_press_key`, one character at a time (it targets whatever the
   real browser currently has focus on, no ref needed — legitimately
   human-equivalent, just far more tool calls than one `browser_type`
   call). This is a structural cost of the mode itself, not a bug to fix
   — Phase 4's judges must read text-entry-heavy tasks with this in
   mind, not treat the extra calls as a regression.
6. **A real leak found in existing plumbing, must be fixed in Phase 2**:
   `_STABILIZE_AFTER_TOOLS = {"browser_navigate", "browser_click"}`
   (`services/mcp-client/app/main.py:776`) auto-appends a real
   `browser_snapshot` after every `browser_navigate` call (Effort 4
   point 1's "return resulting page state"). `browser_navigate` stays
   legitimate and used in this mode, so this auto-append would leak DOM
   content regardless of every other gate above. When
   `VISUAL_NAVIGATION_ONLY` is active, this must attach an auto
   `browser_take_screenshot`+`ocr-service` call instead of the DOM
   snapshot.
7. **Guardrails carried over, not rebuilt**: URL-fabrication guardrail,
   approval tiers, `NEVER_GRANTABLE_TOOLS`/`NEVER_GRANTABLE_TOOLS_EXTRA`
   — this mode changes perception and action granularity, not the
   security model. Confirm each still applies (tier is assigned by
   action nature, not by which tool triggered it).
8. **Truncation**: `AFFORDANCE_THRESHOLD` and its neighbours were tuned
   for DOM snapshot text; an OCR detection list is a different shape
   (short strings + four numbers each) — check whether existing
   truncation logic even applies here, don't assume it transfers.

🧑 Checkpoint passed (2026-09-20) — design reviewed, Phase 2 may start.

## Phase 2 — Build, unit-tested

Implementation of Phase 1's design. No live measurement in this phase.

Judge: existing suites stay green with the flag off (default, unchanged
behavior) — this phase's only judge, same bar every prior flag
introduction in this project was held to.

🧑 Checkpoint passed (2026-09-20) — **delivered**: `--caps=vision` +
`VISUAL_NAVIGATION_ONLY`/`OCR_SERVICE_URL` wiring (`docker-compose.yml`),
mcp-client's stabilization leak fixed, langgraph-agent's schema filter +
`_ocr_replace_image_blocks` OCR routing, `campaign_preflight.py`'s
`check_tools_schema` made aware of the mode's by-design schema
difference, coverage counter threaded through the harness. `langgraph-
agent` suite 450 → 463 passed, `mcp-client` 64 → 65 passed, 0
regressions. Full detail: `docs/engineering-log.md`, "Effort 8
(visual-navigation-only.md): Phase 1 design + Phase 2 build". **Not
live-smoked** — requires the real Docker/GPU stack, outside this
environment's reach; Phase 3 below is for the user to run.

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
