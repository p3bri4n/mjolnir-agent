#!/usr/bin/env bash
# One-off empirical check: what does a single POST /ocr call cost
# (wall-clock latency) on the real PaddleOCR-CPU engine (docs/project-
# status.md, Effort 3 — ocr-service deployed, zero callers, cost never
# measured before the retire-vs-keep decision).
#
# Runs entirely inside the ocr-service container (self-loopback to
# localhost:8004, same target as its own healthcheck) — Pillow is already
# a runtime dependency there (requirements.txt), so no extra install.
# This measures the ENGINE's own processing time only: it does NOT
# include the internal-network hop a real caller (langgraph-agent, on
# agent-net) would add on top — expected sub-millisecond on a Docker
# bridge network, not measured here, not claimed here.
#
# ocr-service has no bind mount (docker-compose.yml), so output is
# captured host-side by redirecting this script's own stdout, same
# technique as probe-visual-snapshot-signal.sh.
#
# Requires: docker compose up -d ocr-service already running.
#
# Usage: bash scripts/probe-ocr-cost.sh
# Output: printed AND saved to scripts/output/ocr-cost/result.txt

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

OUT_DIR="scripts/output/ocr-cost"
mkdir -p "$OUT_DIR"

if ! docker compose ps --services --status running | grep -qx "ocr-service"; then
  echo "Container 'ocr-service' is not running. Start it first:" >&2
  echo "  docker compose up -d ocr-service" >&2
  exit 1
fi

docker compose exec -T ocr-service python3 - <<'PYEOF' | tee "$OUT_DIR/result.txt"
import base64
import io
import json
import statistics
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont

try:
    FONT = ImageFont.truetype("DejaVuSans.ttf", 16)
except OSError:
    FONT = ImageFont.load_default()

SAMPLE_LINES = [
    "Fiche produit — reference KX-4471",
    "Description : capteur de temperature industriel, plage -20C a 85C.",
    "Prix : 129,90 EUR HT — disponibilite : en stock (Entrepot Nord).",
    "Documentation technique : voir la page /docs/format-references.",
    "Compatibilite : montage DIN rail, alimentation 24V DC.",
    "Garantie constructeur : 24 mois pieces et main d'oeuvre.",
    "Contact support : support@example.invalid — +33 1 23 45 67 89.",
    "Derniere mise a jour du catalogue : 2026-08-10.",
]


def build_image(width: int, height: int, n_lines: int) -> bytes:
    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)
    y = 10
    for i in range(n_lines):
        line = SAMPLE_LINES[i % len(SAMPLE_LINES)]
        draw.text((10, y), line, fill="black", font=FONT)
        y += 22
        if y > height - 20:
            break
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def call_ocr(image_bytes: bytes) -> tuple[float, int]:
    body = json.dumps(
        {
            "image_base64": base64.b64encode(image_bytes).decode("ascii"),
            "mime_type": "image/png",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:8004/ocr",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=60) as resp:
        detections = json.loads(resp.read())
    elapsed = time.monotonic() - start
    return elapsed, len(detections)


CASES = [
    ("small_320x100_2lines", 320, 100, 2),
    ("medium_800x600_15lines", 800, 600, 15),
    ("full_1280x800_30lines", 1280, 800, 30),
]

N_REPS = 5

print(f"{'case':<28}{'payload_kb':>12}{'detections':>12}{'min_s':>10}{'median_s':>10}{'mean_s':>10}{'max_s':>10}")
for name, w, h, n_lines in CASES:
    image_bytes = build_image(w, h, n_lines)
    payload_kb = len(base64.b64encode(image_bytes)) / 1024

    # Warmup call, discarded (engine already loaded at process start —
    # see app/ocr_engine.py's module-level `engine = get_engine()` — but
    # kept anyway to absorb any first-call effect, e.g. CPU/thread cache).
    _, _ = call_ocr(image_bytes)

    latencies = []
    detections_count = 0
    for _ in range(N_REPS):
        elapsed, detections_count = call_ocr(image_bytes)
        latencies.append(elapsed)

    print(
        f"{name:<28}{payload_kb:>12.1f}{detections_count:>12}"
        f"{min(latencies):>10.3f}{statistics.median(latencies):>10.3f}"
        f"{statistics.mean(latencies):>10.3f}{max(latencies):>10.3f}"
    )
PYEOF
