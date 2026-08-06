# Consolidated plan — next phase

> **Why this document.** `PLAN.md` describes an effort that is now largely
> delivered, and parts of it are factually stale. Three briefs are open
> (B5, B6, B7), four more were drafted in discussion and never committed,
> and the priority order between them has never been written down. This
> plan replaces the open part of `PLAN.md` and sequences everything that
> remains.
>
> **Sequencing principle**: what makes every later measurement cheaper
> comes first; what may *remove* mechanisms comes before what improves
> them; what needs an instrument waits for the instrument.

---

## First: correct `PLAN.md`, it is stale

Three factual errors, each already contradicted by the repository — fix
before anything else, they are actively misleading:

1. **Context section claims PromptGuard and an egress firewall exist.**
   Neither does (verified 2026-07 during the README features review).
   They belong under B6, not in a description of the current stack.
2. **Phase 1 point 6 describes GhostDesk as the explicit fallback
   channel.** The decision is removal (see effort 3 below), and the
   visual-channel feasibility probe concluded removal loses nothing
   tested.
3. **Phase 3 point 4 says the suite goes "from 11 to 13 tasks" with two
   injection tasks.** Superseded: benchmark v2 family C already covers
   injection and scope, measured 9/9.

Phases 0–2 of `PLAN.md` are delivered; Phase 3 is superseded by B6 and by
v2; Phase 4 (consolidation) survives as a final step. Rewrite `PLAN.md`
as the roadmap of what follows, pointing at this plan's efforts.

---

## Effort 1 — Make campaigns cheap (prerequisite to everything)

Campaign cost is the meta-problem: a full v2 run is over an hour, which
is what makes one-variable-at-a-time feel unaffordable. Three levers, in
order of return:

**1.1 — Tool-schema weight audit (archives + tokenizer, zero runs).**
The dégraissage measurement showed the tool schema at ~13k of a ~14k
median context: **the schema is the context**. Measure the token weight
**per MCP server**, cross it with actual call frequency in the audit log
across all v2 campaigns. Deliverable: a table of weight × usage per
server.

**1.2 — Remove or trim what is heavy and unused.** Likely candidates are
the non-web servers, but `filesystem` is load-bearing (the download path,
family F's T5) — verify before cutting. One variable, judged by median
context, prefill time, and CuP non-regression. Do this *before* the
ablation: every campaign after it is cheaper.

**1.3 — Reopen parallel run execution. STATUS: deferred, quantified
justification** (see `docs/history.md`, "EFFORT 1.3", archives-only
recompute, zero runs). Dismissed earlier when inference was the
bottleneck; the median went from 145.9 s to 45.0 s since — **not** from
the TabbyAPI/dual-GPU migration as stated below previously, but from the
`PLANNER_THINKING_ENABLED` fix (verified against `docs/history.md`'s own
checkpoint campaign). Recomputed expected gain (33-task campaign, N=3
workers, per-task time split ≈22.9s GPU-bound / ≈22s I/O-bound): **×2.2**
pessimistic (TabbyAPI serializes inference) to **×3** optimistic
(concurrent batching effective — unconfirmed, see below). The gain holds
even pessimistically, but implementation is deferred: it requires
per-worker isolation (session reset, volume purge, distinct thread ids)
for **four** contamination incidents, not three — session #30, downloads
volume #28/#29, GhostDesk desktop #42, and `_tools_schema_cache` #31
(same "unscoped shared state" defect family) — the isolation guarantees
are non-negotiable, they are what these four incidents bought. Verified
against the installed TabbyAPI/ExLlamaV3 code (not the config doc, which
turned out generic/outdated): `max_batch_size` defaults to 128 for
standard-attention models, 4 for recurrent-state models
(`backends/exllamav3/model.py`); Qwen3.6's hybrid `gated_delta_net`
attention makes the 4-job branch plausible but unconfirmed without
inspecting the loaded model's capabilities at runtime. A related
architectural limitation was found and documented independent of this
effort: `mcp-client`'s `_persistent_sessions` is keyed by server name
only, not by caller (`docs/architecture/mcp-client-concurrency.md`) —
already reproducible today by two concurrent real conversations, not
just by parallel campaigns. Preferred fix path when this effort resumes:
scope `_persistent_sessions` and the three resets by `worker_id` (cheaper
than N container sets, also fixes the architectural limitation). Efforts
1.2 (delivered), 2, and 4.2 below all reduce campaign duration via the
numerator — re-measure median campaign duration after they land before
resuming this chantier.

🧑 Checkpoint on 1.1 before cutting anything.

## Effort 2 — Factorial ablation (B7 effort 1, amended)

The highest-value measurement available, and it may *simplify* the
architecture rather than extend it. Run B7 effort 1 as written, with two
amendments:

**2.1 — Add a fifth condition: merged planning.** Not in the current
matrix. Planning becomes an action available in the main turn (plan
operations alongside navigation, one action per turn) rather than a
dedicated node with its own LLM call. This is the AgentOccam pattern, and
it is the condition most likely to keep planning's value while removing
its cost — the latency diagnosis attributed 73–89 % of task time to
auxiliary calls. Implement as a fifth env-selected mode, not a branch.

**2.1 addendum (point 3, design pinned down before implementation)**:
new `PLANNING_MODE` env var (`app/graph.py`, default `"nodes"` — current
behavior, unchanged for every existing config; `"merged"` selects the
new path). The only validated combination for `"merged"` is with the 4
existing flags all forced `"false"` at the campaign level (asserted via
`campaign_preflight.py`'s `CAMPAIGN_EXPECTED_FLAGS_OVERRIDE`) — this
keeps `plan_task`/`validate_plan`/`revise_plan`/`replan_task`/
`verify_action` structurally no-op (all already gate on these 4 flags),
so all planning responsibility moves into one new synthetic tool,
`manage_plan`, same precedent as the existing `_REPORT_AND_ACT_TOOL`
(graph.py). Two actions only:
- `set_plan(subtasks)` — creates the plan if none exists, or **replaces
  the remaining subtasks** if one already does (this IS the replan
  path: a subtask never gets a persisted `"echoue"` status in this mode,
  so the costly `replan_task` node is never reached). Validated for
  free via the existing `plan_validation.validate_plan_heuristics` — no
  new bounds invented, no LLM judge call (removing that call is the
  entire point of this mode). Rejection returns the reasons in a
  ToolMessage, plan state untouched.
- `complete_subtask(subtask_index)` — marks it `"fait"`, advances the
  next subtask to `"en_cours"` (mirrors `verify_action`'s `atteint`
  branch).

Both dispatched in `_execute_tool_calls` (never sent to mcp-client,
`TIER_READ` in `approval_policy.py` — pure bookkeeping, nothing to
exfiltrate or undo), logged from day one as `role="merged_planning"`
audit entries (CLAUDE.md's retroactive trigger-rate-counter rule). A
new `_merged_plan_directive(state)` (same shape as the existing
`_verification_directive`) tells the model its active subtask in
`call_llm`'s system prompt — without it, merged mode would be measured
at an information disadvantage unrelated to its actual cost question.

Point 3's measurement itself is 3 configs (cfg1-all-off, cfg8-all-on,
cfg9-merged-planning) × the point-2 subset (`A1`, `A2`, `A3`, `A4`,
`D1`, `B1_conge_hard`) × n=3 — a live smoke (n=1, 1-2 tasks) required
first per CLAUDE.md's measurement rules.

**Point 3 status: CLOSED 2026-08-06, cfg9 dropped, full sweep never
launched.** The live smoke (fifth-condition diagnostic, 3 corrections
tried in isolation — dedicated planner off, persistent/editable plan
section, tool position moved first in the schema) got the model to
engage `manage_plan` on one task family (A1) but never to REVISE a plan
mid-task (`merged_plan_replans` stayed 0 on every run, including the
engaged ones) and never changed task success. Revision under difficulty
is what separates AgentOccam's pattern from a classic planner — without
it, "keep the value, cut the cost" has no object to measure. Full detail
and the tool-position side-finding: docs/history.md, "EFFORT 2". Effort
2's measurement reverts to its original 2-config question (cfg1 vs
cfg8, `scripts/run-flag-sweep.sh`).

**2.2 — Run the four existing flags FIRST.** If the planner proves
globally harmful, building the merged mode is pointless. Measure what
exists before building what is missing.

Decision table is already in B7 and stands: fixed winning configuration →
adopt and **remove** the losers; legible task-nature dependency →
conditional activation with the simplest observable criterion, never an
LLM classifier; differences within noise → keep only what has *safety*
value.

**2.2 measured (2026-08-06)**: cfg1-vs-cfg8, discriminating subset, n=3,
36 runs, coverage counters confirmed non-trivial throughout (resolves
the earlier "not conclusive" verdict from the 7-task/n=2 ablation).
cfg1-all-off 15/15 vs cfg8-all-on 13/15 on the 5 scored tasks (A1 read
for coverage only, per point 2's protocol), cfg1 never losing, at 43%
less cumulative time for the same real work (tool_calls essentially
identical, 195 vs 193). The table's first branch applies: fixed
configuration matches-or-beats all-on → adopt and remove the losers.
Full detail: docs/history.md, "EFFORT 2 — DECISIVE MEASUREMENT".
`PLAN_VALIDATION_ENABLED` keeps its safety-value exception, untouched by
this reading.

🧑 Checkpoint before any removal — reported, not yet acted on.

## Effort 3 — GhostDesk removal

Decision taken (Mjolnir is a web agent), and the feasibility probe
confirms nothing tested is lost: canvas, WebGL, images and native PDF
are all readable via `browser_take_screenshot`, which is Playwright's own
tool. E4 is permanently out of scope by explicit decision.

Removal closes an entire uninspectable channel — no URL argument to
validate, no per-task scope enforceable — and deletes a container, an
image, and a contamination source. It also **simplifies B6**: no kiosk
mode, no per-channel proxy rules, no address-bar OCR.

**`ocr-service` is kept, but changes hands.** It stops being a tool the
model can call and becomes a **graph capability**: the graph decides when
a visual reading is needed, takes a `browser_take_screenshot`, runs OCR,
and attaches the result to the observation. Three gains at once — the
schema loses its OCR and capture tools (effort 1), the model loses a
choice it demonstrably made badly (E2's 1/3 was an audit-verified
confusion between `screen_shot` and `browser_take_screenshot`), and the
decision moves to where the information actually is: the graph can *see*
whether the DOM snapshot is usable, the model can only guess.

**Trigger — reactive, not proactive.** "The DOM doesn't contain what's
needed" is a judgement, not an observable fact, and a heuristic guessing
it will be wrong in both directions. Use a signal already produced: after
a `not_reached` verdict on a read action, the graph enriches the *next*
turn's observation with an OCR'd capture. Wrong only by one turn's delay,
and it reuses post-action verification rather than inventing a detector.
A proactive variant (detecting `<canvas>`, PDF `<embed>`, alt-less images)
may be added later as an optimisation, measured separately.

E2 is re-measured after this change rather than left standing as a
capability limit — its failure mode is exactly what this removes.

Judge: full campaign unchanged outside family E; E2 improves; median time
slightly improved; container count, schema weight and VRAM down.

## Effort 4 — Scaffolding improvements (B7 efforts 2 and 3)

Only after effort 2, whose result may change what is worth improving.

**4.1 — Diff-based observation history.** Natural-language change
descriptions in place of repeated snapshots, generated by the wrapper —
**never by an extra LLM call**, which would reintroduce the auxiliary-call
latency problem. Expect sub-additive gains with post-action verification.

**4.2 — Coarse-grained actions.** Start with the archives: which tool-call
n-grams recur across runs? That frequency analysis names the composites
to build; do not design from intuition. Coarse tools *coexist* with fine
ones, must fail cleanly and redirect, and must say in their description
when to prefer them. `MAX_TOOL_ITERATIONS` is not to be raised — the goal
is needing fewer turns. A1 (0/3) and A4's reverted 9-step extension are
the judges.

## Effort 5 — Security (B6), gated on a working instrument

B6 stands as written, with one blocking correction: **family C measures
9/9 at baseline, so it cannot serve as B6's zero point** — no proxy, no
per-task scope, no provenance tracking can demonstrate an improvement
against a perfect score.

**5.0 — Build v2.1's hostile family first**: indirect and multi-step
injections, plus the canary-token task (a unique string planted in
sensitive context, run fails if it appears in an outbound request). New
benchmark version, frozen fixtures, no cross-version comparison.

Then B6 phases in order: threat model, secrets and data minimisation
(including the audit log's own retention and redaction — it now persists
tool results and model messages in cleartext), network boundary,
per-task scope, provenance tracking. Phase 5 (quarantined reasoning)
overlaps with the deferred second-model decision — treat them as one
arbitration, not three.

## Effort 6 — Unblocking and session persistence

The drafted brief (anti-bot detection, ephemeral VNC hand-over, session
TTL) is not committed. It is worth doing **only if the measurement
justifies it**: one task blocked across the whole v2 suite. Commit the
brief, then start with its Phase 0 (headed mode, `BROWSER_HEADED=true`,
new reference campaign) only when a real usage frequency justifies the
capability. Until then it stays a documented limitation in the README.

## Effort 7 — Quantisation evaluation

Last, and gated on a stable baseline. The drafted brief stands: static
VRAM measurements first (Phase 2 of that brief can close the question
before any campaign — if 5.0bpw consumes the pool, the trade is reasoning
quality against the quarantined LLM *and* the critic model, a design
decision rather than a measurement). The real judge is the failure-cause
distribution, not the score.

**Cheaper alternative to consider first**: one campaign against a
token-billed endpoint with a clearly stronger model, on local fixtures
only, to answer whether reasoning is the limiting factor at all. ~$10 and
an evening. If the ceiling does not move, the whole quantisation question
closes. Dev-only tooling, never a product feature; campaigns from a
remote endpoint are not comparable on latency or prefill, only on score
and failure causes.

## Effort 8 — Visual-only navigation mode (separate, later)

A `VISUAL_NAVIGATION_ONLY` mode in which perception is exclusively
visual: no DOM snapshot, no accessibility tree, capture + OCR only, and
an action space to match (coordinate-based interaction rather than
selectors).

**Kept separate from effort 3 deliberately.** Effort 3 redistributes a
capability; this creates a whole operating mode with its own action
space. Bundling them would repeat the two-variables-in-one-iteration
mistake that cost a campaign in phase 1b.

Why it is worth doing at all: it is the only honest way to measure what
the visual channel is worth *on its own*. Family E only tested isolated
cases. Judges: score in visual-only mode vs DOM mode on the same suite,
tokens per task, median time. A figure for what the visual channel
actually buys is the kind of number few projects can publish — and it is
the definitive answer to the question E4 was going to ask before it went
out of scope.

Prerequisites: effort 3 delivered (OCR already a graph capability),
effort 1 delivered (campaigns cheap enough to afford a second reference).

---

## What is missing and should be added

**B5 (campaign visual feedback) is open but unsequenced.** It is genuinely
useful for diagnosis (Playwright traces especially), but it is not on the
critical path of any effort above. Suggest deferring it explicitly rather
than leaving it open, or folding its trace-capture part into effort 1.3
(parallel runs will make live watching less relevant anyway).

**No external calibration has ever been run.** After ten campaigns of
tuning against the same fixtures, overfitting to them is a real
possibility, and nothing in this plan would detect it. One WebArena run
via BrowserGym, once, as a held-out test. Expect a low figure and decide
that framing before seeing it.

**Campaign control was never verified end-to-end.** Delivered at unit
level only (flagged in its own history entry). The first campaign of
effort 1 should be the one that verifies it live, including a real
pause/resume.

**The multi-agent / sub-agent idea has no brief.** Discussed as a
context-boundary mechanism (web research, quarantined reading,
post-mortem failure classification) with a firm framing: a single
parameterised tool, READ tier, own budget, closed-question outputs only.
It belongs in the Mjolnir folder arbitration alongside the second-model
question, not as a separate effort.

**The GhostDesk removal brief is not committed either** — it was drafted
in discussion, and effort 3 now supersedes part of it (the `ocr-service`
question is settled). Commit a revised version before touching code, per
the brief-before-code rule.

**`docs/history.md` still needs its rename and index** (`engineering-log.md`,
dated index at the top, stop copying campaign result tables into it now
that `campaign-*.json` exists). Small, and it makes every later session
cheaper to start.
