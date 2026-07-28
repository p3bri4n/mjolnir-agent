# Runbook — rebuild/restart commands

Consolidated from mentions already scattered across README.md/docs/architecture
(restructuring effort, see docs/briefs/restructuration-et-anglais.md,
phase 3) — no content rewrite, just a single entry point for these
commands rather than having them scattered around.

- **`docker compose up -d`**: normal stack startup (see README, Quick
  start).
- **`docker compose restart langgraph-agent`**: after a tool-schema change
  on the mcp-client side (`_tools_schema_cache` is filled once for the
  process's lifetime and never invalidated — see
  `campaign_preflight.check_tools_schema`).
- **`docker compose up -d --force-recreate langgraph-agent`**: after a
  `.env` change affecting a variable read at module level in
  `app/graph.py` (e.g. the cognitive-core flags, see
  docs/architecture/autonomy.md) — a plain `restart` does not re-read
  `.env`, the process doesn't restart.
- **`docker compose up -d --build tabbyapi`**: after a TabbyAPI image
  rebuild, so the running container matches the last image built
  (`campaign_preflight.check_tabbyapi_image_fresh` otherwise refuses a
  campaign).
- **`docker compose down` / `up`**: preserves the named volumes
  (`qdrant-data`, `open-webui-data`) — **`docker compose down -v`**
  deletes them; never type it without explicitly meaning to (see
  docs/architecture/observability.md, Data persistence).
