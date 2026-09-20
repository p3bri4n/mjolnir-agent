#!/usr/bin/env bash
# Point 2 of docs/briefs/visual-navigation-only.md's "Amendment —
# optimization pass before Phase 4" (external consultation, 2026-09-20):
# capture a DOM-with-boxes snapshot + a screenshot + its OCR reading, as
# one matched triple, for a handful of real pages across the benchmark
# fixtures — the raw data an OCR-to-layout reconstruction stage (row/
# column clustering, synthetic cell refs) can be iterated against
# offline, in seconds, with no agent loop involved. Same technique as
# probe-visual-snapshot-signal.sh: direct calls to mcp-client's /call
# endpoint.
#
# This script only COLLECTS the triples — it does not score OCR-vs-DOM
# agreement, deliberately: browser_snapshot's boxes=true text format has
# no known-good sample to parse against yet (never captured live before
# this session). Once a first triple's raw shape is seen, the matching/
# scoring logic can be written against the real format instead of a
# guess.
#
# Requires: docker compose up -d (core services) plus
# docker compose --profile test-fixtures up -d (all fixtures) already
# running — this script does not start them for you.
#
# Usage: bash scripts/probe-visual-mode-perception-harness.sh
# Output: scripts/output/visual-mode-perception-harness/<slug>/
#   dom_boxes.txt, ocr.json — one directory per page. The screenshot
# itself isn't saved (the OCR JSON already carries every detected
# text+box the reconstruction logic needs; keeping the binary image out
# of the split avoids a fragile stdout/stderr multiplexing scheme for a
# thin analysis-time benefit — reintroduce it if a visual side-by-side
# turns out to be needed once real triples are in hand).

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

OUT_DIR="scripts/output/visual-mode-perception-harness"
mkdir -p "$OUT_DIR"

RUNNING_SERVICES="$(docker compose ps --services --status running)"
for c in langgraph-agent mcp-client playwright-mcp ocr-service fixture-catalog fixture-docs fixture-hr-app fixture-admin fixture-perception; do
  if ! grep -qx "$c" <<<"$RUNNING_SERVICES"; then
    echo "Container '$c' is not running. Start it first:" >&2
    echo "  docker compose up -d" >&2
    echo "  docker compose --profile test-fixtures up -d" >&2
    exit 1
  fi
done

# slug -> URL. A deliberately varied sample: a dense data table (already
# known costly, T3's own page), a form (typing-heavy, point 5's target),
# a plain content/listing page, and one admin/perception page each for
# breadth — not an attempt at full benchmark coverage, just enough shapes
# for the reconstruction logic to be designed against more than one case.
declare -A PAGES=(
  [hr-employees-table]="http://fixture-hr-app:5000/employees"
  [hr-leave-form]="http://fixture-hr-app:5000/leave-form"
  # Root paths ("/") 404/serve nginx's default page on these three — the
  # real content lives under a subpath, same convention as
  # fixture-visual-probe's own /visual-probe/ (see
  # probe-visual-snapshot-signal.sh's comment). Verified against
  # tests_integration/test_web_tasks.py's own CATALOG_URL/DOCS_URL/
  # PERCEPTION_URL rather than guessed a second time.
  #
  # docs/resolved-bugs.md #66 follow-up: /catalog and /perception ALONE
  # still weren't enough — /catalog is a thin one-link landing page (the
  # real listing is index.html), and /perception has no index at all
  # (403, no autoindex) — its real pages are named individually
  # (e1-offviewport.html/e2-canvas.html/e3-equivalence.html, see
  # test_web_tasks_v2.py). e3 chosen over e1/e2: e2 is deliberately
  # DOM-invisible by design (its own separate probe), not representative
  # of an ordinary page for this harness's purpose.
  [catalog-listing]="http://fixture-catalog/catalog/index.html"
  [docs-listing]="http://fixture-docs/docs"
  [admin-root]="http://fixture-admin:5000/"
  [perception-root]="http://fixture-perception/perception/e3-equivalence.html"
)

capture_one() {
  local url="$1" slug="$2"
  docker compose exec -T langgraph-agent python3 - "$url" "$slug" <<'PYEOF'
import json
import sys

import httpx

url, slug = sys.argv[1], sys.argv[2]
mcp = "http://mcp-client:8003"
ocr_url = "http://ocr-service:8004"
thread_id = f"visual-mode-perception-harness-{slug}"


def call(client, tool, args):
    r = client.post(f"{mcp}/call", json={"tool": tool, "arguments": args, "thread_id": thread_id}, timeout=30)
    r.raise_for_status()
    return r.json()


with httpx.Client() as client:
    call(client, "browser_navigate", {"url": url})
    snapshot = call(client, "browser_snapshot", {"boxes": True})
    dom_text = "\n".join(
        b.get("text", "") for b in snapshot["content"] if isinstance(b, dict) and b.get("type") == "text"
    )
    screenshot = call(client, "browser_take_screenshot", {})
    image = next(b for b in screenshot["content"] if isinstance(b, dict) and b.get("type") == "image")
    ocr_resp = client.post(
        f"{ocr_url}/ocr",
        json={"image_base64": image["data"], "mime_type": image.get("mimeType", "image/png")},
        timeout=30,
    )
    ocr_resp.raise_for_status()
    detections = ocr_resp.json()

# stdout -> dom_boxes.txt ; stderr -> ocr.json (kept on separate streams,
# no line-splitting scheme, since both are plain UTF-8 text of unknown
# length either could realistically reach).
sys.stdout.write(dom_text)
sys.stderr.write(json.dumps(detections, ensure_ascii=False))
PYEOF
}

for slug in "${!PAGES[@]}"; do
  echo "=== $slug (${PAGES[$slug]}) ==="
  out="$OUT_DIR/$slug"
  mkdir -p "$out"
  capture_one "${PAGES[$slug]}" "$slug" 1>"$out/dom_boxes.txt" 2>"$out/ocr.json"
  echo "  saved: $out/{dom_boxes.txt,ocr.json}"
done

echo
echo "All pairs saved under $OUT_DIR/ — paste dom_boxes.txt + ocr.json back for analysis."
