#!/usr/bin/env bash
# effort 1.3, docs/briefs/effort-1.3-parallel-campaigns.md, Phase 3
# follow-up: the decisive measurement missed its primary judge (x1.10
# instead of ~x2) because TabbyAPI's KV cache pool (cache_size: 49152)
# gets evicted under 3 concurrent growing conversations. This script
# tests ONE variable — cache_size raised to 65536 (candidate computed
# from already-measured GPU margins, see the brief for the arithmetic
# and its two flagged, unverified assumptions) — nothing else changes.
#
# Sequence: bump cache_size -> restart tabbyapi -> confirm it actually
# loaded (never leaves tabbyapi down: restores + restarts on failure) ->
# report real VRAM usage against the computed margin -> a SMALL smoke
# (N=3, the two tasks that thrashed worst: A1+A2, 1 rep each) checking
# whether cached_tokens still collapses to the tool-schema floor.
#
# Does NOT revert cache_size on a clean run: if the smoke looks good, the
# decisive re-measurement (same 6-task x3-rep subset, N=3 only — the N=1
# baseline is already measured, no need to redo it) should run with the
# SAME cache_size, printed as the next command at the end.
#
# Usage: bash scripts/probe-cache-size-headroom.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

CONFIG_PATH="services/tabbyapi/config.yml"
NEW_CACHE_SIZE=65536
OLD_CACHE_SIZE=49152

if ! git diff --quiet -- "$CONFIG_PATH"; then
  echo "Uncommitted changes in $CONFIG_PATH — commit or stash before running this script." >&2
  exit 1
fi

RELOAD_CONFIRMED=false
restore_and_restart() {
  git checkout -- "$CONFIG_PATH"
  echo "Restored $CONFIG_PATH, restarting tabbyapi with the original cache_size..." >&2
  docker compose up -d --force-recreate tabbyapi || true
}
trap 'if [ "$RELOAD_CONFIRMED" != "true" ]; then restore_and_restart; fi' EXIT

wait_for_tabbyapi_loaded() {
  local waited=0 timeout=180 interval=5
  until docker compose logs tabbyapi 2>/dev/null | tail -80 | grep -q "Model successfully loaded"; do
    if (( waited >= timeout )); then
      return 1
    fi
    sleep "$interval"
    waited=$((waited + interval))
  done
  return 0
}

echo "=== Applying cache_size: $OLD_CACHE_SIZE -> $NEW_CACHE_SIZE ==="
sed -i "s/^  cache_size: $OLD_CACHE_SIZE/  cache_size: $NEW_CACHE_SIZE/" "$CONFIG_PATH"
grep -n "cache_size" "$CONFIG_PATH"

docker compose up -d --force-recreate tabbyapi
if ! wait_for_tabbyapi_loaded; then
  echo "tabbyapi failed to load within 180s at cache_size=$NEW_CACHE_SIZE — likely OOM." >&2
  echo "Last 30 log lines:" >&2
  docker compose logs tabbyapi 2>/dev/null | tail -30 >&2
  exit 1
fi
RELOAD_CONFIRMED=true
echo "tabbyapi loaded successfully at cache_size=$NEW_CACHE_SIZE."

echo ""
echo "=== Real VRAM usage (compare against the computed margin) ==="
nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv

echo ""
echo "=== Smoke: N=3, A1+A2 (the two tasks that thrashed worst), 1 rep each ==="
for c in mcp-client langgraph-agent; do
  RUNNING="$(docker compose ps --services --status running)"
  if ! grep -qx "$c" <<<"$RUNNING"; then
    echo "Container '$c' is not running — start the stack first (docker compose up -d)." >&2
    exit 1
  fi
done
WEB_TASKS_WORKERS=3 scripts/run-campaign.sh --suite v2 \
  --tasks A1_reconciliation_croisee,A2_schema_references \
  --reps 1 --label "effort1-3-cache-size-smoke"

echo ""
echo "=== Done. Check the smoke's raw campaign JSON for cached_tokens no longer collapsing: ==="
echo "  python3 -c \"import json; d=json.load(open('docs/campaigns/campaign-<id>-effort1-3-cache-size-smoke.json')); [print(s['cached_tokens']) for r in d['runs'] for s in r.get('tabbyapi_raw_samples') or []]\""
echo ""
echo "If clean (no repeated collapse to ~6656), re-run the decisive comparison (N=1 baseline already measured, 17.9 min):"
echo "  WEB_TASKS_WORKERS=3 scripts/run-campaign.sh --suite v2 \\"
echo "    --tasks A1_reconciliation_croisee,A2_schema_references,A3_contact_conges,A4_parcours_guide,D1_cible_inexistante,B1_conge_hard \\"
echo "    --reps 3 --label \"effort1-3-parallele-n3-cache65536\""
echo ""
echo "cache_size stays at $NEW_CACHE_SIZE (not reverted) — this script only reverts on a failed reload."
