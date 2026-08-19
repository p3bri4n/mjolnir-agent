# Six weeks of llama.cpp on two mismatched GPUs

A field report from building a local agent stack on an RTX 4070 Ti Super
(Ada, 16 GB) and an RTX 5060 Ti (Blackwell, sm_120, 16 GB), serving a
multimodal MoE (Qwen3.6-35B-A3B, Q4_K_XL + mmproj) through a llama.cpp fork
with a quantised-KV-cache feature we wanted.

Everything below was verified against logs, `dmesg`, or the installed source.
Where a hypothesis turned out wrong, it is kept — the wrong ones are the
useful part.

## Part 1 — Four traps before a single token

None of these are in any tutorial, and each cost an evening.

**The CUDA VMM allocator breaks Docker builds.** ggml enables its "CUDA
Virtual Memory Management" allocator by default, which links `ggml-cuda`
against the real CUDA driver (`libcuda.so`, CMake target
`CUDA::cuda_driver`). That library is not in a `*-devel` image — the host
driver provides it at *runtime* via nvidia-container-toolkit, never during
`docker build`. Result: `undefined reference to cuMemCreate/cuDeviceGet/…`
at link time. Fix: `-DGGML_CUDA_NO_VMM=ON`. Irrelevant to flash attention or
cache types; it only disables a pooling allocator that matters little at
`--parallel 1`.

**Blackwell needs CUDA ≥ 12.8, and "native" architecture detection cannot
work in Docker.** The `nvidia/cuda:12.4.1` base silently cannot compile for
sm_120 (`120a-real` requires 12.8, as the fork's own CMakeLists states). And
architecture auto-detection needs a GPU visible *during the build*, which a
standard `docker build` does not have. Fix: 12.8.1 base plus an explicit
`CMAKE_CUDA_ARCHITECTURES="89-real;120a-real"`.

**The build produces shared libraries without RPATH.** `libllama.so.0`,
`libggml-base.so.0` and friends land next to the binaries but nothing
embeds a search path (`readelf -d` confirms — our first assumption of an
`$ORIGIN` RPATH was simply wrong). Fix: copy the whole `bin/` directory,
set `LD_LIBRARY_PATH`, and add `libgomp1` to the runtime image — ggml's CPU
backend needs OpenMP, which slim images lack.

**A boolean flag became a value-taking option.** `--flash-attn`, passed bare
as it had always been, swallowed the next argument: `error: unknown value
for --flash-attn: '--jinja'`, and a container restart loop. The fork had
changed it to require `on`/`off`/`auto`. Lesson, applied ever since: read
`--help` from the binary you actually built, not from the documentation of
the project you think you are running.

## Part 2 — Where constrained decoding meets reasoning models

Two findings worth more than their debugging time.

**Tool calls can get trapped inside the reasoning block.** Symptom: the model
sometimes ended a turn with neither a structured `tool_calls` nor visible
text — its call attempt sat in prose, Qwen-style
(`<tool_call><function=NAME><parameter=…>`), buried in `reasoning_content`.
Reading the fork's parser (`common/chat-auto-parser-generator.cpp`) gave the
cause: the reasoning span is captured as *free text*, unconstrained by the
grammar, until `</think>` is seen. Grammar-enforced tool calling only applies
*after* that tag. A model that "reaches for" a tool before closing its
thought produces a call nobody parses. Non-deterministic: the same prompt
sometimes works. This is a general design tension between constrained
decoding and reasoning traces, not a quirk of one fork — worth knowing if you
build on any thinking model.

We mitigated client-side (a regex that reconstructs the trapped call, a
bounded retry, then an explicit notice) rather than server-side. On four
replayed tasks the fallback fired five times and recovered the intent every
time.

**Reasoning field conventions diverge silently.** Our streaming patch read
`reasoning` (the Ollama convention it had been written and tested against).
llama-server streams `reasoning_content` (the DeepSeek-R1/o1 convention).
No error, no warning — the model's reasoning simply vanished from the
stream. Confirmed only by inspecting raw SSE deltas from a real call. If you
support more than one backend, read both keys.

## Part 3 — The crash

With everything working, roughly **40–50 % of generations failed**. The user
saw an internal-error notice; the logs showed a hard CUDA fault on device 1
(the Blackwell card), after which the server's own supervisor restarted it —
so there was never a visible outage, just randomly failing turns.

```
CUDA error: unspecified launch failure
  ggml_backend_cuda_device_event_synchronize, ggml-cuda.cu:5742
```

Six weeks of intermittent failure, resolved by a matrix of single-variable
experiments. The hypotheses, in the order we killed them:

1. **Context checkpoints.** Every crash followed a `restored context
   checkpoint` line. `--cache-ram 0` reduced the rate (52 % → 40 %) — which
   we correctly read as noise on 2×25 runs, not a signal. Disabling
   checkpoints outright eliminated the checkpoint *trigger* and left the
   crash rate unchanged. **Falsified.**
2. **Inter-GPU copies at checkpoint restore.** A second stack pointed at
   `ggml_cuda_cpy_tensor_async` and `ggml_cuda_copy_across_devices` —
   compelling, and wrong as a complete explanation: crashes also followed
   image ingestion and full prompt reprocessing, with no restore involved.
   **Falsified as *the* mechanism**, kept as a clue.
3. **Power delivery.** One card was fed through a pigtail (two 8-pin
   connectors on one Y cable) on a 750 W supply. Recabling: no effect.
   Power-limiting both cards to ~80 %, then the Blackwell alone: no effect.
   **Hardware exonerated** — and worth the two evenings, because it is the
   only way to stop suspecting it.

The turning point was `CUDA_LAUNCH_BLOCKING=1`. `unspecified launch failure`
on an *event synchronise* is an asynchronous error: the collector, not the
culprit. Forced to report synchronously, the real offender appeared —
`launch_mul_mat_q` at `mmq.cuh:4029`, a quantised matmul kernel, always on
device 1, always during a large prefill batch.

Then two experiments that named the cause:

- **`--ubatch-size 128`**: 0 crashes in 25 runs, at default settings
  otherwise. 256: 0 in 50. 512 (the default): crash.
- **Single-GPU isolation**, both cards, at nominal `--ubatch-size 512` with
  partial CPU offload to fit: Blackwell alone, 0 crashes. Ada alone, 0
  crashes. Neither card fails in isolation under equivalent prefill load.

And `dmesg` closed it: **Xid 31 — MMU fault on copy engine CE4**. A memory
fault on a *copy engine* is a bad pointer or size, not failing silicon.

**Conclusion**: a software bug in the fork's inter-GPU copy/synchronisation
path, triggered by large prefill batches on a heterogeneous tensor split.
Not the checkpoints, not the multimodal path, not the power supply, not the
cards.

**Workaround in production**: `--ubatch-size 128`, permanently. Cost: about
+34 % latency per successful generation — trivially worth it against 40–50 %
of turns failing outright. Threshold bracketed: stable at ≤ 256 (75
cumulative generations, no crash), crash at 512. We kept 128 over 256 for
margin, since the exact threshold and its stability across prompt sizes
remain unknown.

**Honest caveat**: we never reproduced this on upstream llama.cpp. The bug
lives in a fork, in code largely inherited from upstream, and without a
vanilla repro an issue cannot be routed to the right repository. That test
was deliberately dropped when we decided to change inference backend — so
this remains a documented diagnosis, not a filed bug. If you hit the same
signature on upstream, that is the missing piece.

## Part 4 — Epilogue, and the counter-witness

We moved to ExLlamaV3 + TabbyAPI (a dense 27B in EXL3, vision and MTP
enabled). The same two mismatched cards, tensor split, heavy prefill: **no
crashes**. That is the strongest evidence the fault was in one engine's
copy path rather than in the hardware pairing — and it is the kind of
cross-engine comparison that only becomes possible once you have a harness
that measures the same thing twice.

## What we would tell someone starting this

- **`CUDA_LAUNCH_BLOCKING=1` first, always.** An async CUDA error names the
  collector, never the culprit. We spent weeks chasing a synchronisation
  primitive that was only reporting someone else's crash.
- **`dmesg | grep -i xid` after every crash.** It is the cheapest
  hardware/software discriminator that exists, and we ran it far too late.
- **Falsify in order, one variable, thresholds written before the run.** Our
  first "improvement" (52 % → 40 %) was noise, and only a pre-declared
  decision threshold stopped us building on it.
- **Suspect your hardware, then clear it properly.** The pigtail cable was a
  genuine defect worth fixing and had nothing to do with the crash. Both
  facts are useful.
- **A fork is a fork.** Every convenience it adds (a compressed KV cache, an
  image format) is paid for in divergence: its own flags, its own parser, its
  own bugs, and no upstream to file against. Weigh the feature against the
  support surface before adopting it.
- **Measure before you optimise anything.** Every number above comes from a
  harness that replays fixed prompts and classifies failures. Without it, all
  of this would have been anecdote.

---

Companion note: [`agent-benchmarking.md`](agent-benchmarking.md) — the harness
this one leans on, and what eleven campaigns on top of it taught us.
