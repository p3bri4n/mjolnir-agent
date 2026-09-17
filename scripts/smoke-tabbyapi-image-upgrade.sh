#!/usr/bin/env bash
# Phase 0, step 3 of docs/briefs/qwen3.8-27b-evaluation.md: the isolated
# PoC already confirmed exllamav3 1.5.0 loads the Qwen3.8 build cleanly
# (docs/engineering-log.md, "Qwen3.8-27B evaluation, Phase 0") and
# services/tabbyapi/Dockerfile's pinned digest has been bumped to match.
# This script isolates the ONE remaining variable before Qwen3.8 goes
# anywhere near production: does the runtime bump alone regress the
# CURRENT Qwen3.6 production model? Model/config.yml stay untouched here
# — only the base image changes.
#
# Same wait_for_tabbyapi_loaded pitfall already hit twice this effort:
# the log string changed between TabbyAPI releases ("Model successfully
# loaded" -> "Model loaded in N s"). This script greps for both, plus the
# generic failure strings, and never tears anything down silently.
#
# Usage: bash scripts/smoke-tabbyapi-image-upgrade.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

TASKS="A1,A2"
REPS=1
LABEL="qwen38-eval-phase0-image-upgrade-smoke"

if ! git diff --quiet -- services/tabbyapi/config.yml; then
  echo "Uncommitted changes in services/tabbyapi/config.yml — this smoke expects the" >&2
  echo "CURRENT Qwen3.6 production config untouched. Commit or stash first." >&2
  exit 1
fi
if grep -q "model_name: agent-llm" services/tabbyapi/config.yml; then
  echo "Confirmed: config.yml still targets agent-llm (Qwen3.6) — only the image changes."
else
  echo "config.yml doesn't target agent-llm as expected — aborting, this smoke is meant" >&2
  echo "to test the image bump in isolation from any model change." >&2
  exit 1
fi

wait_for_tabbyapi_loaded() {
  local waited=0 timeout=180 interval=5
  until docker compose logs tabbyapi 2>/dev/null | tail -80 | grep -qE "Model loaded in|Model successfully loaded|Serving OAI API on"; do
    if docker compose logs tabbyapi 2>/dev/null | tail -80 | grep -qE "Traceback|CUDA out of memory|CUDA error|fatal error"; then
      echo "tabbyapi failed to load — see the log tail below." >&2
      docker compose logs tabbyapi | tail -60 >&2
      return 1
    fi
    if (( waited >= timeout )); then
      echo "tabbyapi hasn't confirmed a load after ${timeout}s — log tail below." >&2
      docker compose logs tabbyapi | tail -60 >&2
      return 1
    fi
    sleep "$interval"
    waited=$((waited + interval))
  done
  return 0
}

wait_for_container_ready() {
  local container="$1" port="$2" waited=0 timeout=90 interval=3
  until docker exec "$container" python3 -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:${port}/health', timeout=3)" \
    &>/dev/null; do
    if (( waited >= timeout )); then
      echo "$container doesn't respond on :${port}/health after ${timeout}s — see docker logs $container" >&2
      exit 1
    fi
    sleep "$interval"
    waited=$((waited + interval))
  done
}

echo "=== Starting self-hosted fixtures (profile test-fixtures) ==="
docker compose --profile test-fixtures up -d fixture-catalog fixture-docs fixture-hr-app fixture-admin fixture-perception

echo
echo "=== Building tabbyapi with the bumped Dockerfile digest ==="
docker compose build tabbyapi

echo
echo "=== Recreating tabbyapi, model unchanged (Qwen3.6 / agent-llm) ==="
docker compose up -d --force-recreate tabbyapi
if ! wait_for_tabbyapi_loaded; then
  echo "Image upgrade regressed the load itself — do NOT proceed to Phase 1/2 on this image." >&2
  exit 1
fi
echo "tabbyapi loaded cleanly on the upgraded image."

echo
echo "=== Real triplet on the now-production image ==="
docker compose exec -T tabbyapi sh -c 'pip show exllamav3 torch | grep -E "^Name|^Version"'

wait_for_container_ready mcp-client 8003
wait_for_container_ready langgraph-agent 8000

echo
echo "=== Smoke: $TASKS, n=$REPS (regression check, not a statistical read) ==="
scripts/run-campaign.sh --suite v2 --tasks "$TASKS" --reps "$REPS" --label "$LABEL"

echo
echo "=== Done. Compare this run's report against the last known-good Qwen3.6 ==="
echo "baseline in docs/campaigns/ (CuP, per-task success, median time) — this is a"
echo "regression check for the image bump alone, record the read in"
echo "docs/engineering-log.md before touching the 3.8 weights (Phase 0, step 4)."
