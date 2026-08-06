"""
Visual feedback (docs/briefs/campaign-visual-feedback.md, minimal subset):
the only change on this side of the wire is _call_mcp_tool forwarding
thread_id to mcp-client's /call so it can key its side-channel capture —
everything about image-block handling (_split_image_blocks) is untouched,
verified by mcp-client's own non-negotiable test
(services/mcp-client/tests/test_main.py) since the capture never re-enters
this service's response path at all.
"""

import json

import httpx
import pytest
import respx


@pytest.mark.asyncio
async def test_call_mcp_tool_forwards_thread_id():
    import app.graph as g

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://fake-mcp-client/call").mock(
            return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
        )
        async with httpx.AsyncClient() as client:
            await g._call_mcp_tool(client, "browser_navigate", {"url": "https://exemple.com"}, "thread-abc")

    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "tool": "browser_navigate",
        "arguments": {"url": "https://exemple.com"},
        "thread_id": "thread-abc",
    }


@pytest.mark.asyncio
async def test_call_mcp_tool_thread_id_defaults_to_none():
    """_fetch_verification_snapshot (app/graph.py) calls _call_mcp_tool
    with no thread_id in scope — must stay a valid, harmless call (mcp-
    client's _maybe_capture_visual no-ops on a missing thread_id)."""
    import app.graph as g

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://fake-mcp-client/call").mock(return_value=httpx.Response(200, json={"content": []}))
        async with httpx.AsyncClient() as client:
            await g._call_mcp_tool(client, "browser_snapshot", {})

    sent = json.loads(route.calls.last.request.content)
    assert sent["thread_id"] is None
