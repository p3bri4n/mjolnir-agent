"""
Proactive OCR enrichment (effort 3, GhostDesk removal — see
app/graph.py:_maybe_enrich_with_ocr/_detect_visual_signal,
docs/history.md "EFFORT 3"). PROACTIVE_OCR_ENABLED disabled by default:
each test that exercises the mechanism activates it explicitly via
monkeypatch, same pattern as PLANNER_ENABLED (tests/test_plan_task.py).
"""

import httpx
import pytest
import respx

import app.audit_log as audit_log


@pytest.mark.asyncio
async def test_detect_visual_signal_stub_always_returns_none():
    """Documents the current stub state (effort 3's explicit next
    checkpoint, docs/briefs/update-plan.md) — must fail loudly the day
    this is implemented for real, as a reminder to write a real test."""
    import app.graph as g

    assert g._detect_visual_signal("") is None
    assert g._detect_visual_signal("- canvas") is None
    assert g._detect_visual_signal("anything at all") is None


@pytest.mark.asyncio
async def test_maybe_enrich_noop_when_disabled(monkeypatch, tmp_path):
    import app.graph as g

    monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(g, "PROACTIVE_OCR_ENABLED", False)
    result = {"content": [{"type": "text", "text": "- canvas"}]}

    async with httpx.AsyncClient() as client:
        out = await g._maybe_enrich_with_ocr(client, "browser_snapshot", result, "thread-ocr-off")

    assert out is result
    assert audit_log.read_entries("thread-ocr-off") == []


@pytest.mark.asyncio
async def test_maybe_enrich_logs_coverage_entry_when_enabled_no_signal(monkeypatch, tmp_path):
    """The day-one trigger-rate counter: an entry is logged on EVERY
    browser_* result while the flag is on, even when nothing fires — the
    stub never detects a signal, so this is the only reachable path
    today."""
    import app.graph as g

    monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(g, "PROACTIVE_OCR_ENABLED", True)
    result = {"content": [{"type": "text", "text": "Page ordinaire, rien de visuel."}]}

    with respx.mock(assert_all_called=False) as mock:
        async with httpx.AsyncClient() as client:
            out = await g._maybe_enrich_with_ocr(client, "browser_snapshot", result, "thread-ocr-nosignal")
    assert mock.calls == []

    assert out is result
    entries = audit_log.read_entries("thread-ocr-nosignal")
    ocr_entries = [e for e in entries if e.get("role") == "proactive_ocr"]
    assert len(ocr_entries) == 1
    assert ocr_entries[0]["content"] == {
        "tool": "browser_snapshot",
        "signal_detected": False,
        "signal_kind": None,
        "ocr_ran": False,
        "detections_count": 0,
        "chars_attached": 0,
    }


@pytest.mark.asyncio
async def test_maybe_enrich_attaches_ocr_text_when_signal_detected(monkeypatch, tmp_path):
    """Exercises the full enrichment path (signal -> screenshot -> OCR ->
    appended text) by forcing a detection — _detect_visual_signal itself
    is a stub (see the test above), this only tests what happens once it
    DOES return a signal, whenever it's implemented for real."""
    import app.graph as g

    monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(g, "PROACTIVE_OCR_ENABLED", True)
    monkeypatch.setattr(g, "_detect_visual_signal", lambda text: "canvas")
    result = {"content": [{"type": "text", "text": "- canvas"}]}

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://fake-mcp-client/call").mock(
            return_value=httpx.Response(
                200,
                json={"content": [{"type": "image", "data": "ZmFrZQ==", "mimeType": "image/png"}]},
            )
        )
        mock.post("http://fake-ocr-service/ocr").mock(
            return_value=httpx.Response(
                200,
                json=[{"text": "VP-1001", "confidence": 0.95}, {"text": "detail", "confidence": 0.6}],
            )
        )
        async with httpx.AsyncClient() as client:
            out = await g._maybe_enrich_with_ocr(client, "browser_snapshot", result, "thread-ocr-hit")

    assert out["content"] == [
        {"type": "text", "text": "- canvas"},
        {"type": "text", "text": "[OCR enrichment] VP-1001; detail"},
    ]
    # Original result dict is not mutated in place — a new dict is returned.
    assert result["content"] == [{"type": "text", "text": "- canvas"}]

    entries = audit_log.read_entries("thread-ocr-hit")
    ocr_entries = [e for e in entries if e.get("role") == "proactive_ocr"]
    assert len(ocr_entries) == 1
    assert ocr_entries[0]["content"] == {
        "tool": "browser_snapshot",
        "signal_detected": True,
        "signal_kind": "canvas",
        "ocr_ran": True,
        "detections_count": 2,
        "chars_attached": len("VP-1001; detail"),
    }


@pytest.mark.asyncio
async def test_maybe_enrich_ocr_service_failure_leaves_result_unchanged(monkeypatch, tmp_path):
    """Best-effort: a down/erroring ocr-service must never block the
    task, same philosophy as _fetch_verification_snapshot."""
    import app.graph as g

    monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(g, "PROACTIVE_OCR_ENABLED", True)
    monkeypatch.setattr(g, "_detect_visual_signal", lambda text: "canvas")
    result = {"content": [{"type": "text", "text": "- canvas"}]}

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://fake-mcp-client/call").mock(
            return_value=httpx.Response(
                200,
                json={"content": [{"type": "image", "data": "ZmFrZQ==", "mimeType": "image/png"}]},
            )
        )
        mock.post("http://fake-ocr-service/ocr").mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            out = await g._maybe_enrich_with_ocr(client, "browser_snapshot", result, "thread-ocr-fail")

    assert out == result

    entries = audit_log.read_entries("thread-ocr-fail")
    ocr_entries = [e for e in entries if e.get("role") == "proactive_ocr"]
    assert ocr_entries[0]["content"]["ocr_ran"] is False
