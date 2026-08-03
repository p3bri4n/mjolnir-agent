"""
Tests for the LangGraph graph (app/graph.py) and the HTTP endpoint
(app/main.py). All outgoing HTTP calls (LLM included) are intercepted by
respx, which patches at the httpx transport level without replacing the
httpx.AsyncClient class itself — unlike a naive monkeypatch, this doesn't
interfere with the openai SDK's internal client (see the README for the
detail of this pitfall).
"""

import base64
import json

import httpx
import pytest
import respx

from tests.fixtures.llm_sse import (
    multi_tool_call_response,
    reasoning_response,
    reasoning_response_combined_final_chunk,
    text_response,
    tool_call_response,
)


@pytest.fixture
def mock_side_services():
    """Mocks the side services (empty context, no skill) to isolate the LLM."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://fake-context-manager/retrieve").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        mock.post("http://fake-skill-manager/match").mock(
            return_value=httpx.Response(200, json={"skill": None})
        )
        mock.post("http://fake-mcp-client/call").mock(
            return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "42"}]})
        )
        mock.get("http://fake-mcp-client/tools/schema").mock(
            return_value=httpx.Response(200, json={"tools": []})
        )
        yield mock


def _sse_response(body):
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


CONFIG = {"configurable": {"thread_id": "test-thread"}}


@pytest.mark.asyncio
async def test_simple_response_without_tool_call(mock_side_services):
    import app.graph as g

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(text_response(["Bonjour", " !"]))
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Salut"}], "tool_iterations": 0, "approved": None}
    result = await g.agent_graph.ainvoke(state, CONFIG)

    assert result["messages"][-1].content == "Bonjour !"
    # human + final answer, nothing more: no system message added since
    # context and skill matching are both empty.
    assert len(result["messages"]) == 2


@pytest.mark.asyncio
async def test_tool_call_pauses_for_approval_without_calling_mcp_client(mock_side_services):
    """The require_approval node must block before any real call to mcp-client."""
    import app.graph as g

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://example.com"}'))
    )
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "42"}]})
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Question ?"}], "tool_iterations": 0, "approved": None}
    await g.agent_graph.ainvoke(state, CONFIG)

    snapshot = await g.agent_graph.aget_state(CONFIG)
    assert snapshot.next == ("require_approval",)
    assert mcp_route.call_count == 0


@pytest.mark.asyncio
async def test_auto_approved_tool_skips_require_approval(mock_side_services):
    """
    Reversible-tier tools (write_file, filesystem) must execute without
    going through require_approval.
    """
    import app.graph as g

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("write_file", "call_1", '{"path": "/workspace/x.txt", "content": "y"}')),
        _sse_response(text_response(["Écrit", "."])),
    ]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Écris ce fichier"}], "tool_iterations": 0, "approved": None}
    result = await g.agent_graph.ainvoke(state, CONFIG)

    snapshot = await g.agent_graph.aget_state(CONFIG)
    assert snapshot.next == ()  # no pause: the turn ran to completion
    assert mcp_route.call_count == 1
    assert result["messages"][-1].content == "Écrit."


@pytest.mark.asyncio
async def test_all_tier_read_tools_skip_approval_silently(mock_side_services):
    """
    A turn where ALL tool_calls are tier 1 (pure read, e.g.
    read_file/list_directory on the MCP side) must execute without ever
    going through require_approval.
    """
    import app.graph as g

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(
            multi_tool_call_response(
                [
                    ("read_file", "call_1", '{"path": "/workspace/x.txt"}'),
                    ("list_directory", "call_2", '{"path": "/workspace"}'),
                ]
            )
        ),
        _sse_response(text_response(["Terminé", "."])),
    ]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Regarde l'état"}], "tool_iterations": 0, "approved": None}
    result = await g.agent_graph.ainvoke(state, CONFIG)

    snapshot = await g.agent_graph.aget_state(CONFIG)
    assert snapshot.next == ()
    assert mcp_route.call_count == 2
    assert result["messages"][-1].content == "Terminé."


@pytest.mark.asyncio
async def test_unknown_tool_requires_approval(mock_side_services):
    """Default = the most restrictive tier: a tool never classified anywhere stays sensitive."""
    import app.graph as g

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(tool_call_response("some_never_seen_tool", "call_1", "{}"))
    )
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Fais un truc inédit"}], "tool_iterations": 0, "approved": None}
    await g.agent_graph.ainvoke(state, CONFIG)

    snapshot = await g.agent_graph.aget_state(CONFIG)
    assert snapshot.next == ("require_approval",)
    assert mcp_route.call_count == 0


@pytest.mark.asyncio
async def test_mixed_auto_and_manual_tools_still_requires_approval(mock_side_services):
    """
    A turn that mixes an auto-approved tool (read_file) and a sensitive
    tool (browser_navigate) must remain entirely subject to approval — no
    partial per-tool approval.
    """
    import app.graph as g

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(
            multi_tool_call_response(
                [
                    ("read_file", "call_1", '{"path": "/workspace/x.txt"}'),
                    ("browser_navigate", "call_2", '{"url": "http://example.com"}'),
                ]
            )
        )
    )
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Lis le fichier puis navigue"}], "tool_iterations": 0, "approved": None}
    await g.agent_graph.ainvoke(state, CONFIG)

    snapshot = await g.agent_graph.aget_state(CONFIG)
    assert snapshot.next == ("require_approval",)
    assert mcp_route.call_count == 0


@pytest.mark.asyncio
async def test_auto_approval_streak_limit_forces_human_checkin(mock_side_services, monkeypatch):
    """
    Defense in depth: past AUTO_APPROVAL_STREAK_LIMIT consecutive
    auto-approved turns, the next turn must go back through
    require_approval even if it contains ONLY normally auto-approved
    tools.
    """
    import app.graph as g

    monkeypatch.setattr(g, "AUTO_APPROVAL_STREAK_LIMIT", 2)

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("read_file", f"call_{i}", '{"path": "/workspace/x.txt"}')) for i in range(3)
    ] + [_sse_response(text_response(["Terminé", "."]))]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Relis en boucle"}], "tool_iterations": 0, "approved": None}
    await g.agent_graph.ainvoke(state, CONFIG)

    snapshot = await g.agent_graph.aget_state(CONFIG)
    # 2 auto-approved turns executed (read_file, iterations 1 and 2),
    # the 3rd is blocked in a pause despite read_file being tier read
    assert snapshot.next == ("require_approval",)
    assert snapshot.values["auto_approval_streak"] == 2
    assert mcp_route.call_count == 2

    # Once the human goes through require_approval, the counter is reset
    # to 0: the practice isn't blocked for good, just checkpointed with a
    # periodic human control point.
    await g.agent_graph.aupdate_state(CONFIG, {"approved": True})
    await g.agent_graph.ainvoke(None, CONFIG)
    snapshot = await g.agent_graph.aget_state(CONFIG)
    assert snapshot.next == ()
    assert snapshot.values["auto_approval_streak"] == 1  # reset to 0, then +1 for this executed turn
    assert mcp_route.call_count == 3


@pytest.mark.asyncio
async def test_max_tool_iterations_ends_loop_with_pending_tool_calls(mock_side_services, monkeypatch):
    """
    Non-regression: encountered in real usage with a rapid-fire
    auto-approved read loop — has_tool_calls forces the graph to end as
    soon as tool_iterations reaches MAX_TOOL_ITERATIONS, EVEN IF the
    model's last message still has a pending tool_calls. Without a check
    on the caller side (see app/main.py), this tool_calls is silently
    lost: the agent just seems to "stop" mid-task, with no error or
    approval pause explaining it.
    """
    import app.graph as g

    monkeypatch.setattr(g, "MAX_TOOL_ITERATIONS", 2)

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    # read_file is tier read: the call_llm -> auto_call_tools loop never
    # goes through a pause as long as the model keeps asking for more.
    route.side_effect = [
        _sse_response(tool_call_response("read_file", f"call_{i}", '{"path": "/workspace/x.txt"}')) for i in range(3)
    ]
    mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Relis en boucle"}], "tool_iterations": 0, "approved": None}
    result = await g.agent_graph.ainvoke(state, {**CONFIG, "recursion_limit": 50})

    snapshot = await g.agent_graph.aget_state(CONFIG)
    assert snapshot.next == ()  # the graph really did end, not paused
    assert snapshot.values["tool_iterations"] == 2
    last_message = result["messages"][-1]
    # the 3rd tool_call (read_file number 2) was never executed or approved
    assert last_message.tool_calls
    assert last_message.tool_calls[0]["name"] == "read_file"


@pytest.mark.asyncio
async def test_approval_resumes_and_calls_mcp_client(mock_side_services):
    import app.graph as g

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://example.com"}')),
        _sse_response(text_response(["Resultat", ": 42."])),
    ]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "42"}]})
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Question ? Site : http://example.com"}], "tool_iterations": 0, "approved": None}
    await g.agent_graph.ainvoke(state, CONFIG)

    await g.agent_graph.aupdate_state(CONFIG, {"approved": True})
    result = await g.agent_graph.ainvoke(None, CONFIG)

    assert mcp_route.call_count == 1
    assert result["messages"][-1].content == "Resultat: 42."

    tool_message = next(m for m in result["messages"] if getattr(m, "type", None) == "tool")
    payload = json.loads(tool_message.content)
    assert payload["content"][0]["text"] == "42"


@pytest.mark.asyncio
async def test_rejection_skips_mcp_client_and_synthesizes_refusal(mock_side_services):
    import app.graph as g

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("browser_evaluate", "call_1", '{"code": "document.title"}')),
        _sse_response(text_response(["Compris", ", annulé."])),
    ]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "42"}]})
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Question ?"}], "tool_iterations": 0, "approved": None}
    await g.agent_graph.ainvoke(state, CONFIG)

    await g.agent_graph.aupdate_state(CONFIG, {"approved": False})
    result = await g.agent_graph.ainvoke(None, CONFIG)

    assert mcp_route.call_count == 0
    tool_message = next(m for m in result["messages"] if getattr(m, "type", None) == "tool")
    payload = json.loads(tool_message.content)
    assert payload["error"] == "Rejeté par l'utilisateur"
    assert result["messages"][-1].content == "Compris, annulé."


@pytest.mark.asyncio
async def test_tool_call_loop_resolves_and_does_not_duplicate_messages(mock_side_services):
    """
    Non-régression du bug corrigé : les nœuds mutaient state['messages'] en
    place et retournaient l'état entier, ce qui faisait dupliquer les messages
    system/tool dans l'historique. Ce test échoue si la régression revient.
    Passe désormais par l'approbation (approved=True fourni dès le départ).
    """
    import app.graph as g

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://example.com"}')),
        _sse_response(text_response(["Resultat", ": 42."])),
    ]
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Question ? Site : http://example.com"}], "tool_iterations": 0, "approved": None}
    await g.agent_graph.ainvoke(state, CONFIG)
    await g.agent_graph.aupdate_state(CONFIG, {"approved": True})
    result = await g.agent_graph.ainvoke(None, CONFIG)

    # human, AI(tool_call), tool, AI(final): exactly 4, no duplicate
    assert len(result["messages"]) == 4
    assert result["messages"][-1].content == "Resultat: 42."

    # the ToolMessage's content must match mcp-client's mocked result
    tool_message = result["messages"][2]
    payload = json.loads(tool_message.content)
    assert payload["content"][0]["text"] == "42"


@pytest.mark.asyncio
async def test_tool_schema_from_mcp_client_is_bound_to_llm(mock_side_services):
    """
    Non-regression: ChatOpenAI used to be instantiated without ever
    calling bind_tools(), so the LLM simply had no idea the MCP tools
    (terminal/filesystem/git/browser/desktop-GhostDesk) existed — has_
    tool_calls()/require_approval() stayed dead code in real usage,
    whatever model was served. This test fails if the schema fetched from
    mcp-client (GET /tools/schema) is no longer forwarded to the LLM in
    the outgoing request.
    """
    import app.graph as g

    tool_schema = [
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": "Exécute une commande shell.",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            },
        }
    ]
    mock_side_services.get("http://fake-mcp-client/tools/schema").mock(
        return_value=httpx.Response(200, json={"tools": tool_schema})
    )
    llm_route = mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(text_response(["OK"]))
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Salut"}], "tool_iterations": 0, "approved": None}
    await g.agent_graph.ainvoke(state, CONFIG)

    sent_body = json.loads(llm_route.calls.last.request.content)
    # VERIFICATION_ENABLED disabled by default (see _get_bound_llm,
    # latency fix 1/2-ter): the schema is neither augmented with
    # constat_precedent nor completed with report_and_act — unchanged
    # behavior compared to before any latency fix.
    assert sent_body["tools"] == tool_schema


def test_bulk_check_directive_mentions_browser_extract_urls():
    """T1 investigation (see docs/history.md): the real blocker was an
    insufficient iteration budget facing info visible only on detail
    pages, never the listing — the instruction pushes toward
    browser_extract's bulk mode (urls parameter, mcp-client, TIER_READ)
    rather than page-by-page navigation or model-written browser_evaluate
    code (old instruction, TIER_SENSITIVE)."""
    import app.graph as g

    assert "browser_extract" in g.BULK_CHECK_DIRECTIVE
    assert "urls" in g.BULK_CHECK_DIRECTIVE
    assert "un seul appel" in g.BULK_CHECK_DIRECTIVE.lower()


@pytest.mark.asyncio
async def test_call_llm_system_message_includes_bulk_check_directive(mock_side_services):
    import app.graph as g

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(text_response(["OK"]))
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Salut"}], "tool_iterations": 0, "approved": None}
    await g.agent_graph.ainvoke(state, CONFIG)

    sent_body = json.loads(route.calls.last.request.content)
    system_content = sent_body["messages"][0]["content"]
    assert g.BULK_CHECK_DIRECTIVE in system_content


@pytest.mark.asyncio
async def test_tool_schema_augmented_with_constat_when_verification_enabled(mock_side_services, monkeypatch):
    """Latency fix 1/2-ter (see docs/history.md): when VERIFICATION_ENABLED
    is active, every real MCP tool gets constat_precedent as a required
    parameter (_inject_constat_param), and report_and_act is added as the
    sole fallback tool (turn with no real action)."""
    import app.graph as g

    monkeypatch.setattr(g, "VERIFICATION_ENABLED", True)

    tool_schema = [
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": "Exécute une commande shell.",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
            },
        }
    ]
    mock_side_services.get("http://fake-mcp-client/tools/schema").mock(
        return_value=httpx.Response(200, json={"tools": tool_schema})
    )
    llm_route = mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(text_response(["OK"]))
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Salut"}], "tool_iterations": 0, "approved": None}
    await g.agent_graph.ainvoke(state, CONFIG)

    sent_body = json.loads(llm_route.calls.last.request.content)
    sent_tools = sent_body["tools"]
    assert len(sent_tools) == 2
    run_command = next(t for t in sent_tools if t["function"]["name"] == "run_command")
    assert "constat_precedent" in run_command["function"]["parameters"]["properties"]
    assert "constat_precedent" in run_command["function"]["parameters"]["required"]
    assert "command" in run_command["function"]["parameters"]["required"]
    assert any(t["function"]["name"] == "report_and_act" for t in sent_tools)


@pytest.mark.asyncio
async def test_tool_image_result_becomes_multimodal_user_message(mock_side_services):
    """
    Non-regression: a tool's raw result (e.g.
    browser_take_screenshot, MCP format {"type": "image", "data":
    <base64>, "mimeType": ...}) used to be entirely json.dumps()'d into a
    ToolMessage, a role that only supports OpenAI-compatible text — the
    model therefore received an unreadable base64 blob, not an image,
    regardless of its vision capabilities. call_tools must now extract
    the image blocks and reinject them as a multimodal "user" message
    (image_url), the only role that supports them. WebP
    (browser_take_screenshot's default format) must additionally be
    re-converted to PNG: Ollama's image decoder (mtmd/llama.cpp)
    explicitly fails on it ("Failed to load image or audio file",
    verified under real conditions), PNG works.
    """
    import io

    from PIL import Image

    import app.graph as g

    webp_buf = io.BytesIO()
    Image.new("RGB", (2, 2), color="red").save(webp_buf, format="WEBP", lossless=True)
    webp_b64 = base64.b64encode(webp_buf.getvalue()).decode()

    # browser_take_screenshot then a final text answer: a single tool
    # round trip, so as not to depend on MAX_TOOL_ITERATIONS to end the
    # loop (browser_take_screenshot is tier read — a fixed return_value
    # would therefore loop indefinitely until hitting LangGraph's internal
    # recursion_limit, unrelated to what's being tested here).
    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("browser_take_screenshot", "call_1", "{}")),
        _sse_response(text_response(["Capture", " prise."])),
    ]
    mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(
            200,
            json={"content": [{"type": "image", "data": webp_b64, "mimeType": "image/webp"}]},
        )
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Capture le bureau"}], "tool_iterations": 0, "approved": None}
    result = await g.agent_graph.ainvoke(state, CONFIG)

    tool_message = next(m for m in result["messages"] if getattr(m, "type", None) == "tool")
    assert webp_b64 not in tool_message.content  # the base64 must no longer pollute the ToolMessage

    image_message = next(m for m in result["messages"] if getattr(m, "type", None) == "human" and isinstance(m.content, list))
    url = image_message.content[0]["image_url"]["url"]
    assert image_message.content[0]["type"] == "image_url"
    assert url.startswith("data:image/png;base64,")

    # round-trip: the payload must be a valid 2x2 red PNG, not just a
    # correct prefix
    png_bytes = base64.b64decode(url.split(",", 1)[1])
    decoded = Image.open(io.BytesIO(png_bytes))
    assert decoded.format == "PNG"
    assert decoded.size == (2, 2)
    assert decoded.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


@pytest.mark.asyncio
async def test_reasoning_field_is_folded_into_think_tags(mock_side_services):
    """
    Ollama (Qwen3+) streams reasoning in a "reasoning" field separate
    from "content", outside the standard OpenAI format: langchain-openai
    silently ignores it by default (_convert_delta_to_message_chunk only
    reads "content"/"tool_calls"/"function_call"). app/graph.py folds it
    into "content", wrapped in <think>...</think>, so Open WebUI renders
    it as a collapsible bubble. This test fails if this folding breaks or
    disappears.
    """
    import app.graph as g

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(
            reasoning_response(["12*7", "=84"], ["Ça fait", " 84."])
        )
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Combien font 12*7 ?"}], "tool_iterations": 0, "approved": None}
    result = await g.agent_graph.ainvoke(state, CONFIG)

    assert result["messages"][-1].content == "<think>12*7=84</think>\n\nÇa fait 84."


@pytest.mark.asyncio
async def test_reasoning_content_field_is_folded_into_think_tags(mock_side_services):
    """
    Non-regression: llama-server (turboquant-webp fork serving Qwen3.6)
    streams reasoning in a "reasoning_content" field, NOT "reasoning"
    like Ollama — DeepSeek-R1/OpenAI o1 convention, confirmed by a real
    streamed HTTP call against the real binary. Without handling this
    second field name, llama-server's reasoning silently disappeared (no
    error, just absent from the streamed content).
    """
    import app.graph as g

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(
            reasoning_response(["12*7", "=84"], ["Ça fait", " 84."], field="reasoning_content")
        )
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Combien font 12*7 ?"}], "tool_iterations": 0, "approved": None}
    result = await g.agent_graph.ainvoke(state, CONFIG)

    assert result["messages"][-1].content == "<think>12*7=84</think>\n\nÇa fait 84."


@pytest.mark.asyncio
async def test_reasoning_and_content_combined_in_same_chunk_still_yields_visible_answer(mock_side_services):
    """
    Non-regression (real bug observed with TabbyAPI/ExLlamaV3 serving
    Qwen3.6-27B EXL3, discovered in real usage via Open WebUI/API after
    the migration from llama-server): unlike llama-server/Ollama, which
    always separate reasoning and the final answer into distinct SSE
    chunks, TabbyAPI can group the end of the reasoning and the start of
    the answer in the SAME delta ({"reasoning_content": "...", "content":
    "..."}). The _convert_delta_with_reasoning patch used to overwrite
    chunk.content with the reasoning alone, silently discarding the real
    answer — the turn would end with no visible content, a symptom
    identical to the "empty answer" bug already documented for
    llama-server (tool_calls trapped in prose) but unrelated to it: here
    there isn't even a tool_calls, just a lost text answer.
    """
    import app.graph as g

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(
            reasoning_response_combined_final_chunk(["Aucune action requise."], "Bonjour !")
        )
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Dis bonjour."}], "tool_iterations": 0, "approved": None}
    result = await g.agent_graph.ainvoke(state, CONFIG)

    assert result["messages"][-1].content == "<think>Aucune action requise.</think>\n\nBonjour !"


@pytest.mark.asyncio
async def test_reasoning_without_trailing_content_still_closes_think_tag(mock_side_services):
    """Edge case: reasoning runs to the end with no final content after
    (never observed in practice with Qwen3, but call_llm must stay
    robust: the <think> tag must never stay open in persisted history)."""
    import app.graph as g

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(reasoning_response(["Hmm."], []))
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "..."}], "tool_iterations": 0, "approved": None}
    result = await g.agent_graph.ainvoke(state, CONFIG)

    assert result["messages"][-1].content == "<think>Hmm.</think>"


@pytest.mark.asyncio
async def test_node_with_no_new_message_does_not_raise(mock_side_services):
    """
    Non-regression: a node that produces no new message must explicitly
    return {"messages": []}, otherwise LangGraph raises
    InvalidUpdateError ("Must write to at least one of [...]").
    """
    import app.graph as g

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(text_response(["OK"]))
    )
    g.agent_graph = g.build_graph()

    state = {
        "messages": [{"role": "user", "content": "Question sans contexte ni skill"}],
        "tool_iterations": 0,
        "approved": None,
    }
    # must raise no exception (empty context + skill=None -> retrieve_context
    # and select_skill produce no new message)
    result = await g.agent_graph.ainvoke(state, CONFIG)
    assert result["messages"][-1].content == "OK"
