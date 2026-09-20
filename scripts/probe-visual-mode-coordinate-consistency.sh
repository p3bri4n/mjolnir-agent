#!/usr/bin/env bash
# Point 1 of docs/briefs/visual-navigation-only.md's "Amendment —
# optimization pass before Phase 4" (external consultation, 2026-09-20):
# before building anything on top of VISUAL_NAVIGATION_ONLY's OCR/
# coordinate pipeline, rule out a device-pixel-vs-CSS-pixel mismatch and
# quantify how much a screenshot's OCR reading jitters between two
# identical captures of the same static page. Same technique as
# probe-visual-snapshot-signal.sh: direct calls to mcp-client's /call
# endpoint, no LLM, no LangGraph loop.
#
# Part A: 5 consecutive browser_take_screenshot calls of the same static
# page (no navigation in between), OCR'd independently — measures pure
# capture/OCR jitter for matching detected text, nothing else varying.
# Part B: one browser_snapshot(boxes=true) (real DOM element boxes,
# CSS-pixel, viewport-relative, per the installed @playwright/mcp
# schema) captured back-to-back with one screenshot+OCR of the identical
# page state — a direct ground-truth comparison of "where the DOM says
# an element is" vs "where OCR says its label is", without needing to
# perform and verify an actual click. If the two disagree by more than a
# few pixels on the same visible text, that's the device/CSS-pixel
# mismatch (or an OCR-side resize) the consultation's top hypothesis
# named — checked here empirically rather than assumed.
#
# Requires: docker compose up -d (core services) plus
# docker compose --profile test-fixtures up -d fixture-hr-app already
# running — this script does not start them for you.
#
# Usage: bash scripts/probe-visual-mode-coordinate-consistency.sh
# Output: printed to stdout AND saved under
# scripts/output/visual-mode-coordinate-consistency/ for offline review.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

OUT_DIR="scripts/output/visual-mode-coordinate-consistency"
mkdir -p "$OUT_DIR"

RUNNING_SERVICES="$(docker compose ps --services --status running)"
for c in langgraph-agent mcp-client playwright-mcp ocr-service fixture-hr-app; do
  if ! grep -qx "$c" <<<"$RUNNING_SERVICES"; then
    echo "Container '$c' is not running. Start it first:" >&2
    echo "  docker compose up -d" >&2
    echo "  docker compose --profile test-fixtures up -d fixture-hr-app" >&2
    exit 1
  fi
done

docker compose exec -T langgraph-agent python3 - <<'PYEOF' | tee "$OUT_DIR/report.txt"
import json
from collections import defaultdict

import httpx

MCP = "http://mcp-client:8003"
OCR = "http://ocr-service:8004"
URL = "http://fixture-hr-app:5000/employees"
THREAD = "visual-mode-coordinate-consistency-probe"


def call(client, tool, args):
    r = client.post(f"{MCP}/call", json={"tool": tool, "arguments": args, "thread_id": THREAD}, timeout=30)
    r.raise_for_status()
    return r.json()


def ocr_image_block(client, content):
    image = next(b for b in content if isinstance(b, dict) and b.get("type") == "image")
    r = client.post(
        f"{OCR}/ocr",
        json={"image_base64": image["data"], "mime_type": image.get("mimeType", "image/png")},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


with httpx.Client() as client:
    call(client, "browser_navigate", {"url": URL})

    print("=== Part A: OCR jitter across 5 consecutive screenshots (same static page) ===")
    shots = []
    for i in range(5):
        result = call(client, "browser_take_screenshot", {})
        detections = ocr_image_block(client, result["content"])
        shots.append(detections)
        print(f"shot {i}: {len(detections)} detections")

    # Bug found live (docs/resolved-bugs.md #65): grouping by text VALUE
    # alone conflates same-text-different-row with same-row-different-
    # capture whenever a column repeats a value (department names,
    # round-numbered salaries) — the exact ambiguity the reconstruction
    # amendment (point 3) warns about, just hit here first. Only text
    # appearing EXACTLY ONCE PER SHOT is a safe jitter probe; anything
    # appearing more than once in a single shot is excluded rather than
    # silently mismatched across rows.
    per_shot_counts = defaultdict(int)
    for shot in shots:
        seen_this_shot = set()
        for d in shot:
            if d["text"] in seen_this_shot:
                per_shot_counts[d["text"]] += 1  # mark as repeated-within-a-shot
            seen_this_shot.add(d["text"])
    by_text = defaultdict(list)
    for shot in shots:
        for d in shot:
            if per_shot_counts.get(d["text"], 0) == 0:  # never repeated within any single shot
                by_text[d["text"]].append((d["x"], d["y"]))
    max_dx = max_dy = 0.0
    worst = None
    stable_texts = 0
    for text, coords in by_text.items():
        if len(coords) < 5:
            continue  # only compare text detected in EVERY shot
        stable_texts += 1
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        dx, dy = max(xs) - min(xs), max(ys) - min(ys)
        if dx > max_dx or dy > max_dy:
            max_dx, max_dy, worst = max(dx, max_dx), max(dy, max_dy), text
    print(f"unique-per-shot texts usable as jitter probes: {stable_texts}")
    print(f"max x jitter: {max_dx:.1f}px, max y jitter: {max_dy:.1f}px (worst: {worst!r})")

    print("\n=== Part B: DOM boxes (ground truth) vs OCR boxes, same page state ===")
    snapshot = call(client, "browser_snapshot", {"boxes": True})
    snapshot_text = "\n".join(
        b.get("text", "") for b in snapshot["content"] if isinstance(b, dict) and b.get("type") == "text"
    )
    screenshot = call(client, "browser_take_screenshot", {})
    detections = ocr_image_block(client, screenshot["content"])

    print("--- browser_snapshot(boxes=true), raw text ---")
    print(snapshot_text[:6000])
    print("\n--- OCR detections, raw JSON ---")
    print(json.dumps(detections, ensure_ascii=False, indent=2)[:6000])
PYEOF

echo
echo "Full report saved to $OUT_DIR/report.txt — paste back for analysis."
