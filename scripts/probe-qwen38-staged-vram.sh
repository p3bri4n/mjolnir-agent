#!/usr/bin/env bash
# Phase 2 of docs/briefs/qwen3.8-27b-evaluation.md: VRAM footprint,
# staged one feature at a time (vision -> +MTP -> +tool_format) rather
# than all at once. Production's own VRAM margin is documented as
# already thin with the current, LIGHTER 3.50bpw build
# (services/tabbyapi/config.yml: "~822 Mio de marge seulement... avec
# vision activée"); this build is 4.5bpw — heavier, not lighter. A
# single big-bang config would just produce an opaque OOM with no way
# to tell which feature pushed the limit; this measures each addition's
# own delta.
#
# Uses gpu_split_auto: true for THIS discovery run only — production
# uses an explicit split because autosplit was found unstable for
# reproducible placement (docs/briefs/archive/deterministic-gpu-
# placement.md), but that split was itself originally derived from an
# autosplit observation. Same method, not a regression.
#
# Stage 3 also runs a scripted tool-calling sanity check with a
# JSON-string-valued argument — flagged externally as a crash point on
# official Qwen3.8 templates, and directly on this project's tool-calling
# critical path.
#
# Stops at the first stage that fails, with the log tail — that is the
# point of staging. Leaves the container running at the end either way,
# for manual follow-up; tear down yourself: cd poc/qwen3.8 && docker compose down
#
# Usage: bash scripts/probe-qwen38-staged-vram.sh

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

if ! git -C "$PROJECT_DIR" diff --quiet -- "$CONFIG_PATH"; then
  echo "Uncommitted changes in $CONFIG_PATH — commit or stash before running this script" >&2
  echo "(it edits this file in place, stage by stage)." >&2
  exit 1
fi

if docker ps --filter "name=^tabbyapi$" --format '{{.Names}}' | grep -qx tabbyapi; then
  cat >&2 <<'EOF'
WARNING: the production `tabbyapi` container is currently running.
This PoC reserves ALL GPUs, same as production — contention risk, and
it will also confound the VRAM readings below (they include whatever
production already holds).
EOF
  read -rp "Continue anyway? [y/N] " confirm
  if [[ "${confirm:-N}" != "y" && "${confirm:-N}" != "Y" ]]; then
    echo "Aborted." >&2
    exit 1
  fi
fi

vram_snapshot() {
  nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits
}

wait_for_load_or_fail() {
  local waited=0 timeout=180 interval=5
  until docker compose logs tabbyapi 2>/dev/null | tail -100 | grep -qE "Model loaded in|Serving OAI API on"; do
    if docker compose logs tabbyapi 2>/dev/null | tail -100 | grep -qE "Traceback|CUDA out of memory|CUDA error|fatal error|Insufficient VRAM"; then
      echo "LOAD FAILED at this stage — log tail:" >&2
      docker compose logs tabbyapi | tail -80 >&2
      return 1
    fi
    if (( waited >= timeout )); then
      echo "TIMEOUT waiting for a load outcome at this stage — log tail:" >&2
      docker compose logs tabbyapi | tail -80 >&2
      return 1
    fi
    sleep "$interval"
    waited=$((waited + interval))
  done
  return 0
}

run_stage() {
  local stage_name="$1"
  echo
  echo "=== Stage: $stage_name ==="
  grep -n "vision:\|draft_mode:\|tool_format:" "$CONFIG_PATH" || true

  local before after
  before="$(vram_snapshot)"

  docker compose up -d --force-recreate tabbyapi
  if ! wait_for_load_or_fail; then
    echo "Stopping here — see docs/briefs/qwen3.8-27b-evaluation.md, Phase 2." >&2
    exit 1
  fi

  after="$(vram_snapshot)"
  echo "$stage_name loaded cleanly."
  echo "VRAM before:"
  echo "$before" | sed 's/^/  /'
  echo "VRAM after:"
  echo "$after" | sed 's/^/  /'
}

echo "=== Stage 1/3: vision only ==="
run_stage "vision"

echo
echo "=== Adding draft_model: {draft_mode: mtp} (native MTP head) ==="
if ! grep -q "^draft_model:" "$CONFIG_PATH"; then
  cat >> "$CONFIG_PATH" <<'YAML'

draft_model:
  draft_mode: mtp
YAML
fi
run_stage "vision + MTP"

echo
echo "=== Adding tool_format: qwen3_coder ==="
if ! grep -q "^  tool_format:" "$CONFIG_PATH"; then
  sed -i '/^  vision: true/a\  tool_format: qwen3_coder' "$CONFIG_PATH"
fi
run_stage "vision + MTP + tool_format"

echo
echo "=== Tool-calling sanity check: JSON-string-valued argument ==="
python3 - <<'PYEOF'
import json
import urllib.request

payload = {
    "model": "qwen38",
    "messages": [
        {
            "role": "user",
            "content": (
                'Call the save_note tool with title="Test" and '
                'metadata=\'{"source": "probe", "count": 3}\' (a JSON string, verbatim).'
            ),
        }
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "save_note",
                "description": "Saves a note with a JSON-encoded metadata string.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "metadata": {
                            "type": "string",
                            "description": "A JSON-encoded string, e.g. '{\"source\": \"x\"}'.",
                        },
                    },
                    "required": ["title", "metadata"],
                },
            },
        }
    ],
    "max_tokens": 300,
}

req = urllib.request.Request(
    "http://localhost:5001/v1/chat/completions",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()[:500]}")
    raise SystemExit(1)

msg = body["choices"][0]["message"]
tool_calls = msg.get("tool_calls")
if not tool_calls:
    print(f"No tool_calls returned — content: {msg.get('content')!r}")
    raise SystemExit(1)

call = tool_calls[0]["function"]
print(f"tool_calls[0]: name={call['name']!r}")
print(f"  raw arguments string: {call['arguments']!r}")
try:
    parsed = json.loads(call["arguments"])
except json.JSONDecodeError as e:
    print(f"FAIL: arguments string itself isn't valid JSON: {e}")
    raise SystemExit(1)

print(f"  parsed: {parsed}")
metadata = parsed.get("metadata")
if isinstance(metadata, str):
    try:
        json.loads(metadata)
        print("PASS: metadata round-trips as a JSON-string value inside the arguments.")
    except json.JSONDecodeError:
        print(f"WARN: metadata present but not valid embedded JSON: {metadata!r}")
else:
    print(f"WARN: metadata isn't a string as requested (got {type(metadata).__name__}): {metadata!r}")
PYEOF

echo
echo "=== Done. Record the VRAM deltas above in docs/architecture/inference-backend.md ==="
echo "and docs/engineering-log.md. Container left running — tear down when finished:"
echo "  cd $POC_DIR && docker compose down"
