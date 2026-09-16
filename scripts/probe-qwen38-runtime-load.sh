#!/usr/bin/env bash
# Phase 0 of docs/briefs/qwen3.8-27b-evaluation.md: before touching the
# production tabbyapi service's pinned Dockerfile digest, verify — in
# complete isolation from the production stack — that a current TabbyAPI
# release can load the Qwen3.8-27B EXL3 4.50bpw build cleanly, and record
# the real exllamav3/torch/tabbyAPI triplet actually running it (never
# deduced from pyproject.toml, see docs/engineering-log.md "Triplet de
# versions").
#
# Why this matters: the installed production runtime is exllamav3 1.1.0
# (confirmed live, docs/engineering-log.md:134); this build's
# quantization_config.json says it was produced by quantizer 1.4.1 — NEWER
# than the runtime that would load it. That is the dangerous direction: a
# codebook-version mismatch does not error, it silently mis-decodes.
# Qwen3.8 architecture support only landed in the 1.4.x exllamav3 line.
#
# Uses the existing isolated PoC (poc/qwen3.8/, container
# qwen38-tabbyapi, port 5001, added 2026-08-19, commit aa5f96c) — does
# NOT touch services/tabbyapi/*, docker-compose.yml, or the production
# `tabbyapi` container. poc/qwen3.8/Dockerfile applies the same
# python3-dev patch as services/tabbyapi/Dockerfile (gated_delta_net's
# Triton JIT compile, same architecture family, see that file's comment)
# on top of the digest this brief resolved live — a first run without
# this patch timed out with neither a load confirmation nor a recognized
# error string in the logs, because the stock image lacks the patch and
# this script's failure detection didn't cover that error text; fixed
# here on both sides (build the patched image, AND never tear down
# without dumping the logs first).
#
# That PoC config has vision/MTP/tool_format disabled — this script only
# clears Phase 0's gate (does the codebook decode correctly at all);
# Phase 2's static measurements (VRAM, MTP, tool-calling) need those
# re-enabled separately, later.
#
# Usage: bash scripts/probe-qwen38-runtime-load.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
POC_DIR="$PROJECT_DIR/poc/qwen3.8"

MIN_EXLLAMAV3_MAJOR=1
MIN_EXLLAMAV3_MINOR=4
LOAD_TIMEOUT_SECONDS=600
POLL_INTERVAL_SECONDS=5

if [ ! -d "$POC_DIR" ]; then
  echo "Expected $POC_DIR — the PoC compose/config this script drives. Not found." >&2
  exit 1
fi
cd "$POC_DIR"

if docker ps --filter "name=^tabbyapi$" --format '{{.Names}}' | grep -qx tabbyapi; then
  cat >&2 <<'EOF'
WARNING: the production `tabbyapi` container is currently running.

This PoC's docker-compose.yml reserves ALL GPUs (`count: all`), same as
the production stack — running both at once will contend for VRAM and
may OOM one or the other rather than testing the codebook cleanly.

Stop the production stack first (docker compose down, from the project
root) for a clean read, or continue only if you know there's enough
headroom for both models loaded simultaneously.
EOF
  read -rp "Continue anyway? [y/N] " confirm
  if [[ "${confirm:-N}" != "y" && "${confirm:-N}" != "Y" ]]; then
    echo "Aborted." >&2
    exit 1
  fi
fi

BASE_DIGEST="$(grep -m1 '^FROM' Dockerfile | awk '{print $2}')"
echo "=== Building the patched image (python3-dev on top of $BASE_DIGEST) ==="
docker compose build --pull tabbyapi

echo
echo "=== Starting the isolated PoC container (qwen38-tabbyapi, port 5001) ==="
docker compose up -d --force-recreate tabbyapi

LOAD_CONFIRMED=false
cleanup() {
  echo
  echo "=== Last 120 log lines (captured before teardown, whatever the outcome) ==="
  docker compose logs tabbyapi 2>/dev/null | tail -120
  echo
  echo "=== Tearing down the isolated PoC container ==="
  docker compose down
  if [ "$LOAD_CONFIRMED" != "true" ]; then
    echo
    echo "Run did NOT reach a confirmed clean load — see the log tail above for the" >&2
    echo "actual reason (this script no longer discards it on timeout/failure)." >&2
  fi
}
trap cleanup EXIT

echo
echo "=== Waiting for a load outcome (timeout ${LOAD_TIMEOUT_SECONDS}s) ==="
waited=0
until docker compose logs tabbyapi 2>/dev/null \
  | grep -qE "Model successfully loaded|Traceback|CUDA out of memory|CUDA error|fatal error|Segmentation fault|Exception"; do
  if ! docker compose ps --status running --services 2>/dev/null | grep -qx tabbyapi; then
    echo "Container exited before reaching a recognized log outcome — treating as a failure." >&2
    exit 1
  fi
  if (( waited >= LOAD_TIMEOUT_SECONDS )); then
    echo "TIMEOUT — no load confirmation and no recognized error string after ${LOAD_TIMEOUT_SECONDS}s." >&2
    echo "Log tail follows (see below); if it's still visibly progressing (reading" >&2
    echo "safetensors, allocating cache), just raise LOAD_TIMEOUT_SECONDS and re-run —" >&2
    echo "this mount may simply be slow for a first, cold read of a ~15GB model." >&2
    exit 1
  fi
  sleep "$POLL_INTERVAL_SECONDS"
  waited=$((waited + POLL_INTERVAL_SECONDS))
done

if docker compose logs tabbyapi 2>/dev/null \
  | grep -qE "Traceback|CUDA out of memory|CUDA error|fatal error|Segmentation fault|Exception"; then
  echo "LOAD FAILED — see the log tail below (also printed again on teardown)." >&2
  echo "If this is a CUDA/codebook-shaped error: that is the SAFE failure mode the" >&2
  echo "brief expects if the runtime is still wrong for this architecture/quant." >&2
  echo "Do NOT proceed to Phase 1 on this result." >&2
  exit 1
fi

echo "Log shows 'Model successfully loaded'. Confirming via GET /v1/model..."
sleep 2
curl -sf http://localhost:5001/v1/model || {
  echo "Model reported loaded but /v1/model didn't respond as expected — investigate before trusting this run." >&2
  exit 1
}
echo

echo
echo "=== Real triplet, constaté AU RUNTIME ==="
docker compose exec -T tabbyapi sh -c 'pip show exllamav3 torch; pip list 2>/dev/null | grep -iE "exllamav3|torch|tabbyapi"'

EXL3_VERSION="$(docker compose exec -T tabbyapi sh -c "pip show exllamav3 2>/dev/null | grep -i '^Version'" | cut -d' ' -f2 | tr -d '\r\n')"
echo
echo "exllamav3 version reported: ${EXL3_VERSION:-unknown}"

EXL3_MAJOR="$(echo "$EXL3_VERSION" | cut -d. -f1)"
EXL3_MINOR="$(echo "$EXL3_VERSION" | cut -d. -f2)"

if [[ -z "$EXL3_MAJOR" || -z "$EXL3_MINOR" ]]; then
  echo "Could not parse a major.minor version from '$EXL3_VERSION' — check by hand before trusting this run." >&2
  exit 1
fi

if (( EXL3_MAJOR > MIN_EXLLAMAV3_MAJOR )) || { (( EXL3_MAJOR == MIN_EXLLAMAV3_MAJOR )) && (( EXL3_MINOR >= MIN_EXLLAMAV3_MINOR )); }; then
  echo "GATE PASSED: exllamav3 $EXL3_VERSION >= $MIN_EXLLAMAV3_MAJOR.$MIN_EXLLAMAV3_MINOR — safe direction for this quant's codebook."
else
  echo "GATE FAILED: exllamav3 $EXL3_VERSION < $MIN_EXLLAMAV3_MAJOR.$MIN_EXLLAMAV3_MINOR." >&2
  echo "A 'successful' load above does NOT mean correct decoding at this version — do not trust it." >&2
  exit 1
fi

LOAD_CONFIRMED=true

echo
echo "=== Next steps (manual, per docs/briefs/qwen3.8-27b-evaluation.md, Phase 0) ==="
echo "1. Record in docs/engineering-log.md: this triplet, and base image digest:"
echo "     $BASE_DIGEST"
echo "2. Bump services/tabbyapi/Dockerfile's pinned digest to the value above."
echo "3. docker compose build tabbyapi && docker compose up -d --force-recreate tabbyapi"
echo "   (from the project root, not this poc/ directory)."
echo "4. Run the standard A1/A2 smoke on the CURRENT Qwen3.6 production model on"
echo "   this new image BEFORE loading the 3.8 weights into production — isolates"
echo "   the image-upgrade effect from the model-swap effect (Phase 0, step 3)."
echo
echo "This run did NOT validate vision, MTP, or tool_format (poc/qwen3.8/config.yml"
echo "has them off/absent) — that's Phase 2's static measurements, not this gate."
