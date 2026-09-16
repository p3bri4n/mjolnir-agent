#!/usr/bin/env bash
# Phase 1's own judge, docs/briefs/qwen3.8-27b-evaluation.md: confirm,
# via the raw audit log transcript (not the campaign report), that the
# _should_suppress_thinking migration (app/graph.py) genuinely suppresses
# reasoning on the turns it fires on, and only those — mechanistic proof,
# not a success/failure score.
#
# ADAPTIVE_THINKING defaults to "false" in production and stays that way
# after this script — this only flips it for one smoke run, then reverts.
# Since the reference EXPECTED_AGENT_FLAGS now correctly expects "false"
# (docs/resolved-bugs.md #53), this run needs
# CAMPAIGN_EXPECTED_FLAGS_OVERRIDE to tell the preflight this specific
# campaign's flags are deliberately different, exactly what that
# mechanism exists for (see scripts/run-flag-sweep.sh for the same
# pattern).
#
# Vehicle: A2_schema_references (v2 suite) — already does several
# sequential browser_extract/navigate calls, all auto-approved
# (TIER_READ/TIER_REVERSIBLE), the exact shape needed for
# ADAPTIVE_THINKING to fire more than once in one run.
#
# Only REBUILDS langgraph-agent (code changed there, nothing else) — but
# `docker compose up -d langgraph-agent` still brings up any other
# service in the project that wasn't already running (observed live: a
# first attempt hit every container, tabbyapi included, going from
# stopped to "Started"), so this waits on tabbyapi's own load
# confirmation too, not just langgraph-agent's health endpoint — a first
# run without this wait hit campaign_preflight's LLM-readiness check
# timing out at 180s while tabbyapi was still loading.
#
# Usage: bash scripts/smoke-adaptive-thinking.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

LABEL="phase1-adaptive-thinking-smoke"

echo "=== Rebuilding langgraph-agent (code changed: app/graph.py) ==="
docker compose build langgraph-agent

echo
echo "=== Recreating with ADAPTIVE_THINKING=true (this run only) ==="
ADAPTIVE_THINKING=true docker compose up -d --force-recreate langgraph-agent

restore() {
  echo
  echo "=== Restoring langgraph-agent to its default (ADAPTIVE_THINKING=false) ==="
  docker compose up -d --force-recreate langgraph-agent
}
trap restore EXIT

echo "Waiting for tabbyapi to confirm a model load (may have been brought up as a side effect)..."
waited=0
until docker compose logs tabbyapi 2>/dev/null | tail -80 | grep -qE "Model loaded in|Serving OAI API on"; do
  if docker compose logs tabbyapi 2>/dev/null | tail -80 | grep -qE "Traceback|CUDA out of memory|CUDA error|fatal error"; then
    echo "tabbyapi failed to load — see the log tail below." >&2
    docker compose logs tabbyapi | tail -60 >&2
    exit 1
  fi
  if (( waited >= 180 )); then
    echo "tabbyapi hasn't confirmed a load after 180s — see docker compose logs tabbyapi" >&2
    exit 1
  fi
  sleep 5
  waited=$((waited + 5))
done
echo "tabbyapi ready."

echo "Waiting for langgraph-agent to be ready..."
waited=0
until docker exec langgraph-agent python3 -c \
  "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" \
  &>/dev/null; do
  if (( waited >= 60 )); then
    echo "langgraph-agent didn't come up after 60s — see docker compose logs langgraph-agent" >&2
    exit 1
  fi
  sleep 3
  waited=$((waited + 3))
done

EFFECTIVE="$(docker exec langgraph-agent env | grep '^ADAPTIVE_THINKING=' || true)"
echo "Effective env: ${EFFECTIVE:-ABSENT}"
if [[ "$EFFECTIVE" != "ADAPTIVE_THINKING=true" ]]; then
  echo "ADAPTIVE_THINKING did not apply as expected — aborting before running anything." >&2
  exit 1
fi

echo
echo "=== Smoke: A2_schema_references, n=1, flag override declared ==="
CAMPAIGN_EXPECTED_FLAGS_OVERRIDE='{"ADAPTIVE_THINKING":"true"}' \
  scripts/run-campaign.sh --suite v2 --tasks A2 --reps 1 --label "$LABEL"

CAMPAIGN_JSON="$(ls -t docs/campaigns/campaign-*"${LABEL}".json | head -1)"
echo
echo "=== Reading back $CAMPAIGN_JSON ==="
THREAD_ID="$(python3 -c "
import json
d = json.load(open('$CAMPAIGN_JSON'))
print(d['runs'][0]['thread_id'])
")"
echo "thread_id: $THREAD_ID"

echo
echo "=== Audit log analysis (raw transcript, not the campaign score) ==="
python3 "$SCRIPT_DIR/read-adaptive-thinking-audit.py" "$THREAD_ID"
