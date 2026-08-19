# Mjolnir agent

<img src="docs/assets/logo.jpeg" alt="Mjolnir agent logo" width="200">

![CI](https://github.com/p3bri4n/mjolnir-agent/actions/workflows/ci.yml/badge.svg)
![License](https://img.shields.io/badge/license-MIT-green)
![Backend](https://img.shields.io/badge/backend-TabbyAPI%2FExLlamaV3-blue)

**A fully local, autonomous web agent for consumer dual-GPU hardware, no API
keys and no data leaving the machine — with human approval tiers, mechanical
guardrails, and a benchmark that measures whether any of it actually
works.**

Open WebUI → LangGraph agent → (Skill Manager / Context Manager / MCP
Client) → TabbyAPI. Tested on Qwen3.6-27B (EXL3) across a dual-GPU setup
(RTX 4070 Ti Super + RTX 5060 Ti).

<!-- Demo: docs/assets/demo.gif — scripted recording, see
     docs/briefs/readme-rework.md §2. Not embedded until that script has
     produced a real capture; a placeholder image would be worse than none. -->

- **22-task, 6-family benchmark** ([docs/benchmark-v2.md](docs/benchmark-v2.md)) — programmatic
  assertions, no LLM-as-judge on the score.
- **Latest full campaign: 53/56** across every family (one known limit:
  E2's vision-reading, see [docs/resolved-bugs.md](docs/resolved-bugs.md)).
- **91 archived campaigns**, every raw result kept — [docs/campaigns/](docs/campaigns/).
- Runs on **consumer dual-GPU hardware** (RTX 4070 Ti Super + RTX 5060 Ti,
  16 GB each).

Scores went from 16/33 to 30/33 over eleven campaigns on the original
11-task suite — [docs/notes/agent-benchmarking.md](docs/notes/agent-benchmarking.md).
Tested the hard way first, too:
[six weeks of llama.cpp on two mismatched GPUs](docs/notes/llamacpp-dual-gpu.md)
before switching engines.

Forty-two engineering rules, each with the incident and the numbers that
produced it — including the one where we measured that our own cognitive
core did nothing: [docs/lessons-learned.md](docs/lessons-learned.md).

## Quick start

```bash
cp .env.example .env
# edit .env: WORKSPACE_HOST_PATH must be the ABSOLUTE path of ./workspace on the host
# (required because mcp-client mounts this path into containers it spawns itself)
# place the model's EXL3 quant (safetensors + config.json + tokenizer,
# HuggingFace format) under ./models/agent-llm/ — TabbyAPI is the default
# backend, never downloaded automatically (see docs/architecture/inference-backend.md).

docker pull mcp/filesystem:latest
docker pull mcp/playwright:latest   # persistent HTTP server (playwright-mcp service), see docs/resolved-bugs.md

docker compose up -d
```

UI available at http://localhost:3000 (Open WebUI). Rebuild/restart
commands: see [docs/operations/runbook.md](docs/operations/runbook.md).

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
  as fallback** (`browser_take_screenshot`) when the DOM channel doesn't
  cover it (canvas, WebGL, images, native PDF).
- **Affordance-preserving truncation**: page content may be summarised, the
  inventory of links, buttons and fields never is.

### Measured, not asserted

- **Task-level benchmark**: 22 tasks across 6 families
  ([docs/benchmark-v2.md](docs/benchmark-v2.md)) — regression core (F), long-horizon multi-page
  tasks (A), policy compliance under session grants (B), hostile content
  and prompt injection (C), honesty on unanswerable/stale questions (D),
  perception channels (E) — programmatic assertions only, no LLM-as-judge
  on the final score.
- **Permanent judges**: success rate, average time per task, prompt tokens
  per task, URL-fabrication count, verification coverage, human
  interventions, prefill cost.
- **Campaign persistence**: per-run JSON with effective configuration (git
  commit, image digests, behaviour flags) — every campaign can say *which
  agent* it measured.
- **Live campaign progress**: a progress file updated at every run
  boundary and a read-only dashboard page (`/campaign`) — per-task ETA
  range, current run, running counters — no need to tail a terminal for a
  long-running campaign.
- **Visual feedback** (`CAMPAIGN_VISUAL_CAPTURE`, off by default pending
  an overhead measurement): the current run's latest browser viewport,
  refreshed on the same dashboard page — captured harness-side after
  every browser action, never entering the model's own context.
- **Pause/resume**: `run-campaign.sh --pause`/`--resume` — a resume
  replays the full preflight and refuses if the effective configuration
  (commit, image digests, env flags) drifted since the pause; per-segment
  cache metrics never pooled across a pause boundary.
- **Full audit trail**: intentions, tool results and model messages
  persisted as JSONL, which is how most of this project's bugs were
  diagnosed without re-running anything.

Latest full campaign: **53/56** across every family (one known limit: E2's
vision-reading) — see [docs/campaigns/](docs/campaigns/) for the full history, and
[docs/methodology.md](docs/methodology.md) for how these numbers are produced and why some of
them were thrown away.

### Security posture

- **Approval tiers with a never-grantable class**: some tools (arbitrary JS
  execution) can never be covered by a session grant, whatever the state.
- **Mechanical guardrails, not prompt instructions**: navigation to an
  address never observed in a page is refused in code; extraction goes
  through a named read-tier tool rather than model-written JavaScript.
- **Isolated browser profile**: `--isolated`, in-memory, never persisted —
  no personal cookies or credentials reachable by the agent.
- **Full audit trail**: intentions, tool results and model messages
  persisted, so any action taken can be reconstructed after the fact.
- Everything runs locally: no API keys, no data leaving the machine.

### Runs on hardware you own

- Dual-GPU heterogeneous setups supported (documented Ada + Blackwell
  configuration, including the tensor-split crash diagnosis and its
  workaround in [docs/resolved-bugs.md](docs/resolved-bugs.md)).
- A powerful inference backend for mixed Nvidia GPUs: TabbyAPI/ExLlamaV3

## Known, accepted limitations (design choices, not bugs)

- **`mcp-client` mounts `/var/run/docker.sock`**: equivalent to root access
  on the host. Acceptable for local use; should be replaced by a filtering
  socket proxy before any network exposure.
- **Skill matching and RAG are deliberately simplistic** (naive keyword
  match, no reranker) — to be strengthened if the volume of skills/documents
  grows.
- **`ocr-service` currently has no caller**: a proactive trigger
  (auto-OCR after detecting a visual-only element in a `browser_*`
  result) was built, then abandoned before going live — an empirical
  check against `fixture-visual-probe` found that canvas/WebGL/alt-less
  images leave no trace in `browser_snapshot`'s text for any after-the-
  fact heuristic to catch. Replaced by a tool-description routing hint on
  `browser_take_screenshot` instead (`mcp-client`, see
  [docs/architecture/autonomy.md](docs/architecture/autonomy.md)) — the model reads a screenshot
  directly rather than an OCR pass. `ocr-service` stays deployed as a
  standalone capability, kept for a possible future role, not currently
  wired into the agent loop.
- **`playwright-mcp` (the "browser" server) has been a persistent HTTP
  server since the fix documented in detail in [docs/resolved-bugs.md](docs/resolved-bugs.md)** —
  it used to be spawned as an ephemeral STDIO process
  (`docker run -i --rm mcp/playwright:latest` per call), losing all
  navigation state between two tool calls. The official image natively
  exposes an HTTP server mode (`--host 0.0.0.0 --port 8931`, Streamable
  HTTP endpoint `/mcp`); this alone was NOT enough, though, because
  Playwright MCP scopes its browser context (page, cookies, history) to
  the MCP SESSION, not the server process — `mcp-client` therefore also
  has to keep the "browser" session open between two HTTP calls
  (`_get_persistent_session`/`_persistent_sessions` in
  `services/mcp-client/app/main.py`), instead of reopening a fresh one
  every time as it does for the other servers.
- **Shared download volume `agent-downloads`** (Phase 1d-revised, see
  [docs/engineering-log.md](docs/engineering-log.md), T5): `playwright-mcp` keeps its `--isolated` browser
  profile (in memory, never persisted), but a download triggered on the
  page (a link/button with `Content-Disposition: attachment`) now lands in
  an EXPLICIT, shared path (`--output-dir=/downloads`, named volume
  `agent-downloads`) rather than in the container's internal filesystem
  (actual observed default: `/home/node/.playwright-mcp/`, never guessed
  correctly by the model). The filesystem MCP server mounts this same
  volume READ-ONLY (`services/mcp-client/app/main.py`, root `/downloads`
  in addition to `/projects`): the downloaded artifact is shared, never
  the browser's state. The system prompt documents this path explicitly
  (`DOWNLOAD_DIRECTIVE`, `app/graph.py`) rather than letting the model
  guess one.
- **Long-term memory (`context-manager`) never wired into the
  conversation**: `POST /remember` (stores a fact tied to a `user_id`,
  Qdrant `memory` collection) and `POST /retrieve` with
  `collection="memory"` exist and are tested at the `context-manager`
  level itself, but nothing in `langgraph-agent` calls them. The
  `retrieve_context` node (`app/graph.py`), which runs automatically on
  every turn, queries ONLY the `documents` collection (RAG) — never
  `memory`. Concretely: a fact stored via `/remember` never resurfaces on
  its own in a conversation, and there is currently no MCP tool nor slash
  command to store or recall one from the chat — only a direct call to the
  `context-manager` API (curl, etc.) allows using it.

## Roadmap

Planned, not implemented — tracked in [PLAN.md](PLAN.md):

- Egress firewall on the agent network (`agent-net` is currently a plain
  Docker bridge) and network restriction on spawned MCP containers.
- PromptGuard screening of untrusted web content.
- Approval tiers refined by action nature (read / reversible write /
  engagement) and per-task domain scope.
- A prompt-injection benchmark family (v2, family C) to measure resistance
  rather than assert it.

## Documentation

- [docs/layout.md](docs/layout.md) — directory tree, one line per service.
- [docs/architecture/inference-backend.md](docs/architecture/inference-backend.md) — TabbyAPI, image
  conversion, adaptive thinking.
- [docs/architecture/autonomy.md](docs/architecture/autonomy.md) — plan → act → verify → replan loop
  ("cognitive core" Phase 1), supplementary OCR.
- [docs/architecture/tool-supervision.md](docs/architecture/tool-supervision.md) — human approval, reversibility
  tiers, session grants, audit log.
- [docs/architecture/observability.md](docs/architecture/observability.md) — dashboard, data persistence.
- [docs/operations/testing.md](docs/operations/testing.md) — per-service test suites, SSE streaming.
- [docs/operations/runbook.md](docs/operations/runbook.md) — rebuild/restart commands.
- [docs/project-status.md](docs/project-status.md) — progress status (changes at every checkpoint).
- [docs/lessons-learned.md](docs/lessons-learned.md) — 42 engineering rules, each anchored to the
  incident and numbers that produced it.
- [docs/methodology.md](docs/methodology.md) — the six measurement principles behind those
  rules (read once, not every session).
- [PLAN.md](PLAN.md) — roadmap (changes rarely, source of truth).
- [docs/engineering-log.md](docs/engineering-log.md) / [docs/resolved-bugs.md](docs/resolved-bugs.md) — progress log and resolved bugs
  (consult by targeted search, never read in full — see [CLAUDE.md](CLAUDE.md)).
- [docs/briefs/](docs/briefs/) — briefs for ongoing work.

## Notes

Long-form field reports, kept separate from the reference docs above because
they are narrative and dated rather than current-state:

- [docs/notes/llamacpp-dual-gpu.md](docs/notes/llamacpp-dual-gpu.md) — six weeks debugging llama.cpp on two
  mismatched GPUs: build traps, a reasoning/tool-call parsing gap, and a
  cross-GPU crash diagnosis.
- [docs/notes/agent-benchmarking.md](docs/notes/agent-benchmarking.md) — what eleven measurement campaigns on
  the autonomous agent taught us, and the four that regressed on purpose.

## Troubleshooting

- **OOM at model load, or after switching quant/model**: `gpu_split_auto:
  true` is the shipped default and adapts to your hardware, but the
  combined VRAM still has to fit the quant plus `cache_size`. First thing
  to reduce is `cache_size` (`services/tabbyapi/config.yml`) — it's
  usually the biggest lever, well before switching quant. Pinning a manual
  `gpu_split` (per-model, per-machine, not a global setting) is a
  reproducibility choice for measurement, not a fix for insufficient VRAM
  — see [docs/architecture/inference-backend.md](docs/architecture/inference-backend.md), "GPU split".

