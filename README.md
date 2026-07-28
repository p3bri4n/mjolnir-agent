# Mjolnir agent

![CI](https://github.com/p3bri4n/mjolnir-agent/actions/workflows/ci.yml/badge.svg)

![Logo](docs/assets/logo.jpeg)

Docker Compose stack for a local AI agent: Open WebUI → LangGraph Agent →
(Skill Manager / Context Manager / MCP Client) → TabbyAPI.

## Quick start

```bash
cp .env.example .env
# edit .env: WORKSPACE_HOST_PATH must be the ABSOLUTE path of ./workspace on the host
# (required because mcp-client mounts this path into containers it spawns itself)
# place the model's EXL3 quant (safetensors + config.json + tokenizer,
# HuggingFace format) under ./models/agent-llm/ — TabbyAPI is the default
# backend, never downloaded automatically (see docs/architecture/inference-backend.md).
# For the alternative llama-server backend (.gguf), edit .env:
# LLAMA_MODEL_FILE/LLAMA_MMPROJ_FILE must match the files
# actually present in ./models

docker pull mcp/filesystem:latest
docker pull mcp/git:latest
docker pull mcp/playwright:latest   # persistent HTTP server (playwright-mcp service), see docs/resolved-bugs.md
docker compose --profile build-only build mcp-terminal-build   # builds the local mcp-terminal:local image

docker compose up -d
```

UI available at http://localhost:3000 (Open WebUI). Rebuild/restart
commands: see `docs/operations/runbook.md`.

## Layout

```
docker-compose.yml
.env.example
requirements-test.txt   shared test dependencies (pytest, respx)
services/
  langgraph-agent/   OpenAI-compatible API + LangGraph graph (autonomy,
                     human supervision — see docs/architecture/)
    app/
    tests/
  skill-manager/      lists/matches skills (./skills)
    app/
    tests/
  context-manager/    RAG + memory (Qdrant + sentence-transformers)
    app/
    tests/
  mcp-client/          spawns filesystem/git/terminal on demand (docker.sock) ;
                       browser/desktop/ocr are persistent HTTP servers
                       (mcp-client connects to them over Streamable HTTP)
    app/
    tests/
  mcp-terminal/        homegrown "terminal" MCP server, strict allowlist
    server.py
    tests/
  ghostdesk/           official YV17labs image, virtual desktop driven by
                       the agent (separate docker-compose service, Streamable HTTP)
  playwright-mcp/      official mcp/playwright image, browser driven by
                       the agent (separate docker-compose service, native HTTP
                       server — see docs/resolved-bugs.md)
  llama-server/        build of the llama.cpp fork serving the model (alternative
                       backend — see docs/architecture/inference-backend.md)
  ocr-service/         supplementary OCR for VLM grounding (PaddleOCR CPU,
                       find_text/read_screen — see docs/architecture/autonomy.md)
    app/
    tests/
  dashboard/           local observability cockpit — see
                       docs/architecture/observability.md
    app/
      static/          vanilla HTML/JS page served as-is (no build step)
    tests/
skills/     to be filled in (one subfolder per skill, each with a SKILL.md)
workspace/  shared with the filesystem/git/terminal MCP servers, and
            with langgraph-agent for the audit log (.audit/, see
            docs/architecture/tool-supervision.md)
models/     weights (.gguf) of the model and multimodal projector served by
            llama-server — never downloaded automatically, see
            docs/architecture/inference-backend.md
```

## Documentation

- `docs/architecture/inference-backend.md` — TabbyAPI/llama-server, image
  conversion, adaptive thinking.
- `docs/architecture/autonomy.md` — plan → act → verify → replan loop
  ("cognitive core" Phase 1), supplementary OCR.
- `docs/architecture/tool-supervision.md` — human approval, reversibility
  tiers, session grants, audit log.
- `docs/architecture/observability.md` — dashboard, data persistence.
- `docs/operations/testing.md` — per-service test suites, SSE streaming.
- `docs/operations/runbook.md` — rebuild/restart commands.
- `docs/project-status.md` — progress status (changes at every checkpoint).
- `PLAN.md` — roadmap (changes rarely, source of truth).
- `docs/history.md` / `docs/resolved-bugs.md` — progress log and resolved bugs
  (consult by targeted search, never read in full — see `CLAUDE.md`).
- `docs/briefs/` — briefs for ongoing work.

## Known, accepted limitations (design choices, not bugs)

- **`mcp-terminal` does not expose a free-form shell**: strict allowlist
  (`ls`, `pwd`, `cat`, `git status`), confined to `/workspace`. Extend this
  list with caution: every added command is a new potential attack surface.
- **`mcp-client` mounts `/var/run/docker.sock`**: equivalent to root access
  on the host. Acceptable for local use; should be replaced by a filtering
  socket proxy before any network exposure.
- **Skill matching and RAG are deliberately simplistic** (naive keyword
  match, no reranker) — to be strengthened if the volume of skills/documents
  grows.
- **`ghostdesk` (the "desktop" MCP server) runs with `cap_add: SYS_ADMIN`
  and exposes a shell**: a much larger attack surface than `mcp-terminal`
  (no allowlist, full GUI control). Never expose it beyond the internal
  `agent-net` network — only the noVNC port (6080) is published on the
  host, deliberately, to observe the agent driving the desktop; the MCP
  port (3000) is not. `mcp-terminal` remains the default tool for simple
  commands; `ghostdesk` is only used for GUI control that genuinely
  warrants it — the two knowingly coexist rather than one replacing the
  other. Access: http://localhost:6080 once the service is started,
  password = `GHOSTDESK_VNC_PASSWORD` (see `.env`).
- **Historical limitation lifted**: GhostDesk's screenshot/guided-click
  tools were not usable by the agent as long as the model served
  (Qwen2.5-Coder, via vLLM) was not multimodal. The default backend is now
  `llama-server` (see docs/architecture/inference-backend.md), serving
  Qwen3.6-35B-A3B with a multimodal projector (`--mmproj`) — the agent can
  therefore now receive and interpret GhostDesk screenshots. A distinct
  limitation remains, however, now mitigated but not resolved: the
  grounding accuracy (aiming at the right on-screen element) of a
  general-purpose vision model. `ocr-service` (see
  docs/architecture/autonomy.md) compensates for TEXTUAL elements via
  `find_text`/`read_screen` (exact OCR coordinates rather than a visual
  estimate); elements without text (icons) are still estimated visually by
  the VLM, with no dedicated UI-element detection (OmniParser-style,
  explicitly out of scope for now).
- **`ghostdesk` is a stateful, persistent HTTP MCP server** (desktop/VNC
  session), unlike the other MCP servers in the project, which are spawned
  as ephemeral STDIO processes by `mcp-client` (`docker run -i --rm` per
  call). It runs continuously as a separate `docker-compose` service;
  `mcp-client` connects to it via `streamablehttp_client` (`mcp` SDK ≥ 1.8,
  hence the bump from `mcp==1.2.0` to `mcp==1.9.4` in
  `services/mcp-client/requirements.txt`), authenticated with a bearer
  token (`GHOSTDESK_AUTH_TOKEN`, see `.env.example`).
- **`playwright-mcp` (the "browser" server) has been a persistent HTTP
  server since the fix documented in detail in `docs/resolved-bugs.md`** —
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
  docs/history.md, T5): `playwright-mcp` keeps its `--isolated` browser
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
- **Click accuracy with Qwen models**: these models natively reason in a
  normalized 0-1000 coordinate space, whereas GhostDesk expects native
  screen pixels by default (documented by GhostDesk) — without
  correction, clicks land next to their target. `mcp-client` therefore
  sends the `GhostDesk-Model-Space` header (value `GHOSTDESK_MODEL_SPACE`,
  default `1000`) on every HTTP call to GhostDesk (`_run_on_server`,
  `services/mcp-client/app/main.py`). Clear it (`GHOSTDESK_MODEL_SPACE=`)
  if the served model switches to a frontier model (Claude, GPT-4o), which
  natively works in screen pixels. This fix does not solve grounding
  itself (aiming at the right element remains imprecise with a
  general-purpose vision model) — see the limitation above on the absence
  of OCR/UI-element detection.
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
