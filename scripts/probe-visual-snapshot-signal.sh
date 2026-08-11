#!/usr/bin/env bash
# One-off empirical check for effort 3's explicit checkpoint
# (docs/briefs/update-plan.md, "Effort 3": "_detect_visual_signal is a
# stub ... pending an empirical check of what browser_snapshot actually
# emits for these elements"). Same technique as the original visual-
# channel feasibility probe (docs/architecture/visual-channel-
# feasibility.md, docs/history.md "SONDE DE FAISABILITÉ CANAL VISUEL"):
# direct calls to mcp-client's /call endpoint, no LLM, no LangGraph loop.
#
# What the feasibility probe already established (matrix ✗/✓ per case)
# is NOT what this script checks: it recorded whether the GROUND-TRUTH
# STRING is readable via each channel, not the raw text browser_snapshot
# emits for a canvas/webgl/img/pdf-viewer node. _detect_visual_signal
# needs a pattern to grep for ("is there a visual-only element here at
# all", not "what does it say") — that raw text was never captured. This
# script captures it, for VP1 (canvas), VP2 (webgl), VP3 (img alt=""),
# VP4 (PDF, native viewer), plus VP7 (SVG text) and VP8 (off-viewport) as
# DOM-transparent controls so a candidate pattern can be checked against
# false positives too.
#
# Requires: docker compose up -d (core services) plus
# docker compose --profile test-fixtures up -d fixture-visual-probe
# already running — this script does not start them for you (mirrors
# run-campaign.sh's own preflight discipline: fail loud, don't guess).
#
# Usage: bash scripts/probe-visual-snapshot-signal.sh
# Output: raw browser_snapshot text per case, printed AND saved under
# scripts/output/visual-snapshot-signal/<case>.txt for offline diffing.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

OUT_DIR="scripts/output/visual-snapshot-signal"
mkdir -p "$OUT_DIR"

RUNNING_SERVICES="$(docker compose ps --services --status running)"
for c in langgraph-agent mcp-client playwright-mcp fixture-visual-probe; do
  if ! grep -qx "$c" <<<"$RUNNING_SERVICES"; then
    echo "Container '$c' is not running. Start it first:" >&2
    echo "  docker compose up -d" >&2
    echo "  docker compose --profile test-fixtures up -d fixture-visual-probe" >&2
    exit 1
  fi
done

# slug -> URL (VP4 navigates straight to the .pdf, matching the original
# probe's method: "the most representative case of a PDF the agent is
# looking at" — not the HTML wrapper page with a link to it).
# Dockerfile generates into /site/visual-probe/ then COPYs the whole
# /site tree to nginx's html root — pages are served under /visual-probe/,
# not at the fixture's root (caught by the first run: every case 404'd).
declare -A CASES=(
  [vp1-canvas2d]="http://fixture-visual-probe/visual-probe/vp1-canvas2d.html"
  [vp2-webgl]="http://fixture-visual-probe/visual-probe/vp2-webgl.html"
  [vp3-image]="http://fixture-visual-probe/visual-probe/vp3-image.html"
  [vp4-pdf]="http://fixture-visual-probe/visual-probe/vp4-document.pdf"
  [vp7-svg-text-control]="http://fixture-visual-probe/visual-probe/vp7-svg-text.html"
  [vp8-offviewport-control]="http://fixture-visual-probe/visual-probe/vp8-offviewport.html"
)

# Raw text only on stdout (nothing written from inside the container):
# langgraph-agent's only bind mount is ./workspace:/workspace, which does
# NOT cover the repo-root scripts/ directory this script lives in — so
# the file is written HOST-side, by redirecting this function's stdout,
# not container-side.
run_probe() {
  local case_id="$1" url="$2"
  docker compose exec -T langgraph-agent python3 - "$case_id" "$url" <<'PYEOF'
import sys

import httpx

case_id, url = sys.argv[1], sys.argv[2]
base = "http://mcp-client:8003"
thread_id = "visual-snapshot-signal-probe"

with httpx.Client(timeout=30.0) as client:
    nav = client.post(
        f"{base}/call",
        json={"tool": "browser_navigate", "arguments": {"url": url}, "thread_id": thread_id},
    )
    nav.raise_for_status()

    snap = client.post(
        f"{base}/call",
        json={"tool": "browser_snapshot", "arguments": {}, "thread_id": thread_id},
    )
    snap.raise_for_status()
    blocks = snap.json().get("content", [])
    text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")

sys.stderr.write(f"=== {case_id} ({url}) ===\n")
print(text)
PYEOF
}

for case_id in "${!CASES[@]}"; do
  out_file="$OUT_DIR/$case_id.txt"
  run_probe "$case_id" "${CASES[$case_id]}" | tee "$out_file"
  echo
done

echo "Raw snapshots saved under $OUT_DIR/ — paste back for pattern design."
