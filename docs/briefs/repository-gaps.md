# Repository gaps — corrective brief

> Six gaps found during a full-repo review (2026-08-17). None is urgent
> engineering; two are urgent *preservation*. One commit per nature of
> change, as usual.

---

## 1 — Commit the two engineering notes (highest priority)

Two long-form notes were written but never left the conversation they were
drafted in. They exist nowhere in the repository. Create `docs/notes/` and
add them, in English:

- `docs/notes/llamacpp-dual-gpu.md` — "Six weeks of llama.cpp on two
  mismatched GPUs": the four build traps (CUDA VMM allocator breaking the
  Docker link step, sm_120 requiring CUDA ≥ 12.8, missing RPATH,
  `--flash-attn` becoming a value-taking option), tool calls trapped in the
  reasoning span before `</think>`, the `reasoning` vs `reasoning_content`
  divergence, then the crash diagnosis: hypotheses falsified in order
  (context checkpoints, inter-GPU copies at restore, pigtail power,
  power limit), `CUDA_LAUNCH_BLOCKING` revealing `launch_mul_mat_q`,
  Xid 31 on copy engine CE4, single-GPU isolation both cards clean, the
  `--ubatch-size` threshold (stable ≤256, crash at 512), the permanent
  workaround at 128 and its ~+34 % latency cost. Honest caveat: never
  reproduced on upstream vanilla, so no issue was ever filed. Epilogue:
  the same two cards under ExLlamaV3 do not crash — the cross-engine
  counter-witness.

- `docs/notes/agent-benchmarking.md` — "Measuring an autonomous agent:
  what eleven campaigns taught us": the instrument first and what it
  caught before its own first campaign (URL fabrication); the zigzag
  16 → 24 → 20 → 24 → 30 and the bundling mistake (a net −4 hiding a
  +5/−9); the honesty ceiling fixing T7 and breaking T5; the missing-
  capability theme (download path, `browser_extract`, `browser_inspect`,
  the dt/dd defect); the `[CONSTAT:]` marker at 18/33 and the 9 %
  coverage flattering zero; the ×8 latency passing a green checkpoint;
  the three false diagnoses from trusting self-narration; the four
  contamination incidents; and the compaction built ahead of its need.

Both notes: keep the incidents and the numbers. The value is in the
failures named, not in the trajectory. Do not smooth them.

Add a `## Notes` section to the README linking both, and cross-link each
note to the other at its end.

## 2 — Fix a doc/code contradiction in `project-status.md`

The document states the cognitive-core flags had their defaults "flipped
to `true` (measured and adopted)". `services/langgraph-agent/app/graph.py`
sets `PLANNER_ENABLED` to `"false"` by default. Verify all four flags in
code, then rewrite that paragraph to state:

- the cfg1/cfg8 sweep result (15/15 vs 13/15, +76 % time, identical
  tool-call count) and the decision it triggered;
- the current real state of each flag's default;
- where the removal stands: PR1 (defaults to false + full v2 campaign)
  done or partial, PR2 (removing `plan_task`, `verify_action`, the judge,
  their directives and tests) not done — `plan_task` and `verify_action`
  are still in the graph;
- `PLAN_VALIDATION_ENABLED`'s exception (safety value, no LLM call) and
  whether it is de facto inert without a planner.

This is the class of error already caught twice (README security section,
`PLAN.md` context) — a document that says where we are must not contradict
the code.

## 3 — `docs/history.md`: header, index, rename

6393 lines, no document title (the file opens on an entry), no index.
`CLAUDE.md` says to consult it by targeted search, which is impossible
without a table of contents.

1. Rename to `docs/engineering-log.md` (`git mv`, own commit, no content
   change). It is a lab notebook, not a version changelog — `CHANGELOG.md`
   stays free for that use.
2. Add a title and a **dated index** at the top: one line per entry,
   generated from the existing `## ` headings. Mechanical.
3. Record the entry convention in `CLAUDE.md`:
   `## YYYY-MM-DD — <effort>: <title>`, then context / measurements /
   verdict / decision.
4. Update every reference (`CLAUDE.md`, `README.md`, `PLAN.md`,
   `project-status.md`, briefs) in a separate commit from the `git mv`.

## 4 — Resolve the double claim of authority

`PLAN.md` declares itself authoritative in case of divergence, then points
to `docs/briefs/update-plan.md` as "the authoritative roadmap". Two
documents cannot both be the source of truth. Pick one:

- either absorb `update-plan.md`'s sequencing into `PLAN.md` and archive
  the brief with a status header;
- or have `PLAN.md` state explicitly that it is authoritative on delivered
  phases only, and that the forward roadmap lives in `update-plan.md`.

Preference: the first — `PLAN.md` is the documented entry point, and a
roadmap that lives in `docs/briefs/` will be missed.

## 5 — Audit-log blind spot: an entry and a check

`browser_extract` has **zero** occurrences in the audit log's
`"tool"`-keyed entries, which is how `scripts/analyze-tool-call-ngrams.sh`
counts calls. Documented in passing as a known blind spot; it deserves
more.

1. Entry in `docs/resolved-bugs.md`: symptom (a heavily used tool invisible
   to tool-keyed queries), what is known of the cause, status.
2. **Inventory which tools are affected** — is it `browser_extract` alone,
   or a whole class (wrapper-dispatched tools, tools added after the audit
   schema was fixed)? Zero runs needed.
3. State the consequence: any archive analysis that counts tool calls may
   be incomplete, including the n-gram script that informed effort 3's
   candidate list. Re-read that conclusion in this light.
4. Fix the logging so those calls are recorded under the same key as the
   rest, with a test asserting a wrapper-dispatched tool appears.

## 6 — Minor

`README.md`'s Documentation section lists `docs/lessons-learned.md` but
not `docs/methodology.md`. Add it.

---

## Order

1 first (preservation, no dependency), then 2 (a false statement about the
current state is actively misleading), then 5 (it casts doubt on an
analysis already used for decisions), then 3, 4, 6.
