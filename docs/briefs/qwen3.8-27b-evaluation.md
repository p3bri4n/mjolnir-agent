# Qwen3.8-27B evaluation — brief

> **Context**: an EXL3 4.50bpw build of Qwen3.8-27B has been downloaded
> (`models/qwen3.8-27b-exl3-4.50bpw/`), alongside the still-untested 5.0bpw
> Qwen3.6 build from `quantisation-evaluation.md`. Architecturally the two
> models are the same family (`Qwen3_5ForConditionalGeneration`/`qwen3_5`,
> identical hybrid linear/full-attention layout, identical `config.json`
> beyond version metadata) — this is not a quantisation-only question, it is
> a genuine model swap, with its own template and its own trained behaviour.
>
> **This is not a single-variable comparison out of the box.** Two
> prerequisites are coupled to the swap and must ship, and be judged, BEFORE
> Phase 2's comparison — per `CLAUDE.md`'s measurement rules ("if a technical
> coupling forces two changes to ship together, declare it at the checkpoint
> beforehand, with one judge per mechanism"):
>
> 1. the installed `exllamav3` runtime (`1.1.0`, confirmed live,
>    `docs/engineering-log.md:134`) is OLDER than the quantizer that produced
>    the 3.8 build (`1.4.1`, `quantization_config.json`) — the dangerous
>    direction: a codebook-version mismatch does not error, it silently
>    mis-decodes;
> 2. `NO_THINK_DIRECTIVE` (frozen directive, `CLAUDE.md` "Measured
>    behavior") is implemented as a `/no_think` text prefix
>    (`_apply_adaptive_thinking`, `app/graph.py:2064`) — already confirmed to
>    have **no effect on this backend** by a direct TabbyAPI call bypassing
>    langgraph-agent (`docs/resolved-bugs.md:139`, `:193`). This is not a
>    swap-induced risk, it is a pre-existing dead mechanism the swap makes
>    worth fixing: Qwen3.8's template defaults to `reasoning_effort: xhigh`
>    on every call unless a real per-request parameter is threaded, raising
>    the cost of leaving it broken.
>
> Once both are closed, the comparison itself follows the same discipline as
> `quantisation-evaluation.md`: same benchmark, same fixtures, same
> preflight, only the model differs.

---

## Phase 0 — TabbyAPI runtime upgrade (prerequisite, its own judge)

**Why**: loading a codebook encoded by a newer quantizer than the installed
runtime understands is the one risk that produces no error — a "working"
load with wrong weights. Qwen3.8 architecture support and its cache
quantisation only landed in the `1.4.x` line. Do not attempt to load the
3.8 build on `1.1.0`.

1. Bump the pinned digest in `services/tabbyapi/Dockerfile` to a `1.4.9`+
   release. Re-check whether the `python3-dev` JIT patch (documented in the
   same file, required for `gated_delta_net`'s Triton compilation) is still
   needed on the new base image — do not assume it carries over unchanged.
2. `docker compose build tabbyapi && docker compose up -d --force-recreate
   tabbyapi`, then repeat the exact "triplet de versions constaté AU
   RUNTIME" check from `docs/engineering-log.md:123-136` (`pip show
   exllamav3 torch` inside the container) — record the new triplet the same
   way, not deduced from `pyproject.toml`.
3. **Regression judge, model unchanged**: reload the CURRENT production
   Qwen3.6 build on the upgraded image first and run the standard A1/A2
   smoke. This isolates "image upgrade effect" from "model swap effect" —
   confirm no regression from the runtime bump alone before ever touching
   the 3.8 weights.
4. Only then load the 3.8 build and confirm a clean start (`GET
   /v1/model`, no traceback in `docker logs tabbyapi`).

🧑 **Checkpoint**: new triplet recorded, Qwen3.6 smoke green on the new
image, Qwen3.8 loads cleanly — before Phase 1.

## Phase 1 — Fix per-request thinking control (prerequisite, its own judge)

**Why**: `NO_THINK_DIRECTIVE`'s only live path is dead
(`docs/resolved-bugs.md:139`/`:193`, confirmed by a direct backend call);
the only mechanism proven to work is the formal per-request parameter
already used by `planner_llm` (`extra_body={"enable_thinking":
PLANNER_THINKING_ENABLED}`, `app/graph.py:1350`) — itself currently inert
in production since `PLANNER_ENABLED` defaults `false` (effort 2.4). Today,
**nothing in the main loop actually controls thinking** — a pre-existing
gap, not something the swap introduces, but the swap raises the stakes:
external reports (unverified against our exact files, to be settled
empirically in step 1 below) describe Qwen3.8's template as defaulting to
`reasoning_effort: xhigh` — the costliest setting — on every call unless a
real kwarg is threaded, and restricting `reasoning_effort` to
`{xhigh, medium, low}` on pain of a template exception.

1. **Empirical test on the actual downloaded 3.8 files first**, settling
   contradictory external claims rather than trusting either: a direct
   TabbyAPI call (bypassing langgraph-agent, same technique as the
   resolved-bugs entries above) with `enable_thinking: false` — does it
   error, silently no-op, or genuinely suppress `reasoning_content`? Repeat
   with `reasoning_effort: "medium"` and `"low"`.
2. Replace `_apply_adaptive_thinking`'s text-prefix injection with the
   formal `chat_template_kwargs` path (`enable_thinking`, `reasoning_effort`,
   `preserve_thinking` as needed) for the MAIN loop call — same trigger
   condition (previous turn fully auto-approved), same env var name
   (`ADAPTIVE_THINKING`) to avoid an unrelated rename.
3. Update the docstrings/comments referencing "Qwen3.6" specifically
   (`app/graph.py:540-550`, `_apply_adaptive_thinking`'s docstring) — stale
   the moment a second model is in scope, per the docstring contract (WHY,
   not WHAT, and not tied to one model's name if the mechanism is meant to
   generalise).
4. Extend `test_image_retention_and_thinking.py` for the new kwarg path;
   drop or rewrite the assertions tied to the old text-prefix behaviour.

**Judge**: a live smoke on Qwen3.8 confirming, via the raw transcript (not
the aggregate report), that `reasoning_content` is genuinely absent on
turns where `ADAPTIVE_THINKING` fires and present otherwise — the same
"confirmed mechanistically, not just success/failure" bar used throughout
this project's history. Per `CLAUDE.md`'s trigger-rate rule, this mechanism
already needs a coverage counter for Phase 2's own read — add one now if it
doesn't exist (how often does the new thinking-control call actually change
`reasoning_effort` from the model's default per run).

🧑 **Checkpoint**: mechanism confirmed working on the actual 3.8 files,
tests updated — before Phase 2. Do not let this phase's own validation
double as Phase 2's comparison — it is a mechanism fix, judged on its own
correctness, not on benchmark score.

## Phase 2 — Static measurements (no full campaign yet)

Mirrors `quantisation-evaluation.md` Phase 1, plus one item specific to
this swap:

1. Peak VRAM per GPU (weights + vision tower + MTP + KV cache at the
   production context size) vs. the current build.
2. Free VRAM remaining per GPU under production settings.
3. Raw throughput: prefill and decode tokens/s, with and without MTP.
4. MTP acceptance rate, if exposed.
5. **Tool-calling sanity check**, cheap and deterministic, done before any
   agent loop: a handful of scripted turns confirming the XML
   `<tool_call>`/`<function>`/`<parameter>` format round-trips correctly,
   specifically including a JSON-string-valued argument — flagged
   externally as a crash point on official Qwen3.8 templates and directly
   on this project's tool-calling critical path. Settle it here, not
   mid-campaign.

🧑 Checkpoint: VRAM and tool-calling sanity reviewed before spending a full
campaign.

## Phase 3 — Full campaign, single variable

**Re-establish the baseline first, on the updated stack.** Phases 0 and 1
changed the image and the thinking-control mechanism — the comparison's
baseline must be Qwen3.6 reloaded on the NEW image with the NEW mechanism,
not the historical baseline campaign. Re-run the standard v2 suite on
Qwen3.6 under these conditions before touching Qwen3.8, and record it as
the reference for this specific comparison.

Then run the identical suite (3 repetitions, family F at 2) on Qwen3.8.

**Judges**:

- CuP and per-family scores — a gain under ~2 points is within documented
  run-to-run noise.
- Failure-cause distribution — same reading as the quantisation brief:
  mechanical/perceptual failures cannot be fixed by a more capable model.
- Median time per task and tokens per task.
- **Thinking-token volume and `ADAPTIVE_THINKING` trigger rate** — new
  judge, direct consequence of Phase 1's fix and Qwen3.8's costlier
  default; read this before reading CuP, since a latency regression here
  would be attributable to the thinking mechanism, not the model's
  reasoning quality.
- Family B CuP specifically (policy compliance).

## Decision table (fill before reading results)

| Phase 3 result | VRAM (Phase 2) | Decision |
|---|---|---|
| Clear gain (>2 pts CuP or fewer reasoning-attributable failures) | pool intact | Adopt Qwen3.8 |
| Clear gain | pool consumed | Same three-claimant trade-off as the quantisation brief's Phase 2 — design decision, not a measurement |
| No meaningful gain | either | Keep current model; record the finding |
| Gain but latency/tokens materially worse | either | Check thinking-token judge first; may be a `reasoning_effort` default, not a model-capability cost |

## Deliverables

- `docs/campaigns/` entries for: the Qwen3.6-on-upgraded-image baseline
  re-run, and the Qwen3.8 campaign.
- Engineering log entries for Phase 0 (runtime triplet), Phase 1 (thinking
  mechanism fix, its own dated entry per the docstring contract's pointer
  rule), and Phase 3 (the decision, stated without advocacy even if the
  result is "no change").
- `docs/architecture/inference-backend.md` updated: runtime triplet, VRAM
  budget per build, and the per-request thinking-control mechanism
  replacing the dead text-prefix description.
- `docs/resolved-bugs.md`: no new entry needed for the `/no_think` dead
  mechanism (already recorded) — but cross-reference this brief from that
  entry once Phase 1 ships, so a future reader finds the fix from the bug.
