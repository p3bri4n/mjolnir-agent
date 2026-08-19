# Lessons learned

## Measurement and instrumentation

- **Pin and record GPU order and per-card layer split** — nvidia-smi,
  2026-08-10: `Loading with autosplit`, 14 GB on the 5060 Ti at 84 % against
  4.4 GB on the 4070 Ti Super at 0 %.
- **Every conditional mechanism ships with its trigger counter** —
  compaction campaign of 2026-07-28, requalified "inconclusive": 9-15 %
  trigger rate, coverage reconstructed after the fact.
- **Every reliability judge comes with a coverage judge** —
  constats_inexploitables ≈ 0 on a 9 % real emission rate (campaign
  1/2-bis, figure itself skewed by a query bug fixed the next round).
- **Check a subset's discriminating power before concluding** —
  judge-validity analysis: of 7 tasks, T3/A4/E3 pure ceiling, A1 near
  floor, only A2 and D1 discriminating.
- **One variable per experiment** — iteration 1b: truncation and feedback
  shipped together, a net −4 hiding a +5/−9.
- **Freeze thresholds before measuring** — `--cache-ram 0`: 52 % → 40 %
  read as a signal before any threshold existed.
- **Non-regression is not a gain** — cognitive core validated at 29/33 vs
  30/33 ("not a regression"), later measured as having no effect by the
  cfg1/cfg8 sweep.
- **Median time per task is a judge, not a metric** — cognitive-core
  checkpoint: four criteria green, an ×8 latency regression passed
  through.
- **A resumed campaign is not a continuous one** — designed into the
  campaign-control brief (restarting TabbyAPI empties the prefix cache).
- **Fixtures frozen and hashed** — decided at benchmark v1 design, applied
  at the v2 transition (family F reused verbatim, comparison at task level
  only).
- **Never widen a frozen budget** — A4's 9-step extension, 0/3 on
  MAX_TOOL_ITERATIONS, reverted rather than raising the budget.
- **A mechanism that doesn't fire on the benchmark needs a targeted
  exercise** — A4/compaction: 4 threads out of 101 crossed the threshold;
  the multi-turn exercise produced the net negative result.
- **A live smoke precedes every final measurement** — v2 effort: five
  smokes, five bugs caught (stale image, missing route, two fixture leaks,
  a judge blind to the audit log).
- **No campaign without a green preflight** — 2026-07-28 campaign at
  14/33, 44 minutes lost, fixtures never started.
- **Keep the raw, redo the interpretation** — persistence inventory: four
  columns, none "present" across sixteen campaigns.

## Diagnosis

- **Verify the real, never the narration** — three false diagnoses:
  `about:blank` (the guardrail, not the browser), "4471 read as a number"
  (contradicted by the fixture generator), "Google blocked us"
  (contradicted by the tool result).
- **Every verification against installed code gets recorded** — TabbyAPI
  continuous batching verified, never recorded, became an "unknown" again
  during the effort 1.3 recalculation.
- **Archives first, zero runs** — latency diagnosis (73-89 % in auxiliary
  calls), B-β hard, A1, all done without a single run.
- **An async error names the collector, not the culprit** —
  `CUDA_LAUNCH_BLOCKING=1` revealing `launch_mul_mat_q` behind
  `cudaEventSynchronize`.
- **Falsify before fixing** — the `BULK_CHECK_DIRECTIVE` hypothesis on
  B-β: refuted from archives, the planned fix abandoned before a line was
  written.
- **Isolate before measuring** — ghost tabs (#30), shared downloads volume
  (#28/#29), thread_ids shared across repetitions.

## Agent design

- **Look for the missing capability** — four times: download (T5),
  `browser_extract`, `browser_inspect`, and A1's dt/dd defect.
- **Blocking without redirecting makes it worse** — iteration 1a:
  guardrail alone, fabrications rising to 5-20 per failing run.
- **Condition a global nudge on observable state** — iteration 1c: the
  honesty ceiling fixes T7 (0/3 → 3/3) and breaks T5 (3/3 → 0/3).
- **Mechanical decisions travel through guaranteed channels** —
  `[CONSTAT:]` marker: 18/33, real coverage 9 %; tool-schema field: 96 %.
- **A protocol must fit in one atomic gesture** — `report_and_act` as a
  second coordinated call: coverage collapsed, backend enforces no
  tool-call grammar.
- **Summarise what a page says, never amputate what it affords** — T10,
  target past the 8000th character, 49 of 82 links surviving.
- **Rewriting the agent's history creates a coherence conflict** —
  compaction exercise: the model itself flags the inconsistency, 0/6 on
  the dependent turn.
- **A tool's position in the catalogue affects its adoption** —
  `manage_plan`: 0 calls at the tail of a ~64-tool catalogue, real
  engagement on A1 once moved to the head.
- **Stacking yields negative returns** — cfg1/cfg8 sweep: 15/15 vs 13/15,
  +76 % time, identical work; intermediate configurations below both
  extremes.
- **Coarse tools add to fine gestures, never replace them** — pattern
  validated three times (`browser_extract`, bulk mode, `browser_inspect`).

## Security and supervision

- **Web content is untrusted; escalation is a valid outcome** — family C
  measured 9/9, and A3 introducing `safe_deferral` as an outcome.
- **A channel with no inspectable argument gets removed, not policed** —
  GhostDesk: coordinate clicks, no URL to validate; the feasibility probe
  confirming removal loses nothing tested.
- **A tool exposes an action, never a secret** — reasoning on the
  KeePassXC/YubiKey vault.
- **A never-grantable capability stays never-grantable** —
  `browser_evaluate` kept `NEVER_GRANTABLE` despite the cost on T1/T10,
  replaced by `browser_extract`.
- **No capability claim before verification** — README security section:
  PromptGuard and egress firewall announced, neither existed.
- **A document describing current state is reverified against the code at
  every update, not only when it is first written** — `project-status.md`
  claimed the cognitive-core flags "flipped to `true` (measured and
  adopted)" while `graph.py` still defaults `PLANNER_ENABLED` to `false`;
  third occurrence of the same class after the README security section and
  `PLAN.md`'s authority claim (docs/briefs/archives/repository-gaps.md, point 2).

## Operations

- **Env vars are read at import** — cognitive-core flags silently reset to
  `false` by a `up -d --build` (bug #53).
- **A file copied at build time needs a rebuild** — llama-server's
  `entrypoint.sh`.
- **Two lists that must stay in sync get a test** — `PLANNING_MODE`
  missing from `CAMPAIGN_ENV_FLAGS` while its twin was fixed (#48).
- **The brief before the code** — brief 1d lost in a session crash.
- **One commit, one nature of change** — rule set during the
  restructuring effort to preserve file history.
- **A service nothing depends on is debt** — the ocr-service question
  raised at GhostDesk removal.
