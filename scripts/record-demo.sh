#!/usr/bin/env bash
# Scripted demo GIF recording (docs/briefs/readme-rework.md, point 2).
# Brings up the stack + the demo fixture profile, drives a real Chromium
# against Open WebUI (scripts/demo/drive_demo.py), records the session,
# converts to an optimised GIF. See scripts/demo/drive_demo.py's own
# docstring for the parts that need live verification against Open
# WebUI's actual DOM before this produces a usable capture.
#
# Requires on THIS machine (not in a container): docker, docker compose,
# ffmpeg, Xvfb, python3. Never run from Claude's own sandbox -- no
# Docker/GPU there (CLAUDE.md, "Operational traps").
#
# Usage:
#   DEMO_OWUI_EMAIL=you@example.com DEMO_OWUI_PASSWORD=... \
#     ./scripts/record-demo.sh
#
# Needs an EXISTING Open WebUI account (WEBUI_AUTH=true in
# docker-compose.yml) -- sign up once through the normal UI first if you
# don't have one yet.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

: "${DEMO_OWUI_EMAIL:?Set DEMO_OWUI_EMAIL (an existing Open WebUI account).}"
: "${DEMO_OWUI_PASSWORD:?Set DEMO_OWUI_PASSWORD.}"

DISPLAY_NUM="${DEMO_DISPLAY_NUM:-99}"
export DISPLAY=":${DISPLAY_NUM}"
WIDTH=1280
HEIGHT=800
FPS=15
RAW_VIDEO="/tmp/demo-raw.mp4"
READY_SENTINEL="/tmp/demo-ready"
DONE_SENTINEL="/tmp/demo-done"
VENV_DIR="/tmp/demo-venv"
OUT_GIF="docs/assets/demo.gif"

for bin in docker ffmpeg Xvfb python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "Missing required tool: $bin" >&2; exit 1; }
done

rm -f "$READY_SENTINEL" "$DONE_SENTINEL" "$RAW_VIDEO"

echo "==> Bringing up the stack + demo fixtures"
docker compose up -d
docker compose --profile demo up -d fixture-demo-catalog fixture-demo-admin

echo "==> Waiting for services to answer"
wait_http() {
  local url="$1" label="$2" tries=60
  until curl -sf -o /dev/null "$url"; do
    tries=$((tries - 1))
    [ "$tries" -le 0 ] && { echo "Timed out waiting for $label ($url)" >&2; exit 1; }
    sleep 2
  done
}
wait_http "http://localhost:3000/" "open-webui"
wait_http "http://localhost:8090/health" "dashboard"

echo "==> Setting up the demo driver's Python environment"
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install -q -r scripts/demo/requirements.txt
  "$VENV_DIR/bin/python" -m playwright install --with-deps chromium
fi

echo "==> Starting Xvfb on display $DISPLAY (${WIDTH}x${HEIGHT})"
Xvfb "$DISPLAY" -screen 0 "${WIDTH}x${HEIGHT}x24" &
XVFB_PID=$!
trap 'kill "$XVFB_PID" 2>/dev/null || true' EXIT
sleep 1

echo "==> Launching the demo driver"
DEMO_OWUI_EMAIL="$DEMO_OWUI_EMAIL" \
DEMO_OWUI_PASSWORD="$DEMO_OWUI_PASSWORD" \
DEMO_READY_SENTINEL="$READY_SENTINEL" \
DEMO_DONE_SENTINEL="$DONE_SENTINEL" \
  "$VENV_DIR/bin/python" scripts/demo/drive_demo.py &
DRIVER_PID=$!

echo "==> Waiting for the browser window to be ready"
tries=60
until [ -f "$READY_SENTINEL" ]; do
  tries=$((tries - 1))
  [ "$tries" -le 0 ] && { echo "Timed out waiting for the demo driver" >&2; kill "$DRIVER_PID" 2>/dev/null || true; exit 1; }
  sleep 1
done

echo "==> Recording (display $DISPLAY)"
ffmpeg -y -f x11grab -video_size "${WIDTH}x${HEIGHT}" -framerate "$FPS" \
  -i "$DISPLAY" -codec:v libx264 -preset ultrafast -pix_fmt yuv420p \
  "$RAW_VIDEO" &
FFMPEG_PID=$!

echo "==> Waiting for the task to complete"
tries=150
until [ -f "$DONE_SENTINEL" ]; do
  tries=$((tries - 1))
  if [ "$tries" -le 0 ]; then
    echo "Timed out waiting for the task to finish -- stopping anyway" >&2
    break
  fi
  sleep 2
done

echo "==> Stopping the recording"
kill -INT "$FFMPEG_PID"
wait "$FFMPEG_PID" 2>/dev/null || true
wait "$DRIVER_PID" 2>/dev/null || true

echo "==> Converting to an optimised GIF"
mkdir -p "$(dirname "$OUT_GIF")"
PALETTE="/tmp/demo-palette.png"
ffmpeg -y -i "$RAW_VIDEO" -vf "fps=${FPS},scale=${WIDTH}:-1:flags=lanczos,palettegen" "$PALETTE"
ffmpeg -y -i "$RAW_VIDEO" -i "$PALETTE" \
  -filter_complex "fps=${FPS},scale=${WIDTH}:-1:flags=lanczos[x];[x][1:v]paletteuse" \
  "$OUT_GIF"

SIZE_MB=$(du -m "$OUT_GIF" | cut -f1)
echo "==> Done: $OUT_GIF (${SIZE_MB} MB)"
if [ "$SIZE_MB" -gt 4 ]; then
  echo "Over the ~4 MB budget (docs/briefs/readme-rework.md, point 2) -- lower FPS in this script before resolution, then re-run." >&2
fi
