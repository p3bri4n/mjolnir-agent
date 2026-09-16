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
usage = body.get('usage') or {}
print(f\"completion_tokens (usage): {usage.get('completion_tokens')}, prompt_tokens: {usage.get('prompt_tokens')}\")
" || { echo "Request failed for $label" >&2; return 1; }

  sleep 1  # let the log line flush
  after="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  echo
  echo "--- Raw tabbyapi log window ($label) ---"
  docker compose logs --since "$before" --until "$after" tabbyapi 2>/dev/null | grep -v "^$" || true

  echo
  echo "--- Parsed metrics ($label) ---"
  docker compose logs --no-log-prefix --since "$before" --until "$after" tabbyapi 2>/dev/null | python3 -c "
import re, sys

# Real log format on exllamav3 1.5.0/tabbyAPI (found live — this is NOT
# the format tests_integration/campaign_persistence.py's _TABBY_METRICS_RE
# expects; that regex is now stale against production's own upgraded
# image, see docs/resolved-bugs.md and the engineering log entry this
# probe fed into):
# '#1 chat/completions: 234 tokens generated at 16.9 T/s . prompt 36
#  tokens, none cached, 36 new in 6.73 s . first token 6.73 s, total
#  20.6 s . draft 163/284 accepted (57%)'
# The trailing 'draft N/M accepted (P%)' clause is only present when MTP
# is active — its absence IS the without-MTP signal, no separate flag
# needed.
text = re.sub(r'\s+', ' ', sys.stdin.read())
pattern = re.compile(
    r'(\d+) tokens generated at ([\d.]+) T/s . prompt (\d+) tokens, '
    r'(?:(\d+) cached|none cached), (\d+) new in ([\d.]+) s . '
    r'first token ([\d.]+) s, total ([\d.]+) s'
    r'(?: . draft (\d+)/(\d+) accepted \((\d+)%\))?'
)
matches = list(pattern.finditer(text))
if not matches:
    print('No matching tabbyapi metrics line found in this window.')
    sys.exit(1)
m = matches[-1]
tokens_generated = int(m.group(1))
decode_tps = float(m.group(2))
prompt_tokens = int(m.group(3))
cached = int(m.group(4)) if m.group(4) else 0
new = int(m.group(5))
prefill_seconds = float(m.group(6))
first_token_s = float(m.group(7))
total_s = float(m.group(8))
prefill_tps = new / prefill_seconds if prefill_seconds > 0 else 0

print(f'tokens_generated={tokens_generated} decode_tps={decode_tps:.1f} T/s')
print(f'prompt_tokens={prompt_tokens} cached={cached} new={new} prefill_seconds={prefill_seconds}')
print(f'prefill_tps={prefill_tps:.1f} T/s (computed: new/prefill_seconds)')
print(f'first_token_s={first_token_s} total_s={total_s}')
if m.group(9):
    accepted, drafted, pct = int(m.group(9)), int(m.group(10)), int(m.group(11))
    print(f'MTP acceptance: {accepted}/{drafted} accepted ({pct}%)')
else:
    print('MTP acceptance: n/a (no draft clause in this log line — MTP inactive this run)')
"
}

echo "=== Ensuring the PoC is up with the current staged config (MTP on) ==="
add_mtp
docker compose up -d --force-recreate tabbyapi
wait_for_load_or_fail || exit 1

echo
echo "=========================================="
echo "=== WITH MTP: request 1/2 (cold — first call after load, JIT-contaminated) ==="
echo "=========================================="
send_and_measure "with-mtp-cold"

echo
echo "=========================================="
echo "=== WITH MTP: request 2/2 (warm — same container, no reload) ==="
echo "=========================================="
send_and_measure "with-mtp-warm"

echo
echo "=== Removing MTP, reloading with the SAME prompt ==="
remove_mtp
docker compose up -d --force-recreate tabbyapi
wait_for_load_or_fail || exit 1

echo
echo "=========================================="
echo "=== WITHOUT MTP: request 1/2 (cold) ==="
echo "=========================================="
send_and_measure "without-mtp-cold"

echo
echo "=========================================="
echo "=== WITHOUT MTP: request 2/2 (warm) ==="
echo "=========================================="
send_and_measure "without-mtp-warm"

echo
echo "=== Done. The *-warm readings are the fair with/without MTP comparison — ==="
echo "the *-cold ones quantify the first-call JIT overhead itself, a separate finding."
echo "Record the read in docs/engineering-log.md / docs/architecture/inference-backend.md."
echo "Container left running — tear down when finished: cd $POC_DIR && docker compose down"
