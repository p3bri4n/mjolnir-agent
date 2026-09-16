#!/usr/bin/env bash
# Phase 2's remaining measurements, docs/briefs/qwen3.8-27b-evaluation.md:
# raw prefill/decode throughput, with vs. without MTP, single variable
# (same fixed prompt both times, only draft_model toggled). Also dumps
# the raw per-request log lines around the MTP-on run so a real
# acceptance-rate line format can be identified — this project's own
# tooling (campaign_persistence.py's _TABBY_METRICS_RE) has never parsed
# one, only the tokens/prefill line, so this is exploratory rather than
# assuming a format.
#
# Reuses the exact log-parsing regex from
# services/langgraph-agent/tests_integration/campaign_persistence.py
# (`_TABBY_METRICS_RE`) — copied, not imported, to avoid a path/dependency
# dance for a one-off probe; keep it byte-identical if that regex ever
# changes.
#
# thinking disabled for this measurement (enable_thinking: false) so the
# generated tokens are answer content, not variable-length reasoning —
# a cleaner, more comparable decode-speed reading.
#
# Assumes poc/qwen3.8/config.yml is currently staged with vision + MTP +
# tool_format (left in that state by probe-qwen38-staged-vram.sh).
# Restores MTP at the end either way.
#
# Usage: bash scripts/probe-qwen38-throughput-mtp.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
POC_DIR="$PROJECT_DIR/poc/qwen3.8"
CONFIG_PATH="$POC_DIR/config.yml"

if [ ! -f "$CONFIG_PATH" ]; then
  echo "Expected $CONFIG_PATH — not found." >&2
  exit 1
fi
cd "$POC_DIR"

if docker ps --filter "name=^tabbyapi$" --format '{{.Names}}' | grep -qx tabbyapi; then
  echo "WARNING: production tabbyapi is running — VRAM contention risk, this PoC reserves all GPUs too." >&2
  read -rp "Continue anyway? [y/N] " confirm
  [[ "${confirm:-N}" =~ ^[Yy]$ ]] || { echo "Aborted." >&2; exit 1; }
fi

wait_for_load_or_fail() {
  local waited=0 timeout=180 interval=5
  until docker compose logs tabbyapi 2>/dev/null | tail -100 | grep -qE "Model loaded in|Serving OAI API on"; do
    if docker compose logs tabbyapi 2>/dev/null | tail -100 | grep -qE "Traceback|CUDA out of memory|CUDA error|fatal error|Insufficient VRAM"; then
      echo "Load failed — log tail:" >&2
      docker compose logs tabbyapi | tail -80 >&2
      return 1
    fi
    if (( waited >= timeout )); then
      echo "Timeout waiting for load — log tail:" >&2
      docker compose logs tabbyapi | tail -80 >&2
      return 1
    fi
    sleep "$interval"
    waited=$((waited + interval))
  done
  return 0
}

has_mtp() { grep -q "^draft_model:" "$CONFIG_PATH"; }

remove_mtp() {
  # Deletes the draft_model: block (that key and everything indented
  # under it) — sed range from the literal line to the next
  # unindented-or-EOF line.
  sed -i '/^draft_model:/,/^[^ ]/{/^draft_model:/d; /^  /d}' "$CONFIG_PATH"
}

add_mtp() {
  if ! has_mtp; then
    cat >> "$CONFIG_PATH" <<'YAML'

draft_model:
  draft_mode: mtp
YAML
  fi
}

restore_mtp_on_exit() {
  echo
  echo "=== Restoring MTP in config.yml (leaving it in the staged state) ==="
  add_mtp
}
trap restore_mtp_on_exit EXIT

FIXED_PROMPT='Write a detailed, 150-word explanation of how photosynthesis works, covering light-dependent and light-independent reactions.'

send_and_measure() {
  local label="$1"
  local before after
  before="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  PROMPT_ENV="$FIXED_PROMPT" python3 -c "
import json, os, urllib.request
payload = {
    'model': 'qwen38',
    'messages': [{'role': 'user', 'content': os.environ['PROMPT_ENV']}],
    'max_tokens': 300,
    'temperature': 0,
    'enable_thinking': False,
}
req = urllib.request.Request(
    'http://localhost:5001/v1/chat/completions',
    data=json.dumps(payload).encode(),
    headers={'Content-Type': 'application/json'},
    method='POST',
)
with urllib.request.urlopen(req, timeout=60) as resp:
    body = json.loads(resp.read().decode())
usage = body.get('usage', {})
print(f\"completion_tokens (usage): {usage.get('completion_tokens')}, prompt_tokens: {usage.get('prompt_tokens')}\")
" || { echo "Request failed for $label" >&2; return 1; }

  sleep 1  # let the log line flush
  after="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  echo
  echo "--- Raw tabbyapi log window ($label) ---"
  docker compose logs --since "$before" --until "$after" tabbyapi 2>/dev/null | grep -v "^$" || true

  echo
  echo "--- Parsed metrics ($label) ---"
  docker compose logs --since "$before" --until "$after" tabbyapi 2>/dev/null | python3 -c "
import re, sys

text = re.sub(r'\s+', ' ', sys.stdin.read())
pattern = re.compile(
    r'(\d+) tokens generated in ([\d.]+) seconds \(Queue: ([\d.]+) s, Process: (\d+) cached tokens '
    r'and (\d+) new tokens at ([\d.]+) T/s'
)
matches = list(pattern.finditer(text))
if not matches:
    print('No matching tabbyapi metrics line found in this window.')
    sys.exit(1)
m = matches[-1]
tokens_generated, generation_seconds, queue_seconds, cached, new, prefill_tps = (
    int(m.group(1)), float(m.group(2)), float(m.group(3)), int(m.group(4)), int(m.group(5)), float(m.group(6))
)
decode_tps = tokens_generated / generation_seconds if generation_seconds > 0 else 0
print(f'tokens_generated={tokens_generated} generation_seconds={generation_seconds}')
print(f'decode_tps={decode_tps:.1f} T/s')
print(f'prefill_tps={prefill_tps:.1f} T/s (cached={cached} new={new})')
"
}

echo "=== Ensuring the PoC is up with the current staged config ==="
docker compose up -d --force-recreate tabbyapi
wait_for_load_or_fail || exit 1

echo
echo "=========================================="
echo "=== RUN 1: WITH MTP (current config) ==="
echo "=========================================="
send_and_measure "with-mtp"

echo
echo "=== Removing MTP, reloading with the SAME prompt ==="
remove_mtp
docker compose up -d --force-recreate tabbyapi
wait_for_load_or_fail || exit 1

echo
echo "=========================================="
echo "=== RUN 2: WITHOUT MTP ==="
echo "=========================================="
send_and_measure "without-mtp"

echo
echo "=== Done. Compare decode_tps/prefill_tps between the two runs above. ==="
echo "Record the read in docs/engineering-log.md / docs/architecture/inference-backend.md."
echo "Container left running — tear down when finished: cd $POC_DIR && docker compose down"
