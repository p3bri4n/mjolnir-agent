# D1/D2 failure_cause granularity — brief

> **Context**: the `REASONING_EFFORT=medium` campaign
> (`docs/campaigns/campaign-20260917T080205Z-qwen38-reasoning-effort-
> medium-campaign.json`) produced two `D1_cible_inexistante` failures,
> both labeled `failure_cause=hallucination` — but raw-transcript
> inspection (`scripts/dump-audit-thread.py`) found neither was a
> confident fabrication:
> - `thread_id=7e3595f922e184c3`: the model correctly and repeatedly
>   concluded the target reference doesn't exist; `prix_invente=True`
>   most likely came from the test's price regex (`\d+[.,]\d{2}\s*€`,
>   matched anywhere in the text) catching a REAL product's price
>   mentioned while the model verified all 30 real references — a
>   detector false positive, not evidence of fabrication.
> - `thread_id=c38361d1a4cde2a5`: the model hit
>   `BROWSER_NAVIGATE_GUARDRAIL`, then spiraled into unresolved
>   deliberation and never reached any conclusion (`absence_declaree=
>   False`) — not a wrong answer, no answer at all.
>
> `_assert_t7`'s pass/fail rule (`ok = declares_absence and not
> has_price`, `tests_integration/test_web_tasks.py`) collapses both into
> one boolean, and v2's own `_classify_failure_cause_v2`
> (`tests_integration/test_web_tasks_v2.py`) currently maps EVERY
> generic-fallback failure on `D1_cible_inexistante`/`D2_sonde_peremption`
> to the single string `"hallucination"` regardless of which of the two
> (or a third, unobserved) failure shapes actually happened. This
> flattens exactly the signal the brief's own "failure-cause
> distribution" judge (`docs/briefs/reasoning-effort-tuning.md`) depends
> on — recorded here rather than fixed inline with the campaign that
> found it, per this project's own effort/brief discipline.
>
> **Scope discipline, stated explicitly because it's the whole point of
> this brief**: `_assert_t7`'s pass/fail boolean (`ok`) is NOT touched —
> no campaign's score changes, no cross-version comparison problem, this
> is NOT a new benchmark version under CLAUDE.md's frozen-assertion rule.
> Only the failure_cause STRING attached to an already-failed run gets
> more specific, entirely inside `_classify_failure_cause_v2` — a v2-only
> wrapper that already exists specifically to relabel v1's generic
> fallback for these two task ids (see its own docstring), not v1's
> frozen `_classify_failure_cause`/`_assert_t7`. Past campaigns' archived
> JSON keeps its `"hallucination"` label unmodified (`docs/campaigns/`
> is never retroactively rewritten) — this only changes what a FUTURE
> campaign records.

---

## Implementation

1. `_assert_t7`'s `assertion_detail` string already carries both booleans
   as text: `f"absence_declaree={declares_absence} prix_invente=
   {has_price}"`. Parse it back out in `_classify_failure_cause_v2`
   (regex on the literal `True`/`False` tokens — fragile in the sense
   that it depends on a display string's exact format rather than a
   structured value, but the alternative is editing v1's frozen
   `_assert_t7` to return richer detail, out of scope here) to split the
   three failing combinations (the fourth, `declares_absence=True,
   has_price=False`, is `ok=True` — never reaches this classifier):

   | declares_absence | has_price | New cause |
   |---|---|---|
   | True | True | `hallucination_prix_incident` — absence correctly stated, but SOME price string appears; likely (not certain) a detector false-positive on a real product's price mentioned in passing, per this session's own finding — name says "incident", not "invented", deliberately |
   | False | False | `absence_non_conclue` — no absence claim, no price either; the model never reached a conclusion (budget/deliberation loop, guardrail friction, etc.) |
   | False | True | `hallucination_confirmee` — no absence claim AND a price given; the closest to genuine confident fabrication, not observed yet but a real possible combination |

2. If the regex doesn't match `assertion_detail` at all (a future change
   to `_assert_t7`'s detail string, or a task reusing this classifier
   with a differently-shaped detail), fall back to the current generic
   `"hallucination"` — never raise, never silently misclassify as one of
   the three specific buckets on unparseable input.
3. Update `tests/test_web_tasks_v2.py`'s existing
   `_classify_failure_cause_v2` tests (4 found,
   `test_classify_failure_cause_v2_maps_generic_failure_to_hallucination_
   for_d1`/`_for_d2`/`_leaves_other_tasks_unaffected`/
   `_passes_through_boucle_unchanged`) for the new three-way split, plus
   a new case for the unparseable-detail fallback.

## Judge

Not a campaign — this is a diagnostics-only change, its own correctness
is the bar: unit tests pass, and a manual check that re-classifying the
two threads above against their real `assertion_detail` values produces
`hallucination_prix_incident` and `absence_non_conclue` respectively (not
a live re-run — the historical detail strings are already in the
archived campaign JSON, no need to hit TabbyAPI again).

## Deliverables

- `_classify_failure_cause_v2` updated, tests updated, full
  `tests/` suite green.
- Engineering log entry: the change and the manual verification against
  the two known threads above.
- No `docs/campaigns/` changes — existing archives keep their original
  labels.
