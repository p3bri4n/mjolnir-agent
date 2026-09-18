# Qwen3.8 `reasoning_effort` tuning — brief

> **Status: CLOSED, ADOPTED (2026-09-18).** `REASONING_EFFORT=medium` is
> now the default (`docker-compose.yml`, `campaign_preflight.py`'s
> `EXPECTED_AGENT_FLAGS`, `docs/architecture/inference-backend.md`) —
> decisive measurement met the decision table's adopt row, and the D1
> confirmation follow-up closed the one open wrinkle clean (zero
> confirmed fabrication across two n=5 campaigns). The two context-
> overflow mitigation probes explored alongside this brief
> (`HISTORY_DIFF_ENABLED`, `max_seq_len`/`cache_size`) are tracked
> separately: `max_seq_len`/`cache_size` (40960/81920) is ALSO now
> adopted as the default (`services/tabbyapi/config.yml`);
> `HISTORY_DIFF_ENABLED` stays off pending a full v2 regression campaign
> — not yet decided.

> **Context**: the `ADAPTIVE_THINKING=true` follow-up campaign
> (`docs/engineering-log.md`, "Qwen3.8-27B evaluation follow-up —
> ADAPTIVE_THINKING=true campaign") bought a real -21% cumulative time but
> at the cost of a real, mechanistically-confirmed score regression
> (58/62 → 53/62): full reasoning suppression on "safe" turns removed the
> model's ability to notice and correct a dead end. `T10_books_toscrape`
> (a frozen regression alarm) froze into 18 byte-identical
> `browser_navigate` calls in a row; `A1_reconciliation_croisee` wandered
> through broad, unfocused exploration and ran out of budget just short
> of using a page it had already found. Both burned their full
> `MAX_TOOL_ITERATIONS` budget as `failure_cause=boucle`.
>
> Qwen3.8's own model card (`models/qwen3.8-27b-exl3-4.50bpw/README.md`,
> verified locally) already warns about exactly this shape of failure for
> low `reasoning_effort` in multi-turn agentic tasks — full suppression
> (`enable_thinking: false`) is a more extreme cut than any
> `reasoning_effort` level, consistent with how severe the regression
> was. `reasoning_effort` is official, per-request, and never suppresses
> reasoning entirely — `xhigh` (default) / `medium` / `low`. Candidate:
> keep SOME deliberation on every turn (no suppression gate,
> `ADAPTIVE_THINKING` off) at `medium`, see if it recovers most of the
> `ADAPTIVE_THINKING` latency win without breaking the two tasks that
> failed.
>
> **Single variable**: `reasoning_effort` only. `ADAPTIVE_THINKING` stays
> `false` (matching Phase 3's baseline flag state) — this is NOT a
> combination of the two mechanisms. A `medium` + `ADAPTIVE_THINKING`
> combined experiment, if this one doesn't fully close the gap, is its
> own separate measurement, not folded in here.
>
> **Not yet wired in code today**: `_should_suppress_thinking`/`call_llm`
> (`app/graph.py`) only ever sends `enable_thinking`. `reasoning_effort`
> needs new code — brief before the first line, per this project's own
> discipline.

---

## Phase 0 — Confirm the wire format empirically (prerequisite, its own judge)

**Why**: the model card's own Python example passes `reasoning_effort`
as a **top-level** `chat.completions.create()` kwarg (sibling of
`messages`), NOT nested inside `extra_body.chat_template_kwargs` the way
`enable_thinking`/`preserve_thinking` are shown. Our own working
`enable_thinking` mechanism instead passes it as a bare key of
`extra_body` (`bound_llm.bind(extra_body={"enable_thinking": False})`,
`_should_suppress_thinking`) — TabbyAPI's own convention, confirmed
empirically (`docs/engineering-log.md`, "ADAPTIVE_THINKING mechanism
confirmed on Qwen3.8"), not the vLLM-style nesting the model card shows.
Do not assume `reasoning_effort` follows the same convention as
`enable_thinking` on THIS backend without checking.

1. Direct, non-streaming call to TabbyAPI, bypassing langgraph-agent
   (same technique as `scripts/smoke-adaptive-thinking.sh`'s Step 0):
   try `reasoning_effort` as a bare `extra_body` key, and if that errors
   or is silently ignored (reasoning volume unchanged vs. no override),
   try nesting it — settle which one TabbyAPI actually honors.
2. Confirm `medium` measurably reduces `reasoning_content` length vs. no
   override (default `xhigh`) on the same fixed prompt — a cheap,
   deterministic sanity check before touching `call_llm`.

🧑 **Checkpoint**: wire format confirmed, reasoning-volume delta observed
— before Phase 1.

## Phase 1 — Thread `REASONING_EFFORT` through the main loop call

1. New env var `REASONING_EFFORT` (default: unset/empty string = no
   override, byte-for-byte unchanged behavior — same "default changes
   nothing" convention as every other flag in this project). Accepted
   values: `xhigh`, `medium`, `low` (matching the model's own three
   levels) — anything else is a startup-time config error, not a silent
   fallback.
2. In `call_llm` (`app/graph.py`), apply it via `bound_llm.bind(...)`
   using the wire format Phase 0 confirmed, **unconditionally** on every
   invocation when set — no turn-based gate like
   `_should_suppress_thinking`'s approval-tier condition. This is a
   different mechanism, not a variant of `ADAPTIVE_THINKING`; keep the
   two independent in the code, not just in the campaign config, so a
   future combined experiment is a clean composition rather than a
   rewrite.
3. Docstring/comment for the new code block: WHY (the two failure
   mechanisms observed, one-line pointer to the engineering-log entry —
   not the detail copied in, per the docstring contract).
4. Tests: a new `test_graph.py` case asserting the bind call fires with
   the right `extra_body` shape when `REASONING_EFFORT` is set, and that
   it's a no-op (no `.bind()` call at all) when unset — same assertion
   style already used for `ADAPTIVE_THINKING`/`enable_thinking`.

🧑 **Checkpoint**: mechanism implemented and unit-tested — before Phase 2.
Do not let Phase 0/1's own verification double as Phase 2's campaign
judge.

## Phase 2 — Full campaign, single variable

`REASONING_EFFORT=medium`, `ADAPTIVE_THINKING=false` — compare against
the Phase 3 Qwen3.8 result already in hand
(`campaign-20260916T173945Z-qwen38-eval-phase3-qwen38.json`, `xhigh`
default, `ADAPTIVE_THINKING=false`). No need to rerun that baseline.

**Judges**:

- `T10_books_toscrape` and `A1_reconciliation_croisee` specifically —
  the two tasks that broke under full suppression. Recovering these is
  the primary bar; a score gain elsewhere doesn't compensate for a loss
  here.
- CuP and per-family scores overall — same ~2-point noise threshold as
  Phase 3.
- Cumulative `duration_seconds` and `prompt_tokens_total` vs. BOTH prior
  campaigns (Phase 3's `xhigh` baseline and the `ADAPTIVE_THINKING=true`
  campaign) — where does `medium` actually land between the two.
- Zero `failure_cause=boucle` on any task that didn't already show it at
  `xhigh` — a new loop anywhere else would mean `medium` still cuts too
  deep for that task shape.

## Decision table (fill before reading results)

| Phase 2 result | Decision |
|---|---|
| T10/A1 recover, meaningful time gain vs. `xhigh`, no new `boucle` failures | Adopt `REASONING_EFFORT=medium` as the new default |
| T10/A1 recover, but time gain is marginal (most of `ADAPTIVE_THINKING`'s win came from cases `medium` can't touch) | Record the finding; `ADAPTIVE_THINKING=true` stays rejected, `medium` alone may not be worth a permanent default change |
| T10/A1 still fail (or a new task does) | Reject; the failure mode isn't specific to full suppression — reconsider whether the underlying issue is task-shape (long tool-call chains) rather than reasoning depth at all |
| No meaningful time gain at all | Reject; keep `xhigh` default |

## Deliverables

- `docs/campaigns/` entry for the Phase 2 campaign.
- Engineering log entries for Phase 0 (wire-format finding) and Phase 2
  (the decision, stated without advocacy even if "no change").
- `docs/architecture/inference-backend.md` updated: `REASONING_EFFORT`
  alongside the existing `ADAPTIVE_THINKING` description, noting the two
  are independent mechanisms.

## Phase 2 follow-up — D1 confirmation campaign (judge frozen before running)

**Why**: Phase 2's own D1 result (1/3) is the one open wrinkle blocking
adoption. `docs/briefs/archives/d1-failure-cause-granularity.md` (closed)
built a finer-grained classifier and, applied to Phase 2's two D1
failures, read them as `hallucination_prix_incident` (detector false
positive) and `absence_non_conclue` (no conclusion reached) — neither a
confirmed fabrication — but that reading is inference on n=3, not a
dedicated measurement. This follow-up gets a real read with the granular
labels attached from the start, not reconstructed after the fact.

**Single variable**: none changed — same `REASONING_EFFORT=medium`,
`ADAPTIVE_THINKING=false` as Phase 2. Only `n` increases (3 → 5) and the
task set narrows to `D1_cible_inexistante` alone (T10/A1's recovery is
already settled by Phase 2, not re-tested here).

**Judge, declared before running**:
- `failure_cause` distribution across the 5 runs (via
  `_classify_failure_cause_v2`, already emits the three-way split for
  every future campaign — no reconstruction needed this time).
- Frozen reading: **zero `hallucination_confirmee`** among any failures
  → confirms the artifact reading, adoption case stands as Phase 2 left
  it. **One or more `hallucination_confirmee`** → the dip includes a real
  fabrication under `medium`, not just detector/deliberation noise —
  reopens the adoption question, weighed against Phase 2's score/time
  gains rather than auto-rejected.
- Raw pass/fail count is recorded but is NOT the primary bar here (n=5 on
  one task still has thin statistical weight on its own) — the
  failure_cause distribution is.

**Command** (single campaign, existing harness — no new code):

```bash
# only if the container isn't already running with REASONING_EFFORT=medium —
# check first: docker exec langgraph-agent env | grep REASONING_EFFORT
REASONING_EFFORT=medium docker compose up -d --force-recreate langgraph-agent

CAMPAIGN_EXPECTED_FLAGS_OVERRIDE='{"REASONING_EFFORT": "medium"}' \
  scripts/run-campaign.sh --suite v2 --tasks D1_cible_inexistante --reps 5 \
  --label "reasoning-effort-medium-d1-confirmation"
```

🧑 **Checkpoint**: report the failure_cause distribution and the frozen
reading above — before touching the adoption decision.

**Result (2026-09-18)**: n=5 valid (2 of the first 5 runs hit a genuine
context-window overflow, `openai.BadRequestError`/
`context_length_exceeded` at 33040 > `max_seq_len: 32768`, confirmed via
container logs — not a `REASONING_EFFORT` effect, retried per this
project's own cfg6-infra precedent) — 1 success, 1
`hallucination_prix_incident`, 3 `absence_non_conclue`, **0
`hallucination_confirmee`**. Frozen reading applies: confirms the
artifact hypothesis, adoption case stands as Phase 2 left it. Full
detail: `docs/engineering-log.md`, "reasoning_effort tuning, Phase 2
follow-up". Adoption decision: still pending, user's call.

## D1 context-overflow mitigation probe — HISTORY_DIFF_ENABLED (judge frozen before running)

**Why**: counting the D1 confirmation campaign's 2 excluded infra runs
back in (as real occurrences, not noise), `context_overflow` hit 2/7
attempts — a real, structural failure mode driven by D1's exhaustive-
verification shape (many `browser_extract`/`browser_navigate` calls
accumulating raw output, no compaction/diff by default).

**Single variable, chosen deliberately over the alternative**:
`HISTORY_DIFF_ENABLED=true` only — `EPISODE_COMPACTION_ENABLED` was
considered and ruled out by code inspection before running anything
(`app/graph.py:1908-1922`, `_apply_episode_compaction`): it only compacts
subtasks with `status in ("fait", "echoue")`, and `plan` stays `[]`
whenever `PLANNER_ENABLED=false` (the current default per Effort 2.4's
decisive measurement) — enabling compaction alone would be a guaranteed
no-op, and re-enabling the planner to make it non-inert would reopen a
question this project already closed decisively (cfg1 beats cfg8).
`HISTORY_DIFF_ENABLED`'s own `_apply_history_diff` has no such
dependency — it operates on raw message history only. `REASONING_EFFORT`
stays `medium` (the config the overflow was observed under, held
constant); the only change vs. the D1 confirmation campaign above is
`HISTORY_DIFF_ENABLED=false → true`.

**Judge, declared before running**:
- `context_overflow` occurrence rate vs. the 2/7 baseline above — the
  primary bar.
- `history_diff_applied_count`/`history_diff_messages_replaced` non-zero
  on at least the runs that go long enough to have mattered before
  (coverage judge — a flattering zero here would mean the mechanism
  never actually engaged, telling us nothing about whether it helps).
- Success/`absence_non_conclue`/`hallucination_prix_incident` rates
  recorded but secondary — n on one task is still thin, and this probe's
  question is specifically about context growth, not D1's overall score.

**Command**:

```bash
docker exec langgraph-agent env | grep -E '^(REASONING_EFFORT|HISTORY_DIFF_ENABLED)='
# if not already REASONING_EFFORT=medium / HISTORY_DIFF_ENABLED=true:
REASONING_EFFORT=medium HISTORY_DIFF_ENABLED=true \
  docker compose up -d --force-recreate langgraph-agent

CAMPAIGN_EXPECTED_FLAGS_OVERRIDE='{"REASONING_EFFORT": "medium", "HISTORY_DIFF_ENABLED": "true"}' \
  scripts/run-campaign.sh --suite v2 --tasks D1_cible_inexistante --reps 5 \
  --label "reasoning-effort-medium-history-diff-d1-probe"
```

🧑 **Checkpoint**: report the context_overflow rate and coverage counters
above before drawing any conclusion.

**Result (2026-09-18)**: clean positive — n=5, **0 `context_overflow`**
(vs. 2/7 baseline), coverage non-flattering on every run
(`history_diff_applied_count` 3-13). Mechanistically confirmed: the
longest run's `cached_tokens` stayed nearly flat (6656→11776 over 14
requests) against the baseline's comparable run climbing to 32515 before
erroring. Contrasts with Effort 4's "mixed" A1/A2 smoke — read as a
lack-of-opportunity finding on those short tasks, not a mechanism
weakness; D1's exhaustive shape gives it real material. Full detail:
`docs/engineering-log.md`, "D1 context-overflow mitigation probe —
HISTORY_DIFF_ENABLED". **Not yet a default-flip decision**: n=5 on one
task is thin — a full v2 regression campaign (all families,
`HISTORY_DIFF_ENABLED=true` as the single variable) is the natural next
step, not run yet.

## D1 context-ceiling mitigation probe — max_seq_len/cache_size (VRAM check required before any number is committed)

**Why**: `services/tabbyapi/config.yml`'s `max_seq_len: 32768` is the
literal ceiling `context_length_exceeded` hit (33040 > 32768). Raising it
removes the wall directly, orthogonal to any application-level mechanism
— but it's an infra/VRAM lever, not a behavior change, so it needs its
own live verification discipline (same as
`docs/briefs/archives/deterministic-gpu-placement.md`), not a guessed
number.

**Design constraint already recorded in config.yml's own comment,
re-stated here so it isn't missed**: `cache_size` (65536) is
DELIBERATELY set larger than `max_seq_len` (32768) — a 2x margin chosen
so a short `planner_llm`-style request can share the KV cache pool
alongside the main loop's long context without evicting it (the
"cache=0 hunt" fix, docs/engineering-log.md). **Raising `max_seq_len`
alone to match `cache_size` would erase that margin and reproduce the
exact problem it was chosen to prevent.** Both values move together,
preserving (at least) the current ratio — not a one-line edit.

**Single variable, isolated from the `HISTORY_DIFF_ENABLED` probe
above**: this campaign runs with `HISTORY_DIFF_ENABLED=false` (current
default) so the two candidate fixes are never measured entangled. If
both look promising independently, a combined config is its own later
measurement, not assumed additive.

### Phase 0 — live VRAM headroom check (prerequisite, own judge, requires the user's machine)

`docker exec tabbyapi nvidia-smi` (or `scripts/gpu-placement-smoke.sh`'s
own probe) BEFORE touching `config.yml` — confirms actual free VRAM per
GPU under the current `gpu_split: [10, 13]`. Candidate target,
provisional pending this check: `max_seq_len: 49152` (+50%),
`cache_size: 98304` (keeps the current 2x ratio exactly). Do not commit
to these numbers if the check shows insufficient headroom — scale both
down proportionally instead, never `max_seq_len` alone.

🧑 **Checkpoint**: report free VRAM per GPU before finalizing numbers.

**Result (2026-09-18)**: live check found less headroom than expected —
~4.24 GiB free on GPU0, ~4.85 GiB on GPU1 (vs. the ~5.3-5.4 GiB recorded
when `gpu_split: [10, 13]` was originally adopted, same model/split — the
~1 GiB/~300 MiB drift isn't explained by any config change and is
flagged as worth understanding later, not investigated here). Scaled
down from the provisional +50% to **+25%**: `max_seq_len: 32768→40960`,
`cache_size: 65536→81920` (ratio preserved). Committed to
`services/tabbyapi/config.yml` as an empirical test, not a calculation —
no validated VRAM/token ratio exists for this exact
model/quant/cache_mode combination (the one in the config's own comment
predates this model and is explicitly marked stale). Phase 1's
reload-stability check is the real verification.

### Phase 1 — apply, verify stable across reloads

Same discipline as `deterministic-gpu-placement.md`: edit `config.yml`,
`docker compose up -d --force-recreate tabbyapi` (bind-mounted config,
no `docker compose build` needed for this file — but DOES require a
full container recreate, the model reloads with the new cache
allocation), confirm 3 clean reloads land on consistent per-GPU VRAM
(`docker exec tabbyapi nvidia-smi` or
`campaign_persistence.collect_gpu_devices`), and a trivial
`/v1/chat/completions` call succeeds before running anything bigger.

**Result (2026-09-18)**: a real operational trap surfaced first —
`services/tabbyapi/config.yml` (tracked) isn't what the user's machine
actually mounts; `services/tabbyapi/config.local.yml` (gitignored,
machine-specific override, see its own header comment) is, and it still
had the old `32768`/`65536` values untouched by any git sync. No amount
of `git pull`/branch-checking could have caught this — worth its own
`CLAUDE.md` operational-trap entry (see below). Once corrected on
`config.local.yml` directly: 2 clean reloads, GPU0 identical both times
(11009 MiB), GPU1 within 63 MiB (11651/11714) — consistent with this
project's own already-established tolerance for that card. **Only 2
reloads, not 3, a deliberate deviation**: `gpu_split` here is explicit,
not autosplit — the instability 3 reloads were originally designed to
catch (placement drift) doesn't apply to a fixed-size cache allocation
under an already-fixed split. Free VRAM post-load: ~5.18 GiB (GPU0),
~4.61 GiB (GPU1) — comfortable margin, no OOM at any point.

### Phase 2 — D1 campaign, single variable

`REASONING_EFFORT=medium`, `HISTORY_DIFF_ENABLED=false`, new
`max_seq_len`/`cache_size`. n=5, `D1_cible_inexistante` only.

**Judge, declared before running**:
- `context_overflow` rate vs. the 2/7 baseline — should reach 0 given
  the longest observed prompt so far (33040 tokens) fits comfortably
  under the new 40960 ceiling.
- `cache_zero_requests` must NOT regress vs. the current baseline — a
  regression here would mean the margin got squeezed despite scaling
  both values, silently reintroducing the original cache-eviction
  problem this ratio exists to prevent.

**Decision table**:

| Result | Reading |
|---|---|
| context_overflow → 0, cache_zero_requests stable | Adopt the new sizing (own status header on this brief's config change, not a "measured behavior" item per CLAUDE.md's frozen list, but a persistent infra change worth recording like deterministic-gpu-placement.md was) |
| context_overflow → 0, cache_zero_requests regresses | Margin insufficient — revisit the ratio, not just the absolute numbers |
| context_overflow persists | The task-shape's growth outpaces even this margin — `HISTORY_DIFF_ENABLED` (or a `BROWSER_TOOL_OUTPUT_MAX_CHARS` reduction, not yet probed) remain the live candidates |

**Command** (after Phase 0/1 confirm specific numbers and a stable reload):

```bash
docker exec tabbyapi env  # sanity: config.yml is bind-mounted, not baked — no image rebuild needed
CAMPAIGN_EXPECTED_FLAGS_OVERRIDE='{"REASONING_EFFORT": "medium"}' \
  scripts/run-campaign.sh --suite v2 --tasks D1_cible_inexistante --reps 5 \
  --label "reasoning-effort-medium-max-seq-len-d1-probe"
```

🧑 **Checkpoint**: report Phase 0's VRAM numbers first — the specific
`max_seq_len`/`cache_size` values in this section are provisional until
then.

**Result (2026-09-18)**: matches row 1 — `context_overflow → 0/5`,
`cache_zero_requests` stable. Also 5/5 success (D1's best result all
session), but read cautiously: the longest run peaked at 25,235 tokens,
comfortably under even the OLD 32768 ceiling — this sample never
produced a near-miss the new ceiling actually had to save, so the
mechanistic proof is weaker than `HISTORY_DIFF_ENABLED`'s flattened
token-growth trace. The 5/5 score is more likely D1's known run-to-run
variance than a causal effect of a larger context window. Full detail:
`docs/engineering-log.md`, "D1 context-overflow mitigation probe —
max_seq_len/cache_size, applied and measured" (also covers a real
operational trap found along the way: `config.local.yml`, gitignored,
was the file actually in effect, invisible to every git-based check).
**Not adopted as a new default yet** — both this and `HISTORY_DIFF_
ENABLED` are measured, positive, and independent; the adoption decision
(for either, or `REASONING_EFFORT=medium` itself) is still pending.
