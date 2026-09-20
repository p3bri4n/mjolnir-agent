"""
Visual-only navigation mode (effort 8, docs/briefs/visual-navigation-only.md):
tool-schema filtering (_visual_navigation_filter/_get_tools_schema) and the
OCR-routing (_ocr_replace_image_blocks) that replaces every image block
under VISUAL_NAVIGATION_ONLY. mcp-client's own half of the fix
(_STABILIZE_AFTER_TOOLS swapping browser_snapshot for
browser_take_screenshot) is verified independently, see
services/mcp-client/tests/test_main.py.
"""

import httpx
import pytest
import respx


def test_visual_navigation_filter_hides_dom_tools_when_mode_active(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "VISUAL_NAVIGATION_ONLY", True)

    assert g._visual_navigation_filter("browser_snapshot") is False
    assert g._visual_navigation_filter("browser_click") is False
    assert g._visual_navigation_filter("browser_extract") is False
    assert g._visual_navigation_filter("browser_inspect") is False
    assert g._visual_navigation_filter("browser_mouse_click_xy") is True
    assert g._visual_navigation_filter("browser_navigate") is True
    assert g._visual_navigation_filter("browser_take_screenshot") is True


def test_visual_navigation_filter_hides_vision_tools_by_default(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "VISUAL_NAVIGATION_ONLY", False)

    assert g._visual_navigation_filter("browser_mouse_click_xy") is False
    assert g._visual_navigation_filter("browser_snapshot") is True
    assert g._visual_navigation_filter("browser_click") is True
    assert g._visual_navigation_filter("browser_navigate") is True


@pytest.mark.asyncio
async def test_get_tools_schema_applies_the_filter(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "VISUAL_NAVIGATION_ONLY", True)
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://fake-mcp-client/tools/schema").mock(
            return_value=httpx.Response(
                200,
                json={
                    "tools": [
                        {"type": "function", "function": {"name": "browser_snapshot"}},
                        {"type": "function", "function": {"name": "browser_mouse_click_xy"}},
                        {"type": "function", "function": {"name": "browser_navigate"}},
                    ]
                },
            )
        )
        schema = await g._get_tools_schema()

    names = {t["function"]["name"] for t in schema}
    assert names == {"browser_mouse_click_xy", "browser_navigate"}


def test_format_ocr_detections_empty_list():
    import app.graph as g

    assert g._format_ocr_detections([]) == "(aucun texte détecté par OCR sur cette capture)"


def test_format_ocr_detections_formats_bounding_box_and_confidence():
    import app.graph as g

    text = g._format_ocr_detections(
        [{"text": "Connexion", "x": 10, "y": 20, "width": 30, "height": 40, "confidence": 0.87}]
    )

    assert '[10,20,30,40] "Connexion" (confiance 0.87)' in text


@pytest.mark.asyncio
async def test_ocr_replace_image_blocks_converts_image_and_logs_coverage():
    import app.audit_log as audit_log
    import app.graph as g

    content = [
        {"type": "text", "text": "Page URL: https://exemple.com"},
        {"type": "image", "data": "ZmFrZQ==", "mimeType": "image/png"},
    ]
    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://fake-ocr-service/ocr").mock(
            return_value=httpx.Response(
                200, json=[{"text": "OK", "x": 1, "y": 2, "width": 3, "height": 4, "confidence": 0.9}]
            )
        )
        async with httpx.AsyncClient() as client:
            out = await g._ocr_replace_image_blocks(client, content, "thread-x", "browser_take_screenshot")

    assert out[0] == content[0]  # non-image block passed through untouched
    assert out[1]["type"] == "text"
    assert '"OK"' in out[1]["text"]

    entries = audit_log.read_entries("thread-x")
    assert len(entries) == 1
    assert entries[0]["role"] == "visual_navigation_only"
    assert entries[0]["content"] == {"tool": "browser_take_screenshot", "ocr_calls": 1}


@pytest.mark.asyncio
async def test_ocr_replace_image_blocks_degrades_on_ocr_failure_no_coverage_logged():
    import app.audit_log as audit_log
    import app.graph as g

    content = [{"type": "image", "data": "ZmFrZQ==", "mimeType": "image/png"}]
    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://fake-ocr-service/ocr").mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            out = await g._ocr_replace_image_blocks(client, content, "thread-y", "browser_take_screenshot")

    assert out == [{"type": "text", "text": "(OCR indisponible pour cette capture)"}]
    assert audit_log.read_entries("thread-y") == []


@pytest.mark.asyncio
async def test_ocr_replace_image_blocks_degrades_on_malformed_response_no_coverage_logged():
    """docs/resolved-bugs.md #63, caught live: ocr-service is a separate
    deployable — a stale image (missing x/y/width/height, the pre-Phase-0
    response shape) or any other malformed body must degrade this one
    image, never crash the whole turn."""
    import app.audit_log as audit_log
    import app.graph as g

    content = [{"type": "image", "data": "ZmFrZQ==", "mimeType": "image/png"}]
    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://fake-ocr-service/ocr").mock(
            return_value=httpx.Response(200, json=[{"text": "OK", "confidence": 0.9}])  # missing x/y/width/height
        )
        async with httpx.AsyncClient() as client:
            out = await g._ocr_replace_image_blocks(client, content, "thread-malformed", "browser_take_screenshot")

    assert out == [{"type": "text", "text": "(OCR indisponible pour cette capture)"}]
    assert audit_log.read_entries("thread-malformed") == []


@pytest.mark.asyncio
async def test_call_mcp_tool_routes_image_through_ocr_under_visual_navigation_only(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "VISUAL_NAVIGATION_ONLY", True)
    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://fake-mcp-client/call").mock(
            return_value=httpx.Response(
                200, json={"content": [{"type": "image", "data": "ZmFrZQ==", "mimeType": "image/png"}]}
            )
        )
        mock.post("http://fake-ocr-service/ocr").mock(
            return_value=httpx.Response(
                200, json=[{"text": "OK", "x": 0, "y": 0, "width": 1, "height": 1, "confidence": 0.5}]
            )
        )
        async with httpx.AsyncClient() as client:
            result, images = await g._call_mcp_tool(client, "browser_take_screenshot", {}, "thread-z")

    assert images == []  # never a multimodal user message under this mode
    assert result["content"][0]["type"] == "text"
    assert "OK" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_call_mcp_tool_unaffected_when_visual_navigation_only_is_off(monkeypatch):
    """Same tool call, flag off (default): unchanged behavior — the image
    still splits out for a multimodal message, ocr-service never called."""
    import app.graph as g

    monkeypatch.setattr(g, "VISUAL_NAVIGATION_ONLY", False)
    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://fake-mcp-client/call").mock(
            return_value=httpx.Response(
                200, json={"content": [{"type": "image", "data": "ZmFrZQ==", "mimeType": "image/png"}]}
            )
        )
        async with httpx.AsyncClient() as client:
            result, images = await g._call_mcp_tool(client, "browser_take_screenshot", {}, "thread-w")

    assert len(images) == 1
    assert images[0]["type"] == "image"
