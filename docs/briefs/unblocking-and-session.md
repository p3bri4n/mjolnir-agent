# Human-in-the-loop unblocking + session persistence — brief

> **Problem**: an agent that never gets past an anti-bot challenge is
> excluded from a real share of the web. The answer that fits this project is
> not evasion but **escalation with hands-on resolution**: the agent stops,
> the human resolves the challenge *inside the agent's own session*, the
> agent resumes. Same pattern as every capability gap this project has closed
> — give the missing gesture in a named, controlled form.
>
> **Decisions already taken** (do not re-litigate):
> - headed mode behind `BROWSER_HEADED`, default `true`;
> - session persistence behind a TTL env var, default 7 days.
>
> **Order matters**: Phase 1 delivers the capability, Phase 2 only makes it
> less frequent. Do not build Phase 2 first — without hands-on resolution,
> persistence has nothing to capitalise on.

---

## Phase 0 — Headed mode and its baseline

`BROWSER_HEADED` (default `true`) launches the Playwright browser windowed on
a virtual display. Headless remains available for CI.

**This changes the measured system** — rendering, timings, anti-bot exposure,
resources. Therefore:

1. Ship headed mode alone, no other change.
2. Run a full v2 campaign as the **new reference**. Previous campaigns become
   non-comparable; state this in `docs/benchmark-v2.md`.
3. Record `BROWSER_HEADED` in campaign metadata like every behaviour flag —
   two campaigns compared must share its value.

Expected: results broadly unchanged, possibly fewer challenges. If the score
moves materially, investigate before proceeding — a perception change at this
depth must be understood, not absorbed.

🧑 Checkpoint: reference campaign reviewed.

## Phase 1 — Blocked-state detection and hand-over

### 1.1 Detection: "not progressing", never "this is a CAPTCHA"

No signature list. Signatures rot, and a blocked agent looks the same whether
it hit a challenge, a login wall, a rate limit or a broken page.

**Generic signal** (the mechanism): N consecutive actions with a
`not_reached` verdict and **no observable state change** — same URL, snapshot
unchanged — despite the mandatory-alternative-strategy rule already forcing
*different* attempts. That combination distinguishes *blocked* from *doing it
wrong*. Threshold in env (`BLOCKED_DETECTION_STREAK`, default 3).

**Hints layer** (informational only, never a gating condition): known
provider iframes, 403/429, characteristic titles. They enrich the message
shown to the human ("likely a Cloudflare challenge") and may escalate earlier
than budget exhaustion. If the hints ever become the condition, the mechanism
has failed.

Outcome is **escalation, not failure**: the agent is not failing, it is
prevented. This reuses the existing interrupt/approval path and the
`safe_deferral` outcome introduced by task A3.

**Critical false-positive case — do not confuse *blocked* with *searching in
vain*.** Task D1 (non-existent product reference) produces a superficially
similar signal: repeated attempts, no result. The discriminant is observable
state — a searching agent *changes pages*, a blocked agent does not. Any
escalation on D1 is a bug, and it is the same failure mode as the
honesty/persistence flip that cost a campaign earlier in this project.

### 1.2 Hand-over via ephemeral VNC

The browser is already headed on a virtual display (Phase 0). What activates
on escalation is **access**, not the browser:

1. On escalation, start a VNC/noVNC server bound to localhost, with a
   single-use token; put the URL in the approval message alongside the last
   screenshot and the detected hint.
2. The human resolves the challenge **in the agent's own session** — same
   cookies, same IP, same context. This is the whole point: no other
   arrangement works.
3. **Resolution trigger: the human's explicit resume approval. Never
   automatic detection.** Deciding a challenge is solved is exactly as
   fragile as deciding one is present — a cleared CAPTCHA may lead to a
   second one, a verification page, a redirect. The person who just solved it
   knows; one click removes all ambiguity. Reuse the existing approval path,
   add nothing.
4. On resume: VNC stopped, token revoked. Exposure lasts minutes, not a
   session.
5. **On resume, observe before acting**: take a snapshot and compare. If the
   state is unchanged, re-escalate rather than loop — incomplete resolution
   or a second challenge.
6. **Reset the failure budget on resume.** Otherwise the agent inherits the
   blocked attempts and abandons at the next minor obstacle.
7. **Timeout** (`UNBLOCK_TIMEOUT`, default 15 min): VNC stopped, task marked
   failed with cause `blocked_unresolved`. Never leave a pause and an open
   port hanging.
8. **Audit**: a dedicated entry records the escalation, the hand-over window
   and the resumption. A human intervention inside the session must appear in
   the trace, or the recorded trajectory is a lie.

### 1.3 Proving the mechanism works

Three levels; the first carries the weight.

**Deterministic integration test** — a fixture that simulates a block without
depending on a real CAPTCHA: a page that ignores every action until a given
cookie is set. The test walks the whole chain: detection after N attempts,
escalation emitted, VNC started with a token, resolution simulated (set the
cookie through a test-only channel), resume, state-change check, task
completed. Plus the error paths: timeout stops the VNC and classifies
`blocked_unresolved`; token revoked after resume; unchanged page
re-escalates; **D1 does not escalate**.

**False-positive rate**, measured on the v2 suite: escalations on local
fixture tasks, where no challenge exists. Expected zero. Any unjustified
escalation is a threshold bug, not an incident — a mechanism that interrupts
wrongly is worse than no mechanism.

**False-negative rate**, only honestly measurable on real-site tasks: how
many runs failed with a cause that, on re-reading the archives, was an
undetected block. Assessed after the fact, not live.

### 1.4 Campaign behaviour

In campaigns the harness auto-approves; it must **not** auto-resolve. A
blocked run is classified `blocked_external` and reported separately, never
averaged into capability scores — same rule as the security family. This
keeps the anti-bot reality of real-site tasks from polluting the measurement.

New permanent metric: **escalations per campaign, by cause**.

🧑 Checkpoint: false-positive rate and escalation frequency reviewed before
building Phase 2.

## Phase 2 — Session persistence (conditional on Phase 1 data)

Purpose is **not** stealth. It is to **capitalise on human resolutions**: the
cookie obtained after a hand-over survives, so the challenge does not
reappear on every run. That is a far stronger argument than fingerprint
evasion, and it only holds once Phase 1 exists.

1. Persistent profile **dedicated to the agent** (never a personal profile),
   with `SESSION_PERSISTENCE_TTL` (default 7 days): storage state older than
   the TTL is purged on startup.
2. **Always ephemeral in campaigns.** Non-negotiable: a benchmark whose state
   accumulates measures the agent plus its past. This project has already
   paid for that four times (shared thread ids, ghost tabs, a ten-hour-old
   browser, unpurged volumes). The harness forces isolation regardless of the
   env var, and the preflight verifies it.
3. **Manual purge** exposed (command + dashboard), because the honest failure
   mode is unwanted or compromised state accumulating silently.
4. **What is lost, written down** in `docs/architecture/`: amnesia. Today a
   successful injection's residue (cookie, localStorage, open session) dies
   with the run; with persistence it survives, and a later task on another
   domain may inherit an authentication it never requested. This is a
   deliberate trade, not an oversight.

🧑 Checkpoint before enabling by default.

## README

State the limitation plainly rather than hiding it: sites with anti-bot
challenges require occasional human resolution; the agent detects the block,
hands over its session, and resumes. That is the correct behaviour for a
supervised agent, not a defect.

## Out of scope

- CAPTCHA-solving services (paid, non-local, contrary to the project's
  posture).
- Behavioural humanisation (randomised delays, mouse trajectories): low
  effect against modern detection, real latency cost, and it adds variance to
  a system whose noise is already documented across eleven campaigns.
- Keeping GhostDesk for stealth reasons: its advantage came from headed mode
  and a real display, both of which Phase 0 provides on the inspectable
  channel.
