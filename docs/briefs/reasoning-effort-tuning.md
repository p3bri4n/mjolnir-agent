# Qwen3.8 `reasoning_effort` tuning — brief

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
