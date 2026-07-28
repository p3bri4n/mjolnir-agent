"""
Local observability cockpit: a single page (GET /) that polls GET
/api/snapshot every 2s. This endpoint aggregates in parallel, each source
best-effort (a down source returns its section as null, never a global
500 — see _fetch_* below):

- llama-server: /metrics (Prometheus, see app/prometheus.py) and /slots
  (context occupied per slot).
- langgraph-agent: /threads/recent (selection menu, Phase 3) then
  /context for the resolved thread (detailed context breakdown).
- GPU VRAM via nvidia-smi (see app/gpu.py), only if
  ENABLE_GPU_STATS=true (requires the nvidia runtime on the
  docker-compose side).
"""

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.gpu import parse_nvidia_smi_csv, run_nvidia_smi
from app.prometheus import extract_llama_metrics, normalize_slots

app = FastAPI(title="Dashboard")

LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL", "http://llama-server:8000")
LANGGRAPH_AGENT_URL = os.environ.get("LANGGRAPH_AGENT_URL", "http://langgraph-agent:8000")
ENABLE_GPU_STATS = os.environ.get("ENABLE_GPU_STATS", "false").lower() == "true"
# Short: /api/snapshot is polled every 2s by the page (see static/
# index.html) — a slow source must never blow this budget, even if it
# means returning this section as null for THIS snapshot.
HTTP_TIMEOUT_SECONDS = 2.0

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index():
    return (_STATIC_DIR / "index.html").read_text(encoding="utf-8")


async def _fetch_llama_metrics(client: httpx.AsyncClient) -> Optional[dict]:
    try:
        resp = await client.get(f"{LLAMA_SERVER_URL}/metrics")
        resp.raise_for_status()
        return extract_llama_metrics(resp.text)
    except httpx.HTTPError:
        return None


async def _fetch_llama_slots(client: httpx.AsyncClient) -> Optional[list]:
    try:
        resp = await client.get(f"{LLAMA_SERVER_URL}/slots")
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    return normalize_slots(data)


async def _fetch_recent_threads(client: httpx.AsyncClient) -> list:
    try:
        resp = await client.get(f"{LANGGRAPH_AGENT_URL}/threads/recent")
        resp.raise_for_status()
        return resp.json().get("threads", [])
    except (httpx.HTTPError, ValueError):
        return []


async def _fetch_context(client: httpx.AsyncClient, thread_id: str) -> Optional[dict]:
    try:
        resp = await client.post(f"{LANGGRAPH_AGENT_URL}/context", json={"thread_id": thread_id})
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return None


async def _fetch_gpu_stats() -> Optional[list]:
    if not ENABLE_GPU_STATS:
        return None
    text = await asyncio.to_thread(run_nvidia_smi)
    if text is None:
        return None
    return parse_nvidia_smi_csv(text)


@app.get("/api/snapshot")
async def snapshot(thread_id: Optional[str] = None):
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        llama_metrics, llama_slots, threads, gpu = await asyncio.gather(
            _fetch_llama_metrics(client),
            _fetch_llama_slots(client),
            _fetch_recent_threads(client),
            _fetch_gpu_stats(),
        )

        # Resolved thread: the one explicitly requested (user selection on
        # the page side, Phase 3), otherwise the most recently known one —
        # never call /context without a valid thread_id, otherwise that
        # endpoint derives a thread_id from an empty history (see
        # langgraph-agent).
        resolved_thread_id = thread_id or (threads[0]["thread_id"] if threads else None)
        context = await _fetch_context(client, resolved_thread_id) if resolved_thread_id else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "llama": {"metrics": llama_metrics, "slots": llama_slots},
        "threads": threads,
        "selected_thread_id": resolved_thread_id,
        "context": context,
        "gpu": gpu,
    }
