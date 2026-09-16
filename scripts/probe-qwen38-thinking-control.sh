#!/usr/bin/env bash
# Phase 1, step 1 of docs/briefs/qwen3.8-27b-evaluation.md: settle,
# empirically and against the ACTUAL downloaded Qwen3.8 files, whether
# the formal per-request thinking-control kwargs work as needed before
# writing the _apply_adaptive_thinking migration (app/graph.py) around
# them. External reports disagreed (some say enable_thinking=false
# errors on the official 3.8 template, NVIDIA NIM docs say it works) —
# this settles it directly rather than trusting either.
#
# Bypasses langgraph-agent entirely (same technique as the resolved-bugs
# #entries that first diagnosed /no_think's lack of effect): raw HTTP
# calls to the isolated PoC (poc/qwen3.8/, port 5001), no tools involved,
# a trivial prompt whose answer is easy to eyeball. Reads back
# `reasoning_content` (present/absent, length) and the HTTP status for
# each variant:
#   1. default (no kwarg at all)              -> expect xhigh, long reasoning_content
#   2. enable_thinking: false                  -> the contested one
#   3. reasoning_effort: "medium"
#   4. reasoning_effort: "low"
#
# Does NOT touch production or langgraph-agent. Leaves the PoC running at
# the end (unlike the Phase 0 probes) so you can poke at it further by
# hand if a result is ambiguous — tear it down yourself when done:
#   cd poc/qwen3.8 && docker compose down
#
# Usage: bash scripts/probe-qwen38-thinking-control.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
POC_DIR="$PROJECT_DIR/poc/qwen3.8"
URL="http://localhost:5001/v1/chat/completions"

if [ ! -d "$POC_DIR" ]; then
  echo "Expected $POC_DIR — not found." >&2
  exit 1
fi
cd "$POC_DIR"

if docker ps --filter "name=^tabbyapi$" --format '{{.Names}}' | grep -qx tabbyapi; then
  cat >&2 <<'EOF'
WARNING: the production `tabbyapi` container is currently running.
This PoC reserves ALL GPUs, same as production — contention risk.
EOF
  read -rp "Continue anyway? [y/N] " confirm
  if [[ "${confirm:-N}" != "y" && "${confirm:-N}" != "Y" ]]; then
    echo "Aborted." >&2
    exit 1
  fi
fi

ALREADY_UP=false
if docker compose ps --status running --services 2>/dev/null | grep -qx tabbyapi; then
  ALREADY_UP=true
  echo "PoC already running, reusing it."
else
  echo "=== Starting the isolated PoC (qwen38-tabbyapi, port 5001) ==="
  docker compose up -d --force-recreate tabbyapi
  echo "Waiting for the model to load..."
  waited=0
  until docker compose logs tabbyapi 2>/dev/null | grep -qE "Model loaded in|Serving OAI API on"; do
    if docker compose logs tabbyapi 2>/dev/null | grep -qE "Traceback|CUDA out of memory|CUDA error|fatal error"; then
      echo "Load failed — see logs:" >&2
      docker compose logs tabbyapi | tail -60 >&2
      exit 1
    fi
    if (( waited >= 120 )); then
      echo "Timeout waiting for load — see logs:" >&2
      docker compose logs tabbyapi | tail -60 >&2
      exit 1
    fi
    sleep 5
    waited=$((waited + 5))
  done
  echo "Loaded."
fi

PROMPT='What is 2+2? Answer in one word, no explanation.'

build_payload() {
  # Reads PROMPT_ENV/EXTRA_JSON_ENV from the environment (never
  # interpolated into the Python source itself) to avoid any risk of
  # breaking out of the string — even though these are script-local
  # constants today, not attacker input.
  PROMPT_ENV="$PROMPT" EXTRA_JSON_ENV="$1" python3 -c "
import json, os
extra_raw = os.environ.get('EXTRA_JSON_ENV', '')
extra = json.loads(extra_raw) if extra_raw else {}
payload = {
    'model': 'qwen38',
    'messages': [{'role': 'user', 'content': os.environ['PROMPT_ENV']}],
    'max_tokens': 300,
}
payload.update(extra)
print(json.dumps(payload))
"
}

run_variant() {
  local label="$1" extra_json="$2"
  echo
  echo "=== $label ==="
  local body
  body="$(build_payload "$extra_json")"
  local response http_code
  response=$(curl -s -w '\n%{http_code}' -X POST "$URL" \
    -H 'Content-Type: application/json' \
    -d "$body")
  http_code=$(echo "$response" | tail -1)
  local json_body
  json_body=$(echo "$response" | sed '$d')
  echo "HTTP $http_code"
  if [[ "$http_code" != "200" ]]; then
    echo "$json_body" | python3 -m json.tool 2>/dev/null || echo "$json_body"
    return
  fi
  echo "$json_body" | python3 -c "
import json, sys
d = json.load(sys.stdin)
msg = d['choices'][0]['message']
reasoning = msg.get('reasoning_content')
content = msg.get('content')
print('reasoning_content:', 'ABSENT' if not reasoning else f'{len(reasoning)} chars')
print('content:', repr(content)[:200])
"
}

run_variant "1. default (no kwarg)" ""
run_variant "2. enable_thinking: false" '{"enable_thinking": false}'
run_variant "3. reasoning_effort: medium" '{"reasoning_effort": "medium"}'
run_variant "4. reasoning_effort: low" '{"reasoning_effort": "low"}'

echo
echo "=== Done. PoC left running (port 5001) — tear down yourself when finished: ==="
echo "  cd $POC_DIR && docker compose down"
