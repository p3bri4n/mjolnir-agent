# Runbook — rebuild/restart commands

Consolidated from mentions already scattered across README.md/docs/architecture
(restructuring effort, see docs/briefs/archive/A4-restructuration-et-anglais.md,
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

## Pausing/resuming a campaign (docs/briefs/B2-campaign-control.md, Part 2)

Resource release is the OPERATOR's business, never the harness's — the
running pytest process only writes a sentinel-triggered pause state, it
never touches Docker itself.

1. In another terminal (while a campaign launched via
   `scripts/run-campaign.sh` is running): `scripts/run-campaign.sh --pause
   <campaign-id>` — drops a sentinel file, read at the NEXT run boundary
   (a run itself is never interrupted mid-flight).
2. Once the running harness has acknowledged the pause (`paused: true` in
   `docs/campaigns/<campaign-id>.progress.json`), release the GPU:
   `docker compose stop tabbyapi playwright-mcp fixture-catalog
   fixture-docs fixture-hr-app` — or pass `--release` to the `--pause`
   call above and let it wait for the confirmation and run this for you.
3. To resume later: bring the stopped services back
   (`docker compose up -d`), then `scripts/run-campaign.sh --resume
   <campaign-id>` — replays the FULL preflight and refuses if the
   effective config (commit, image digests, env flags) has drifted since
   the campaign started (diff printed, nothing runs). A resume more than
   7 days after the pause (`CAMPAIGN_RESUME_STALENESS_DAYS`) prints a
   warning, not a refusal — real sites may have moved since.
