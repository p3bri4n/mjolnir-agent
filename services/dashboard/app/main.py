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
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from app.gpu import parse_nvidia_smi_csv, run_nvidia_smi
from app.prometheus import extract_llama_metrics, normalize_slots

app = FastAPI(title="Dashboard")

LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL", "http://llama-server:8000")
LANGGRAPH_AGENT_URL = os.environ.get("LANGGRAPH_AGENT_URL", "http://langgraph-agent:8000")
ENABLE_GPU_STATS = os.environ.get("ENABLE_GPU_STATS", "false").lower() == "true"
# Live campaign progress (docs/briefs/B2-campaign-control.md, Part 1.3) —
# the harness (services/langgraph-agent) writes these files, this service
# only reads them (bind mounts, docker-compose.yml service "dashboard"):
# no HTTP coupling between the two, per the brief's design principle.
CAMPAIGNS_DIR = Path(os.environ.get("CAMPAIGNS_DIR", "/campaigns"))
DURATION_ESTIMATE_CACHE_PATH = Path(os.environ.get("DURATION_ESTIMATE_CACHE_PATH", "/duration-estimates.json"))
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


@app.get("/campaign", response_class=HTMLResponse)
async def campaign_page():
    return (_STATIC_DIR / "campaign.html").read_text(encoding="utf-8")


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


def _list_progress_files() -> list:
    if not CAMPAIGNS_DIR.is_dir():
        return []
    return sorted(CAMPAIGNS_DIR.glob("*.progress.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def _read_json_best_effort(path: Path) -> Optional[dict]:
    """A progress file mid-write (temp+rename in campaign_persistence.py is
    atomic, but the read can still race a concurrent rewrite on some
    filesystems) must never 500 the page — retried by the next poll."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _normalize_duration_estimate(value) -> dict:
    """Duplicated from campaign_persistence.normalize_duration_estimate
    (services/langgraph-agent/tests_integration/) rather than imported —
    this service has no access to that package (separate image, separate
    dependency set, per the brief's "harness writes, dashboard reads"
    decoupling)."""
    if isinstance(value, dict):
        return value
    return {"median": value, "min": value, "max": value, "n": 1}


def _compute_remaining_eta(state: dict, estimates: dict) -> dict:
    """Same logic as campaign_persistence.compute_remaining_eta — see that
    function's docstring for the "why per-task, never a global median"
    rationale (B2 Part 1.4)."""
    planned = state.get("planned", [])
    remaining = planned[len(state.get("completed", [])):]

    median_total = min_total = max_total = 0.0
    unreliable_tasks = set()
    for entry in remaining:
        task_id = entry["task_id"]
        raw = estimates.get(task_id)
        if raw is None:
            unreliable_tasks.add(task_id)
            continue
        entry = _normalize_duration_estimate(raw)
        median_total += entry["median"]
        min_total += entry["min"]
        max_total += entry["max"]

    return {
        "remaining_runs": len(remaining),
        "median_seconds": round(median_total, 1),
        "min_seconds": round(min_total, 1),
        "max_seconds": round(max_total, 1),
        "unreliable_task_count": len(unreliable_tasks),
        "reliable": not unreliable_tasks,
    }


def _campaign_counters(completed: list) -> dict:
    """Part 1.3's "running counters" — CuP so far, fabrications, approvals.
    Per-family score isn't included: v1 tasks (test_web_tasks.py) have no
    family concept, that's introduced by benchmark v2
    (docs/briefs/B3-benchmark-v2.md, not built yet)."""
    n = len(completed)
    successes = sum(1 for r in completed if r.get("status") == "success")
    return {
        "runs_completed": n,
        "cup_so_far": round(successes / n, 3) if n else None,
        "fabrications_total": sum(r.get("fabricated_urls_count", 0) for r in completed),
        "approvals_total": sum(r.get("approvals", 0) for r in completed),
    }


async def _fetch_audit_tail(client: httpx.AsyncClient, thread_id: str, limit: int = 15) -> list:
    try:
        resp = await client.get(f"{LANGGRAPH_AGENT_URL}/audit", params={"thread_id": thread_id})
        resp.raise_for_status()
        entries = resp.json().get("entries", [])
    except (httpx.HTTPError, ValueError):
        return []
    return entries[-limit:]


@app.get("/api/campaigns")
async def list_campaigns():
    """Summaries for the campaign picker — newest progress file first."""
    summaries = []
    for path in _list_progress_files():
        state = _read_json_best_effort(path)
        if state is None:
            continue
        summaries.append(
            {
                "campaign_id": state.get("campaign_id"),
                "label": state.get("label"),
                "started_at": state.get("started_at"),
                "total_runs": state.get("total_runs"),
                "runs_completed": len(state.get("completed", [])),
                "paused": state.get("paused", False),
            }
        )
    return {"campaigns": summaries}


@app.get("/api/campaign/{campaign_id}")
async def campaign_detail(campaign_id: str):
    path = CAMPAIGNS_DIR / f"{campaign_id}.progress.json"
    state = _read_json_best_effort(path)
    if state is None:
        raise HTTPException(status_code=404, detail="campagne inconnue ou fichier de progression illisible")

    estimates_payload = _read_json_best_effort(DURATION_ESTIMATE_CACHE_PATH) or {}
    eta = _compute_remaining_eta(state, estimates_payload.get("estimates", {}))
    counters = _campaign_counters(state.get("completed", []))

    current = state.get("current")
    audit_tail = []
    if current and current.get("thread_id"):
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            audit_tail = await _fetch_audit_tail(client, current["thread_id"])

    return {
        "state": state,
        "eta": eta,
        "counters": counters,
        "audit_tail": audit_tail,
        # Raw per-task cache (not just the remaining-runs sum in `eta`) —
        # the live panel needs THIS task's own expected duration to show
        # "elapsed / expected" (Part 1.4's "most useful figure in
        # practice"), separate from the aggregate ETA.
        "estimates": estimates_payload.get("estimates", {}),
    }
