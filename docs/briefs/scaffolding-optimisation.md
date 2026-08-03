# Scaffolding optimisation — three efforts

> **Context**: eleven campaigns show that almost every point gained in this
> project came from scaffolding, not from the model. This brief covers the
> three scaffolding changes with the best evidence behind them, ordered by
> effort/benefit ratio.
>
> **Grounding**: recent factorial work on agent scaffolds finds that stacking
> components produces *negative* marginal returns beyond a task-optimal
> subset, that tool use carries most of the scaffold's value while planning
> can carry negative value, and that this interference is strongest at
> mid-size models and fades at frontier scale. On the web-agent side, an
> ablation on a comparable model size (~31B) validates four mechanisms:
> adaptive observation, diff-based history, coarse-grained actions, task
> decomposition. Sources are cited in the engineering log entry that opens
> this effort — record them there, this brief is not a bibliography.
>
> **Everything here is measured with the existing v2 harness.** No mechanism
> is adopted on literature alone; the literature only decides what is worth
> measuring first.

---

## Effort 1 — Factorial ablation of the cognitive-core flags

**Zero development cost.** `PLANNER_ENABLED`, `VERIFICATION_ENABLED`,
`PLAN_VALIDATION_ENABLED`, `PLAN_JUDGE_ENABLED` are four switches: 16
configurations, no code to write.

### Protocol

1. **Reduce the cost before starting.** A full v2 suite × 16 configurations
   is unaffordable. Select a **representative subset** of 6-8 tasks covering
   the families that could plausibly react differently — long horizon (A1,
   A2, A4), short (F, D1), policy (one B-hard), perception (E3) — and
   declare it *before* seeing any result. 2 repetitions. Estimate and report
   the total runtime before launching.
2. **Dependency constraint**: plan validation and the plan judge are inert
   without the planner, and the judge is inert without validation. Only
   coherent configurations are run; list them explicitly (there are fewer
   than 16).
3. Every run through the standard preflight, flags recorded in campaign
   metadata as usual. Unattended, via `run-campaign.sh`.

### Reading the results — decide the rule before measuring

| Outcome | Decision |
|---|---|
| One fixed configuration matches or beats all-on | **Adopt it and remove the losing mechanisms.** Simplest outcome, most likely per the literature. A mechanism that never helps is debt, not insurance. |
| A clear, legible dependency on task nature (e.g. planner helps long tasks, hurts short ones) | Consider conditional activation — but only with the **simplest observable criterion** that captures the dependency. Never an LLM classifier without first proving a simple rule insufficient: that would add a mechanism to arbitrate between mechanisms. |
| Differences within noise | Also a result: the mechanisms cost latency without changing outcomes. The question becomes which to keep for their **safety** value (plan validation, approval tiering) rather than their score. |

Judges: CuP on the subset, median time per task, tokens per task, human
interventions per task.

🧑 Checkpoint before any removal or conditional routing.

## Effort 2 — Diff-based observation history

**Problem**: each turn ships a full page snapshot. Turn after turn, the
context fills with thousands of tokens that have not changed since the
previous turn.

**Change**: keep the current observation, but represent *history* as
**natural-language change descriptions** — "the form cleared and a red error
appeared under the email field" — rather than repeated full snapshots or
positional diffs. The ablation cited above found natural-language change
descriptions outperform standard diff formats by preserving semantic context.

### Notes

- This partially overlaps with post-action verification, which already asks
  the agent to observe what changed. Expect **sub-additive gains** — the
  cited ablation found the observation and history axes substitute
  partially. Measure the combination, do not assume the effects add.
- The change description is generated **by the harness/wrapper, not by an
  extra LLM call**. A dedicated call would reintroduce the auxiliary-call
  latency problem that cost three campaigns to fix. Structural comparison of
  successive snapshots (elements appeared/disappeared/changed value, URL
  change, new error text) is enough.
- Truncation rules still apply: affordances are never amputated.

Judges: tokens per task (the expected gain), CuP (must not regress),
verification coverage, median time per task.

🧑 Checkpoint.

## Effort 3 — Coarse-grained actions

**Problem**: the action space is fine-grained, so a single intent costs ten
to fifteen turns; long context dilutes relevant information and
`MAX_TOOL_ITERATIONS` becomes an arithmetic ceiling (documented: ~41 messages
max per task, the cause of A1's 0/3 and of the abandoned A4 extension).

### 3.1 Find the candidates in the archives, not by intuition

Before designing anything: a **frequency analysis of action sequences** in
the audit log. Which n-grams of tool calls recur across runs? A
`snapshot → click → snapshot → type → snapshot → click` repeated fifty times
names the composite tool to build. Zero agent calls, and it says where the
twenty turns actually go rather than where we assume they go.

Deliverable: ranked list of candidate composites with their observed
frequency and the turns they would save.

🧑 Checkpoint: candidate list reviewed before any tool is written.

### 3.2 Design rule — add a level, never replace one

Coarse tools **coexist** with the fine-grained ones. On an ordinary page the
agent takes the wide tool and finishes in one turn; on an unusual layout it
falls back to elementary gestures. The choice is dynamic because the *model*
exercises it each turn — not because a classifier routes it. This is the
pattern already validated three times here (`browser_extract` above the basic
gestures, bulk mode above that).

Two conditions, both learned from this project's own failures:

- **The wide tool must fail cleanly and redirect**: "could not fill field X,
  here are the fields I can see" — otherwise the agent loops instead of
  stepping down, exactly as it did for weeks on the `ref=` format defect.
- **Its description must say when to prefer it**, or it goes unused — as the
  bulk shortcut went unused on A1 where it would have helped.

Same tiering discipline: declarative arguments, never model-written code,
tier assigned by action nature.

### 3.3 Measure

Judges: turns per task (the target), `MAX_TOOL_ITERATIONS` exhaustion rate
(expected to fall), CuP, and specifically A1 (0/3 today — the task the
ceiling blocks) and the abandoned 9-step A4 extension, which becomes worth
retrying once the ceiling is no longer binding.

**Do not raise `MAX_TOOL_ITERATIONS` to fix A1.** The ceiling is a measured,
frozen budget; the point of this effort is to need fewer turns, not to allow
more. If coarse actions work, A1 passes at the current budget.

🧑 Checkpoint.

## Out of scope

- Raising `MAX_TOOL_ITERATIONS` or any frozen budget to make a task pass.
- An LLM classifier to route between configurations or tool granularities.
- Adopting any of the above on the strength of the literature without a
  campaign.
