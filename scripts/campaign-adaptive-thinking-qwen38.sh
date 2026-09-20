#!/usr/bin/env bash
# Follow-up measurement flagged in docs/engineering-log.md, "Qwen3.8-27B
# evaluation, Phase 3 CLOSED": both the Qwen3.6 baseline and the Qwen3.8
# campaign that decided the model swap ran with ADAPTIVE_THINKING=false,
# so Qwen3.8's costlier default thinking effort (reasoning_effort: xhigh
# on every call) went uncontrolled — a measured +15% cumulative campaign
# time. This runs the identical v2 suite with ADAPTIVE_THINKING=true to
# see whether Phase 1's per-request thinking-control mechanism recovers
# that cost. Single variable: only the flag changes, same suite, same
# model, same image already confirmed in Phase 3.
#
# Prerequisite (mechanistic proof, not a success/failure score): the
# smoke in scripts/smoke-adaptive-thinking.sh — run and PASSED on
# 2026-09-17 (thread 8a0eba2250abe8fb), confirming the mechanism
# genuinely suppresses reasoning on Qwen3.8 and that a stray leading
# </think> on a suppressed turn is a harmless model habit (re-closing an
# already-closed empty think block), not a real failure. Do not skip
# straight to this script without that proof holding for the current
# image/model — re-run the smoke first if either changed since.
#
# Judges (docs/briefs/archives/qwen3.8-27b-evaluation.md, Phase 3):
# CuP/per-family score should stay flat (no quality loss from suppressing
# reasoning); cumulative time and token volume should move toward the
# Qwen3.6 baseline; trigger rate must be reported, not assumed (this
# script's own final step, scripts/aggregate-adaptive-thinking-audit.py —
# CLAUDE.md's trigger-rate coverage rule for conditional mechanisms).
#
# Same fixtures prerequisite as any v2 campaign — not handled here:
#   docker compose --profile test-fixtures up -d fixture-catalog \
#     fixture-docs fixture-hr-app fixture-admin fixture-perception
#
# Does NOT change production by default: ADAPTIVE_THINKING=true only for
# this campaign's duration, restored to "false" on exit (success or
# failure) via the same trap pattern as smoke-adaptive-thinking.sh.
#
# Usage: bash scripts/campaign-adaptive-thinking-qwen38.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

LABEL="qwen38-adaptive-thinking-campaign"

# Real completion call (docker exec, not a log-tail scan) — same
# technique and same rationale as campaign_preflight.py's
# wait_for_llm_ready/_fetch_llm_ready: "the only check that would have
# caught the real-conditions case found (server not yet listening despite
# a model already loaded)". A log-grep for "Model loaded in" is fragile
# whenever tabbyapi was NOT just recreated (this script only recreates
# langgraph-agent) — its original load-confirmation line can scroll out
# of `docker compose logs | tail -N` after enough later traffic, causing
# a false timeout against a tabbyapi that has been ready and serving the
# whole time (hit live, 2026-09-17: 180s timeout on an already-running,
# already-healthy tabbyapi).
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

echo "=== Recreating langgraph-agent with ADAPTIVE_THINKING=true (this campaign only) ==="
ADAPTIVE_THINKING=true docker compose up -d --force-recreate langgraph-agent

restore() {
  echo
  echo "=== Restoring langgraph-agent to its default (ADAPTIVE_THINKING=false) ==="
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

EFFECTIVE="$(docker exec langgraph-agent env | grep '^ADAPTIVE_THINKING=' || true)"
echo "Effective env: ${EFFECTIVE:-ABSENT}"
if [[ "$EFFECTIVE" != "ADAPTIVE_THINKING=true" ]]; then
  echo "ADAPTIVE_THINKING did not apply as expected — aborting before running anything." >&2
  exit 1
fi

echo
echo "=== Full v2 campaign, ADAPTIVE_THINKING=true, compared against the Phase 3 ==="
echo "=== Qwen3.8 result already in hand (campaign-20260916T173945Z-qwen38-eval-phase3-qwen38.json) ==="
CAMPAIGN_EXPECTED_FLAGS_OVERRIDE='{"ADAPTIVE_THINKING":"true"}' \
  scripts/run-campaign.sh --suite v2 --label "$LABEL"

CAMPAIGN_JSON="$(ls -t docs/campaigns/campaign-*"${LABEL}".json | head -1)"
echo
echo "=== Trigger-rate coverage: $CAMPAIGN_JSON ==="
python3 "$SCRIPT_DIR/aggregate-adaptive-thinking-audit.py" "$CAMPAIGN_JSON"

echo
echo "=== Done. Compare $CAMPAIGN_JSON against"
echo "docs/campaigns/campaign-20260916T173945Z-qwen38-eval-phase3-qwen38.json"
echo "(CuP/family scores, cumulative duration_seconds, prompt_tokens_total)"
echo "before deciding whether to enable ADAPTIVE_THINKING in production."
