# Autonomy — plan → act → verify → replan loop

Content moved as-is from README.md (restructuring effort, see docs/briefs/restructuration-et-anglais.md, phase 3), Supplementary OCR included (a grounding tool serving the same loop) — no rewrite at this stage.

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
`PLAN_VALIDATION_ENABLED`/`PLAN_JUDGE_ENABLED`) — **defaults FLIPPED to
`true`** since the final campaign (29/33, consistent with pre-cognitive-core
Campaign A at 30/33 — see docs/briefs/flags-du-coeur-cognitif.md and
docs/history.md): the cognitive core is measured and adopted, it's now
DISABLING it that must be explicit. See each one's detail below.

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
default `true` — Iteration 2): **only has an effect if `PLANNER_ENABLED`
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
`PLAN_JUDGE_ENABLED` (default `true` since docs/briefs/
flags-du-coeur-cognitif.md — measured withdrawal clause, see docs/history.md
Iteration 3: it did really veto a plan the heuristics let through), an
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

## Supplementary OCR (`services/ocr-service`)

**Why**: the default served VLM (Qwen3.6 MoE) reasons well but localizes
poorly — its visual grounding (aiming at the right on-screen pixel for an
element) remains imprecise, with no dedicated OCR or UI-element detection
(see Known, accepted limitations below). `ocr-service` compensates by
giving the agent EXACT text coordinates via two MCP tools: `find_text
(query, fuzzy=true)` (matches sorted by confidence, empty list if none —
never an error) and `read_screen()` (all detected text, capped at 80
elements). A grounding instruction is injected into langgraph-agent's
system prompt (`GROUNDING_DIRECTIVE`, `app/graph.py`): prefer `find_text`
over visual estimation to click on text, reserve the latter for elements
without text (icons).

Persistent HTTP MCP server (Streamable HTTP, `OCR_AUTH_TOKEN` bearer), on
the same model as `desktop`/GhostDesk on the `mcp-client` side — not a
container spawned on demand. `find_text`/`read_screen` are read tier
(`approval_policy.py`): pure read, no side effect, auto-approved and
silent.

**Capture**: `ocr-service` itself connects over Streamable HTTP to
GhostDesk (internal `agent-net` network, `GHOSTDESK_AUTH_TOKEN` bearer,
explicit `format="png"` — no dependency on llama-server's native WebP
decoding, irrelevant here) to call `screen_shot` on every
`find_text`/`read_screen`. No image ever passes through `mcp-client` nor
the LLM for this flow, entirely internal to `ocr-service`.

**Coordinate mapping — a classic source of off-target clicks**: PaddleOCR
works in the capture's real pixels, whereas `mouse_click` on the
GhostDesk side expects the normalized 0-1000 frame (same frame as
`GHOSTDESK_MODEL_SPACE` on the `mcp-client` side, see Human supervision
below). `ocr-service` therefore systematically converts its coordinates
before answering (`x_norm = round(x_px * 1000 / image_width)`, see
`app/coords.py`) — without this conversion, the coordinates returned by
`find_text` would be in pixels while the model (and GhostDesk) interpret
them as 0-1000, guaranteeing off-target clicks. `OCR_COORD_SPACE` (default
`"1000"`) disables this conversion (`"pixels"`) if the caller itself
works in pixels.

**PaddleOCR**: PaddleOCR groups French and English under a single
recognition model (shared Latin alphabet), no need to run two separate
OCR passes for this project. Models downloaded **at build time** of the
Docker image (`ARG OCR_LANGS`, see `services/ocr-service/Dockerfile`),
never on the first call — avoids a network access and several seconds of
latency in production.

Explicitly out of scope (future iteration): text-free icon/UI-element
detection (OmniParser-style), Set-of-Marks annotation of screenshots, GPU
OCR, caching of results between calls.
