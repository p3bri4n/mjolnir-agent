#!/usr/bin/env bash
# One-off overhead smoke for CAMPAIGN_VISUAL_CAPTURE (docs/briefs/
# campaign-visual-feedback.md, minimal subset — see that file's "Status"
# section). Point 6 of the implementation instruction: the flag's default
# must not be set before a real with/without number exists — this script
# produces that number. Same tasks, same n, one variable (the flag).
#
# Rebuilds mcp-client and langgraph-agent first (both changed: the
# capture logic itself, and thread_id now forwarded in every /call) —
# unlike scripts/point3-smoke.sh, this does NOT assume the stack is
# already on the right code.
#
# Delete this script once the default is decided and recorded in
# docs/history.md (CLAUDE.md, Scripts: one-off campaign scripts are
# expected to be short-lived).
#
# Usage: bash scripts/visual-capture-smoke.sh
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# A handful of tasks spanning different interaction shapes (short/long,
# single/multi-site, forms) rather than one family — the overhead this
# measures (an extra screenshot round-trip per browser tool call) doesn't
# depend on task semantics, but duration comparability across the two
# runs does depend on using the exact same set both times.
TASKS="A2_schema_references,A3_contact_conges,B1_conge_hard,D1_cible_inexistante"
REPS=3

wait_for_container_ready() {
  # port must match the service's own EXPOSE/uvicorn --port (Dockerfile) —
  # mcp-client is 8003, langgraph-agent is 8000, they are NOT the same.
  local container="$1" port="$2" waited=0 timeout=90 interval=3
  until docker exec "$container" python3 -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:${port}/health', timeout=3)" \
    &>/dev/null; do
    if (( waited >= timeout )); then
      echo "$container ne répond pas sur :${port}/health après ${timeout}s — voir docker logs $container" >&2
      exit 1
    fi
    sleep "$interval"
    waited=$((waited + interval))
  done
}

echo "=== Starting self-hosted fixtures (profile test-fixtures) ==="
docker compose --profile test-fixtures up -d fixture-catalog fixture-docs fixture-hr-app fixture-admin fixture-perception

echo "=== Rebuilding mcp-client and langgraph-agent (code changed on both) ==="
docker compose build mcp-client langgraph-agent

for capture in false true; do
  label="visual-capture-smoke-${capture}"
  echo ""
  echo "=== CAMPAIGN_VISUAL_CAPTURE=${capture} ==="
  export CAMPAIGN_VISUAL_CAPTURE="$capture"
  docker compose up -d --force-recreate mcp-client langgraph-agent
  wait_for_container_ready mcp-client 8003
  wait_for_container_ready langgraph-agent 8000
  scripts/run-campaign.sh --suite v2 --tasks "$TASKS" --reps "$REPS" --label "$label"
done

echo ""
echo "=== Done. Compare median duration per task between the two reports: ==="
echo "    docs/campaigns/*_campaign-v2_visual-capture-smoke-false.md"
echo "    docs/campaigns/*_campaign-v2_visual-capture-smoke-true.md"
echo "Record the read in docs/history.md, then decide CAMPAIGN_VISUAL_CAPTURE's"
echo "default per point 6: negligible -> true; otherwise -> true in smoke runs,"
echo "false in campaigns, and say so."

echo ""
echo "=== Restoring mcp-client/langgraph-agent to their default config ==="
unset CAMPAIGN_VISUAL_CAPTURE
docker compose up -d --force-recreate mcp-client langgraph-agent
wait_for_container_ready mcp-client 8003
wait_for_container_ready langgraph-agent 8000
