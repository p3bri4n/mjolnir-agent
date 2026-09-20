"""
Visual-only navigation mode (effort 8, docs/briefs/visual-navigation-only.md):
tool-schema filtering (_visual_navigation_filter/_get_tools_schema), the
OCR-routing + layout reconstruction (_ocr_replace_image_blocks/
_reconstruct_layout) that replaces every image block under
VISUAL_NAVIGATION_ONLY, and browser_click_ref's resolution against
AgentState.visual_ref_map (points 3-4 of the optimization amendment).
mcp-client's own half (type_text, the extended _STABILIZE_AFTER_TOOLS) is
verified independently, see services/mcp-client/tests/test_main.py.
"""

import json

import httpx
import pytest
import respx
from langchain_core.messages import AIMessage, HumanMessage

from tests.fixtures.llm_sse import text_response


def _sse_response(body):
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


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
    # docs/resolved-bugs.md #64: read_file could read playwright-mcp's own
    # DOM snapshot artifact from the shared downloads volume.
    assert g._visual_navigation_filter("read_file") is False
    assert g._visual_navigation_filter("write_file") is False


def test_visual_navigation_filter_hides_vision_tools_by_default(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "VISUAL_NAVIGATION_ONLY", False)

    assert g._visual_navigation_filter("browser_mouse_click_xy") is False
    assert g._visual_navigation_filter("type_text") is False
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


# ─────────────────────────────────────────────────────────────────────────
# _reconstruct_layout (points 3-4): row clustering by vertical interval
# overlap, columns by left-to-right order within each row, stable refs.
# ─────────────────────────────────────────────────────────────────────────


def test_reconstruct_layout_empty_list():
    import app.graph as g

    text, ref_map = g._reconstruct_layout([])

    assert text == "(aucun texte détecté par OCR sur cette capture)"
    assert ref_map == {}


def test_reconstruct_layout_single_row_orders_columns_left_to_right():
    import app.graph as g

    detections = [
        {"text": "52000", "x": 216, "y": 151, "width": 44, "height": 18, "confidence": 0.99},
        {"text": "Julien Faure", "x": 10, "y": 151, "width": 110, "height": 18, "confidence": 0.99},
        {"text": "Ingénierie", "x": 122, "y": 151, "width": 92, "height": 18, "confidence": 0.99},
    ]

    text, ref_map = g._reconstruct_layout(detections)

    assert '[r0c0] "Julien Faure"' in text
    assert '[r0c1] "Ingénierie"' in text
    assert '[r0c2] "52000"' in text
    assert ref_map["r0c0"] == (10 + 110 / 2, 151 + 18 / 2)


def test_reconstruct_layout_separates_rows_with_no_vertical_overlap():
    """docs/resolved-bugs.md #65: fixture-hr-app's own repeated department
    values (e.g. "RH" x3) at genuinely different rows must never merge —
    non-overlapping y-intervals stay distinct rows regardless of shared
    text."""
    import app.graph as g

    detections = [
        {"text": "RH", "x": 120, "y": 217, "width": 32, "height": 20, "confidence": 0.99},
        {"text": "RH", "x": 120, "y": 349, "width": 28, "height": 19, "confidence": 0.99},
    ]

    _, ref_map = g._reconstruct_layout(detections)

    assert set(ref_map) == {"r0c0", "r1c0"}


def test_reconstruct_layout_clusters_same_row_despite_different_glyph_heights():
    """The exact ambiguity that motivated interval-overlap over y-center
    distance: two boxes on the same visual row can have different
    heights (hence different centers) if their glyphs render at
    different sizes."""
    import app.graph as g

    detections = [
        {"text": "Nom", "x": 10, "y": 129, "width": 110, "height": 20, "confidence": 0.99},
        {"text": "Salaire", "x": 216, "y": 127, "width": 50, "height": 23, "confidence": 0.99},
    ]

    _, ref_map = g._reconstruct_layout(detections)

    assert set(ref_map) == {"r0c0", "r0c1"}


# ─────────────────────────────────────────────────────────────────────────
# _ocr_replace_image_blocks / _call_mcp_tool: now return (content, ref_map)
# / (result, images, ref_map) respectively.
# ─────────────────────────────────────────────────────────────────────────


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
            out, ref_map = await g._ocr_replace_image_blocks(client, content, "thread-x", "browser_take_screenshot")

    assert out[0] == content[0]  # non-image block passed through untouched
    assert out[1]["type"] == "text"
    assert '"OK"' in out[1]["text"]
    assert ref_map == {"r0c0": (1 + 3 / 2, 2 + 4 / 2)}

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
            out, ref_map = await g._ocr_replace_image_blocks(client, content, "thread-y", "browser_take_screenshot")

    assert out == [{"type": "text", "text": "(OCR indisponible pour cette capture)"}]
    assert ref_map == {}
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
            out, ref_map = await g._ocr_replace_image_blocks(
                client, content, "thread-malformed", "browser_take_screenshot"
            )

    assert out == [{"type": "text", "text": "(OCR indisponible pour cette capture)"}]
    assert ref_map == {}
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
            result, images, ref_map = await g._call_mcp_tool(client, "browser_take_screenshot", {}, "thread-z")

    assert images == []  # never a multimodal user message under this mode
    assert result["content"][0]["type"] == "text"
    assert "OK" in result["content"][0]["text"]
    assert ref_map == {"r0c0": (0.5, 0.5)}


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
            result, images, ref_map = await g._call_mcp_tool(client, "browser_take_screenshot", {}, "thread-w")

    assert len(images) == 1
    assert images[0]["type"] == "image"
    assert ref_map == {}


# ─────────────────────────────────────────────────────────────────────────
# browser_click_ref (point 4): resolved in _execute_tool_calls against
# AgentState.visual_ref_map, never reaches mcp-client under its own name.
# ─────────────────────────────────────────────────────────────────────────


def _state_with_tool_call(tool_call, visual_ref_map=None):
    return {
        "messages": [
            HumanMessage(content="Clique sur la case ciblée"),
            AIMessage(content="", tool_calls=[tool_call]),
        ],
        "tool_iterations": 0,
        "session_grants": [],
        "visual_ref_map": visual_ref_map or {},
    }


@pytest.mark.asyncio
async def test_browser_click_ref_resolves_and_dispatches_resolved_coordinates():
    import app.graph as g

    state = _state_with_tool_call(
        {"id": "call_1", "name": "browser_click_ref", "args": {"ref": "r2c1"}},
        visual_ref_map={"r2c1": (55.0, 78.0)},
    )
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://fake-mcp-client/call").mock(
            return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
        )
        await g.auto_call_tools(state, {"configurable": {"thread_id": "click-ref-ok"}})

    sent = json.loads(route.calls.last.request.content)
    assert sent["tool"] == "browser_mouse_click_xy"
    assert sent["arguments"] == {"x": 55.0, "y": 78.0}


@pytest.mark.asyncio
async def test_browser_click_ref_audit_log_keeps_the_high_level_call():
    """Matches browser_extract's own precedent: the audit trail (and the
    approval-tier check) sees the call the model actually made, not its
    internal translation — the fix for "click at (412,338) isn't
    reviewable"."""
    import app.audit_log as audit_log
    import app.graph as g

    state = _state_with_tool_call(
        {"id": "call_1", "name": "browser_click_ref", "args": {"ref": "r2c1"}},
        visual_ref_map={"r2c1": (55.0, 78.0)},
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://fake-mcp-client/call").mock(
            return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
        )
        await g.auto_call_tools(state, {"configurable": {"thread_id": "click-ref-audit"}})

    entries = audit_log.read_entries("click-ref-audit")
    tool_call_entries = [e for e in entries if e.get("kind") is None]
    assert len(tool_call_entries) == 1
    assert tool_call_entries[0]["tool"] == "browser_click_ref"
    assert tool_call_entries[0]["arguments"] == {"ref": "r2c1"}


@pytest.mark.asyncio
async def test_browser_click_ref_unknown_ref_returns_error_without_dispatch():
    """No mock registered at all: if the guard failed to stop dispatch,
    the unmocked httpx call would raise and fail this test."""
    import app.graph as g

    state = _state_with_tool_call({"id": "call_1", "name": "browser_click_ref", "args": {"ref": "r9c9"}})
    with respx.mock(assert_all_called=False):
        result = await g.auto_call_tools(state, {"configurable": {"thread_id": "click-ref-unknown"}})

    tool_message = result["messages"][0]
    assert "ref inconnue" in tool_message["content"]


@pytest.mark.asyncio
async def test_execute_tool_calls_persists_fresh_ref_map_from_ocr_conversion(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "VISUAL_NAVIGATION_ONLY", True)
    state = _state_with_tool_call(
        {"id": "call_1", "name": "browser_navigate", "args": {"url": "https://exemple.com"}}
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://fake-mcp-client/call").mock(
            return_value=httpx.Response(
                200, json={"content": [{"type": "image", "data": "ZmFrZQ==", "mimeType": "image/png"}]}
            )
        )
        mock.post("http://fake-ocr-service/ocr").mock(
            return_value=httpx.Response(
                200, json=[{"text": "OK", "x": 10, "y": 20, "width": 30, "height": 15, "confidence": 0.9}]
            )
        )
        result = await g.auto_call_tools(state, {"configurable": {"thread_id": "ref-map-persist"}})

    assert result["visual_ref_map"] == {"r0c0": (25.0, 27.5)}


@pytest.mark.asyncio
async def test_execute_tool_calls_omits_visual_ref_map_when_unchanged(monkeypatch):
    """Same discipline as "plan" (merged-planning): every mode that never
    touches visual_ref_map must leave the key absent, not overwrite it
    with an unchanged copy."""
    import app.graph as g

    monkeypatch.setattr(g, "VISUAL_NAVIGATION_ONLY", False)
    state = _state_with_tool_call({"id": "call_1", "name": "browser_navigate", "args": {"url": "https://exemple.com"}})
    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://fake-mcp-client/call").mock(
            return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
        )
        result = await g.auto_call_tools(state, {"configurable": {"thread_id": "ref-map-unchanged"}})

    assert "visual_ref_map" not in result


# ─────────────────────────────────────────────────────────────────────────
# _get_bound_llm: browser_click_ref only exposed under VISUAL_NAVIGATION_ONLY
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_click_ref_tool_exposed_when_visual_navigation_only(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "VISUAL_NAVIGATION_ONLY", True)
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://fake-mcp-client/tools/schema").mock(return_value=httpx.Response(200, json={"tools": []}))
        llm_route = mock.post("http://fake-vllm/v1/chat/completions").mock(
            return_value=_sse_response(text_response(["OK"]))
        )
        state = {"messages": [HumanMessage(content="Salut")], "tool_iterations": 0}
        await g.call_llm(state, {"configurable": {"thread_id": "click-ref-schema-on"}})

    sent_tools = json.loads(llm_route.calls.last.request.content)["tools"]
    assert g._VISUAL_CLICK_REF_TOOL_NAME in {t["function"]["name"] for t in sent_tools}


@pytest.mark.asyncio
async def test_click_ref_tool_absent_by_default(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "VISUAL_NAVIGATION_ONLY", False)
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://fake-mcp-client/tools/schema").mock(return_value=httpx.Response(200, json={"tools": []}))
        llm_route = mock.post("http://fake-vllm/v1/chat/completions").mock(
            return_value=_sse_response(text_response(["OK"]))
        )
        state = {"messages": [HumanMessage(content="Salut")], "tool_iterations": 0}
        await g.call_llm(state, {"configurable": {"thread_id": "click-ref-schema-off"}})

    sent = json.loads(llm_route.calls.last.request.content)
    assert "tools" not in sent or g._VISUAL_CLICK_REF_TOOL_NAME not in {
        t["function"]["name"] for t in sent["tools"]
    }


# ─────────────────────────────────────────────────────────────────────────
# VISUAL_MODE_DIRECTIVE (point 6): static, empty outside this mode.
# ─────────────────────────────────────────────────────────────────────────


def test_visual_mode_directive_empty_by_default():
    import app.graph as g

    if not g.VISUAL_NAVIGATION_ONLY:
        assert g.VISUAL_MODE_DIRECTIVE == ""
