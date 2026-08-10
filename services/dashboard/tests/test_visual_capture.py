"""
GET /api/visual/{thread_id}: serves the single overwritten latest.jpg
mcp-client writes per thread (docs/briefs/campaign-visual-feedback.md).
Read-only, file-based — same "harness writes, dashboard reads" principle
as the campaign progress endpoints (see app/main.py's module docstring).
"""

import httpx
import pytest

# Minimal real JPEG bytes (1x1 pixel) — no Pillow dependency needed here
# (this service never decodes the image, only serves the bytes as-is).
_TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300030202020202"
    "03020202030303030406040404040408060605060909080a0a090809090a0c"
    "0f0c0a0b0e0b09090d110d0e0f101011100a0c12131210130f101010ffc900"
    "0b080001000101011100ffcc000600101005ffda0008010100003f00d2cf20"
    "ffd9"
)


def _client():
    import app.main as main_mod

    transport = httpx.ASGITransport(app=main_mod.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _write_jpeg(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_TINY_JPEG)


@pytest.mark.asyncio
async def test_visual_capture_serves_existing_file(monkeypatch, tmp_path):
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "VISUAL_CAPTURE_DIR", tmp_path)
    _write_jpeg(tmp_path / "thread-abc" / "latest.jpg")

    async with _client() as client:
        resp = await client.get("/api/visual/thread-abc")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.content == _TINY_JPEG


@pytest.mark.asyncio
async def test_visual_capture_404_when_missing(monkeypatch, tmp_path):
    """No capture yet for this thread (or CAMPAIGN_VISUAL_CAPTURE is off
    on the mcp-client side) — a 404, never a 500."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "VISUAL_CAPTURE_DIR", tmp_path)

    async with _client() as client:
        resp = await client.get("/api/visual/unknown-thread")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_visual_capture_404_when_directory_absent(monkeypatch, tmp_path):
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "VISUAL_CAPTURE_DIR", tmp_path / "does-not-exist")

    async with _client() as client:
        resp = await client.get("/api/visual/thread-abc")

    assert resp.status_code == 404
