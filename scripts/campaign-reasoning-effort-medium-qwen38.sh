#!/usr/bin/env bash
# Phase 2 of docs/briefs/reasoning-effort-tuning.md: full v2 campaign
# with REASONING_EFFORT=medium on Qwen3.8, ADAPTIVE_THINKING left at its
# "false" default (matching Phase 3's baseline flag state) — single
# variable. Compared against the Phase 3 Qwen3.8 result already in hand
# (campaign-20260916T173945Z-qwen38-eval-phase3-qwen38.json, reasoning
# at the model's own xhigh default) and against the
# ADAPTIVE_THINKING=true campaign (campaign-20260917T062658Z-
# qwen38-adaptive-thinking-campaign.json, full suppression) — three
# points on the same axis: xhigh (no override) / medium / full off.
#
# Prerequisite: Phase 0 (wire format confirmed empirically,
# scripts/probe-qwen38-reasoning-effort.sh) and Phase 1 (REASONING_EFFORT
# threaded into call_llm, app/graph.py) both done — full suite verified
# 498 passed, 0 failed, including the merge-safety regression test
# (REASONING_EFFORT and ADAPTIVE_THINKING coexisting in one extra_body)
# and the CAMPAIGN_ENV_FLAGS/EXPECTED_AGENT_FLAGS tracking fix (same
# class of gap as the historical PLANNING_MODE omission,
# docs/resolved-bugs.md).
#
# Primary judges (docs/briefs/reasoning-effort-tuning.md, Phase 2):
# T10_books_toscrape and A1_reconciliation_croisee specifically — the two
# tasks that broke under full suppression (frozen 18-call navigate loop,
# unfinished wide search) — recovering these is the bar, not just an
# aggregate score. Also: zero NEW failure_cause=boucle anywhere else, and
# cumulative duration_seconds/prompt_tokens_total read against BOTH prior
# campaigns to see where medium actually lands.
#
# Does NOT change production by default: REASONING_EFFORT=medium only
# for this campaign's duration, restored to "" (empty, no override) on
# exit via the same trap pattern as the ADAPTIVE_THINKING campaign
# script.
#
# Same fixtures prerequisite as any v2 campaign — not handled here:
#   docker compose --profile test-fixtures up -d fixture-catalog \
#     fixture-docs fixture-hr-app fixture-admin fixture-perception
#
# Usage: bash scripts/campaign-reasoning-effort-medium-qwen38.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

LABEL="qwen38-reasoning-effort-medium-campaign"

# Real completion call (docker exec, not a log-tail scan) — see
# docs/resolved-bugs.md #57 for why a docker-logs tail scan is not used
# here (false-timed-out on an already-healthy, non-recreated tabbyapi).
wait_for_tabbyapi() {
  echo "Waiting for tabbyapi to answer a real completion..."
  local waited=0 status
  while true; do
    status="$(docker exec langgraph-agent python3 -c "
import json, urllib.request
req = urllib.request.Request(
    'http://tabbyapi:5000/v1/chat/completions',
    data=json.dumps({
        'model': 'agent-llm',
        'messages': [{'role': 'user', 'content': 'ping'}],
        'max_tokens': 1,
        'enable_thinking': False,
    }).encode(),
    headers={'Content-Type': 'application/json'},
)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        print(r.status)
except Exception as e:
    print('ERROR', repr(e))
" 2>/dev/null)"
    if [[ "$status" == "200" ]]; then
      echo "tabbyapi ready."
      return 0
    fi
    if (( waited >= 180 )); then
      echo "tabbyapi hasn't answered a real completion after 180s (last: ${status:-no response}) — see docker compose logs tabbyapi" >&2
      exit 1
    fi
    sleep 5
    waited=$((waited + 5))
  done
}

echo "=== Rebuilding langgraph-agent (code changed: app/graph.py, REASONING_EFFORT) ==="
docker compose build langgraph-agent

echo
echo "=== Recreating with REASONING_EFFORT=medium (this campaign only) ==="
REASONING_EFFORT=medium docker compose up -d --force-recreate langgraph-agent

restore() {
  echo
  echo "=== Restoring langgraph-agent to its default (REASONING_EFFORT unset) ==="
  docker compose up -d --force-recreate langgraph-agent
}
trap restore EXIT

wait_for_tabbyapi  # may have been brought up as a side effect of the recreate above

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

EFFECTIVE="$(docker exec langgraph-agent env | grep '^REASONING_EFFORT=' || true)"
echo "Effective env: ${EFFECTIVE:-ABSENT}"
if [[ "$EFFECTIVE" != "REASONING_EFFORT=medium" ]]; then
  echo "REASONING_EFFORT did not apply as expected — aborting before running anything." >&2
  exit 1
fi

echo
echo "=== Full v2 campaign, REASONING_EFFORT=medium, ADAPTIVE_THINKING=false ==="
echo "=== Compare against Phase 3 (xhigh): campaign-20260916T173945Z-qwen38-eval-phase3-qwen38.json"
echo "=== and against full suppression: campaign-20260917T062658Z-qwen38-adaptive-thinking-campaign.json"
CAMPAIGN_EXPECTED_FLAGS_OVERRIDE='{"REASONING_EFFORT":"medium"}' \
  scripts/run-campaign.sh --suite v2 --label "$LABEL"

echo
echo "=== Done. Check first: T10_books_toscrape and A1_reconciliation_croisee"
echo "(both failed with failure_cause=boucle under ADAPTIVE_THINKING=true) —"
echo "did medium recover them? Then compare CuP/family scores, cumulative"
echo "duration_seconds, and prompt_tokens_total against both prior campaigns."
