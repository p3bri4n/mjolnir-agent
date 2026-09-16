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
# `tabbyapi` container. That PoC config has vision/MTP/tool_format
# disabled — this script only clears Phase 0's gate (does the codebook
# decode correctly at all); Phase 2's static measurements (VRAM, MTP,
# tool-calling) need those re-enabled separately, later.
#
# Usage: bash scripts/probe-qwen38-runtime-load.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
POC_DIR="$PROJECT_DIR/poc/qwen3.8"

MIN_EXLLAMAV3_MAJOR=1
MIN_EXLLAMAV3_MINOR=4

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

echo "=== Pulling ghcr.io/theroyallab/tabbyapi:latest ==="
docker compose pull tabbyapi

IMAGE_DIGEST="$(docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/theroyallab/tabbyapi:latest 2>/dev/null || echo "unknown")"
echo "Resolved image: $IMAGE_DIGEST"
echo "(this is 'latest' resolved NOW — not reproducible by name alone; record this exact digest, not the tag, if you act on this result)"

echo
echo "=== Starting the isolated PoC container (qwen38-tabbyapi, port 5001) ==="
docker compose up -d --force-recreate tabbyapi

cleanup() {
  echo
  echo "=== Tearing down the isolated PoC container ==="
  docker compose down
}
trap cleanup EXIT

echo
echo "=== Waiting for a load outcome (timeout 300s) ==="
waited=0
timeout=300
interval=5
until docker compose logs tabbyapi 2>/dev/null | grep -qE "Model successfully loaded|Traceback|CUDA out of memory|CUDA error"; do
  if (( waited >= timeout )); then
    echo "TIMEOUT waiting for a load outcome — inspect manually:" >&2
    echo "  cd $POC_DIR && docker compose logs tabbyapi" >&2
    exit 1
  fi
  sleep "$interval"
  waited=$((waited + interval))
done

if docker compose logs tabbyapi 2>/dev/null | grep -qE "Traceback|CUDA out of memory|CUDA error"; then
  echo "LOAD FAILED — see the log tail below." >&2
  echo "This is the SAFE failure mode the brief expects if the runtime is still" >&2
  echo "too old for this architecture. Do NOT proceed to Phase 1 on this result." >&2
  docker compose logs tabbyapi | tail -60
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

echo
echo "=== Next steps (manual, per docs/briefs/qwen3.8-27b-evaluation.md, Phase 0) ==="
echo "1. Record in docs/engineering-log.md: this triplet, and image digest:"
echo "     $IMAGE_DIGEST"
echo "2. Bump services/tabbyapi/Dockerfile's pinned digest to the value above"
echo "   (or a tag you deliberately choose to track instead of a moving 'latest')."
echo "3. Re-check whether the python3-dev JIT patch in that Dockerfile is still"
echo "   needed on the new base image — do not assume it carries over."
echo "4. docker compose build tabbyapi && docker compose up -d --force-recreate tabbyapi"
echo "5. Run the standard A1/A2 smoke on the CURRENT Qwen3.6 production model on"
echo "   this new image BEFORE loading the 3.8 weights into production — isolates"
echo "   the image-upgrade effect from the model-swap effect (Phase 0, step 3)."
echo
echo "This run did NOT validate vision, MTP, or tool_format (poc/qwen3.8/config.yml"
echo "has them off/absent) — that's Phase 2's static measurements, not this gate."
