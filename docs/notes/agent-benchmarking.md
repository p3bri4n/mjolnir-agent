# Measuring an autonomous agent: what eleven campaigns taught us

A field report from building a local web agent and, more importantly, from
building the thing that tells you whether it works. Scores went from 16/33 to
30/33 over eleven campaigns. The interesting part is not the number — it is
the four campaigns that went *down*, and what each of them was actually
telling us.

Setup: a local model (Qwen3.6, no API), a LangGraph agent driving Playwright
and a desktop-automation channel through MCP, a human approval layer, and a
suite of web tasks with programmatic assertions on self-hosted fixtures.

## The instrument comes first

We built the task harness before touching the agent. Eleven web tasks —
paginated catalogue, multi-field form, dynamic table, multi-hop
documentation search, download-then-compute, authenticated session, and two
that matter more than they look:

- **A task that cannot succeed**: find a product reference that does not
  exist. Success = the agent says so. Any invented price is a failure. This
  is the hallucination detector, and it is the first capability to degrade
  when you push autonomy.
- **A staleness probe**: a question whose true answer post-dates the model's
  training, with ground truth fetched live by the harness at campaign time.
  Answering from weights = failure.

Every assertion is programmatic — an exact extracted value, a final form
state, a file on disk. No LLM-as-judge on the score. A judge that shares the
agent's blind spots is not a judge.

The harness paid for itself before its first official campaign. Two smoke
tests, and it surfaced a behaviour nobody had described: the agent
**fabricated URLs**. Asked for page 4 of a three-page catalogue, it composed
`page-4.html` rather than reading the "next" link. Given a reference to find,
it guessed `product-KX-4471.html`. Not a harness bug — a real behaviour,
caught by an instrument that did not exist a week earlier.

## The zigzag, and the mistake that caused it

16 → 24 → 20 → 24 → 30. The dip in the middle is the most instructive
campaign we ran.

**Iteration a** added a mechanical guardrail: navigating to a URL never
observed on a page is refused. Score rose to 24, but the fabrication counter
*rose too* — five to twenty rejected attempts per failing run. The guardrail
blocked execution correctly and changed nothing about the behaviour. First
lesson, learned the hard way: **blocking without redirecting makes things
worse.** A guardrail that says "no" without showing the alternatives turns
one error into a loop of errors.

**Iteration b** shipped two mechanisms at once — structured truncation
(never amputate a page's links, only its prose) and an enriched rejection
message listing the real links available. Score fell to 20. The net of −4
hid a +5 on one mechanism and a −9 on the other, and it took a full
campaign to separate them.

The enriched feedback was the culprit, and not for the reason we assumed.
It was not too verbose; it was **prescriptive at the wrong moment**. On the
impossible task, each rejection answered "where should I go next" to an
agent whose real problem was "should I still be going anywhere". Forty
lines of links is an invitation to keep hunting. That task went to 0/3.

**Iteration c** made the feedback graduated: a bare message for the first
rejections, targeted suggestions on the third, and at a ceiling a different
message entirely — *concluding the target does not exist is a valid answer*.
Back to 24, but a healthy 24: the impossible task reached 3/3 with genuine
convergence.

And it broke the download task, which fell from 3/3 to 0/3. The honesty
nudge, useful where the target was absent, offered an honourable exit on a
task where the target existed and the agent simply had not found it yet. We
had repaired honesty by breaking persistence — the exact inverse of the
previous iteration.

**Rule extracted**: any global behavioural nudge has a task where it turns
against you. The fix is never to reword it; it is to **condition it on an
observable state**. The ceiling now runs a proximity match against the
observed affordances: candidates found → "the target is probably reachable
via these"; nothing close → "concluding absence is valid". Same mechanism,
two branches, chosen by a measurement rather than by a sentence.

## When the agent cheats, look for the missing capability

This is the theme that recurred most, and the one we would put first if we
started again.

**The download task.** The agent hallucinated file paths — `file:///…`
inventions, a CSV that existed nowhere. Three verifications later the real
story emerged: the browser container ran with an in-memory isolated profile
and no shared volume, and the terminal MCP had no network access by design.
There was **no legitimate path** for the agent to consume a downloaded file.
It was not hallucinating out of sloppiness; the gesture the task required did
not exist in its world. We added a dedicated download volume, mounted
read-only on the agent side, and documented the path in the tool description.
The task went to 3/3.

**Arbitrary JavaScript.** We had classified `browser_evaluate` — arbitrary JS
execution — as never-blanket-approvable, on the principle that a data
transfer must not travel through an arbitrary execution channel. Two tasks
immediately collapsed to 0/3. The audit archives showed why: the agent had
been using JS as its *extraction* tool, and deprived of it, fell back to
pressing Ctrl+F (invisible to the accessibility tree) and then to visiting
products one by one — 111 tool calls where 88 had sufficed, and failure.

We did not restore the capability. We **renamed** it: a `browser_extract`
tool taking a text or selector — never code — dispatching internally to a
fixed JS template, at read tier. Arbitrary execution stayed locked. Score
rose to 30/33.

**DOM introspection without arbitrary execution.** A related gap surfaced
from a different angle. `browser_snapshot` annotates elements with
`[ref=eN]` tokens, and the model sometimes copied that annotation literally
into a `target` argument — Playwright accepts only the bare token or a CSS
selector, so `"ref=e7"` failed as an unknown selector engine, every time
(28/28 in the audit history; 33/35 successes without the prefix). Whenever
the guessed-selector fallback missed, the model reached for
`browser_evaluate` again, for the same underlying reason as above: no named
tool let it inspect the DOM's actual structure. Fix: normalize the `ref=`
prefix away before dispatch, and add `browser_inspect` — read tier, fixed JS
template — so the introspection fallback has a legitimate home instead of
only an arbitrary-execution one.

**The dt/dd defect.** `browser_extract` matched a structured label node (a
`dt`, or a table's first `td`) but never returned the value sitting next to
it — a `dd`, or the row's other cells. The agent could find "Prix" but not
read "84,90 €". On the trajectory that surfaced it, the gap forced an
8-turn per-page re-navigation just to recover values the tool had already
located. Fix: an `adjacent_value` field returned alongside every match,
resolved from DOM structure (`dt` → sibling `dd`; `td`/`th` → the row's
other cells).

The general form: **when a model fabricates or works around, look for the
legitimate capability that is missing before punishing the behaviour.**
Remove the Swiss army knife, hand over the screwdriver — named, declarative,
auditable, at the right permission tier. Security and capability were not
trading against each other; the boundary had simply been drawn in the wrong
place. Four instances of the same shape: the download volume, `browser_extract`
itself, `browser_inspect`, and the dt/dd defect.

## Text conventions cannot carry mechanical decisions

The cognitive core — explicit plan, per-action success criterion, verified
after the fact, with a failure budget — worked, and cost a factor of eight in
latency. The fix was to fold the verification into the following turn instead
of a dedicated model call. To carry the verdict, we asked the model to emit a
marker: `[CONSTAT: reached|not_reached]`.

Score: 18/33. The worst campaign of the project.

The marker was sometimes missing, and a missing marker degraded
conservatively to "not reached" — so successful actions were charged against
the failure budget, triggering replans and premature abandons. We had
replaced a reliable mechanism (a dedicated call) with a wish expressed in a
prompt.

Worse, the first metric said the mechanism was fine: zero malformed verdicts.
True, and meaningless — the marker was emitted in **9 % of turns**. We had
measured the reliability of a channel almost nobody used. **A reliability
judge is worthless without a coverage judge**, and a flattering zero deserves
more suspicion than a bad number.

Moving the verdict into a dedicated tool call brought coverage to 96 %, and
broke the score again — because a turn now required *two* coordinated tool
calls, and the backend enforces no grammar on tool calls at all. Reliability
came from the model's habit, not from a guarantee. The working version puts
the verdict in a **required field of every action tool's schema**: one call
per turn, the native pattern, the verdict travelling in a structure.

**Rule**: any information a mechanical decision depends on — budget, status,
routing — must travel through a structurally guaranteed channel: a typed
field, a constrained schema, a tool call. Free text is for reasoning.
Decisions are taken on structures. And on a backend without grammar
enforcement, the protocol must fit in a **single atomic gesture** — a
two-call choreography fails silently.

## What you do not judge, drifts

The cognitive core passed its checkpoint with four green criteria. Task
duration had multiplied by eight. It was recorded as a metric, not as a
judge, so nobody had to explain it.

The diagnosis, run entirely on archives: auxiliary calls (planning,
verification, plan judging) were roughly as numerous as the main turns but
individually two to five times slower and generating three to six times more
tokens — 73 to 89 % of total time. The bottleneck was not prompt size; it was
**output volume**: a three-word verdict wrapped in 1400 tokens of reasoning.
Folding verification into the following turn and bridling reasoning on the
remaining auxiliary calls brought the median from 145 s to 45 s.

Median time per task is now a permanent judge, at the same rank as the score.
Everything not explicitly judged eventually drifts.

## Three false diagnoses, one cause

Each time, we believed a narrative instead of reading the data.

- **"The browser is stuck on about:blank."** It was our own anti-fabrication
  guardrail refusing a navigation. The phrase came from the model's own
  confused account of the rejection, not from the tool result — which nobody
  had opened.
- **"The agent treats the reference as a number."** Contradicted by the
  fixture generator.
- **"Google blocked us."** Contradicted by the actual tool result; the
  earlier conclusion had been drawn from stale data.

**Verify the real, never the narration.** Effective configuration is read
from `/proc/1/cmdline` or the running container, not from the config file. A
library's behaviour is read from the installed source, not the docs. What a
tool returned is read from the tool result, not from what the model says it
did.

This is also why the audit log became the most valuable artefact of the
project: intentions, tool results and model messages, persisted as JSONL.
Most diagnoses were then done **on archives, with zero new runs** — and one
of them, run before writing any code, killed a fix we were about to build for
nothing.

The corollary is that the audit log was incomplete for a long time, and it
showed. It recorded intentions but not results, which is exactly the half
that breaks. And a blind spot survived even longer: turns passing through
human approval were not logged at all — so the *first* call of every task was
invisible. That gap directly produced one of the false diagnoses above.

## Contamination is the silent failure mode

Four incidents, all of the same family, all producing plausible numbers:

- Repetitions of a task sharing a thread identifier: repetitions 2 and 3 ran
  on state accumulated by repetition 1. Not measurements — echoes.
- A browser session shared across tasks: a tab left open by one task turned up
  inside another, hours later.
- A desktop browser left running for ten hours, producing a "success" that
  proved nothing.
- A campaign launched before its fixture containers were ready: 14/33, 44
  minutes, void.

Hence a preflight that now gates every campaign — tool schema as the agent
actually sees it, image digests, effective behaviour flags, session resets,
volume purges, fixtures reachable. **A campaign started on an unverified
stack is void**, and it is cheaper to refuse it than to interpret it.

The same discipline applies to the fixtures themselves: hashed, frozen, and
any change creates a new benchmark version whose results may not be compared
with the old.

## A mechanism built ahead of its need

The last lesson is the least satisfying. We built episode compaction —
summarising completed subtasks to control context growth — and measured it in
a proper single-variable campaign. Result: 30/33 with it enabled, and a
better cache-hit rate. Encouraging.

Then we asked the coverage question, which nobody had asked: compaction only
triggers past a message threshold. It had fired in **9 to 15 % of runs**. The
campaign had not tested the mechanism; it had measured the noise of the runs
that never reached it. And the cache improvement was, if anything, evidence
against it — rewriting history mid-conversation should *hurt* prefix caching,
not help.

The honest conclusion was to requalify the campaign as inconclusive, keep the
flag off, and stop there. Not to add repetitions — **when a mechanism's firing
rate on your benchmark is low, the benchmark is the wrong instrument**; you
build a targeted exercise instead. The mechanism was built ahead of its need,
which is not a failure, but it cannot be validated by an instrument that does
not exercise it.

## What we would tell someone starting

- **Build the instrument before the mechanism.** Everything above exists
  because a harness existed first.
- **One variable per experiment, and name the judge before you measure.** The
  one time we bundled, it cost a full campaign to untangle a net of −4.
- **Write your decision thresholds down first.** Our first "improvement" —
  52 % to 40 % — was noise, and only a pre-declared threshold stopped us
  building on it.
- **A reliability judge needs a coverage judge.** Zeros are the most
  flattering and least trustworthy numbers you will produce.
- **Keep the raw data; interpretations get redone.** Our conclusions were
  wrong several times. The data let us notice.
- **When the agent misbehaves, ask what capability it lacks.** It was the
  right answer four times out of four.
- **Expect the score to go down.** Four campaigns out of eleven regressed.
  Each one isolated an interaction and turned it into a permanent metric. The
  curve that matters is not the score — it is the stock of mechanisms you have
  *confirmed*.

---

Companion note: [`llamacpp-dual-gpu.md`](llamacpp-dual-gpu.md) — six weeks
diagnosing the inference backend this harness was built to measure.
