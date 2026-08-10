# Quantisation evaluation — 5.0bpw vs current — brief

> **Context**: a 5.0bpw EXL3 build of Qwen3.6-27B has been downloaded. The
> current production model is a lower-bpw build of the same model. The
> question is not "is 5bpw better" but **what does it cost, what does it buy,
> and does it foreclose anything**.
>
> **Single variable**: only the model changes. Same benchmark, same flags,
> same fixtures, same preflight. Any other change invalidates the comparison.
>
> **Frame it honestly before measuring**: almost every point gained in this
> project came from scaffolding, not from the model. Nothing so far shows
> reasoning quality is the limiting factor. This evaluation exists to test
> that assumption, not to confirm a preference.

---

## Phase 0 — Establish the current baseline first

Record, from the most recent reference campaign (or re-run it if flags have
changed since): CuP, per-family scores, median time per task, tokens per
task, tool calls per task, peak VRAM per GPU, decode throughput.

Also record the **current model's exact identity** (repo, revision, bpw,
file digests) in the campaign metadata. A comparison whose baseline model is
unlabelled is worthless six months on.

## Phase 1 — Static measurements (no agent calls)

Load the 5.0bpw build alone and measure:

1. **Peak VRAM per GPU**, model + vision tower + MTP + KV cache at the
   production context size. Compare against the current build.
2. **Free VRAM remaining**, per GPU, under production settings — this is the
   number that decides everything downstream.
3. **Raw throughput**: prefill tokens/s and decode tokens/s on a fixed
   prompt, with and without MTP. Expect decode to drop roughly in proportion
   to the weight volume read per token: 5.0 vs 3.5bpw is ~+43% of weights,
   so a decode drop in the 25–30% range is the expected order of magnitude —
   **measure it, do not assume it**.
4. **MTP acceptance rate**, if the server exposes it. Speculative drafting
   may amortise differently at higher precision; this is a real unknown.

🧑 Checkpoint: VRAM figures reviewed before spending a campaign.

## Phase 2 — Does the remaining VRAM foreclose anything?

The freed-memory pool has three declared claimants, and they compete:

| Claimant | Rough need | Consequence if VRAM is short |
|---|---|---|
| Quarantined LLM (security plan, Phase 5) | 0 if same-model/separate-context; ~5–8 GB for a small dedicated model | Fall back to same-model separate context, or CPU (slow prefill — the dominant cost for that role) |
| Mjolnir critic model (multi-agent) | ~5–8 GB for genuine weight diversity | Falls back to same-model critic, losing the diversity that justified it |
| Headroom for context growth / longer tasks | variable | Hard ceiling on context size |

Write the answer explicitly: **with 5.0bpw loaded, which of these three
remain possible?** If the answer is "none", that is the decisive finding and
the campaign in Phase 3 is optional — the trade is then reasoning quality
against two planned capabilities, and that is a design decision, not a
measurement.

## Phase 3 — Full campaign, single variable

Run the complete v2 suite, 3 repetitions (family F at 2), with the same
preflight and the same flags as the baseline.

**Judges:**

- **CuP and per-family scores** — the headline. A gain under ~2 points on
  this suite is within documented run-to-run noise; do not read it as a win.
- **Failure-cause distribution** — more informative than the score. The
  question is whether failures currently attributable to *reasoning* (badly
  decomposed plans, wrong extraction despite correct navigation, absurd
  strategies) decrease. If failures stay mechanical or perceptual, a more
  precise model cannot fix them, and that settles the matter regardless of
  the score.
- **Median time per task and tokens per task** — the cost side. Latency was
  brought from 145s to 45s over three campaigns; giving part of that back
  needs to buy something measurable.
- **Family B CuP specifically** — policy compliance may or may not track
  capability. Worth reading separately.

## Phase 4 — Intermediate quantisation, only if warranted

Do **not** test intermediate levels pre-emptively. Test one only if Phase 3
shows a real capability gain that Phase 2 shows is unaffordable. In that
case the intermediate build answers a precise question — how much of the gain
survives at a bpw that leaves the VRAM pool intact — and it is worth one more
campaign.

If Phase 3 shows no meaningful gain, stop: the current build stays, and the
finding is recorded.

## Decision table (fill before reading results)

| Phase 3 result | Phase 2 result | Decision |
|---|---|---|
| Clear gain (>2 pts CuP or a visible shift in reasoning-attributable failures) | VRAM pool intact | Adopt 5.0bpw |
| Clear gain | Pool consumed | Test an intermediate build (Phase 4) |
| No meaningful gain | either | Keep current build; record the finding |
| Gain but latency materially worse | either | Weigh against the latency judge; MTP acceptance may explain part of it — investigate before deciding |

## Deliverables

- `docs/campaigns/` entry for the 5.0bpw campaign, with model identity in
  metadata.
- A short section in `docs/architecture/inference-backend.md`: VRAM budget
  per build, throughput, and what the remaining pool allows.
- An entry in the engineering log stating the decision **and the reasoning**,
  including the case where the answer is "no change" — a measured non-result
  is a result.
