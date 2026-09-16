#!/usr/bin/env bash
# Follow-up to Phase 2's staged VRAM measurement
# (docs/briefs/qwen3.8-27b-evaluation.md): autosplit put ~91% of GPU 0's
# capacity in use (14 787/16 311 MiB, only ~1.5 GiB free) vs. ~45% on
# GPU 1 — too thin a margin, and autosplit itself is documented as
# unstable for reproducible placement (docs/briefs/archive/
# deterministic-gpu-placement.md, the exact reason production uses an
# explicit gpu_split instead of relying on it).
#
# Switches to an explicit gpu_split: [10, 13] (candidate computed for a
# ~10-15% / ~2 GiB free margin per card on the measured ~22.25 GiB total
# footprint) and reloads 3 times, checking BOTH that it fits and that the
# resulting VRAM usage is stable run to run — not just computed once and
# trusted.
#
# Usage: bash scripts/probe-qwen38-explicit-split-stability.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
POC_DIR="$PROJECT_DIR/poc/qwen3.8"
CONFIG_PATH="$POC_DIR/config.yml"
REPS=3

if [ ! -f "$CONFIG_PATH" ]; then
  echo "Expected $CONFIG_PATH — not found." >&2
  exit 1
fi
cd "$POC_DIR"

if ! git -C "$PROJECT_DIR" diff --quiet -- "$CONFIG_PATH"; then
  echo "Uncommitted changes in $CONFIG_PATH — commit or stash before running this script" >&2
  exit 1
fi

if docker ps --filter "name=^tabbyapi$" --format '{{.Names}}' | grep -qx tabbyapi; then
  echo "WARNING: production tabbyapi is running — VRAM contention risk." >&2
  read -rp "Continue anyway? [y/N] " confirm
  [[ "${confirm:-N}" =~ ^[Yy]$ ]] || { echo "Aborted." >&2; exit 1; }
fi

echo "=== Switching to explicit gpu_split: [10, 13] ==="
sed -i \
  -e 's/^  gpu_split_auto: true/  gpu_split_auto: false/' \
  -e 's/^  gpu_split: \[\]/  gpu_split: [10, 13]/' \
  "$CONFIG_PATH"
grep -n "gpu_split" "$CONFIG_PATH"

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

declare -a GPU0_READINGS
declare -a GPU1_READINGS

for i in $(seq 1 "$REPS"); do
  echo
  echo "=== Reload $i/$REPS ==="
  docker compose up -d --force-recreate tabbyapi
  if ! wait_for_load_or_fail; then
    echo "Stopping after reload $i's failure — explicit split doesn't fit as chosen." >&2
    exit 1
  fi
  reading="$(nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits)"
  echo "$reading" | sed 's/^/  /'
  g0="$(echo "$reading" | awk -F', ' '$1==0{print $2}')"
  g1="$(echo "$reading" | awk -F', ' '$1==1{print $2}')"
  GPU0_READINGS+=("$g0")
  GPU1_READINGS+=("$g1")
done

echo
echo "=== Stability across $REPS reloads ==="
echo "GPU 0 readings (MiB): ${GPU0_READINGS[*]}"
echo "GPU 1 readings (MiB): ${GPU1_READINGS[*]}"

python3 -c "
readings0 = [int(x) for x in '${GPU0_READINGS[*]}'.split()]
readings1 = [int(x) for x in '${GPU1_READINGS[*]}'.split()]
cap0, cap1 = 16311, 16376

for label, readings, cap in [('GPU 0', readings0, cap0), ('GPU 1', readings1, cap1)]:
    spread = max(readings) - min(readings)
    free_worst = cap - max(readings)
    print(f'{label}: min={min(readings)} max={max(readings)} spread={spread} MiB, worst-case free={free_worst} MiB')
    if spread > 200:
        print(f'  WARN: {spread} MiB spread across reloads — not as stable as hoped.')
    if free_worst < 1536:
        print(f'  WARN: worst-case free margin ({free_worst} MiB) below the ~1.5-2 GiB target.')
"

echo
echo "=== Done. If both GPUs show a small spread and comfortable free margin, ==="
echo "this split is a good candidate. Record it in docs/engineering-log.md."
echo "Container left running — tear down when finished: cd $POC_DIR && docker compose down"
