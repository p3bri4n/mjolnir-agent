# Autonomy — plan → act → verify → replan loop

Content moved as-is from README.md (restructuring effort, see docs/briefs/restructuration-et-anglais.md, phase 3) — no rewrite at this stage, except the "Visual-only content" section (effort 3, GhostDesk removal), rewritten in full to match the current design.

**Loop architecture** (see `docs/briefs/phase-1-coeur-cognitif.md` for the
full effort, sequenced in 4 iterations, one iteration = one mechanism =
one designated judge = one checkpoint): `plan_task` decomposes the
objective into validated JSON subtasks, `validate_plan` runs them through
a heuristics pipeline then (optionally) an LLM judge before tiered human
approval, `call_llm`/`_execute_tool_calls` execute, `verify_action`
compares each result against the active subtask's criterion,
`replan_task` takes back over on budget failure, `report_failure` ends
honestly if the replanning budget is exhausted. The 4 mechanisms are
independently toggleable (`PLANNER_ENABLED`/`VERIFICATION_ENABLED`/
`PLAN_VALIDATION_ENABLED`/`PLAN_JUDGE_ENABLED`). History: defaults were
FLIPPED to `true` after the initial campaign (29/33, consistent with
pre-cognitive-core Campaign A at 30/33 — see
docs/briefs/flags-du-coeur-cognitif.md), then **flipped back to `false`
for `PLANNER_ENABLED`/`VERIFICATION_ENABLED`/`PLAN_JUDGE_ENABLED`
(EFFORT 2.4)**: a later decisive ablation (36 runs, discriminating
5-task subset) found cfg1 (all 4 flags off) never losing to cfg8 (all
on, the interim default) at 43% less cumulative time for essentially
identical real work, and separate diagnostics (the A1 trajectory
diagnostic, `docs/resolved-bugs.md` #51) found the mechanism actively
discarding genuine progress via attempt/replan-budget churn on
multi-page tasks, not merely costing more for the same result — see
docs/history.md, "EFFORT 2 — DECISIVE MEASUREMENT". `PLAN_VALIDATION_ENABLED`
alone is KEPT `true` (safety-value exception: a programmatic heuristic
gate, not a score-driven mechanism, untouched by that reading). See each
one's detail below.

**⚠️ Operational trap**: these 4 flags (as well as
`MAX_TOOL_ITERATIONS`/the attempt and replanning budgets/
`PLANNER_THINKING_ENABLED`/tier overrides/truncation thresholds — full
list in `EXPECTED_AGENT_FLAGS`, `tests_integration/campaign_preflight.py`)
are read at MODULE level in `app/graph.py` (Python constants computed
once at import time): any `.env` change requires
`docker compose up -d --force-recreate langgraph-agent` — a plain
`restart` does NOT re-read `.env`, the process doesn't restart. Setting
them in the shell that launches `scripts/run-campaign.sh` has **NO
effect at all**: the harness talks to the agent over HTTP (`docker exec
... curl`), these flags live in the container's server process, not in
the harness's environment — only editing `.env` THEN the
`--force-recreate` above changes the measured behavior.
`campaign_preflight.check_agent_flags()` now refuses a campaign BEFORE
its first run if the container's effective flags diverge from the
expected config, to catch this trap early.

`plan_task` (`app/graph.py`, a new node between `select_skill` and
`call_llm`) decomposes the task's objective into JSON subtasks
(`{description, critere_succes, outils}`, programmatically validated
schema, 1 to 8 items) via a dedicated LLM call — not tool-bound
(`planner_llm.ainvoke`, not `bound_llm`), not streamed, separate from the
main loop. `planner_llm` is a `ChatOpenAI` client SEPARATE from `llm`
(the conversational loop), with its own `PLANNER_MAX_TOKENS` budget
(default `8192`, much larger than `LLM_MAX_TOKENS`): a real bug found
under real conditions (see docs/history.md, Iteration 3) — Qwen3.6/TabbyAPI
reasons in a `reasoning_content` field separate from `content` before
answering, and this reasoning alone consumed the entire `LLM_MAX_TOKENS`
budget (2048), systematically truncating the JSON reply. The user message
sent to the planner also includes the real list of available MCP tools
(`_available_tools_hint`, same reason: without it, the planner invents
plausible but nonexistent tool names). Computed ONLY ONCE per task
(`AgentState.plan`, reset to `[]` on every new top-level user message
just like `observed_urls`): any error (transport, invalid JSON) degrades
to a single-subtask plan wrapping the objective as-is, never blocking the
task. The plan is visible in the logs and summarized in the existing
approval message (`_format_plan_summary`, `app/main.py`).

**Why the original "false" default** (Iteration 1, before the full
measurement): a second LLM call at the start of every task would have
broken almost all existing tests, which mock a fixed sequence of
`/v1/chat/completions` replies — see docs/history.md, "Iteration 1:
explicit plan". The tests concerned now explicitly force the value they
test (`_default_cognitive_core_flags_to_false` fixture,
`tests/conftest.py`) rather than depending on the default.

**Post-action verification + failure budget** (`VERIFICATION_ENABLED`,
default `false` since EFFORT 2.4, see above — Iteration 2): **only has an effect if `PLANNER_ENABLED`
is also on** (nothing to verify without a plan). After every tool-call
turn, `verify_action` (`app/graph.py`) compares the result to the ACTIVE
subtask's `success_criterion`, via a dedicated LLM judge call
(`{"atteint": bool, "raison": str}`, validated by
`_validate_verification_json`, same pipeline as the planner) — not a
criterion reformulated on the fly in the turn's reasoning (no structured
reasoning exists in this graph to extract it reliably, see docs/history.md
"Iteration 2"). Positive verdict: subtask `"fait"`, moves to the next
one. Negative verdict: `SUBTASK_ATTEMPT_BUDGET` attempts (default `3`)
before marking `"echoue"` — every retry must change strategy, an
identical (name+args) tool_call to the previous turn after a first
failure is blocked by `_execute_tool_calls` without calling mcp-client
(`_repeated_strategy_feedback`). Subtask `"echoue"` → replanning
(`replan_task`, reuses the planner with the failure's context,
`REPLAN_BUDGET` attempts, default `2`) → beyond that, `report_failure`
produces an honest report of the state reached (never a false success,
never an infinite loop) and ends the task.

**Plan validation pipeline** (`PLAN_VALIDATION_ENABLED`, default `true` —
Iteration 3): **only has an effect if `PLANNER_ENABLED` is also on**.
`validate_plan` (`app/graph.py`, between `plan_task`/`replan_task` and
`call_llm`) first applies programmatic heuristics
(`app/plan_validation.py`: 2-12 subtask bounds, no duplicates, existing
referenced tools, domains within the declared scope), then, if
`PLAN_JUDGE_ENABLED` (default `false` since EFFORT 2.4, see above —
measured withdrawal clause, see docs/history.md Iteration 3: it did
really veto a plan the heuristics let through), an
LLM judge (`{"faisable": bool, "risques": [...], "etapes_manquantes":
[...]}`, FAIL-OPEN on error). Rejection → `revise_plan` (max
`PLAN_VALIDATION_CYCLES_MAX` = 2 cycles) → beyond that, human escalation
with the reasons displayed. Accepted plan: tier = the worst tier among
all declared tools (`_plan_tier`, reuses `approval_policy.tool_tier`) —
`TIER_READ` goes straight through, `TIER_REVERSIBLE`/`TIER_SENSITIVE`
trigger `require_plan_approval` (mirrors `require_approval` but for the
whole plan, new `plan_approved` field). A plan grant (`plan_grant`) is
possible for `TIER_REVERSIBLE` on a later replanning of the same task,
**never for `TIER_SENSITIVE`** (same philosophy as
`NEVER_GRANTABLE_TOOLS`). **Stays non-mergeable** with the individual
approval of a `TIER_SENSITIVE` tool at execution time — `require_approval`/
`_execute_tool_calls` unchanged, plan approval is an additional upstream
gate, never a substitute (verified under real conditions, see
docs/history.md).

**Grounding on the page's real state** (Iteration 4, no new flag — part
of the existing `VERIFICATION_ENABLED`/`PLAN_JUDGE_ENABLED`/
`PLAN_VALIDATION_ENABLED`): found in 2 passes over successive live probes
(see docs/history.md, Iteration 4, for the detail of the 6 probes).
`verify_action` used to judge a `success_criterion` literally, without
ever seeing the real page — a criterion assuming a missing feature (e.g.
a search bar) would wrongly fail legitimate progress (e.g. via
pagination). `_fetch_verification_snapshot(objective)` captures a fresh
`browser_snapshot` after any turn using a `browser_*` tool, passed to the
verifier (`etat_actuel_de_la_page`) — judges real progress, not the
criterion's letter. The planner/plan judge had the same grounding flaw on
replanning: `_grounding_snapshot(state, objective)` (reuses the function
above, `None` if no navigation has happened yet — the very first
`plan_task` remains structurally ungrounded) passes the same snapshot to
`revise_plan`/`replan_task`/`_judge_plan`. Side effect discovered AFTER
this second fix: the planner, now able to see real product names on the
page, started conflating the exact item requested by the objective with
a real but different item visible on the page — the prompts
(`snapshot_hint`, `PLAN_JUDGE_SYSTEM_PROMPT`) now explicitly warn against
this substitution.

**v1 campaigns of the "cognitive core" effort** (11 tasks × 3 repetitions,
see `docs/benchmark-v1.md` for the full v1 suite — its LAST reference
campaign, the v1 suite nearing saturation):

**Final campaign** (4 flags active, ~104 min): **29/33** after a fix and
a retry (28/33 raw initially — see below) — full detail in
`docs/campaigns/2026-07-23_campaign_coeur-cognitif.md`. Consistent with
pre-cognitive-core Campaign A (30/33, see docs/history.md), not a
regression. Of the 4 missing points: 1 harness infra timeout (T7,
unrelated to the agent), 1 extraction failure (T1), 2 extraction failures
on T8 (Wikipedia — see below). Aggregate score deliberately shown WITHOUT
smoothing: see docs/history.md for the task-by-task detail.

| Task | Score | Note |
|---|---|---|
| T1 — paginated extraction | 2/3 | 1 extraction failure |
| T2 — leave-request form | 3/3 | — |
| T3 — dynamic table | 3/3 | — |
| T4 — multi-hop search | 3/3 | — |
| T5 — download + calculation | 3/3 | — |
| T6 — authenticated session | 3/3 | — |
| T7 — impossible by construction | 2/3 | 1 harness infra timeout (not the agent) |
| T8 — Wikipedia | 1/3 (after retry) | 2 extraction failures, 0 context overflow once repetitions were made independent |
| T9 — Google/INSEE | 3/3 | — |
| T10 — books.toscrape | 3/3 | — |
| T11 — staleness probe | 3/3 | version checked live every time |

**Harness bug found and fixed on this campaign** (`31aacac`, see
docs/resolved-bugs.md): repetitions of the same task in `_run_campaign()`
shared their `thread_id` (`_derive_thread_id` hashes a fixed prompt,
identical across repetitions) — T8 rep1 overflowed the context (170285
tokens > 32768 on the TabbyAPI side, a real large Wikipedia page + several
plan/verify/judge cycles), and repetitions 2/3 then replayed the SAME
already-stuck thread, failing identically in 0.4s — wrongly reading T8 as
0/3 instead of 1 real failure. Fixed (unique marker per repetition) and
verified live (2 distinct threads, two fully independent runs) before
replaying T8 alone for the corrected score above. The real context
overflow on long tasks remains a side effect to address — confirms the
need for Phase 2 (history compaction), next in `PLAN.md`'s order.

**Lessons learned**: (1) a mechanism that "sees" a terse tool result
(action confirmation) without ever seeing the real state it produces
judges in a vacuum — true for verification AND for (re)planning, found
twice separately before being fixed in both places; (2) fixing one
grounding flaw can expose another right behind it (the reference
confusion didn't exist before the planner could see real content) — every
fix in this effort was verified by a dedicated live probe, never assumed
correct from unit tests alone; (3) a measurement false negative (the
harness itself) can look like a regression in the agent — the audit log
(`GET /audit?thread_id=`) was the only way to tell the two apart each
time.

**v2 suite (proposed, approved, fixtures not yet built)**: 8 tasks
covering multi-site/long tasks, ambiguity to resolve, 2 prompt-injection
traps (foreshadowing Phase 3 — failure expected as long as PromptGuard
doesn't exist), and tasks with real COMMIT actions (cancellation,
deletion) to exercise the validation pipeline under real conditions. New
point zero assumed, v1/v2 comparisons forbidden. Detail in
`docs/briefs/phase-1-coeur-cognitif.md`.

### Post-action observation: history and current mechanism

Three successive versions (see docs/history.md, "latency fix 1/2" then
"1/2-bis" then "1/2-ter") before the current one: a separate LLM call
(`verify_action`, costly) -> a text marker `[CONSTAT: ...]` in the next
turn's reply (too fragile, often omitted) -> a mandatory dedicated tool
call `report_and_act` (real measured reliability ~9%, the model didn't
coordinate two tool_calls in the same turn) -> **current, merged
mechanism**: `constat_precedent` (`atteint`/`non_atteint`/`sans_objet`)
is a REQUIRED parameter of the schema of EVERY real tool
(`_inject_constat_param`, `app/graph.py`, gated on
`VERIFICATION_ENABLED`) — a single tool call carries both the action and
its observation. `report_and_act` remains the fallback tool for the sole
case with no real action (plain-text reply). INVERTED degradation
(missing/malformed observation -> `sans_objet`, attempt budget
unchanged, counted in `constats_inexploitables` rather than charged as a
failure) and a permanent COVERAGE judge (`verification_opportunities`/
`verification_exploitable`, audit log `role="verification"`) — observed
latency tradeoff: this schema, expanded across ~64 tools on every turn,
has a measurable prompt cost (see docs/history.md for the exact numbers),
still an open matter.

### Temporal awareness (PLAN.md Phase 1, point 7)

Planned as early as Phase 0 (T11 probe, "what is the latest stable Python
version?") but never built until the latency effort — implemented after
directly diagnosing the failure (the model decided to verify via the web
but queried `browser_extract` with a version prefix drawn from its own
frozen knowledge, missing the version actually displayed):
- `_date_directive()` (`app/graph.py`): date injection on EVERY turn, DAY
  granularity only (never the time, to preserve the ExLlamaV3 prefix
  cache), placed at the end of the static system block. `TZ` timezone
  (`docker-compose.yml`, default `Europe/Paris`).
- `PEREMPTION_DIRECTIVE`: instruction to verify any volatile fact
  (versions, prices, news, roles, service status) via the web rather than
  answering from memory, **and** to never inject an already-assumed value
  into the verification query itself (search for a neutral term, not a
  precise version number already assumed) — otherwise a real page that
  also mentions old values (release history) confirms the bias instead of
  correcting it.

Measured result: T11 3/3 on the full campaign (0/3 on the 3 previous
campaigns).

### Bulk verification (`BULK_CHECK_DIRECTIVE`, bulk mode of `browser_extract`)

Found while investigating T1 (see docs/history.md): when the information
sought only appears on detail pages (never the listing) and several must
be checked, page-by-page navigation exhausts the iteration budget before
everything is even checked — the model would end up guessing a URL
(rightly blocked by the anti-fabrication guardrail). First fixed via
`browser_evaluate` (a `fetch()` loop written by the model,
`TIER_SENSITIVE`/`NEVER_GRANTABLE`, see `approval_policy.py`): it worked
(T1 3/3, 0/3 on previous campaigns, 5-6 tool calls per run vs. 20-30+
before) but was fragile — dependent on the model writing correct JS every
time, for a need that never actually required arbitrary code.

`browser_extract` (`services/mcp-client/app/main.py`) now accepts an
optional `urls` parameter (bulk mode): same FIXED JS template as the
single-page search (`fetch()` + `DOMParser` + the same text-node walk,
per URL), `TIER_READ` — the model only supplies the list of URLs, never
code. Failure on an individual URL (network, cross-origin CORS) is
captured per page, never propagated to the whole batch.
`BULK_CHECK_DIRECTIVE` now points to this parameter rather than to
`browser_evaluate`.

### Campaign tooling (`scripts/run-campaign.sh`)

Runs the harness end-to-end, zero intervention between launch and the
report: duration estimate (current median per task x tasks x
repetitions, see `DURATION_ESTIMATE_CACHE.json` — a rolling ESTIMATE
cache, not a history, see below) -> preamble
(`campaign_preflight.run_preflight`: real LLM readiness — a completion
call, not just a `/health` check — THEN agent/mcp-client tool schema
sync) -> campaign -> report written -> completion notification (`.DONE`
file always; `ntfy`/mail on top if `NTFY_TOPIC`/`MAIL_TO` are set).

**Campaign persistence (`tests_integration/campaign_persistence.py`)**:
following an inventory finding (see docs/history.md, "PERSISTENCE
INVENTORY" then "CAMPAIGN PERSISTENCE") showing that nothing survived a
campaign beyond the Markdown prose, every campaign now writes
`campaign-<timestamp>-<label>.json` (never rewritten afterward) next to
the report: context metadata frozen at launch (git commit, image ID of
the `langgraph-agent`/`mcp-client`/`tabbyapi`/`playwright-mcp`
containers, model actually loaded on the TabbyAPI side via `GET
/v1/model`, effective env flags of the `langgraph-agent` container) + one
line per run (`thread_id` — a direct join key with `/workspace/.audit`,
no field to add on the `audit_log.py` side — status, classified failure
cause, tool_calls, observation coverage, duration) + a RAW TabbyAPI
sample per logged request (not just the aggregate). `_write_report` (the
existing Markdown, unchanged in appearance) is now a VIEW: rendered from
a re-read of this JSON, never directly from in-memory data — the sole
source of truth.

Factual correction made during this effort (CLAUDE.md #8): TabbyAPI
(verified in the `mjolnir-agent-tabbyapi` image,
`/app/endpoints/*/router.py`) does NOT expose a Prometheus `/metrics`
endpoint, unlike llama-server (see Observability below and the comment
already present in `docker-compose.yml`, `dashboard` service) — a
"before/after" reading on this endpoint couldn't have retrieved
anything. The persisted samples therefore come from the container's log
text (regex on "N tokens generated in ... Process: X cached tokens and Y
new tokens at Z T/s"), the only real per-request performance source
available — hence also the `logging` config (max-size/max-file) added to
the `tabbyapi` service in `docker-compose.yml`: these logs must no longer
disappear at the whim of a Docker daemon default stricter than expected.

**Bounded backfill** (`tests_integration/backfill_campaigns_index.py`): a
one-off script, run once for campaigns predating this mechanism —
reconstructs an APPROXIMATE time window per campaign from the dates
already present in the Markdown reports/`.DONE` files
(`campaigns-index.json`). Doesn't resurrect any lost metric, only makes
`/workspace/.audit` (never purged) retroactively navigable by time
window.

```
scripts/run-campaign.sh                      # full campaign (11 tasks x 3)
scripts/run-campaign.sh --tasks T1,T7,T11    # targeted smoke, fast iteration
scripts/run-campaign.sh --tasks T7 --reps 1  # minimal smoke
```

**Protocol**: smoke mode (`--tasks`) is for ITERATING fast on a fix — n
reduced, no statistical significance to decide a pass/regression
threshold. Only the full campaign (3 repetitions, 11 tasks) counts as the
reference measurement for a checkpoint. Found under real conditions (see
docs/history.md, "campaign tooling"): LLM readiness bit once —
`docker compose up --build` had recreated TabbyAPI at the same time a
campaign was starting, which then ran ~20s too early against a server not
yet listening (30 near-instant failures, no assertion to flag it) — hence
its systematic check at the head of the preamble now.

## Visual-only content: tool description, not detection (effort 3)

**Why**: some page content is unreadable by any DOM channel
(`browser_snapshot`/`browser_extract`) — canvas 2D, WebGL, `<img>` with no
`alt`, a native PDF viewer — confirmed structural, not a tooling gap, by
the visual-channel feasibility probe (`docs/architecture/visual-channel-feasibility.md`,
cases VP1-VP4). Playwright's own `browser_take_screenshot` already reads
all four cleanly — the open question was never capability, only how the
agent finds out it needs it.

**A proactive OCR-enrichment mechanism was built, then abandoned before
going live** (`_detect_visual_signal`/`_maybe_enrich_with_ocr`,
`app/graph.py`, removed — see docs/history.md, "PROBE VISUEL — SIGNAL
BROWSER_SNAPSHOT"). Its premise — pattern-match an already-fetched
`browser_snapshot`/`browser_extract` result's TEXT for a hint that a
visual-only element is present — was checked empirically against
`fixture-visual-probe` before going live, and falsified for 3 of its 4
target patterns: canvas, WebGL, and an `<img alt="">` leave **zero**
trace in `browser_snapshot`'s accessibility-tree text. A page with a
canvas is text-identical to one without — there is nothing to grep for.
A candidate heuristic (matching accessibility role `img`) was also
checked against a control case (VP7, inline SVG text) and produced a
proven false positive: SVG text is wrapped in an `img` role too, and
needs no capture at all. No after-the-fact heuristic survives this
evidence.

**What replaced it: the routing decision moved into the tool's own
description**, evaluated by the model BEFORE it commits to a channel,
rather than by the wrapper AFTER the fact. `mcp-client`'s
`_tool_description_with_appends` (`services/mcp-client/app/main.py`)
appends a routing hint to `browser_take_screenshot`'s real, upstream
Playwright description: use this tool when `browser_snapshot` doesn't
carry the expected information — canvas/WebGL content, an alt-less
image, or a PDF opened directly. This is the only viable fix for
canvas/WebGL/alt-less-img: there's no signal in the RESULT to react to,
only a decision the caller has to make in advance.

**The one pattern that IS empirically detectable gets a real, structural
fix**: a native PDF navigation (VP4) came back with an entirely EMPTY
accessibility tree — no page title line even, unlike canvas/WebGL/img
which sit on an otherwise normal page. `mcp-client`'s
`_flag_empty_snapshot` detects this specific shape (an empty
` ```yaml ``` ` block in `browser_snapshot`'s own response) and appends a
redirect hint to the SAME result — the call still succeeds, this is
guidance, not a block. Covered by unit tests using the real captured VP4
text as a fixture, not a guessed shape.

**Judge**: family E's own E2 (visual-only task) re-run, 3 repetitions —
its earlier 1/3 was an audit-verified channel confusion between
GhostDesk's `screen_shot` and Playwright's `browser_take_screenshot`,
which the corrected tool description should resolve now that GhostDesk
is gone and the description names the right tool explicitly. E1/E3
non-regression, in particular E3 staying at 0/3 capture recourse — the
description must sharpen routing for the cases named, not turn
`browser_take_screenshot` into a reflex.

**`ocr-service` (`services/ocr-service`) is unaffected by this change but
currently has zero callers** in the codebase: it was built as the
enrichment mechanism's text-extraction backend, and that mechanism is
gone. The container still builds and serves `POST /ocr` (PaddleOCR,
French+English, models downloaded at build time — see
`services/ocr-service/Dockerfile`), kept deployed as a standalone
capability rather than removed, pending a decision on whether it has a
future role (e.g. effort 8's visual-only navigation mode) or should be
retired — an open question, not resolved here.
