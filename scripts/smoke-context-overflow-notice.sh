#!/usr/bin/env bash
# Live smoke for docs/resolved-bugs.md #59 (app/main.py's
# _error_notice_for): a genuine TabbyAPI context-window overflow used to
# collapse into the same generic _INTERNAL_ERROR_NOTICE/failure_cause=
# "infra" as any other error. Deterministic — sends ONE deliberately
# oversized user message directly to langgraph-agent's own
# /v1/chat/completions (bypassing task execution entirely, no tool_calls/
# approval needed) to force openai.BadRequestError/
# context_length_exceeded on the very first LLM call, then checks the
# response carries the NEW specific notice, not the generic one, and
# that the container logs still show the real underlying exception.
#
# Requires the fix already deployed:
#   docker compose build langgraph-agent
#   docker compose up -d --force-recreate langgraph-agent
set -euo pipefail

echo "--- image (confirm fresh build, not a stale --force-recreate) ---"
docker inspect langgraph-agent --format 'Image: {{.Image}}  Started: {{.State.StartedAt}}'

echo "--- health check ---"
docker exec langgraph-agent python3 -c \
  "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)"
echo "OK"

echo "--- sending oversized request (random content, forces context_length_exceeded) ---"
# A repeated single character (tried first) compresses way too well under
# BPE merges — 200k 'A' chars turned out to tokenize under the 32768
# ceiling instead of over it. Random bytes have no repeats for the
# tokenizer to merge away, so token count tracks length reliably.
RESPONSE="$(docker exec langgraph-agent python3 -c "
import base64, json, os, urllib.request

content = base64.b64encode(os.urandom(100000)).decode()
payload = json.dumps({
    'model': 'agent-llm',
    'messages': [{'role': 'user', 'content': content}],
    'stream': False,
})
req = urllib.request.Request(
    'http://localhost:8000/v1/chat/completions',
    data=payload.encode(),
    headers={'Content-Type': 'application/json'},
    method='POST',
)
with urllib.request.urlopen(req, timeout=60) as resp:
    print(resp.read().decode())
")"

echo "$RESPONSE"

if echo "$RESPONSE" | grep -q "Contexte de conversation trop long"; then
  echo "OK: specific context-overflow notice returned (_CONTEXT_OVERFLOW_NOTICE)."
elif echo "$RESPONSE" | grep -q "Erreur interne pendant la génération"; then
  echo "FAIL: generic notice returned instead of the specific one — fix not active (stale image?)." >&2
  exit 1
else
  echo "FAIL: unexpected response, neither notice matched." >&2
  exit 1
fi

echo "--- confirming the real exception still reaches the container logs ---"
docker compose logs langgraph-agent 2>&1 | grep -B2 -A3 "context_length_exceeded" | tail -12
