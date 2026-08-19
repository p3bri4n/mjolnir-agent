# Visual channel feasibility probe

Preliminary to the GhostDesk removal decision (`docs/briefs/B5-security-hardening.md`,
Phase 3 point 3: "if the DOM channel covers the useful web, remove the
browser from the GhostDesk image entirely"). A session-technique probe,
not an agent-capability measurement: every case below was driven
directly against `mcp-client`'s `/call` endpoint (`http://mcp-client:8003`),
no LLM call, no LangGraph loop, no task_id, nothing wired into
`docs/benchmark-v1.md`/`v2.md`.

Family E's own E2 (`docs/history.md`, "B3 SLICE 10") already established
that a genuinely capture-only case is hard to build honestly — two prior
attempts leaked the value through a purely textual path (a literal string
inside a `<script>` tag, then a computed character-code array still
readable via `innerHTML`) before a build-time PNG closed both. E2's own
result (1/3, an audit-log-verified tool-confusion between GhostDesk's
`screen_shot` and Playwright's `browser_take_screenshot`) is a tooling
artifact, not a capability finding — this probe, not E2, is what the
brief designates as the actual basis for the removal decision.

## Method

8 fixtures (`services/langgraph-agent/tests_integration/fixtures/visual-probe/`,
`fixture-visual-probe`, `test-fixtures` profile — never wired into any
frozen benchmark), one per content-rendering pattern that could plausibly
force a visual read. Each carries one ground-truth string; where the
point is to test a channel that should carry no DOM representation
(canvas, WebGL, image, PDF), the string is baked into a PNG/PDF at BUILD
time, never a JS/HTML literal — otherwise `browser_extract`'s
`TreeWalker(SHOW_TEXT)` (which walks every text node under
`document.body`, including a `<script>` tag's own text content) would
leak it regardless of the rendering channel actually exercised. Ground
truth for the cross-origin case reuses the existing, unmodified
`fixture-docs` title rather than a synthetic string — real different-
origin content, no new container, no touching a frozen fixture.

Three channels checked per case, matching the tools actually available
to the agent today (not a theoretical DOM-vs-visual binary):

- **`browser_snapshot`** — Playwright's own accessibility-tree snapshot,
  the tool used for grounding actions.
- **`browser_extract`** — this project's own fixed-template text search
  (`mcp-client`, `_build_extract_function`), a plain `TreeWalker` over
  `document.body`.
- **screenshot + OCR** — `browser_take_screenshot` scoped to the relevant
  element (`target`), decoded and run through the SAME PaddleOCR engine
  `ocr-service` uses in production (`app/ocr_engine.py`, `get_engine().run()`),
  invoked directly (no GhostDesk desktop capture involved at all — this
  isolates "can visual capture read this" from "does GhostDesk specifically
  provide it").

## Matrix

| Case | `browser_snapshot` | `browser_extract` | screenshot + OCR |
|---|---|---|---|
| VP1 — Canvas 2D (pre-rendered PNG drawn via `drawImage`) | ✗ | ✗ | ✓ |
| VP2 — WebGL (same PNG uploaded as a texture, textured quad) | ✗ | ✗ | ✓ |
| VP3 — `<img>`, `alt=""` (control, same pattern as family E's E2) | ✗ | ✗ | ✓ |
| VP4 — PDF opened directly (Chromium's built-in viewer) | ✗ | ✗ | ✓ |
| VP5 — cross-origin iframe (`fixture-docs`, unmodified) | ✓ | ✗ | ✓ |
| VP6 — open shadow DOM (`attachShadow({mode:'open'})`) | ✓ | ✗ | ✓ |
| VP7 — inline SVG `<text>` (control) | ✓ | ✓ | ✓ |
| VP8 — off-viewport (`position:absolute; left:-9999px`) | ✓ | ✓ | ✗ (empty OCR) |

Raw evidence: `browser_extract` on VP1 returns the literal JSON `"[]"`
under its own `### Result` marker (verified by parsing that field
specifically — the tool's response ALSO echoes the executed Playwright
code, which contains the query string in clear text; a naive substring
check against the whole response is a guaranteed false positive, caught
and fixed while building this probe). OCR text detected per case:
`VP1→"VP-1001"`, `VP2→"VP-1002"`, `VP3→"VP-1003"`,
`VP4→["vp4-document.pdf","100%","VP-1004","VP-1004"]` (viewer chrome +
content, both real), `VP5→["Documentation - Sommaire", "config-reseau-avancee", ...]`,
`VP6→"VP-1006"`, `VP7→"VP-1007"`, `VP8→[]` (58×18px capture, genuinely
blank — the element is never painted to any visible region, screenshot
"succeeding" at the API level does not mean it captured anything).

## Reading the matrix

- **VP1-VP4 (canvas, WebGL, image, native PDF viewer)**: no DOM channel
  reaches these — a real, structural gap, not a tooling artifact. Visual
  capture is the ONLY way to read them, and it works cleanly in all 4
  cases via `browser_take_screenshot`, Playwright's own tool. **GhostDesk
  is not involved in this result at all**: nothing here required
  GhostDesk's `screen_shot` (which captures the OS desktop, not the
  browser's rendered page specifically) — Playwright's own screenshot
  tool, already present regardless of GhostDesk, covers every one of
  these.
- **VP5-VP6 (cross-origin iframe, open shadow DOM)**: `browser_snapshot`
  already covers both — Playwright's accessibility tree pierces
  cross-origin iframes and open shadow roots. `browser_extract` does
  not (a real gap in THIS tool specifically: its `TreeWalker` only walks
  `document.body`, never descending into iframe documents or shadow
  roots) — but since the agent already has `browser_snapshot` for
  grounding, this is not a capability gap for the agent overall, only an
  extraction-tool limitation worth knowing about.
- **VP7 (SVG text)**: control case, confirmed as expected — ordinary DOM
  text, no capture needed.
- **VP8 (off-viewport)**: the exact inverse of VP1-VP4 — only the DOM
  channels read it; visual capture returns nothing (confirmed empty OCR,
  not just an assumption). This is what family E's E1 already relies on;
  this probe corroborates it independently.

## What GhostDesk removal would lose

Nothing, for the 8 patterns tested here. Every case that visual capture
is needed for (VP1-VP4) is already served by Playwright's own
`browser_take_screenshot`, which has no dependency on GhostDesk
whatsoever. GhostDesk's distinguishing capability — `screen_shot` of the
**OS desktop** plus coordinate-based mouse/keyboard control **outside**
the browser — is simply never exercised by any in-page content pattern,
by construction (nothing here leaves the browser). The only capability
that would be lost by removing GhostDesk is native, out-of-browser
interaction (dialogs, other desktop applications) — exactly `E4`'s
territory, already decided out of scope by explicit user choice
(`docs/project-status.md`, family E): *"GhostDesk's own justification
(the question only E4 could answer) stays permanently unmeasured by
this benchmark."* This probe does not reopen that decision; it confirms
there is no OTHER, undocumented reason to keep GhostDesk around.

## Side observation — fixed since (commit `6b4264e`)

`browser_snapshot` and `browser_take_screenshot` were NOT in
`_DEFAULT_TIER_READ` (`app/approval_policy.py`) at the time of this probe,
despite being declared `type: "readOnly"` by the Playwright MCP server
itself (verified against the installed `mcp/playwright:latest` image's
own schema, CLAUDE.md #8) — both defaulted to TIER_SENSITIVE, same as a
mutating action, unlike `browser_extract`/`browser_inspect` which already
got this treatment for the same reason (pure read, nothing to exfiltrate,
nothing to undo). Fixed shortly after this probe (commit `6b4264e`,
"promote browser_snapshot/browser_take_screenshot to TIER_READ") — both
tools are TIER_READ today. This is a direct prerequisite for effort 3's
visual-only-content handling (`docs/architecture/autonomy.md`):
`browser_take_screenshot` needed to be silent (no approval pause) for the
model to reach for it routinely, whatever the routing mechanism.

## Follow-up — browser_snapshot's raw signal (effort 3 checkpoint, 2026-08-11)

This probe's matrix (above) recorded whether the ground-truth STRING is
readable per channel — it does not say what `browser_snapshot`'s raw
accessibility-tree TEXT actually contains for VP1-VP4. That gap mattered
for `app/graph.py`'s `_detect_visual_signal` (since removed — see
docs/history.md, "PROBE VISUEL — SIGNAL BROWSER_SNAPSHOT"), which needed
a pattern to grep for in an already-fetched result. Same method as
above, extended: direct `mcp-client` calls, raw `browser_snapshot` text
captured per case (`scripts/probe-visual-snapshot-signal.sh`).

**Result**: VP1 (canvas), VP2 (WebGL), VP3 (`<img alt="">`) all come back
as heading + intro paragraph ONLY — the element itself produces **zero**
accessibility nodes, not even an unlabeled placeholder. A page with a
canvas is text-identical to one without; there is no positive pattern to
detect after the fact. VP4 (PDF opened directly) is the one exception:
the entire response comes back empty (no page-title line even) — a real,
structural, and detectable signal, but it's an ABSENCE tied to
navigation context, not a keyword. VP7 (SVG text, control) renders as
accessibility role `img` wrapping a `generic` node with the real text —
proof that a naive `role: img` heuristic would false-positive on content
that needs no capture at all. VP8 (off-viewport, control) renders as an
ordinary `generic` node, correctly indistinguishable from any other DOM
text.

**Reading**: no after-the-fact heuristic over `browser_snapshot`/
`browser_extract` text can catch canvas/WebGL/alt-less-img — the
routing decision has to be made BEFORE the fact. Resolved by moving the
hint into `browser_take_screenshot`'s own tool description
(`_tool_description_with_appends`, `services/mcp-client/app/main.py`)
instead of a detector; the PDF case's genuine empty-snapshot signal gets
a real redirect hint (`_flag_empty_snapshot`, same file). See
`docs/architecture/autonomy.md`, "Visual-only content: tool description,
not detection" for the full design.
