#!/usr/bin/env bash
# Phase 0 of docs/briefs/effort-1.3-parallel-campaigns.md: the two live
# checks that archives alone cannot answer, both required before Phase 1
# (mcp-client worker-scoping) is worth building.
#
# 1. TabbyAPI concurrent-request behavior: 3 concurrent chat completions
#    vs 3 sequential, same prompt/max_tokens — replaces the brief's
#    pessimistic/optimistic bracket with a real number.
# 2. playwright-mcp session isolation under real concurrent load: two
#    INDEPENDENT MCP sessions opened directly against playwright-mcp
#    (bypassing mcp-client, whose _persistent_sessions is still keyed by
#    server_name alone until Phase 1 lands — going through mcp-client
#    today would reuse the same shared session and prove nothing), each
#    navigating to a different fixture-visual-probe page, confirming
#    browser_snapshot shows each session's own page.
#
# Requires: docker compose up -d (core services), plus
# docker compose --profile test-fixtures up -d fixture-visual-probe
# for check 2 (same fixture already used by the effort 3 probe).
#
# Usage: bash scripts/probe-parallel-phase0.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

RUNNING_SERVICES="$(docker compose ps --services --status running)"
for c in langgraph-agent tabbyapi mcp-client playwright-mcp fixture-visual-probe; do
  if ! grep -qx "$c" <<<"$RUNNING_SERVICES"; then
    echo "Container '$c' is not running. Start it first:" >&2
    echo "  docker compose up -d" >&2
    echo "  docker compose --profile test-fixtures up -d fixture-visual-probe" >&2
    exit 1
  fi
done

echo "=== Check 1: TabbyAPI concurrent vs sequential (3 requests each) ==="
docker compose exec -T langgraph-agent python3 - <<'PYEOF'
import time
import httpx

URL = "http://tabbyapi:5000/v1/chat/completions"
PAYLOAD = {
    "model": "agent-llm",
    "messages": [{"role": "user", "content": "Reply with the single word: ok."}],
    "max_tokens": 8,
    "temperature": 0,
}

def one_request(client):
    r = client.post(URL, json=PAYLOAD, timeout=60)
    r.raise_for_status()
    return r.json()

with httpx.Client() as client:
    t0 = time.monotonic()
    for _ in range(3):
        one_request(client)
    sequential_s = time.monotonic() - t0

import concurrent.futures

with httpx.Client() as client:
    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: one_request(client), range(3)))
    concurrent_s = time.monotonic() - t0

print(f"sequential (3 requests): {sequential_s:.2f}s")
print(f"concurrent (3 requests): {concurrent_s:.2f}s")
print(f"speedup: {sequential_s / concurrent_s:.2f}x (1.0x = fully serialized, 3.0x = fully parallel)")
PYEOF

echo ""
echo "=== Check 2: playwright-mcp session isolation (2 independent sessions) ==="
docker compose exec -T mcp-client python3 - <<'PYEOF'
import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://playwright-mcp:8931/mcp"
PAGES = {
    "session-A": "http://fixture-visual-probe/visual-probe/vp7-svg-text.html",
    "session-B": "http://fixture-visual-probe/visual-probe/vp8-offviewport.html",
}


async def run_session(label, url):
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("browser_navigate", {"url": url})
            result = await session.call_tool("browser_snapshot", {})
            text = "\n".join(b.text for b in result.content if getattr(b, "type", None) == "text")
            return label, url, text


async def main():
    results = await asyncio.gather(
        *(run_session(label, url) for label, url in PAGES.items())
    )
    ok = True
    for label, url, text in results:
        page_url_line = next((l for l in text.splitlines() if l.strip().startswith("- Page URL:")), "MISSING")
        matches = url in page_url_line
        ok = ok and matches
        print(f"{label}: expected {url}")
        print(f"  {page_url_line}  {'OK' if matches else '!! MISMATCH — sessions are NOT isolated'}")
    print()
    print("RESULT: sessions are isolated" if ok else "RESULT: sessions are NOT isolated — re-read the brief's architecture section")


asyncio.run(main())
PYEOF
