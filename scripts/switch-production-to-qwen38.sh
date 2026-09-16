#!/usr/bin/env bash
# Phase 3 of docs/briefs/qwen3.8-27b-evaluation.md: switches PRODUCTION's
# actual served model from Qwen3.6 to Qwen3.8. This is the first script
# this effort runs against the real stack rather than the isolated PoC
# (poc/qwen3.8/) — everything it needs was already verified there first:
# runtime version (Phase 0), thinking-control migration (Phase 1), VRAM/
# tool-calling/throughput/MTP acceptance (Phase 2), gpu_split stability
# (this session). services/tabbyapi/config.local.yml and
# campaign_preflight.py's EXPECTED_GPU_DEVICES are already updated to
# [10, 13] — this script only repoints the model symlink and reloads.
#
# Reversible: models/agent-llm is a symlink, not a copy — switching back
# to Qwen3.6 is the same command with the other target (printed at the
# end of this script for convenience).
#
# Usage: bash scripts/switch-production-to-qwen38.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

AGENT_LLM_LINK="models/agent-llm"
QWEN36_TARGET="qwen3.6-27b-exl3-3.50bpw"
QWEN38_TARGET="qwen3.8-27b-exl3-4.50bpw"

if [ ! -L "$AGENT_LLM_LINK" ]; then
  echo "$AGENT_LLM_LINK is not a symlink — expected it to be one (see" >&2
  echo "docs/architecture/inference-backend.md). Aborting rather than guessing." >&2
  exit 1
fi

CURRENT_TARGET="$(readlink "$AGENT_LLM_LINK")"
echo "Current agent-llm target: $CURRENT_TARGET"

if [ ! -d "models/$QWEN38_TARGET" ]; then
  echo "models/$QWEN38_TARGET not found — check the path before proceeding." >&2
  exit 1
fi

echo
echo "=== This repoints PRODUCTION's served model: $CURRENT_TARGET -> $QWEN38_TARGET ==="
read -rp "Continue? [y/N] " confirm
[[ "${confirm:-N}" =~ ^[Yy]$ ]] || { echo "Aborted." >&2; exit 1; }

ln -sfn "$QWEN38_TARGET" "$AGENT_LLM_LINK"
echo "Repointed: $(readlink "$AGENT_LLM_LINK")"

echo
echo "=== Recreating tabbyapi (config.local.yml already set to gpu_split [10, 13]) ==="
docker compose up -d --force-recreate tabbyapi

echo "Waiting for load (timeout 180s)..."
waited=0
until docker compose logs tabbyapi 2>/dev/null | tail -100 | grep -qE "Model loaded in|Serving OAI API on"; do
  if docker compose logs tabbyapi 2>/dev/null | tail -100 | grep -qE "Traceback|CUDA out of memory|CUDA error|fatal error|Insufficient VRAM"; then
    echo "LOAD FAILED — log tail:" >&2
    docker compose logs tabbyapi | tail -80 >&2
    echo >&2
    echo "Revert: ln -sfn $QWEN36_TARGET $AGENT_LLM_LINK && docker compose up -d --force-recreate tabbyapi" >&2
    echo "(and restore services/tabbyapi/config.local.yml's gpu_split to [5, 14] first)" >&2
    exit 1
  fi
  if (( waited >= 180 )); then
    echo "TIMEOUT — log tail:" >&2
    docker compose logs tabbyapi | tail -80 >&2
    exit 1
  fi
  sleep 5
  waited=$((waited + 5))
done
echo "Loaded."

echo
echo "=== Real triplet + model check ==="
docker exec tabbyapi sh -c 'pip show exllamav3 2>/dev/null | grep -i "^Version"'
curl -s http://localhost:5000/v1/model 2>/dev/null | head -c 300 || true
echo

echo
echo "=== Done. Run the Qwen3.8 comparison campaign: ==="
echo '  scripts/run-campaign.sh --suite v2 --label "qwen38-eval-phase3-qwen38"'
echo
echo "To revert to Qwen3.6 afterward:"
echo "  ln -sfn $QWEN36_TARGET $AGENT_LLM_LINK"
echo "  (restore services/tabbyapi/config.local.yml's gpu_split to [5, 14] first)"
echo "  docker compose up -d --force-recreate tabbyapi"
