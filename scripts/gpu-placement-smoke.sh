#!/usr/bin/env bash
# One-off before/after smoke for the explicit gpu_split vs autosplit
# (docs/briefs/deterministic-gpu-placement.md, step 4). Isolates ONE
# variable: CUDA_DEVICE_ORDER=PCI_BUS_ID (docker-compose.yml) stays pinned
# in BOTH arms — unpinning it for the "before" run would make that run
# itself non-reproducible, which defeats a controlled comparison. Only
# services/tabbyapi/config.yml's gpu_split/gpu_split_auto toggle.
#
# Same fixed 4-task subset as scripts/visual-capture-smoke.sh, for the same
# reason: comparability doesn't depend on task semantics here, but does
# depend on using the exact same set both times, and this one is already
# validated.
#
# Judges (per the brief): decode throughput (tokens/s), prefill time,
# median time per task — read from the two campaign reports this produces.
#
# Delete this script once the default is decided and recorded in
# docs/history.md (CLAUDE.md, Scripts: one-off campaign scripts are
# expected to be short-lived).
#
# Usage: bash scripts/gpu-placement-smoke.sh
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

TASKS="A2_schema_references,A3_contact_conges,B1_conge_hard,D1_cible_inexistante"
REPS=3
CONFIG_PATH="services/tabbyapi/config.yml"

if ! git diff --quiet -- "$CONFIG_PATH"; then
  echo "Uncommitted changes in $CONFIG_PATH — commit or stash before running this script." >&2
  exit 1
fi

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

wait_for_tabbyapi_loaded() {
  local waited=0 timeout=180 interval=5
  until docker compose logs tabbyapi 2>/dev/null | tail -50 | grep -q "Model successfully loaded"; do
    if (( waited >= timeout )); then
      echo "tabbyapi n'a pas fini de charger le modèle après ${timeout}s — voir docker compose logs tabbyapi" >&2
      exit 1
    fi
    sleep "$interval"
    waited=$((waited + interval))
  done
}

restore_config() {
  git checkout -- "$CONFIG_PATH"
}
trap restore_config EXIT

echo "=== Starting self-hosted fixtures (profile test-fixtures) ==="
docker compose --profile test-fixtures up -d fixture-catalog fixture-docs fixture-hr-app fixture-admin fixture-perception

echo ""
echo "=== BEFORE: autosplit (gpu_split_auto: true, gpu_split: []) ==="
sed -i \
  -e 's/^  gpu_split_auto: false/  gpu_split_auto: true/' \
  -e 's/^  gpu_split: \[5, 14\]/  gpu_split: []/' \
  "$CONFIG_PATH"
grep -n "gpu_split" "$CONFIG_PATH"
docker compose up -d --force-recreate tabbyapi
wait_for_tabbyapi_loaded
wait_for_container_ready mcp-client 8003
wait_for_container_ready langgraph-agent 8000
scripts/run-campaign.sh --suite v2 --tasks "$TASKS" --reps "$REPS" --label "gpu-placement-smoke-before"

echo ""
echo "=== AFTER: manual split (gpu_split: [5, 14]) ==="
restore_config
grep -n "gpu_split" "$CONFIG_PATH"
docker compose up -d --force-recreate tabbyapi
wait_for_tabbyapi_loaded
wait_for_container_ready mcp-client 8003
wait_for_container_ready langgraph-agent 8000
scripts/run-campaign.sh --suite v2 --tasks "$TASKS" --reps "$REPS" --label "gpu-placement-smoke-after"

echo ""
echo "=== Done. Compare decode throughput / prefill time / median task time between: ==="
echo "    docs/campaigns/*_campaign-v2_gpu-placement-smoke-before.md"
echo "    docs/campaigns/*_campaign-v2_gpu-placement-smoke-after.md"
echo "Record the read in docs/history.md."
