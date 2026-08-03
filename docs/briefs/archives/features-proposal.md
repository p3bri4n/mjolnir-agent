# README — proposed opening + features section

**Status: closed.** Figures filled from the latest full campaign
(`docs/campaigns/2026-07-28_campaign_post-rename-mjolnir-v2.md`, 29/33 —
not the later `episode-compaction-enabled` run, an experimental flag-flip
requalified "non concluante", not representative of default behavior).
Deviation from this brief: the "Security posture" section as drafted
contained several unverified/false claims (PromptGuard, egress firewall,
"no network access" for mcp-terminal, prompt-injection resistance as a
benchmark family) — none of these exist yet (see `PLAN.md` Phase 3, not
started). Rewritten to keep only what's verified against the installed
code, with the rest moved to a new `## Roadmap` section instead of
`## Features`. New rule added to `CLAUDE.md` #9 as a result: any
capability claim in the README/public docs is verified before
publication; planned features go under Roadmap, never Features.

> Drop-in replacement for the current intro (lines 1–12), plus a new
> `## Features` section before `## Quick start`. English, repo style.
> Figures marked `<X>` must be filled from the latest campaign report — never
> from memory.

---

# Mjolnir agent

![CI](https://github.com/p3bri4n/mjolnir-agent/actions/workflows/ci.yml/badge.svg)

![Logo](docs/assets/logo.jpeg)

**A fully local, autonomous web agent that runs on consumer NVIDIA GPUs —
with human approval tiers, mechanical guardrails, and a benchmark that
measures whether any of it actually works.**

No API keys, no data leaving the machine. The whole stack is dockerised
behind an egress firewall. Tested on Qwen3.6-27B (EXL3) across a dual-GPU
setup (RTX 4070 Ti Super + RTX 5060 Ti).

Open WebUI → LangGraph agent → (Skill Manager / Context Manager / MCP
Client) → TabbyAPI.

## Features

### Autonomy that reports its own failures

- **Explicit plan**: tasks are decomposed into subtasks with a stated
  success criterion each, before any action.
- **Post-action verification**: every tool call is checked against the
  criterion stated beforehand — the agent cannot silently proceed on a
  failed step.
- **Failure budget with mandatory alternative**: a retry must use a
  different strategy, not repeat the same call. Budget exhausted → replan;
  replans exhausted → honest failure report with the state reached.
- **Plan validation pipeline**: programmatic heuristics, then an optional
  LLM judge, then human approval — tiered by the plan's riskiest action.

### Human supervision that scales

- **Approval tiers by action nature**: read is auto-approvable, reversible
  writes are covered by a session grant, engagements (submissions, uploads,
  arbitrary code) always require individual approval.
- **Never-grantable tools**: arbitrary JS execution can never be blanket-
  approved, whatever the session state.
- **Approve a plan, not twenty clicks**: reviewing one trajectory replaces
  N action-by-action confirmations.

### Guardrails in code, not in prompts

- **URL-fabrication guardrail**: navigation to an address never observed in
  a page is refused mechanically, with the real links offered instead.
- **`browser_extract`**: a named, declarative extraction tool (single or
  bulk) replacing model-written JavaScript — same capability, auditable, at
  read tier.
- **Structured reporting**: statuses and budgets travel through tool-call
  schemas, never through textual conventions the model is asked to honour.

### Hybrid perception

- **DOM first** (Playwright MCP: accessibility tree, real links), **vision
  as fallback** (GhostDesk desktop capture), **OCR** for exact text.
- **Affordance-preserving truncation**: page content may be summarised, the
  inventory of links, buttons and fields never is.

### Measured, not asserted

- **Task-level benchmark**: <N> web tasks on self-hosted fixtures, with
  programmatic assertions — no LLM-as-judge on the final score.
- **Permanent judges**: success rate, median time per task, tokens per task,
  URL-fabrication count, verification coverage, human interventions,
  prefill cost.
- **Campaign persistence**: per-run JSON with effective configuration (git
  commit, image digests, behaviour flags) — every campaign can say *which
  agent* it measured.
- **Full audit trail**: intentions, tool results and model messages
  persisted as JSONL, which is how most of this project's bugs were
  diagnosed without re-running anything.

Latest campaign: **<score>** — see `docs/campaigns/` for the full history,
and `docs/methodology.md` for how these numbers are produced and why some
of them were thrown away.

### Security posture

- Egress firewall on the agent container; MCP terminal restricted to a
  command whitelist with no network access.
- PromptGuard screening; browser profile isolated from personal credentials.
- Prompt-injection resistance is a benchmark family, not a claim — page
  content is treated as untrusted input.

### Runs on hardware you own

- Dual-GPU heterogeneous setups supported (documented Ada + Blackwell
  configuration, including the tensor-split crash diagnosis and its
  workaround in `docs/resolved-bugs.md`).
- Two interchangeable inference backends: TabbyAPI/ExLlamaV3 (default) or
  llama.cpp.
