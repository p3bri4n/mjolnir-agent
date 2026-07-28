# Methodology

This project runs an autonomous agent on local hardware — non-deterministic
model, young software stack, heterogeneous GPUs. In this context, intuition
and manual testing are not enough: a fix that "looks like it works" gets
paid for two campaigns later. The six principles below were formulated over
the course of the work, each one after a concrete failure that made it
necessary. They are given together with the incidents that ground them.

## 1. The instrument before the mechanism

Every effort starts by building what's needed to measure, before any
change: the tool-calling harness preceded the CUDA diagnosis, the task
harness preceded the cognitive core, a baseline precedes every fix.

Corollary: **anything not explicitly judged drifts.** An 8x latency
regression cleared a checkpoint whose four criteria were all green, because
time was logged as a metric there, not as a judge. Median time per task has
been a standing judge since, at the same rank as score.

## 2. One variable per experiment, criteria frozen before the measurement

Decision thresholds are written before the results, never reinterpreted
once read. Each mechanism has its judge designated in advance: one task
judges truncation, another judges feedback.

The two times the rule was skipped, it cost a campaign. Iteration 1b
shipped snapshot truncation and rejection feedback together: the net score
(−4) masked a +5 on one mechanism and a −9 on the other, and it took a
whole iteration to untangle them. When a technical coupling forces two
things to ship together, it is declared at the checkpoint *beforehand*,
with one judge per mechanism.

## 3. Falsify rather than confirm

A hypothesis is tested by what would disprove it. The CUDA crash diagnosis
successively buried context checkpoints, cross-GPU copies on restore, a
pigtail power cable, and the power budget — it was that graveyard of
hypotheses that eventually pointed to the real trigger (prefill batch size
on the dual-GPU path).

The same framing applies to evaluations: the inference-engine-swap PoC was
designed to *stress* a hypothesis, not to prove it, with rejection criteria
written in advance. A candidate designed to win always finds a way to win.

## 4. Verify reality, never the narration

Effective configuration is read from `/proc/1/cmdline`, not from the config
file. A library's behavior is read from the installed code, not the
documentation. What a tool returned is read from the tool result, not from
what the model claims to have done with it.

The project's three false diagnoses all came from taking self-narration at
face value: a "browser stuck on about:blank" that was actually the
anti-fabrication guardrail rejecting the action, a "misparsed numeric
request" contradicted by the fixture generator, a "Google blocked it"
contradicted by the actual tool result. Three times, the data existed and
said something else.

Practical extension: **archives first, zero runs.** Most diagnoses were
made on already-collected data. That is what justifies the investment in
observability — a persistent audit log (intentions, tool results, model
messages), server metrics, campaign results in machine-readable format.

## 5. Push rules down from text into code

Information a mechanical decision depends on — budget, status, routing —
never travels through a textual convention the model is merely asked to
respect. A `[FINDING: ...]` marker requested in prose was emitted in 9% of
turns and wrecked a campaign score; the same finding carried by a mandatory
tool call reaches 96%.

Three corollaries, each learned from a failure:

- **Blocking without redirecting makes things worse.** A guardrail that
  refuses an action without showing the alternatives turns one error into
  a loop of errors. The correct shape of a constraint is "no, but here" —
  and knowing when to say stop searching is also a redirection.
- **When the model confabulates or works around a limit, look for the
  missing capability before punishing the behavior.** Invented file paths
  came from a stack with no legitimate download path; resorting to
  arbitrary JS came from the absence of an extraction tool. Take away the
  Swiss army knife, hand back the screwdriver: a named, declarative,
  auditable capability, at the right security tier.
- **A protocol must fit in one atomic gesture.** On a backend that doesn't
  constrain tool-call grammar, asking for two coordinated calls per turn
  fails silently. Whatever must be guaranteed fits in a single call, or in
  a typed schema field.

Compression and perception follow the same logic: you can summarize what a
page *says*, never amputate what it *lets you do*.

## 6. Raw data is kept, interpretation is redone

Conclusions have been wrong more than once; raw data is what made the
corrections possible. An interpretation on its own becomes the official
story and stops being verifiable.

Three requirements follow:

- **Isolation** — browser and desktop session reset, volume purge, unique
  thread IDs per run. Without it, a campaign measures the agent *plus its
  residual past*: a browser left open ten hours earlier produced a
  "success" that proved nothing.
- **Traceability** — every campaign records its effective configuration
  (commit, image digests, behavior flags) and its per-run results in
  machine-readable format. A campaign must be able to say *which agent* it
  measured, not just what it did.
- **Distrust flattering zeros** — an error counter at zero on a mechanism
  that only triggered in 9% of cases measures nothing. A reliability judge
  always comes with a coverage judge.

A benchmark's fixtures are frozen and versioned: any change creates a new
version, and cross-version comparisons are forbidden.

## The delegation framework

These principles are applied by a coding agent working autonomously on the
repository. What makes the delegation workable:

- **Stop at the boundaries.** The agent stops at declared checkpoints and
  does not chain onward on its own. Arbitration decisions stay human.
- **Pre-filled decision grids.** Every experiment ships with the
  interpretation table for its possible results. The agent applies it, it
  does not reinterpret.
- **Report without advocacy.** Missed criteria are announced as such,
  without a flattering rewrite. A report that only announces successes is
  not a report.
- **No opportunistic refactor.** Anything found out of scope is noted and
  proposed at the next checkpoint.
- **The brief before the code.** Every effort's instructions are written
  and committed before the first line, so that recovery after an
  interruption is "read the last brief and the last report."

## Accepted cost

This method is slow. It has produced zigzagging campaigns (16 → 24 → 20 →
24 on the same task suite), expensive hypotheses invalidated, and
mechanisms built then removed. The bet is that the stock of *confirmed*
mechanisms rises with every iteration even when the score drops, and that a
non-deterministic system does not stabilize any other way.
