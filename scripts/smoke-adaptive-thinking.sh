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
# Only rebuilds/recreates langgraph-agent — tabbyapi/mcp-client untouched.
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
curl -s "http://localhost:8000/audit?thread_id=${THREAD_ID}" | python3 -c "
import json, sys

data = json.load(sys.stdin)
entries = data['entries']

thinking_flags = [e for e in entries if e.get('kind') == 'message' and e.get('role') == 'adaptive_thinking']
assistant_msgs = [e for e in entries if e.get('kind') == 'message' and e.get('role') == 'assistant']

print(f'{len(thinking_flags)} adaptive_thinking entries, {len(assistant_msgs)} assistant turns')
print()

hard_fail = False
for i, (flag, msg) in enumerate(zip(thinking_flags, assistant_msgs)):
    suppressed = flag['content']['suppressed']
    content = msg['content'].get('content') or ''
    has_think = '<think>' in content
    if suppressed and has_think:
        verdict = 'FAIL (suppressed but still reasoned — mechanism not working)'
        hard_fail = True
    elif not suppressed and not has_think:
        verdict = 'WARN (not suppressed, but no <think> anyway — model chose not to reason, not a bug)'
    else:
        verdict = 'OK'
    print(f'turn {i+1}: suppressed={suppressed!s:5} has_think={has_think!s:5} [{verdict}]')

print()
if not any(f['content']['suppressed'] for f in thinking_flags):
    print('INCONCLUSIVE: suppressed never fired once in this run — not a real test.')
    sys.exit(1)
if hard_fail:
    print('FAIL: at least one suppressed turn still produced a <think> block.')
    sys.exit(1)
print('PASS: every suppressed turn produced no <think> block.')
"
