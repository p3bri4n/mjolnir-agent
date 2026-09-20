#!/usr/bin/env bash
# Phase 0 of docs/briefs/reasoning-effort-tuning.md: settle, empirically
# against the ACTUAL production TabbyAPI/Qwen3.8, how `reasoning_effort`
# is actually wired on this backend before touching call_llm (app/graph.py).
#
# Two candidate wire formats, both tried:
#   1. bare extra_body key: {"reasoning_effort": "medium"}
#      — matches how our own working enable_thinking mechanism is wired
#      (_should_suppress_thinking, TabbyAPI's own convention, confirmed
#      empirically, docs/engineering-log.md "ADAPTIVE_THINKING mechanism
#      confirmed on Qwen3.8").
#   2. top-level chat.completions.create() kwarg: {"reasoning_effort": "medium"}
#      as a sibling of "messages"/"model" — what Qwen's own model card
#      example shows for the vLLM-style OpenAI SDK path.
# Do not assume either without checking — same discipline as the
# enable_thinking wire-format question this project already settled once.
#
# Also confirms medium/low measurably reduce reasoning_content length vs.
# the default (xhigh) on a fixed prompt with enough real reasoning to
# show a difference (unlike a trivial "2+2", used for the enable_thinking
# probe where only presence/absence mattered, not degree).
#
# Bypasses langgraph-agent's graph entirely (same technique as
# smoke-adaptive-thinking.sh's Step 0): docker exec into langgraph-agent
# (already on agent-net, has python3) relaying a raw call to TabbyAPI
# (tabbyapi:5000, no host port published). No production model change —
# same tabbyapi container, config, and Qwen3.8 weights already serving.
#
# Usage: bash scripts/probe-qwen38-reasoning-effort.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Same real-completion-call technique as campaign_preflight.py's
# wait_for_llm_ready/_fetch_llm_ready — see docs/resolved-bugs.md #57 for
# why a docker-logs tail scan is NOT used here.
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

docker compose up -d langgraph-agent
wait_for_tabbyapi

docker exec langgraph-agent python3 -c "
import json
import urllib.request

URL = 'http://tabbyapi:5000/v1/chat/completions'
PROMPT = (
    'A train leaves station A at 60 km/h. Another train leaves station B, '
    '300 km away, at 40 km/h, heading toward the first train. How many '
    'minutes until they meet? Give only the final number of minutes.'
)


def call(label, payload_extra, top_level_extra=None):
    payload = {
        'model': 'agent-llm',
        'messages': [{'role': 'user', 'content': PROMPT}],
        'max_tokens': 2000,
    }
    payload.update(payload_extra)
    if top_level_extra:
        payload.update(top_level_extra)
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    print(f'=== {label} ===')
    print('payload keys beyond model/messages/max_tokens:', {k: v for k, v in payload.items() if k not in ('model', 'messages', 'max_tokens')})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f'HTTP {e.code}: {e.read().decode()[:300]}')
        print()
        return
    msg = body['choices'][0]['message']
    reasoning = msg.get('reasoning_content')
    content = msg.get('content') or ''
    print('reasoning_content:', 'ABSENT' if not reasoning else f'{len(reasoning)} chars')
    print('content:', repr(content[:150]))
    print()


call('1. default (no reasoning_effort — expect xhigh, longest reasoning)', {})
call('2. reasoning_effort:medium as a BARE extra_body key', {'reasoning_effort': 'medium'})
call('3. reasoning_effort:medium as a TOP-LEVEL kwarg (model-card style)', {}, top_level_extra={'reasoning_effort': 'medium'})
call('4. reasoning_effort:low as a BARE extra_body key', {'reasoning_effort': 'low'})
call('5. reasoning_effort:not-a-real-level (expect an error per the template raise_exception)', {'reasoning_effort': 'not-a-real-level'})
"

echo "=== Phase 0 done. Read the reasoning_content lengths above:"
echo "- If variant 2 (bare extra_body) shows a shorter reasoning_content than"
echo "  variant 1 (default), and variant 3 (top-level kwarg) does NOT, TabbyAPI"
echo "  follows the same bare-extra_body convention as enable_thinking — wire"
echo "  call_llm the same way (Phase 1 of the brief)."
echo "- If it's the reverse, use the top-level kwarg instead."
echo "- If NEITHER changes the length vs. default, reasoning_effort is not"
echo "  honored on this backend/runtime at all — stop, do not proceed to"
echo "  Phase 1 on an unverified premise, report back."
echo "- Variant 5 should error (HTTP 4xx/5xx) per the template's own"
echo "  raise_exception on an unsupported reasoning_effort value — confirms"
echo "  the parameter is actually reaching the chat template, not silently"
echo "  swallowed somewhere upstream."
