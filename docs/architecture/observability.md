# Observability and data persistence

Content moved as-is from README.md (restructuring effort, see docs/briefs/restructuration-et-anglais.md, phase 3) — no rewrite at this stage.

Local single-page web cockpit (http://localhost:8090 by default,
`DASHBOARD_PORT`): llama-server inference metrics (decode/prefill
throughput in tok/s, context occupied per slot), detailed breakdown of the
context built by langgraph-agent (system prompt, skills, tool schema,
history, images — see `POST /context` below), and GPU VRAM.

**Architecture**: `GET /api/snapshot` aggregates in parallel, each source
best-effort (a down source returns its section as `null`, never a global
500, systematic 200 status — the dashboard polls this endpoint every 2s):
`llama-server` (`/metrics`, Prometheus format parsed by a homegrown
minimal parser, `app/prometheus.py`; `/slots`), `langgraph-agent`
(`/threads/recent` then `POST /context` for the resolved thread) and
`nvidia-smi` as a subprocess (VRAM, `app/gpu.py`). The page (`GET /`,
vanilla HTML/JS, no external dependency) never talks directly to
llama-server/langgraph-agent: only Open WebUI and the dashboard have a
port published on the host, everything else is reachable only via the
internal `agent-net` network — hence the backend-side aggregation in the
dashboard rather than calls from the browser.

**`POST /context` (langgraph-agent, `app/graph.py:describe_context`)**:
breaks a thread's persisted context down into approximate blocks
(`system`/`skills`/`tools_schema`/`history_text`/`images`/`pending`), each
with an estimated token count (`estimate_tokens`, ~3.5 characters/token —
not an exact tokenizer, deliberately out of scope, see below) and a fixed
per-image allowance (`IMAGE_TOKEN_ESTIMATE`, default `1500`, an exact
count would depend on the served model's visual tokenizer). The tool
schema is measured from the cache already filled by `_get_bound_llm`
(never recomputed: `/context` stays strictly read-only, like `/pending`).
Thread unknown to the checkpointer -> 200 with empty blocks rather than a
404, so as not to turn the dashboard's continuous polling into
client-side error noise.

**`GET /tools/schema` (langgraph-agent)**: tool names as ACTUALLY seen by
this process (`_tools_schema_cache`), not those served by mcp-client at
call time — the distinction has bitten in real conditions (see
docs/engineering-log.md, "tool-schema cache bug"): this cache is filled once for
the process's lifetime and never invalidated, so restarting mcp-client
alone can leave langgraph-agent answering with a stale schema. Read-only,
like `/pending`/`/context`. Consumed by the web-task harness's campaign
preamble (`tests_integration/campaign_preflight.py`, see
`docs/briefs/phase-1-coeur-cognitif.md`) to refuse a campaign BEFORE its
first run if this schema is out of sync with mcp-client's.

**Thread selection (`GET /threads/recent`)**: langgraph-agent never has a
stable conversation identifier on the Open WebUI side (see below,
`_derive_thread_id`); an in-process, never-persisted registry (consistent
with the `MemorySaver` checkpointer itself being in-memory) keeps the 5
most recently seen threads (fed by `/v1/chat/completions` and `/approve`,
never by the purely read-only `/pending` or `/context` endpoints
themselves). The page selects the most recent one by default, with a
dropdown to pick another.

**VRAM (`ENABLE_GPU_STATS`, default `false`)**: `nvidia-smi
--query-gpu=... --format=csv,noheader,nounits` as a subprocess, disabled
by default — requires the nvidia runtime (`deploy` block commented out in
`docker-compose.yml`, to be uncommented along with this variable) for the
`nvidia-smi` binary to be present in the dashboard's `python:3.12-slim`
container, which otherwise has no need for GPU access at all.

Explicitly out of scope (see the original request): Prometheus/Grafana,
Langfuse, metrics persistence (everything is in memory, lost on restart),
alerting, auth (local network), WebSocket/SSE (2s polling is enough),
task telemetry (success rate), exact tokenizer.


## Data persistence

Two named Docker volumes persist across restarts and `docker compose down`
/ `up` (but not `docker compose down -v`, which deletes them):

- **`qdrant-data`**: contents of `context-manager`'s `documents` and
  `memory` collections (RAG and long-term memory).
- **`open-webui-data`** (`/app/backend/data`): conversations, user
  accounts, uploaded files and Open WebUI settings (SQLite database
  internal to the image).

Three bind-mounted directories persist natively, since they live directly
on the host's filesystem, independent of the containers' lifecycle:
`./workspace`, `./skills`, `./models`.

**Fixed point of attention**: `WEBUI_SECRET_KEY` used to be set nowhere.
Without this fixed key, Open WebUI generates a new one on every container
recreation, which invalidates all login sessions (and prevents decrypting
any stored secrets, such as OAuth tokens) even though the data itself
remains intact in the volume. Fixed: the key is now configured via `.env`
(see `.env.example`), to be generated once with `openssl rand -hex 32`.

The other services (`skill-manager`, `mcp-client`, `mcp-terminal`) are
stateless. `langgraph-agent` also remains conceptually stateless: it's
Open WebUI that resends the full conversation history on every
`/v1/chat/completions` request, not `langgraph-agent` persistently
keeping it. It does, however, now compile its graph with a checkpointer
(`MemorySaver`, **in-memory only**), needed for human supervision of tool
calls (see the next section): a service restart loses any pending
approval, which simply restarts a "fresh" conversation for the thread in
question — so no data is ever truly lost in the strict sense.
