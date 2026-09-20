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

🧑 Checkpoint passed (2026-09-20) — **closed**: T3, n=1, four attempts on
the user's machine. Three real "no cheating" leaks found and fixed along
the way — none pre-anticipated at design time, each caught by actually
reading the raw audit log rather than trusting the campaign's aggregate
score alone:
- `docs/resolved-bugs.md` #62: `VISUAL_NAVIGATION_ONLY` never reached
  `mcp-client` (`docker-compose.yml` only declared it for
  `langgraph-agent`) — the schema filter worked, the stabilization-leak
  fix silently didn't. Smoke #1 passed with `ocr_calls: 0`, a genuine
  flattering zero the coverage counter caught.
- `docs/resolved-bugs.md` #63: a malformed `ocr-service` response (a
  stale, pre-Phase-0 image) crashed the whole turn instead of degrading
  — `_format_ocr_detections` was called outside its own `try/except`.
  Smoke #2 failed outright on a real internal-error notice.
- `docs/resolved-bugs.md` #64: the filesystem MCP server (`read_file`)
  could read the real DOM accessibility-tree snapshot `playwright-mcp`
  writes to the shared downloads volume on every `browser_navigate` —
  never gated, since the block list only ever named `browser_*` tools.
  Smoke #3 passed clean on the aggregate, but a full raw-audit-entry
  read (prompted by how precise the answer was) caught the model
  successfully reading that snapshot verbatim mid-task.

**Smoke #4, clean**: `visual_navigation_only_ocr_calls: 7` (vs. 1 on the
leaking #3), and the model's own reasoning text states outright that
`read_file` is not among its available functions — direct confirmation
of the gate, not an inference from absence. 12 tool calls, 89.6s (vs.
1-4 calls, <20s previously): the model visibly struggled with noisy OCR
coordinates and an uncooperative native `<select>` before finding the
correct answer — the "authentic capability-limit struggle" this phase's
own judge was written to accept, not a red flag. No known leak remains.

## Amendment — optimization pass before Phase 4 (external consultation, 2026-09-20)

Smoke #4 worked but cost 12 tool calls on a single short table-reading
task, mostly from OCR coordinate noise (row/column matching by y-center
alone is unreliable — different glyph heights give different box
centers on the same visual row) and a native `<select>` that never
visibly responded to a coordinate click. Before spending a full 22-task
campaign measuring a pipeline known to be this rough, a larger-capacity
model was consulted for an optimization plan given exactly this
evidence. Read in full: `docs/engineering-log.md`, "Effort 8 — external
consultation on visual-mode optimization". Filtered and prioritized
here; nothing below is committed to yet beyond the two checkpointed
first steps.

**Methodological point to keep regardless of any further change**:
`browser_snapshot` is a heavily processed, ref-annotated DOM view;
comparing it against raw OCR triples measures "curated DOM vs.
unprocessed pixels," not "DOM vs. vision." Phase 4's eventual write-up
must name this asymmetry explicitly, whether or not the pipeline below
gets built.

**Prioritized plan**:
1. **Coordinate-consistency sanity check** (near-free, do first): OCR
   jitter across repeated screenshots of the same static page, and one
   click verified against a real DOM element's own box (`browser_snapshot`'s
   `boxes: true` option — CSS-pixel, viewport-relative, per the installed
   `@playwright/mcp` schema) rather than assumed correct. Rules out (or
   confirms) a device-vs-CSS-pixel mismatch before anything else is
   built on top of possibly-wrong coordinates.
2. **Offline perception harness** (cheap, no live agent loop): capture
   screenshot + DOM-with-boxes pairs from ~20 real pages (including
   benchmark fixtures), evaluate the OCR reading against DOM ground
   truth offline — iterable in seconds, none of it spent inside a
   12-tool-call agent trace.
3. Layout reconstruction between OCR and the model: cluster rows by
   vertical INTERVAL OVERLAP (not y-center distance), columns by
   left/right edge alignment kept separately — hand the model a
   reconstructed table, not a bag of boxes.
4. Stable synthetic cell references (`r7c3`) resolved to coordinates by
   the harness, not raw `x,y` in the model's own tool call — the
   dominant cost lever, and it also fixes an approval-tier problem
   found along the way: "click at (412, 338)" is not a reviewable action
   for a human approver, "click the salary cell in the Dubois row" is.
5. A coordinate-free `type_text`(string) tool typing into whatever
   currently has focus (focus itself still established by a coordinate
   click, so the mode stays honest) — `browser_press_key`'s one-
   character-at-a-time cost is an artifact of which tools are enabled,
   not a property of visual-only perception. Basic keyboard nav
   (Tab/arrows/Enter/modifiers) alongside it, same reasoning.
   **Implementation path clarified (user finding, 2026-09-20)**:
   Playwright's own `page.keyboard.type(string)`/`.down()`/`.up()` are
   real primitives, but `@playwright/mcp` never wraps them as a
   standalone, target-less tool — every tool in its catalog is built
   around a `ref`/`target` or a single key, `keyboard.type()` fits
   neither shape, so this isn't configurable, it has to be built. Same
   pattern as `browser_extract`/`browser_inspect`: a synthetic
   `mcp-client` tool, fixed JS template (never model-supplied code),
   dispatched internally to `browser_evaluate` — writes into
   `document.activeElement` via simulated `input`/`keydown` events. The
   model only ever sees `type_text(text)`, never `browser_evaluate`
   itself, so this doesn't reopen the leak just closed. **Caveat to
   verify empirically before trusting it**: simulated DOM events don't
   have the same fidelity as Playwright's real OS-level `keyboard.type`
   — some JS-framework-controlled inputs may not respond to synthetic
   events the same way real keystrokes would.
6. Native `<select>` handling via keyboard after a focusing click
   (arrow keys or first-letter typing, then Enter), verified against the
   collapsed widget's own visible label — WITH an explicit fallback: if
   the popup truly never appears in a screenshot (renders in browser
   chrome, outside the page compositing surface), record that as a
   capability-limit finding, same spirit as A1's 0/3 and E2's 1/3, not a
   bug to keep forcing.
7. Extend the "return resulting state" pattern (Effort 4 point 1) to the
   new coordinate tools: auto screenshot+OCR after a coordinate click
   too, not just after `browser_navigate` — a meaningful share of the
   12 calls were likely act-then-look round trips this would remove for
   free.

**Explicitly deferred**: full widget detection (OmniParser-style
connected-components/edge-detection grounding) — already out of scope
per `PLAN.md` ("motivated by OBSERVED failures," not assumed needed);
point 2's harness is exactly the observation that would tell us whether
text-box centers already suffice on these 22 tasks before building
anything bigger.

🧑 Checkpoint (2026-09-20): points 1-2 approved to start now. Points
3-7 and the deferred item are not yet scheduled — revisit after the
harness's own findings are in.

**Point 1 result (2026-09-20)**: run against `fixture-hr-app/employees`
(`scripts/probe-visual-mode-coordinate-consistency.sh`). The script's own
jitter measurement had a bug — it grouped detections by TEXT VALUE
alone, and this page's department column repeats values ("RH" ×3,
"Ventes" ×5, ...) at different rows, so the reported "max y jitter:
309px (worst: 'RH')" is cross-ROW distance mistaken for cross-CAPTURE
drift, not a real signal. Corrected by hand using text that appears
exactly once per page (person names): **DOM boxes
(`browser_snapshot(boxes=true)`) and OCR boxes agree to within 1-3px on
every checked case** (e.g. Karim Haddad: DOM `box=10,217` vs OCR
`x=8,y=217`). **The consultation's top hypothesis (device-vs-CSS-pixel
mismatch) is empirically REFUTED**, not just inferred from documented
defaults as in the earlier engineering-log entry — coordinates are
reliable. This reframes smoke #4's difficulty: the ingredients (OCR
positions) are accurate, the cost came from asking the MODEL to
mentally re-sort a table from scattered triples — exactly what point 3
(layout reconstruction) targets, not a calibration fix.

Confirms the native `<select>` finding structurally, straight from the
DOM: `combobox [box=168,107,81,19]` but every one of its `option`
children reports `[box=0,0,0,0]` — the popup's options have no position
in the page's own rendering surface at all. No screenshot at any
resolution can show them. Point 6's fallback (record as a capability
limit, don't keep forcing a coordinate click) is confirmed as the right
call, not just a hedge.

**Repeated-text ambiguity is itself a finding for point 3**: this real
data shows exactly why the consultation warned against matching by
value or naive y-distance — even a small fixture page has non-unique
column values throughout. Point 1 closed.

**Point 2 result (2026-09-20)**: run across 6 pages
(`scripts/probe-visual-mode-perception-harness.sh`). 3 of 6
(`catalog-listing`/`docs-listing`/`perception-root`) hit a script bug —
wrong root URLs, captured nginx's default page instead of real content
(`docs/resolved-bugs.md` #66, fixed, not yet re-run). Usable findings
from the other 3 (`hr-employees-table`, `hr-leave-form`, `admin-root`):

- `hr-employees-table` confirms point 1's own reading — nothing new.
- **Both form pages (`hr-leave-form`, `admin-root`) surface a real gap
  points 3-4 as designed don't cover**: an empty input field has no OCR
  text at all (blank pixels have nothing to detect), so there is no
  text+box pair to cluster into a synthetic ref for it — the plan only
  ever reconstructs from TEXT. Fields need a rule of their own: associate
  an empty field to its label by geometric proximity (nearest field
  below/right of the label's box), not by shared OCR content.
- **PaddleOCR merges visually adjacent short strings into one
  detection**, coarser than the DOM's own element boundaries: a 6-link
  nav bar came back as a single OCR string spanning all six labels; a
  `<select>`'s value got fused with its label. Row/column clustering
  cannot split a value OCR already merged — this is a floor set by the
  OCR stage, not something the reconstruction layer on top can fix.
- **Text-fidelity artifacts on both form pages, not a one-off**: dropped
  accents, a flattened em dash, a stray `]` after some button labels.

**Reading**: points 3-4 are workable for tabular pages as designed, but
incomplete for forms — 2 of the 3 usable pages in this sample were
forms, not an edge case. Before building, add: (a) label→field
association by proximity when the field itself has no OCR text, (b) an
explicit acknowledgment that OCR-level text fusion is a hard ceiling the
reconstruction logic cannot see past, regardless of clustering quality.
**Point 2 re-run (2026-09-20)**, `docs/resolved-bugs.md` #66's URL fixes
applied: `docs-listing` now returns real content (36 links). Automated
DOM-vs-OCR comparison (nearest y-position, exact string match) run for
the first time: **31/36 exact (86%)**. 2 of the 5 mismatches are the
already-known nav-fusion issue; **3 are genuine content-changing OCR
misreads, not cosmetic** — `config-reseau-avancee` →
`contig-reseau-avancee`, `optimisation-performances-catalogue` →
`gptimisation-performances-catalogue`, `organisation-equipe-rh` →
`grganisation-equipe-rh` (position stays accurate, only the recognized
text is wrong, specifically on words starting "o"/"co" on this
font/rendering). **More serious than the earlier cosmetic accent/em-dash
findings**: an ~8% content-fidelity failure rate on ordinary hyphenated
slugs means a task like `A2_schema_references` (naming-conformance
check) could misjudge a genuinely correct reference as non-conforming
from OCR noise alone — a judge Phase 4 should declare for explicitly,
not assume away.

Two more rounds needed past that: `/catalog/index.html` was ALSO a thin
landing page (real listing: `/catalog/page-1.html`, 10 products);
`perception/e3-equivalence.html` confirmed clean, no new finding.
`catalog-listing`'s own comparison: 8/11 exact, all 3 mismatches accent
loss on capitals (`Étagère`→`Etagere`, both accents; `Élégant`→`Elégant`,
only the leading one) — a DIFFERENT failure mode from `docs-listing`'s
letter-substitution: visibly-wrong-but-recoverable vs. silently-plausible-
but-wrong.

**Point 2 CLOSED — consolidated finding set, 6 real pages**:
1. Coordinates reliable (reconfirms point 1).
2. Row/column clustering (points 3-4) workable on tabular/list pages.
3. Forms NOT covered by the plan as designed — empty fields have no OCR
   text; needs label→field association by proximity, added as a
   requirement before building.
4. OCR merges adjacent short strings below DOM granularity (nav bars,
   label+value pairs) — a floor the reconstruction layer cannot fix.
5. Two distinct text-fidelity failure modes: cosmetic accent/dash/
   bracket loss (recoverable) vs. genuine letter-substitution on
   unfamiliar slugs (silent, ~8% on one sample, dangerous specifically
   for naming-conformance tasks like `A2_schema_references`).
6. Native `<select>` options structurally invisible to any screenshot
   (`box=0,0,0,0` in the DOM itself) — capability limit, not a bug.

🧑 **Checkpoint**: points 1-2 fully answered. Building points 3-7 on
this finding set, or scaling back to running Phase 4 as-is with these
limitations documented as expected caveats, is the decision this
evidence was collected for — not made here.

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
