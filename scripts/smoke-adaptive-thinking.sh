#!/usr/bin/env bash
# Originally Phase 1's own judge, docs/briefs/archives/qwen3.8-27b-evaluation.md:
# confirm, via the raw audit log transcript (not the campaign report),
# that the _should_suppress_thinking migration (app/graph.py) genuinely
# suppresses reasoning on the turns it fires on, and only those —
# mechanistic proof, not a success/failure score. Re-run here as a
# pre-campaign smoke on the model now actually in production
# (Qwen3.8-27B) — the original Phase 1 live smoke ran before the
# production switch, on Qwen3.6, never re-verified on Qwen3.8 itself
# until now.
#
# IMPORTANT: Qwen3.8's chat_template.jinja bakes the opening <think> tag
# into the generation PROMPT (both when thinking is on and off), never
# into what the model generates — see the comment in
# read-adaptive-thinking-audit.py this script calls. Only the closing
# </think> can appear in a real reasoning turn's completion. This does
# NOT weaken the suppression mechanism itself (the template pre-closes
# an EMPTY think block when enable_thinking is false, so no reasoning can
# be generated structurally either way) — it only means the audit
# reader's tag check had to change to stay reliable on this model.
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

LABEL="qwen38-adaptive-thinking-smoke"

# Real completion call (docker exec, not a log-tail scan) — same
# technique and same rationale as campaign_preflight.py's
# wait_for_llm_ready/_fetch_llm_ready: "the only check that would have
# caught the real-conditions case found (server not yet listening despite
# a model already loaded)". A log-grep for "Model loaded in" is fragile
# whenever tabbyapi was NOT just recreated — its original load-
# confirmation line can scroll out of `docker compose logs | tail -N`
# after enough later traffic, causing a false timeout against a tabbyapi
# that has been ready and serving the whole time (hit live, 2026-09-17:
# 180s timeout on an already-running, already-healthy tabbyapi).
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

echo "=== Step 0: raw reasoning-split probe, bypassing langgraph-agent entirely ==="
echo "Settles whether TabbyAPI's start_in_reasoning: auto (our config never"
echo "overrides it — services/tabbyapi/config.yml, config.local.yml both silent"
echo "on this key) actually detects Qwen3.8's chat_template.jinja convention:"
echo "the opening <think> is baked into the PROMPT, not generated — auto is"
echo "documented to scan for exactly that (an unclosed reasoning start token at"
echo "the end of the templated prompt), but the Phase 3 campaign data shows"
echo "</think> in final answers with no matching <think> ever added by our own"
echo "reinjection code (app/graph.py, _convert_delta_with_reasoning) — which"
echo "would only happen if reasoning_content came back genuinely empty."
echo
echo "No production model change here — same tabbyapi container, config,"
echo "and Qwen3.8 weights already serving. Only ensures langgraph-agent is up"
echo "(started if needed, not rebuilt) so docker exec has a container on"
echo "agent-net to relay the direct call from (tabbyapi publishes no host port)."
docker compose up -d langgraph-agent
wait_for_tabbyapi

docker exec langgraph-agent python3 -c "
import json
import urllib.request

URL = 'http://tabbyapi:5000/v1/chat/completions'
PROMPT = 'What is 2+2? Answer in one word, no explanation.'


def call(label, extra):
    payload = {
        'model': 'agent-llm',
        'messages': [{'role': 'user', 'content': PROMPT}],
        'max_tokens': 300,
    }
    payload.update(extra)
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    print(f'=== {label} ===')
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f'HTTP {e.code}: {e.read().decode()[:300]}')
        return
    msg = body['choices'][0]['message']
    reasoning = msg.get('reasoning_content')
    content = msg.get('content') or ''
    print('reasoning_content:', 'ABSENT' if not reasoning else f'{len(reasoning)} chars')
    print('content has <think>:', '<think>' in content, '| </think>:', '</think>' in content)
    print('content (first 200 chars):', repr(content[:200]))
    print()


call('1. default (thinking on, no kwarg — expect reasoning_content populated if start_in_reasoning:auto works)', {})
call('2. enable_thinking: false (expect no reasoning at all, template pre-closes the block)', {'enable_thinking': False})
"

echo "=== Step 0 done. If variant 1 shows reasoning_content: ABSENT, that's the"
echo "root cause confirmed (start_in_reasoning: auto not detecting this"
echo "template's convention) — fix candidate: start_in_reasoning: always in"
echo "services/tabbyapi/config.local.yml, docker compose up -d --force-recreate"
echo "tabbyapi, re-run this probe alone before touching the mechanistic smoke"
echo "below. If variant 1 already shows reasoning_content populated, the"
echo "earlier campaign-data reading needs another explanation — stop and"
echo "report back rather than proceeding into Step 1 on a wrong premise."
echo

echo "=== Step 1: mechanistic smoke (ADAPTIVE_THINKING=true, one campaign run) ==="
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
